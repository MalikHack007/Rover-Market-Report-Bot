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
    """Create one PENDING placeholder. Idempotent on (thread_key, episode, kind), and
    RACE-SAFE across processes: the DB row is CLAIMED before the calendar event is created,
    so two near-simultaneous confirms — the SMS marker (rover-sms) and the confirmation email
    (rover-email-fallback), which land within a second and run in different processes — can't
    each place a duplicate Google Calendar event. Only the process that wins the claim writes
    to the calendar; the loser skips it. (Previously the guard was check-then-act with the
    calendar write BEFORE the DB insert, so both processes created events and the UNIQUE
    constraint only deduped the DB row, orphaning the extra calendar events.)
    """
    if not target_day:
        log.warning("no target date for %s %s — not creating an event", thread_key, kind)
        return None

    # Claim the slot atomically FIRST. If we didn't win, another confirm already owns this
    # leg — return its id and do NOT create a second calendar event.
    event_id, won = store.claim_scheduling_event(
        conn, thread_key, episode, kind, source=source, target_date=target_day.isoformat())
    if not won:
        log.info("scheduling event already claimed for %s/%s/%s — skipping calendar create",
                 thread_key, episode, kind)
        return event_id

    calendar = calendar or GoogleCalendar()
    start_iso, end_iso = default_slot(target_day, kind)
    summary = title(pet_name, kind, PENDING)
    gcal_id = calendar.create_event(
        summary, start_iso, end_iso,
        description=("Placeholder — the client hasn't picked a time yet.\n"
                     f"Thread: {thread_key} (episode {episode})"),
        transparency=TRANSPARENT)
    if not gcal_id:
        # Roll back the claim so a later confirm can retry; never fail silently.
        store.delete_scheduling_event(conn, event_id)
        try:
            from . import telegram_notify
            telegram_notify.send_alert(
                f"Could not create calendar event: {summary} on {target_day}. "
                "The booking is NOT on your calendar.")
        except Exception:
            log.exception("alert failed")
        return None

    store.update_scheduling_event(conn, event_id, gcal_event_id=gcal_id)
    return event_id


def stay_conflicts(conn, thread_key, episode, start_day, end_day):
    """True if the current episode ALREADY owns drop-off/pick-up events that belong to a
    DIFFERENT booking than (start_day, end_day) — meaning this confirmation is a second,
    overlapping booking on the same Rover number and must NOT reuse this episode.

    A leg conflicts if it is CANCELLED (a prior cancelled booking still holds the unique
    (thread, episode, kind) row, which would silently block a re-claim) or if its target date
    doesn't match the newly-confirmed stay. Returns False on a fresh episode (no events yet)
    and on the normal SMS-marker + confirmation-email double-fire for the SAME dates.
    """
    want = {d.isoformat() for d in (start_day, end_day) if d}
    rows = [r for r in (store.get_scheduling_event(conn, thread_key, episode, k)
                        for k in (DROPOFF, PICKUP)) if r]
    if not rows:
        return False
    for _id, status, target, _sched, _gcal, _link in rows:
        if status == CANCELLED:
            return True
        if want and target and target not in want:
            return True
    return False


def _stay_str(start_day, end_day):
    if start_day and end_day and end_day != start_day:
        return f"{start_day.isoformat()} to {end_day.isoformat()}"
    return start_day.isoformat() if start_day else None


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

    # A second, overlapping booking on the same Rover number (confirmed out of order) would
    # otherwise collide with the prior booking's events + links-sent flag on the current
    # episode — silently placing no events and sending no links. Give it its own episode.
    # send_scheduling_links (called by our callers right after) then reads the bumped episode.
    if stay_conflicts(conn, thread_key, episode, start_day, end_day):
        episode = store.start_new_booking_episode(
            conn, thread_key, from_episode=episode, stay_dates=_stay_str(start_day, end_day))
        log.info("second booking on an occupied episode -> episode %d for %s (%s to %s)",
                 episode, thread_key, start_day, end_day)

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


# --- Addendum B / C4: booking cancellation & modification -----------------
def on_booking_cancelled(conn, thread_key, calendar=None, episode=None):
    """Rover cancelled the whole booking -> delete the drop-off/pick-up calendar events for
    this episode, mark them cancelled, neutralize any live Cal.com booking (so the poller
    doesn't re-confirm), and expire the scheduling links. Returns how many legs were removed.
    """
    episode = episode if episode is not None else store.get_episode(conn, thread_key)
    calendar = calendar or GoogleCalendar()
    removed = 0
    for r in store.list_scheduling_events(conn, thread_key=thread_key):
        ev_id, _tk, ep, kind, _src, status, _target, _sched, gcal_id, _link = r
        if ep != episode or status == CANCELLED:
            continue
        full = store.get_scheduling_event_by_id(conn, ev_id)      # full row carries booking_ref
        booking_ref = full[9] if full else None
        if booking_ref:
            store.ignore_calcom_booking(conn, booking_ref)        # stop the poller re-confirming
        if gcal_id:
            calendar.delete_event(gcal_id)
        store.update_scheduling_event(conn, ev_id, status=CANCELLED, gcal_event_id=None,
                                      booking_ref=None, scheduled_at=None)
        removed += 1
    store.del_meta(conn, f"links_sent:{thread_key}:{episode}")    # expire links
    log.info("cancelled booking for %s (episode %d): removed %d event(s)",
             thread_key, episode, removed)
    return removed


def apply_date_change(conn, thread_key, start_day, end_day, calendar=None, episode=None):
    """Move a booking's drop-off/pick-up legs to new dates (a Rover modification, or the
    /movebooking command). Only legs whose date ACTUALLY changed are touched, so an already-
    confirmed leg on an unchanged date keeps its booked time. A confirmed leg whose date DID
    change is reverted to PENDING (its old time is no longer valid) and its Cal.com booking is
    neutralized. Returns {moved, kept, invalidated, start, end}.
    """
    episode = episode if episode is not None else store.get_episode(conn, thread_key)
    calendar = calendar or GoogleCalendar()
    t = store.get_thread(conn, thread_key)
    pet, owner = (t[1], t[0]) if t else (None, None)

    moved, kept, invalidated = [], [], []
    for r in store.list_scheduling_events(conn, thread_key=thread_key):
        ev_id, _tk, ep, kind, _src, status, target, sched, gcal_id, _link = r
        if ep != episode or status == CANCELLED or kind == MEET_GREET:
            continue
        day = start_day if kind == DROPOFF else end_day
        if not day:
            continue
        if target == day.isoformat():
            kept.append((kind, status, sched))            # unchanged leg — do not disturb
            continue
        start_iso, end_iso = default_slot(day, kind)
        if gcal_id:
            calendar.update_event(gcal_id, summary=title(pet, kind, PENDING),
                                  start_iso=start_iso, end_iso=end_iso,
                                  transparency=TRANSPARENT)
        if status == CONFIRMED and sched:
            full = store.get_scheduling_event_by_id(conn, ev_id)
            store.ignore_calcom_booking(conn, full[9] if full else None)
            invalidated.append((kind, sched))
        new_link = build_link(kind, day.isoformat(), owner, ref=ev_id)
        store.update_scheduling_event(conn, ev_id, target_date=day.isoformat(), status=PENDING,
                                      scheduled_at=None, booking_ref=None, link_url=new_link)
        moved.append(kind)
    store.upsert_sms_thread(conn, thread_key, stay_dates=f"{start_day} to {end_day}")
    return {"moved": moved, "kept": kept, "invalidated": invalidated,
            "start": start_day, "end": end_day}


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


def _load_post_confirmation_template():
    """Read the post-confirmation message template, stripping a leading comment block
    (contiguous run of blank or '#'-prefixed lines) so the .example header never ships."""
    try:
        with open(config.POST_CONFIRMATION_PATH, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        log.error("post-confirmation template missing at %s", config.POST_CONFIRMATION_PATH)
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return "\n".join(lines[i:]).strip()


def scheduling_message(owner_name, pet_name, links, start_date=None, end_date=None):
    """The post-confirmation message with both booking links folded in (no LLM call).

    Loads POST_CONFIRMATION_PATH and substitutes {dropoff_link}/{pickup_link}. owner_name,
    pet_name and dates are accepted for signature compatibility but the template no longer
    references them (kept so callers don't need to change).
    """
    if DROPOFF not in links or PICKUP not in links:
        return None
    template = _load_post_confirmation_template()
    if not template:
        return None
    return (template
            .replace("{dropoff_link}", links[DROPOFF])
            .replace("{pickup_link}", links[PICKUP]))


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


def build_leg_links_message(conn, thread_key, kinds, calendar=None):
    """Compose a message re-issuing links for ONLY the given legs (C4 modification).

    When a booking is modified but only one date changed, only that leg's link should go
    out — re-sending an unchanged (possibly already-booked) leg's link risks a double
    booking. Returns (text, links_for_those_legs) or (None, {}).
    """
    episode = store.get_episode(conn, thread_key)
    row = store.get_thread(conn, thread_key)
    owner, pet = (row[0], row[1]) if row else (None, None)
    all_links = ensure_links(conn, thread_key, episode, owner_name=owner)
    wanted = [k for k in (DROPOFF, PICKUP) if k in kinds and k in all_links]
    if not wanted:
        return None, {}
    lines = [f"Hi {owner or 'there'}, {pet or 'your pup'}'s dates changed — please pick a new "
             f"{'time' if len(wanted) == 1 else 'time for each'}:"]
    for k in wanted:
        row_k = store.get_scheduling_event(conn, thread_key, episode, k)
        when = _pretty(row_k[2]) if row_k else ""
        lines.append(f"\n{_LABEL.get(k, k)} ({when}): {all_links[k]}")
    lines.append("\nLet me know if none of the times work and we'll sort something out!")
    return "\n".join(lines), {k: all_links[k] for k in wanted}


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