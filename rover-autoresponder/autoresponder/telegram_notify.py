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

# One session for all calls: reusing the TCP/TLS connection removes a full handshake
# per request, which on a slow link was pushing calls past Telegram's ~15s callback
# expiry and leaving the button spinner hanging.
_SESSION = requests.Session()
# Telegram's hard limit is 4096 chars per message. Budget the card so the DRAFT is
# always complete (it's what you send), then give what's left to the client's messages —
# newest first, since that's what the draft is responding to. Anything that still
# doesn't fit is sent as a follow-up rather than silently cut (which used to undo
# truncation recovery: a recovered 944-char answer was chopped back to 500).
TELEGRAM_LIMIT = 4096
CARD_BUDGET = 3800          # leaves room for HTML tags and Telegram's own overhead
_MAX_MSG_CHARS = 500        # per-message cap when several must share the budget

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


def _split(m):
    """SMS passes ("Client"|"You", text) tuples; email passes plain strings."""
    if isinstance(m, (tuple, list)) and len(m) == 2:
        speaker, text = m
        return ("" if speaker == "Client" else "<i>you:</i> "), text
    return "", m


def _quote_messages(history, budget=None):
    """Render recent messages as quotes, newest given priority for the budget.

    Returns (blocks, overflow) where overflow holds full texts that had to be trimmed,
    so the caller can send them separately instead of losing them.
    """
    recent = list(history[-6:])
    overflow = []
    if budget is None:
        blocks = []
        for m in recent:
            prefix, text = _split(m)
            if len(text) > _MAX_MSG_CHARS:
                text = text[:_MAX_MSG_CHARS] + "…"
            blocks.append("<blockquote>" + prefix + _esc(text) + "</blockquote>")
        return blocks, overflow

    # Allocate newest-first so the message being replied to is the one shown in full.
    rendered = [None] * len(recent)
    remaining = budget
    for i in range(len(recent) - 1, -1, -1):
        prefix, text = _split(recent[i])
        if len(text) <= remaining:
            rendered[i] = "<blockquote>" + prefix + _esc(text) + "</blockquote>"
            remaining -= len(text)
            continue
        keep = max(0, remaining - 40)
        if keep < 80:                      # no room left worth using
            rendered[i] = None
            overflow.append(text)
            continue
        rendered[i] = ("<blockquote>" + prefix + _esc(text[:keep]) +
                       "…</blockquote>")
        overflow.append(text)
        remaining = 0
    return [b for b in rendered if b], overflow


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
    # The draft must never be cut — it's the thing you send. Everything else shares
    # what's left of the budget.
    tail = ["", "<b>Suggested reply</b> (tap to copy):",
            "<pre>" + _esc(draft_text) + "</pre>"]
    fixed = len("\n".join(lines + ["", "<b>Client said:</b>"] + tail))
    quotes, overflow = _quote_messages(history, budget=max(0, CARD_BUDGET - fixed))
    lines += ["", "<b>Client said:</b>"] + quotes + tail
    return "\n".join(lines), overflow


def format_offplaybook_card(owner, flags, history) -> str:
    lines = [f"⚠️ <b>Needs your attention — {_esc(owner) or 'unknown'}</b>"]
    if flags:
        lines.append(_esc("; ".join(flags)))
    tail = ["", "<i>No draft — handle this one manually.</i>"]
    fixed = len("\n".join(lines + ["", "<b>Client said:</b>"] + tail))
    quotes, overflow = _quote_messages(history, budget=max(0, CARD_BUDGET - fixed))
    lines += ["", "<b>Client said:</b>"] + quotes + tail
    return "\n".join(lines), overflow


def _call(method: str, payload: dict):
    """POST to the Bot API; return the 'result' object, or None on failure."""
    if not enabled():
        log.info("  (telegram disabled: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return None
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = _SESSION.post(url, json=payload, timeout=(5, 15))
    except Exception as e:
        log.warning("  telegram %s error: %s", method, type(e).__name__)
        return None
    if r.status_code != 200:
        body = r.text[:300]
        # Both of these are benign and expected in normal use:
        #   "message is not modified" — the buttons were already removed (double tap)
        #   "query is too old"        — we answered after Telegram's ~15s expiry
        if "message is not modified" in body:
            log.debug("  telegram %s: already in that state", method)
            return {}
        if "query is too old" in body or "query ID is invalid" in body:
            log.debug("  telegram %s: callback expired", method)
            return {}
        log.error("  telegram %s failed: %s %s", method, r.status_code, body)
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


def send_draft_card(owner, dates, stage, flags, history, draft_text,
                    needs_review=False, reply_markup=None):
    """Send a draft card, spilling any over-long client message into a follow-up.

    Returns the card's message_id (the one to link for reply-to-edit), so the follow-up
    never becomes the card you reply to.
    """
    card, overflow = format_draft_card(owner, dates, stage, flags, history, draft_text,
                                       needs_review=needs_review)
    mid = send_message(card, reply_markup=reply_markup)
    for text in overflow:
        send_message("📄 <b>Full message</b> (too long for the card):\n"
                     "<blockquote>" + _esc(text[:3500]) + "</blockquote>")
    return mid


def send_offplaybook_card(owner, flags, history):
    card, overflow = format_offplaybook_card(owner, flags, history)
    mid = send_message(card)
    for text in overflow:
        send_message("📄 <b>Full message</b>:\n<blockquote>" + _esc(text[:3500]) +
                     "</blockquote>")
    return mid