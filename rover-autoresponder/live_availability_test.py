#!/usr/bin/env python3
"""Addendum D / D0 — prove the availability mechanism against real Cal.com.

Run DELIBERATELY (not in the pytest suite). This talks to the live Cal.com API.

WHAT IT PROVES
--------------
The whole feature rests on one assumption (Addendum D §11):

    A day is AVAILABLE  <=>  Cal.com offers >=1 slot on the probe event type.
    Zero slots          <=>  an all-day block on EITHER the ROVER or the personal
                             calendar (or a fully-booked day) => UNAVAILABLE.

So D0 does not "pass/fail" on its own — it prints Cal.com's per-day open-slot counts for
the probe event type so YOU can confirm the cross-calendar block actually zeroes a day.

SETUP (once)
------------
1. In Cal.com create an event type used ONLY as a probe (never publicized), e.g.
   `availability-probe`:
     - "Check for conflicts" MUST include BOTH the ROVER and your personal calendar.
     - Availability schedule: a WIDE daily window every day (e.g. 08:00-20:00, 7 days) so
       only an ALL-DAY block can zero a day out (a single mid-day event must not).
     - Short duration (e.g. 30 min), NO buffers — we only care whether the count is > 0.
2. Put its numeric id in .env as  AVAIL_PROBE_EVENT_TYPE_ID=<id>
   (Cal.com -> the event type -> its URL/settings show the id.)
3. Make sure CALCOM_API_KEY and CALENDAR_TIMEZONE are set in .env.

THE PROCEDURE (this is the actual test)
---------------------------------------
  a. Run:  python live_availability_test.py
     Note a day that shows AVAILABLE (count > 0).
  b. On your ROVER calendar, add an ALL-DAY event on that day. Re-run.
     -> that day must flip to BLOCKED (0 slots).  [proves ROVER half]
  c. Remove it. On your PERSONAL calendar, add an ALL-DAY event on a free day. Re-run.
     -> that day must flip to BLOCKED (0 slots).  [proves the personal-calendar half —
        the new reliance]
  d. Remove it. Confirm the day returns to AVAILABLE.

If (b) and (c) both flip the day to 0, D0 is proven and D1 (the publisher) can proceed.
If a block does NOT zero the day, STOP: the probe's conflict-calendars or its schedule
window is misconfigured (§4/§10) — fix that before building anything else.

  --days N   how many days ahead to probe (default 30; the real publisher uses 90)
  --raw      dump the raw JSON of the first response (to learn/verify the shape)
"""
import argparse
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta

import requests

# Importing the package applies the IPv4 preference (netprefs) and load_dotenv() via config
# — required on the bridged VM or Cal.com calls stall ~16s (Addendum B invariant).
from autoresponder import config

API_BASE = "https://api.cal.com/v2"


def _headers():
    return {
        "Authorization": f"Bearer {config.CALCOM_API_KEY}",
        "cal-api-version": config.AVAIL_SLOTS_API_VERSION,
    }


def fetch_slots(event_type_id, start_day, end_day, tz):
    """Hit Cal.com's slots endpoint. Returns (raw_json, used_url, used_params).

    The v2 slots shape has drifted across versions, so we try the current endpoint first
    and fall back to the older `/slots/available` spelling. D0 is exploratory on purpose —
    once the real shape is known here it gets hardened into calcom_client.available_days().
    """
    attempts = [
        # (url, params) — newest first.
        (f"{API_BASE}/slots", {
            "eventTypeId": event_type_id,
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "timeZone": tz,
        }),
        (f"{API_BASE}/slots/available", {
            "eventTypeId": event_type_id,
            "startTime": f"{start_day.isoformat()}T00:00:00Z",
            "endTime": f"{end_day.isoformat()}T23:59:59Z",
            "timeZone": tz,
        }),
    ]
    last = None
    for url, params in attempts:
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=(10, 30))
        except requests.exceptions.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            continue
        if r.status_code == 200:
            try:
                return r.json(), url, params
            except ValueError:
                last = f"200 but unparseable JSON from {url}"
                continue
        last = f"{r.status_code} from {url}: {r.text[:200]}"
    raise SystemExit(f"[D0] slots request failed. Last error:\n    {last}\n"
                     "Check CALCOM_API_KEY, AVAIL_PROBE_EVENT_TYPE_ID, and "
                     "AVAIL_SLOTS_API_VERSION.")


def _slots_by_day(payload, tz):
    """Reduce whatever Cal.com returned into {‘YYYY-MM-DD’: slot_count}.

    Handles the two shapes seen in the wild:
      A) data.slots = {"2026-09-20": [ {...}, ... ], ...}         (date-keyed)
      B) data        = {"2026-09-20": [ {...}, ... ], ...}         (date-keyed, no wrapper)
      C) a flat list of slot objects with a start/time timestamp   (bucket by local day)
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    # Shape A/B: a dict keyed by date string -> list of slots.
    date_keyed = None
    if isinstance(data, dict):
        if isinstance(data.get("slots"), dict):
            date_keyed = data["slots"]
        elif data and all(_looks_like_date(k) for k in data.keys()):
            date_keyed = data
    if date_keyed is not None:
        return OrderedDict(
            (k, len(v) if isinstance(v, list) else 0)
            for k, v in sorted(date_keyed.items())
        )

    # Shape C: a flat list of slot objects -> bucket by local calendar day.
    flat = data.get("slots") if isinstance(data, dict) else data
    counts = {}
    if isinstance(flat, list):
        for s in flat:
            ts = None
            if isinstance(s, dict):
                ts = s.get("start") or s.get("time") or s.get("startTime")
            day = _local_day(ts, tz)
            if day:
                counts[day] = counts.get(day, 0) + 1
    return OrderedDict(sorted(counts.items()))


def _looks_like_date(s):
    try:
        datetime.strptime(str(s)[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _local_day(ts, tz):
    """Bucket a UTC timestamp into a local calendar day (Addendum B: Cal.com is UTC)."""
    if not ts:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).date().isoformat()
    except Exception:
        return str(ts)[:10]  # fall back to the date prefix if tz conversion fails


def main():
    ap = argparse.ArgumentParser(description="D0 — Cal.com availability probe check")
    ap.add_argument("--days", type=int, default=30, help="days ahead to probe (default 30)")
    ap.add_argument("--raw", action="store_true", help="dump raw JSON of the response")
    args = ap.parse_args()

    if not config.CALCOM_API_KEY:
        raise SystemExit("[D0] CALCOM_API_KEY not set in .env")
    if not config.AVAIL_PROBE_EVENT_TYPE_ID:
        raise SystemExit("[D0] AVAIL_PROBE_EVENT_TYPE_ID not set in .env — create the probe "
                         "event type first (see this file's docstring, SETUP).")

    tz = config.CALENDAR_TIMEZONE
    start_day = date.today()
    end_day = start_day + timedelta(days=args.days)

    print(f"[D0] probe event type = {config.AVAIL_PROBE_EVENT_TYPE_ID}")
    print(f"[D0] window = {start_day} .. {end_day}  ({args.days} days)  tz={tz}")
    print(f"[D0] api-version header = {config.AVAIL_SLOTS_API_VERSION}\n")

    payload, url, params = fetch_slots(
        config.AVAIL_PROBE_EVENT_TYPE_ID, start_day, end_day, tz)
    print(f"[D0] hit: {url}\n")

    if args.raw:
        import json
        print("----- RAW JSON -----")
        print(json.dumps(payload, indent=2)[:4000])
        print("----- END RAW -----\n")

    by_day = _slots_by_day(payload, tz)
    if not by_day:
        print("[D0] Cal.com returned ZERO slots for the whole window.")
        print("     Either every day is blocked (unlikely), or the probe's availability "
              "schedule / conflict calendars are misconfigured, or the response shape is "
              "new — re-run with --raw to inspect it.")
        sys.exit(2)

    # Walk every day in the window so BLOCKED days (absent from the response) show too.
    print(f"{'DATE':<12} {'SLOTS':>5}  STATUS")
    print("-" * 34)
    avail = blocked = 0
    d = start_day
    while d <= end_day:
        key = d.isoformat()
        count = by_day.get(key, 0)
        status = "AVAILABLE" if count > 0 else "BLOCKED  (0 slots)"
        if count > 0:
            avail += 1
        else:
            blocked += 1
        print(f"{key:<12} {count:>5}  {status}")
        d += timedelta(days=1)

    print("-" * 34)
    print(f"[D0] {avail} available / {blocked} blocked over {args.days + 1} days.")
    print("\nNow run the PROCEDURE in this file's docstring: add an ALL-DAY event on a\n"
          "currently-AVAILABLE day — first on ROVER, then (separately) on your PERSONAL\n"
          "calendar — and confirm each flips that day to BLOCKED (0 slots). That is D0.")


if __name__ == "__main__":
    main()
