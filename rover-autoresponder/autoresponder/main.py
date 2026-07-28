"""Phase 1 entrypoint: watch -> Pub/Sub -> history.list -> parse -> store -> log.

No LLM, no Telegram yet. Two modes:
  python -m autoresponder.main            # live (needs Gmail + Pub/Sub set up)
  python -m autoresponder.main --replay samples/vatsal_message.txt   # offline dev
"""
import argparse
import hashlib
import logging

from . import config, store
from .parser import parse_notification

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("autoresponder")


def process_message_id(service, conn, msg_id: str) -> None:
    from . import gmail_client  # deferred: only the live path needs Google libs

    if store.already_seen(conn, msg_id):
        log.debug("skip already-seen %s", msg_id)
        return
    msg = gmail_client.get_message(service, msg_id)
    subject, body_text, thread_id = gmail_client.extract_fields(msg)
    pm = parse_notification(subject, body_text, gmail_msg_id=msg_id, thread_key=thread_id)
    if not store.record_message(conn, pm):
        return
    if pm.recognized:
        log.info(
            "NEW MSG | thread=%s owner=%s pet=%s dates=%s->%s | msg=%r",
            pm.thread_key, pm.owner_name, pm.pet_name, pm.stay_start, pm.stay_end,
            pm.message_text,
        )
    else:
        log.warning(
            "UNRECOGNIZED format | subject=%r | stored for template review | id=%s",
            pm.raw_subject, msg_id,
        )


def handle_notification(service, conn, email_address: str, history_id: str) -> None:
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
            process_message_id(service, conn, mid)
        except Exception:
            log.exception("failed processing message %s", mid)
    store.set_meta(conn, "last_history_id", history_id)


def run_live() -> None:
    from . import gmail_client, watch_renew  # deferred: needs Google libs
    from .pubsub_listener import listen

    conn = store.init_db(config.DB_PATH)
    service = gmail_client.build_service()
    watch_renew.renew_once(service, conn)
    watch_renew.start_daily_renewal(service, conn)
    listen(lambda email, hist: handle_notification(service, conn, email, hist))


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
    pm = parse_notification(subject, raw, gmail_msg_id=mid, thread_key="replay-thread")
    store.record_message(conn, pm)
    log.info(
        "REPLAY | owner=%s pet=%s dates=%s->%s recognized=%s | msg=%r",
        pm.owner_name, pm.pet_name, pm.stay_start, pm.stay_end, pm.recognized,
        pm.message_text,
    )


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
