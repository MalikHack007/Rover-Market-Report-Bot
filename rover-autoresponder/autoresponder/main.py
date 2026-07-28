"""Entrypoint: watch -> Pub/Sub -> history.list -> parse -> store -> draft -> log.

Phase 2: adds the LLM drafter (stage machine + playbook). Drafts are logged, not
sent — Telegram delivery is Phase 3. Two modes:
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

    store.update_thread_stage(conn, thread_key, d.stage)
    if d.off_playbook:
        log.warning("  OFF-PLAYBOOK [%s] flags=%s (no draft) — needs your attention",
                    d.stage, d.flags)
        return
    store.set_last_draft(conn, thread_key, d.draft_text)
    flag_note = f" flags={d.flags}" if d.flags else ""
    log.info("  DRAFT [%s]%s (from %d msg) \n----- draft -----\n%s\n-----------------",
             d.stage, flag_note, len(history), d.draft_text)


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


def run_live() -> None:
    from . import gmail_client, watch_renew  # deferred: needs Google libs
    from .pubsub_listener import listen
    from .debounce import Debouncer

    conn = store.init_db(config.DB_PATH)
    service = gmail_client.build_service()

    # Coalesce a burst of messages per thread into one draft call.
    debouncer = Debouncer(config.DEBOUNCE_SECONDS,
                          on_fire=lambda tk: draft_thread(conn, tk)).start()
    log.info("debouncer active: %ss window", config.DEBOUNCE_SECONDS)

    watch_renew.renew_once(service, conn)
    watch_renew.start_daily_renewal(service, conn)
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
