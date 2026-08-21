"""Confirmation via EMAIL — the pay-first booking flow.

Normally a booking confirms with an SMS marker ("… has confirmed a booking request …").
But when a client pays up front, Rover sends:

    SMS:   [ J D. wants you to care for Buddy on Rover! Confirm booking ASAP @ … ]
    (you hit Accept)
    EMAIL: Confirmed: Buddy's upcoming booking from Nov 20, 2026 - Nov 27, 2026

and **no confirmation SMS ever arrives**. Without this module the booking would never
reach the calendar.

The email carries the client's PHONE NUMBER, which is our SMS thread key — so it
correlates exactly, with no name matching. It also carries full dates with the year,
which are authoritative over the SMS marker's bare MM/DD.
"""
import logging
import re
from datetime import datetime

from . import store

log = logging.getLogger(__name__)

# "Confirmed: Buddy's upcoming booking from Nov 20, 2026 - Nov 27, 2026"
SUBJECT_RE = re.compile(
    r"Confirmed:\s*(.+?)['\u2019]s upcoming booking from\s+"
    r"([A-Za-z]{3}\s+\d{1,2},\s*\d{4})\s*[-–]\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE)

DATES_RE = re.compile(
    r"Dates:\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})\s*[-–]\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE)
OWNER_RE = re.compile(r"^\s*Owner:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
PHONE_RE = re.compile(r"Phone number:\s*([\d\s().+-]{10,})", re.IGNORECASE)
PETS_RE = re.compile(r"Pet\(s\):\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def normalize_phone(raw):
    """'(323) 458-5614' -> '+13234585614' so it matches the SMS thread key."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits if digits else None


def _date(text):
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_confirmation_email(subject, body):
    """Return the booking's details, or None if this isn't a confirmation email."""
    m = SUBJECT_RE.search(subject or "")
    if not m:
        return None
    pet, start_text, end_text = m.group(1), m.group(2), m.group(3)

    body = body or ""
    md = DATES_RE.search(body)
    if md:                                   # body dates are the same, but prefer them
        start_text, end_text = md.group(1), md.group(2)

    owner = OWNER_RE.search(body)
    phone = PHONE_RE.search(body)
    pets = PETS_RE.search(body)

    return {
        "pet_name": (pets.group(1).strip() if pets else pet.strip()),
        "owner_name": owner.group(1).strip() if owner else None,
        "phone": normalize_phone(phone.group(1) if phone else None),
        "start_date": _date(start_text),
        "end_date": _date(end_text),
    }


def handle_confirmation_email(conn, subject, body, calendar=None, notify=True):
    """Confirm a booking from the email. Returns the thread key, or None.

    Idempotent: the scheduling events are unique per (thread, episode, kind), so if the
    SMS marker already confirmed this booking nothing is duplicated.
    """
    info = parse_confirmation_email(subject, body)
    if not info:
        return None
    if not info["phone"]:
        log.warning("confirmation email for %s has no phone number — cannot correlate",
                    info["pet_name"])
        _alert(f"Booking confirmed for {info['pet_name']} "
               f"({info['start_date']} – {info['end_date']}) but I couldn't match it to "
               "a conversation, so it is NOT on your calendar.")
        return None

    number = info["phone"]
    log.info("EMAIL CONFIRMATION | %s | pet=%s owner=%s %s -> %s",
             number, info["pet_name"], info["owner_name"],
             info["start_date"], info["end_date"])

    known = store.get_thread(conn, number) is not None
    store.upsert_sms_thread(conn, number, owner_name=info["owner_name"],
                            pet_name=info["pet_name"], status="converted",
                            stay_dates=f"{info['start_date']} to {info['end_date']}")
    store.mark_has_booked(conn, number)
    if not known:
        log.info("  (no prior SMS thread for %s — pay-first booking)", number)

    from .scheduling import on_booking_confirmed
    created = on_booking_confirmed(
        conn, number, info["pet_name"],
        info["start_date"].strftime("%m/%d/%Y") if info["start_date"] else None,
        info["end_date"].strftime("%m/%d/%Y") if info["end_date"] else None,
        calendar=calendar)
    if created and notify:
        try:
            from .sms_pipeline import send_scheduling_links
            send_scheduling_links(conn, number)
        except Exception:
            log.exception("  could not send scheduling links for %s", number)
    return number


def _alert(text):
    try:
        from . import telegram_notify
        telegram_notify.send_alert(text)
    except Exception:
        log.exception("alert failed")
