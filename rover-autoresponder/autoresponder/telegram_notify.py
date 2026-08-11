"""Telegram delivery + interactive controls (Phase 3 send, Phase 4 buttons).

Raw Telegram Bot API over HTTP to keep the service synchronous. Sending lives here;
the receive side (getUpdates long-poll) is in telegram_poll.py.
"""
import html
import logging

import requests

from . import config

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_MSG_CHARS = 500

# Phase 4: inline-button layout. callback_data is "<action>:<thread_key>" (< 64 bytes).
_BUTTONS = [
    [("✅ Mark sent", "sent"), ("🔁 Regenerate", "regen")],
    [("☀️ Warmer", "warm"), ("✂️ Shorter", "short")],
    [("🎉 Converted", "conv"), ("🚫 Not suitable", "unfit")],
]


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _esc(s) -> str:
    return html.escape(s or "", quote=False)


def _quote_messages(history) -> list:
    out = []
    for m in history[-6:]:
        # Addendum A: SMS passes ("Client"|"You", text) tuples; email passes strings.
        if isinstance(m, (tuple, list)) and len(m) == 2:
            speaker, text = m
            prefix = "" if speaker == "Client" else "<i>you:</i> "
        else:
            prefix, text = "", m
        text = text if len(text) <= _MAX_MSG_CHARS else text[:_MAX_MSG_CHARS] + "…"
        out.append("<blockquote>" + prefix + _esc(text) + "</blockquote>")
    return out


# Addendum A / S4: SMS keyboard. Approve & Send TRANSMITS to the client, so it sits
# alone on the top row to reduce mis-taps.
_SMS_BUTTONS = [
    [("✅ Approve & Send", "send")],
    [("✏️ Edit", "edit"), ("🔁 Regenerate", "regen")],
    [("☀️ Warmer", "warm"), ("✂️ Shorter", "short")],
    [("🎉 Converted", "conv"), ("🚫 Not suitable", "unfit")],
]


def build_sms_keyboard(thread_key: str) -> dict:
    """Keyboard for SMS draft cards (approve-and-send flow)."""
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": f"{a}:{thread_key}"} for t, a in row]
            for row in _SMS_BUTTONS
        ]
    }


def build_keyboard(thread_key: str) -> dict:
    """Phase 4: inline keyboard whose buttons carry the thread key."""
    return {
        "inline_keyboard": [
            [{"text": t, "callback_data": f"{a}:{thread_key}"} for t, a in row]
            for row in _BUTTONS
        ]
    }


def format_draft_card(owner, dates, stage, flags, history, draft_text,
                      needs_review: bool = False) -> str:
    """needs_review=True (off-playbook): same card, but flagged for careful reading.

    The draft is still shown with buttons so you can edit-and-send from Telegram
    rather than having to go handle it manually elsewhere.
    """
    if needs_review:
        lines = [f"⚠️ <b>Needs your review — {_esc(owner) or 'unknown'}</b>",
                 "<i>Off-playbook — read carefully before sending.</i>"]
    else:
        lines = [f"🐾 <b>New inquiry — {_esc(owner) or 'unknown'}</b>"]
    meta = f"Stage: {_esc(stage)}"
    if dates:
        meta += f" · starting {_esc(dates)}"
    lines.append(meta)
    if flags:
        lines.append("⚠️ " + _esc("; ".join(flags)))
    lines += ["", "<b>Client said:</b>"] + _quote_messages(history)
    lines += ["", "<b>Suggested reply</b> (tap to copy):",
              "<pre>" + _esc(draft_text) + "</pre>"]
    return "\n".join(lines)


def format_offplaybook_card(owner, flags, history) -> str:
    lines = [f"⚠️ <b>Needs your attention — {_esc(owner) or 'unknown'}</b>"]
    if flags:
        lines.append(_esc("; ".join(flags)))
    lines += ["", "<b>Client said:</b>"] + _quote_messages(history)
    lines += ["", "<i>No draft — handle this one manually.</i>"]
    return "\n".join(lines)


def _call(method: str, payload: dict):
    """POST to the Bot API; return the 'result' object, or None on failure."""
    if not enabled():
        log.info("  (telegram disabled: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return None
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = requests.post(url, json=payload, timeout=15)
    except Exception:
        log.exception("  telegram %s error", method)
        return None
    if r.status_code != 200:
        log.error("  telegram %s failed: %s %s", method, r.status_code, r.text[:300])
        return None
    return r.json().get("result")


def send_message(text, parse_mode="HTML", reply_markup=None):
    """Send a message. Returns the new message_id (int) or None."""
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _call("sendMessage", payload)
    return (result or {}).get("message_id") if result else None


# --- Phase 4: edit / answer for button interactions ---
def edit_message_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=None) -> bool:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _call("editMessageText", payload) is not None


def edit_reply_markup(chat_id, message_id, reply_markup=None) -> bool:
    """Replace/remove a message's buttons (pass None to strip them)."""
    payload = {"chat_id": chat_id, "message_id": message_id}
    payload["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
    return _call("editMessageReplyMarkup", payload) is not None


def answer_callback(callback_query_id, text="") -> bool:
    return _call("answerCallbackQuery",
                 {"callback_query_id": callback_query_id, "text": text}) is not None


# --- Phase 5: alerting ---
def send_alert(text) -> bool:
    """Push an operational alert to the chat (silent-failure surfacing)."""
    return send_message("⚠️ <b>Rover bot alert</b>\n" + _esc(text)) is not None