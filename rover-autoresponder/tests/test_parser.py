import base64

from autoresponder.parser import (
    extract_text_from_payload,
    html_to_text,
    parse_notification,
)

VATSAL_SUBJECT = "New message from Vatsal about Gypsy's stay"
VATSAL_BODY = (
    "Hi Yujie,\n"
    "Vatsal sent you a message about a stay from 08/26/2025 to 08/28/2025.\n"
    "Vatsal says:\n"
    "I'm tracking for 3:30\n"
    "Reply now\n"
    "If you have any questions, don't hesitate to contact us via the Help Center "
    "or reply directly to this email.\n"
)


def test_parses_owner_pet_dates_and_message():
    pm = parse_notification(VATSAL_SUBJECT, VATSAL_BODY, "m1", "t1")
    assert pm.owner_name == "Vatsal"
    assert pm.pet_name == "Gypsy"
    assert pm.stay_start == "08/26/2025"
    assert pm.stay_end == "08/28/2025"
    assert pm.message_text == "I'm tracking for 3:30"
    assert pm.recognized is True
    assert pm.thread_key == "t1"
    assert pm.gmail_msg_id == "m1"


def test_second_message_in_thread():
    body = VATSAL_BODY.replace("I'm tracking for 3:30", "Here")
    pm = parse_notification(VATSAL_SUBJECT, body, "m2", "t1")
    assert pm.message_text == "Here"
    assert pm.recognized is True


def test_multiline_message():
    body = VATSAL_BODY.replace(
        "I'm tracking for 3:30",
        "Hi! Is Gypsy okay with cats?\nAlso can I drop off early?",
    )
    pm = parse_notification(VATSAL_SUBJECT, body, "m3", "t1")
    assert "cats" in pm.message_text
    assert "drop off early" in pm.message_text


def test_curly_apostrophe_subject():
    subj = "New message from Ana about Rex\u2019s stay"
    pm = parse_notification(subj, VATSAL_BODY, "m4", "t2")
    assert pm.owner_name == "Ana"
    assert pm.pet_name == "Rex"


def test_unrecognized_subject_is_flagged_not_crashed():
    pm = parse_notification("Your Rover payout is on the way", "some body\nReply now", "m5", "t3")
    assert pm.recognized is False
    # still safe to store; owner/pet just unknown
    assert pm.owner_name is None


def test_html_fallback_extraction():
    html = (
        "<html><body><p>Hi Yujie,</p>"
        "<p>Vatsal sent you a message about a stay from 08/26/2025 to 08/28/2025.</p>"
        "<p><b>Vatsal says:</b></p><p>I'm tracking for 3:30</p>"
        "<a href='#'>Reply now</a></body></html>"
    )
    text = html_to_text(html)
    pm = parse_notification(VATSAL_SUBJECT, text, "m6", "t4")
    assert pm.message_text == "I'm tracking for 3:30"
    assert pm.stay_start == "08/26/2025"


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def test_extract_text_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("PLAIN Vatsal says:\nHere\nReply now")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML version</p>")}},
        ],
    }
    text = extract_text_from_payload(payload)
    assert "PLAIN" in text
    assert "HTML version" not in text


def test_new_message_about_a_booking():
    # Real subject captured 2026-07-27 (a booking-message notification).
    subj = "Yisell sent you a new message about a booking starting 07/28/2026"
    body = "Hi there,\nYisell says:\nHi Onel, can you watch our baby Milo?\nReply now\n"
    pm = parse_notification(subj, body, "m10", "t10")
    assert pm.owner_name == "Yisell"
    assert pm.stay_start == "07/28/2026"
    assert pm.message_text == "Hi Onel, can you watch our baby Milo?"
    assert pm.recognized is True
    # pet isn't in this subject template; that's fine
    assert pm.pet_name is None


def test_booking_message_generic_says_fallback():
    # Even if the "owner says:" line is absent, generic says:/Reply now still works.
    subj = "Yisell sent you a new message about a booking starting 07/28/2026"
    body = "says:\nHi Onel, can you watch our baby Milo?\nReply now"
    pm = parse_notification(subj, body, "m11", "t11")
    assert pm.owner_name == "Yisell"
    assert pm.recognized is True


# --- Real Yisell thread captured 2026-07-27 (two messages, one thread) ---
_YISELL_SUBJ = "Yisell sent you a new message about a booking starting 07/28/2026"

def test_yisell_freeform_message_real_body():
    body = (
        "Hi Yujie,\n"
        "Yisell sent you a message about a stay starting 07/28/2026.\n"
        "Yisell says:\n"
        "Hi Onel, can you watch our baby Milo?\n"
        "Reply now\n"
        "Book this stay\n"
        "Remember, all services booked through Rover are covered by the Rover Guarantee.\n"
    )
    pm = parse_notification(_YISELL_SUBJ, body, "y1", "yt1")
    assert pm.owner_name == "Yisell"
    assert pm.stay_start == "07/28/2026"
    assert pm.message_text == "Hi Onel, can you watch our baby Milo?"
    assert pm.recognized is True

def test_yisell_structured_booking_request_message_real_body():
    # The auto-sent "Boarding Request" summary arrives as a MESSAGE, same template.
    body = (
        "Hi Yujie,\n"
        "Yisell sent you a message about a stay starting 07/28/2026.\n"
        "Yisell says:\n"
        "Boarding Request - One Time:\n"
        "Drop-off: Tue, Jul 28 at 9:00 AM - 9:00 AM\n"
        "Pick-up: Wed, Jul 29 at 9:00 AM - 9:00 AM\n"
        "Reply now\n"
        "Book this stay\n"
    )
    pm = parse_notification(_YISELL_SUBJ, body, "y2", "yt1")
    assert pm.owner_name == "Yisell"
    assert pm.recognized is True
    assert pm.message_text.startswith("Boarding Request - One Time:")
    assert "Drop-off: Tue, Jul 28" in pm.message_text
    assert "Pick-up: Wed, Jul 29" in pm.message_text
    # "Reply now" / "Book this stay" must NOT bleed into the captured message
    assert "Reply now" not in pm.message_text
    assert "Book this stay" not in pm.message_text


# --- subject kind classification (new-inquiry vs confirmed vs other) ---
def test_kind_inquiry_singular():
    subj = "Hyejin sent you a new message about a booking starting 07/28/2026"
    body = "Hyejin says:\nWill you be available to host Daisy(boy) for day care tomorrow?\nReply now"
    pm = parse_notification(subj, body, "k1", "kt1")
    assert pm.kind == "inquiry"
    assert pm.owner_name == "Hyejin"
    assert pm.stay_start == "07/28/2026"

def test_kind_inquiry_multi_owner_and_dog():
    subj = "Ezekiel & Janice sent you a new message about a booking starting 08/30/2026"
    body = "Ezekiel & Janice says:\nWill you be available to sit Rusty & Osha on Aug 30-Sep 7?\nReply now"
    pm = parse_notification(subj, body, "k2", "kt2")
    assert pm.kind == "inquiry"
    assert pm.owner_name == "Ezekiel & Janice"
    assert pm.stay_start == "08/30/2026"
    assert "Rusty & Osha" in pm.message_text

def test_kind_confirmed_is_not_drafted():
    subj = "New message from Minyoung about Captain's stay"
    body = "Minyoung says:\nWhat time should I drop off?\nReply now"
    pm = parse_notification(subj, body, "k3", "kt3")
    assert pm.kind == "confirmed"

def test_kind_other_for_unfamiliar_subject():
    pm = parse_notification("Your Rover payout is on the way", "some body", "k4", "kt4")
    assert pm.kind == "other"
