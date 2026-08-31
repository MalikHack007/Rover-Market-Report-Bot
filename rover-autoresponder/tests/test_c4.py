"""Addendum B / C4 — booking cancellation (SMS) and modification (email)."""
from datetime import date

from autoresponder import store
from autoresponder.scheduling import (
    DROPOFF, PICKUP, CANCELLED, PENDING, on_booking_confirmed, on_booking_cancelled,
)
from autoresponder.sms_parser import parse_sms
from autoresponder.modification_email import parse_modification_email, handle_modification_email

A = "+15125550001"


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
    return store.init_db(str(tmp_path / "c4.db"))


def _book(conn, num, owner, pet, stay):
    store.upsert_sms_thread(conn, num, owner_name=owner, pet_name=pet,
                            stay_dates=stay, status="converted")
    store.mark_has_booked(conn, num)


# --- cancellation (SMS) --------------------------------------------------
def test_cancelled_sms_parses():
    m = parse_sms(A, "[ Rover Update: Your booking from 08/15/2026 to 08/18/2026 with "
                     "Joshua K. has been cancelled. Review this @ r.rover.com/N4WtLK ]")
    assert m.kind == "cancelled"
    assert m.owner_name == "Joshua K."
    assert m.start_date == "08/15/2026" and m.end_date == "08/18/2026"


def test_on_booking_cancelled_removes_events_and_expires_links(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06", calendar=cal,
                         today=date(2026, 8, 20))
    store.set_meta(conn, f"links_sent:{A}:1", "1")
    assert len(cal.created) == 2

    removed = on_booking_cancelled(conn, A, calendar=cal)
    assert removed == 2
    assert len(cal.deleted) == 2                                   # both gcal events deleted
    for kind in (DROPOFF, PICKUP):
        assert store.get_scheduling_event(conn, A, 1, kind)[1] == CANCELLED
    assert not store.meta_exists(conn, f"links_sent:{A}:1")        # links expired
    # idempotent — a re-send of the cancellation does nothing more
    assert on_booking_cancelled(conn, A, calendar=cal) == 0


# --- modification (email) ------------------------------------------------
def test_modification_parse_real_body():
    info = parse_modification_email(
        "Your revised itinerary for your booking with Shadow",
        "We also sent these details to Dominique, so you're all set. "
        "Dates: Aug 28, 2026 - Aug 31, 2026")
    assert info["pet_name"] == "Shadow"
    assert info["owner_name"] == "Dominique"
    assert info["start_date"] == date(2026, 8, 28)
    assert info["end_date"] == date(2026, 8, 31)


def _modify(conn, cal, today):
    return handle_modification_email(
        conn, "Your revised itinerary for your booking with Shadow",
        "sent these details to Dominique, so you're all set. Dates: Aug 28, 2026 - Aug 31, 2026",
        calendar=cal, notify=False, today=today)


def test_modification_moves_a_CURRENT_booking(tmp_path):
    """A stay already in progress can be modified — not just upcoming ones."""
    conn = _db(tmp_path)
    cal = FakeCalendar()
    _book(conn, A, "Dominique W.", "Shadow", "2026-08-20 to 2026-08-25")
    on_booking_confirmed(conn, A, "Shadow", "08/20/2026", "08/25/2026", calendar=cal,
                         today=date(2026, 8, 15))
    tk = _modify(conn, cal, today=date(2026, 8, 22))              # today is DURING the stay
    assert tk == A
    assert store.get_scheduling_event(conn, A, 1, DROPOFF)[2] == "2026-08-28"
    assert store.get_scheduling_event(conn, A, 1, PICKUP)[2] == "2026-08-31"
    assert cal.updated                                            # gcal events were moved
    assert store.get_thread(conn, A)[2] == "2026-08-28 to 2026-08-31"


def test_modification_moves_an_UPCOMING_booking(tmp_path):
    conn = _db(tmp_path)
    cal = FakeCalendar()
    _book(conn, A, "Dominique W.", "Shadow", "2026-09-10 to 2026-09-14")
    on_booking_confirmed(conn, A, "Shadow", "09/10/2026", "09/14/2026", calendar=cal,
                         today=date(2026, 8, 15))
    tk = _modify(conn, cal, today=date(2026, 8, 22))             # today is BEFORE the stay
    assert tk == A
    assert store.get_scheduling_event(conn, A, 1, DROPOFF)[2] == "2026-08-28"


def test_modification_ambiguous_does_not_move(tmp_path, monkeypatch):
    from autoresponder import telegram_notify as tg
    alerts = []
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    conn = _db(tmp_path)
    cal = FakeCalendar()
    # two different clients, both Dominique + Shadow, both still upcoming -> ambiguous
    _book(conn, "+1", "Dominique W.", "Shadow", "2026-09-10 to 2026-09-14")
    _book(conn, "+2", "Dominique K.", "Shadow", "2026-09-20 to 2026-09-24")
    tk = handle_modification_email(
        conn, "Your revised itinerary for your booking with Shadow",
        "sent these details to Dominique. Dates: Aug 28, 2026 - Aug 31, 2026",
        calendar=cal, today=date(2026, 8, 22))
    assert tk is None
    assert alerts and "couldn't match" in alerts[0]


# --- modification: ONLY the changed leg's link is re-issued ---
def test_build_leg_links_message_single_leg(tmp_path):
    from autoresponder.scheduling import build_leg_links_message
    conn = _db(tmp_path)
    cal = FakeCalendar()
    _book(conn, A, "Dana", "Rex", "2026-09-01 to 2026-09-06")
    on_booking_confirmed(conn, A, "Rex", "09/01/2026", "09/06/2026", calendar=cal,
                         today=date(2026, 8, 20))
    text, links = build_leg_links_message(conn, A, [PICKUP])
    assert set(links) == {PICKUP}                          # only pick-up
    assert "Pick-up" in text and "Drop-off" not in text


def test_modification_pickup_only_reissues_only_pickup(tmp_path, monkeypatch):
    from autoresponder import telegram_notify as tg, sms_pipeline
    monkeypatch.setattr(tg, "send_alert", lambda t: True)
    captured = {}
    monkeypatch.setattr(sms_pipeline, "send_modified_links",
                        lambda conn, number, kinds: captured.update(kinds=list(kinds)) or True)
    conn = _db(tmp_path)
    cal = FakeCalendar()
    _book(conn, A, "Dominique W.", "Shadow", "2026-09-01 to 2026-09-06")
    on_booking_confirmed(conn, A, "Shadow", "09/01/2026", "09/06/2026", calendar=cal,
                         today=date(2026, 8, 20))
    # drop-off SAME (Sep 1); pick-up moves Sep 6 -> Sep 8
    handle_modification_email(
        conn, "Your revised itinerary for your booking with Shadow",
        "sent these details to Dominique. Dates: Sep 1, 2026 - Sep 8, 2026",
        calendar=cal, notify=True, today=date(2026, 8, 25))
    assert captured.get("kinds") == [PICKUP]               # only pick-up re-issued
    # and the drop-off leg is untouched on the calendar/DB
    assert store.get_scheduling_event(conn, A, 1, DROPOFF)[2] == "2026-09-01"
    assert store.get_scheduling_event(conn, A, 1, PICKUP)[2] == "2026-09-08"
