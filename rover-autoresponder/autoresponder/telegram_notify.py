"""Phase 3: deliver drafts to Telegram (SEND-ONLY).

Uses the raw Telegram Bot API over HTTP (requests) rather than python-telegram-bot,
to keep the whole service synchronous (Pub/Sub streaming pull + SQLite + debouncer
are all sync). Phase 4 will add a synchronous getUpdates long-poll here for buttons.

Drafts are sent to a single chat (yours) formatted for tap-to-copy: the reply sits
in a <pre> block, which Telegram renders with a copy button on mobile.
"""
import html
import logging

import requests

from . import config

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_MSG_CHARS = 500      # cap each quoted client message so the card stays under Telegram's 4096


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _esc(s) -> str:
    """Escape text placed inside Telegram HTML tags (& < > matter for owners/dogs like 'Rusty & Osha')."""
    return html.escape(s or "", quote=False)


def _quote_messages(history) -> list:
    out = []
    for m in history[-6:]:                       # last few messages only
        text = m if len(m) <= _MAX_MSG_CHARS else m[:_MAX_MSG_CHARS] + "…"
        out.append("<blockquote>" + _esc(text) + "</blockquote>")
    return out


def format_draft_card(owner, dates, stage, flags, history, draft_text) -> str:
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


def send_message(text, parse_mode="HTML") -> bool:
    if not enabled():
        log.info("  (telegram disabled: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return False
    url = _API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    try:
        r = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception:
        log.exception("  telegram send error")
        return False
    if r.status_code != 200:
        log.error("  telegram send failed: %s %s", r.status_code, r.text[:300])
        return False
    return True
