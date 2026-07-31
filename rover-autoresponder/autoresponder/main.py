"""Entrypoint: watch -> Pub/Sub -> history.list -> parse -> store -> draft -> Telegram.

Phase 2 drafter (stage machine + playbook), Phase 3 Telegram delivery, Phase 4
interactive buttons (Mark sent / Regenerate / Warmer / Shorter / Converted / Not
suitable) via a synchronous getUpdates poll, plus startup reconciliation. Modes:
  python -m autoresponder.main            # live (needs Gmail + Pub/Sub set up)
  python -m autoresponder.main --replay samples/yisell_booking_message.txt  # offline dev

Drafting runs only when ANTHROPIC_API_KEY is set; without it, parse+store still work.
"""
import argparse
import hashlib
import logging
import os

from . import config, store
from .parser import parse_notification

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("autoresponder")


def process_message_id(service, conn, schedule_draft, msg_id: str) -> None:
    from . import gmail_client  # deferred: only the live path needs Google libs

    if store.already_seen(conn, msg_id):
        log.debug("skip already-seen %s", msg_id)
        return
    msg = gmail_client.get_message(service, msg_id)
    # Phase 3 fix: history referenced a message that's gone (404 -> None).
    # Tombstone it so a duplicate push doesn't re-fetch it, then skip.
    if msg is None:
        store.mark_seen(conn, msg_id)
        return
    subject, body_text, thread_id = gmail_client.extract_fields(msg)
    pm = parse_notification(subject, body_text, gmail_msg_id=msg_id, thread_key=thread_id)
    if not store.record_message(conn, pm):
        return
    dispatch(conn, pm, schedule_draft)


def dispatch(conn, pm, schedule_draft) -> None:
    """Route a stored message by subject kind.

    inquiry  -> schedule a (debounced) draft for the thread.
    else     -> confirmed booking / unfamiliar subject: mark converted, no action.
    """
    if pm.kind == "inquiry":
        log.info(
            "NEW INQUIRY | thread=%s owner=%s start=%s | msg=%r",
            pm.thread_key, pm.owner_name, pm.stay_start, pm.message_text,
        )
        schedule_draft(pm.thread_key)
    else:
        store.set_thread_status(conn, pm.thread_key, "converted")
        log.info("  no action (%s) | marked converted | subject=%r",
                 pm.kind, pm.raw_subject)


def draft_thread(conn, thread_key: str) -> None:
    """Draft the next reply for an active inquiry thread and log it (no sending yet).

    Called by the debouncer once a thread has been quiet: it reads the thread's
    FULL accumulated history, so a burst of messages produces a single, informed
    draft that accounts for the stage plus everything the client said.
    """
    if not config.ANTHROPIC_API_KEY:
        log.info("  (draft skipped: ANTHROPIC_API_KEY not set)")
        return
    row = store.get_thread(conn, thread_key)
    if not row:
        return
    owner, pet, dates, stage, status = row

    from .drafter import should_draft, draft_reply
    if not should_draft(status):
        log.info("  (thread %s is %s; not drafting)", thread_key, status)
        return

    history = store.get_thread_messages(conn, thread_key)
    try:
        d = draft_reply(owner, pet, dates, stage, history)
    except Exception:
        log.exception("  draft failed for thread %s", thread_key)
        return

    # Phase 3: also deliver to Telegram (send-only). No-op if TELEGRAM_* unset.
    from . import telegram_notify

    store.update_thread_stage(conn, thread_key, d.stage)
    if d.off_playbook:
        log.warning("  OFF-PLAYBOOK [%s] flags=%s (no draft) — needs your attention",
                    d.stage, d.flags)
        telegram_notify.send_message(
            telegram_notify.format_offplaybook_card(owner, d.flags, history))
        return
    store.set_last_draft(conn, thread_key, d.draft_text)
    flag_note = f" flags={d.flags}" if d.flags else ""
    log.info("  DRAFT [%s]%s (from %d msg) \n----- draft -----\n%s\n-----------------",
             d.stage, flag_note, len(history), d.draft_text)
    # Phase 3: push the draft card to Telegram for tap-to-copy.
    # Phase 4: attach the action buttons (Mark sent / Regenerate / tone / terminal).
    telegram_notify.send_message(
        telegram_notify.format_draft_card(owner, dates, d.stage, d.flags, history, d.draft_text),
        reply_markup=telegram_notify.build_keyboard(thread_key))


# --- Phase 4: button-driven stage/status transitions ---
_STAGE_ORDER = ["S0_INITIAL", "S1_CONSENT", "S2_ANSWERS", "S3_POST_SCREEN"]
_TONE = {
    "regen": None,
    "warm": "Make the reply warmer and friendlier while keeping the same intent.",
    "short": "Make the reply more concise.",
}


def advance_stage(stage: str) -> str:
    """Next stage on 'Mark sent'; S3 is the last and stays put."""
    try:
        i = _STAGE_ORDER.index(stage)
    except ValueError:
        return "S0_INITIAL"
    return _STAGE_ORDER[min(i + 1, len(_STAGE_ORDER) - 1)]


def handle_callback(conn, data, chat_id, message_id, cq_id) -> None:
    """React to an inline-button tap. data == '<action>:<thread_key>'."""
    from . import telegram_notify as tg
    from .drafter import draft_reply

    action, _, thread_key = data.partition(":")
    row = store.get_thread(conn, thread_key)
    if not row:
        tg.answer_callback(cq_id, "Thread not found")
        return
    owner, pet, dates, stage, status = row

    if action == "sent":
        new_stage = advance_stage(stage)
        store.update_thread_stage(conn, thread_key, new_stage)
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)  # buttons done
        tg.answer_callback(cq_id, f"Marked sent · stage → {new_stage}")

    elif action == "conv":
        store.set_thread_status(conn, thread_key, "converted")
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)
        tg.answer_callback(cq_id, "Converted — drafting stopped for this thread")

    elif action == "unfit":
        store.set_thread_status(conn, thread_key, "not_suitable")
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)
        tg.answer_callback(cq_id, "Marked not suitable — drafting stopped")

    elif action in _TONE:
        history = store.get_thread_messages(conn, thread_key)
        try:
            d = draft_reply(owner, pet, dates, stage, history,
                            extra_instruction=_TONE[action])
        except Exception:
            log.exception("  re-draft failed for %s", thread_key)
            tg.answer_callback(cq_id, "Re-draft failed — try again")
            return
        store.set_last_draft(conn, thread_key, d.draft_text)
        tg.edit_message_text(
            chat_id, message_id,
            tg.format_draft_card(owner, dates, d.stage, d.flags, history, d.draft_text),
            reply_markup=tg.build_keyboard(thread_key))
        tg.answer_callback(cq_id, "Updated")

    else:
        tg.answer_callback(cq_id, "Unknown action")


def handle_notification(service, conn, schedule_draft, email_address: str,
                        history_id: str) -> None:
    from . import gmail_client  # deferred

    start = store.get_meta(conn, "last_history_id")
    if start is None:
        # First notification we've ever seen — set the baseline and wait for the next.
        store.set_meta(conn, "last_history_id", history_id)
        return
    try:
        ids = gmail_client.list_history(service, start)
    except Exception:
        log.exception("history.list failed from %s", start)
        return
    for mid in ids:
        try:
            process_message_id(service, conn, schedule_draft, mid)
        except Exception:
            log.exception("failed processing message %s", mid)
    store.set_meta(conn, "last_history_id", history_id)


def reconcile_startup(service, conn, schedule_draft) -> None:
    """Phase 4: catch up on messages that arrived while the service was down.

    Replays Gmail history from the stored checkpoint. Dedupe (already_seen) means
    anything already handled is skipped, so only genuinely-missed messages act.
    """
    start = store.get_meta(conn, "last_history_id")
    if not start:
        return
    try:
        ids = gmail_client_list_history(service, start)
    except Exception:
        log.exception("startup reconcile: history.list failed")
        return
    if ids:
        log.info("startup reconcile: %d message(s) since checkpoint", len(ids))
    for mid in ids:
        try:
            process_message_id(service, conn, schedule_draft, mid)
        except Exception:
            log.exception("startup reconcile: failed on %s", mid)


def gmail_client_list_history(service, start):
    from . import gmail_client
    return gmail_client.list_history(service, start)


def run_live() -> None:
    import threading

    from . import gmail_client, watch_renew  # deferred: needs Google libs
    from .pubsub_listener import listen
    from .debounce import Debouncer
    from .telegram_poll import poll_loop
    from .heartbeat import start as start_heartbeat

    conn = store.init_db(config.DB_PATH)
    service = gmail_client.build_service()

    # Phase 5: startup ping + periodic liveness heartbeat to Telegram.
    start_heartbeat(conn, config.HEARTBEAT_INTERVAL_SEC)

    # Phase 5: the playbook is gitignored, so a fresh deploy could be missing it.
    # An empty playbook = useless drafts, so surface it loudly rather than silently.
    from .drafter import load_text
    if not load_text(config.PLAYBOOK_PATH).strip():
        log.error("playbook missing/empty at %s — copy playbook.md.example and fill it in",
                  config.PLAYBOOK_PATH)
        from . import telegram_notify
        telegram_notify.send_alert(
            f"playbook.md missing/empty ({config.PLAYBOOK_PATH}) — drafts will be poor.")

    # Coalesce a burst of messages per thread into one draft call.
    debouncer = Debouncer(config.DEBOUNCE_SECONDS,
                          on_fire=lambda tk: draft_thread(conn, tk)).start()
    log.info("debouncer active: %ss window", config.DEBOUNCE_SECONDS)

    # Phase 4: receive button taps in a background thread.
    threading.Thread(
        target=poll_loop,
        args=(lambda d, c, m, q: handle_callback(conn, d, c, m, q),),
        daemon=True, name="telegram-poller",
    ).start()

    watch_renew.renew_once(service, conn)
    watch_renew.start_daily_renewal(service, conn)

    # Phase 4: catch up on anything missed during downtime, before waiting for pushes.
    reconcile_startup(service, conn, debouncer.bump)

    listen(lambda email, hist: handle_notification(
        service, conn, debouncer.bump, email, hist))


def run_replay(path: str) -> None:
    """Parse a local sample (optional leading 'Subject:' line) — no Gmail needed."""
    conn = store.init_db(config.DB_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    subject = ""
    for line in raw.splitlines():
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            break
    mid = "replay-" + hashlib.sha1(raw.encode()).hexdigest()[:12]
    thread_key = "replay-" + os.path.basename(path)
    pm = parse_notification(subject, raw, gmail_msg_id=mid, thread_key=thread_key)
    store.record_message(conn, pm)
    log.info(
        "REPLAY | kind=%s owner=%s pet=%s start=%s recognized=%s | msg=%r",
        pm.kind, pm.owner_name, pm.pet_name, pm.stay_start, pm.recognized,
        pm.message_text,
    )
    # Replay drafts immediately (no debounce) for single-file dev testing.
    dispatch(conn, pm, lambda tk: draft_thread(conn, tk))


def main() -> None:
    ap = argparse.ArgumentParser(description="Rover auto-responder — Phase 1")
    ap.add_argument("--replay", metavar="FILE", help="parse a local sample, no Gmail")
    args = ap.parse_args()
    if args.replay:
        run_replay(args.replay)
    else:
        run_live()


if __name__ == "__main__":
    main()