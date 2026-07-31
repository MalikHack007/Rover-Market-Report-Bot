"""Phase 5: liveness heartbeat.

Sends a "started" ping on boot and a periodic summary so a silently-dead service
is noticeable (no heartbeat = something's wrong). No-op if Telegram isn't set up.
"""
import logging
import threading
import time

from . import store, telegram_notify

log = logging.getLogger(__name__)


def summary(conn) -> str:
    s = store.stats(conn)
    return (
        "✅ <b>Rover auto-responder heartbeat</b>\n"
        f"messages (24h): {s['messages_24h']}\n"
        f"threads — active: {s['active']}, "
        f"converted: {s['converted']}, not suitable: {s['not_suitable']}"
    )


def start(conn, interval_sec: int, stop_event: threading.Event = None):
    def loop():
        telegram_notify.send_message("✅ Rover auto-responder started.")
        while not (stop_event and stop_event.is_set()):
            time.sleep(interval_sec)
            try:
                telegram_notify.send_message(summary(conn))
            except Exception:
                log.exception("heartbeat send failed")

    t = threading.Thread(target=loop, daemon=True, name="heartbeat")
    t.start()
    return t
