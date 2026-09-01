"""Shared date parsing + formatting for the auto-responder.

Before this module these helpers were copy-pasted across `scheduling`, `confirmation_email`,
`modification_email`, `commands`, and `photos.store`. They are gathered here, one function per
distinct behaviour, because the *sources* legitimately differ in how much of a date they carry:

  - **Emails** always carry a full date with year ("Aug 20, 2026")      → `parse_email_date`
  - **SMS booking markers** never carry a year ("from 09/01 to 09/06")  → `parse_booking_date`
  - **Stored `stay_dates`** may be ISO, MM/DD/YYYY, or bare MM/DD        → `parse_stay`
  - **Telegram command args** accept a few explicit forms, else MM/DD    → `parse_command_date`

Only stdlib is imported, so this module has no project dependencies and can't create an
import cycle (every other module may import it freely).
"""
from datetime import date, datetime, timedelta

# "Aug 20, 2026" / "August 20, 2026" — the confirmation + modification email date format.
_EMAIL_FORMATS = ("%b %d, %Y", "%B %d, %Y")


def parse_email_date(text):
    """An email date string ('Aug 20, 2026' / 'August 20, 2026') -> date, or None."""
    for fmt in _EMAIL_FORMATS:
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def parse_booking_date(text, today=None):
    """An SMS/booking date -> date. Accepts 'MM/DD' or 'MM/DD/YYYY'.

    The confirmation SMS omits the year ('from 09/01 to 09/06'). A booking is in the
    future, so a bare MM/DD resolves to its next occurrence — which also rolls a
    December→January range into the following year. A 2-digit year is treated as 20xx.
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


def _parse_stay_token(token, today=None):
    """One end of a stored stay: ISO ('2026-10-04'), 'MM/DD/YYYY', or bare 'MM/DD'.

    A bare 'MM/DD' is assumed to be THIS year (not strptime's 1900 default — a year-1900 date
    silently fails every 'is this stay current?' comparison).
    """
    token = (token or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(token, "%m/%d").date().replace(
            year=(today or date.today()).year)
    except ValueError:
        return None


def parse_stay(stay_dates, today=None):
    """A stored `stay_dates` string -> (start, end) dates. Either may be None.

    Splits on ' to ' ('2026-10-04 to 2026-10-24'); a single date ('08/31/2026') yields
    start == end. Handles ISO, MM/DD/YYYY, and bare MM/DD (this year) per token.
    """
    if not stay_dates:
        return None, None
    parts = [p.strip() for p in str(stay_dates).split(" to ")]
    start = _parse_stay_token(parts[0], today)
    end = _parse_stay_token(parts[1], today) if len(parts) > 1 else start
    return start, (end or start)


def parse_command_date(text, today=None):
    """A Telegram command's date arg -> date. Accepts 'YYYY-MM-DD', 'MM/DD/YYYY',
    'MM-DD-YYYY', else falls back to `parse_booking_date` (bare MM/DD → next occurrence)."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return parse_booking_date(text, today=today)


def pretty(iso_date):
    """'2026-09-01' -> 'Tue, Sep 1' for a friendly card line. Passes non-ISO input through."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%a, %b %-d")
    except (ValueError, TypeError):
        return iso_date
