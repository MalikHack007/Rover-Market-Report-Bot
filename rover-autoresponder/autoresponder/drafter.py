"""Phase 2 drafter: turn a thread's client-side history into a stage + draft reply.

The Anthropic call is isolated in call_model() so the prompt building and JSON
parsing can be unit-tested with a fake client (no API key, no network).
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from . import config

log = logging.getLogger(__name__)

STAGES = ["S0_INITIAL", "S1_CONSENT", "S2_ANSWERS", "S3_POST_SCREEN"]


def should_draft(status: str) -> bool:
    """Draft on any thread that hasn't gone terminal.

    Only inquiry threads reach here (the caller gates on subject kind), so this
    just enforces the terminal states: converted / not_suitable stop drafting.
    Active inquiry threads draft on every message -> multi-turn.
    """
    return not (status and status != "active")


@dataclass
class Draft:
    stage: str
    draft_text: str
    off_playbook: bool
    flags: List[str] = field(default_factory=list)
    raw: str = ""
    # Name recovery layer 3: the pet's name inferred from the client's own message,
    # used when the inquiry marker (which normally carries it) never arrived.
    inferred_pet: str = ""


def load_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def build_system_prompt(playbook_text: str, faq_text: str, sitter_name: str) -> str:
    sys_prompt = playbook_text.replace("{SITTER_NAME}", sitter_name or "the sitter")
    # Names a client might legitimately greet you by — anything else in a greeting means
    # the request was meant for a different sitter (§ "Wrong sitter" in the playbook).
    aliases = [a.strip() for a in (config.SITTER_ALIASES or "").split(",") if a.strip()]
    if sitter_name and sitter_name not in aliases:
        aliases.insert(0, sitter_name)
    sys_prompt = sys_prompt.replace(
        "{SITTER_ALIASES}", ", ".join(aliases) if aliases else (sitter_name or "the sitter"))
    sys_prompt = sys_prompt.replace(
        "{WRONG_SITTER_TEMPLATE}", config.WRONG_SITTER_TEMPLATE)
    if faq_text.strip():
        # Phase 5: FAQ wired in. It's authoritative where it overlaps the playbook
        # (e.g. the meet-and-greet link/wording), so the model has one source of truth.
        sys_prompt += (
            "\n\n# FAQ — canned answers to common client questions\n"
            "Reproduce the relevant answer closely when a client asks one of these. "
            "Where the FAQ and a playbook template cover the same thing (e.g. the "
            "meet-and-greet), THIS FAQ wording is the current, authoritative version — "
            "prefer it over the playbook template.\n\n"
            + faq_text
        )
    return sys_prompt


def build_user_content(owner: Optional[str], pet: Optional[str],
                       dates: Optional[str], stored_stage: Optional[str],
                       history: List[str], extra_instruction: str = None) -> str:
    lines = [
        f"Owner: {owner or 'unknown'}",
        f"Pet: {pet or 'unknown (may be mentioned in the messages)'}",
        f"Stay start: {dates or 'unknown'}",
        f"Stored stage hint: {stored_stage or 'S0_INITIAL'}",
        "",
        "Conversation so far (oldest first; 'Client' = them, 'You' = replies you "
        "already sent):",
    ]
    for i, msg in enumerate(history, 1):
        # Addendum A: SMS history arrives as ("Client"|"You", text) tuples so the model
        # sees BOTH sides. The email path still passes plain strings (client-only).
        if isinstance(msg, (tuple, list)) and len(msg) == 2:
            speaker, text = msg
            lines.append(f"  {i}. {speaker}: {text}")
        else:
            lines.append(f"  {i}. {msg}")
    lines += [
        "",
        "Draft the next reply per the playbook, or set off_playbook=true (empty "
        "draft) if it doesn't fit any stage. Respond with the JSON object only.",
    ]
    # Phase 4: a revision nudge from the Regenerate / Warmer / Shorter buttons.
    if extra_instruction:
        lines += ["", f"Revision request for this draft: {extra_instruction}"]
    return "\n".join(lines)


def parse_llm_json(text: str) -> dict:
    """Tolerant JSON extraction: strip fences, then take the outermost { ... }."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


def _make_client():
    import anthropic  # deferred: only the live draft path needs the SDK
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def call_model(client, system: str, user: str) -> str:
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.DRAFT_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content
        if getattr(b, "type", None) == "text"
    )


def draft_reply(owner, pet, dates, stored_stage, history,
                client=None, system_prompt=None, extra_instruction=None) -> Draft:
    """Produce a Draft for the latest message. Pass client/system_prompt in tests.

    extra_instruction (Phase 4): a revision nudge from the Regenerate/Warmer/Shorter
    buttons, appended to the user content.
    """
    client = client or _make_client()
    if system_prompt is None:
        system_prompt = build_system_prompt(
            load_text(config.PLAYBOOK_PATH),
            load_text(config.FAQ_PATH),
            config.SITTER_NAME,
        )
    user = build_user_content(owner, pet, dates, stored_stage, history, extra_instruction)
    raw = call_model(client, system_prompt, user)
    data = parse_llm_json(raw)

    stage = data.get("stage") or stored_stage or "S0_INITIAL"
    if stage not in STAGES:
        stage = stored_stage or "S0_INITIAL"
    return Draft(
        stage=stage,
        draft_text=(data.get("draft_text") or "").strip(),
        off_playbook=bool(data.get("off_playbook")),
        flags=list(data.get("flags") or []),
        raw=raw,
        inferred_pet=(data.get("pet_name") or "").strip(),
    )