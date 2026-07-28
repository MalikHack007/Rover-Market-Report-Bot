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
