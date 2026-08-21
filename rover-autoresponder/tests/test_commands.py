"""C5: private-booking commands."""
from datetime import date, timedelta

from autoresponder import store, config, commands
from autoresponder import telegram_notify as tg
from autoresponder import sms_approve
from autoresponder.commands import handle_command
from autoresponder.scheduling import DROPOFF, PICKUP, PENDING, CANCELLED
from tests.test_scheduling import FakeCalendar


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "rover@g.com")
    monkeypatch.setattr(tg, "send_alert", lambda t: True)
    return store.init_db(str(tmp_path / "c5.db"))


def _future(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_booking_creates_events_and_links(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    out = handle_command(conn, f"/booking Willow {_future(10)} {_future(13)} Sarah", cal)
    assert "Willow" in out and "Sarah" in out
    assert "cal.com/malik/dropoff" in out and "cal.com/malik/pickup" in out
    assert len(cal.created) == 2                       # drop-off + pick-up placeholders
    rows = store.list_scheduling_events(conn)
    assert len(rows) == 2
    assert all(r[4] == "private" for r in rows)        # tagged as private


def test_owner_is_optional(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    out = handle_command(conn, f"/booking Willow {_future(5)} {_future(7)}",
                         FakeCalendar())
    assert "Willow" in out


def test_bad_dates_rejected(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Couldn't read" in handle_command(conn, "/booking Willow notadate x",
                                             FakeCalendar())


def test_end_before_start_rejected(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    out = handle_command(conn, f"/booking Willow {_future(10)} {_future(3)}",
                         FakeCalendar())
    assert "before the start" in out


def test_duplicate_booking_refused(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)}", cal)
    out = handle_command(conn, f"/booking Willow {_future(10)} {_future(12)}", cal)
    assert "already exists" in out
    assert len(cal.created) == 2                       # not four


def test_bookings_lists_and_marks_private(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)} Sarah",
                   FakeCalendar())
    out = handle_command(conn, "/bookings")
    assert "Willow" in out and "🏠" in out


def test_cancel_deletes_calendar_events(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)}", cal)
    ev_id = store.list_scheduling_events(conn)[0][0]
    out = handle_command(conn, f"/cancelbooking {ev_id}", cal)
    assert "Cancelled 2" in out
    assert len(cal.deleted) == 2
    assert all(store.get_scheduling_event_by_id(conn, r[0])[5] == CANCELLED
               for r in store.list_scheduling_events(conn))


def test_move_updates_dates_and_regenerates_links(tmp_path, monkeypatch):
    """The link embeds the date, so moving MUST mint new links."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)}", cal)
    ev_id = store.list_scheduling_events(conn)[0][0]
    before = store.get_scheduling_event_by_id(conn, ev_id)[10]
    out = handle_command(conn, f"/movebooking {ev_id} {_future(20)} {_future(22)}", cal)
    after = store.get_scheduling_event_by_id(conn, ev_id)
    assert after[6] == _future(20)                     # target date moved
    assert after[10] != before                          # link regenerated
    assert _future(20) in after[10]
    assert "cal.com/malik" in out


def test_move_clears_a_previously_booked_time(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)}", cal)
    ev_id = store.list_scheduling_events(conn)[0][0]
    store.update_scheduling_event(conn, ev_id, status="confirmed",
                                  scheduled_at="2026-01-01T10:00:00")
    handle_command(conn, f"/movebooking {ev_id} {_future(20)} {_future(22)}", cal)
    row = store.get_scheduling_event_by_id(conn, ev_id)
    assert row[5] == PENDING and row[7] is None        # booking no longer valid


def test_unknown_id_handled(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "No scheduling event" in handle_command(conn, "/cancelbooking 999",
                                                   FakeCalendar())


def test_help_and_non_commands(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Private booking commands" in handle_command(conn, "/help")
    assert handle_command(conn, "just a normal message") is None


def test_commands_work_without_replying_to_a_card(tmp_path, monkeypatch):
    """A standalone /booking message must work — there's no card to reply to."""
    conn = _db(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda t, **k: sent.append(t) or 1)
    handled = sms_approve.handle_text_reply(
        conn, f"/booking Willow {_future(10)} {_future(12)}", chat_id=1,
        reply_to_message_id=None, calendar=FakeCalendar())
    assert handled is True
    assert sent and "Willow" in sent[0]


def test_pet_command_still_routes_to_name_recovery(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: 1)
    store.upsert_sms_thread(conn, "+1555", status="active")
    store.link_card(conn, 77, "+1555")
    assert sms_approve.handle_text_reply(conn, "/pet Maple", 1, 77) is True
    assert store.get_thread(conn, "+1555")[1] == "Maple"
