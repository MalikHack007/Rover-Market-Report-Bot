"""Addendum B / C5 — Telegram commands for private (non-Rover) bookings.

Private clients reach you on a different phone line the bot never sees, so nothing about
them arrives automatically. You enter the booking here; the bot places the calendar
placeholders and hands you the links to send yourself.

Why bother: it keeps ONE honest source of truth for availability. A private drop-off
consumes a real slot — if the bot didn't know, Cal.com would offer that window to a Rover
client and you'd be double-booked.

Commands:
    /booking <pet> <start> <end> [owner]   create a private booking
    /bookings                              list upcoming scheduling events
    /movebooking <id> <start> <end>        change a private booking's dates
    /cancelbooking <id>                    delete a private booking's events
    /help                                  this list
"""
import logging
import re
from datetime import datetime

from . import store
from .scheduling import (
    CANCELLED, DROPOFF, PICKUP, build_link, default_slot, on_booking_confirmed,
    ensure_links, title, PENDING,
)

log = logging.getLogger(__name__)

PRIVATE = "private"

HELP = (
    "<b>Private booking commands</b>\n"
    "<code>/booking Willow 2026-09-01 2026-09-06 Sarah</code>\n"
    "   create a booking (owner optional) → calendar events + links\n"
    "<code>/bookings</code>  list upcoming\n"
    "<code>/movebooking 12 2026-09-03 2026-09-08</code>  change dates\n"
    "<code>/cancelbooking 12</code>  delete its events\n\n"
    "Private clients text your other line, so the bot never messages them — "
    "it just gives you the links to send."
)


def parse_date(text):
    """Accept YYYY-MM-DD, MM/DD/YYYY, or MM/DD (next future occurrence)."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    from .scheduling import parse_booking_date
    return parse_booking_date(text)


def _thread_key(pet, start_day):
    slug = re.sub(r"[^a-z0-9]+", "", (pet or "pet").lower())[:20] or "pet"
    return f"{PRIVATE}:{slug}-{start_day:%Y%m%d}"


def cmd_booking(conn, args, calendar=None):
    if len(args) < 3:
        return ("Usage: <code>/booking &lt;pet&gt; &lt;start&gt; &lt;end&gt; [owner]</code>\n"
                "e.g. <code>/booking Willow 2026-09-01 2026-09-06 Sarah</code>")
    pet, start_raw, end_raw = args[0], args[1], args[2]
    owner = " ".join(args[3:]) if len(args) > 3 else None

    start_day, end_day = parse_date(start_raw), parse_date(end_raw)
    if not start_day or not end_day:
        return f"Couldn't read those dates ({start_raw}, {end_raw}). Try YYYY-MM-DD."
    if end_day < start_day:
        return "The end date is before the start date."

    thread_key = _thread_key(pet, start_day)
    if store.list_scheduling_events(conn, thread_key=thread_key):
        return (f"A booking for {pet} starting {start_day} already exists. "
                f"Use <code>/bookings</code> to see it.")

    store.upsert_sms_thread(conn, thread_key, owner_name=owner, pet_name=pet,
                            stay_dates=f"{start_day} to {end_day}", status="converted")
    created = on_booking_confirmed(
        conn, thread_key, pet, start_day.strftime("%m/%d/%Y"),
        end_day.strftime("%m/%d/%Y"), source=PRIVATE, calendar=calendar)
    if not created:
        return "Could not create the calendar events — check the logs."

    links = ensure_links(conn, thread_key, store.get_episode(conn, thread_key),
                         owner_name=owner)
    if not links:
        return (f"Calendar events created for {pet}, but links couldn't be built "
                "(check CALCOM_USERNAME).")

    return (
        f"🐾 <b>Private booking created — {pet}</b>\n"
        f"{start_day:%a, %b %d} → {end_day:%a, %b %d}"
        f"{f' · {owner}' if owner else ''}\n\n"
        "Calendar placeholders are up. Send these to your client:\n\n"
        f"<b>Drop-off</b>\n<code>{links.get(DROPOFF, '—')}</code>\n\n"
        f"<b>Pick-up</b>\n<code>{links.get(PICKUP, '—')}</code>"
    )


def cmd_bookings(conn):
    rows = [r for r in store.list_scheduling_events(conn)
            if r[5] in (PENDING, "confirmed")]
    if not rows:
        return "No upcoming scheduling events."
    lines = ["<b>Upcoming</b>"]
    for r in rows:
        ev_id, thread_key, _ep, kind, source, status, target, sched = r[:8]
        t = store.get_thread(conn, thread_key)
        who = (t[1] if t and t[1] else thread_key)
        when = (sched[:16].replace("T", " ") if sched else f"{target} (unscheduled)")
        tag = " 🏠" if source == PRIVATE else ""
        lines.append(f"<code>{ev_id:>3}</code> {who} {kind} — {when}{tag}")
    lines.append("\n🏠 = private booking")
    return "\n".join(lines)


def cmd_cancelbooking(conn, args, calendar=None):
    if not args:
        return "Usage: <code>/cancelbooking &lt;id&gt;</code> (see <code>/bookings</code>)"
    row = store.get_scheduling_event_by_id(conn, _int(args[0]))
    if not row:
        return f"No scheduling event with id {args[0]}."
    thread_key = row[1]
    from .calendar_client import GoogleCalendar
    calendar = calendar or GoogleCalendar()
    n = 0
    for r in store.list_scheduling_events(conn, thread_key=thread_key):
        if r[8]:
            calendar.delete_event(r[8])
        store.update_scheduling_event(conn, r[0], status=CANCELLED)
        n += 1
    t = store.get_thread(conn, thread_key)
    return (f"Cancelled {n} event(s) for {(t[1] if t else thread_key)}. "
            "Cancel any Cal.com bookings there too.")


def cmd_movebooking(conn, args, calendar=None):
    if len(args) < 3:
        return ("Usage: <code>/movebooking &lt;id&gt; &lt;start&gt; &lt;end&gt;</code>")
    row = store.get_scheduling_event_by_id(conn, _int(args[0]))
    if not row:
        return f"No scheduling event with id {args[0]}."
    thread_key = row[1]
    start_day, end_day = parse_date(args[1]), parse_date(args[2])
    if not start_day or not end_day:
        return "Couldn't read those dates. Try YYYY-MM-DD."

    from .calendar_client import GoogleCalendar
    calendar = calendar or GoogleCalendar()
    t = store.get_thread(conn, thread_key)
    pet, owner = (t[1], t[0]) if t else (None, None)

    for r in store.list_scheduling_events(conn, thread_key=thread_key):
        ev_id, _tk, _ep, kind, _src, _status, _target, _sched, gcal_id, _link = r
        day = start_day if kind == DROPOFF else end_day
        start_iso, end_iso = default_slot(day, kind)
        if gcal_id:
            calendar.update_event(gcal_id, summary=title(pet, kind, PENDING),
                                  start_iso=start_iso, end_iso=end_iso)
        # The link embeds the date, so a move invalidates it — mint a fresh one.
        new_link = build_link(kind, day.isoformat(), owner, ref=ev_id)
        store.update_scheduling_event(conn, ev_id, target_date=day.isoformat(),
                                      status=PENDING, scheduled_at=None,
                                      booking_ref=None, link_url=new_link)
    store.upsert_sms_thread(conn, thread_key,
                            stay_dates=f"{start_day} to {end_day}")
    links = ensure_links(conn, thread_key, store.get_episode(conn, thread_key), owner)
    return (
        f"Moved {pet or thread_key} to {start_day:%a, %b %d} → {end_day:%a, %b %d}.\n"
        "Any previously booked times were cleared. New links:\n\n"
        f"<b>Drop-off</b>\n<code>{links.get(DROPOFF, '—')}</code>\n\n"
        f"<b>Pick-up</b>\n<code>{links.get(PICKUP, '—')}</code>"
    )


def _int(x, default=-1):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def handle_command(conn, text, calendar=None):
    """Route a /command. Returns a reply string, or None if it isn't a command."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd, args = parts[0].lower().lstrip("/"), parts[1:]
    try:
        if cmd == "booking":
            return cmd_booking(conn, args, calendar)
        if cmd == "bookings":
            return cmd_bookings(conn)
        if cmd == "cancelbooking":
            return cmd_cancelbooking(conn, args, calendar)
        if cmd == "movebooking":
            return cmd_movebooking(conn, args, calendar)
        if cmd in ("help", "start"):
            return HELP
    except Exception:
        log.exception("command failed: %s", text)
        return "That command failed — check the logs."
    return None
