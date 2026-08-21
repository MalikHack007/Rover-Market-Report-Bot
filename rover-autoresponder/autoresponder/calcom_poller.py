"""Addendum B / C3 — reconcile Cal.com bookings into calendar events.

Each poll compares Cal.com's state against ours, so this doubles as the reconcile job
(§9): drift is corrected as a side effect rather than needing a separate task.

Transitions handled:
  new booking   -> PENDING placeholder moves to the chosen time, retitled (CONFIRMED),
                   and becomes OPAQUE (it's a real commitment now, so it should block
                   other bookings — unlike the transparent placeholder).
  reschedule    -> event moves again.
  cancellation  -> event reverts to a PENDING placeholder (the BOOKING still exists;
                   only the chosen time went away).
"""
import logging
from datetime import datetime, timedelta

from . import config, store
from .calendar_client import GoogleCalendar, OPAQUE, TRANSPARENT
from .scheduling import (
    CONFIRMED, DROPOFF, MEET_GREET, PENDING, PICKUP, default_slot, title,
)

log = logging.getLogger(__name__)

_SLUG_TO_KIND = {}


def _slug_map():
    global _SLUG_TO_KIND
    _SLUG_TO_KIND = {
        config.CALCOM_EVENT_DROPOFF: DROPOFF,
        config.CALCOM_EVENT_PICKUP: PICKUP,
        config.CALCOM_EVENT_MEETGREET: MEET_GREET,
    }
    return _SLUG_TO_KIND


def _iso_date(iso_dt):
    if not iso_dt:
        return None
    try:
        return datetime.fromisoformat(str(iso_dt).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(iso_dt)[:10]


def match_event(conn, booking):
    """Find the scheduling_event a Cal.com booking belongs to.

    Primary: the `ref` we embedded in the link (exact).
    Fallback: event kind + target date + pending status — used if Cal.com doesn't echo
    custom metadata back through its API, which we can't verify in advance.
    """
    ref = booking.get("ref")
    if ref:
        try:
            row = store.get_scheduling_event_by_id(conn, int(ref))
            if row:
                return row
        except (TypeError, ValueError):
            pass

    kind = _slug_map().get(booking.get("event_type_slug"))
    if not kind:
        return None
    booked_date = _iso_date(booking.get("start"))
    candidates = [r for r in store.list_scheduling_events(conn)
                  if r[3] == kind and r[5] == PENDING]
    exact = [r for r in candidates if r[6] == booked_date]
    if len(exact) == 1:
        return store.get_scheduling_event_by_id(conn, exact[0][0])
    if len(exact) > 1:
        name = (booking.get("attendee_name") or "").strip().lower()
        if name:
            named = [r for r in exact
                     if name.split()[0] in ((store.get_thread(conn, r[1]) or [""])[0]
                                            or "").lower()]
            if len(named) == 1:
                return store.get_scheduling_event_by_id(conn, named[0][0])
        log.warning("ambiguous cal.com booking %s (%d candidates) — not guessing",
                    booking.get("id"), len(exact))
    return None


def confirm(conn, event_row, booking, calendar=None):
    """Move the placeholder to the booked time and mark it CONFIRMED."""
    (ev_id, thread_key, episode, kind, source, status, target_date,
     scheduled_at, gcal_id, booking_ref, link_url) = event_row
    calendar = calendar or GoogleCalendar()

    start_iso, end_iso = booking.get("start"), booking.get("end")
    if not start_iso:
        return False
    booked_date = _iso_date(start_iso)

    # Date validation: the link pre-selects a day but doesn't strictly enforce it.
    if target_date and booked_date != target_date and kind != MEET_GREET:
        _alert(f"Client booked {kind} on {booked_date}, but the booking is on "
               f"{target_date}. Check Rover — the calendar was updated to the booked "
               f"date.")

    row = store.get_thread(conn, thread_key)
    pet = row[1] if row else None
    summary = title(pet, kind, CONFIRMED)

    if gcal_id:
        ok = calendar.update_event(gcal_id, summary=summary, start_iso=start_iso,
                                   end_iso=end_iso, transparency=OPAQUE)
        if not ok:
            _alert(f"Could not update the calendar event for {summary}.")
            return False
    else:
        # Meet-and-greet has no placeholder until a date exists — create it now.
        gcal_id = calendar.create_event(summary, start_iso, end_iso,
                                        description=f"Thread: {thread_key}",
                                        transparency=OPAQUE)
        if not gcal_id:
            _alert(f"Could not create the calendar event for {summary}.")
            return False

    store.update_scheduling_event(
        conn, ev_id, status=CONFIRMED, scheduled_at=start_iso, gcal_event_id=gcal_id,
        booking_ref=booking.get("id"),
        target_date=target_date or booked_date)
    log.info("CONFIRMED %s for %s at %s", kind, thread_key, start_iso)
    return True


def revert_to_pending(conn, event_row, calendar=None):
    """Client cancelled their slot: back to a transparent placeholder on the target date."""
    (ev_id, thread_key, episode, kind, source, status, target_date,
     scheduled_at, gcal_id, booking_ref, link_url) = event_row
    calendar = calendar or GoogleCalendar()
    row = store.get_thread(conn, thread_key)
    pet = row[1] if row else None
    summary = title(pet, kind, PENDING)

    if target_date:
        day = datetime.fromisoformat(target_date).date()
        start_iso, end_iso = default_slot(day, kind)
        calendar.update_event(gcal_id, summary=summary, start_iso=start_iso,
                              end_iso=end_iso, transparency=TRANSPARENT)
    else:
        calendar.delete_event(gcal_id)
        gcal_id = None

    store.update_scheduling_event(conn, ev_id, status=PENDING, scheduled_at=None,
                                  booking_ref=None, gcal_event_id=gcal_id)
    log.info("reverted %s for %s to PENDING (client cancelled their slot)",
             kind, thread_key)
    _alert(f"{pet or 'A client'} cancelled their {kind} time. The slot is open again.")
    return True


def process_bookings(conn, bookings, calendar=None) -> int:
    """Apply a batch of Cal.com bookings. Idempotent — re-running changes nothing."""
    applied = 0
    for b in bookings:
        row = match_event(conn, b)
        if not row:
            continue
        (ev_id, _tk, _ep, _kind, _src, status, _td, scheduled_at,
         _gid, booking_ref, _link) = row

        if b.get("cancelled"):
            if status == CONFIRMED and booking_ref == b.get("id"):
                applied += int(bool(revert_to_pending(conn, row, calendar)))
            continue

        already = (status == CONFIRMED and booking_ref == b.get("id")
                   and scheduled_at == b.get("start"))
        if already:
            continue                                  # no-op: already applied
        applied += int(bool(confirm(conn, row, b, calendar)))
    return applied


def poll_once(conn, client=None, calendar=None) -> int:
    from .calcom_client import CalcomClient

    client = client or CalcomClient()
    after = (datetime.now() - timedelta(days=1)).isoformat()
    bookings = client.list_bookings(after_iso=after)
    if not bookings:
        return 0
    return process_bookings(conn, bookings, calendar)


def poll_loop(conn, stop_event=None, interval=None):
    import time

    interval = interval or config.CALCOM_POLL_SECONDS
    log.info("cal.com poller active (%ss)", interval)
    failures = 0
    while not (stop_event and stop_event.is_set()):
        try:
            poll_once(conn)
            failures = 0
        except Exception:
            failures += 1
            log.exception("cal.com poll failed (%d in a row)", failures)
            if failures == 5:
                _alert("Cal.com polling has failed 5 times in a row — booked times are "
                       "not reaching your calendar.")
        time.sleep(interval)


def _alert(text):
    try:
        from . import telegram_notify
        telegram_notify.send_alert(text)
    except Exception:
        log.exception("alert failed")
