"""Phase 4: keyboard, callback dispatch guard, stage advance, and handle_callback."""
from autoresponder import telegram_notify as tg
from autoresponder import telegram_poll
from autoresponder import main
from autoresponder import store


# --- keyboard ---
def test_keyboard_encodes_action_and_thread():
    kb = tg.build_keyboard("19faXYZ")
    rows = kb["inline_keyboard"]
    datas = [b["callback_data"] for row in rows for b in row]
    assert "sent:19faXYZ" in datas
    assert "conv:19faXYZ" in datas
    assert "unfit:19faXYZ" in datas
    assert "warm:19faXYZ" in datas
    assert all(len(d) <= 64 for d in datas)   # Telegram callback_data limit


# --- stage advance ---
def test_advance_stage_walks_forward_and_caps():
    assert main.advance_stage("S0_INITIAL") == "S1_CONSENT"
    assert main.advance_stage("S1_CONSENT") == "S2_ANSWERS"
    assert main.advance_stage("S2_ANSWERS") == "S3_POST_SCREEN"
    assert main.advance_stage("S3_POST_SCREEN") == "S3_POST_SCREEN"  # caps at last
    assert main.advance_stage("garbage") == "S0_INITIAL"


# --- poller only honors callbacks from the allowed chat ---
def test_dispatch_update_ignores_other_chats():
    seen = []
    upd = {"callback_query": {"data": "sent:t1", "id": "q", "message":
           {"message_id": 5, "chat": {"id": 999}}}}
    telegram_poll.dispatch_update(upd, lambda *a: seen.append(a), allowed_chat=111)
    assert seen == []                                   # wrong chat -> ignored

def test_dispatch_update_passes_allowed_chat():
    seen = []
    upd = {"callback_query": {"data": "sent:t1", "id": "q", "message":
           {"message_id": 5, "chat": {"id": 111}}}}
    telegram_poll.dispatch_update(upd, lambda *a: seen.append(a), allowed_chat=111)
    assert seen == [("sent:t1", 111, 5, "q")]


# --- handle_callback transitions (telegram + drafter mocked) ---
def _mock_tg(monkeypatch):
    calls = {"answer": [], "edit_markup": [], "edit_text": []}
    monkeypatch.setattr(tg, "answer_callback", lambda q, t="": calls["answer"].append(t) or True)
    monkeypatch.setattr(tg, "edit_reply_markup", lambda c, m, reply_markup=None: calls["edit_markup"].append((c, m)) or True)
    monkeypatch.setattr(tg, "edit_message_text", lambda c, m, text, reply_markup=None, **k: calls["edit_text"].append(text) or True)
    return calls

def test_mark_sent_advances_stage(tmp_path, monkeypatch):
    conn = store.init_db(str(tmp_path / "t.db"))
    from autoresponder.models import ParsedMessage
    store.record_message(conn, ParsedMessage("m1", "t1", "Marie", None, "07/30/2026", None, "hi", kind="inquiry"))
    _mock_tg(monkeypatch)
    main.handle_callback(conn, "sent:t1", chat_id=111, message_id=5, cq_id="q")
    assert store.get_thread(conn, "t1")[3] == "S1_CONSENT"   # stage advanced

def test_converted_sets_terminal_status(tmp_path, monkeypatch):
    conn = store.init_db(str(tmp_path / "t.db"))
    from autoresponder.models import ParsedMessage
    store.record_message(conn, ParsedMessage("m1", "t1", "Marie", None, None, None, "hi", kind="inquiry"))
    _mock_tg(monkeypatch)
    main.handle_callback(conn, "conv:t1", 111, 5, "q")
    assert store.get_thread(conn, "t1")[4] == "converted"

def test_not_suitable_sets_terminal_status(tmp_path, monkeypatch):
    conn = store.init_db(str(tmp_path / "t.db"))
    from autoresponder.models import ParsedMessage
    store.record_message(conn, ParsedMessage("m1", "t1", "Marie", None, None, None, "hi", kind="inquiry"))
    _mock_tg(monkeypatch)
    main.handle_callback(conn, "unfit:t1", 111, 5, "q")
    assert store.get_thread(conn, "t1")[4] == "not_suitable"

def test_warmer_redrafts_and_edits(tmp_path, monkeypatch):
    conn = store.init_db(str(tmp_path / "t.db"))
    from autoresponder.models import ParsedMessage
    from autoresponder.drafter import Draft
    store.record_message(conn, ParsedMessage("m1", "t1", "Marie", None, None, None, "hi", kind="inquiry"))
    calls = _mock_tg(monkeypatch)
    captured = {}
    def fake_draft(owner, pet, dates, stage, history, **kw):
        captured["extra"] = kw.get("extra_instruction")
        return Draft(stage="S0_INITIAL", draft_text="warmer draft!", off_playbook=False, flags=[])
    monkeypatch.setattr("autoresponder.drafter.draft_reply", fake_draft)
    main.handle_callback(conn, "warm:t1", 111, 5, "q")
    assert "warmer" in (captured["extra"] or "").lower()   # tone nudge passed through
    assert calls["edit_text"] and "warmer draft!" in calls["edit_text"][0]
    last = conn.execute("SELECT last_draft_text FROM threads WHERE thread_key='t1'").fetchone()[0]
    assert last == "warmer draft!"  # last_draft updated
