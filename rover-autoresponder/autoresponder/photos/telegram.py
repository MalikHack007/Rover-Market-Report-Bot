"""Telegram rendering + photo download for the photo-update feature.

Reuses telegram_notify's low-level send/edit/answer primitives, and owns the feature's own
callback-data scheme (`ph:*`) and keyboards/cards so the SMS Telegram code stays untouched.
"""
import os
import tempfile
import uuid

import requests

from .. import config as _cfg
from .. import telegram_notify as _tg

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE = "https://api.telegram.org/file/bot{token}/{path}"
STAGING_DIR = os.path.join(tempfile.gettempdir(), "rover_photos")

# convenience re-exports of the shared primitives
send = _tg.send_message
edit_text = _tg.edit_message_text
edit_markup = _tg.edit_reply_markup
answer = _tg.answer_callback
esc = _tg._esc


def download_photo(file_id):
    """Download a Telegram photo by file_id → a staged local path. Returns path or None."""
    token = _cfg.TELEGRAM_BOT_TOKEN
    if not token:
        return None
    try:
        r = requests.get(_API.format(token=token, method="getFile"),
                         params={"file_id": file_id}, timeout=(5, 20))
        r.raise_for_status()
        file_path = (r.json().get("result") or {}).get("file_path")
        if not file_path:
            return None
        data = requests.get(_FILE.format(token=token, path=file_path), timeout=(5, 60))
        data.raise_for_status()
    except Exception:
        return None
    os.makedirs(STAGING_DIR, exist_ok=True)
    ext = os.path.splitext(file_path)[1] or ".jpg"
    dest = os.path.join(STAGING_DIR, f"{uuid.uuid4().hex}{ext}")
    with open(dest, "wb") as fh:
        fh.write(data.content)
    return dest


def send_photos(file_ids, caption=None):
    """Show a dog's photos in Telegram by their EXISTING file_ids (no re-upload).

    1 photo → sendPhoto; 2–10 → one album (sendMediaGroup); >10 → successive albums
    (Telegram caps an album at 10). Albums can't carry buttons, so the control card that
    follows owns the caption + buttons — these messages are purely the visual preview.
    """
    if not file_ids:
        return
    if len(file_ids) == 1:
        payload = {"chat_id": _cfg.TELEGRAM_CHAT_ID, "photo": file_ids[0]}
        if caption:
            payload["caption"] = caption
        _tg._call("sendPhoto", payload)
        return
    for i in range(0, len(file_ids), 10):
        media = [{"type": "photo", "media": fid} for fid in file_ids[i:i + 10]]
        if caption and i == 0:
            media[0]["caption"] = caption
        _tg._call("sendMediaGroup", {"chat_id": _cfg.TELEGRAM_CHAT_ID, "media": media})


# --- keyboards (callback_data = ph:<action>[:<arg>]) ---------------------
def roster_keyboard(roster):
    """One button per dog in custody + a Review button."""
    rows = [[{"text": f"{e['pet'] or '?'} · {e['owner'] or '?'}",
              "callback_data": f"ph:pick:{e['thread_key']}"}] for e in roster]
    rows.append([{"text": "✅ Review & send", "callback_data": "ph:review"}])
    return {"inline_keyboard": rows}


def review_keyboard(update_id, held=False):
    return {"inline_keyboard": [
        [{"text": "✏️ Edit caption", "callback_data": f"ph:edit:{update_id}"},
         {"text": "🔁 Another caption", "callback_data": f"ph:cap:{update_id}"}],
        [{"text": "➕ More photos", "callback_data": f"ph:more:{update_id}"},
         {"text": "▶️ Unhold" if held else "⏸ Hold", "callback_data": f"ph:hold:{update_id}"},
         {"text": "🗑", "callback_data": f"ph:disc:{update_id}"}],
    ]}


def sendall_keyboard(n):
    return {"inline_keyboard": [[{"text": f"✅ Send all ({n})", "callback_data": "ph:sendall"}]]}


# --- cards ---------------------------------------------------------------
def review_card_text(pet, owner, n_photos, caption, held=False):
    head = f"🐾 <b>{esc(pet) or '?'}</b> → {esc(owner) or '?'}"
    if held:
        head = "⏸ " + head + "  <i>(held — won't send)</i>"
    plural = "s" if n_photos != 1 else ""
    return "\n".join([head, f"{n_photos} photo{plural}", "",
                      "<b>Caption:</b>", "<pre>" + esc(caption or "") + "</pre>"])
