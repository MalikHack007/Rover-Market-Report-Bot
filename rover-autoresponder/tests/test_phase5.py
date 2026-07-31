"""Phase 5: FAQ wiring, heartbeat stats/summary, and alerting."""
from autoresponder import drafter, store, heartbeat
from autoresponder import telegram_notify as tg
from autoresponder import config
from autoresponder.models import ParsedMessage


# --- FAQ makes it into the system prompt and is marked authoritative ---
def test_faq_included_and_authoritative():
    sp = drafter.build_system_prompt("You are {SITTER_NAME}.",
                                     "**Can we do a meet and greet?**\nBrownie Park link...",
                                     "Onel")
    assert "meet and greet" in sp
    assert "authoritative" in sp.lower()

def test_real_faq_file_loads_by_default():
    # The shipped autoresponder/faq.md should load via the default FAQ_PATH.
    faq = drafter.load_text(config.FAQ_PATH)
    assert "Brownie Neighborhood Park" in faq
    sp = drafter.build_system_prompt(drafter.load_text(config.PLAYBOOK_PATH), faq, "Onel")
    assert "neutered" in sp.lower()          # a real FAQ answer is present
    assert "video call" in sp.lower()


# --- heartbeat stats ---
def test_stats_counts_by_status(tmp_path):
    conn = store.init_db(str(tmp_path / "t.db"))
    store.record_message(conn, ParsedMessage("m1", "t1", "A", None, None, None, "hi", kind="inquiry"))
    store.record_message(conn, ParsedMessage("m2", "t2", "B", None, None, None, "hi", kind="inquiry"))
    store.set_thread_status(conn, "t2", "converted")
    s = store.stats(conn)
    assert s["messages_24h"] == 2
    assert s["active"] == 1
    assert s["converted"] == 1
    assert s["not_suitable"] == 0

def test_heartbeat_summary_mentions_counts(tmp_path):
    conn = store.init_db(str(tmp_path / "t.db"))
    text = heartbeat.summary(conn)
    assert "heartbeat" in text.lower()
    assert "messages (24h)" in text


# --- alert routes through send_message with the warning marker ---
def test_send_alert_prefixes_warning(monkeypatch):
    sent = {}
    monkeypatch.setattr(tg, "send_message", lambda text, **k: sent.setdefault("text", text) or 1)
    assert tg.send_alert("watch renewal failed") is True
    assert "alert" in sent["text"].lower()
    assert "watch renewal failed" in sent["text"]
