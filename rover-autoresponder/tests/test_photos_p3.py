"""Addendum C — P3 polish: roster 'already updated today' marks, album (media_group)
intake handled once, and 'same caption for all'."""
import datetime

import pytest

from autoresponder import store, telegram_poll
from autoresponder.photos import (store as pstore, pipeline, approve, captions,
                                   telegram as ui, config as pconfig)


def _book(conn, num, pet, stay="08/30/2026 to 09/30/2026"):
    store.upsert_sms_thread(conn, num, owner_name="Owner " + pet, pet_name=pet,
                            stay_dates=stay, status="converted")
    store.mark_has_booked(conn, num)


@pytest.fixture
def cap(monkeypatch):
    """Capture every Telegram surface call the photo UI makes."""
    calls = {"send": [], "answer": [], "edit": []}
    monkeypatch.setattr(ui, "send", lambda text=None, **kw: (calls["send"].append((text, kw)) or 100 + len(calls["send"])))
    monkeypatch.setattr(ui, "answer", lambda cq, text=None, **kw: calls["answer"].append(text))
    monkeypatch.setattr(ui, "edit_text", lambda chat, mid, text, **kw: calls["edit"].append((mid, text)))
    monkeypatch.setattr(ui, "send_photos", lambda *a, **k: None)
    monkeypatch.setattr(ui, "download_photo", lambda fid: "/tmp/%s.jpg" % fid)
    return calls


# --- P3.1 roster: mark who already got an update today --------------------
def test_threads_updated_today_only_counts_sent(tmp_path):
    conn = store.init_db(str(tmp_path / "p3a.db"))
    _book(conn, "+1", "Blue")
    _book(conn, "+2", "Max")
    b = pstore.start_batch(conn, 1)
    u1 = pstore.get_or_create_update(conn, b, "+1", 1, "Blue")
    pstore.set_status(conn, u1, "sent")                 # Blue: went out today
    u2 = pstore.get_or_create_update(conn, b, "+2", 1, "Max")
    pstore.set_status(conn, u2, "ready")                # Max: only staged, not sent
    assert pstore.threads_updated_today(conn) == {"+1"}


def test_roster_keyboard_marks_updated_dogs():
    roster = [{"pet": "Blue", "owner": "Ann", "thread_key": "+1"},
              {"pet": "Max", "owner": "Sam", "thread_key": "+2"}]
    kb = ui.roster_keyboard(roster, updated={"+1"})
    labels = [row[0]["text"] for row in kb["inline_keyboard"][:2]]
    assert labels[0].startswith("✅ Blue")
    assert not labels[1].startswith("✅")


def test_show_roster_passes_updated_set(tmp_path, cap):
    conn = store.init_db(str(tmp_path / "p3a2.db"))
    _book(conn, "+1", "Blue")
    b = pstore.start_batch(conn, 1)
    pstore.set_status(conn, pstore.get_or_create_update(conn, b, "+1", 1, "Blue"), "sent")
    pipeline._show_roster(conn, intro=True)
    text, kw = cap["send"][-1]
    assert "already sent an update today" in text
    assert kw["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("✅ Blue")


# --- P3.2 album intake: react once per media_group ------------------------
def test_no_active_dog_prompts_once_per_album(tmp_path, cap):
    conn = store.init_db(str(tmp_path / "p3b.db"))
    _book(conn, "+1", "Blue")
    pstore.start_batch(conn, 1)                          # batch open, but no dog tapped
    pipeline.on_photo(conn, 1, "f1", media_group_id="G")
    pipeline.on_photo(conn, 1, "f2", media_group_id="G")   # same album — no second nudge
    prompts = [t for t, _ in cap["send"] if "Tap a dog first" in (t or "")]
    assert len(prompts) == 1


def test_active_dog_album_acks_once_but_keeps_all_photos(tmp_path, cap):
    conn = store.init_db(str(tmp_path / "p3b2.db"))
    _book(conn, "+1", "Blue")
    b = pstore.start_batch(conn, 1)
    pstore.set_active_dog(conn, 1, "+1")
    for fid in ("f1", "f2", "f3"):
        pipeline.on_photo(conn, 1, fid, media_group_id="ALB")
    acks = [t for t, _ in cap["send"] if "Collecting for" in (t or "")]
    assert len(acks) == 1                               # one ack for the whole album
    uid = pstore.get_or_create_update(conn, b, "+1", 1, "Blue")
    assert len(pstore.get_media(conn, uid)) == 3        # every photo still attached


# --- P3.3 same caption for all --------------------------------------------
def test_same_caption_for_all(tmp_path, cap, monkeypatch):
    pool = tmp_path / "captions.txt"
    pool.write_text("{pet} had a blast today!\n", encoding="utf-8")
    monkeypatch.setattr(pconfig, "CAPTIONS_PATH", str(pool))

    conn = store.init_db(str(tmp_path / "p3c.db"))
    _book(conn, "+1", "Blue")
    _book(conn, "+2", "Max")
    b = pstore.start_batch(conn, 1)
    for num, pet, mid in (("+1", "Blue", 201), ("+2", "Max", 202)):
        uid = pstore.get_or_create_update(conn, b, num, 1, pet)
        pstore.add_media(conn, uid, f"file-{pet}", f"/tmp/{pet}.jpg")
        pstore.set_status(conn, uid, "ready")
        pstore.link_card(conn, mid, uid)               # wire the reverse card map

    approve.same_caption_all(conn, 1, cq_id="cq")

    caps = {}
    for u in pstore.list_batch(conn, b):
        caps[u[4]] = u[5]                              # pet_name -> caption
    assert caps["Blue"] == "Blue had a blast today!"
    assert caps["Max"] == "Max had a blast today!"     # same line, each personalized
    assert {mid for mid, _ in cap["edit"]} == {201, 202}   # both cards re-rendered
