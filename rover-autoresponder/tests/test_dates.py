"""Coverage for the consolidated date helpers (autoresponder/dates.py)."""
from datetime import date

from autoresponder import dates

TODAY = date(2026, 8, 20)


# --- parse_email_date: full 'Mon DD, YYYY' from confirmation/modification emails ---
def test_email_date_short_and_long_month():
    assert dates.parse_email_date("Aug 20, 2026") == date(2026, 8, 20)
    assert dates.parse_email_date("August 20, 2026") == date(2026, 8, 20)


def test_email_date_bad_input_is_none():
    assert dates.parse_email_date("not a date") is None
    assert dates.parse_email_date(None) is None          # was a crash before consolidation


# --- parse_booking_date: SMS marker dates (no year) ---
def test_booking_date_bare_resolves_to_next_occurrence():
    assert dates.parse_booking_date("09/01", today=TODAY) == date(2026, 9, 1)


def test_booking_date_bare_already_passed_rolls_to_next_year():
    assert dates.parse_booking_date("01/05", today=TODAY) == date(2027, 1, 5)


def test_booking_date_explicit_year():
    assert dates.parse_booking_date("08/15/2026") == date(2026, 8, 15)
    assert dates.parse_booking_date("08/15/26") == date(2026, 8, 15)   # 2-digit → 20xx


# --- parse_stay: stored stay_dates in any of the stored formats ---
def test_stay_iso_range():
    assert dates.parse_stay("2026-10-04 to 2026-10-24") == (date(2026, 10, 4), date(2026, 10, 24))


def test_stay_single_date_start_equals_end():
    assert dates.parse_stay("08/31/2026") == (date(2026, 8, 31), date(2026, 8, 31))


def test_stay_bare_mmdd_assumes_this_year_not_1900():
    """Bare MM/DD (SMS-first bookings) must resolve to the given year, not strptime's 1900 —
    a 1900 date silently fails every 'is this stay current/upcoming?' check."""
    start, end = dates.parse_stay("09/01 to 09/06", today=TODAY)
    assert start == date(2026, 9, 1) and end == date(2026, 9, 6)


def test_stay_empty_is_none_pair():
    assert dates.parse_stay(None) == (None, None)
    assert dates.parse_stay("") == (None, None)


# --- parse_command_date: private-booking command args ---
def test_command_date_explicit_forms():
    assert dates.parse_command_date("2026-08-15") == date(2026, 8, 15)
    assert dates.parse_command_date("08/15/2026") == date(2026, 8, 15)
    assert dates.parse_command_date("08-15-2026") == date(2026, 8, 15)


def test_command_date_falls_back_to_next_occurrence():
    assert dates.parse_command_date("09/01", today=TODAY) == date(2026, 9, 1)


# --- pretty: friendly card formatting ---
def test_pretty_formats_iso_and_passes_through_other():
    assert dates.pretty("2026-09-01") == "Tue, Sep 1"
    assert dates.pretty("") == ""
    assert dates.pretty("whatever") == "whatever"
