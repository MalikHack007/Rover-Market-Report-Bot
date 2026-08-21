"""Addendum B / C3 — Cal.com API access.

We POLL rather than take webhooks: Cal.com's cloud can't reach a LAN box behind home NAT,
and polling is self-healing (a missed poll is corrected by the next one, unlike a dropped
webhook). See Addendum B §4.1.

Cal.com's response shape varies across API versions, so `normalize()` is defensive: it
pulls the handful of fields we need and tolerates the rest being absent.
"""
import logging
import time

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