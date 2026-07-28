import json

from autoresponder import drafter
from autoresponder.drafter import (
    Draft, build_system_prompt, build_user_content, parse_llm_json, draft_reply,
)

PLAYBOOK = "You are {SITTER_NAME}. Rules... Templates..."


# --- JSON parsing tolerance ---
def test_parse_plain_json():
    d = parse_llm_json('{"stage":"S0_INITIAL","off_playbook":false,"flags":[],"draft_text":"hi"}')
    assert d["stage"] == "S0_INITIAL" and d["draft_text"] == "hi"

def test_parse_json_in_code_fence():
    raw = '```json\n{"stage":"S1_CONSENT","off_playbook":false,"flags":[],"draft_text":"q"}\n```'
    d = parse_llm_json(raw)
    assert d["stage"] == "S1_CONSENT"

def test_parse_json_with_prose_around_it():
    raw = 'Here is the reply:\n{"stage":"S2_ANSWERS","off_playbook":false,"flags":[],"draft_text":"thanks"}\nHope that helps!'
    d = parse_llm_json(raw)
    assert d["stage"] == "S2_ANSWERS"


# --- prompt building ---
def test_system_prompt_substitutes_sitter_name_and_appends_faq():
    sp = build_system_prompt(PLAYBOOK, "Q: parking? A: street.", "Onel")
    assert "You are Onel." in sp
    assert "{SITTER_NAME}" not in sp
    assert "FAQ" in sp and "parking" in sp

def test_system_prompt_without_faq_has_no_faq_section():
    sp = build_system_prompt(PLAYBOOK, "", "Onel")
    assert "FAQ" not in sp

def test_user_content_lists_history_in_order():
    uc = build_user_content("Yisell", None, "07/28/2026", "S0_INITIAL",
                            ["Boarding Request - One Time: ...", "Hi Onel, can you watch Milo?"])
    assert "Owner: Yisell" in uc
    assert "1. Boarding Request" in uc
    assert "2. Hi Onel" in uc


# --- end-to-end with a fake Anthropic client ---
class _Block:
    def __init__(self, text): self.type = "text"; self.text = text
class _Resp:
    def __init__(self, text): self.content = [_Block(text)]
class FakeClient:
    def __init__(self, payload): self._payload = payload; self.last_kwargs = None
    class _Messages:
        def __init__(self, outer): self._outer = outer
        def create(self, **kwargs):
            self._outer.last_kwargs = kwargs
            return _Resp(self._outer._payload)
    @property
    def messages(self): return FakeClient._Messages(self)

def test_draft_reply_happy_path_s0():
    payload = json.dumps({
        "stage": "S0_INITIAL", "off_playbook": False, "flags": [],
        "draft_text": "Hey Yisell, Milo looks adorable! ...",
    })
    fake = FakeClient(payload)
    d = draft_reply("Yisell", None, "07/28/2026", "S0_INITIAL",
                    ["Hi Onel, can you watch our baby Milo?"],
                    client=fake, system_prompt="SYS")
    assert isinstance(d, Draft)
    assert d.stage == "S0_INITIAL"
    assert d.off_playbook is False
    assert d.draft_text.startswith("Hey Yisell")
    # the model was actually called with our system prompt + a user message
    assert fake.last_kwargs["system"] == "SYS"
    assert fake.last_kwargs["messages"][0]["role"] == "user"

def test_draft_reply_off_playbook_midstay_logistics():
    payload = json.dumps({
        "stage": "S3_POST_SCREEN", "off_playbook": True,
        "flags": ["mid-stay logistics: 'I'm tracking for 3:30'"], "draft_text": "",
    })
    d = draft_reply("Vatsal", "Gypsy", "08/26/2025", "S3_POST_SCREEN",
                    ["I'm tracking for 3:30"], client=FakeClient(payload), system_prompt="SYS")
    assert d.off_playbook is True
    assert d.draft_text == ""
    assert d.flags and "logistics" in d.flags[0]

def test_draft_reply_bad_stage_falls_back_to_stored():
    payload = json.dumps({"stage": "NONSENSE", "off_playbook": False,
                          "flags": [], "draft_text": "x"})
    d = draft_reply("A", None, None, "S1_CONSENT", ["sure!"],
                    client=FakeClient(payload), system_prompt="SYS")
    assert d.stage == "S1_CONSENT"  # invalid stage -> stored stage


# --- gating: should_draft (multi-turn; inquiry gating happens upstream) ---
def test_gate_active_thread_drafts():
    assert drafter.should_draft("active") is True

def test_gate_terminal_threads_never_draft():
    assert drafter.should_draft("converted") is False
    assert drafter.should_draft("not_suitable") is False
