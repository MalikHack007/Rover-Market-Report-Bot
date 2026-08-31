"""SQLite schema + CRUD for the photo-update feature.

Reuses the shared connection and re-entrant lock from `autoresponder.store` (opened with
check_same_thread=False), so photo state is thread-safe with the rest of the service. The main
`store.init_db` calls `init_schema(conn)` once at startup.
"""
from .. import store as base

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_updates (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id         TEXT,
  thread_key       TEXT,                    -- owner's relay number (target)
  episode          INTEGER DEFAULT 1,
  pet_name         TEXT,
  caption          TEXT,                    -- picked from the pool; editable
  caption_index    INTEGER,                 -- pool index, for anti-repeat
  status           TEXT DEFAULT 'collecting',
                   -- collecting | ready | held | sending | sent | delivered | failed | discarded
  telerivet_msg_id TEXT,
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now')),
  UNIQUE (thread_key, episode, batch_id)    -- one dog-update per dog per batch (idempotency)
);
CREATE TABLE IF NOT EXISTS photo_update_media (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  update_id        INTEGER,                 -- FK -> photo_updates.id
  telegram_file_id TEXT,
  local_path       TEXT,                    -- staged original on the box
  r2_key           TEXT,                    -- object key once uploaded (for teardown)
  position         INTEGER DEFAULT 0,
  UNIQUE (update_id, telegram_file_id)      -- dedupe a redelivered photo
);
"""


def init_schema(conn):
    with base._LOCK:
        conn.executescript(SCHEMA)
        conn.commit()


# --- dog-updates ---------------------------------------------------------
def get_or_create_update(conn, batch_id, thread_key, episode, pet_name):
    """The single dog-update row for this dog in this batch (created 'collecting' if new)."""
    with base._LOCK, conn:
        cur = conn.execute(
            "SELECT id FROM photo_updates WHERE thread_key=? AND episode=? AND batch_id=?",
            (thread_key, episode, batch_id))
        row = cur.fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            "INSERT INTO photo_updates (batch_id, thread_key, episode, pet_name, status) "
            "VALUES (?,?,?,?, 'collecting')", (batch_id, thread_key, episode, pet_name))
        return cur.lastrowid


def set_caption(conn, update_id, caption, caption_index=None):
    with base._LOCK, conn:
        conn.execute(
            "UPDATE photo_updates SET caption=?, caption_index=COALESCE(?, caption_index), "
            "updated_at=datetime('now') WHERE id=?", (caption, caption_index, update_id))


def set_status(conn, update_id, status):
    with base._LOCK, conn:
        conn.execute("UPDATE photo_updates SET status=?, updated_at=datetime('now') WHERE id=?",
                     (status, update_id))


def set_telerivet_id(conn, update_id, msg_id):
    with base._LOCK, conn:
        conn.execute(
            "UPDATE photo_updates SET telerivet_msg_id=?, updated_at=datetime('now') WHERE id=?",
            (msg_id, update_id))


def claim_send(conn, update_id):
    """Idempotently flip 'ready' -> 'sending'. Returns True iff we won the claim (so a
    double-tap / retry can't resend a dog-update that's already gone)."""
    with base._LOCK, conn:
        cur = conn.execute(
            "UPDATE photo_updates SET status='sending', updated_at=datetime('now') "
            "WHERE id=? AND status='ready'", (update_id,))
        return cur.rowcount == 1


def get_update(conn, update_id):
    with base._LOCK:
        return conn.execute(
            "SELECT id, batch_id, thread_key, episode, pet_name, caption, caption_index, "
            "status, telerivet_msg_id FROM photo_updates WHERE id=?", (update_id,)).fetchone()


def list_batch(conn, batch_id, statuses=None):
    """Dog-updates in a batch, optionally filtered to a set of statuses (e.g. ('ready',))."""
    cols = ("SELECT id, batch_id, thread_key, episode, pet_name, caption, caption_index, "
            "status, telerivet_msg_id FROM photo_updates WHERE batch_id=?")
    with base._LOCK:
        if statuses:
            q = cols + " AND status IN (%s) ORDER BY id" % ",".join("?" * len(statuses))
            return conn.execute(q, (batch_id, *statuses)).fetchall()
        return conn.execute(cols + " ORDER BY id", (batch_id,)).fetchall()


def list_pending_sent(conn):
    """Sent-but-not-terminal updates (for the batched delivery-status poller):
    (id, telerivet_msg_id, updated_at)."""
    with base._LOCK:
        return conn.execute(
            "SELECT id, telerivet_msg_id, updated_at FROM photo_updates "
            "WHERE status='sent' AND telerivet_msg_id IS NOT NULL").fetchall()


# --- media ---------------------------------------------------------------
def add_media(conn, update_id, telegram_file_id, local_path):
    """Append a photo to a dog-update (deduped on telegram_file_id). Returns the media id."""
    with base._LOCK, conn:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM photo_update_media WHERE update_id=?",
            (update_id,)).fetchone()[0]
        cur = conn.execute(
            "INSERT OR IGNORE INTO photo_update_media "
            "(update_id, telegram_file_id, local_path, position) VALUES (?,?,?,?)",
            (update_id, telegram_file_id, local_path, pos))
        return cur.lastrowid


def get_media(conn, update_id):
    with base._LOCK:
        return conn.execute(
            "SELECT id, telegram_file_id, local_path, r2_key, position "
            "FROM photo_update_media WHERE update_id=? ORDER BY position", (update_id,)).fetchall()


def set_media_r2_key(conn, media_id, r2_key):
    with base._LOCK, conn:
        conn.execute("UPDATE photo_update_media SET r2_key=? WHERE id=?", (r2_key, media_id))


# --- roster: dogs in custody today (Rover bookings only) ------------------
def _parse_date(token):
    from datetime import datetime
    token = (token or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d"):
        try:
            d = datetime.strptime(token, fmt).date()
            if fmt == "%m/%d":                      # year-less → assume this year
                from datetime import date as _date
                d = d.replace(year=_date.today().year)
            return d
        except ValueError:
            continue
    return None


def _parse_stay(stay_dates):
    """'2026-08-31 to 2026-09-06' / '08/31/2026 to 09/06/2026' / single date -> (start, end)."""
    if not stay_dates:
        return None, None
    parts = [p.strip() for p in str(stay_dates).split(" to ")]
    start = _parse_date(parts[0])
    end = _parse_date(parts[1]) if len(parts) > 1 else start
    return start, (end or start)


def list_active_bookings(conn, today=None):
    """Confirmed Rover bookings whose stay covers today. Each entry:
    {thread_key, owner, pet, episode, stay_dates}.

    The bounds are INCLUSIVE (`start <= today <= end`), which already covers a same-day
    drop-off (start == today) and a same-day pick-up (end == today). No grace beyond that —
    an extra ±1-day window wrongly pulled in stays that start tomorrow or ended yesterday.
    """
    from datetime import date
    today = today or date.today()
    with base._LOCK:
        rows = conn.execute(
            "SELECT thread_key, owner_name, pet_name, stay_dates, episode FROM threads "
            "WHERE has_booked=1 AND status='converted'").fetchall()
    out = []
    for thread_key, owner, pet, stay_dates, episode in rows:
        start, end = _parse_stay(stay_dates)
        if start and end and start <= today <= end:
            out.append({"thread_key": thread_key, "owner": owner, "pet": pet,
                        "episode": episode or 1, "stay_dates": stay_dates})
    out.sort(key=lambda e: (e["pet"] or "", e["owner"] or ""))
    return out


# --- daily budgets (Telerivet caps) --------------------------------------
def _today():
    from datetime import date
    return date.today().isoformat()


def _month():
    return _today()[:7]


def api_calls_today(conn):
    return int(base.get_meta(conn, f"telerivet_api_calls:{_today()}", "0") or 0)


def bump_api_calls(conn, n=1):
    base.set_meta(conn, f"telerivet_api_calls:{_today()}", api_calls_today(conn) + n)


def sends_today(conn):
    return int(base.get_meta(conn, f"mms_sends:{_today()}", "0") or 0)


def bump_sends(conn, n=1):
    base.set_meta(conn, f"mms_sends:{_today()}", sends_today(conn) + n)


# --- caption anti-repeat --------------------------------------------------
def caption_last(conn, thread_key):
    v = base.get_meta(conn, f"caption_last:{thread_key}")
    return int(v) if v not in (None, "") else None


def set_caption_last(conn, thread_key, idx):
    base.set_meta(conn, f"caption_last:{thread_key}", idx)


# --- session state (per operator chat) -----------------------------------
def start_batch(conn, chat):
    import uuid
    batch_id = uuid.uuid4().hex[:12]
    base.set_meta(conn, f"photo_batch:{chat}", batch_id)
    base.set_meta(conn, f"photo_active:{chat}", "")   # no active dog yet
    return batch_id


def current_batch(conn, chat):
    return base.get_meta(conn, f"photo_batch:{chat}") or None


def set_active_dog(conn, chat, thread_key):
    base.set_meta(conn, f"photo_active:{chat}", thread_key or "")


def active_dog(conn, chat):
    return base.get_meta(conn, f"photo_active:{chat}") or None


# --- card <-> update map (so a reply to a card edits that caption) --------
def link_card(conn, message_id, update_id):
    base.set_meta(conn, f"photo_card:{message_id}", update_id)


def update_for_card(conn, message_id):
    v = base.get_meta(conn, f"photo_card:{message_id}")
    return int(v) if v not in (None, "") else None
