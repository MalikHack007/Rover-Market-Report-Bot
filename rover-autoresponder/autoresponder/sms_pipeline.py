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

from . import store
from .sms_parser import parse_sms

log = logging.getLogger(__name__)


def handle_sms(conn, sender: str, body: str, schedule_draft=None):
    """Parse, persist, and apply the state machine. Returns the SmsMessage."""
    msg = parse_sms(sender, body)
    thread = store.get_thread(conn, sender)
    known = thread is not None

    if msg.kind == "inquiry":
        store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name,
                                pet_name=msg.pet_name, stay_dates=_dates(msg),
                                status="active")
        log.info("NEW INQUIRY (sms) | %s | owner=%s pet=%s %s | service=%s",
                 sender, msg.owner_name, msg.pet_name, _dates(msg), msg.service)

    elif msg.kind in ("confirmed", "modified"):
        store.upsert_sms_thread(conn, sender, owner_name=msg.owner_name,
                                pet_name=msg.pet_name, status="converted")
        log.info("%s (sms) | %s | owner=%s -> converted, no action",
                 msg.kind.upper(), sender, msg.owner_name)

    else:  # ordinary client message
        if not known:
            # The auto-sent "Boarding Request - One Time:" block often arrives just
            # BEFORE the inquiry marker. Treat it as a provisional inquiry opener so
            # the opening burst isn't stranded in 'unknown'; a marker landing next
            # promotes it to active. Anything else from an unseen number stays out.
            initial = "pending" if msg.is_booking_block else "unknown"
            store.upsert_sms_thread(conn, sender, status=initial)
            log.info("new sms thread | %s | %s", sender,
                     "booking block seen; awaiting inquiry marker"
                     if msg.is_booking_block else
                     "no inquiry marker seen; not drafting")
        status = (store.get_thread(conn, sender) or [None] * 5)[4]
        if status == "active":
            log.info("CLIENT MSG (sms) | %s | %r%s", sender, msg.text[:120],
                     " [TRUNCATED]" if msg.truncated else "")
            if schedule_draft:
                schedule_draft(sender)     # S3 wires this to the drafter
        elif status == "pending":
            log.info("pending thread (sms) | %s | holding until inquiry marker", sender)
        else:
            log.info("message on %s thread (sms) | %s | ignored", status, sender)

    store.record_sms(conn, sender, msg)
    if msg.truncated:
        log.warning("  truncated SMS on %s — full text needs email fallback (S5)", sender)
    return msg


def _dates(msg) -> str:
    if msg.start_date and msg.end_date:
        return f"{msg.start_date} to {msg.end_date}"
    return msg.start_date or ""
