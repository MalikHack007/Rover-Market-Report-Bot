"""Addendum A / S1 entrypoint — prove the phone<->box bridge, both directions.

  python -m autoresponder.sms_main --serve                 # run the inbound receiver
  python -m autoresponder.sms_main --send "+15551234567" "test from the bot"

Standalone from the email pipeline (which keeps running) until later S-phases wire
them together.
"""
import argparse
import logging

from . import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sms")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rover SMS bridge")
    ap.add_argument("--serve", action="store_true", help="run the inbound webhook receiver")
    ap.add_argument("--send", nargs=2, metavar=("NUMBER", "TEXT"),
                    help="send a test SMS via the phone gateway")
    ap.add_argument("--replay", nargs=2, metavar=("NUMBER", "TEXT"),
                    help="feed one SMS through the pipeline offline (no phone)")
    args = ap.parse_args()

    if args.send:
        from .sms_gateway import SmsGateForAndroid
        number, text = args.send
        mid = SmsGateForAndroid().send(number, text)
        print("sent" if mid else "FAILED", "— gateway message id:", mid)
        return

    if args.replay:
        from . import store
        from .sms_pipeline import handle_sms, draft_for_thread
        conn = store.init_db(config.DB_PATH)
        # Replay drafts immediately (no debounce) for single-message dev testing.
        handle_sms(conn, args.replay[0], args.replay[1],
                   schedule_draft=lambda n: draft_for_thread(conn, n))
        return

    if args.serve:
        from . import store
        from .sms_receiver import serve
        from .sms_pipeline import handle_sms, draft_for_thread
        from .debounce import Debouncer

        conn = store.init_db(config.DB_PATH)

        # S3: coalesce a burst of messages per thread into ONE draft call. Rover's
        # opening arrives as several texts (booking block, "will you be available",
        # sometimes a later afterthought) — all should produce a single draft.
        debouncer = Debouncer(config.DEBOUNCE_SECONDS,
                              on_fire=lambda n: draft_for_thread(conn, n)).start()
        log.info("debouncer active: %ss window", config.DEBOUNCE_SECONDS)

        def on_event(data):
            if store.sms_event_seen(conn, data.get("id")):
                return                       # gateway retry of an event we handled
            if data.get("event") != "sms:received":
                from .sms_receiver import log_event
                log_event(data)
                return
            p = data.get("payload") or {}
            handle_sms(conn, p.get("sender"), p.get("message"),
                       schedule_draft=debouncer.bump)

        serve(on_event=on_event)
        return

    ap.print_help()


if __name__ == "__main__":
    main()