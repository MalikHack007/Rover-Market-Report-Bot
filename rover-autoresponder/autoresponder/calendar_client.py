"""Addendum B / C1 — Google Calendar access for the ROVER calendar.

Reuses the existing OAuth token (now scoped for both Gmail and Calendar). All writes go
to GOOGLE_CALENDAR_ID, the dedicated ROVER calendar, which must live under the SAME
account that granted consent — the token can only see that account's calendars.

The concrete Google client sits behind the CalendarClient interface so scheduling logic
can be tested with a fake.
"""
import logging

from . import config

log = logging.getLogger(__name__)

# PENDING placeholders are marked transparent (free) so they never block the very slots
# we're offering — see Addendum B §6, the feedback-loop warning.
TRANSPARENT = "transparent"
OPAQUE = "opaque"


class CalendarClient:
    """Interface. Implementations must not raise on transient failure — return None."""

    def create_event(self, summary, start_iso, end_iso, description="",
                     transparency=TRANSPARENT):
        raise NotImplementedError

    def update_event(self, event_id, summary=None, start_iso=None, end_iso=None,
                     transparency=None):
        raise NotImplementedError

    def delete_event(self, event_id):
        raise NotImplementedError


class GoogleCalendar(CalendarClient):
    def __init__(self, service=None, calendar_id=None):
        self._service = service
        self.calendar_id = calendar_id or config.GOOGLE_CALENDAR_ID

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build
            from .gmail_client import get_credentials
            self._service = build("calendar", "v3", credentials=get_credentials(),
                                  cache_discovery=False)
        return self._service

    def _body(self, summary=None, start_iso=None, end_iso=None, description=None,
              transparency=None):
        body = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if start_iso is not None:
            body["start"] = {"dateTime": start_iso, "timeZone": config.CALENDAR_TIMEZONE}
        if end_iso is not None:
            body["end"] = {"dateTime": end_iso, "timeZone": config.CALENDAR_TIMEZONE}
        if transparency is not None:
            body["transparency"] = transparency
        return body

    def create_event(self, summary, start_iso, end_iso, description="",
                     transparency=TRANSPARENT):
        if not self.calendar_id:
            log.error("GOOGLE_CALENDAR_ID not set — cannot create '%s'", summary)
            return None
        body = self._body(summary, start_iso, end_iso, description, transparency)
        try:
            ev = self.service.events().insert(
                calendarId=self.calendar_id, body=body).execute()
        except Exception:
            log.exception("calendar create failed for %r", summary)
            return None
        log.info("calendar event created: %r at %s (%s)", summary, start_iso, ev.get("id"))
        return ev.get("id")

    def update_event(self, event_id, summary=None, start_iso=None, end_iso=None,
                     transparency=None):
        body = self._body(summary, start_iso, end_iso, None, transparency)
        if not body:
            return True
        try:
            self.service.events().patch(
                calendarId=self.calendar_id, eventId=event_id, body=body).execute()
        except Exception:
            log.exception("calendar update failed for %s", event_id)
            return False
        log.info("calendar event %s updated (%s)", event_id, ", ".join(body))
        return True

    def delete_event(self, event_id):
        try:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=event_id).execute()
        except Exception as e:
            # A 410/404 means it's already gone — that's the desired end state.
            if "410" in str(e) or "404" in str(e):
                log.info("calendar event %s already deleted", event_id)
                return True
            log.exception("calendar delete failed for %s", event_id)
            return False
        log.info("calendar event %s deleted", event_id)
        return True
