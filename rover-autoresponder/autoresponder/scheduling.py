"""Addendum B / C1 — scheduling orchestrator.

On booking confirmation, place two PENDING placeholders on the ROVER calendar:
"{Pet} Drop-off (PENDING)" on the start date and "{Pet} Pick-up (PENDING)" on the end
date. They are transparent (free) so they never block the slots we're about to offer.

C2 adds the scheduling links, C3 the Cal.com poller that flips them to CONFIRMED.
"""
import logging
from datetime import date, datetime, timedelta

from . import config, store
from .calendar_client import GoogleCalendar, TRANSPARENT

log = logging.getLogger(__name__)

DROPOFF = "dropoff"
PICKUP = "pickup"
MEET_GREET = "meet_greet"

PENDING = "pending"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"

_LABEL = {DROPOFF: "Drop-off", PICKUP: "Pick-up", MEET_GREET: "Meet & Greet"}


def title(pet_name, kind, status):
    """'Archie Drop-off (PENDING)' — status is visible at a glance on the calendar."""
    pet = (pet_name or "Booking").strip()
    return f"{pet} {_LABEL.get(kind, kind)} ({status.upper()})"


# --- date handling -------------------------------------------------------
def parse_booking_date(text, today=None):
    """Parse 'MM/DD' or 'MM/DD/YYYY' into a date.

    The confirmation SMS omits the year ('from 09/01 to 09/06'). A booking is in the
    future, so a bare MM/DD resolves to its next occurrence — which also rolls a
    December→January range into the following year.
    """
    if not text:
        return None
    today = today or date.today()
    parts = text.strip().split("/")
    try:
        month, day = int(parts[0]), int(parts[1])
        if len(parts) >= 3:
            year = int(parts[2])
            if year < 100:
                year += 2000
            return date(year, month, day)
    except (ValueError, IndexError):
        return None
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue                     # e.g. 02/29 in a non-leap year
        if d >= today - timedelta(days=1):   # small grace for same-day confirmations
            return d
    return None


def default_slot(day, kind):
    """(start_iso, end_iso) for the PENDING placeholder — a 30-min block."""
    hh, mm = (config.DEFAULT_DROPOFF_TIME if kind != PICKUP
              else config.DEFAULT_PICKUP_TIME).split(":")
    start = datetime.combine(day, datetime.min.time()).replace(
        hour=int(hh), minute=int(mm))
    end = start + timedelta(minutes=config.SLOT_MINUTES)
    return start.isoformat(), end.isoformat()


# --- placement -----------------------------------------------------------
def create_pending_event(conn, thread_key, episode, kind, pet_name, target_day,
                         source="rover", calendar=None):
    """Create one PENDING placeholder. Idempotent on (thread_key, episode, kind)."""
    existing = store.get_scheduling_event(conn, thread_key, episode, kind)
    if existing:
        log.info("scheduling event already exists for %s/%s/%s — skipping",
                 thread_key, episode, kind)
        return existing[0]
    if not target_day:
        log.warning("no target date for %s %s — not creating an event", thread_key, kind)
        return None

    calendar = calendar or GoogleCalendar()
    start_iso, end_iso = default_slot(target_day, kind)
    summary = title(pet_name, kind, PENDING)
    gcal_id = calendar.create_event(
        summary, start_iso, end_iso,
        description=("Placeholder — the client hasn't picked a time yet.\n"
                     f"Thread: {thread_key} (episode {episode})"),
        transparency=TRANSPARENT)
    if not gcal_id:
        # Never fail silently: an un-placed booking is invisible to you.
        try:
            from . import telegram_notify
            telegram_notify.send_alert(
                f"Could not create calendar event: {summary} on {target_day}. "
                "The booking is NOT on your calendar.")
        except Exception:
            log.exception("alert failed")
        return None

    return store.add_scheduling_event(
        conn, thread_key=thread_key, episode=episode, kind=kind, source=source,
        target_date=target_day.isoformat(), gcal_event_id=gcal_id, status=PENDING)


def on_booking_confirmed(conn, thread_key, pet_name, start_text, end_text,
                         source="rover", calendar=None, today=None):
    """Booking confirmed -> place drop-off and pick-up placeholders.

    Dates arrive as MM/DD (SMS, no year) or MM/DD/YYYY. Same-day bookings (day care)
    still get two events: one drop-off, one pick-up.
    """
    episode = store.get_episode(conn, thread_key)
    start_day = parse_booking_date(start_text, today=today)
    end_day = parse_booking_date(end_text, today=today) or start_day
    if start_day and end_day and end_day < start_day:
        end_day = start_day               # defensive: never place a pick-up before drop-off

    created = []
    for kind, day in ((DROPOFF, start_day), (PICKUP, end_day)):
        ev_id = create_pending_event(conn, thread_key, episode, kind, pet_name, day,
                                     source=source, calendar=calendar)
        if ev_id:
            created.append(ev_id)
    if created:
        log.info("placed %d PENDING event(s) for %s (%s) %s -> %s",
                 len(created), thread_key, pet_name, start_day, end_day)
    return created
