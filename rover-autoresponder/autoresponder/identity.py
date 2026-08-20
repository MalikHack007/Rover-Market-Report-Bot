"""Owner / pet name recovery.

The inquiry marker carries owner+pet, but it only arrives ~95% of the time. When it
doesn't, names are recovered in layers, cheapest and most reliable first:

  1. MARKER            — "[ New booking request (boarding) from Charlotte: Royal ... ]"
  2. EMAIL SUBJECT     — the owner's name is ALWAYS in the email subject
                         ("Destiny sent you a new message about a booking starting ...").
                         The booking block text appears in both SMS and email, so we
                         content-match it to find the client's email thread, then lift
                         owner_name (and pet_name if the email side has one).
  3. LLM INFERENCE     — the pet's name is usually in the client's own message
                         ("...take great care of Maple while I'm away"). The drafter
                         returns it as a field, so this costs no extra API call.
  4. MANUAL            — you set it from Telegram: reply to a card with
                         "/pet Maple" or "/owner Daniel".

Anything still unknown degrades gracefully: the playbook falls back to "your pup".
"""
import logging
import re

from . import store

log = logging.getLogger(__name__)

# Enough of the booking block to identify the conversation, ignoring whitespace/wrapping.
_BLOCK_PREFIX_CHARS = 70


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def find_email_thread_by_content(conn, number: str):
    """Correlate this SMS thread to its email thread by matching message text.

    Uses the client's messages (including the auto-sent booking block, which appears
    verbatim in both channels) rather than names — names are exactly what we're missing.
    """
    sms_msgs = [t for t in store.get_thread_messages(conn, number) if t]
    if not sms_msgs:
        return None
    prefixes = [_norm(t)[:_BLOCK_PREFIX_CHARS] for t in sms_msgs]
    prefixes = [p for p in prefixes if len(p) >= 25]      # too-short prefixes overmatch
    if not prefixes:
        return None

    for thread_key, _owner, _pet in store.list_email_threads(conn):
        for text in store.get_thread_messages(conn, thread_key):
            norm = _norm(text)
            for p in prefixes:
                if norm.startswith(p) or p.startswith(norm[:len(p)]) and len(norm) >= 25:
                    return thread_key
    return None


def recover_names_from_email(conn, number: str):
    """Layer 2: pull owner (and pet, if present) from the correlated email thread.

    Returns (owner_name, pet_name) — either may be None. Persists what it finds.
    """
    row = store.get_thread(conn, number)
    if not row:
        return None, None
    owner, pet = row[0], row[1]
    if owner and pet:
        return owner, pet                                  # nothing to do

    email_thread = store.get_email_thread_key(conn, number)
    if not email_thread:
        email_thread = find_email_thread_by_content(conn, number)
        if not email_thread:
            return owner, pet
        store.bind_email_thread(conn, number, email_thread)
        log.info("correlated %s -> email thread %s (for names)", number, email_thread)

    e_row = store.get_thread(conn, email_thread)
    if not e_row:
        return owner, pet
    e_owner, e_pet = e_row[0], e_row[1]
    new_owner = owner or e_owner
    new_pet = pet or e_pet
    if (new_owner, new_pet) != (owner, pet):
        store.upsert_sms_thread(conn, number, owner_name=new_owner, pet_name=new_pet)
        log.info("recovered names from email for %s | owner=%s pet=%s",
                 number, new_owner, new_pet)
    return new_owner, new_pet


def apply_inferred_pet(conn, number: str, inferred: str):
    """Layer 3: store a pet name the drafter inferred from the client's message."""
    if not inferred:
        return None
    name = inferred.strip().strip(".,!?\"'")
    # Guard against the model echoing a placeholder instead of a real name.
    if not name or len(name) > 40 or name.lower() in {
            "unknown", "your pup", "the dog", "n/a", "none", "pup", "dog"}:
        return None
    row = store.get_thread(conn, number)
    if row and row[1]:
        return row[1]                                      # already known; don't override
    store.upsert_sms_thread(conn, number, pet_name=name)
    log.info("inferred pet name for %s from the client's message: %r", number, name)
    return name


def set_manual(conn, number: str, field: str, value: str) -> bool:
    """Layer 4: manual override from Telegram ('/pet Maple', '/owner Daniel')."""
    value = (value or "").strip()
    if not value:
        return False
    if field == "pet":
        store.upsert_sms_thread(conn, number, pet_name=value)
    elif field == "owner":
        store.upsert_sms_thread(conn, number, owner_name=value)
    else:
        return False
    log.info("manually set %s=%r for %s", field, value, number)
    return True
