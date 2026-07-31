"""Daily renewal of the Gmail watch() (it expires ~7 days out).

A lapsed watch silently stops all triggers, so a failed renewal is logged at
ERROR so alerting (Phase 5) can catch it.
"""
import logging
import threading
import time

from . import gmail_client, store

log = logging.getLogger(__name__)

RENEW_INTERVAL_SEC = 24 * 3600


def renew_once(service, conn) -> dict:
    resp = gmail_client.start_watch(service)
    hist, exp = resp.get("historyId"), resp.get("expiration")
    # Seed the baseline history id only if we have none yet; don't clobber progress.
    if store.get_meta(conn, "last_history_id") is None and hist:
        store.set_meta(conn, "last_history_id", hist)
    store.set_meta(conn, "watch_expiration", exp)
    log.info("watch() renewed | historyId=%s expiration=%s", hist, exp)
    return resp


def start_daily_renewal(service, conn, stop_event: threading.Event = None):
    def loop():
        while not (stop_event and stop_event.is_set()):
            time.sleep(RENEW_INTERVAL_SEC)
            try:
                renew_once(service, conn)
            except Exception:
                log.exception("watch() renewal FAILED — trigger may be dead")
                # Phase 5: surface this silent-killer to Telegram.
                try:
                    from . import telegram_notify
                    telegram_notify.send_alert(
                        "Gmail watch() renewal failed — push notifications may stop. "
                        "Check the service/logs.")
                except Exception:
                    pass

    t = threading.Thread(target=loop, daemon=True, name="watch-renewal")
    t.start()
    return t