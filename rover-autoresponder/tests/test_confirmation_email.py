"""Pay-first bookings: confirmation arrives ONLY by email, never by SMS."""
from datetime import date

from autoresponder import store, config
from autoresponder import telegram_notify as tg
from autoresponder.confirmation_email import (
    handle_confirmation_email, normalize_phone, parse_confirmation_email,
)
from autoresponder.scheduling import DROPOFF, PICKUP, PENDING
from autoresponder.sms_parser import parse_sms
from autoresponder.sms_pipeline import handle_sms
from tests.test_scheduling import FakeCalendar

# The real email (2026-08-21)
SUBJECT = "Confirmed: Buddy's upcoming booking from Nov 20, 2026 - Nov 27, 2026"
BODY = """Success!
Booking details
Dates: Nov 20, 2026 - Nov 27, 2026
Owner: J
Phone number: (323) 458-5614
Emergency contact: Molly Rogers (+17185106600)
Location: Your home
Address:
9708 Cottle Dr
Austin
TX
78753
Pet information
Pet(s): Buddy
Care instructions: Review pet profile(s)
Payment information
Booking price: $575.00
"""
PHONE = "+13234585614"


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "rover@g.com")
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    monkeypatch.setattr(tg, "send_alert", lambda t: True)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: 1)
    return store.init_db(str(tmp_path / "ce.db"))


# --- phone normalization (this is what makes correlation exact) ---
def test_phone_normalization():
    assert normalize_phone("(323) 458-5614") == PHONE
    assert normalize_phone("+1 323-458-5614") == PHONE
    assert normalize_phone("3234585614") == PHONE
    assert normalize_phone("") is None


# --- parsing ---
def test_parses_the_real_email():
    info = parse_confirmation_email(SUBJECT, BODY)
    assert info["pet_name"] == "Buddy"
    assert info["owner_name"] == "J"
    assert info["phone"] == PHONE
    assert info["start_date"] == date(2026, 11, 20)
    assert info["end_date"] == date(2026, 11, 27)


def test_other_emails_ignored():
    assert parse_confirmation_email("Your revised itinerary", BODY) is None
    assert parse_confirmation_email("New message from X about Y's stay", "") is None


# --- the pay-first flow end to end ---
def test_email_confirmation_places_calendar_events(tmp_path, monkeypatch):
    """No confirmation SMS ever arrives — the email must do the whole job."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    assert handle_confirmation_email(conn, SUBJECT, BODY, calendar=cal) == PHONE
    assert len(cal.created) == 2
    assert store.get_scheduling_event(conn, PHONE, 1, DROPOFF)[2] == "2026-11-20"
    assert store.get_scheduling_event(conn, PHONE, 1, PICKUP)[2] == "2026-11-27"


def test_correlates_to_an_existing_sms_thread(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    handle_sms(conn, PHONE, "[ New booking request (boarding) from J: Buddy "
                            "(3 yr, 50 lbs) 11/20/2026 to 11/27/2026. Book @ r.rover.com/x ]")
    handle_confirmation_email(conn, SUBJECT, BODY, calendar=FakeCalendar())
    row = store.get_thread(conn, PHONE)
    assert row[4] == "converted"
    assert store.has_booked(conn, PHONE) is True


def test_idempotent_with_the_sms_marker(tmp_path, monkeypatch):
    """If both signals arrive, don't create the events twice."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_sms(conn, PHONE, "[ New booking request (boarding) from J: Buddy "
                            "(3 yr, 50 lbs) 11/20/2026 to 11/27/2026. Book @ r.rover.com/x ]")
    handle_confirmation_email(conn, SUBJECT, BODY, calendar=cal)
    handle_confirmation_email(conn, SUBJECT, BODY, calendar=cal)
    assert len(cal.created) == 2                      # not four


def test_missing_phone_alerts_rather_than_guessing(tmp_path, monkeypatch):
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    body = BODY.replace("Phone number: (323) 458-5614", "")
    assert handle_confirmation_email(conn, SUBJECT, body,
                                     calendar=FakeCalendar()) is None
    assert alerts and "NOT on your calendar" in alerts[0]


# --- the "waiting for you to accept" SMS ---
def test_awaiting_accept_marker_parsed():
    m = parse_sms("+1555", "[ J D. wants you to care for Buddy on Rover! "
                           "Confirm booking ASAP @ r.rover.com/kqjuM4 ]")
    assert m.kind == "awaiting_accept"
    assert m.owner_name == "J D."
    assert m.pet_name == "Buddy"


def test_awaiting_accept_alerts_and_does_not_draft(tmp_path, monkeypatch):
    alerts, scheduled = [], []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    handle_sms(conn, PHONE, "[ J D. wants you to care for Buddy on Rover! "
                            "Confirm booking ASAP @ r.rover.com/kqjuM4 ]",
               schedule_draft=scheduled.append)
    assert scheduled == []                            # no wasted API call
    assert alerts and "ACCEPT" in alerts[0]
