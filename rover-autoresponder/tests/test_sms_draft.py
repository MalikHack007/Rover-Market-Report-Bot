"""S3: the drafter wired into the SMS path (client-only history, gating, Telegram)."""
from autoresponder import store, config
from autoresponder import telegram_notify as tg
from autoresponder.drafter import Draft
from autoresponder.sms_pipeline import handle_sms, draft_for_thread

A = "+15125550001"
ANIKA_INQ = ("[ New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs) "
             "08/21/2026 to 08/23/2026. Book @ r.rover.com/8C48qS ]")
BLOCK = "Boarding Request - One Time: Drop-off: Fri, Aug 21 at 1:00 PM"
CONF = ("[ Anika has confirmed a booking request (boarding) with Teddy "
        "from 08/21 to 08/23 - View on Rover ]")


def _db(tmp_path):
    return store.init_db(str(tmp_path / "s3.db"))


def _mock(monkeypatch, draft=None, captured=None):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda text, **k: sent.append(text) or 1)
    def fake_draft(owner, pet, dates, stage, history, **kw):
        if captured is not None:
            captured["history"] = history
            captured["owner"] = owner
            captured["pet"] = pet
        return draft or Draft(stage="S0_INITIAL", draft_text="Hey Anika, Teddy looks adorable!",
                              off_playbook=False, flags=[])
    monkeypatch.setattr("autoresponder.drafter.draft_reply", fake_draft)
    return sent


def test_draft_sees_only_client_messages_not_markers(tmp_path, monkeypatch):
    """Rover's marker boilerplate must not pollute the drafter's view."""
    conn = _db(tmp_path)
    captured = {}
    _mock(monkeypatch, captured=captured)
    handle_sms(conn, A, BLOCK)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "Will you be available to sit Teddy on Aug 21 - 23?")
    draft_for_thread(conn, A)
    hist = captured["history"]                       # now ("Client"|"You", text) tuples
    texts = [t for _, t in hist]
    assert any("Will you be available" in t for t in texts)
    assert any("Boarding Request" in t for t in texts)      # block IS useful context
    assert not any("New booking request" in t for t in texts)  # marker excluded
    assert captured["owner"] == "Anika" and captured["pet"] == "Teddy"


def test_draft_sends_telegram_card(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = _mock(monkeypatch)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "Will you be available?")
    draft_for_thread(conn, A)
    assert sent and "Anika" in sent[0]
    assert "Hey Anika, Teddy looks adorable!" in sent[0]
    # persisted for S4's approve-and-send
    last = conn.execute("SELECT last_draft_text FROM threads WHERE thread_key=?", (A,)).fetchone()[0]
    assert last == "Hey Anika, Teddy looks adorable!"


def test_no_draft_on_converted_thread(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = _mock(monkeypatch)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "hi")
    handle_sms(conn, A, CONF)                 # booking confirmed
    draft_for_thread(conn, A)
    assert sent == []                          # nothing drafted or sent


def test_no_draft_without_api_key(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    _mock(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "hi")
    draft_for_thread(conn, A)                  # should no-op, not raise


def test_offplaybook_still_drafts_with_buttons(tmp_path, monkeypatch):
    """Off-playbook = flagged for review, NOT a dead end: draft + approve/edit buttons."""
    conn = _db(tmp_path)
    sent = _mock(monkeypatch, draft=Draft(
        stage="S3_POST_SCREEN", off_playbook=True, flags=["asks about cats"],
        draft_text="Great question — let me confirm and get right back to you!"))
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "do you watch kittens too?")
    draft_for_thread(conn, A)
    assert sent and "Needs your review" in sent[0]      # flagged...
    assert "let me confirm" in sent[0]                   # ...but the draft is there
    assert "cats" in sent[0]
    # and it's armed for Approve & Send / editing
    assert store.get_pending_text(conn, A) == "Great question — let me confirm and get right back to you!"


def test_empty_draft_falls_back_to_attention_card(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = _mock(monkeypatch, draft=Draft(stage="S3_POST_SCREEN", draft_text="",
                                          off_playbook=True, flags=["weird"]))
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "???")
    draft_for_thread(conn, A)
    assert sent and "Needs your attention" in sent[0]


def test_burst_coalesces_to_one_draft(tmp_path, monkeypatch):
    """The debounce contract: many messages -> one draft with full context."""
    from autoresponder.debounce import Debouncer
    conn = _db(tmp_path)
    captured = {}
    sent = _mock(monkeypatch, captured=captured)
    d = Debouncer(60, on_fire=lambda n: draft_for_thread(conn, n))
    for body in [BLOCK, ANIKA_INQ, "Will you be available to sit Teddy?",
                 "Oh also I forgot to add my kitten:("]:
        handle_sms(conn, A, body, schedule_draft=d.bump)
    assert d.pending_count() == 1
    d.flush()
    assert len(sent) == 1                                   # ONE card, not four
    assert any("kitten" in t for _, t in captured["history"])  # afterthought included


# --- two-sided conversation (SMS mirrors both halves) ---
def test_drafter_sees_both_sides_labelled(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    captured = {}
    _mock(monkeypatch, captured=captured)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "Will you be available?")
    store.record_outbound(conn, A, "Hey Anika! Do you mind answering a few questions?")
    handle_sms(conn, A, "sure!")
    draft_for_thread(conn, A)
    hist = captured["history"]
    speakers = [s for s, _ in hist]
    assert "You" in speakers and "Client" in speakers          # both sides present
    assert hist[-1] == ("Client", "sure!")                      # ordering preserved
    assert any(s == "You" and "answering a few questions" in t for s, t in hist)


def test_no_draft_when_only_our_own_messages(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    sent = _mock(monkeypatch)
    handle_sms(conn, A, ANIKA_INQ)
    store.record_outbound(conn, A, "Hi there!")
    draft_for_thread(conn, A)
    assert sent == []          # nothing from the client yet -> don't draft


def test_conversation_excludes_markers(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "hello")
    store.record_outbound(conn, A, "hi back")
    convo = store.get_conversation(conn, A)
    assert convo == [("Client", "hello"), ("You", "hi back")]