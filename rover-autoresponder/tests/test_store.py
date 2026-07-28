from autoresponder import store
from autoresponder.models import ParsedMessage


def _pm(msg_id="m1", thread="t1", text="hello"):
    return ParsedMessage(
        gmail_msg_id=msg_id, thread_key=thread, owner_name="Vatsal",
        pet_name="Gypsy", stay_start="08/26/2025", stay_end="08/28/2025",
        message_text=text, raw_subject="New message from Vatsal about Gypsy's stay",
        recognized=True,
    )


def test_record_and_dedupe(tmp_path):
    conn = store.init_db(str(tmp_path / "t.db"))
    assert store.record_message(conn, _pm("m1")) is True
    assert store.already_seen(conn, "m1") is True
    # same message id again -> not recorded twice
    assert store.record_message(conn, _pm("m1", text="dup")) is False
    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert rows == 1


def test_thread_upsert_and_multiple_messages(tmp_path):
    conn = store.init_db(str(tmp_path / "t.db"))
    store.record_message(conn, _pm("m1", "t1", "first"))
    store.record_message(conn, _pm("m2", "t1", "second"))
    threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    last = conn.execute("SELECT last_msg_text FROM threads WHERE thread_key='t1'").fetchone()[0]
    assert threads == 1
    assert msgs == 2
    assert last == "second"


def test_meta_roundtrip(tmp_path):
    conn = store.init_db(str(tmp_path / "t.db"))
    assert store.get_meta(conn, "last_history_id") is None
    store.set_meta(conn, "last_history_id", "12345")
    assert store.get_meta(conn, "last_history_id") == "12345"
    store.set_meta(conn, "last_history_id", "67890")
    assert store.get_meta(conn, "last_history_id") == "67890"
