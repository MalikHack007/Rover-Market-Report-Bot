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


# --- REAL email layout: label and value are in separate table cells ---
# The hand-typed sample above has them inline, which is why this parsed in tests but
# failed on live mail. This is the shape Gmail actually renders.
REAL_SUBJECT = "Confirmed: Gus's upcoming booking from Sep 4, 2026 - Sep 7, 2026"
REAL_BODY = """Your booking with Linda is
confirmed
.
You're all set! See booking details, care instructions, and payment information.
Success!
Booking details
Dates:
Sep 4, 2026 - Sep 7, 2026
Owner:
Linda
Phone number:
(720) 370-7925
Emergency contact:
Request from Linda
Location:
Your home
Address:
9708 Cottle Dr
Austin
TX
78753
Pet information
Pet(s):
Gus
Care instructions:
Review pet profile(s)
Payment information
Booking price:
$260.00
"""


def test_parses_real_multiline_layout():
    info = parse_confirmation_email(REAL_SUBJECT, REAL_BODY)
    assert info["pet_name"] == "Gus"
    assert info["owner_name"] == "Linda"
    assert info["phone"] == "+17203707925"
    assert info["start_date"] == date(2026, 9, 4)
    assert info["end_date"] == date(2026, 9, 7)


def test_real_layout_places_calendar_events(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    assert handle_confirmation_email(conn, REAL_SUBJECT, REAL_BODY,
                                     calendar=cal) == "+17203707925"
    assert len(cal.created) == 2
    assert store.get_scheduling_event(conn, "+17203707925", 1, DROPOFF)[2] == "2026-09-04"
    assert store.get_scheduling_event(conn, "+17203707925", 1, PICKUP)[2] == "2026-09-07"


def test_single_day_booking_has_no_end_date_in_subject():
    """e.g. 'Confirmed: Royal's upcoming booking from Aug 19, 2026'"""
    body = "Dates:\nAug 19, 2026\nOwner:\nCharlotte\nPhone number:\n(310) 555-0100\nPet(s):\nRoyal\n"
    info = parse_confirmation_email(
        "Confirmed: Royal's upcoming booking from Aug 19, 2026", body)
    assert info["start_date"] == date(2026, 8, 19)
    assert info["end_date"] == date(2026, 8, 19)      # same day, not None
    assert info["phone"] == "+13105550100"


def test_pet_names_with_punctuation():
    for subj, pet in (
        ("Confirmed: Cudi (Cuh-dee)'s upcoming booking from Aug 18, 2026", "Cudi (Cuh-dee)"),
        ("Confirmed: Rooster and Rocky's upcoming booking from Aug 15, 2026 - Aug 18, 2026",
         "Rooster and Rocky"),
    ):
        info = parse_confirmation_email(subj, "")
        assert info is not None and info["pet_name"] == pet


def test_phone_formats():
    for raw, want in (("(720) 370-7925", "+17203707925"),
                      ("720-370-7925", "+17203707925"),
                      ("+1 (720) 370-7925", "+17203707925")):
        body = f"Owner:\nLinda\nPhone number:\n{raw}\nPet(s):\nGus\n"
        assert parse_confirmation_email(REAL_SUBJECT, body)["phone"] == want


# --- don't cry wolf when the SMS marker already handled it ---
def test_no_false_alarm_when_already_scheduled(tmp_path, monkeypatch):
    """Both signals arrive for a normal booking. The SMS marker places the events;
    the email must not then claim it is NOT on the calendar."""
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    # SMS marker path already did the work
    handle_sms(conn, "+15125559999", "[ New booking request (boarding) from Ann: Oscar "
                                     "(4 yr, 30 lbs) 09/04/2026 to 09/07/2026. "
                                     "Book @ r.rover.com/x ]")
    from autoresponder.scheduling import on_booking_confirmed
    on_booking_confirmed(conn, "+15125559999", "Oscar", "09/04/2026", "09/07/2026",
                         calendar=FakeCalendar())
    # then the email arrives WITHOUT a usable phone number
    body = "Dates:\nSep 4, 2026 - Sep 7, 2026\nOwner:\nAnn\nPet(s):\nOscar\n"
    handle_confirmation_email(conn, "Confirmed: Oscar's upcoming booking from "
                                    "Sep 4, 2026 - Sep 7, 2026", body,
                              calendar=FakeCalendar())
    assert alerts == []                       # no contradictory warning


def test_still_alerts_when_genuinely_unmatched(tmp_path, monkeypatch):
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    body = "Dates:\nSep 4, 2026 - Sep 7, 2026\nOwner:\nAnn\nPet(s):\nNobodysDog\n"
    assert handle_confirmation_email(conn, "Confirmed: NobodysDog's upcoming booking "
                                           "from Sep 4, 2026 - Sep 7, 2026", body,
                                     calendar=FakeCalendar()) is None
    assert alerts and "NOT on your calendar" in alerts[0]


def test_pet_match_requires_the_same_date(tmp_path, monkeypatch):
    """A same-named pet on a different date must not suppress the alert."""
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    handle_sms(conn, "+15125559999", "[ New booking request (boarding) from Ann: Oscar "
                                     "(4 yr, 30 lbs) 12/01/2026 to 12/03/2026. "
                                     "Book @ r.rover.com/x ]")
    from autoresponder.scheduling import on_booking_confirmed
    on_booking_confirmed(conn, "+15125559999", "Oscar", "12/01/2026", "12/03/2026",
                         calendar=FakeCalendar())
    body = "Dates:\nSep 4, 2026 - Sep 7, 2026\nOwner:\nAnn\nPet(s):\nOscar\n"
    handle_confirmation_email(conn, "Confirmed: Oscar's upcoming booking from "
                                    "Sep 4, 2026 - Sep 7, 2026", body,
                              calendar=FakeCalendar())
    assert alerts                              # different booking -> still alerts


# --- tolerant phone extraction ---
def test_phone_label_without_colon_and_blank_lines():
    body = "Owner:\nLinda\nPhone number\n\n\n(720) 370-7925\nPet(s):\nGus\n"
    assert parse_confirmation_email(REAL_SUBJECT, body)["phone"] == "+17203707925"


def test_phone_spaced_format():
    body = "Owner:\nLinda\nPhone number:\n+1 720 370 7925\nPet(s):\nGus\n"
    assert parse_confirmation_email(REAL_SUBJECT, body)["phone"] == "+17203707925"


def test_no_phone_at_all_returns_none():
    body = "Owner:\nLinda\nPet(s):\nGus\n"
    assert parse_confirmation_email(REAL_SUBJECT, body)["phone"] is None


# --- both signals fire for a normal booking: no duplicate work ---
def _both_signals(conn, cal, cards, monkeypatch):
    from unittest.mock import patch
    monkeypatch.setattr(tg, "send_message", lambda t, **k: cards.append(t) or 1)
    with patch("autoresponder.scheduling.GoogleCalendar", lambda *a, **k: cal):
        handle_sms(conn, PHONE, "[ New booking request (boarding) from J: Buddy "
                                "(3 yr, 50 lbs) 11/20/2026 to 11/27/2026. "
                                "Book @ r.rover.com/x ]")
        handle_sms(conn, PHONE, "[ J has confirmed a booking request (stay) with Buddy "
                                "from 11/20 to 11/27 ]")
        mid_events = len(store.list_scheduling_events(conn))
        mid_cards = len(cards)
        handle_confirmation_email(conn, SUBJECT, BODY, calendar=cal)
    return mid_events, mid_cards


def test_email_after_sms_creates_no_duplicate_events(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal, cards = FakeCalendar(), []
    mid_events, _ = _both_signals(conn, cal, cards, monkeypatch)
    assert mid_events == 2
    assert len(store.list_scheduling_events(conn)) == 2      # still 2, not 4
    assert len(cal.created) == 2                             # no extra calendar writes


def test_email_after_sms_does_not_resend_the_links_card(tmp_path, monkeypatch):
    """The events dedupe on their unique constraint, but the card would not."""
    conn = _db(tmp_path, monkeypatch)
    cal, cards = FakeCalendar(), []
    _, mid_cards = _both_signals(conn, cal, cards, monkeypatch)
    assert len(cards) == mid_cards                            # no second links card