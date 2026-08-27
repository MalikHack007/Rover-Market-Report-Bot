"""S2: marker parsing against REAL captured Rover SMS samples."""
from autoresponder.sms_parser import parse_sms

N = "+15125550123"

# --- real inquiry sample (Anika / Teddy) ---
ANIKA = ("[ New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs) "
         "08/21/2026 to 08/23/2026. Book @ r.rover.com/8C48qS ]")

def test_inquiry_marker_full():
    m = parse_sms(N, ANIKA)
    assert m.kind == "inquiry"
    assert m.service == "boarding"
    assert m.owner_name == "Anika"
    assert m.pet_name == "Teddy"
    assert m.start_date == "08/21/2026"
    assert m.end_date == "08/23/2026"

def test_inquiry_multi_dog_pet_blob():
    body = ("[ New booking request (boarding) from Ezekiel & Janice: Rusty & Osha "
            "(3 yr, 40 lbs) 08/30/2026 to 09/07/2026. Book @ r.rover.com/x ]")
    m = parse_sms(N, body)
    assert m.kind == "inquiry"
    assert m.owner_name == "Ezekiel & Janice"
    assert m.pet_name == "Rusty & Osha"

def test_inquiry_single_date_daycare():
    body = ("[ New booking request (day care) from Hyejin: Daisy (2 yr, 15 lbs) "
            "07/28/2026. Book @ r.rover.com/y ]")
    m = parse_sms(N, body)
    assert m.kind == "inquiry"
    assert m.service == "day care"
    assert m.owner_name == "Hyejin"
    assert m.pet_name == "Daisy"
    assert m.start_date == "07/28/2026"

# --- real confirmed sample (Brenna / Alfie) ---
def test_confirmed_marker():
    body = ("[ Brenna D. has confirmed a booking request (stay) with Alfie "
            "from 08/13 to 08/14 - View on Rover r.rover.com/VzXwna ]")
    m = parse_sms(N, body)
    assert m.kind == "confirmed"
    assert m.owner_name == "Brenna D."
    assert m.pet_name == "Alfie"
    assert m.start_date == "08/13"
    assert m.end_date == "08/14"

# --- real single-date confirmed samples: Rover uses "on <date>" (no range) for
# day care and single-night stays. Previously these fell through to an ordinary
# message and never converted over SMS (only rescued by the confirmation email). ---
def test_confirmed_marker_single_date_daycare():
    body = ("[ Revanth A. has confirmed a booking request (daycare) with Blue "
            "on 08/31 - View on Rover r.rover.com/dNpLTP ]")
    m = parse_sms(N, body)
    assert m.kind == "confirmed"
    assert m.service == "daycare"
    assert m.owner_name == "Revanth A."
    assert m.pet_name == "Blue"
    assert m.start_date == "08/31"
    assert m.end_date is None

def test_confirmed_marker_single_night_stay():
    body = ("[ Samyak K. has confirmed a booking request (stay) with Coco "
            "on 08/15 - View on Rover r.rover.com/abc123 ]")
    m = parse_sms(N, body)
    assert m.kind == "confirmed"
    assert m.owner_name == "Samyak K."
    assert m.pet_name == "Coco"
    assert m.start_date == "08/15"
    assert m.end_date is None

# --- real modified sample (Joshua) ---
def test_modified_marker():
    body = ("[ Your upcoming booking with Joshua L. has been modified. "
            "Tap to review booking details. Review changes @ r.rover.com/Acm5dN ]")
    m = parse_sms(N, body)
    assert m.kind == "modified"
    assert m.owner_name == "Joshua L."

# --- ordinary messages carry no marker ---
def test_plain_client_message():
    m = parse_sms(N, "Will you be available to sit Teddy on Aug 21 - 23?")
    assert m.kind == "message"
    assert m.truncated is False

def test_afterthought_message():
    m = parse_sms(N, "Hey!! I'm not sure if you also watch kittens but I forgot to add my kitten on the request:(")
    assert m.kind == "message"

# --- real truncation sample (Brenna's questionnaire answers) ---
def test_truncated_message_flagged():
    body = ("1- you're the only person I've contacted. 2- yes but he's fine if he's in a "
            "kennel. 4- he goes out 3-5 times a day. If you're home, he'll want to go "
            "more. He... (more at https://r.rover.com/NWPXeH )")
    m = parse_sms(N, body)
    assert m.kind == "message"
    assert m.truncated is True
    assert any("truncated" in f for f in m.flags)

# --- the auto-sent structured block ---
def test_booking_block_detected():
    body = ("Boarding Request - One Time: Drop-off: Fri, Aug 21 at 1:00 PM - 1:30 PM "
            "Pick-up: Sun, Aug 23 at 2:00 PM - 3:30 PM")
    m = parse_sms(N, body)
    assert m.is_booking_block is True
    assert m.kind == "message"
