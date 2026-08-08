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
    ap = argparse.ArgumentParser(description="Rover SMS bridge (S1)")
    ap.add_argument("--serve", action="store_true", help="run the inbound webhook receiver")
    ap.add_argument("--send", nargs=2, metavar=("NUMBER", "TEXT"),
                    help="send a test SMS via the phone gateway")
    args = ap.parse_args()

    if args.send:
        from .sms_gateway import SmsGateForAndroid
        number, text = args.send
        mid = SmsGateForAndroid().send(number, text)
        print("sent" if mid else "FAILED", "— gateway message id:", mid)
    elif args.serve:
        from .sms_receiver import serve
        serve()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
