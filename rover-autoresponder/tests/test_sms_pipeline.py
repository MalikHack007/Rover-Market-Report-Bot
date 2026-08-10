"""S2: thread state machine keyed by conversation number."""
from autoresponder import store
from autoresponder.sms_pipeline import handle_sms

A = "+15125550001"   # Anika's conversation number
B = "+15125550002"

ANIKA_INQ = ("[ New booking request (boarding) from Anika: Teddy (1 yr, 60 lbs) "
             "08/21/2026 to 08/23/2026. Book @ r.rover.com/8C48qS ]")
BRENNA_CONF = ("[ Brenna D. has confirmed a booking request (stay) with Alfie "
               "from 08/13 to 08/14 - View on Rover r.rover.com/VzXwna ]")
JOSHUA_MOD = ("[ Your upcoming booking with Joshua L. has been modified. "
              "Review changes @ r.rover.com/Acm5dN ]")


def _db(tmp_path):
    return store.init_db(str(tmp_path / "sms.db"))


def test_inquiry_opens_active_thread(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    owner, pet, dates, stage, status = store.get_thread(conn, A)
    assert status == "active"
    assert owner == "Anika" and pet == "Teddy"
    assert dates == "08/21/2026 to 08/23/2026"


def test_client_message_on_active_thread_schedules_draft(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    scheduled = []
    handle_sms(conn, A, "Will you be available to sit Teddy on Aug 21 - 23?",
               schedule_draft=scheduled.append)
    assert scheduled == [A]          # drafting triggered for this thread


def test_confirmed_marker_converts_and_stops_drafting(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, BRENNA_CONF)                 # booking confirms
    assert store.get_thread(conn, A)[4] == "converted"
    scheduled = []
    handle_sms(conn, A, "what time should I drop off?", schedule_draft=scheduled.append)
    assert scheduled == []                            # no drafting after conversion


def test_modified_marker_converts(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, JOSHUA_MOD)
    assert store.get_thread(conn, A)[4] == "converted"


def test_unknown_thread_not_drafted(tmp_path):
    """First message from a number with no inquiry marker -> stay out of it."""
    conn = _db(tmp_path)
    scheduled = []
    handle_sms(conn, B, "hey are you around tomorrow?", schedule_draft=scheduled.append)
    assert store.get_thread(conn, B)[4] == "unknown"
    assert scheduled == []


def test_threads_are_isolated_by_number(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, B, "random text")
    assert store.get_thread(conn, A)[4] == "active"
    assert store.get_thread(conn, B)[4] == "unknown"


def test_truncated_message_recorded_and_flagged(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    msg = handle_sms(conn, A, "1- long answer ... He... (more at https://r.rover.com/NWPXeH )")
    assert msg.truncated is True


def test_messages_are_logged_per_thread(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "Will you be available?")
    msgs = store.get_thread_messages(conn, A)
    assert len(msgs) == 2
    assert "Will you be available?" in msgs[-1]


def test_webhook_event_dedupe(tmp_path):
    conn = _db(tmp_path)
    assert store.sms_event_seen(conn, "evt-1") is False
    assert store.sms_event_seen(conn, "evt-1") is True    # gateway retry
    assert store.sms_event_seen(conn, "evt-2") is False


BLOCK = ("Boarding Request - One Time: Drop-off: Fri, Aug 21 at 1:00 PM - 1:30 PM "
         "Pick-up: Sun, Aug 23 at 2:00 PM - 3:30 PM")


def test_booking_block_before_marker_opens_pending_then_active(tmp_path):
    """Real ordering: the structured block arrives BEFORE the inquiry marker."""
    conn = _db(tmp_path)
    scheduled = []
    handle_sms(conn, A, BLOCK, schedule_draft=scheduled.append)
    assert store.get_thread(conn, A)[4] == "pending"   # held, not stranded as unknown
    assert scheduled == []                              # nothing drafted yet
    handle_sms(conn, A, ANIKA_INQ, schedule_draft=scheduled.append)
    assert store.get_thread(conn, A)[4] == "active"     # marker promotes it
    handle_sms(conn, A, "Will you be available?", schedule_draft=scheduled.append)
    assert scheduled == [A]                             # now drafting
