"""C2: Cal.com link generation and the scheduling-links message."""
from datetime import date

from autoresponder import store, config, scheduling
from autoresponder import telegram_notify as tg
from autoresponder.scheduling import (
    DROPOFF, PICKUP, MEET_GREET, build_link, ensure_links, build_scheduling_draft,
    on_booking_confirmed, ensure_meetgreet_link,
)
from autoresponder.sms_pipeline import handle_sms, send_scheduling_links
from tests.test_scheduling import FakeCalendar

A = "+15125550001"
INQ = ("[ New booking request (boarding) from Jessica: Archie (4 yr, 30 lbs) "
       "09/01/2026 to 09/06/2026. Book @ r.rover.com/x ]")


POST_CONF_TEMPLATE = (
    "Thanks for confirming!\n\n"
    "To schedule drop off & pickup, please use the following links:\n\n"
    "Drop-off\n\n{dropoff_link}\n\n"
    "Pick-up\n\n{pickup_link}\n\n"
    "Packing list:\n\n- Food\n- leash\n\nMy address:\n\n123 Example St.\n"
)


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "rover@group.calendar.google.com")
    tmpl = tmp_path / "post_confirmation.md"
    tmpl.write_text(POST_CONF_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(config, "POST_CONFIRMATION_PATH", str(tmpl))
    return store.init_db(str(tmp_path / "c2.db"))


# --- link building ---
def test_dropoff_link_is_date_locked(monkeypatch):
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    url = build_link(DROPOFF, "2026-09-01", owner_name="Jessica", ref=7)
    assert url.startswith("https://cal.com/malik/dropoff?")
    assert "date=2026-09-01" in url
    assert "month=2026-09" in url
    assert "name=Jessica" in url
    assert "ref" in url and "7" in url


def test_meetgreet_link_has_no_date(monkeypatch):
    """M&G is an open range (next 7 days), so it must not pin a day."""
    monkeypatch.setattr(config, "CALCOM_USERNAME", "malik")
    url = build_link(MEET_GREET, "2026-09-01", owner_name="Jessica")
    assert "date=" not in url
    assert "/meet-greet?" in url


def test_no_username_returns_none(monkeypatch):
    monkeypatch.setattr(config, "CALCOM_USERNAME", "")
    assert build_link(DROPOFF, "2026-09-01") is None


# --- persistence ---
def test_links_persisted_and_stable(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    handle_sms(conn, A, INQ)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    first = ensure_links(conn, A, 1, owner_name="Jessica")
    assert set(first) == {DROPOFF, PICKUP}
    stored = store.get_scheduling_event(conn, A, 1, DROPOFF)[5]
    assert stored == first[DROPOFF]
    assert ensure_links(conn, A, 1)[DROPOFF] == first[DROPOFF]   # unchanged on re-run


def test_link_refs_point_at_the_right_events(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    links = ensure_links(conn, A, 1)
    d_id = store.get_scheduling_event(conn, A, 1, DROPOFF)[0]
    p_id = store.get_scheduling_event(conn, A, 1, PICKUP)[0]
    assert str(d_id) in links[DROPOFF]
    assert str(p_id) in links[PICKUP]
    assert links[DROPOFF] != links[PICKUP]


# --- message ---
def test_scheduling_message_contains_both_links(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    handle_sms(conn, A, INQ)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    text, links = build_scheduling_draft(conn, A)
    # Links are now folded into the full post-confirmation message.
    assert links[DROPOFF] in text and links[PICKUP] in text
    assert "Packing list" in text and "My address" in text


def test_send_scheduling_links_arms_approve_and_send(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda text, **k: sent.append(text) or 42)
    handle_sms(conn, A, INQ)
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    assert send_scheduling_links(conn, A) is True
    assert sent and "cal.com/malik/dropoff" in sent[0]
    # armed for the existing approve-and-send flow
    pending = store.get_pending_text(conn, A)
    assert "cal.com/malik/pickup" in pending
    assert store.thread_for_card(conn, 42) == A        # card linked for editing


def test_no_links_when_calcom_unconfigured(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "CALCOM_USERNAME", "")
    on_booking_confirmed(conn, A, "Archie", "09/01", "09/06",
                         calendar=FakeCalendar(), today=date(2026, 8, 20))
    text, links = build_scheduling_draft(conn, A)
    assert text is None and links == {}


# --- meet & greet ---
def test_meetgreet_link_created_once(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    handle_sms(conn, A, INQ)
    url1 = ensure_meetgreet_link(conn, A, owner_name="Jessica")
    url2 = ensure_meetgreet_link(conn, A, owner_name="Jessica")
    assert url1 == url2
    assert store.get_scheduling_event(conn, A, 1, MEET_GREET)[5] == url1
