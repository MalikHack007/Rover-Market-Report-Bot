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


# --- Addendum B / C2: scheduling links -----------------------------------
def _slug(kind):
    return {DROPOFF: config.CALCOM_EVENT_DROPOFF,
            PICKUP: config.CALCOM_EVENT_PICKUP,
            MEET_GREET: config.CALCOM_EVENT_MEETGREET}.get(kind, kind)


def build_link(kind, target_date=None, owner_name=None, ref=None):
    """Build a Cal.com booking link.

    Params carried:
      date/month  — pre-selects the booking's day (drop-off/pick-up are date-locked;
                    meet-and-greet is an open range so it gets no date).
      name        — prefills the attendee, which also helps C3 match the booking back.
      ref         — our scheduling_events.id. Cal.com may or may not echo custom params
                    back through its API, so C3 treats this as a bonus and falls back to
                    matching on (event type + date + attendee).
    """
    from urllib.parse import urlencode

    if not config.CALCOM_USERNAME:
        log.warning("CALCOM_USERNAME not set — cannot build a scheduling link")
        return None
    base = f"{config.CALCOM_BASE_URL.rstrip('/')}/{config.CALCOM_USERNAME}/{_slug(kind)}"
    params = {}
    if target_date and kind != MEET_GREET:
        params["date"] = target_date
        params["month"] = target_date[:7]
    if owner_name:
        params["name"] = owner_name
    if ref:
        params["metadata[ref]"] = str(ref)
    return f"{base}?{urlencode(params)}" if params else base


def ensure_links(conn, thread_key, episode, owner_name=None):
    """Generate and persist links for this booking's events. Returns {kind: url}."""
    links = {}
    for kind in (DROPOFF, PICKUP):
        row = store.get_scheduling_event(conn, thread_key, episode, kind)
        if not row:
            continue
        ev_id, _status, target_date, _sched, _gcal, existing = row
        url = existing or build_link(kind, target_date, owner_name, ref=ev_id)
        if url and not existing:
            store.update_scheduling_event(conn, ev_id, link_url=url)
        if url:
            links[kind] = url
    return links


def scheduling_message(owner_name, pet_name, links, start_date=None, end_date=None):
    """The fixed message that carries both links (no LLM call — wording is predictable)."""
    if DROPOFF not in links or PICKUP not in links:
        return None
    return config.SCHEDULING_LINKS_TEMPLATE.format(
        owner_name=owner_name or "there",
        pet_name=pet_name or "your pup",
        start_date=_pretty(start_date),
        end_date=_pretty(end_date),
        dropoff_link=links[DROPOFF],
        pickup_link=links[PICKUP],
    )


def _pretty(iso_date):
    """'2026-09-01' -> 'Tue, Sep 1'."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%a, %b %-d")
    except (ValueError, TypeError):
        return iso_date


def build_scheduling_draft(conn, thread_key, calendar=None):
    """After confirmation: ensure links exist and compose the message to send.

    Returns (text, links) or (None, {}) if links couldn't be built.
    """
    episode = store.get_episode(conn, thread_key)
    row = store.get_thread(conn, thread_key)
    owner, pet = (row[0], row[1]) if row else (None, None)
    links = ensure_links(conn, thread_key, episode, owner_name=owner)
    if not links:
        return None, {}
    d_row = store.get_scheduling_event(conn, thread_key, episode, DROPOFF)
    p_row = store.get_scheduling_event(conn, thread_key, episode, PICKUP)
    text = scheduling_message(owner, pet, links,
                              d_row[2] if d_row else None,
                              p_row[2] if p_row else None)
    return text, links


def ensure_meetgreet_link(conn, thread_key, owner_name=None, pet_name=None,
                          calendar=None):
    """Meet-and-greet: an OPEN range (next 7 days), not date-locked.

    Creates a PENDING placeholder only once the client actually books (there's no known
    date to place beforehand), so here we just mint and persist the link.
    """
    episode = store.get_episode(conn, thread_key)
    row = store.get_scheduling_event(conn, thread_key, episode, MEET_GREET)
    if row and row[5]:
        return row[5]                                  # already have a link
    ev_id = row[0] if row else store.add_scheduling_event(
        conn, thread_key=thread_key, episode=episode, kind=MEET_GREET,
        status=PENDING, target_date=None)
    url = build_link(MEET_GREET, owner_name=owner_name, ref=ev_id)
    if url:
        store.update_scheduling_event(conn, ev_id, link_url=url)
    return url