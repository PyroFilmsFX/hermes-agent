"""HE-SECRET-HYGIENE S2: the agent-side ingest choke points.

``build_turn_context`` is the backstop every lane shares (CLI, TUI, gateway, API server,
cron, SDK): it masks THIS turn's user text before the log preview, the persist override
and the append, and never touches ``conversation_history`` (prompt caching). Steer text,
the CLI staging edge and SDK tool results persisted to state.db are masked too.

Every secret is a FAKE built at runtime from a seeded PRNG; the tag key is the fixed
``b"\\x11" * 32`` the root conftest pins, so nothing touches the Keychain.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import string
import types

from agent.secret_hygiene import PLACEHOLDER_RE, ingress_secret_tags, mask_ingress_text
from tests.agent.test_turn_context import _FakeAgent, _build, _stub_runtime_main  # noqa: F401

_ALNUM = string.ascii_letters + string.digits


def fake(n: int, seed: int) -> str:
    rng = random.Random(seed)
    while True:
        value = "".join(rng.choice(_ALNUM) for _ in range(n))
        if re.search("[a-z]", value) and re.search("[A-Z]", value) and re.search("[0-9]", value):
            return value


def _gh(seed: int = 7) -> str:
    return "ghp_" + fake(36, seed)


def _db_pw(seed: int = 11) -> str:
    return fake(22, seed)


def _db_url(pw: str) -> str:
    return f"postgresql://neondb_owner:{pw}@ep-fake-123.us-east-2.aws.neon.tech/neondb?sslmode=require"


def test_build_turn_context_masks_before_log_persist_and_send(caplog):
    token = _gh()
    raw = f"use {token} for the push"
    agent = _FakeAgent()
    with caplog.at_level(logging.INFO, logger="agent.turn_context"):
        ctx = _build(agent, user_message=raw, persist_user_message=raw)
    # Log preview (the turn-start line keeps 80 chars, so the token is inside it).
    assert "conversation turn" in caplog.text
    assert token not in caplog.text
    assert "[REDACTED:github-token:" in caplog.text
    # Persisted row (the override the flush writes) and the model-bound message.
    assert token not in str(agent._persist_user_message_override)
    assert token not in ctx.user_message
    assert token not in json.dumps(ctx.messages[-1])
    assert PLACEHOLDER_RE.search(ctx.messages[-1]["content"])
    assert token not in str(ctx.original_user_message)


def test_backstop_masks_text_parts_of_multimodal_content():
    pw = _db_pw()
    parts = [{"type": "text", "text": f"connect with {_db_url(pw)}"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    ctx = _build(_FakeAgent(), user_message=parts, summarize_user_message_for_log=str)
    content = ctx.messages[-1]["content"]
    assert pw not in json.dumps(content)
    assert "[REDACTED:neon-url:" in content[0]["text"]
    assert content[1] == parts[1]


def test_backstop_never_touches_conversation_history():
    """A pre-feature turn that went out raw stays byte-identical: rewriting it would bust the
    provider prompt cache mid-conversation."""
    old_token = _gh(21)
    history = [
        {"role": "user", "content": f"old turn with {old_token}"},
        {"role": "assistant", "content": "noted"},
    ]
    before = json.dumps(history, sort_keys=True)
    snapshot = copy.deepcopy(history)
    ctx = _build(_FakeAgent(), user_message=f"new turn with {_gh(22)}", conversation_history=history)
    assert json.dumps(history, sort_keys=True) == before
    assert ctx.messages[:-1] == snapshot
    assert ctx.messages[0] is history[0]
    assert old_token in ctx.messages[0]["content"]
    assert "[REDACTED:github-token:" in ctx.messages[-1]["content"]


def test_backstop_is_deterministic_for_the_same_input():
    raw = f"deploy with {_gh(31)}"
    first = _build(_FakeAgent(), user_message=raw).messages[-1]["content"]
    second = _build(_FakeAgent(), user_message=raw).messages[-1]["content"]
    assert first == second
    assert mask_ingress_text(first)[0] == first


def test_pending_cli_handoff_still_adopted_after_edge_mask():
    """The CLI stages the edge-masked text; the backstop re-masks the (raw or masked) turn input
    to the same bytes, so ``_stage_turn_user_message`` still adopts the staged dict."""
    raw = f"store {_gh(41)} in the vault"
    masked, _ = mask_ingress_text(raw)
    assert masked != raw
    for turn_input in (masked, raw):
        agent = _FakeAgent()
        staged = {"role": "user", "content": masked, "_db_persisted": True}
        agent._pending_cli_user_message = staged
        ctx = _build(agent, user_message=turn_input)
        assert ctx.messages[-1] is staged
        assert ctx.messages[-1]["content"] == masked


def test_cli_edge_masks_before_staging(monkeypatch):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

    token = _gh(45)
    agent = types.SimpleNamespace(_session_persist_lock=None, _session_messages=[])
    host = types.SimpleNamespace(conversation_history=[])
    message = CLIChatTurnMixin._chat_mask_user_message(host, f"push with {token}")
    CLIChatTurnMixin._chat_stage_user_message(host, agent, message)
    assert token not in json.dumps(host.conversation_history)
    assert token not in agent._pending_cli_user_message["content"]


def test_steer_text_masked():
    from run_agent import AIAgent

    token = _gh(51)
    agent = types.SimpleNamespace(api_mode="chat_completions", _pending_steer=None,
                                  _pending_steer_lock=None)
    assert AIAgent.steer(agent, f"  also use {token}  ") is True
    assert token not in agent._pending_steer
    assert "[REDACTED:github-token:" in agent._pending_steer

    sent: list[str] = []
    sdk = types.SimpleNamespace(steer=lambda text: sent.append(text) or True)
    agent = types.SimpleNamespace(api_mode="claude_agent_sdk", _claude_sdk_session=sdk,
                                  _pending_steer=None, _pending_steer_lock=None)
    assert AIAgent.steer(agent, f"use {token}") is True
    assert sent and token not in sent[0]


def test_optout_tags_apply_to_one_turn_only(monkeypatch):
    from agent import secret_hygiene

    monkeypatch.setattr(secret_hygiene, "load_secret_hygiene_config",
                        lambda: secret_hygiene.SecretHygieneConfig(optout_allowed=True))
    token = _gh(61)
    raw = f"write {token} into .env"
    tags = {t["tag"] for t in ingress_secret_tags(raw)}
    agent = _FakeAgent()
    agent._secret_optout_tags = frozenset(tags)
    ctx = _build(agent, user_message=raw)
    assert token in ctx.messages[-1]["content"]
    assert not agent._secret_optout_tags
    ctx2 = _build(agent, user_message=raw)
    assert token not in ctx2.messages[-1]["content"]


def test_optout_ignored_when_config_disallows(monkeypatch):
    from agent import secret_hygiene

    token = _gh(62)
    raw = f"write {token} into .env"
    tags = {t["tag"] for t in ingress_secret_tags(raw)}
    disallowed = secret_hygiene.SecretHygieneConfig(optout_allowed=False)
    monkeypatch.setattr(secret_hygiene, "load_secret_hygiene_config", lambda: disallowed)
    agent = _FakeAgent()
    agent._secret_optout_tags = frozenset(tags)
    assert token not in _build(agent, user_message=raw).messages[-1]["content"]


def test_cli_collapsed_paste_is_masked_and_private(monkeypatch, tmp_path):
    import stat

    import cli
    from hermes_cli.cli_tui_mixin import CLITuiMixin

    token = _gh(64)
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    host = types.SimpleNamespace(_tui_paste_counter=0, _tui_paste_just_collapsed=False)
    placeholder = CLITuiMixin._tui_collapse_paste(host, f"run with {token}", 0, fallback=False)
    paste = next((tmp_path / "pastes").glob("*.txt"))
    assert token not in paste.read_text()
    assert "[REDACTED:github-token:" in paste.read_text()
    assert stat.S_IMODE(paste.stat().st_mode) == 0o600
    assert str(paste) in placeholder


def test_mask_ingress_off_leaves_turn_raw(monkeypatch):
    from agent import secret_hygiene

    token = _gh(63)
    monkeypatch.setattr(secret_hygiene, "load_secret_hygiene_config",
                        lambda: secret_hygiene.SecretHygieneConfig(mask_ingress=False))
    assert token in _build(_FakeAgent(), user_message=f"x {token}").messages[-1]["content"]


def test_sdk_tool_result_rows_masked_before_persist():
    """SDK-lane (CLI-native) tool results reach state.db through the projector, not through
    Hermes tool-output redaction; the projected ``role='tool'`` row is masked."""
    from agent.transports.claude_sdk_event_projector import ClaudeSdkEventProjector

    pw = _db_pw(71)
    ToolResultBlock = type("ToolResultBlock", (), {})
    UserMessage = type("UserMessage", (), {})
    block = ToolResultBlock()
    block.tool_use_id, block.is_error = "toolu_1", False
    block.content = [{"type": "text", "text": f"DATABASE_URL={_db_url(pw)}\n"}]
    message = UserMessage()
    message.content = [block]
    projection = ClaudeSdkEventProjector().project(message)
    rows = [m for m in projection.messages if m.get("role") == "tool"]
    assert rows and pw not in json.dumps(rows)
    assert "[REDACTED:neon-url:" in rows[0]["content"]
    assert rows[0]["tool_call_id"] == "toolu_1"


def test_api_server_turns_stay_unmasked_in_v1():
    """Owner decision (v1): the OpenAI-compatible API server is exempt. Its clients resend their
    own raw history every turn, so masking only the current turn would neither keep the secret
    from the model nor keep the prompt-cache prefix stable."""
    token = _gh(81)
    agent = _FakeAgent()
    agent.platform = "api_server"
    assert token in _build(agent, user_message=f"use {token}").messages[-1]["content"]
