"""C1: date resolution + PENDING placeholder creation."""
from datetime import date

from autoresponder import store, scheduling, config
from autoresponder.scheduling import (
    DROPOFF, PICKUP, PENDING, parse_booking_date, title, on_booking_confirmed,
)
from autoresponder.sms_pipeline import handle_sms

A = "+15125550001"
INQ = ("[ New booking request (boarding) from Jessica: Archie (4 yr, 30 lbs) "
       "09/01/2026 to 09/06/2026. Book @ r.rover.com/x ]")
CONF = ("[ Jessica K. has confirmed a booking request (stay) with Archie "
        "from 09/01 to 09/06 - View on Rover r.rover.com/26fLH3 ]")


class FakeCalendar:
    def __init__(self, fail=False):
        self.fail, self.created, self.updated, self.deleted = fail, [], [], []
    def create_event(self, summary, start_iso, end_iso, description="",
                     transparency="transparent"):
        if self.fail:
            return None
        self.created.append((summary, start_iso, end_iso, transparency))
        return f"gcal-{len(self.created)}"
    def update_event(self, event_id, **k):
        self.updated.append((event_id, k)); return True
    def delete_event(self, event_id):
        self.deleted.append(event_id); return True


def _db(tmp_path):
    return store.init_db(str(tmp_path / "c1.db"))


# --- titles ---
def test_title_shows_pet_and_status():
    assert title("Archie", DROPOFF, PENDING) == "Archie Drop-off (PENDING)"
    assert title("Archie", PICKUP, "confirmed") == "Archie Pick-up (CONFIRMED)"
    assert title(None, DROPOFF, PENDING) == "Booking Drop-off (PENDING)"


# --- date resolution (the confirmation SMS has no year) ---
def test_bare_date_resolves_to_next_future_occurrence():
    today = date(2026, 8, 20)
    assert parse_booking_date("09/01", today=today) == date(2026, 9, 1)


def test_bare_date_already_passed_rolls_to_next_year():
    today = date(2026, 8, 20)
    assert parse_booking_date("01/05", today=today) == date(2027, 1, 5)


def test_new_year_range_rolls_end_date_forward():
    today = date(2026, 12, 1)
    start = parse_booking_date("12/28", today=today)
    end = parse_booking_date("01/03", today=today)
    assert start == date(2026, 12, 28)
    assert end == date(2027, 1, 3)
    assert end > start


def test_explicit_year_is_respected():
    assert parse_booking_date("08/15/2026") == date(2026, 8, 15)


def test_garbage_date_returns_none():
    assert parse_booking_date("not a date") is None
    assert parse_booking_date("") is None


# --- placement ---
def test_confirmation_places_two_pending_events(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    assert len(cal.created) == 2
    summaries = [c[0] for c in cal.created]
    assert "Archie Drop-off (PENDING)" in summaries
    assert "Archie Pick-up (PENDING)" in summaries


def test_placeholders_are_transparent(tmp_path):
    """Critical: an opaque placeholder would block its own confirmation slot."""
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    assert all(c[3] == "transparent" for c in cal.created)


def test_events_land_on_correct_dates(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    by_kind = {c[0]: c[1] for c in cal.created}
    assert by_kind["Archie Drop-off (PENDING)"].startswith("2026-09-01")
    assert by_kind["Archie Pick-up (PENDING)"].startswith("2026-09-06")


def test_same_day_daycare_still_gets_two_events(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Daisy", "07/28", "07/28", calendar=cal,
                         today=date(2026, 7, 1))
    assert len(cal.created) == 2
    starts = sorted(c[1] for c in cal.created)
    assert starts[0].startswith("2026-07-28") and starts[1].startswith("2026-07-28")
    assert starts[0] < starts[1]          # drop-off before pick-up


def test_duplicate_confirmation_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    assert len(cal.created) == 2          # NOT four


def test_events_recorded_in_db(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    ev = store.get_scheduling_event(conn, A, 1, DROPOFF)
    assert ev is not None
    assert ev[1] == PENDING
    assert ev[2] == "2026-09-01"
    assert ev[4] == "gcal-1"


def test_calendar_failure_alerts(tmp_path, monkeypatch):
    """A booking that isn't on the calendar must never fail silently."""
    from autoresponder import telegram_notify as tg
    alerts = []
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    conn = _db(tmp_path)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(fail=True), today=date(2026, 8, 20))
    assert alerts and "NOT on your calendar" in alerts[0]


def test_returning_client_new_episode_gets_own_events(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    handle_sms(conn, A, INQ)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    # a year later the same number sends a new request -> episode 2
    handle_sms(conn, A, ("[ New booking request (boarding) from Jessica: Archie "
                         "(5 yr, 31 lbs) 01/10/2027 to 01/12/2027. Book @ r.rover.com/y ]"))
    on_booking_confirmed(conn, A, "Archie", "01/10/2027", "01/12/2027", calendar=cal,
                         today=date(2026, 12, 1))
    assert len(cal.created) == 4          # two bookings, two events each
    assert store.get_scheduling_event(conn, A, 2, DROPOFF) is not None
