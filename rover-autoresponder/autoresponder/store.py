"""SQLite persistence: dedupe, per-thread state, and pipeline checkpoints.

Thread-safety: the Pub/Sub streaming-pull callback and the daily watch-renewal
run in worker threads, not the main thread, so the connection is opened with
check_same_thread=False and every access is serialized behind a re-entrant lock.
Volume is low, so serial DB access costs nothing and avoids SQLite cross-thread
and concurrent-write errors.
"""
import sqlite3
import threading

from .models import ParsedMessage

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_key       TEXT PRIMARY KEY,
  owner_name       TEXT,
  pet_name         TEXT,
  stay_dates       TEXT,
  stage            TEXT DEFAULT 'S0_INITIAL',
  status           TEXT DEFAULT 'active',
  last_msg_text    TEXT,
  last_draft_text  TEXT,
  flags            TEXT,
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_key   TEXT,
  gmail_msg_id TEXT UNIQUE,
  direction    TEXT DEFAULT 'inbound',
  text         TEXT,
  recognized   INTEGER DEFAULT 1,
  raw_subject  TEXT,
  received_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def init_db(path: str) -> sqlite3.Connection:
    # check_same_thread=False: the connection is shared across the subscriber's
    # worker threads and the watch-renewal thread. All access goes through _LOCK.
    conn = sqlite3.connect(path, check_same_thread=False)
    with _LOCK:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


def already_seen(conn: sqlite3.Connection, gmail_msg_id: str) -> bool:
    with _LOCK:
        cur = conn.execute("SELECT 1 FROM messages WHERE gmail_msg_id=?", (gmail_msg_id,))
        return cur.fetchone() is not None


def record_message(conn: sqlite3.Connection, pm: ParsedMessage) -> bool:
    """Insert the message + upsert its thread. Returns False if already seen."""
    with _LOCK:
        if already_seen(conn, pm.gmail_msg_id):
            return False
        dates = f"{pm.stay_start} to {pm.stay_end}" if pm.stay_start else None
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO threads "
                "(thread_key, owner_name, pet_name, stay_dates, last_msg_text) "
                "VALUES (?,?,?,?,?)",
                (pm.thread_key, pm.owner_name, pm.pet_name, dates, pm.message_text),
            )
            conn.execute(
                "UPDATE threads SET last_msg_text=?, updated_at=datetime('now') "
                "WHERE thread_key=?",
                (pm.message_text, pm.thread_key),
            )
            conn.execute(
                "INSERT INTO messages "
                "(thread_key, gmail_msg_id, text, recognized, raw_subject) "
                "VALUES (?,?,?,?,?)",
                (pm.thread_key, pm.gmail_msg_id, pm.message_text,
                 1 if pm.recognized else 0, pm.raw_subject),
            )
        return True


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    with _LOCK:
        cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    with _LOCK:
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )