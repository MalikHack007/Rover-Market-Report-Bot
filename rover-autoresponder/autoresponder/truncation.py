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


# Clients answer the questionnaire by quoting it back, so the first ~124 characters are
# OUR boilerplate ("1. Where are you in your sitter search? ...") and identical across
# every client. Matching on a short prefix therefore matched the wrong client's email.
# We now require the WHOLE truncated body to match, which is client-specific.
MIN_PREFIX_CHARS = 80


# Rover's SMS channel downgrades typography to ASCII (hyphen, straight quotes) while the
# email keeps the client's original "smart" punctuation (en/em dash, curly quotes, ellipsis).
# Fold those to ASCII before matching — otherwise a single en-dash breaks startswith(). Real
# case: Erin/Dakota, SMS "Sep 4 - 6" vs email "Sep 4 – 6" (U+002D vs U+2013).
_FOLD = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
         0x2013: "-", 0x2014: "-", 0x2026: "...", 0x00A0: " "}


def _normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased, punctuation-folded — for content matching."""
    return re.sub(r"\s+", " ", (text or "").translate(_FOLD)).strip().lower()


def prefix_for_match(text: str, chars: int = None) -> str:
    """Normalized text used to match an SMS against an email body.

    Defaults to the ENTIRE truncated message (minus the '(more at …)' tail and the
    cut-off word). A short prefix is not discriminative — see above.
    """
    head = strip_truncation_tail(text)
    if chars:
        head = head[:chars]
    return _normalize(head)


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
    if not prefix or len(prefix) < MIN_PREFIX_CHARS:
        # Too little to identify a client safely — leave it truncated and flagged.
        log.info("truncated text on %s is too short to match safely (%d chars)",
                 number, len(prefix))
        return None

    def _scan(thread_key):
        for text in store.get_thread_messages(conn, thread_key):
            if not text:
                continue
            # Email hard-wraps lines where SMS doesn't, so compare whitespace-collapsed.
            norm = _normalize(text)
            if norm.startswith(prefix) and len(text) > len(truncated_text):
                return text
        return None

    bound = store.get_email_thread_key(conn, number)
    if bound:
        hit = _scan(bound)
        if hit:
            return hit

    sms_owner = _norm_name((store.get_thread(conn, number) or [None])[0])
    for thread_key, e_owner, _pet in store.list_email_threads(conn):
        if thread_key == bound:
            continue
        # Second guard: if both sides name an owner and they disagree, this is a
        # different client — never stitch their message into this thread.
        if sms_owner and e_owner and _norm_name(e_owner) != sms_owner:
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