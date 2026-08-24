from autoresponder import telegram_notify as tg
from autoresponder import config


def test_draft_card_wraps_reply_in_pre_and_escapes():
    card, _ = tg.format_draft_card(
        owner="Ezekiel & Janice", dates="08/30/2026", stage="S0_INITIAL",
        flags=[], history=["Will you sit Rusty & Osha?"],
        draft_text="Hey Ezekiel & Janice, they look adorable!",
    )
    assert "<pre>" in card and "</pre>" in card
    # & must be HTML-escaped everywhere it appears
    assert "Rusty &amp; Osha" in card
    assert "Ezekiel &amp; Janice" in card
    assert "New inquiry" in card
    assert "S0_INITIAL" in card


def test_draft_card_shows_flags():
    card, _ = tg.format_draft_card("A", None, "S3_POST_SCREEN",
                                   ["client asked about a crate"],
                                   ["can we meet?"], "draft")
    assert "crate" in card


def test_offplaybook_card_has_no_draft():
    card, _ = tg.format_offplaybook_card("Vatsal", ["mid-stay logistics"],
                                         ["I'm tracking for 3:30"])
    assert "Needs your attention" in card
    assert "<pre>" not in card
    assert "3:30" in card



class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}
        self.text = ""
    def json(self):
        return self._payload


def test_send_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    assert tg.send_message("hi") is None


def test_send_posts_correct_payload_and_returns_message_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "TOKEN123")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "555")
    captured = {}
    def fake_post(url, json=None, timeout=None):
        captured["url"] = url; captured["json"] = json
        return _Resp(200, {"result": {"message_id": 42}})
    monkeypatch.setattr(tg._SESSION, "post", fake_post)

    mid = tg.send_message("<b>hello</b>", reply_markup={"inline_keyboard": []})
    assert mid == 42
    assert "TOKEN123" in captured["url"] and captured["url"].endswith("/sendMessage")
    assert captured["json"]["chat_id"] == "555"
    assert captured["json"]["parse_mode"] == "HTML"
    assert captured["json"]["reply_markup"] == {"inline_keyboard": []}


def test_send_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(tg._SESSION, "post", lambda url, json, timeout: _Resp(400))
    assert tg.send_message("x") is None


# --- long messages must not be silently chopped (undoing truncation recovery) ---
LONG = ("1. Where are you in your sitter search? " + "x" * 900 +
        " ...and finally, she loves the backyard.")


def test_recovered_long_message_is_shown_in_full():
    """A 944-char recovered answer used to be cut back to 500 in the card."""
    card, overflow = tg.format_draft_card(
        "Sam", "09/10", "S2_ANSWERS", [], [("Client", LONG)], "Thanks!")
    assert "backyard" in card                 # the END of the message survived
    assert overflow == []
    assert len(card) < tg.TELEGRAM_LIMIT


def test_newest_message_gets_priority_for_the_budget():
    """The draft answers the newest message, so that's the one to show in full."""
    history = [("Client", "old " + "a" * 3000), ("Client", "newest " + "b" * 900)]
    card, overflow = tg.format_draft_card("Sam", None, "S2_ANSWERS", [], history, "ok")
    assert "newest " + "b" * 900 in card       # newest complete
    assert overflow                            # older one spilled
    assert len(card) < tg.TELEGRAM_LIMIT


def test_draft_is_never_truncated():
    """The draft is what you send — it must always be complete."""
    draft = "Reply text. " + "z" * 1500
    card, _ = tg.format_draft_card("Sam", None, "S2_ANSWERS", [],
                                   [("Client", "q" * 3000)], draft)
    assert draft in card
    assert len(card) < tg.TELEGRAM_LIMIT


def test_card_stays_under_the_telegram_limit():
    history = [("Client", "m%d " % i + "y" * 1200) for i in range(6)]
    card, overflow = tg.format_draft_card("Sam", None, "S2_ANSWERS", [], history,
                                          "short reply")
    assert len(card) < tg.TELEGRAM_LIMIT
    assert overflow                            # the rest is sent separately


def test_send_draft_card_sends_overflow_followups(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "send_message",
                        lambda text, **k: sent.append(text) or len(sent))
    history = [("Client", "old " + "a" * 3000), ("Client", "new " + "b" * 900)]
    mid = tg.send_draft_card("Sam", None, "S2_ANSWERS", [], history, "ok")
    assert mid == 1                            # the CARD's id, not a follow-up's
    assert len(sent) == 2
    assert "Full message" in sent[1]