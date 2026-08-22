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
from .calcom_client import TransientCalcomError
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


def _local_dt(iso_dt):
    """Cal.com returns UTC ('...T02:00:00.000Z'). Convert to the business timezone.

    Without this, an evening booking rolls past midnight UTC and looks like the NEXT
    day — e.g. 9:00 PM Central on Sep 7 is 02:00 UTC on Sep 8 — which made correct
    bookings fail the target-date check and fire a spurious "wrong day" alert.
    """
    if not iso_dt:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_dt).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(config.CALENDAR_TIMEZONE))
    except Exception:
        return dt.astimezone()          # fall back to the host's local zone


def _iso_date(iso_dt):
    """The calendar date of a booking, in the BUSINESS timezone (not UTC)."""
    dt = _local_dt(iso_dt)
    if dt is None:
        return str(iso_dt)[:10] if iso_dt else None
    return dt.date().isoformat()


def _pretty_local(iso_dt):
    dt = _local_dt(iso_dt)
    return dt.strftime("%a %b %d, %-I:%M %p %Z") if dt else str(iso_dt)


def _local_iso(iso_dt):
    """UTC from cal.com -> local RFC3339, so what we store and write to the calendar
    matches the time you actually see."""
    dt = _local_dt(iso_dt)
    return dt.isoformat() if dt else iso_dt


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
        except (TypeError, ValueError):
            row = None
        else:
            if row:
                return row
            # The link WAS bot-generated, but the entry it points at is gone — the
            # booking was cancelled/cleaned up on our side while the cal.com booking
            # lingered. Falling through to date matching here produced spurious
            # "ambiguous" alerts, so skip it quietly instead.
            log.info("cal.com booking %s references deleted entry #%s — ignoring",
                     booking.get("id"), ref)
            return None

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
        # Never guess — but never fail silently either. Without an alert, a client's
        # booked time would simply never reach your calendar and nothing would say so.
        log.warning("ambiguous cal.com booking %s (%d candidates) — not guessing",
                    booking.get("id"), len(exact))
        _alert_unmatched(conn, booking, exact, "matches several bookings")
    else:
        log.warning("cal.com booking %s (%s on %s) matched nothing",
                    booking.get("id"), kind, booked_date)
        _alert_unmatched(conn, booking, [], "doesn't match any pending booking")
    return None


def _alert_unmatched(conn, booking, candidates, why):
    """Tell you about a booking we couldn't place, with enough detail to fix it."""
    key = _UNMATCHED_KEY.format(booking.get("id"))
    if store.meta_exists(conn, key):
        return                                   # already reported this booking
    store.set_meta(conn, key, "1")               # record AFTER the check, not as part of it
    lines = [f"⚠️ A client booked a <b>{booking.get('event_type_slug')}</b> "
             f"({_pretty_local(booking.get('start'))}) but it {why}, so it is "
             f"<b>not on your calendar</b>.",
             f"Booked by: {booking.get('attendee_name') or 'unknown'}"]
    if not booking.get("ref"):
        lines.append("It carried no reference — booked from a bare cal.com link rather "
                     "than one the bot generated.")
    for r in candidates[:5]:
        t = store.get_thread(conn, r[1])
        lines.append(f"  • <code>#{r[0]}</code> {(t[1] if t else r[1])} "
                     f"{r[3]} on {r[6]}")
    if candidates:
        lines.append("Use <code>/links &lt;id&gt;</code> to check, or set the time "
                     "manually on the calendar.")
    _send_html("\n".join(lines))


def confirm(conn, event_row, booking, calendar=None):
    """Move the placeholder to the booked time and mark it CONFIRMED."""
    (ev_id, thread_key, episode, kind, source, status, target_date,
     scheduled_at, gcal_id, booking_ref, link_url) = event_row
    calendar = calendar or GoogleCalendar()

    start_iso = _local_iso(booking.get("start"))
    end_iso = _local_iso(booking.get("end"))
    if not start_iso:
        return False
    booked_date = _iso_date(booking.get("start"))

    # Date validation: the link pre-selects a day but doesn't strictly enforce it.
    if target_date and booked_date != target_date and kind != MEET_GREET:
        _alert(f"Client booked {kind} for {_pretty_local(start_iso)} "
               f"({booked_date}), but the booking's {kind} date is {target_date}. "
               "The calendar was updated to the booked time — check Rover.")

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
    log.info("CONFIRMED %s for %s at %s (%s)", kind, thread_key,
             _pretty_local(start_iso), start_iso)
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


_UNMATCHED_KEY = "sms_evt:calcom_unmatched:{}"


def _already_unmatched(conn, booking_id) -> bool:
    """Have we already reported this booking as unplaceable?

    The poll window covers all FUTURE bookings, so an orphan would otherwise be
    re-examined (and re-logged) every 60s forever.
    """
    return bool(booking_id) and store.meta_exists(
        conn, _UNMATCHED_KEY.format(booking_id))


def process_bookings(conn, bookings, calendar=None) -> int:
    """Apply a batch of Cal.com bookings. Idempotent — re-running changes nothing."""
    applied = 0
    for b in bookings:
        if _already_unmatched(conn, b.get("id")):
            continue                      # known orphan — reported once, now quiet
        if b.get("cancelled") and not b.get("ref"):
            # A cancelled booking we never placed is nothing to reconcile; don't let it
            # generate "unmatched" noise on every poll.
            continue
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
                   and scheduled_at == _local_iso(b.get("start")))
        if already:
            continue                                  # no-op: already applied
        applied += int(bool(confirm(conn, row, b, calendar)))
    return applied


def poll_once(conn, client=None, calendar=None) -> int:
    """One reconcile pass. Raises TransientCalcomError if the API was unreachable, so
    poll_loop can count consecutive failures and alert."""
    from .calcom_client import CalcomClient

    client = client or CalcomClient()
    after = (datetime.now() - timedelta(days=1)).isoformat()
    bookings = client.list_bookings(after_iso=after)
    if bookings is None:            # defensive: older clients returned None
        raise TransientCalcomError("cal.com bookings unreachable")
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
        except TransientCalcomError as e:
            failures += 1
            log.warning("cal.com poll failed (%d in a row): %s", failures, e)
            if failures == config.CALCOM_ALERT_AFTER:
                _alert(f"Cal.com polling has failed {failures} times in a row — clients' "
                       "booked times are NOT reaching your calendar.")
        except Exception:
            failures += 1
            log.exception("cal.com poll failed (%d in a row)", failures)
            if failures == config.CALCOM_ALERT_AFTER:
                _alert(f"Cal.com polling has failed {failures} times in a row — clients' "
                       "booked times are NOT reaching your calendar.")
        time.sleep(interval)


def _alert(text):
    """Plain-text alert (telegram_notify escapes it)."""
    try:
        from . import telegram_notify
        telegram_notify.send_alert(text)
    except Exception:
        log.exception("alert failed")


def _send_html(html):
    """Alert whose body is ALREADY HTML — send_alert would escape the tags."""
    try:
        from . import telegram_notify
        telegram_notify.send_message("⚠️ <b>Rover bot alert</b>\n" + html)
    except Exception:
        log.exception("alert failed")