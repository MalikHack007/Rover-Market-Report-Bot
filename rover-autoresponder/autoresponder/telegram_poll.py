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


def dispatch_update(upd: dict, on_callback, allowed_chat, on_text=None, on_photo=None) -> None:
    """Handle one update.

    callback_query -> on_callback(data, chat_id, message_id, cq_id)
    message text   -> on_text(text, chat_id, reply_to_message_id)  (SMS edit path / commands)
    message photo  -> on_photo(file_id, chat_id)                   (Addendum C photo intake)
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
    if not m:
        return
    chat_id = (m.get("chat") or {}).get("id")
    if allowed_chat and str(chat_id) != str(allowed_chat):
        log.warning("ignoring message from unexpected chat %s", chat_id)
        return
    # A photo message (Addendum C): take the largest size Telegram offers.
    if m.get("photo") and on_photo:
        on_photo(m["photo"][-1]["file_id"], chat_id)
        return
    if on_text:
        text = m.get("text")
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if text:
            on_text(text, chat_id, reply_to)


def poll_loop(on_callback, stop_event=None, long_poll_sec: int = 10,
              on_text=None, on_photo=None) -> None:
    """Long-poll Telegram for button taps / text replies.

    Timeout tuning: a long-poll connection can go stale silently (NAT timeout, Wi-Fi
    blip, Telegram dropping it). While it's dead, taps land in a black hole until the
    read timeout fires. Keeping the poll short (10s) and the read timeout only a little
    longer (+8s) means a stale connection is detected in seconds instead of ~40, which
    is what caused button taps to take ~30s to register.

    Cost of the shorter poll is negligible: an empty getUpdates every 10s is a tiny
    request, and Telegram is designed for exactly this pattern.
    """
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("telegram poll disabled (no token/chat)")
        return
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    offset = None
    session = requests.Session()          # reuse the TCP/TLS connection between polls
    consecutive_errors = 0
    log.info("telegram button poller active (%ss long-poll)", long_poll_sec)
    while not (stop_event and stop_event.is_set()):
        try:
            allowed = ["callback_query"] + (["message"] if (on_text or on_photo) else [])
            params = {"timeout": long_poll_sec, "allowed_updates": allowed}
            if offset is not None:
                params["offset"] = offset
            # read timeout just past the poll window: detect a dead connection fast.
            r = session.get(url, params=params, timeout=(10, long_poll_sec + 8))
            if r.status_code != 200:
                log.error("getUpdates failed: %s %s", r.status_code, r.text[:200])
                time.sleep(3)
                continue
            consecutive_errors = 0
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                # `upd` is one Telegram Update object. `allowed_updates` limits us to the
                # two variants dispatch_update handles; only the fields it actually reads
                # are shown below (Telegram sends many more we ignore):
                #
                #   button tap (callback_query):
                #     {"update_id": 900123,
                #      "callback_query": {
                #        "id": "4382bfe1",                 # -> answerCallbackQuery (cq_id)
                #        "data": "send:+15551234567",      # "<action>:<thread_key>"
                #        "message": {"message_id": 55,     # the card that was tapped
                #                    "chat": {"id": 111}}}} # must equal TELEGRAM_CHAT_ID
                #
                #   text reply (message) — the edit / "/pet" / "/booking" path:
                #     {"update_id": 900124,
                #      "message": {
                #        "text": "actually make it warmer",
                #        "chat": {"id": 111},              # must equal TELEGRAM_CHAT_ID
                #        "reply_to_message": {"message_id": 55}}} # set only when it's a reply
                #
                # Each update carries exactly ONE of callback_query / message; missing
                # keys are why dispatch_update reads everything defensively with .get().
                try:
                    dispatch_update(upd, on_callback, config.TELEGRAM_CHAT_ID,
                                    on_text=on_text, on_photo=on_photo)
                except Exception:
                    log.exception("callback handling failed")
        except requests.exceptions.RequestException as e:
            # A stale long-poll timing out is normal and self-healing. Only escalate
            # to a full traceback if it keeps happening (a real connectivity problem).
            consecutive_errors += 1
            if consecutive_errors <= 3:
                log.info("getUpdates reconnecting (%s)", type(e).__name__)
            else:
                log.warning("getUpdates failing repeatedly (%d in a row): %s",
                            consecutive_errors, e)
            session.close()
            session = requests.Session()   # fresh connection after a failure
            time.sleep(2)
        except Exception:
            log.exception("getUpdates loop error")
            time.sleep(3)