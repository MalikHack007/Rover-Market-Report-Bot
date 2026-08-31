"""Addendum A / S2 — route parsed SMS through the thread state machine.

Thread identity = the conversation number (stable for the request's whole life:
pending -> confirmed -> complete). State transitions, per real samples:

    inquiry marker   -> ACTIVE   (a new client request; draft it, S3 wires the brain)
    confirmed marker -> CONVERTED (booked; stop drafting)
    modified marker  -> CONVERTED (already-booked stay changed; not our business)
    ordinary message -> no status change; drafted only if the thread is ACTIVE

A number we've never seen whose first message carries NO inquiry marker is left
UNKNOWN and not drafted — conservative by design (it's mid-conversation or a
confirmed booking that predates the bot).
"""
import logging

from . import config, store
from .sms_parser import parse_sms

log = logging.getLogger(__name__)


def handle_sms(conn, sender: str, body: str, schedule_draft=None):
    """Parse, persist, and apply the state machine. Returns the SmsMessage."""
    msg = parse_sms(sender, body)
    thread = store.get_thread(conn, sender)
    known = thread is not None

    if msg.kind == "inquiry":
        # Rover reuses a number per CLIENT across bookings, so an inquiry marker on a
        # thread we've seen before means the SAME client is booking AGAIN. Start a new
        # episode: reactivate (even from 'converted'), reset the stage, and scope the
        # drafter's context to this request only — not a conversation from a year ago.
        #
        # EXCEPTION: the booking block usually arrives seconds BEFORE the marker and has
        # already opened this episode. Bumping again would burn an episode and strand the
        # block's context. Only start a new episode if the current one is actually in use
        # (we've replied) or has gone terminal.
        status = _status(conn, sender)
        fresh = (known and status in ("active", "pending")
                 and not store.episode_has_outbound(conn, sender))
        if fresh:
            episode = store.get_episode(conn, sender)
            store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name,
                                    pet_name=msg.pet_name, stay_dates=_dates(msg),
                                    status="active")
            log.info("NEW INQUIRY (sms) | %s | owner=%s pet=%s %s | service=%s",
                     sender, msg.owner_name, msg.pet_name, _dates(msg), msg.service)
        else:
            episode = store.start_new_episode(
                conn, sender, owner_name=msg.owner_name, pet_name=msg.pet_name,
                stay_dates=_dates(msg))
            if known:
                log.info("RETURNING CLIENT (sms) | %s | new inquiry, episode %d | "
                         "owner=%s pet=%s %s", sender, episode, msg.owner_name,
                         msg.pet_name, _dates(msg))
            else:
                log.info("NEW INQUIRY (sms) | %s | owner=%s pet=%s %s | service=%s",
                         sender, msg.owner_name, msg.pet_name, _dates(msg), msg.service)
        store.record_sms(conn, sender, msg)
        # The structured booking block usually lands seconds BEFORE the marker; pull it
        # into this episode so its drop-off/pick-up details aren't stranded.
        moved = store.promote_recent_to_episode(conn, sender, episode)
        if moved:
            log.info("  pulled %d recent message(s) into episode %d", moved, episode)
        if msg.truncated:
            log.warning("  truncated SMS on %s — full text needs email fallback (S5)",
                        sender)
        return msg

    elif msg.kind == "awaiting_accept":
        # The client paid and is waiting on you to accept in the Rover app. Don't draft
        # a reply — surface it as an action item. Accepting yields only a confirmation
        # EMAIL (no SMS), which confirmation_email.py picks up.
        store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name,
                                pet_name=msg.pet_name)
        store.record_sms(conn, sender, msg)
        log.info("AWAITING YOUR ACCEPTANCE (sms) | %s | owner=%s pet=%s",
                 sender, msg.owner_name, msg.pet_name)
        try:
            from . import telegram_notify
            telegram_notify.send_alert(
                f"💳 {msg.owner_name or 'A client'} paid and is waiting for you to "
                f"ACCEPT {msg.pet_name or 'their booking'} in the Rover app. "
                "The calendar will update once the confirmation email arrives.")
        except Exception:
            log.exception("alert failed")
        return msg

    elif msg.kind == "cancelled":
        # Addendum B / C4: Rover cancelled the whole booking. It arrives on the booking's own
        # thread, so the thread_key tells us which one — remove its calendar events + links,
        # and mark the thread cancelled so the dog drops out of the photo roster / drafting.
        store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name)
        store.set_thread_status(conn, sender, "cancelled")
        log.info("CANCELLED (sms) | %s | owner=%s | %s to %s",
                 sender, msg.owner_name, msg.start_date, msg.end_date)
        if config.GOOGLE_CALENDAR_ID:
            try:
                from .scheduling import on_booking_cancelled
                removed = on_booking_cancelled(conn, sender)
                from . import telegram_notify
                telegram_notify.send_alert(
                    f"🚫 Booking CANCELLED — {msg.owner_name or 'a client'} "
                    f"({msg.start_date} to {msg.end_date}). Removed {removed} calendar "
                    "event(s); links expired.")
            except Exception:
                log.exception("  cancellation handling failed for %s", sender)

    elif msg.kind in ("confirmed", "modified"):
        store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name,
                                pet_name=msg.pet_name, status="converted")
        if msg.kind == "confirmed":
            # They actually booked — a future request skips screening entirely.
            store.mark_has_booked(conn, sender)
            # Addendum B / C1: place PENDING drop-off + pick-up on the ROVER calendar.
            if config.GOOGLE_CALENDAR_ID:
                try:
                    from .scheduling import on_booking_confirmed
                    row = store.get_thread(conn, sender)
                    pet = msg.pet_name or (row[1] if row else None)
                    on_booking_confirmed(conn, sender, pet,
                                         msg.start_date, msg.end_date)
                    # C2: draft the message carrying the scheduling links.
                    send_scheduling_links(conn, sender)
                except Exception:
                    log.exception("  calendar placement failed for %s", sender)
        log.info("%s (sms) | %s | owner=%s -> converted, no action",
                 msg.kind.upper(), sender, msg.owner_name)

    else:  # ordinary client message
        if msg.is_booking_block and (not known or _status(conn, sender) != "active"):
            # Rover only auto-sends "Boarding Request - One Time: Drop-off... Pick-up..."
            # when a client submits a booking request, so the block ALONE is proof of a
            # new inquiry. Open it immediately rather than waiting for the marker —
            # the marker is a nice-to-have (owner/pet/service) that sometimes never
            # arrives, and waiting for it silently stranded real inquiries.
            # This also covers a RETURNING client whose block precedes the marker.
            episode = store.start_new_episode(conn, sender)
            store.record_sms(conn, sender, msg)
            log.info("NEW INQUIRY (sms, booking block) | %s | episode %d | "
                     "awaiting marker for owner/pet details", sender, episode)
            if schedule_draft:
                schedule_draft(sender)
            return msg
        if not known:
            # Some other first message from an unseen number — stay out of it.
            store.upsert_sms_thread(conn, sender, status="unknown")
            log.info("new sms thread | %s | no booking request seen; not drafting",
                     sender)
        status = (store.get_thread(conn, sender) or [None] * 5)[4]
        if status in ("active", "pending"):     # 'pending' = legacy rows, treat as active
            log.info("CLIENT MSG (sms) | %s | %r%s", sender, msg.text[:120],
                     " [TRUNCATED]" if msg.truncated else "")
            if schedule_draft:
                schedule_draft(sender)     # S3 wires this to the drafter
        else:
            log.info("message on %s thread (sms) | %s | ignored", status, sender)

    store.record_sms(conn, sender, msg)
    if msg.truncated:
        # S5: flag it; recovery is attempted at draft time (the email notification
        # may not have arrived yet when the SMS lands).
        store.mark_truncated(conn, sender, msg.text)
        log.warning("  truncated SMS on %s — will try email recovery before drafting",
                    sender)
    return msg


def _status(conn, number: str):
    row = store.get_thread(conn, number)
    return row[4] if row else None


def _dates(msg) -> str:
    if msg.start_date and msg.end_date:
        return f"{msg.start_date} to {msg.end_date}"
    return msg.start_date or ""


# --- Addendum A / S3: wire the brain (drafter + playbook/FAQ + Telegram) ---
def draft_for_thread(conn, number: str) -> None:
    """Draft the next reply for an active SMS inquiry thread and push it to Telegram.

    Called by the debouncer once the thread has been quiet, so a burst of messages
    (booking block + "will you be available" + a later afterthought) produces ONE
    informed draft. S3 = draft + display only; sending is S4.
    """
    from . import config, store, telegram_notify
    from .drafter import should_draft, draft_reply

    if not config.ANTHROPIC_API_KEY:
        log.info("  (draft skipped: ANTHROPIC_API_KEY not set)")
        return
    row = store.get_thread(conn, number)
    if not row:
        return
    owner, pet, dates, stage, status = row
    if status not in ("active", "pending") or not should_draft(status):
        log.info("  (thread %s is %s; not drafting)", number, status)
        return

    # Name recovery layer 2: the inquiry marker doesn't always arrive (~95%), but the
    # owner's name is ALWAYS in the email subject. Correlate by content and lift it.
    if not owner or not pet:
        try:
            from .identity import recover_names_from_email
            owner, pet = recover_names_from_email(conn, number)
        except Exception:
            log.exception("  name recovery from email failed for %s", number)

    # SMS mirrors the whole thread, so the drafter sees BOTH sides (labelled
    # Client/You) — better stage inference than the client-only email view.
    # S5: recover any truncated messages from the correlated email thread first, so
    # the drafter reads the client's FULL words (questionnaire answers get cut most).
    try:
        from .truncation import resolve_truncated
        resolve_truncated(conn, number)
    except Exception:
        log.exception("  truncation recovery failed for %s (drafting anyway)", number)

    history = store.get_conversation(conn, number)
    if not history:
        log.info("  (no client messages yet on %s; nothing to draft)", number)
        return
    if not any(sp == "Client" for sp, _ in history):
        log.info("  (nothing from the client yet on %s; not drafting)", number)
        return

    # --- Returning client short-circuit ---
    # They've booked before, and this is the first reply of a NEW request: skip the
    # whole screening playbook (they've already been through it) and use the template.
    # No LLM call needed — the wording is fixed. Later messages in this episode fall
    # through to normal drafting so questions still get real answers.
    if store.has_booked(conn, number) and not store.episode_has_outbound(conn, number):
        text = config.RETURNING_CLIENT_TEMPLATE.format(
            owner_name=owner or "there", pet_name=pet or "your pup")
        store.set_last_draft(conn, number, text)
        store.set_pending_text(conn, number, text)
        # Screening is done for this client; treat them as post-screen.
        store.update_thread_stage(conn, number, "S3_POST_SCREEN")
        log.info("  RETURNING CLIENT draft (no API call) | %s\n----- draft -----\n%s\n"
                 "-----------------", number, text)
        mid = telegram_notify.send_draft_card(
            owner, dates, "S3_POST_SCREEN", ["returning client — screening skipped"],
            history, text,
            reply_markup=telegram_notify.build_sms_keyboard(number))
        store.link_card(conn, mid, number)
        return

    try:
        d = draft_reply(owner, pet, dates, stage, history)
    except Exception:
        log.exception("  draft failed for %s", number)
        return

    store.update_thread_stage(conn, number, d.stage)

    # Name recovery layer 3: the drafter reports a pet name it inferred from the
    # client's own message (costs no extra API call).
    if d.inferred_pet and not pet:
        try:
            from .identity import apply_inferred_pet
            pet = apply_inferred_pet(conn, number, d.inferred_pet) or pet
        except Exception:
            log.exception("  storing inferred pet name failed for %s", number)

    # Off-playbook no longer means "no draft". The model always drafts a safe,
    # non-committal attempt; we just flag the card so you read it carefully. You can
    # then edit it in Telegram and send — no need to go handle it elsewhere.
    if d.off_playbook:
        log.warning("  OFF-PLAYBOOK [%s] flags=%s — drafted for review", d.stage, d.flags)
    if not d.draft_text:
        # Defensive: an older prompt (or a stubborn model) returned nothing.
        log.warning("  empty draft on %s — sending attention card instead", number)
        telegram_notify.send_offplaybook_card(owner, d.flags, history)
        return

    store.set_last_draft(conn, number, d.draft_text)
    # S4: this is what "Approve & Send" will transmit (until you edit it).
    store.set_pending_text(conn, number, d.draft_text)
    # S5: if a truncated message never got recovered, say so on the card — the draft
    # is based on a partial client message and deserves a closer read.
    flags = list(d.flags)
    if store.list_truncated(conn, number):
        flags.append("⚠️ a client message was cut off by SMS and couldn't be recovered "
                     "— check the full message on Rover")
    # Name recovery layer 4: nothing found it — tell you how to set it by hand.
    if not pet:
        flags.append("pet name unknown — reply “/pet Maple” to set it")
    if not owner:
        flags.append("owner name unknown — reply “/owner Daniel” to set it")
    log.info("  DRAFT [%s]%s (from %d msg)\n----- draft -----\n%s\n-----------------",
             d.stage, f" flags={flags}" if flags else "", len(history), d.draft_text)
    # S4: card carries Approve & Send / Edit / tone / terminal buttons. Link the card
    # to the thread so replying to it edits this draft.
    mid = telegram_notify.send_draft_card(
        owner, dates, d.stage, flags, history, d.draft_text,
        needs_review=d.off_playbook,
        reply_markup=telegram_notify.build_sms_keyboard(number))
    store.link_card(conn, mid, number)


# --- Addendum B / C2: deliver the scheduling links for approval ----------
def send_scheduling_links(conn, number: str) -> bool:
    """Compose the scheduling-links message and push it to Telegram for approval.

    Fixed template, so no LLM call. It goes through the normal approve-and-send flow —
    you review it, can edit it, and only then does it reach the client.

    Note the thread is 'converted' by this point (screening is over); this is the one
    message we still want to send on a converted thread, so it bypasses the drafter
    rather than going through draft_for_thread().
    """
    from . import telegram_notify
    from .scheduling import build_scheduling_draft

    # A normal booking fires BOTH signals (SMS marker + confirmation email). The calendar
    # events are deduped by their unique constraint, but this card is not — guard it so
    # you don't get the links twice.
    episode = store.get_episode(conn, number)
    sent_key = f"links_sent:{number}:{episode}"
    if store.meta_exists(conn, sent_key):
        log.info("  scheduling links already sent for %s (episode %d) — not resending",
                 number, episode)
        return False

    text, links = build_scheduling_draft(conn, number)
    if not text:
        log.warning("  no scheduling links for %s — skipping link message", number)
        return False
    store.set_meta(conn, sent_key, "1")

    row = store.get_thread(conn, number)
    owner, pet, dates = (row[0], row[1], row[2]) if row else (None, None, None)
    store.set_pending_text(conn, number, text)
    store.set_last_draft(conn, number, text)
    history = store.get_conversation(conn, number)
    log.info("  SCHEDULING LINKS drafted for %s\n----- draft -----\n%s\n-----------------",
             number, text)
    mid = telegram_notify.send_draft_card(
        owner, dates, "SCHEDULING", ["booking confirmed — send the booking links"],
        history, text,
        reply_markup=telegram_notify.build_sms_keyboard(number))
    store.link_card(conn, mid, number)
    return True


# --- Addendum B / C4: re-issue ONLY the changed leg(s)' link after a modification --------
def send_modified_links(conn, number: str, kinds) -> bool:
    """Draft a links message for just the moved legs (e.g. only pick-up) and push it for
    approval. Unlike send_scheduling_links this is NOT deduped (a modification should always
    re-issue) and it never re-sends an unchanged leg's link (which could double-book)."""
    from . import telegram_notify
    from .scheduling import build_leg_links_message

    text, links = build_leg_links_message(conn, number, kinds)
    if not text:
        log.warning("  no links to re-issue for %s (kinds=%s)", number, list(kinds))
        return False
    row = store.get_thread(conn, number)
    owner, pet, dates = (row[0], row[1], row[2]) if row else (None, None, None)
    store.set_pending_text(conn, number, text)
    store.set_last_draft(conn, number, text)
    history = store.get_conversation(conn, number)
    log.info("  MODIFIED LINKS (%s) drafted for %s\n----- draft -----\n%s\n---------------",
             list(links), number, text)
    mid = telegram_notify.send_draft_card(
        owner, dates, "SCHEDULING", [f"booking modified — re-issue {', '.join(links)} link(s)"],
        history, text, reply_markup=telegram_notify.build_sms_keyboard(number))
    store.link_card(conn, mid, number)
    return True