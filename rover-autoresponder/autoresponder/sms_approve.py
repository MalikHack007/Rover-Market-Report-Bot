"""Addendum A / S4 — approve-and-send.

THE APPROVAL GATE: this module holds the only code path that transmits to a client,
and it runs solely in response to an explicit "Approve & Send" tap. There is no
auto-send anywhere. Guarantees:

  * idempotent   — store.claim_send() reserves (thread, exact text); a double-tap or
                   retry loses the race and is refused, so nothing sends twice.
  * unmistakable — the card names the client the text will go to.
  * confirmed    — gateway delivery events flip the send to sent/delivered/failed,
                   and a failure alerts rather than silently vanishing.
"""
import logging
import re

from . import config, store, telegram_notify as tg
from .sms_gateway import SmsGateForAndroid

log = logging.getLogger(__name__)

_TONE = {
    "regen": None,
    "warm": "Make the reply warmer and friendlier while keeping the same intent.",
    "short": "Make the reply more concise.",
}


def _gateway():
    return SmsGateForAndroid()


def approve_and_send(conn, number: str, chat_id=None, message_id=None,
                     cq_id=None, gateway=None) -> bool:
    """Send the thread's pending text to the client. Returns True if transmitted."""
    text = store.get_pending_text(conn, number)
    if not text:
        tg.answer_callback(cq_id, "Nothing to send — draft is empty")
        return False

    send_key = store.claim_send(conn, number, text)
    if not send_key:
        # Same (thread, text) already claimed → double-tap or retry.
        tg.answer_callback(cq_id, "Already sent — ignoring duplicate")
        log.warning("duplicate send suppressed for %s", number)
        return False

    gw = gateway or _gateway()
    gateway_msg_id = gw.send(number, text, message_id=send_key)
    if not gateway_msg_id:
        store.release_send(conn, send_key)      # let the user retry the same text
        tg.answer_callback(cq_id, "SEND FAILED — not delivered")
        tg.send_alert(f"SMS send FAILED to {number}. Nothing was delivered; retry from "
                      f"the card.")
        log.error("send failed for %s", number)
        return False

    store.update_send(conn, send_key, "sent", gateway_msg_id=gateway_msg_id)
    store.record_outbound(conn, number, text, gateway_msg_id=gateway_msg_id)
    store.mark_thread_sent(conn, number, "sent")

    # Advance the stage now that the reply is actually out.
    owner, pet, dates, stage, status = store.get_thread(conn, number)
    store.update_thread_stage(conn, number, advance_stage(stage))

    if chat_id and message_id:
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)  # buttons done
    tg.answer_callback(cq_id, "Sent ✅")
    log.info("SENT to %s (%s): %r", number, gateway_msg_id, text[:80])
    return True


_STAGE_ORDER = ["S0_INITIAL", "S1_CONSENT", "S2_ANSWERS", "S3_POST_SCREEN"]


def advance_stage(stage: str) -> str:
    try:
        i = _STAGE_ORDER.index(stage)
    except ValueError:
        return "S0_INITIAL"
    return _STAGE_ORDER[min(i + 1, len(_STAGE_ORDER) - 1)]


def apply_edit(conn, number: str, new_text: str, chat_id=None, card_message_id=None) -> None:
    """Replace the pending text with the user's edited wording and re-show the card."""
    store.set_pending_text(conn, number, new_text)
    owner, pet, dates, stage, status = store.get_thread(conn, number)
    history = store.get_conversation(conn, number)
    card = tg.format_draft_card(owner, dates, stage, ["edited by you"], history, new_text)
    if chat_id and card_message_id:
        tg.edit_message_text(chat_id, card_message_id, card,
                             reply_markup=tg.build_sms_keyboard(number))
    else:
        mid = tg.send_message(card, reply_markup=tg.build_sms_keyboard(number))
        store.link_card(conn, mid, number)
    log.info("draft edited for %s: %r", number, new_text[:80])


def redraft(conn, number: str, action: str, chat_id=None, message_id=None,
            cq_id=None) -> None:
    """Regenerate / Warmer / Shorter — re-draft, then re-show for approval."""
    from .drafter import draft_reply

    owner, pet, dates, stage, status = store.get_thread(conn, number)
    history = store.get_conversation(conn, number)
    try:
        d = draft_reply(owner, pet, dates, stage, history,
                        extra_instruction=_TONE.get(action))
    except Exception:
        log.exception("re-draft failed for %s", number)
        tg.answer_callback(cq_id, "Re-draft failed — try again")
        return
    store.set_pending_text(conn, number, d.draft_text)
    store.set_last_draft(conn, number, d.draft_text)
    card = tg.format_draft_card(owner, dates, d.stage, d.flags, history, d.draft_text)
    if chat_id and message_id:
        tg.edit_message_text(chat_id, message_id, card,
                             reply_markup=tg.build_sms_keyboard(number))
    tg.answer_callback(cq_id, "Updated")


def handle_callback(conn, data: str, chat_id, message_id, cq_id) -> None:
    """Route an inline-button tap. data == '<action>:<thread_key>'."""
    action, _, number = data.partition(":")
    if not store.get_thread(conn, number):
        tg.answer_callback(cq_id, "Thread not found")
        return

    if action == "send":
        approve_and_send(conn, number, chat_id, message_id, cq_id)
    elif action == "edit":
        store.link_card(conn, message_id, number)
        tg.answer_callback(cq_id, "Reply to this card with your version")
        tg.send_message("✏️ Reply to the draft card with your edited text, then tap "
                        "<b>Approve &amp; Send</b>.")
    elif action in _TONE:
        redraft(conn, number, action, chat_id, message_id, cq_id)
    elif action == "conv":
        store.set_thread_status(conn, number, "converted")
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)
        tg.answer_callback(cq_id, "Converted — drafting stopped")
    elif action == "unfit":
        store.set_thread_status(conn, number, "not_suitable")
        tg.edit_reply_markup(chat_id, message_id, reply_markup=None)
        tg.answer_callback(cq_id, "Marked not suitable — drafting stopped")
    else:
        tg.answer_callback(cq_id, "Unknown action")


def handle_text_reply(conn, text: str, chat_id, reply_to_message_id) -> bool:
    """A plain Telegram message replying to a card.

    "/pet Maple" or "/owner Daniel" sets a name manually (name-recovery layer 4);
    anything else is treated as an edit of the draft.
    """
    if not reply_to_message_id:
        return False
    number = store.thread_for_card(conn, reply_to_message_id)
    if not number:
        return False

    m = re.match(r"^\s*/(pet|owner)\s+(.+)$", text or "", re.IGNORECASE)
    if m:
        from .identity import set_manual
        field, value = m.group(1).lower(), m.group(2).strip()
        if set_manual(conn, number, field, value):
            tg.send_message(f"✅ {field.capitalize()} name set to <b>{tg._esc(value)}</b>. "
                            f"Tap 🔁 Regenerate to redraft with it.")
        return True

    apply_edit(conn, number, text, chat_id, reply_to_message_id)
    return True


def handle_delivery_event(conn, event: str, payload: dict) -> None:
    """Gateway sms:sent / sms:delivered / sms:failed → close the loop on a send."""
    gateway_msg_id = payload.get("messageId") or payload.get("id")
    row = store.send_by_gateway_id(conn, gateway_msg_id) if gateway_msg_id else None
    if not row:
        return
    send_key, number, _status = row
    if event == "sms:delivered":
        store.update_send(conn, send_key, "delivered")
        log.info("DELIVERED to %s", number)
    elif event == "sms:failed":
        store.update_send(conn, send_key, "failed")
        store.mark_thread_sent(conn, number, "failed")
        reason = payload.get("reason") or "unknown"
        log.error("DELIVERY FAILED to %s: %s", number, reason)
        tg.send_alert(f"SMS to {number} FAILED to deliver ({reason}). "
                      "The client did not receive it.")