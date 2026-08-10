"""Addendum A / S2 — parse Rover SMS bodies.

SMS has no subject line, so classification runs on machine-generated markers Rover
injects into the thread (all seen in real samples):

  INQUIRY   [ New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs)
              08/21/2026 to 08/23/2026. Book @ r.rover.com/8C48qS ]
  CONFIRMED [ Brenna D. has confirmed a booking request (stay) with Alfie
              from 08/13 to 08/14 - View on Rover r.rover.com/VzXwna ]
  MODIFIED  [ Your upcoming booking with Joshua L. has been modified.
              Tap to review booking details. Review changes @ r.rover.com/Acm5dN ]

Ordinary client messages carry NO marker. Long messages are truncated by SMS and end
with a "(more at https://r.rover.com/...)" tail — the link is cookie-gated, so full
text recovery goes through the email pipeline (S5).
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

# --- markers -------------------------------------------------------------
INQUIRY_RE = re.compile(
    r"New booking request\s*\(([^)]+)\)\s*from\s+([^:]+):\s*"      # service, owner
    r"(.+?)\s+"                                                     # pet blob
    r"(\d{1,2}/\d{1,2}/\d{4})\s+to\s+(\d{1,2}/\d{1,2}/\d{4})",      # dates
    re.IGNORECASE | re.DOTALL,
)
# Day-care / single-date variant: "... from Hyejin: Daisy (2 yr, 15 lbs) 07/28/2026."
INQUIRY_SINGLE_RE = re.compile(
    r"New booking request\s*\(([^)]+)\)\s*from\s+([^:]+):\s*"
    r"(.+?)\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE | re.DOTALL,
)
CONFIRMED_RE = re.compile(
    r"(.+?)\s+has confirmed a booking request\s*(?:\(([^)]+)\))?\s*"
    r"with\s+(.+?)\s+from\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+to\s+"
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
    re.IGNORECASE | re.DOTALL,
)
MODIFIED_RE = re.compile(
    r"Your upcoming booking with\s+(.+?)\s+has been modified", re.IGNORECASE)

# Truncation tail: "... He... (more at https://r.rover.com/NWPXeH )"
TRUNCATED_RE = re.compile(r"\(\s*more at\s+https?://\S+\s*\)", re.IGNORECASE)

# Structured request block that Rover auto-sends as a message.
BOOKING_BLOCK_RE = re.compile(
    r"^\s*(.+?Request\s*-\s*[^:]+):", re.IGNORECASE)

# "Teddy (1 yr, 60 lbs)" -> pet name; also handles "Rusty & Osha (...)"
PET_NAME_RE = re.compile(r"^\s*([^(]+?)\s*(?:\(|$)")


@dataclass
class SmsMessage:
    sender: str                      # conversation number == thread key
    text: str                        # body as received (marker text included)
    kind: str = "message"            # inquiry | confirmed | modified | message
    service: Optional[str] = None    # boarding, stay, day care...
    owner_name: Optional[str] = None
    pet_name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    truncated: bool = False
    is_booking_block: bool = False   # the auto-sent "Boarding Request - One Time:" block
    flags: List[str] = field(default_factory=list)


def _pet_name(blob: Optional[str]) -> Optional[str]:
    if not blob:
        return None
    m = PET_NAME_RE.match(blob.strip())
    name = (m.group(1) if m else blob).strip(" .,:-")
    return name or None


def parse_sms(sender: str, body: str) -> SmsMessage:
    text = (body or "").strip()
    msg = SmsMessage(sender=sender, text=text)

    msg.truncated = bool(TRUNCATED_RE.search(text))
    msg.is_booking_block = bool(BOOKING_BLOCK_RE.search(text))

    # Markers arrive wrapped in [ ... ]; strip the brackets so leading "[" doesn't
    # get captured into owner names (e.g. "[ Brenna D." -> "Brenna D.").
    scan = re.sub(r"[\[\]]", " ", text).strip()

    m = MODIFIED_RE.search(scan)
    if m:
        msg.kind = "modified"
        msg.owner_name = m.group(1).strip()
        return msg

    m = CONFIRMED_RE.search(scan)
    if m:
        msg.kind = "confirmed"
        msg.owner_name = m.group(1).strip()
        msg.service = (m.group(2) or "").strip() or None
        msg.pet_name = _pet_name(m.group(3))
        msg.start_date, msg.end_date = m.group(4), m.group(5)
        return msg

    m = INQUIRY_RE.search(scan)
    if m:
        msg.kind = "inquiry"
        msg.service = m.group(1).strip()
        msg.owner_name = m.group(2).strip()
        msg.pet_name = _pet_name(m.group(3))
        msg.start_date, msg.end_date = m.group(4), m.group(5)
        return msg

    m = INQUIRY_SINGLE_RE.search(scan)
    if m:
        msg.kind = "inquiry"
        msg.service = m.group(1).strip()
        msg.owner_name = m.group(2).strip()
        msg.pet_name = _pet_name(m.group(3))
        msg.start_date = m.group(4)
        return msg

    if msg.truncated:
        msg.flags.append("truncated — full text needs email fallback (S5)")
    return msg
