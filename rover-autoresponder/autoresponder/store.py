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
-- Addendum A / S4: idempotency ledger. One row per (thread, exact text) send claim,
-- so a double-tap or retry can never transmit the same message twice.
CREATE TABLE IF NOT EXISTS sends (
  send_key       TEXT PRIMARY KEY,
  thread_key     TEXT,
  text           TEXT,
  gateway_msg_id TEXT,
  status         TEXT DEFAULT 'claimed',   -- claimed|sent|delivered|failed
  created_at     TEXT DEFAULT (datetime('now')),
  updated_at     TEXT DEFAULT (datetime('now'))
);
-- S4: maps a Telegram card to its thread, so replying to a card edits that draft.
CREATE TABLE IF NOT EXISTS cards (
  message_id  INTEGER PRIMARY KEY,
  thread_key  TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);
"""

# S4: columns added to `threads` on existing databases.
_MIGRATIONS = [
    "ALTER TABLE threads ADD COLUMN pending_text TEXT",
    "ALTER TABLE threads ADD COLUMN send_status TEXT",
    "ALTER TABLE threads ADD COLUMN sent_at TEXT",
]


def init_db(path: str) -> sqlite3.Connection:
    # check_same_thread=False: the connection is shared across the subscriber's
    # worker threads and the watch-renewal thread. All access goes through _LOCK.
    conn = sqlite3.connect(path, check_same_thread=False)
    with _LOCK:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:          # additive; ignore "duplicate column"
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
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


# --- Phase 2: thread state for the drafter ---
def get_thread(conn: sqlite3.Connection, thread_key: str):
    """Return (owner_name, pet_name, stay_dates, stage, status) or None."""
    with _LOCK:
        cur = conn.execute(
            "SELECT owner_name, pet_name, stay_dates, stage, status "
            "FROM threads WHERE thread_key=?",
            (thread_key,),
        )
        return cur.fetchone()


def get_thread_messages(conn: sqlite3.Connection, thread_key: str):
    """All non-empty inbound message texts for a thread, oldest first."""
    with _LOCK:
        cur = conn.execute(
            "SELECT text FROM messages WHERE thread_key=? ORDER BY id",
            (thread_key,),
        )
        return [r[0] for r in cur.fetchall() if r[0]]


def update_thread_stage(conn: sqlite3.Connection, thread_key: str, stage: str) -> None:
    with _LOCK, conn:
        conn.execute(
            "UPDATE threads SET stage=?, updated_at=datetime('now') WHERE thread_key=?",
            (stage, thread_key),
        )


def set_last_draft(conn: sqlite3.Connection, thread_key: str, draft_text: str) -> None:
    with _LOCK, conn:
        conn.execute(
            "UPDATE threads SET last_draft_text=?, updated_at=datetime('now') "
            "WHERE thread_key=?",
            (draft_text, thread_key),
        )


def set_thread_status(conn: sqlite3.Connection, thread_key: str, status: str) -> None:
    """Terminal states 'converted'/'not_suitable' stop future drafting (Phase 4 sets these)."""
    with _LOCK, conn:
        conn.execute(
            "UPDATE threads SET status=?, updated_at=datetime('now') WHERE thread_key=?",
            (status, thread_key),
        )


# --- Phase 3 fix: tombstone an unfetchable message id so duplicate Gmail pushes
#     (at-least-once delivery) don't keep re-fetching a message that 404s. ---
def mark_seen(conn: sqlite3.Connection, gmail_msg_id: str) -> None:
    with _LOCK, conn:
        conn.execute(
            "INSERT OR IGNORE INTO messages (thread_key, gmail_msg_id, text) "
            "VALUES (?, ?, ?)",
            (None, gmail_msg_id, None),
        )


# --- Phase 5: heartbeat stats ---
def stats(conn: sqlite3.Connection) -> dict:
    with _LOCK:
        def one(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "messages_24h": one("SELECT COUNT(*) FROM messages "
                                "WHERE received_at > datetime('now','-1 day')"),
            "active": one("SELECT COUNT(*) FROM threads WHERE status='active'"),
            "converted": one("SELECT COUNT(*) FROM threads WHERE status='converted'"),
            "not_suitable": one("SELECT COUNT(*) FROM threads WHERE status='not_suitable'"),
        }


# --- Addendum A / S2: SMS threads (keyed by conversation number) ---
def upsert_sms_thread(conn: sqlite3.Connection, number: str, owner_name=None,
                      pet_name=None, stay_dates=None, status=None) -> None:
    """Create or update an SMS thread. Only overwrites fields that are provided."""
    with _LOCK, conn:
        conn.execute(
            "INSERT OR IGNORE INTO threads (thread_key, status) VALUES (?, ?)",
            (number, status or "active"),
        )
        sets, vals = [], []
        for col, val in (("owner_name", owner_name), ("pet_name", pet_name),
                         ("stay_dates", stay_dates), ("status", status)):
            if val:
                sets.append(f"{col}=?")
                vals.append(val)
        if sets:
            vals.append(number)
            conn.execute(
                f"UPDATE threads SET {', '.join(sets)}, updated_at=datetime('now') "
                "WHERE thread_key=?", vals)


def record_sms(conn: sqlite3.Connection, number: str, msg) -> None:
    """Append an inbound SMS to the thread's message log."""
    with _LOCK, conn:
        conn.execute(
            "INSERT INTO messages (thread_key, gmail_msg_id, direction, text, "
            "recognized, raw_subject) VALUES (?,?,?,?,?,?)",
            (number, None, "inbound", msg.text, 1 if msg.kind != "message" else 0,
             msg.kind),
        )
        conn.execute(
            "UPDATE threads SET last_msg_text=?, updated_at=datetime('now') "
            "WHERE thread_key=?", (msg.text, number))


def sms_event_seen(conn: sqlite3.Connection, event_id: str) -> bool:
    """Persistent webhook dedupe (the gateway retries until it gets a 2xx)."""
    if not event_id:
        return False
    with _LOCK:
        cur = conn.execute("SELECT 1 FROM meta WHERE key=?", (f"sms_evt:{event_id}",))
        if cur.fetchone():
            return True
    set_meta(conn, f"sms_evt:{event_id}", "1")
    return False


def get_client_messages(conn: sqlite3.Connection, thread_key: str):
    """Addendum A / S3: only the CLIENT's own messages, oldest first.

    Excludes Rover's machine-generated marker texts (inquiry/confirmed/modified),
    which are recorded with their kind in raw_subject. The drafter should read what
    the client actually wrote, not Rover's boilerplate.
    """
    with _LOCK:
        cur = conn.execute(
            "SELECT text FROM messages WHERE thread_key=? AND direction='inbound' "
            "AND (raw_subject IS NULL OR raw_subject='message') ORDER BY id",
            (thread_key,),
        )
        return [r[0] for r in cur.fetchall() if r[0]]


def record_outbound(conn: sqlite3.Connection, number: str, text: str,
                    gateway_msg_id: str = None) -> None:
    """Addendum A: log a reply WE sent, so the drafter sees both sides of the thread."""
    with _LOCK, conn:
        conn.execute(
            "INSERT INTO messages (thread_key, gmail_msg_id, direction, text, "
            "recognized, raw_subject) VALUES (?,?,?,?,?,?)",
            (number, gateway_msg_id, "outbound", text, 1, "message"),
        )
        conn.execute(
            "UPDATE threads SET updated_at=datetime('now') WHERE thread_key=?", (number,))


def get_conversation(conn: sqlite3.Connection, thread_key: str):
    """Full two-sided conversation, oldest first, as ('Client'|'You', text) tuples.

    SMS mirrors the whole thread, so unlike the email path the drafter can see its
    own prior replies — which makes stage inference far more reliable. Rover's
    machine-generated marker texts stay excluded.
    """
    with _LOCK:
        cur = conn.execute(
            "SELECT direction, text FROM messages WHERE thread_key=? "
            "AND (raw_subject IS NULL OR raw_subject='message') ORDER BY id",
            (thread_key,),
        )
        return [("You" if d == "outbound" else "Client", t)
                for d, t in cur.fetchall() if t]


# --- Addendum A / S4: pending text, card mapping, send idempotency ---
def set_pending_text(conn: sqlite3.Connection, number: str, text: str) -> None:
    """The text that Approve & Send will transmit (a draft, or your edited version)."""
    with _LOCK, conn:
        conn.execute(
            "UPDATE threads SET pending_text=?, updated_at=datetime('now') "
            "WHERE thread_key=?", (text, number))


def get_pending_text(conn: sqlite3.Connection, number: str):
    with _LOCK:
        cur = conn.execute("SELECT pending_text FROM threads WHERE thread_key=?", (number,))
        row = cur.fetchone()
        return row[0] if row else None


def link_card(conn: sqlite3.Connection, message_id: int, number: str) -> None:
    """Remember which thread a Telegram card belongs to (for reply-to-edit)."""
    if not message_id:
        return
    with _LOCK, conn:
        conn.execute(
            "INSERT OR REPLACE INTO cards (message_id, thread_key) VALUES (?,?)",
            (int(message_id), number))


def thread_for_card(conn: sqlite3.Connection, message_id: int):
    with _LOCK:
        cur = conn.execute("SELECT thread_key FROM cards WHERE message_id=?",
                           (int(message_id),))
        row = cur.fetchone()
        return row[0] if row else None


def _send_key(number: str, text: str) -> str:
    import hashlib
    return hashlib.sha1(f"{number}\x00{text}".encode()).hexdigest()


def claim_send(conn: sqlite3.Connection, number: str, text: str):
    """Reserve a send. Returns send_key if THIS caller won the claim, else None.

    The idempotency guard: a double-tap of Approve, or a retry, hits the same
    (thread, text) key and loses the race, so the client never gets two copies.
    """
    key = _send_key(number, text)
    with _LOCK, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sends (send_key, thread_key, text) VALUES (?,?,?)",
            (key, number, text))
        return key if cur.rowcount else None


def update_send(conn: sqlite3.Connection, send_key: str, status: str,
                gateway_msg_id: str = None) -> None:
    with _LOCK, conn:
        if gateway_msg_id:
            conn.execute(
                "UPDATE sends SET status=?, gateway_msg_id=?, updated_at=datetime('now') "
                "WHERE send_key=?", (status, gateway_msg_id, send_key))
        else:
            conn.execute(
                "UPDATE sends SET status=?, updated_at=datetime('now') WHERE send_key=?",
                (status, send_key))


def release_send(conn: sqlite3.Connection, send_key: str) -> None:
    """Drop a claim that never made it out, so you can retry the same text."""
    with _LOCK, conn:
        conn.execute("DELETE FROM sends WHERE send_key=?", (send_key,))


def send_by_gateway_id(conn: sqlite3.Connection, gateway_msg_id: str):
    with _LOCK:
        cur = conn.execute(
            "SELECT send_key, thread_key, status FROM sends WHERE gateway_msg_id=?",
            (gateway_msg_id,))
        return cur.fetchone()


def mark_thread_sent(conn: sqlite3.Connection, number: str, status: str) -> None:
    with _LOCK, conn:
        conn.execute(
            "UPDATE threads SET send_status=?, sent_at=datetime('now'), "
            "pending_text=NULL, updated_at=datetime('now') WHERE thread_key=?",
            (status, number))