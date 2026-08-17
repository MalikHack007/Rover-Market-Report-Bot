"""S6: email runs fallback-only (ingest for truncation recovery, no drafting/Telegram)."""
from autoresponder import config, main, store
from autoresponder import telegram_notify as tg
from autoresponder.models import ParsedMessage


def _db(tmp_path):
    return store.init_db(str(tmp_path / "s6.db"))


def test_fallback_is_the_default():
    assert config.email_fallback_only() is True


def test_standalone_mode_opt_out(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_MODE", "standalone")
    assert config.email_fallback_only() is False


def test_draft_thread_is_noop_in_fallback(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    sent, called = [], []
    monkeypatch.setattr(tg, "send_message", lambda text, **k: sent.append(text) or 1)
    monkeypatch.setattr("autoresponder.drafter.draft_reply",
                        lambda *a, **k: called.append(1))
    store.record_message(conn, ParsedMessage("m1", "t1", "Anika", "Teddy",
                                             None, None, "hi", kind="inquiry"))
    main.draft_thread(conn, "t1")
    assert called == []          # no LLM call
    assert sent == []            # no Telegram card


def test_fallback_does_not_touch_thread_status(tmp_path, monkeypatch):
    """SMS owns the lifecycle; the email feed must not flip threads to converted."""
    conn = _db(tmp_path)
    pm = ParsedMessage("m2", "t2", "Minyoung", "Captain", None, None,
                       "drop off time?", kind="confirmed")
    store.record_message(conn, pm)
    store.upsert_sms_thread(conn, "t2", status="active")
    main.dispatch(conn, pm, lambda tk: None)
    assert store.get_thread(conn, "t2")[4] == "active"    # unchanged


def test_fallback_still_stores_messages_for_recovery(tmp_path):
    """The whole point: full email text must land in the DB for S5 to find."""
    conn = _db(tmp_path)
    full = "1- you're the only person I've contacted. He also has medication daily."
    store.record_message(conn, ParsedMessage("m3", "gthread-9", "Brenna", "Alfie",
                                             None, None, full, kind="inquiry"))
    from autoresponder import truncation
    assert truncation.find_email_thread(conn, "Brenna", "Alfie") == "gthread-9"
    assert full in store.get_thread_messages(conn, "gthread-9")
