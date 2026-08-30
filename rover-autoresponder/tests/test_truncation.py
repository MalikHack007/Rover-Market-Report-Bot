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


# --- regression: email keeps smart punctuation, SMS is ASCII -> must still recover ---
# Real case (Erin/Dakota, rows 2620/2621): the email preserves the client's en-dash and curly
# apostrophes while Rover's SMS downgrades to ASCII. An unfolded compare broke at "Sep 4 – 6"
# (email U+2013) vs "Sep 4 - 6" (SMS U+002D), so recovery silently failed.
E = "+15125559999"
E_INQ = ("[ New booking request (boarding) from Erin: Dakota (2 yr, 50 lbs) "
         "09/04/2026 to 09/06/2026. Book @ r.rover.com/x ]")
E_TRUNC = ("Will you be available to sit Dakota on Sep 4 - 6? Hi, I'm looking for a sitter "
           "for my boy Dakota. He is very loving and cuddly. He's also very playful. He is "
           "potty trained so you wouldn't have to... (more at https://r.rover.com/tVtfvE )")
E_FULL = ("Will you be available to sit Dakota on Sep 4 – 6? Hi, I’m looking for a "
          "sitter for my boy Dakota. He is very loving and cuddly. He’s also very "
          "playful. He is potty trained so you wouldn’t have to worry about accidents in "
          "the house. He needs to be leashed at all times.")


def test_recover_across_smart_vs_ascii_punctuation(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, E, E_INQ)
    handle_sms(conn, E, E_TRUNC)
    _add_email_thread(conn, "gthread-erin", "Erin", "Dakota", [E_FULL])

    assert truncation.resolve_truncated(conn, E) == 1              # folded compare matches
    convo = [t for _, t in store.get_conversation(conn, E)]
    assert any("leashed at all times" in t for t in convo)        # full text recovered
    assert not any("more at" in t for t in convo)


def test_untruncated_messages_untouched(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, A, INQ)
    handle_sms(conn, A, "short and complete message")
    assert store.list_truncated(conn, A) == []


# --- regression: the real Cristian case (2026-08-19) ---
# Two email threads for one client, NEITHER with a pet name, and the conversation
# split across both. Name correlation is ambiguous; content matching must still win.
CRIS = "+13103073340"
CRIS_TRUNC = ("Hi Paul and Karina! I am looking for somebody to watch my 11 mo puppy "
              "Mazzy for the weekend. She is a sweet and energeti... "
              "(more at https://r.rover.com/abc )")
# Email wraps lines where SMS doesn't — matching must be whitespace-insensitive.
CRIS_FULL = ("Hi Paul and Karina! I am looking for somebody to watch my 11 mo puppy Mazzy\n"
             "for the weekend. She is a sweet and energetic puppy who loves people. She\n"
             "is on a once-daily antibiotic for her leg recovery.")


def _cristian_setup(conn):
    handle_sms(conn, CRIS, "[ New booking request (boarding) from Cristian: Mazzy "
                           "(10 mos, 41 lbs) 08/20/2026 to 08/23/2026. Book @ r.rover.com/x ]")
    handle_sms(conn, CRIS, CRIS_TRUNC)
    _add_email_thread(conn, "1a0169c3b9078342", "Cristian", None,
                      ["Boarding Request - One Time:\nDrop-off: Thu, Aug 20", CRIS_FULL])
    _add_email_thread(conn, "1a01a741f29fd9f7", "Cristian", None,
                      ["Apologies for incorrect name address."])


def test_recovers_despite_ambiguous_name_and_missing_pet(tmp_path):
    conn = _db(tmp_path)
    _cristian_setup(conn)
    # name correlation genuinely can't resolve this
    assert truncation.find_email_thread(conn, "Cristian", "Mazzy") is None
    # ...but content matching does
    assert truncation.resolve_truncated(conn, CRIS) == 1
    convo = [t for _, t in store.get_conversation(conn, CRIS)]
    assert any("antibiotic" in t for t in convo)
    assert not any("more at" in t for t in convo)


def test_matching_is_whitespace_insensitive(tmp_path):
    """Email hard-wraps lines; the SMS version doesn't."""
    conn = _db(tmp_path)
    _cristian_setup(conn)
    full = truncation.recover_full_text(conn, CRIS, CRIS_TRUNC)
    assert full is not None
    assert "\n" in full                      # the wrapped email version


def test_recovery_searches_all_threads_not_just_bound(tmp_path):
    """A conversation split across threads: the bound one may lack the message."""
    conn = _db(tmp_path)
    _cristian_setup(conn)
    store.bind_email_thread(conn, CRIS, "1a01a741f29fd9f7")   # bound to the WRONG one
    assert truncation.resolve_truncated(conn, CRIS) == 1       # still found elsewhere


def test_no_false_positive_on_different_client(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, CRIS, "[ New booking request (boarding) from Cristian: Mazzy "
                           "(10 mos, 41 lbs) 08/20/2026 to 08/23/2026. Book @ r.rover.com/x ]")
    handle_sms(conn, CRIS, CRIS_TRUNC)
    _add_email_thread(conn, "other", "Someone Else", "Rex",
                      ["Completely unrelated message about a different dog entirely."])
    assert truncation.resolve_truncated(conn, CRIS) == 0       # nothing matches -> no guess


# --- regression: questionnaire answers all START with OUR boilerplate ---
# Clients quote the questionnaire back, so the first ~124 chars are identical across
# every client. Matching on a short prefix stitched one client's message into another's
# thread. Matching must use the whole message, plus an owner cross-check.
QUESTIONNAIRE_HEAD = ("1. Where are you in your sitter search? Are you seriously "
                      "considering booking with me, or still browsing a few other "
                      "sitters? ")
BLUE = "+15125551111"
BLUE_TRUNC = (QUESTIONNAIRE_HEAD +
              "We are browsing to find a good fit. In particular, we want someone who "
              "will not take in other pets during her stay, as she gets anxious around "
              "too many new dogs.  2. Does your dog experience separation anxiety? She "
              "does not have separation anxiety... (more at https://r.rover.com/xyz )")
BLUE_FULL = (QUESTIONNAIRE_HEAD +
             "We are browsing to find a good fit. In particular, we want someone who\n"
             "will not take in other pets during her stay, as she gets anxious around\n"
             "too many new dogs.  2. Does your dog experience separation anxiety? She\n"
             "does not have separation anxiety and is okay being left alone briefly.\n"
             "Would you be ok meeting Blue at a neutral location like a dog park?")
OTHER_FULL = (QUESTIONNAIRE_HEAD +
              "- I am seriously considering booking with you if everything checks out.\n"
              "2. She does pretty well alone; I keep her home while I work 10 AM to 5 PM.")


def _blue(conn):
    handle_sms(conn, BLUE, "[ New booking request (boarding) from Sam: Blue "
                           "(3 yr, 40 lbs) 09/10/2026 to 09/12/2026. Book @ r.rover.com/x ]")
    handle_sms(conn, BLUE, BLUE_TRUNC)


def test_does_not_match_a_different_clients_questionnaire(tmp_path):
    """The exact production failure: identical boilerplate, different client."""
    conn = _db(tmp_path)
    _blue(conn)
    _add_email_thread(conn, "gthread-other", "Marta", "Nala", [OTHER_FULL])
    assert truncation.resolve_truncated(conn, BLUE) == 0
    convo = [t for _, t in store.get_conversation(conn, BLUE)]
    assert not any("10 AM to 5 PM" in t for t in convo)     # no contamination
    assert len(store.list_truncated(conn, BLUE)) == 1        # stays flagged instead


def test_still_recovers_the_same_clients_message(tmp_path):
    conn = _db(tmp_path)
    _blue(conn)
    _add_email_thread(conn, "gthread-sam", "Sam", "Blue", [BLUE_FULL])
    assert truncation.resolve_truncated(conn, BLUE) == 1
    assert any("dog park" in t for _, t in store.get_conversation(conn, BLUE))


def test_owner_mismatch_blocks_a_content_match(tmp_path):
    """Even if text somehow matched, a different owner name must veto it."""
    conn = _db(tmp_path)
    _blue(conn)
    _add_email_thread(conn, "gthread-imposter", "Marta", "Nala", [BLUE_FULL])
    assert truncation.resolve_truncated(conn, BLUE) == 0


def test_very_short_truncation_is_not_matched(tmp_path):
    """Too little text to identify anyone — better truncated than wrong."""
    conn = _db(tmp_path)
    handle_sms(conn, BLUE, "[ New booking request (boarding) from Sam: Blue "
                           "(3 yr, 40 lbs) 09/10/2026 to 09/12/2026. Book @ r.rover.com/x ]")
    handle_sms(conn, BLUE, "Sure... (more at https://r.rover.com/xyz )")
    _add_email_thread(conn, "gthread-sam", "Sam", "Blue", ["Sure thing, sounds great!"])
    assert truncation.resolve_truncated(conn, BLUE) == 0