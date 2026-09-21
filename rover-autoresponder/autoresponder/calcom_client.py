"""Addendum B / C3 — Cal.com API access.

We POLL rather than take webhooks: Cal.com's cloud can't reach a LAN box behind home NAT,
and polling is self-healing (a missed poll is corrected by the next one, unlike a dropped
webhook). See Addendum B §4.1.

Cal.com's response shape varies across API versions, so `normalize()` is defensive: it
pulls the handful of fields we need and tolerates the rest being absent.
"""
import logging
import time
from datetime import datetime, timedelta

import requests

from . import config

log = logging.getLogger(__name__)

API_BASE = "https://api.cal.com/v2"

CANCELLED_STATES = {"cancelled", "canceled", "rejected"}


class TransientCalcomError(Exception):
    """The API couldn't be reached (timeout / connection error).

    Deliberately distinct from "no bookings": swallowing these and returning [] made an
    outage indistinguishable from a quiet day, which silently disabled the poller's
    consecutive-failure alerting.
    """


class CalcomClient:
    def __init__(self, api_key=None, base=API_BASE):
        self.api_key = api_key or config.CALCOM_API_KEY
        self.base = base

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}",
                "cal-api-version": "2024-08-13"}

    def list_bookings(self, after_iso=None, take=100):
        """Recent bookings.

        Returns [] on a hard failure. Raises TransientCalcomError if the network stalled
        so the caller can count consecutive failures — swallowing those made the poller's
        "N failures in a row" alert impossible to trigger.

        A brief stall is common on a flaky link, so retry a couple of times before
        giving up; polling is self-healing anyway (the next poll re-reads current state).
        """
        if not self.api_key:
            return []
        params = {"take": take, "sortStart": "desc"}
        if after_iso:
            params["afterStart"] = after_iso

        last_error = None
        for attempt in range(1, config.CALCOM_RETRIES + 1):
            try:
                r = requests.get(f"{self.base}/bookings", headers=self._headers(),
                                 params=params, timeout=(10, 30))
            except requests.exceptions.RequestException as e:
                last_error = e
                log.info("cal.com request stalled (%s), attempt %d/%d",
                         type(e).__name__, attempt, config.CALCOM_RETRIES)
                if attempt < config.CALCOM_RETRIES:
                    time.sleep(2 * attempt)
                continue
            if r.status_code != 200:
                log.error("cal.com bookings failed: %s %s", r.status_code, r.text[:200])
                return []
            try:
                payload = r.json()
            except Exception:
                log.exception("cal.com returned unparseable JSON")
                return []
            data = payload.get("data", payload)
            if isinstance(data, dict):
                data = data.get("bookings", []) or []
            return [normalize(b) for b in data if isinstance(b, dict)]

        raise TransientCalcomError(f"cal.com unreachable after "
                                   f"{config.CALCOM_RETRIES} attempts: {last_error}")

    # --- Addendum D: availability probe (open-slot counts) ---

    def available_slots_payload(self, event_type_id, start_day, end_day, tz):
        """Raw Cal.com slots response for the probe event type over [start_day, end_day].

        Same discipline as list_bookings: a network stall raises TransientCalcomError (so
        the publisher keeps the last good feed instead of publishing an all-blocked lie);
        an HTTP or parse error returns {}. The v2 slots shape has drifted across versions,
        so we try the current /slots spelling first and fall back to /slots/available; the
        reducer (slots_by_day) stays defensive about the response shape.
        """
        if not self.api_key or not event_type_id:
            return {}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "cal-api-version": config.AVAIL_SLOTS_API_VERSION}
        attempts = [
            (f"{self.base}/slots", {"eventTypeId": event_type_id,
                                    "start": start_day.isoformat(),
                                    "end": end_day.isoformat(), "timeZone": tz}),
            (f"{self.base}/slots/available",
             {"eventTypeId": event_type_id,
              "startTime": f"{start_day.isoformat()}T00:00:00Z",
              "endTime": f"{end_day.isoformat()}T23:59:59Z", "timeZone": tz}),
        ]
        last_error = None
        for url, params in attempts:
            for attempt in range(1, config.CALCOM_RETRIES + 1):
                try:
                    r = requests.get(url, headers=headers, params=params, timeout=(10, 30))
                except requests.exceptions.RequestException as e:
                    last_error = e
                    log.info("cal.com slots stalled (%s), attempt %d/%d",
                             type(e).__name__, attempt, config.CALCOM_RETRIES)
                    if attempt < config.CALCOM_RETRIES:
                        time.sleep(2 * attempt)
                    continue
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:
                        log.exception("cal.com slots returned unparseable JSON")
                        return {}
                # A 4xx on this spelling — try the fallback URL rather than retrying it.
                log.info("cal.com slots %s -> %s %s", url, r.status_code, r.text[:150])
                break
        if last_error is not None:
            raise TransientCalcomError(f"cal.com slots unreachable: {last_error}")
        return {}

    def available_day_counts(self, event_type_id, start_day, end_day, tz):
        """{'YYYY-MM-DD': slot_count} for days Cal.com reported (blocked days may be absent)."""
        payload = self.available_slots_payload(event_type_id, start_day, end_day, tz)
        return slots_by_day(payload, tz)

    def available_days(self, event_type_id, start_day, end_day, tz):
        """{'YYYY-MM-DD': bool} for EVERY day in [start_day, end_day].

        True  = Cal.com offers >=1 slot that day (available).
        False = zero slots — an all-day block on the ROVER or personal calendar (both are
                in the probe's conflict-check set), or a fully-booked day.

        A day Cal.com omits inside the queried window counts as zero => False, so the probe
        event type MUST permit bookings at least as far out as the horizon (Addendum D §4),
        or the far tail is mislabeled 'away' instead of 'unknown'.
        """
        counts = self.available_day_counts(event_type_id, start_day, end_day, tz)
        out = {}
        d = start_day
        while d <= end_day:
            key = d.isoformat()
            out[key] = counts.get(key, 0) > 0
            d += timedelta(days=1)
        return out


def _looks_like_date(s):
    try:
        datetime.strptime(str(s)[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _local_day(ts, tz):
    """Bucket a UTC timestamp into a local calendar day (Cal.com timestamps are UTC —
    Addendum B invariant; an evening slot must not roll onto the next day)."""
    if not ts:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).date().isoformat()
    except Exception:
        return str(ts)[:10]


def slots_by_day(payload, tz):
    """Reduce a Cal.com slots response to {'YYYY-MM-DD': slot_count}.

    Defensive across the shapes seen in the wild (D0 verified the live one; we keep the
    others so a version bump can't silently break the feed):
      A) data.slots = {"2026-09-20": [ {...}, ... ], ...}   (date-keyed dict, wrapped)
      B) data        = {"2026-09-20": [ {...}, ... ], ...}   (date-keyed dict, unwrapped)
      C) a flat list of slot objects with a start/time timestamp (bucketed by local day)
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    date_keyed = None
    if isinstance(data, dict):
        if isinstance(data.get("slots"), dict):
            date_keyed = data["slots"]
        elif data and all(_looks_like_date(k) for k in data.keys()):
            date_keyed = data
    if date_keyed is not None:
        return {k: (len(v) if isinstance(v, list) else 0)
                for k, v in date_keyed.items()}

    flat = data.get("slots") if isinstance(data, dict) else data
    counts = {}
    if isinstance(flat, list):
        for s in flat:
            ts = s.get("start") or s.get("time") or s.get("startTime") \
                if isinstance(s, dict) else None
            day = _local_day(ts, tz)
            if day:
                counts[day] = counts.get(day, 0) + 1
    return counts


def _first(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def normalize(b: dict) -> dict:
    """Flatten a Cal.com booking into the fields we care about."""
    attendees = b.get("attendees") or []
    attendee = attendees[0] if attendees and isinstance(attendees[0], dict) else {}
    event_type = b.get("eventType") or {}
    metadata = b.get("metadata") or {}
    status = str(_first(b, "status", "state", default="")).lower()
    return {
        "id": str(_first(b, "uid", "id", default="")),
        "status": status,
        "cancelled": status in CANCELLED_STATES,
        "start": _first(b, "start", "startTime"),
        "end": _first(b, "end", "endTime"),
        "event_type_slug": _first(event_type, "slug", default=_first(b, "eventTypeSlug")),
        "event_type_id": _first(event_type, "id", default=_first(b, "eventTypeId")),
        "attendee_name": _first(attendee, "name", default=""),
        # Our scheduling_events.id, if Cal.com echoes the link's metadata back.
        "ref": _first(metadata, "ref", default=None),
        "raw": b,
    }