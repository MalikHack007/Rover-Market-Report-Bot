"""Photo callback router + batch Send-all (the keystone, batch-approved) + budget guardrail.

Nothing sends until Malik taps the single **Send all**, which then dispatches every `ready`
dog-update sequentially: claim → upload each photo to R2 → one MMS per dog via Telerivet →
record. Idempotent per dog-update; sends are capped by the daily 50-message budget.
"""
import logging

from .. import store as base
from . import store as pstore, captions, config
from . import telegram as ui
from . import pipeline
from .hosting import upload
from .telerivet import TelerivetClient

log = logging.getLogger(__name__)

_ACK = {"pick": "…", "review": "Building review…", "sendall": "Sending…",
        "cap": "New caption…", "edit": "Reply with your caption", "more": "Send more…",
        "hold": "…", "disc": "Removed"}


def handle_callback(conn, data, chat_id, message_id, cq_id):
    """Route a `ph:*` callback. Returns True if it belonged to this feature."""
    if not data.startswith("ph:"):
        return False
    action, _, arg = data[len("ph:"):].partition(":")
    if action == "pick":
        pipeline.set_active_dog(conn, chat_id, arg, cq_id)
    elif action == "review":
        pipeline.review(conn, chat_id, cq_id)
    elif action == "sendall":
        send_all(conn, chat_id, cq_id)
    elif action in ("cap", "edit", "more", "hold", "disc"):
        _update_action(conn, chat_id, action, _int(arg), message_id, cq_id)
    else:
        ui.answer(cq_id, "…")
    return True


def _update_action(conn, chat_id, action, update_id, message_id, cq_id):
    row = pstore.get_update(conn, update_id)
    if not row:
        ui.answer(cq_id, "That update is gone.")
        return
    _id, _batch, thread, _ep, pet, caption, cap_idx, status, _tid = row
    if action == "cap":                                   # re-roll the caption from the pool
        text, idx = captions.pick(pet, avoid_index=cap_idx)
        pstore.set_caption(conn, update_id, text, idx)
        pstore.set_caption_last(conn, thread, idx)
        pipeline.render_card(conn, update_id, message_id)
        ui.answer(cq_id, "New caption")
    elif action == "edit":
        pstore.link_card(conn, message_id, update_id)      # so the text reply targets this card
        ui.answer(cq_id, "Reply to this card with your caption")
    elif action == "more":
        pstore.set_active_dog(conn, chat_id, thread)
        ui.answer(cq_id, f"Send more photos for {pet} 📷")
    elif action == "hold":
        pstore.set_status(conn, update_id, "ready" if status == "held" else "held")
        pipeline.render_card(conn, update_id, message_id)
        ui.answer(cq_id, "Held" if status != "held" else "Un-held")
    elif action == "disc":
        pstore.set_status(conn, update_id, "discarded")
        ui.edit_markup(chat_id, message_id, reply_markup=None)
        ui.edit_text(chat_id, message_id, f"🗑 <s>{ui.esc(pet)}</s> — discarded")
        ui.answer(cq_id, "Discarded")


def send_all(conn, chat_id, cq_id=None):
    ui.answer(cq_id, "Sending…")
    batch = pstore.current_batch(conn, chat_id)
    if not batch:
        ui.send("No batch to send. Send /photos to start.")
        return
    updates = pstore.list_batch(conn, batch, statuses=("ready",))
    if not updates:
        ui.send("Nothing ready to send.")
        return

    gw = TelerivetClient()
    sent = failed = skipped = 0
    for u in updates:
        uid, _b, thread, _ep, pet, caption, _ci, _st, _tid = u
        # Guardrail: never exceed the daily message cap; leftover stays `ready` for tomorrow.
        if pstore.sends_today(conn) >= config.TELERIVET_DAILY_MSG_CAP:
            skipped += 1
            continue
        if not pstore.claim_send(conn, uid):      # idempotent: someone/some retry already has it
            continue
        try:
            urls = []
            for m in pstore.get_media(conn, uid):
                media_id, _fid, local_path, _key, _pos = m
                url, key = upload(local_path)
                pstore.set_media_r2_key(conn, media_id, key)
                urls.append(url)
            res = gw.send(thread, caption or "", media_urls=urls or None)
        except Exception:
            log.exception("photo send failed for update %s (%s)", uid, pet)
            pstore.set_status(conn, uid, "ready")  # back to ready so a retry Send-all catches it
            failed += 1
            ui.send(f"⚠️ Couldn't send {ui.esc(pet)}'s update — it stays ready; tap Send all "
                    "again to retry.")
            continue
        pstore.set_telerivet_id(conn, uid, res.get("id"))
        pstore.set_status(conn, uid, "sent")
        pstore.bump_sends(conn, 1)
        pstore.bump_api_calls(conn, 1)
        sent += 1

    parts = [f"Sent {sent}"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped (daily 50-send cap — left for tomorrow)")
    remaining = config.TELERIVET_DAILY_MSG_CAP - pstore.sends_today(conn)
    ui.send("✅ " + " · ".join(parts) + f". {remaining} sends left today.")


def handle_text_reply(conn, text, chat_id, reply_to_message_id):
    """`/photos` starts a session; a reply to a photo card edits that dog's caption.
    Returns True if this feature handled the message (so the SMS handler is skipped)."""
    stripped = (text or "").strip()
    if stripped.lower() in ("/photos", "/photo"):
        pipeline.start_session(conn, chat_id)
        return True
    if not reply_to_message_id:
        return False
    uid = pstore.update_for_card(conn, reply_to_message_id)
    if not uid:
        return False
    pstore.set_caption(conn, uid, stripped)
    pipeline.render_card(conn, uid, reply_to_message_id)
    ui.send("✏️ Caption updated.")
    return True


def _int(x, default=-1):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default
