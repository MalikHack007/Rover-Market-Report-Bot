"""Parse Rover notification emails into ParsedMessage.

We rely on the SUBJECT and BODY TEXT (not the From header). Mail arrives direct from
rover@e.rover.com in the dedicated inbox, but keying off body content keeps the parser
robust to any future relay/forwarding too. Confirmed format (real sample):

    Subject: New message from Vatsal about Gypsy's stay
    Body:
        Hi Yujie,
        Vatsal sent you a message about a stay from 08/26/2025 to 08/28/2025.
        Vatsal says:
        <the client's message>
        Reply now
        ...footer...

Booking-*request* emails may use a different layout; when we capture a real
sample we add a variant here. Unrecognized formats are stored and logged so
they can be templated later, never silently dropped.
"""
import base64
import re
from typing import Optional

from bs4 import BeautifulSoup

from .models import ParsedMessage

# Each entry: (compiled subject regex, function mapping match -> {owner, pet?, start?}).
# Patterns are tried in order; first match wins. Add a new tuple here whenever a
# new Rover subject template shows up (unrecognized ones are logged, not dropped).
SUBJECT_PATTERNS = [
    # "New message from Vatsal about Gypsy's stay"
    (re.compile(r"New message from (.+?) about (.+?)['\u2019]s stay", re.IGNORECASE),
     lambda m: {"owner": m.group(1).strip(), "pet": m.group(2).strip()}),
    # "Yisell sent you a new message about a booking starting 07/28/2026"
    (re.compile(r"^(.+?) sent you a (?:new )?message about a booking "
                r"starting (\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
     lambda m: {"owner": m.group(1).strip(), "start": m.group(2)}),
    # defensive: "... sent you a message about a stay starting/from MM/DD/YYYY"
    (re.compile(r"^(.+?) sent you a (?:new )?message about a stay "
                r"(?:starting|from) (\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
     lambda m: {"owner": m.group(1).strip(), "start": m.group(2)}),
]

# A date range in the body, e.g. "stay from 08/26/2025 to 08/28/2025".
DATES_RANGE_RE = re.compile(
    r"(?:stay|booking) from (\d{1,2}/\d{1,2}/\d{4})\s+to\s+(\d{1,2}/\d{1,2}/\d{4})"
)


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n")


def _collect_parts(payload: dict, mime: str) -> str:
    out = []

    def walk(part: dict):
        if part.get("mimeType") == mime:
            data = (part.get("body") or {}).get("data")
            if data:
                out.append(_b64url_decode(data).decode("utf-8", errors="replace"))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(out)


def extract_text_from_payload(payload: dict) -> str:
    """Walk a Gmail message payload; prefer text/plain, fall back to text/html."""
    plain = _collect_parts(payload, "text/plain")
    if plain.strip():
        return plain
    html = _collect_parts(payload, "text/html")
    if html.strip():
        return html_to_text(html)
    return ""


def _normalize(text: str) -> str:
    """Trim each line and drop blank lines so anchor regexes are stable."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_message(body: str, owner: Optional[str]) -> Optional[str]:
    anchors = []
    if owner:
        anchors.append(re.escape(owner) + r"\s+says:")
    anchors.append(r"says:")  # generic fallback
    for a in anchors:
        m = re.search(a + r"\s*(.+?)\s*Reply now", body, re.IGNORECASE | re.DOTALL)
        if m:
            msg = _normalize(m.group(1)).strip()
            if msg:
                return msg
    return None


def parse_notification(subject: str, body_text: str,
                       gmail_msg_id: str, thread_key: str) -> ParsedMessage:
    subject = subject or ""
    body = _normalize(body_text or "")

    owner = pet = start = end = None
    subject_ok = False

    for rx, extract in SUBJECT_PATTERNS:
        m = rx.search(subject)
        if m:
            fields = extract(m)
            owner = fields.get("owner")
            pet = fields.get("pet")
            start = fields.get("start")
            subject_ok = True
            break

    # Supplement with a date range from the body if present (older "stay" emails).
    md = DATES_RANGE_RE.search(body)
    if md:
        start = start or md.group(1)
        end = end or md.group(2)

    message = _extract_message(body, owner)

    return ParsedMessage(
        gmail_msg_id=gmail_msg_id,
        thread_key=thread_key,
        owner_name=owner,
        pet_name=pet,
        stay_start=start,
        stay_end=end,
        message_text=message,
        raw_subject=subject,
        recognized=bool(subject_ok and message),
    )