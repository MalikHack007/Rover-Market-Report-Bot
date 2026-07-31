"""Phase 4: receive Telegram button taps via a synchronous getUpdates long-poll.

Runs in its own daemon thread. We deliberately avoid python-telegram-bot's async
runtime; a plain long-poll matches the rest of the (synchronous) service. Only
callbacks from the configured chat are honored, so a stranger who finds the bot
can't drive it.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def dispatch_update(upd: dict, on_callback, allowed_chat) -> None:
    """Handle one update. Ignores everything but callback_query from allowed_chat."""
    cq = upd.get("callback_query")
    if not cq:
        return
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if allowed_chat and str(chat_id) != str(allowed_chat):
        log.warning("ignoring callback from unexpected chat %s", chat_id)
        return
    on_callback(cq.get("data", ""), chat_id, msg.get("message_id"), cq.get("id"))


def poll_loop(on_callback, stop_event=None, long_poll_sec: int = 25) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("telegram poll disabled (no token/chat)")
        return
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    offset = None
    log.info("telegram button poller active")
    while not (stop_event and stop_event.is_set()):
        try:
            params = {"timeout": long_poll_sec, "allowed_updates": ["callback_query"]}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=long_poll_sec + 15)
            if r.status_code != 200:
                log.error("getUpdates failed: %s %s", r.status_code, r.text[:200])
                time.sleep(3)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    dispatch_update(upd, on_callback, config.TELEGRAM_CHAT_ID)
                except Exception:
                    log.exception("callback handling failed")
        except Exception:
            log.exception("getUpdates loop error")
            time.sleep(3)
