from autoresponder import telegram_notify as tg
from autoresponder import config


def test_draft_card_wraps_reply_in_pre_and_escapes():
    card = tg.format_draft_card(
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
    card = tg.format_draft_card("A", None, "S3_POST_SCREEN", ["client asked about a crate"],
                                ["can we meet?"], "draft")
    assert "crate" in card


def test_offplaybook_card_has_no_draft():
    card = tg.format_offplaybook_card("Vatsal", ["mid-stay logistics"], ["I'm tracking for 3:30"])
    assert "Needs your attention" in card
    assert "<pre>" not in card
    assert "3:30" in card


def test_send_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    assert tg.send_message("hi") is False


class _Resp:
    def __init__(self, code, text=""): self.status_code = code; self.text = text


def test_send_posts_correct_payload(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "TOKEN123")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "555")
    captured = {}
    def fake_post(url, json, timeout):
        captured["url"] = url; captured["json"] = json
        return _Resp(200)
    monkeypatch.setattr(tg.requests, "post", fake_post)

    ok = tg.send_message("<b>hello</b>")
    assert ok is True
    assert "TOKEN123" in captured["url"] and captured["url"].endswith("/sendMessage")
    assert captured["json"]["chat_id"] == "555"
    assert captured["json"]["parse_mode"] == "HTML"
    assert captured["json"]["text"] == "<b>hello</b>"


def test_send_non_200_returns_false(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(tg.requests, "post", lambda url, json, timeout: _Resp(400, "bad request"))
    assert tg.send_message("x") is False
