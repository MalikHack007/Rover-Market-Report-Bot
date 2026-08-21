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


def test_booking_block_alone_opens_the_inquiry(tmp_path):
    """The block IS the new-request signal — don't wait for a marker that may never come."""
    conn = _db(tmp_path)
    scheduled = []
    handle_sms(conn, A, BLOCK, schedule_draft=scheduled.append)
    assert store.get_thread(conn, A)[4] == "active"     # open immediately
    assert scheduled == [A]                              # and drafting is scheduled


def test_marker_after_block_enriches_details(tmp_path):
    """The marker still fills in owner/pet/dates when it does arrive."""
    conn = _db(tmp_path)
    handle_sms(conn, A, BLOCK)
    handle_sms(conn, A, ANIKA_INQ)
    owner, pet, dates, stage, status = store.get_thread(conn, A)
    assert status == "active"
    assert owner == "Anika" and pet == "Teddy"
    assert dates == "08/21/2026 to 08/23/2026"


def test_custom_message_inquiry_without_marker_still_drafts(tmp_path):
    """Real case (+18589255548): client writes their own message, no marker ever comes."""
    conn = _db(tmp_path)
    scheduled = []
    handle_sms(conn, A, BLOCK, schedule_draft=scheduled.append)
    handle_sms(conn, A, "Hi! We're headed out of town and need boarding for our pup.",
               schedule_draft=scheduled.append)
    assert store.get_thread(conn, A)[4] == "active"
    assert scheduled == [A, A]                           # not stranded


# --- returning clients: Rover reuses a number per CLIENT across bookings ---
INQ2 = ("[ New booking request (boarding) from Anika: Teddy (2 yr, 62 lbs) "
        "01/10/2027 to 01/12/2027. Book @ r.rover.com/new ]")


def test_returning_client_reactivates_converted_thread(tmp_path):
    """A year later, the same number sends a NEW request — must not stay converted."""
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "will you be available?")
    handle_sms(conn, A, BRENNA_CONF)                    # booked -> converted
    assert store.get_thread(conn, A)[4] == "converted"

    handle_sms(conn, A, INQ2)                            # returns with a new request
    owner, pet, dates, stage, status = store.get_thread(conn, A)
    assert status == "active"                            # reactivated
    assert stage == "S0_INITIAL"                         # fresh screening
    assert dates == "01/10/2027 to 01/12/2027"           # new booking's dates
    assert store.get_episode(conn, A) == 2


def test_new_episode_scopes_conversation(tmp_path):
    """The new request must not inherit the old conversation as context."""
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, "old question from last year")
    store.record_outbound(conn, A, "old reply from last year")
    handle_sms(conn, A, BRENNA_CONF)

    handle_sms(conn, A, INQ2)
    handle_sms(conn, A, "new question this time")
    convo = store.get_conversation(conn, A)
    texts = [t for _, t in convo]
    assert "new question this time" in texts
    assert not any("last year" in t for t in texts)      # old episode excluded


def test_returning_client_drafts_again(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, BRENNA_CONF)                     # converted; bot goes quiet
    scheduled = []
    handle_sms(conn, A, "hey!", schedule_draft=scheduled.append)
    assert scheduled == []                                # still quiet
    handle_sms(conn, A, INQ2)                             # new request
    handle_sms(conn, A, "are you free in January?", schedule_draft=scheduled.append)
    assert scheduled == [A]                               # drafting resumes


def test_booking_block_before_marker_joins_new_episode(tmp_path):
    """The block lands seconds before the marker; it must not be stranded."""
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    handle_sms(conn, A, BRENNA_CONF)
    handle_sms(conn, A, BLOCK)          # new request's block arrives first
    handle_sms(conn, A, INQ2)           # then the marker
    convo = store.get_conversation(conn, A)
    assert any("Boarding Request" in t for _, t in convo)  # pulled into this episode


def test_first_ever_inquiry_is_episode_one(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    assert store.get_episode(conn, A) == 1


def test_block_then_marker_stays_on_one_episode(tmp_path):
    """Real Rover ordering: the block opens the thread, the marker must not bump it."""
    conn = _db(tmp_path)
    handle_sms(conn, A, BLOCK)
    assert store.get_episode(conn, A) == 1
    handle_sms(conn, A, ANIKA_INQ)
    assert store.get_episode(conn, A) == 1          # NOT 2
    # and the block's context is still in this episode
    assert any("Boarding Request" in t for _, t in store.get_conversation(conn, A))


def test_marker_after_a_real_exchange_starts_a_new_episode(tmp_path):
    """Once we've replied, the episode is in use — a new marker means a new booking."""
    conn = _db(tmp_path)
    handle_sms(conn, A, ANIKA_INQ)
    store.record_outbound(conn, A, "Hey Anika!")
    handle_sms(conn, A, BRENNA_CONF)
    handle_sms(conn, A, INQ2)
    assert store.get_episode(conn, A) == 2