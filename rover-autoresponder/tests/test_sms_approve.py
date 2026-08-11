"""S4: approve-and-send — the approval gate, idempotency, editing, delivery."""
from autoresponder import store, telegram_poll
from autoresponder import telegram_notify as tg
from autoresponder import sms_approve
from autoresponder.drafter import Draft
from autoresponder.sms_pipeline import handle_sms

A = "+15125550001"
INQ = ("[ New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs) "
       "08/21/2026 to 08/23/2026. Book @ r.rover.com/x ]")


class FakeGateway:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []
    def send(self, number, text, message_id=None):
        self.sent.append((number, text, message_id))
        return "gw-1" if self.ok else None


def _setup(tmp_path, monkeypatch, pending="Hey Anika, Teddy looks adorable!"):
    conn = store.init_db(str(tmp_path / "s4.db"))
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, "Will you be available?")
    store.set_pending_text(conn, A, pending)
    calls = {"answer": [], "alert": [], "edit_markup": [], "edit_text": [], "sent": []}
    monkeypatch.setattr(tg, "answer_callback", lambda q, t="": calls["answer"].append(t) or True)
    monkeypatch.setattr(tg, "send_alert", lambda t: calls["alert"].append(t) or True)
    monkeypatch.setattr(tg, "edit_reply_markup", lambda c, m, reply_markup=None: calls["edit_markup"].append(m) or True)
    monkeypatch.setattr(tg, "edit_message_text", lambda c, m, text, reply_markup=None, **k: calls["edit_text"].append(text) or True)
    monkeypatch.setattr(tg, "send_message", lambda text, **k: calls["sent"].append(text) or 999)
    return conn, calls


# --- the core gate ---
def test_approve_sends_and_records(tmp_path, monkeypatch):
    conn, calls = _setup(tmp_path, monkeypatch)
    gw = FakeGateway()
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=gw) is True
    assert gw.sent[0][0] == A
    assert "Teddy looks adorable" in gw.sent[0][1]
    # logged as OUR turn so the drafter sees both sides
    convo = store.get_conversation(conn, A)
    assert ("You", "Hey Anika, Teddy looks adorable!") in convo
    assert store.get_pending_text(conn, A) is None      # consumed
    assert any("Sent" in a for a in calls["answer"])


def test_double_tap_does_not_double_send(tmp_path, monkeypatch):
    """The idempotency guard: same (thread, text) can only transmit once."""
    conn, calls = _setup(tmp_path, monkeypatch)
    gw = FakeGateway()
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=gw) is True
    store.set_pending_text(conn, A, "Hey Anika, Teddy looks adorable!")   # re-armed
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=gw) is False
    assert len(gw.sent) == 1                                    # ONE transmission
    assert any("duplicate" in a.lower() for a in calls["answer"])


def test_send_failure_alerts_and_allows_retry(tmp_path, monkeypatch):
    conn, calls = _setup(tmp_path, monkeypatch)
    bad = FakeGateway(ok=False)
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=bad) is False
    assert any("FAILED" in a for a in calls["alert"])
    # claim released -> the same text can be retried successfully
    good = FakeGateway()
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=good) is True


def test_nothing_sends_without_pending_text(tmp_path, monkeypatch):
    conn, calls = _setup(tmp_path, monkeypatch, pending=None)
    store.set_pending_text(conn, A, None)
    gw = FakeGateway()
    assert sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=gw) is False
    assert gw.sent == []


def test_approve_advances_stage(tmp_path, monkeypatch):
    conn, _ = _setup(tmp_path, monkeypatch)
    assert store.get_thread(conn, A)[3] == "S0_INITIAL"
    sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=FakeGateway())
    assert store.get_thread(conn, A)[3] == "S1_CONSENT"


# --- editing ---
def test_reply_to_card_edits_pending_text(tmp_path, monkeypatch):
    conn, calls = _setup(tmp_path, monkeypatch)
    store.link_card(conn, 555, A)
    handled = sms_approve.handle_text_reply(conn, "My own wording here", chat_id=1,
                                            reply_to_message_id=555)
    assert handled is True
    assert store.get_pending_text(conn, A) == "My own wording here"
    assert any("My own wording here" in t for t in calls["edit_text"])


def test_edited_text_is_what_gets_sent(tmp_path, monkeypatch):
    conn, _ = _setup(tmp_path, monkeypatch)
    store.link_card(conn, 555, A)
    sms_approve.handle_text_reply(conn, "Edited version!", 1, 555)
    gw = FakeGateway()
    sms_approve.approve_and_send(conn, A, 1, 555, "q", gateway=gw)
    assert gw.sent[0][1] == "Edited version!"       # YOUR text, not the draft


def test_reply_to_unknown_message_ignored(tmp_path, monkeypatch):
    conn, _ = _setup(tmp_path, monkeypatch)
    assert sms_approve.handle_text_reply(conn, "stray text", 1, 4242) is False


# --- terminal buttons ---
def test_converted_and_unfit_stop_drafting(tmp_path, monkeypatch):
    conn, _ = _setup(tmp_path, monkeypatch)
    sms_approve.handle_callback(conn, f"conv:{A}", 1, 2, "q")
    assert store.get_thread(conn, A)[4] == "converted"
    sms_approve.handle_callback(conn, f"unfit:{A}", 1, 2, "q")
    assert store.get_thread(conn, A)[4] == "not_suitable"


# --- delivery confirmation ---
def test_delivery_failure_alerts(tmp_path, monkeypatch):
    conn, calls = _setup(tmp_path, monkeypatch)
    sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=FakeGateway())
    sms_approve.handle_delivery_event(conn, "sms:failed",
                                      {"messageId": "gw-1", "reason": "no service"})
    assert any("FAILED" in a for a in calls["alert"])
    assert store.get_thread(conn, A)[4] != "delivered"


def test_delivery_success_marks_delivered(tmp_path, monkeypatch):
    conn, _ = _setup(tmp_path, monkeypatch)
    sms_approve.approve_and_send(conn, A, 1, 2, "q", gateway=FakeGateway())
    sms_approve.handle_delivery_event(conn, "sms:delivered", {"messageId": "gw-1"})
    assert store.send_by_gateway_id(conn, "gw-1")[2] == "delivered"


# --- poller routes text replies ---
def test_poller_dispatches_text_reply():
    seen = []
    upd = {"message": {"text": "my edit", "chat": {"id": 111},
                       "reply_to_message": {"message_id": 555}}}
    telegram_poll.dispatch_update(upd, lambda *a: None, allowed_chat=111,
                                  on_text=lambda *a: seen.append(a))
    assert seen == [("my edit", 111, 555)]


def test_poller_ignores_text_from_other_chats():
    seen = []
    upd = {"message": {"text": "hi", "chat": {"id": 999}}}
    telegram_poll.dispatch_update(upd, lambda *a: None, allowed_chat=111,
                                  on_text=lambda *a: seen.append(a))
    assert seen == []
