"""Addendum A / S5 — truncation recovery via the email fallback.

SMS truncates long client messages with a "(more at https://r.rover.com/...)" tail, and
that link is cookie-gated (unusable from automation). Email is NOT truncated, so we
recover the full text from the Gmail pipeline that already exists.

Correlation is ONE-TIME per thread: SMS is keyed by the client's number, email by the
Gmail thread id. We match on owner name (+ pet when available), then store the binding —
because the SMS number is stable for the client's whole life, the mapping holds for every
later message on that thread.

Matching is deliberately conservative: an ambiguous match is left unresolved and flagged
rather than guessed, since stitching the wrong client's message into a thread would be
worse than leaving it truncated.
"""
import logging
import re

from . import store

log = logging.getLogger(__name__)

# The "... (more at https://r.rover.com/XXXX )" tail SMS appends when it truncates.
TRUNCATION_TAIL_RE = re.compile(r"\s*\(\s*more at\s+https?://\S+\s*\)\s*$", re.IGNORECASE)
# Trailing ellipsis/word fragment left by the cut, e.g. "... He..."
FRAGMENT_TAIL_RE = re.compile(r"(?:\s*\S*)?\.{2,}\s*$")


def strip_truncation_tail(text: str) -> str:
    """The SMS body minus the '(more at ...)' marker and the trailing fragment."""
    t = TRUNCATION_TAIL_RE.sub("", text or "").strip()
    return FRAGMENT_TAIL_RE.sub("", t).strip()


def prefix_for_match(text: str, chars: int = 60) -> str:
    """A normalized leading slice, used to match the SMS against an email body."""
    head = strip_truncation_tail(text)[:chars]
    return re.sub(r"\s+", " ", head).strip().lower()


def _norm_name(name: str) -> str:
    """'Brenna D.' -> 'brenna'  (Rover shows surnames inconsistently across channels)."""
    if not name:
        return ""
    first = re.split(r"\s+", name.strip())[0]
    return re.sub(r"[^a-z]", "", first.lower())


def find_email_thread(conn, owner_name: str, pet_name: str = None):
    """Find the Gmail thread for this client. Returns thread_key, or None if unsure.

    Only email threads (those with a gmail_msg_id) are considered — SMS threads live
    in the same table keyed by phone number.
    """
    if not owner_name:
        return None
    target = _norm_name(owner_name)
    if not target:
        return None

    rows = store.list_email_threads(conn)
    matches = [r for r in rows if _norm_name(r[1]) == target]
    if pet_name and len(matches) > 1:
        pet = _norm_name(pet_name)
        narrowed = [r for r in matches if _norm_name(r[2]) == pet]
        if narrowed:
            matches = narrowed
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        log.warning("email correlation ambiguous for %s (%d candidates) — not guessing",
                    owner_name, len(matches))
    return None


def recover_full_text(conn, number: str, truncated_text: str):
    """Return the full version of a truncated SMS, or None if unavailable.

    Strategy is CONTENT-FIRST: find an email message whose normalized text starts with
    the truncated message's prefix. Matching text is near-conclusive evidence — much
    stronger than a name match — and it solves two real cases that name correlation
    can't:
      * the same client has SEVERAL email threads (ambiguous by name, and Rover often
        omits the pet name from the email thread, so it can't be narrowed), and
      * one conversation's messages are SPLIT across those threads, so no single
        "correct" thread contains everything.

    The bound email thread (if any) is searched first as an optimization, then all
    email threads. Name correlation is still recorded when it succeeds, purely as a hint.
    """
    prefix = prefix_for_match(truncated_text)
    if not prefix:
        return None

    def _scan(thread_key):
        for text in store.get_thread_messages(conn, thread_key):
            if not text:
                continue
            # Email hard-wraps lines where SMS doesn't, so compare whitespace-collapsed.
            norm = re.sub(r"\s+", " ", text).strip().lower()
            if norm.startswith(prefix) and len(text) > len(truncated_text):
                return text
        return None

    bound = store.get_email_thread_key(conn, number)
    if bound:
        hit = _scan(bound)
        if hit:
            return hit

    for thread_key, _owner, _pet in store.list_email_threads(conn):
        if thread_key == bound:
            continue
        hit = _scan(thread_key)
        if hit:
            if not bound:
                store.bind_email_thread(conn, number, thread_key)
                log.info("correlated %s -> email thread %s (by content)",
                         number, thread_key)
            return hit
    return None


def resolve_truncated(conn, number: str) -> int:
    """Replace any truncated messages on this thread with their full email versions.

    Returns how many were recovered. Safe to call repeatedly; already-resolved rows
    are skipped.
    """
    pending = store.list_truncated(conn, number)
    recovered = 0
    for msg_id, text in pending:
        full = recover_full_text(conn, number, text)
        if full:
            store.replace_message_text(conn, msg_id, full)
            recovered += 1
            log.info("recovered full text for %s (%d -> %d chars)",
                     number, len(text), len(full))
    if pending and not recovered:
        log.warning("could not recover %d truncated message(s) on %s — "
                    "drafting from the truncated text", len(pending), number)
    return recovered