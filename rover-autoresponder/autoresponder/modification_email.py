"""Addendum B / C4 — booking MODIFICATION via the "revised itinerary" email.

Rover's modification arrives as a SEPARATE standalone email (not in the conversation thread),
so it can't be correlated by the phone↔thread binding. The new dates live ONLY here (the SMS
"modified" marker carries none). Real body (samples/revised_itinerary.txt):

    Subject: Your revised itinerary for your booking with Shadow
    ... We also sent these details to Dominique, so you're all set.
    Dates: Aug 28, 2026 - Aug 31, 2026

We parse pet (subject), owner (body), and the new dates (body, same format as the confirmation
email), then correlate to a CURRENT or UPCOMING booking by owner+pet (both required; ambiguous
or zero → alert, never guess) and move the calendar legs to the new dates + re-issue links.
"""
import logging
import re
from datetime import date

from . import store, dates

log = logging.getLogger(__name__)

SUBJECT_RE = re.compile(r"revised itinerary for your booking with\s+(.+?)\s*$", re.IGNORECASE)
# "We also sent these details to Dominique, so you're all set." The HTML->text extraction wraps
# lines mid-sentence, so the body is whitespace-collapsed before matching (see parse_*).
OWNER_RE = re.compile(r"sent these details to\s+([^,.]+)", re.IGNORECASE)
# Same "Mon DD, YYYY - Mon DD, YYYY" format as the confirmation email — full dates with year.
DATES_RE = re.compile(
    r"Dates:\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})"
    r"(?:\s*[-–—]\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}))?",
    re.IGNORECASE)


def _first(name):
    """'Dominique W.' / 'Rusty & Osha' -> a normalized first token for matching."""
    parts = re.split(r"\s+", (name or "").strip())
    return re.sub(r"[^a-z]", "", parts[0].lower()) if parts and parts[0] else ""


def parse_modification_email(subject, body):
    """Return {pet_name, owner_name, start_date, end_date}, or None if not a modification."""
    m = SUBJECT_RE.search(subject or "")
    if not m:
        return None
    body = re.sub(r"\s+", " ", body or "")     # collapse HTML->text line wraps for matching
    md = DATES_RE.search(body)
    if not md:
        return None                       # no new dates -> nothing actionable
    owner = OWNER_RE.search(body)
    return {
        "pet_name": m.group(1).strip().rstrip("."),
        "owner_name": owner.group(1).strip() if owner else None,
        "start_date": dates.parse_email_date(md.group(1)),
        "end_date": dates.parse_email_date(md.group(2) or md.group(1)),
    }


def _find_booking(conn, owner, pet, today=None):
    """Correlate to a CURRENT or UPCOMING booking by owner+pet (both required, normalized).

    "Current" matters as much as "upcoming" — a stay already in progress can be modified. So
    we accept any confirmed booking whose stay hasn't ENDED yet (end >= today). Returns
    (thread_key, None) on a single match, else (None, reason) so the caller alerts.
    """
    today = today or date.today()
    o, p = _first(owner), _first(pet)
    if not o or not p:
        return None, "missing owner or pet name"
    with store._LOCK:
        rows = conn.execute(
            "SELECT thread_key, owner_name, pet_name, stay_dates FROM threads "
            "WHERE has_booked=1 AND status='converted'").fetchall()
    matches = []
    for tk, ow, pn, stay in rows:
        if _first(ow) == o and _first(pn) == p:
            _start, end = dates.parse_stay(stay, today=today)
            if end and end >= today:      # current (in progress) OR upcoming — not yet ended
                matches.append(tk)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "no matching current/upcoming booking"
    return None, f"{len(matches)} matching bookings (ambiguous)"


def handle_modification_email(conn, subject, body, calendar=None, notify=True, today=None):
    """Apply a modification email. Returns the thread key, or None."""
    info = parse_modification_email(subject, body)
    if not info:
        return None
    if not (info["start_date"] and info["end_date"]):
        return None
    pet, owner = info["pet_name"], info["owner_name"]
    start, end = info["start_date"], info["end_date"]

    thread_key, reason = _find_booking(conn, owner, pet, today=today)
    if not thread_key:
        log.warning("modification for %s (%s) %s-%s couldn't be matched: %s",
                    pet, owner, start, end, reason)
        _alert(f"📅 Booking modified for {pet} ({owner or 'owner?'}) → {start} to {end}, but I "
               f"couldn't match it to a booking ({reason}). Update the calendar manually.")
        return None

    from .scheduling import apply_date_change
    result = apply_date_change(conn, thread_key, start, end, calendar=calendar)
    log.info("MODIFICATION | %s | %s (%s) -> %s to %s | moved=%s kept=%s invalidated=%s",
             thread_key, pet, owner, start, end,
             result["moved"], [k for k, *_ in result["kept"]], result["invalidated"])

    if not notify:
        return thread_key

    # Re-issue ONLY the moved leg(s)' link(s) — never re-send an unchanged (maybe already
    # booked) leg, which could double-book. So a pick-up-only change re-issues just pick-up.
    if result["moved"]:
        try:
            from .sms_pipeline import send_modified_links
            send_modified_links(conn, thread_key, result["moved"])
        except Exception:
            log.exception("  could not re-issue links for %s", thread_key)

    if result["invalidated"]:
        legs = ", ".join(f"{k} (was {s[:16].replace('T', ' ')})"
                         for k, s in result["invalidated"])
        _alert(f"⚠️ Booking for {pet} moved to {start}–{end}, but a time was already booked: "
               f"{legs}. That slot no longer applies — the client must rebook; cancel the old "
               "Cal.com slot. New links sent for approval.")
    elif result["moved"]:
        _alert(f"📅 Booking for {pet} ({owner}) modified → {start} to {end}. Calendar updated; "
               "new scheduling links drafted for your approval.")
    return thread_key


def _alert(text):
    try:
        from . import telegram_notify
        telegram_notify.send_alert(text)
    except Exception:
        log.exception("alert failed")
