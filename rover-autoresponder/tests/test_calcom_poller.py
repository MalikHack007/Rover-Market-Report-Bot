"""C3: matching Cal.com bookings to events, and the PENDING -> CONFIRMED transitions."""
from datetime import date

from autoresponder import store, config
from autoresponder import telegram_notify as tg
from autoresponder.calcom_client import normalize
from autoresponder.calcom_poller import match_event, process_bookings
from autoresponder.scheduling import (
    CONFIRMED, DROPOFF, PENDING, PICKUP, ensure_links, on_booking_confirmed,
)
from autoresponder.sms_pipeline import handle_sms
from tests.test_scheduling import FakeCalendar

A = "+15125550001"
B = "+15125550002"
INQ = ("[ New booking request (boarding) from Jessica: Archie (4 yr, 30 lbs) "
       "09/01/2026 to 09/06/2026. Book @ r.rover.com/x ]")


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "rover@g.calendar.google.com")
    monkeypatch.setattr(tg, "send_alert", lambda t: True)
    conn = store.init_db(str(tmp_path / "c3.db"))
    handle_sms(conn, A, INQ)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    ensure_links(conn, A, 1, owner_name="Jessica")
    return conn


def _booking(ref=None, slug="dropoff", start="2026-09-01T10:00:00",
             end="2026-09-01T10:30:00", status="accepted", uid="bk-1", name="Jessica"):
    return normalize({
        "uid": uid, "status": status, "start": start, "end": end,
        "eventType": {"slug": slug}, "attendees": [{"name": name}],
        "metadata": ({"ref": str(ref)} if ref else {}),
    })


# --- normalization ---
def test_normalize_extracts_fields():
    b = _booking(ref=7)
    assert b["id"] == "bk-1" and b["ref"] == "7"
    assert b["event_type_slug"] == "dropoff"
    assert b["attendee_name"] == "Jessica"
    assert b["cancelled"] is False


def test_cancelled_status_detected():
    assert _booking(status="cancelled")["cancelled"] is True
    assert _booking(status="CANCELLED")["cancelled"] is True


# --- matching ---
def test_match_by_ref(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    row = match_event(conn, _booking(ref=ev_id))
    assert row and row[0] == ev_id


def test_match_falls_back_to_kind_and_date(tmp_path, monkeypatch):
    """If Cal.com doesn't echo our metadata, match on event type + date."""
    conn = _db(tmp_path, monkeypatch)
    row = match_event(conn, _booking(ref=None, slug="dropoff",
                                     start="2026-09-01T10:00:00"))
    assert row and row[3] == DROPOFF


def test_fallback_disambiguates_by_attendee(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    # a second client with a drop-off on the SAME day
    handle_sms(conn, B, ("[ New booking request (boarding) from Priya: Bolt "
                         "(2 yr, 20 lbs) 09/01/2026 to 09/03/2026. Book @ r.rover.com/y ]"))
    on_booking_confirmed(conn, B, "Bolt", "09/01", "09/03",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    row = match_event(conn, _booking(ref=None, name="Priya"))
    assert row and row[1] == B


def test_unknown_slug_does_not_match(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert match_event(conn, _booking(ref=None, slug="haircut")) is None


# --- confirm ---
def test_booking_confirms_and_moves_event(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    assert process_bookings(conn, [_booking(ref=ev_id)], cal) == 1
    ev = store.get_scheduling_event(conn, A, 1, DROPOFF)
    assert ev[1] == CONFIRMED
    assert ev[3] == "2026-09-01T10:00:00"
    (eid, kwargs) = cal.updated[0]
    assert kwargs["summary"] == "Archie Drop-off (CONFIRMED)"
    assert kwargs["start_iso"] == "2026-09-01T10:00:00"


def test_confirmed_event_becomes_opaque(tmp_path, monkeypatch):
    """A confirmed time is a real commitment and must block other bookings."""
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    process_bookings(conn, [_booking(ref=ev_id)], cal)
    assert cal.updated[0][1]["transparency"] == "opaque"


def test_reapplying_same_booking_is_noop(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    b = _booking(ref=ev_id)
    assert process_bookings(conn, [b], cal) == 1
    assert process_bookings(conn, [b], cal) == 0        # idempotent
    assert len(cal.updated) == 1


def test_reschedule_moves_the_event(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    process_bookings(conn, [_booking(ref=ev_id)], cal)
    process_bookings(conn, [_booking(ref=ev_id, start="2026-09-01T14:00:00",
                                     end="2026-09-01T14:30:00")], cal)
    assert store.get_scheduling_event(conn, A, 1, DROPOFF)[3] == "2026-09-01T14:00:00"


def test_wrong_day_booking_alerts(tmp_path, monkeypatch):
    """The link pre-selects a date but doesn't enforce it."""
    alerts = []
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    process_bookings(conn, [_booking(ref=ev_id, start="2026-09-04T10:00:00",
                                     end="2026-09-04T10:30:00")], FakeCalendar())
    assert any("booked" in a and "09-04" in a for a in alerts)


# --- cancel ---
def test_cancelled_slot_reverts_to_pending(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cal = FakeCalendar()
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    process_bookings(conn, [_booking(ref=ev_id)], cal)
    process_bookings(conn, [_booking(ref=ev_id, status="cancelled")], cal)
    ev = store.get_scheduling_event(conn, A, 1, DROPOFF)
    assert ev[1] == PENDING
    assert ev[3] is None                                 # scheduled_at cleared
    assert cal.updated[-1][1]["transparency"] == "transparent"


def test_cancel_of_unknown_booking_ignored(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    assert process_bookings(conn, [_booking(ref=ev_id, status="cancelled")],
                            FakeCalendar()) == 0        # was never confirmed


# --- outage detection (regression: alerting used to be dead code) ---
class _FakeClient:
    def __init__(self, result): self.result = result
    def list_bookings(self, **k): return self.result


def test_unavailable_raises_not_silently_empty(tmp_path, monkeypatch):
    """A failed request must be distinguishable from [] (no bookings)."""
    from autoresponder.calcom_client import TransientCalcomError
    from autoresponder.calcom_poller import poll_once
    import pytest
    conn = _db(tmp_path, monkeypatch)
    with pytest.raises(TransientCalcomError):
        poll_once(conn, client=_FakeClient(None), calendar=FakeCalendar())


def test_client_raises_after_exhausting_retries(monkeypatch):
    """The client itself signals an outage rather than returning []."""
    import pytest
    import requests as _rq
    from autoresponder import calcom_client
    from autoresponder.calcom_client import CalcomClient, TransientCalcomError
    monkeypatch.setattr(calcom_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "CALCOM_RETRIES", 2)
    monkeypatch.setattr(calcom_client.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _rq.exceptions.ReadTimeout("stalled")))
    with pytest.raises(TransientCalcomError):
        CalcomClient(api_key="k").list_bookings()


def test_client_retries_then_succeeds(monkeypatch):
    """A single stall shouldn't count as an outage."""
    import requests as _rq
    from autoresponder import calcom_client
    from autoresponder.calcom_client import CalcomClient
    monkeypatch.setattr(calcom_client.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _R:
        status_code = 200
        def json(self): return {"data": []}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rq.exceptions.ReadTimeout("stalled")
        return _R()
    monkeypatch.setattr(calcom_client.requests, "get", flaky)
    assert CalcomClient(api_key="k").list_bookings() == []
    assert calls["n"] == 2


def test_no_bookings_is_not_an_error(tmp_path, monkeypatch):
    from autoresponder.calcom_poller import poll_once
    conn = _db(tmp_path, monkeypatch)
    assert poll_once(conn, client=_FakeClient([]), calendar=FakeCalendar()) == 0


def test_repeated_outage_alerts(tmp_path, monkeypatch):
    """A sustained outage must reach Telegram — booked times silently not arriving
    is exactly the failure this system must never have."""
    import threading
    from autoresponder import calcom_poller
    conn = _db(tmp_path, monkeypatch)
    alerts = []
    monkeypatch.setattr(calcom_poller, "_alert", lambda t: alerts.append(t))
    monkeypatch.setattr(config, "CALCOM_ALERT_AFTER", 3)
    monkeypatch.setattr(config, "CALCOM_POLL_SECONDS", 0)
    from autoresponder.calcom_client import TransientCalcomError
    monkeypatch.setattr(calcom_poller, "poll_once",
                        lambda *a, **k: (_ for _ in ()).throw(
                            TransientCalcomError("down")))
    stop = threading.Event()
    calls = {"n": 0}
    real_sleep = calcom_poller.time.sleep if hasattr(calcom_poller, "time") else None

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= 4:
            stop.set()
    import time as _t
    monkeypatch.setattr(_t, "sleep", fake_sleep)
    calcom_poller.poll_loop(conn, stop_event=stop, interval=0)
    assert alerts and "NOT reaching your calendar" in alerts[0]
    assert len(alerts) == 1          # alerts once, doesn't spam every poll


# --- timezone (regression: 9pm Central looked like the NEXT day) ---
def test_evening_booking_keeps_local_date(tmp_path, monkeypatch):
    """cal.com sends UTC: 9:00 PM Sep 7 Central == 02:00 Sep 8 UTC."""
    from autoresponder.calcom_poller import _iso_date, _local_iso
    monkeypatch.setattr(config, "CALENDAR_TIMEZONE", "America/Chicago")
    assert _iso_date("2026-09-08T02:00:00.000Z") == "2026-09-07"
    assert _local_iso("2026-09-08T02:00:00.000Z").startswith("2026-09-07T21:00")


def test_evening_booking_does_not_trigger_wrong_day_alert(tmp_path, monkeypatch):
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CALENDAR_TIMEZONE", "America/Chicago")
    monkeypatch.setattr(tg, "send_alert", lambda t: alerts.append(t) or True)
    ev_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]   # target 2026-09-01
    # 9pm local on the correct day, expressed in UTC as the following day
    process_bookings(conn, [_booking(ref=ev_id, start="2026-09-02T02:00:00.000Z",
                                     end="2026-09-02T02:30:00.000Z")], FakeCalendar())
    assert not any("booked" in a and "wrong" in a.lower() for a in alerts)
    ev = store.get_scheduling_event(conn, A, 1, DROPOFF)
    assert ev[1] == CONFIRMED
    assert ev[3].startswith("2026-09-01T21:00")                   # stored as local


# --- unmatched bookings must be surfaced, not swallowed ---
def test_ambiguous_booking_alerts(tmp_path, monkeypatch):
    """Several pending drop-offs share a date and the attendee matches none of them."""
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: alerts.append(t) or 1)
    for num, owner, pet in ((B, "Zed", "Bolt"), ("+15125550003", "Ada", "Cocoa")):
        handle_sms(conn, num, (f"[ New booking request (boarding) from {owner}: {pet} "
                               "(2 yr, 20 lbs) 09/01/2026 to 09/03/2026. "
                               "Book @ r.rover.com/y ]"))
        on_booking_confirmed(conn, num, pet, "09/01", "09/03",
                             calendar=FakeCalendar(), today=date(2026, 8, 20))
    # 3 pending drop-offs on 09/01, no ref, attendee matches nobody -> can't decide
    process_bookings(conn, [_booking(ref=None, name="Nobody", uid="ambig-1")],
                     FakeCalendar())
    assert alerts and "not on your calendar" in alerts[0]


def test_unmatched_booking_alerts_once_not_every_poll(tmp_path, monkeypatch):
    """The poll window covers all FUTURE bookings, so an orphan is re-seen every 60s.
    It must be reported once, then stay quiet."""
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: alerts.append(t) or 1)
    # a date with no pending event at all -> matches nothing
    b = _booking(ref=None, slug="dropoff", start="2030-01-01T15:00:00.000Z",
                 end="2030-01-01T15:30:00.000Z", uid="orphan-1")
    for _ in range(3):
        process_bookings(conn, [b], FakeCalendar())
    assert len(alerts) == 1                    # deduped across polls


def test_cancelled_orphan_is_ignored_quietly(tmp_path, monkeypatch):
    """A cancelled booking we never placed is nothing to reconcile."""
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: alerts.append(t) or 1)
    b = _booking(ref=None, status="cancelled", uid="cancelled-orphan",
                 start="2030-02-02T15:00:00.000Z", end="2030-02-02T15:30:00.000Z")
    assert process_bookings(conn, [b], FakeCalendar()) == 0
    assert alerts == []


def test_stale_ref_never_hijacks_another_clients_event(tmp_path, monkeypatch):
    """A booking whose entry was deleted must NOT fuzzy-match someone else's event.

    Falling back to kind+date here would move a different client's calendar — worse
    than doing nothing.
    """
    alerts = []
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tg, "send_message", lambda t, **k: alerts.append(t) or 1)
    before = store.get_scheduling_event(conn, A, 1, DROPOFF)
    stale = _booking(ref=9999, uid="stale-1", start="2026-09-01T15:00:00.000Z",
                     end="2026-09-01T15:30:00.000Z", name="Ghost")
    assert process_bookings(conn, [stale], FakeCalendar()) == 0
    after = store.get_scheduling_event(conn, A, 1, DROPOFF)
    assert after == before                      # untouched
    assert alerts == []                          # and no spurious "ambiguous" alert