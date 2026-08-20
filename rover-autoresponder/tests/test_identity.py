"""Layered owner/pet name recovery when the inquiry marker doesn't arrive."""
from autoresponder import store, identity
from autoresponder import telegram_notify as tg
from autoresponder import sms_approve
from autoresponder.models import ParsedMessage
from autoresponder.sms_pipeline import handle_sms

N = "+18589255548"
BLOCK = ("Boarding Request - One Time: Drop-off: Wed, Aug 19 at 2:30 PM - 2:30 PM "
         "Pick-up: Tue, Aug 25 at 6:00 PM - 6:00 PM")
CUSTOM = ("Hi Daniel! I'm looking for someone who will take great care of Maple while "
          "I'm away. She's a sweet girl, but she can be a little shy and nervous.")


def _db(tmp_path):
    return store.init_db(str(tmp_path / "id.db"))


def _email_thread(conn, key, owner, pet, texts):
    for i, t in enumerate(texts):
        store.record_message(conn, ParsedMessage(f"g-{key}-{i}", key, owner, pet,
                                                 None, None, t, kind="inquiry"))


# --- Layer 2: owner name from the email subject, correlated by content ---
def test_owner_recovered_from_email_when_marker_missing(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)                 # no marker ever arrives
    handle_sms(conn, N, CUSTOM)
    assert store.get_thread(conn, N)[0] is None            # owner unknown from SMS
    # the email side always has the owner (parsed from the subject line)
    _email_thread(conn, "gthread-9", "Destiny", None, [BLOCK, CUSTOM])
    owner, pet = identity.recover_names_from_email(conn, N)
    assert owner == "Destiny"
    assert store.get_thread(conn, N)[0] == "Destiny"       # persisted
    assert store.get_email_thread_key(conn, N) == "gthread-9"


def test_correlation_uses_booking_block_content(tmp_path):
    """The block appears verbatim in both channels — ideal correlation key."""
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    _email_thread(conn, "gthread-1", "Destiny", None, [BLOCK])
    assert identity.find_email_thread_by_content(conn, N) == "gthread-1"


def test_no_correlation_when_nothing_matches(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    _email_thread(conn, "gthread-2", "Someone", "Rex", ["totally unrelated content here"])
    assert identity.find_email_thread_by_content(conn, N) is None


# --- Layer 3: pet name inferred by the drafter from the client's message ---
def test_inferred_pet_name_is_stored(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    handle_sms(conn, N, CUSTOM)
    assert identity.apply_inferred_pet(conn, N, "Maple") == "Maple"
    assert store.get_thread(conn, N)[1] == "Maple"


def test_placeholder_inferences_rejected(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    for junk in ["unknown", "your pup", "the dog", "", "  ", "N/A"]:
        assert identity.apply_inferred_pet(conn, N, junk) is None
    assert store.get_thread(conn, N)[1] is None


def test_inference_does_not_override_known_name(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, "[ New booking request (boarding) from Charlotte: Royal "
                        "(5 yrs, 41 lbs) 08/19/2026 to 08/19/2026. Book @ r.rover.com/x ]")
    assert identity.apply_inferred_pet(conn, N, "Wrongname") == "Royal"
    assert store.get_thread(conn, N)[1] == "Royal"


# --- Layer 4: manual override from Telegram ---
def test_manual_pet_and_owner(tmp_path):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    assert identity.set_manual(conn, N, "pet", "Maple") is True
    assert identity.set_manual(conn, N, "owner", "Daniel") is True
    owner, pet = store.get_thread(conn, N)[0], store.get_thread(conn, N)[1]
    assert (owner, pet) == ("Daniel", "Maple")
    assert identity.set_manual(conn, N, "pet", "  ") is False


def test_slash_pet_command_from_telegram(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    store.link_card(conn, 555, N)
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda text, **k: sent.append(text) or 1)
    assert sms_approve.handle_text_reply(conn, "/pet Maple", 1, 555) is True
    assert store.get_thread(conn, N)[1] == "Maple"
    assert any("Maple" in s for s in sent)


def test_non_command_reply_is_still_an_edit(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    handle_sms(conn, N, BLOCK)
    store.link_card(conn, 555, N)
    monkeypatch.setattr(tg, "send_message", lambda text, **k: 1)
    monkeypatch.setattr(tg, "edit_message_text", lambda *a, **k: True)
    assert sms_approve.handle_text_reply(conn, "My own wording", 1, 555) is True
    assert store.get_pending_text(conn, N) == "My own wording"
