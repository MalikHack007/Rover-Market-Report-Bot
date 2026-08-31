"""Tap-to-assign photo intake + roster + review (Addendum C, P1).

Flow: /photos opens a batch and shows the roster → Malik taps a dog (active dog) and sends
its photo(s), which accumulate → tap the next dog → Review builds one card per dog with a
pool caption and the Send-all button. No name typing.
"""
import logging

from .. import store as base
from . import store as pstore, captions
from . import telegram as ui

log = logging.getLogger(__name__)


def start_session(conn, chat_id):
    """/photos — open a fresh batch and show the roster to tap."""
    pstore.start_batch(conn, chat_id)
    _show_roster(conn, intro=True)


def _show_roster(conn, intro=False):
    roster = pstore.list_active_bookings(conn)
    if not roster:
        ui.send("No dogs are in custody today — no confirmed Rover booking covers today.")
        return
    updated = pstore.threads_updated_today(conn)          # P3: mark who already got one today
    legend = "  (✅ = already sent an update today)" if updated else ""
    text = ("📸 <b>Photo updates</b>\nTap a dog, then send their photo(s). Tap the next dog "
            "for theirs. Hit <b>Review &amp; send</b> when done." + legend if intro
            else "Tap a dog, then send their photo(s):" + legend)
    ui.send(text, reply_markup=ui.roster_keyboard(roster, updated=updated))


def set_active_dog(conn, chat_id, thread_key, cq_id=None):
    """A roster button was tapped: this dog becomes active; photos now attach to it."""
    entry = {e["thread_key"]: e for e in pstore.list_active_bookings(conn)}.get(thread_key)
    if not entry:
        ui.answer(cq_id, "That dog isn't in custody anymore.")
        return
    batch = pstore.current_batch(conn, chat_id) or pstore.start_batch(conn, chat_id)
    pstore.set_active_dog(conn, chat_id, thread_key)
    pstore.get_or_create_update(conn, batch, thread_key, entry["episode"], entry["pet"])
    ui.answer(cq_id, f"Now send {entry['pet']}'s photos 📷")


def on_photo(conn, chat_id, file_id, media_group_id=None):
    """An incoming photo attaches to the active dog's update (downloaded + staged).

    P3 album intake: Telegram delivers each photo of an album as a SEPARATE message sharing
    one `media_group_id`. They all attach to the still-active dog automatically, but the
    per-photo prompts/acks would otherwise fire once per photo. So the "tap a dog first"
    nudge (no active dog) and the first-photo "collecting" ack are each emitted at most ONCE
    per album — tracked by the last group we reacted to for this chat.
    """
    batch = pstore.current_batch(conn, chat_id)
    active = pstore.active_dog(conn, chat_id)
    roster = {e["thread_key"]: e for e in pstore.list_active_bookings(conn)}
    same_group = media_group_id and media_group_id == pstore.last_photo_group(conn, chat_id)
    if not batch or not active or active not in roster:
        pstore.set_active_dog(conn, chat_id, "")
        if not same_group:                       # nudge once per album, not once per photo
            pstore.set_last_photo_group(conn, chat_id, media_group_id)
            ui.send("Tap a dog first, then send their photos:",
                    reply_markup=ui.roster_keyboard(
                        pstore.list_active_bookings(conn),
                        updated=pstore.threads_updated_today(conn)))
        return
    entry = roster[active]
    path = ui.download_photo(file_id)
    if not path:
        ui.send("⚠️ Couldn't download that photo — try resending it.")
        return
    uid = pstore.get_or_create_update(conn, batch, active, entry["episode"], entry["pet"])
    pstore.add_media(conn, uid, file_id, path)
    n = len(pstore.get_media(conn, uid))
    log.info("photo attached | %s (%s) | %d so far", entry["pet"], active, n)
    # Light ack on the FIRST photo of a dog only — and only once per album — to avoid spam.
    if n == 1 and not same_group:
        ui.send(f"📷 Collecting for <b>{ui.esc(entry['pet'])}</b>… send more, tap another dog, "
                "or hit Review.")
    pstore.set_last_photo_group(conn, chat_id, media_group_id)


def review(conn, chat_id, cq_id=None):
    """Build one review card per dog (with a pool caption) + the Send-all summary card."""
    batch = pstore.current_batch(conn, chat_id)
    if not batch:
        ui.answer(cq_id, "No photo session — send /photos to start.")
        return
    updates = [u for u in pstore.list_batch(conn, batch, statuses=("collecting", "ready", "held"))
               if pstore.get_media(conn, u[0])]
    if not updates:
        ui.answer(cq_id, "No photos yet — tap a dog and send some.")
        return
    ui.answer(cq_id, "Building review…")
    pstore.set_active_dog(conn, chat_id, "")   # end the collecting phase
    ready = 0
    for u in updates:
        held = render_card(conn, u[0])
        if not held:
            ready += 1
    ui.send("Review each above, edit if needed, then send everything at once:",
            reply_markup=ui.sendall_keyboard(ready, show_capall=ready > 1))


def render_card(conn, update_id, message_id=None):
    """(Re)draw a dog's review card. Picks a pool caption if none set yet. Returns held?."""
    row = pstore.get_update(conn, update_id)
    if not row:
        return False
    _id, _batch, thread, _ep, pet, caption, cap_idx, status, _tid = row
    if status == "discarded":
        return False
    if not caption:                              # first render → pick from the pool
        avoid = pstore.caption_last(conn, thread)
        caption, idx = captions.pick(pet, avoid_index=avoid)
        pstore.set_caption(conn, update_id, caption, idx)
        pstore.set_caption_last(conn, thread, idx)
    held = status == "held"
    if status not in ("held",):                  # collecting/ready → ready for send
        pstore.set_status(conn, update_id, "ready")
    trow = base.get_thread(conn, thread)
    owner = trow[0] if trow else None
    media = pstore.get_media(conn, update_id)
    text = ui.review_card_text(pet, owner, len(media), caption, held=held)
    kb = ui.review_keyboard(update_id, held=held)
    if message_id:                               # re-render (caption/hold change): edit text only
        ui.edit_text(_cfg_chat(), message_id, text, reply_markup=kb)
    else:                                        # first render: show the actual photos, then the card
        ui.send_photos([m[1] for m in media])    # m[1] = telegram_file_id
        mid = ui.send(text, reply_markup=kb)
        if mid:
            pstore.link_card(conn, mid, update_id)
    return held


def _cfg_chat():
    from .. import config
    return config.TELEGRAM_CHAT_ID
