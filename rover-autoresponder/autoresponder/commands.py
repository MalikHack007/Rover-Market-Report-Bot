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

from . import store, dates
from .scheduling import (
    CANCELLED, CONFIRMED, DROPOFF, MEET_GREET, PICKUP, build_link, default_slot,
    on_booking_confirmed, ensure_links, title, PENDING, apply_date_change,
)

log = logging.getLogger(__name__)

PRIVATE = "private"

HELP = (
    "<b>Bookings</b>\n"
    "<code>/booking Willow 2026-09-01 2026-09-06 Sarah</code>\n"
    "   full booking (owner optional) → both events + both links\n\n"
    "<b>Single links</b>\n"
    "<code>/dropoff Willow 2026-09-01 [owner]</code>\n"
    "<code>/pickup Willow 2026-09-06 [owner]</code>\n"
    "<code>/meetgreet Willow [owner]</code>  (open range — next 7 days)\n\n"
    "<b>Manage</b>\n"
    "<code>/bookings</code>  list upcoming\n"
    "<code>/links 12</code>  re-show the links for an entry\n"
    "<code>/movebooking 12 2026-09-03 2026-09-08</code>  change dates (resets both legs)\n"
    "<code>/retarget 12 2026-09-08</code>  fix ONE leg's date, keep its booked time\n"
    "<code>/cancelbooking 12</code>  delete its events\n\n"
    "Private clients text your other line, so the bot never messages them — "
    "it just gives you the links to send."
)


def parse_date(text):
    """Accept YYYY-MM-DD, MM/DD/YYYY, MM-DD-YYYY, or bare MM/DD (next future occurrence)."""
    return dates.parse_command_date(text)


def _slug(pet):
    return re.sub(r"[^a-z0-9]+", "", (pet or "pet").lower())[:20] or "pet"


def _thread_key(pet, start_day, kind=None):
    """Full bookings key on pet+date; single-leg entries add the kind so a standalone
    drop-off and pick-up for the same pet can't collide."""
    base = f"{PRIVATE}:{_slug(pet)}-{start_day:%Y%m%d}"
    return f"{base}-{kind}" if kind else base


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
        if sched:
            when = sched[:16].replace("T", " ")
        elif target:
            when = f"{target} (unscheduled)"
        else:
            when = "open range (unscheduled)"
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
    """/movebooking <id> <start> <end> — change a booking's dates.

    Only legs whose date ACTUALLY changes are touched. If a stay is extended by a day,
    the drop-off is left completely alone — including a time the client already chose —
    rather than being reset and re-linked for no reason.
    """
    if len(args) < 3:
        return ("Usage: <code>/movebooking &lt;id&gt; &lt;start&gt; &lt;end&gt;</code>")
    row = store.get_scheduling_event_by_id(conn, _int(args[0]))
    if not row:
        return f"No scheduling event with id {args[0]}."
    thread_key = row[1]
    start_day, end_day = parse_date(args[1]), parse_date(args[2])
    if not start_day or not end_day:
        return "Couldn't read those dates. Try YYYY-MM-DD."
    if end_day < start_day:
        return "The end date is before the start date."

    from .calendar_client import GoogleCalendar
    calendar = calendar or GoogleCalendar()
    t = store.get_thread(conn, thread_key)
    pet, owner = (t[1] or thread_key, t[0]) if t else (thread_key, None)

    # Shared with the Rover-modification path (C4): move only the legs whose date changed,
    # revert a confirmed-but-now-invalid leg to PENDING, re-mint links.
    res = apply_date_change(conn, thread_key, start_day, end_day, calendar=calendar)
    moved, kept, invalidated = res["moved"], res["kept"], res["invalidated"]
    if not moved:
        return (f"{pet} is already {start_day:%a, %b %d} → "
                f"{end_day:%a, %b %d}. Nothing changed.")

    links = ensure_links(conn, thread_key, store.get_episode(conn, thread_key), owner)
    out = [f"Moved {pet or thread_key} to {start_day:%a, %b %d} → {end_day:%a, %b %d}."]
    for kind, status, sched in kept:
        when = f", booked {sched[:16].replace('T', ' ')}" if sched else ""
        out.append(f"• {_KIND_LABEL.get(kind, kind)} unchanged ({status}{when}) — "
                   "left as is.")
    for kind, sched in invalidated:
        out.append(f"⚠️ {_KIND_LABEL.get(kind, kind)} was booked for "
                   f"{sched[:16].replace('T', ' ')}; that date changed, so it needs "
                   "rebooking. Cancel the old slot in Cal.com.")
    if moved:
        out.append("\nNew link(s) for the changed leg(s):")
        for kind in moved:
            out.append(f"<b>{_KIND_LABEL.get(kind, kind)}</b>\n"
                       f"<code>{links.get(kind, '—')}</code>")
    return "\n".join(out)


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
        if cmd in ("dropoff", "drop-off", "drop"):
            return cmd_single(conn, DROPOFF, args, calendar)
        if cmd in ("pickup", "pick-up", "pick"):
            return cmd_single(conn, PICKUP, args, calendar)
        if cmd in ("meetgreet", "meet-greet", "mg"):
            return cmd_meetgreet(conn, args, calendar)
        if cmd == "links":
            return cmd_links(conn, args)
        if cmd in ("retarget", "redate"):
            return cmd_retarget(conn, args, calendar)
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


# --- single-leg links -----------------------------------------------------
_KIND_LABEL = {DROPOFF: "Drop-off", PICKUP: "Pick-up", MEET_GREET: "Meet & Greet"}


def cmd_single(conn, kind, args, calendar=None):
    """/dropoff, /pickup — one event + one link, for when only one leg needs booking."""
    label = _KIND_LABEL[kind]
    if len(args) < 2:
        return (f"Usage: <code>/{kind} &lt;pet&gt; &lt;date&gt; [owner]</code>\n"
                f"e.g. <code>/{kind} Willow 2026-09-01 Sarah</code>")
    pet, day = args[0], parse_date(args[1])
    owner = " ".join(args[2:]) if len(args) > 2 else None
    if not day:
        return f"Couldn't read that date ({args[1]}). Try YYYY-MM-DD."

    thread_key = _thread_key(pet, day, kind)
    if store.get_scheduling_event(conn, thread_key, 1, kind):
        return (f"A {label.lower()} for {pet} on {day} already exists — "
                f"use <code>/bookings</code> to find it.")

    store.upsert_sms_thread(conn, thread_key, owner_name=owner, pet_name=pet,
                            stay_dates=str(day), status="converted")
    from .scheduling import create_pending_event
    ev_id = create_pending_event(conn, thread_key, 1, kind, pet, day,
                                 source=PRIVATE, calendar=calendar)
    if not ev_id:
        return "Could not create the calendar event — check the logs."

    url = build_link(kind, day.isoformat(), owner, ref=ev_id)
    if not url:
        return (f"Calendar event created, but the link couldn't be built "
                "(check CALCOM_USERNAME).")
    store.update_scheduling_event(conn, ev_id, link_url=url)
    return (f"🐾 <b>{label} — {pet}</b>\n{day:%a, %b %d}"
            f"{f' · {owner}' if owner else ''}  <code>#{ev_id}</code>\n\n"
            f"<code>{url}</code>")


def cmd_meetgreet(conn, args, calendar=None):
    """/meetgreet — open range (next 7 days), so no date and no placeholder yet."""
    if not args:
        return ("Usage: <code>/meetgreet &lt;pet&gt; [owner]</code>\n"
                "Open range — the client picks any time in the next 7 days.")
    pet = args[0]
    owner = " ".join(args[1:]) if len(args) > 1 else None
    from datetime import date as _date
    thread_key = _thread_key(pet, _date.today(), MEET_GREET)
    store.upsert_sms_thread(conn, thread_key, owner_name=owner, pet_name=pet,
                            status="converted")
    from .scheduling import ensure_meetgreet_link, MEET_GREET as _MG
    url = ensure_meetgreet_link(conn, thread_key, owner_name=owner, pet_name=pet)
    row0 = store.get_scheduling_event(conn, thread_key, 1, _MG)
    if row0:
        store.update_scheduling_event(conn, row0[0], link_url=url)
        with store._LOCK, conn:      # tag as private so /bookings marks it 🏠
            conn.execute("UPDATE scheduling_events SET source=? WHERE id=?",
                         (PRIVATE, row0[0]))
    if not url:
        return "Could not build the meet & greet link (check CALCOM_USERNAME)."
    row = store.get_scheduling_event(conn, thread_key, 1, MEET_GREET)
    return (f"🐾 <b>Meet &amp; Greet — {pet}</b>"
            f"{f' · {owner}' if owner else ''}  <code>#{row[0] if row else '?'}</code>\n"
            "Open range — no calendar hold until they book.\n\n"
            f"<code>{url}</code>")


def cmd_links(conn, args):
    """/links <id> — re-show the link(s) for an entry (e.g. to resend)."""
    if not args:
        return "Usage: <code>/links &lt;id&gt;</code> (see <code>/bookings</code>)"
    row = store.get_scheduling_event_by_id(conn, _int(args[0]))
    if not row:
        return f"No scheduling event with id {args[0]}."
    thread_key = row[1]
    t = store.get_thread(conn, thread_key)
    pet = (t[1] if t else None) or thread_key
    out = [f"🔗 <b>Links — {pet}</b>"]
    for r in store.list_scheduling_events(conn, thread_key=thread_key):
        ev_id, _tk, _ep, kind, _src, status, target, sched, _g, link = r
        when = (sched[:16].replace("T", " ") if sched else (target or "open range"))
        out.append(f"\n<b>{_KIND_LABEL.get(kind, kind)}</b> ({status}, {when})  "
                   f"<code>#{ev_id}</code>\n<code>{link or '— no link —'}</code>")
    return "\n".join(out)


def cmd_retarget(conn, args, calendar=None):
    """/retarget <id> <date> — change ONE leg's expected date, keeping its booked time.

    For when a client shifts a day but has already picked their slot. Unlike
    /movebooking (which resets both legs to PENDING and mints fresh links), this only
    corrects target_date, so a confirmed time and its calendar event are untouched —
    it just stops the date-validation check flagging a mismatch.

    If the leg is still PENDING, the placeholder and its link move to the new date,
    since nothing has been booked yet.
    """
    if len(args) < 2:
        return ("Usage: <code>/retarget &lt;id&gt; &lt;date&gt;</code>\n"
                "e.g. <code>/retarget 12 2026-09-08</code>\n"
                "Changes the expected date for one leg, keeping any booked time.")
    row = store.get_scheduling_event_by_id(conn, _int(args[0]))
    if not row:
        return f"No scheduling event with id {args[0]}."
    day = parse_date(args[1])
    if not day:
        return f"Couldn't read that date ({args[1]}). Try YYYY-MM-DD."

    (ev_id, thread_key, _ep, kind, _src, status, old_target,
     scheduled_at, gcal_id, _bref, _link) = row
    t = store.get_thread(conn, thread_key)
    pet, owner = (t[1], t[0]) if t else (None, None)
    label = _KIND_LABEL.get(kind, kind)

    if status == CONFIRMED and scheduled_at:
        # Keep the client's chosen time and its calendar event exactly as they are.
        store.update_scheduling_event(conn, ev_id, target_date=day.isoformat())
        return (f"📅 <b>{label} — {pet or thread_key}</b>\n"
                f"Expected date {old_target} → <b>{day}</b>.\n"
                f"Their booked time ({scheduled_at[:16].replace('T', ' ')}) and the "
                "calendar event are unchanged.")

    # Still unbooked: move the placeholder and re-mint the link for the new date.
    from .calendar_client import GoogleCalendar
    calendar = calendar or GoogleCalendar()
    start_iso, end_iso = default_slot(day, kind)
    if gcal_id:
        calendar.update_event(gcal_id, summary=title(pet, kind, PENDING),
                              start_iso=start_iso, end_iso=end_iso)
    new_link = build_link(kind, day.isoformat(), owner, ref=ev_id)
    store.update_scheduling_event(conn, ev_id, target_date=day.isoformat(),
                                  link_url=new_link)
    return (f"📅 <b>{label} — {pet or thread_key}</b>\n"
            f"Moved {old_target} → <b>{day}</b> (still unbooked). New link:\n\n"
            f"<code>{new_link}</code>")