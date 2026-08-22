"""C5: private-booking commands."""
from datetime import date, timedelta

from autoresponder import store, config, commands
from autoresponder import telegram_notify as tg
from autoresponder import sms_approve
from autoresponder.commands import handle_command
from autoresponder.scheduling import DROPOFF, MEET_GREET, PICKUP, PENDING, CANCELLED
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
    assert "/booking" in handle_command(conn, "/help")
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


# --- single-leg links ---
def test_dropoff_only_creates_one_event(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    out = handle_command(conn, f"/dropoff Willow {_future(10)} Sarah", cal)
    assert "Drop-off" in out and "Willow" in out
    assert "cal.com/malik/dropoff" in out
    assert len(cal.created) == 1                       # just the one leg
    rows = store.list_scheduling_events(conn)
    assert len(rows) == 1 and rows[0][3] == DROPOFF


def test_pickup_only_creates_one_event(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    out = handle_command(conn, f"/pickup Willow {_future(12)}", cal)
    assert "Pick-up" in out and "cal.com/malik/pickup" in out
    assert len(cal.created) == 1
    assert store.list_scheduling_events(conn)[0][3] == PICKUP


def test_single_legs_for_same_pet_dont_collide(tmp_path, monkeypatch):
    """A standalone drop-off and pick-up for one pet must be separate entries."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/dropoff Willow {_future(10)}", cal)
    handle_command(conn, f"/pickup Willow {_future(12)}", cal)
    rows = store.list_scheduling_events(conn)
    assert len(rows) == 2
    assert {r[3] for r in rows} == {DROPOFF, PICKUP}
    assert rows[0][1] != rows[1][1]                    # different thread keys


def test_duplicate_single_leg_refused(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/dropoff Willow {_future(10)}", cal)
    out = handle_command(conn, f"/dropoff Willow {_future(10)}", cal)
    assert "already exists" in out
    assert len(cal.created) == 1


def test_single_leg_bad_date(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Couldn't read" in handle_command(conn, "/dropoff Willow notadate",
                                             FakeCalendar())


def test_single_leg_usage_when_incomplete(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Usage" in handle_command(conn, "/dropoff Willow", FakeCalendar())


# --- meet & greet ---
def test_meetgreet_has_no_date_and_no_calendar_hold(tmp_path, monkeypatch):
    """Open range: nothing is placed until the client actually books."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    out = handle_command(conn, "/meetgreet Willow Sarah", cal)
    assert "Meet" in out and "cal.com/malik/meet-greet" in out
    assert "date=" not in out                           # open range
    assert cal.created == []                            # no placeholder yet
    rows = store.list_scheduling_events(conn)
    assert len(rows) == 1 and rows[0][6] is None        # no target date


def test_meetgreet_needs_a_pet(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Usage" in handle_command(conn, "/meetgreet", FakeCalendar())


# --- re-showing links ---
def test_links_reshows_both_legs(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Willow {_future(10)} {_future(12)} Sarah", cal)
    ev_id = store.list_scheduling_events(conn)[0][0]
    out = handle_command(conn, f"/links {ev_id}")
    assert "cal.com/malik/dropoff" in out and "cal.com/malik/pickup" in out
    assert "Willow" in out


def test_links_unknown_id(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "No scheduling event" in handle_command(conn, "/links 999")


def test_help_lists_the_new_commands(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    out = handle_command(conn, "/help")
    for c in ("/dropoff", "/pickup", "/meetgreet", "/links"):
        assert c in out


# --- /retarget: fix one leg's expected date without disturbing a booked time ---
def test_retarget_keeps_a_confirmed_time(tmp_path, monkeypatch):
    """The real case: a private client shifts pickup a day but already chose a slot."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    ev = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    store.update_scheduling_event(conn, ev[0], status="confirmed",
                                  scheduled_at=f"{_future(14)}T09:00:00-05:00")
    out = handle_command(conn, f"/retarget {ev[0]} {_future(15)}", cal)
    row = store.get_scheduling_event_by_id(conn, ev[0])
    assert row[6] == _future(15)                    # expected date updated
    assert row[5] == "confirmed"                    # still confirmed
    assert row[7] == f"{_future(14)}T09:00:00-05:00"  # booked time untouched
    assert cal.updated == []                        # calendar event NOT moved
    assert "unchanged" in out


def test_retarget_moves_an_unbooked_leg(tmp_path, monkeypatch):
    """Nothing booked yet -> move the placeholder and re-mint the link."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    ev = [r for r in store.list_scheduling_events(conn) if r[3] == DROPOFF][0]
    old_link = store.get_scheduling_event_by_id(conn, ev[0])[10]
    out = handle_command(conn, f"/retarget {ev[0]} {_future(11)}", cal)
    row = store.get_scheduling_event_by_id(conn, ev[0])
    assert row[6] == _future(11)
    assert row[10] != old_link and _future(11) in row[10]   # link re-minted
    assert cal.updated                                       # placeholder moved
    assert "still unbooked" in out


def test_retarget_does_not_touch_the_other_leg(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    drop = [r for r in store.list_scheduling_events(conn) if r[3] == DROPOFF][0]
    pick_before = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    handle_command(conn, f"/retarget {drop[0]} {_future(11)}", cal)
    pick_after = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    assert pick_after == pick_before


def test_retarget_validation(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "Usage" in handle_command(conn, "/retarget 1", FakeCalendar())
    assert "No scheduling event" in handle_command(conn, "/retarget 999 2026-09-08",
                                                   FakeCalendar())
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    ev = store.list_scheduling_events(conn)[0][0]
    assert "Couldn't read" in handle_command(conn, f"/retarget {ev} notadate", cal)


# --- /movebooking must not disturb legs whose date didn't change ---
def test_move_leaves_unchanged_leg_completely_alone(tmp_path, monkeypatch):
    """Extending a stay by a day must NOT reset a confirmed drop-off."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    drop = [r for r in store.list_scheduling_events(conn) if r[3] == DROPOFF][0]
    store.update_scheduling_event(conn, drop[0], status="confirmed",
                                  scheduled_at=f"{_future(10)}T09:00:00-05:00",
                                  booking_ref="bk-keep")
    before = store.get_scheduling_event_by_id(conn, drop[0])

    # only the END date changes
    out = handle_command(conn, f"/movebooking {drop[0]} {_future(10)} {_future(15)}", cal)
    after = store.get_scheduling_event_by_id(conn, drop[0])
    assert after == before                       # drop-off byte-identical
    assert "unchanged" in out
    # and the pick-up did move
    pick = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    assert pick[6] == _future(15)


def test_move_invalidates_a_confirmed_leg_whose_date_changed(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    pick = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    store.update_scheduling_event(conn, pick[0], status="confirmed",
                                  scheduled_at=f"{_future(14)}T17:00:00-05:00",
                                  booking_ref="bk-stale")
    out = handle_command(conn, f"/movebooking {pick[0]} {_future(10)} {_future(16)}", cal)
    row = store.get_scheduling_event_by_id(conn, pick[0])
    assert row[6] == _future(16)
    assert row[5] == PENDING and row[7] is None      # booked time cleared
    assert "needs rebooking" in out


def test_superseded_booking_is_not_reconfirmed_by_the_poller(tmp_path, monkeypatch):
    """The old cal.com booking is still live; without neutralising it the poller
    would re-confirm the invalid time on the next pass."""
    from autoresponder.calcom_poller import _already_unmatched
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    pick = [r for r in store.list_scheduling_events(conn) if r[3] == PICKUP][0]
    store.update_scheduling_event(conn, pick[0], status="confirmed",
                                  scheduled_at=f"{_future(14)}T17:00:00-05:00",
                                  booking_ref="bk-superseded")
    handle_command(conn, f"/movebooking {pick[0]} {_future(10)} {_future(16)}", cal)
    assert _already_unmatched(conn, "bk-superseded") is True


def test_move_to_identical_dates_is_a_noop(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    handle_command(conn, f"/booking Kylo {_future(10)} {_future(14)}", cal)
    before = store.list_scheduling_events(conn)
    cal.updated.clear()
    out = handle_command(conn, f"/movebooking {before[0][0]} {_future(10)} {_future(14)}",
                         cal)
    assert "Nothing changed" in out
    assert store.list_scheduling_events(conn) == before
    assert cal.updated == []                     # no pointless calendar writes