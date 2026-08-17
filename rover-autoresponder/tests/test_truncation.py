"""S5: truncation detection, email correlation, and full-text recovery."""
from autoresponder import store, truncation
from autoresponder.models import ParsedMessage
from autoresponder.sms_pipeline import handle_sms

A = "+15125550001"
INQ = ("[ New booking request (boarding) from Brenna: Alfie (3 yr, 40 lbs) "
       "08/13/2026 to 08/14/2026. Book @ r.rover.com/x ]")

# Real truncation sample (Brenna's questionnaire answers, cut off by SMS)
TRUNC = ("1- you're the only person I've contacted. Alfie needs to be the only dog. "
         "2- yes but he's fine if he's in a kennel. 4- he goes out 3-5 times a day. "
         "If you're home, he'll want to go more. He... (more at https://r.rover.com/NWPXeH )")
FULL = ("1- you're the only person I've contacted. Alfie needs to be the only dog. "
        "2- yes but he's fine if he's in a kennel. 4- he goes out 3-5 times a day. "
        "If you're home, he'll want to go more. He also loves the backyard and naps "
        "in the afternoon. 5- no submissive urination. 6- one photo a day is perfect!")


def _db(tmp_path):
    return store.init_db(str(tmp_path / "s5.db"))


# --- text helpers ---
def test_strip_truncation_tail():
    out = truncation.strip_truncation_tail(TRUNC)
    assert "more at" not in out
    assert not out.endswith("...")
    assert out.startswith("1- you're the only person")


def test_prefix_for_match_normalizes():
    p = truncation.prefix_for_match(TRUNC)
    assert p == p.lower()
    assert "  " not in p


# --- correlation ---
def _add_email_thread(conn, thread_key, owner, pet, texts):
    for i, t in enumerate(texts):
        pm = ParsedMessage(f"gmail-{thread_key}-{i}", thread_key, owner, pet,
                           None, None, t, kind="inquiry")
        store.record_message(conn, pm)


def test_find_email_thread_by_owner(tmp_path):
    conn = _db(tmp_path)
    _add_email_thread(conn, "gthread-1", "Brenna D.", "Alfie", [FULL])
    assert truncation.find_email_thread(conn, "Brenna", "Alfie") == "gthread-1"


def test_find_email_thread_ambiguous_returns_none(tmp_path):
    """Two clients with the same first name and no pet match -> don't guess."""
    conn = _db(tmp_path)
    _add_email_thread(conn, "g1", "Brenna D.", "Alfie", ["a"])
    _add_email_thread(conn, "g2", "Brenna S.", "Rex", ["b"])
    assert truncation.find_email_thread(conn, "Brenna", None) is None


def test_find_email_thread_disambiguates_by_pet(tmp_path):
    conn = _db(tmp_path)
    _add_email_thread(conn, "g1", "Brenna D.", "Alfie", ["a"])
    _add_email_thread(conn, "g2", "Brenna S.", "Rex", ["b"])
    assert truncation.find_email_thread(conn, "Brenna", "Rex") == "g2"


def test_sms_thread_not_treated_as_email_thread(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    assert truncation.find_email_thread(conn, "Brenna", "Alfie") is None


# --- recovery ---
def test_recover_full_text_and_bind(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, TRUNC)
    _add_email_thread(conn, "gthread-1", "Brenna", "Alfie", [FULL])

    n = truncation.resolve_truncated(conn, A)
    assert n == 1
    convo = [t for _, t in store.get_conversation(conn, A)]
    assert any("naps in the afternoon" in t for t in convo)      # full text now present
    assert not any("more at" in t for t in convo)
    assert store.get_email_thread_key(conn, A) == "gthread-1"     # binding stored


def test_recovery_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, TRUNC)
    _add_email_thread(conn, "gthread-1", "Brenna", "Alfie", [FULL])
    assert truncation.resolve_truncated(conn, A) == 1
    assert truncation.resolve_truncated(conn, A) == 0     # nothing left to recover


def test_no_email_match_leaves_truncated(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, TRUNC)
    assert truncation.resolve_truncated(conn, A) == 0
    assert len(store.list_truncated(conn, A)) == 1        # still flagged for the card


def test_untruncated_messages_untouched(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, "short and complete message")
    assert store.list_truncated(conn, A) == []
