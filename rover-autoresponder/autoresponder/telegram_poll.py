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


def dispatch_update(upd: dict, on_callback, allowed_chat, on_text=None) -> None:
    """Handle one update.

    callback_query -> on_callback(data, chat_id, message_id, cq_id)
    message (S4)   -> on_text(text, chat_id, reply_to_message_id) for the EDIT path:
                      replying to a draft card with new wording edits that draft.
    Only traffic from allowed_chat is honored.
    """
    cq = upd.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if allowed_chat and str(chat_id) != str(allowed_chat):
            log.warning("ignoring callback from unexpected chat %s", chat_id)
            return
        on_callback(cq.get("data", ""), chat_id, msg.get("message_id"), cq.get("id"))
        return

    m = upd.get("message")
    if m and on_text:
        chat_id = (m.get("chat") or {}).get("id")
        if allowed_chat and str(chat_id) != str(allowed_chat):
            log.warning("ignoring message from unexpected chat %s", chat_id)
            return
        text = m.get("text")
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if text:
            on_text(text, chat_id, reply_to)


def poll_loop(on_callback, stop_event=None, long_poll_sec: int = 25,
              on_text=None) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("telegram poll disabled (no token/chat)")
        return
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    offset = None
    log.info("telegram button poller active")
    while not (stop_event and stop_event.is_set()):
        try:
            allowed = ["callback_query"] + (["message"] if on_text else [])
            params = {"timeout": long_poll_sec, "allowed_updates": allowed}
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
                    dispatch_update(upd, on_callback, config.TELEGRAM_CHAT_ID,
                                    on_text=on_text)
                except Exception:
                    log.exception("callback handling failed")
        except Exception:
            log.exception("getUpdates loop error")
            time.sleep(3)