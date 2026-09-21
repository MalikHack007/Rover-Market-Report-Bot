"""Addendum D / D1 — build and publish the availability feed.

Pipeline: Cal.com probe slots -> {iso: bool} per day -> availability.json (the exact shape
the static page consumes). The payload is built and written locally so it is independently
testable; D2 wires the R2 upload via the `publish` callback.

Runs as a daemon thread inside rover-sms.service (settled, Addendum D §9) — started by
`start_thread()` from sms_main. Self-healing like the Cal.com poller: a failed cycle keeps
the last good feed and retries next tick, so a transient outage never publishes an
all-blocked lie.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import config
from ..calcom_client import CalcomClient, TransientCalcomError

log = logging.getLogger(__name__)

DEFAULT_UNKNOWN = "unknown"


def build_payload(days, generated_at, tz, horizon_days, default=DEFAULT_UNKNOWN):
    """Assemble the availability.json dict. `days` is {iso: bool} (True=available)."""
    return {
        "generated_at": generated_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": tz,
        "horizon_days": horizon_days,
        "default": default,
        "days": dict(sorted(days.items())),
    }


def _today_local(now, tz):
    """The sitter's local calendar date — a UTC evening is still 'today' locally."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tz)).date()
    except Exception:
        return now.date()


def refresh(client=None, now=None, tz=None, horizon_days=None,
            event_type_id=None):
    """Build the payload from live Cal.com data. Raises TransientCalcomError on outage."""
    client = client or CalcomClient()
    tz = tz or config.CALENDAR_TIMEZONE
    horizon_days = config.AVAIL_HORIZON_DAYS if horizon_days is None else horizon_days
    event_type_id = event_type_id or config.AVAIL_PROBE_EVENT_TYPE_ID
    now = now or datetime.now(timezone.utc)

    start_day = _today_local(now, tz)
    end_day = start_day + timedelta(days=horizon_days)
    days = client.available_days(event_type_id, start_day, end_day, tz)
    _warn_if_window_short(days, start_day, horizon_days)
    return build_payload(days, now, tz, horizon_days)


def _warn_if_window_short(days, start_day, horizon_days):
    """If Cal.com reports no availability anywhere near the horizon end, it may be capping
    its slot window below the horizon — in which case the tail is mislabeled 'away' rather
    than 'unknown'. Log it; the fix is the probe's future-booking limit (Addendum D §4).
    A genuinely full tail looks the same, so this is a warning, not an error.
    """
    avail = [d for d, ok in days.items() if ok]
    if not avail:
        log.warning("availability: ZERO available days over the %d-day horizon — probe "
                    "schedule/conflict calendars misconfigured, or a truly full window",
                    horizon_days)
        return
    last_avail = datetime.strptime(max(avail), "%Y-%m-%d").date()
    horizon_end = start_day + timedelta(days=horizon_days)
    gap = (horizon_end - last_avail).days
    if gap > 14:
        log.warning("availability: last available day %s is %d days short of horizon end %s "
                    "— Cal.com may be capping its slot window; check the probe's future "
                    "booking limit (Addendum D §4)", last_avail, gap, horizon_end)


def write_json(payload, path):
    """Write availability.json atomically (temp + os.replace) so a reader — or the D2
    uploader — never sees a half-written file."""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return path


def run_once(client=None, path=None, publish=None):
    """One refresh cycle: build -> write locally -> (optional) publish callback (D2 = R2).

    On a Cal.com outage, keep the last good feed (return None) rather than overwrite it
    with an all-blocked payload. Returns the payload on success.
    """
    path = path or config.AVAIL_LOCAL_JSON_PATH
    try:
        payload = refresh(client=client)
    except TransientCalcomError as e:
        log.warning("availability refresh skipped — Cal.com unreachable: %s", e)
        return None
    write_json(payload, path)
    if publish is not None:
        try:
            publish(payload)
        except Exception:
            log.exception("availability publish (upload) failed; local file still written")
    n = len(payload["days"])
    log.info("availability refreshed: %d days, %d available", n,
             sum(1 for v in payload["days"].values() if v))
    return payload


def run_loop(stop_event=None, client=None, path=None, publish=None, interval=None):
    """Thread body: refresh every AVAIL_REFRESH_SEC until stop_event is set. A crashed cycle
    is logged and retried next tick (the last good feed stays up)."""
    interval = config.AVAIL_REFRESH_SEC if interval is None else interval
    client = client or CalcomClient()
    log.info("availability publisher started (every %ds, %d-day horizon)",
             interval, config.AVAIL_HORIZON_DAYS)
    while not (stop_event is not None and stop_event.is_set()):
        try:
            run_once(client=client, path=path, publish=publish)
        except Exception:
            log.exception("availability cycle crashed; retrying next tick")
        if stop_event is not None:
            if stop_event.wait(interval):
                break
        else:
            time.sleep(interval)


def start_thread(publish=None):
    """Start the publisher as a daemon thread (called from sms_main). Guarded: if the probe
    isn't configured, log and no-op so the SMS service still starts cleanly."""
    if not config.AVAIL_PROBE_EVENT_TYPE_ID:
        log.warning("availability publisher disabled — AVAIL_PROBE_EVENT_TYPE_ID not set")
        return None
    t = threading.Thread(target=run_loop, kwargs={"publish": publish},
                         daemon=True, name="availability-publisher")
    t.start()
    return t
