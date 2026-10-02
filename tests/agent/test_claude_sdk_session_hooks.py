"""Tests for CLI-side tool and subagent hook passthrough (M10).

Verifies that Claude CLI tool use (PreToolUse, PostToolUse) and subagent
lifecycle (SubagentStart, SubagentStop) translate to Hermes plugin hooks
(pre_tool_call, post_tool_call, subagent_start, subagent_stop), remain
strictly observe-only, swallow exceptions, and leave CLI decisions untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.transports.claude_agent_sdk_session_hooks import (
    _sdk_exposes_subagent_hooks,
)
from tests.agent.claude_sdk_fakes import (
    _make_session,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


class TestClaudeSdkSessionHooks:
    def test_options_carry_both_compaction_and_new_hooks(self):
        """Option fields must merge compaction hooks and new CLI lifecycle hooks."""
        session, _ = _make_session()
        options = session.build_option_fields()
        hooks = options.get("hooks")

        assert hooks is not None
        assert "PreCompact" in hooks
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        if _sdk_exposes_subagent_hooks():
            assert "SubagentStart" in hooks
            assert "SubagentStop" in hooks

        for event in ("PreCompact", "PreToolUse", "PostToolUse"):
            matchers = hooks[event]
            assert len(matchers) >= 1
            assert len(matchers[0].hooks) >= 1

    def test_pre_tool_use_invokes_pre_tool_call_with_source_claude_cli(self, monkeypatch):
        """PreToolUse must invoke pre_tool_call with {tool_name, tool_input, session_id, source}."""
        session, _ = _make_session(hermes_session_id="session-sdk-1")
        hooks = session.build_option_fields()["hooks"]
        cb = hooks["PreToolUse"][0].hooks[0]

        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook",
            lambda name, **kw: calls.append((name, kw)) or [],
        )

        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "session_id": "session-sdk-1",
        }
        res = asyncio.run(cb(input_data, "call-1", None))

        assert len(calls) == 1
        name, kwargs = calls[0]
        assert name == "pre_tool_call"
        assert kwargs["tool_name"] == "Bash"
        assert kwargs["tool_input"] == {"command": "ls -la"}
        assert kwargs["args"] == {"command": "ls -la"}
        assert kwargs["session_id"] == "session-sdk-1"
        assert kwargs["source"] == "claude_cli"
        assert kwargs["tool_call_id"] == "call-1"
        assert res == {}

    def test_post_tool_use_invokes_post_tool_call_with_response(self, monkeypatch):
        """PostToolUse must invoke post_tool_call with the response and result."""
        session, _ = _make_session(hermes_session_id="session-sdk-1")
        hooks = session.build_option_fields()["hooks"]
        cb = hooks["PostToolUse"][0].hooks[0]

        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook",
            lambda name, **kw: calls.append((name, kw)) or [],
        )

        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": {"output": "hi\n"},
            "session_id": "session-sdk-1",
        }
        res = asyncio.run(cb(input_data, "call-2", None))

        assert len(calls) == 1
        name, kwargs = calls[0]
        assert name == "post_tool_call"
        assert kwargs["tool_name"] == "Bash"
        assert kwargs["tool_input"] == {"command": "echo hi"}
        assert kwargs["tool_response"] == {"output": "hi\n"}
        assert kwargs["result"] == {"output": "hi\n"}
        assert kwargs["session_id"] == "session-sdk-1"
        assert kwargs["source"] == "claude_cli"
        assert kwargs["tool_call_id"] == "call-2"
        assert res == {}

    def test_raising_plugin_does_not_break_callback_return_value(self, monkeypatch):
        """A raising plugin or lifecycle error must be swallowed and not affect the return."""
        session, _ = _make_session(hermes_session_id="session-sdk-1")
        hooks = session.build_option_fields()["hooks"]
        pre_cb = hooks["PreToolUse"][0].hooks[0]
        post_cb = hooks["PostToolUse"][0].hooks[0]

        def _exploding_invoke(name, **kw):
            raise RuntimeError("plugin failure")

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _exploding_invoke)

        pre_res = asyncio.run(pre_cb({"tool_name": "Bash", "tool_input": {}}, None, None))
        assert pre_res == {}

        post_res = asyncio.run(post_cb({"tool_name": "Bash", "tool_input": {}, "tool_response": None}, None, None))
        assert post_res == {}

    def test_callback_return_value_leaves_cli_decision_untouched(self):
        """The callback's return value must be {} so CLI permissions and flows are untouched."""
        session, _ = _make_session(hermes_session_id="session-sdk-1")
        hooks = session.build_option_fields()["hooks"]

        for event in ("PreToolUse", "PostToolUse"):
            cb = hooks[event][0].hooks[0]
            res = asyncio.run(cb({"tool_name": "Bash", "tool_input": {}}, "call-id", None))
            assert res == {}
            assert "permissionDecision" not in res
            assert "decision" not in res
            assert "continue_" not in res
            assert "updatedInput" not in res

    def test_subagent_start_and_stop_hooks_fire(self, monkeypatch):
        """SubagentStart and SubagentStop translate to subagent_start / subagent_stop."""
        session, _ = _make_session(hermes_session_id="session-sdk-1")
        hooks = session.build_option_fields()["hooks"]
        if "SubagentStart" not in hooks or "SubagentStop" not in hooks:
            pytest.skip("Subagent hooks not exposed by installed SDK")

        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            "hermes_cli.lifecycle.invoke_hook",
            lambda name, **kw: calls.append((name, kw)) or [],
        )

        start_cb = hooks["SubagentStart"][0].hooks[0]
        start_res = asyncio.run(
            start_cb(
                {"agent_id": "child-agent-1", "agent_type": "Explore", "session_id": "session-sdk-1"},
                None,
                None,
            )
        )
        assert start_res == {}
        assert len(calls) == 1
        assert calls[0][0] == "subagent_start"
        assert calls[0][1]["child_subagent_id"] == "child-agent-1"
        assert calls[0][1]["child_role"] == "Explore"
        assert calls[0][1]["source"] == "claude_cli"

        stop_cb = hooks["SubagentStop"][0].hooks[0]
        stop_res = asyncio.run(
            stop_cb(
                {
                    "agent_id": "child-agent-1",
                    "agent_type": "Explore",
                    "session_id": "session-sdk-1",
                    "agent_transcript_path": "/tmp/transcript.jsonl",
                    "stop_hook_active": True,
                },
                None,
                None,
            )
        )
        assert stop_res == {}
        assert len(calls) == 2
        assert calls[1][0] == "subagent_stop"
        assert calls[1][1]["child_subagent_id"] == "child-agent-1"
        assert calls[1][1]["child_role"] == "Explore"
        assert calls[1][1]["agent_transcript_path"] == "/tmp/transcript.jsonl"
        assert calls[1][1]["stop_hook_active"] is True
        assert calls[1][1]["source"] == "claude_cli"

    def test_subagent_hooks_omitted_when_unsupported_by_sdk(self, monkeypatch):
        """When the SDK does not expose subagent hooks, they must be omitted."""
        monkeypatch.setattr(
            "agent.transports.claude_agent_sdk_session_hooks._sdk_exposes_subagent_hooks",
            lambda: False,
        )
        session, _ = _make_session()
        hooks = session.build_option_fields()["hooks"]
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        assert "SubagentStart" not in hooks
        assert "SubagentStop" not in hooks

    def test_test_plugin_observes_cli_tool_calls(self):
        """A test plugin registered on pre_tool_call and post_tool_call observes CLI tool calls."""
        from hermes_cli import plugins

        pre_observed: list[dict[str, Any]] = []
        post_observed: list[dict[str, Any]] = []

        def on_pre(**kw):
            pre_observed.append(kw)

        def on_post(**kw):
            post_observed.append(kw)

        mgr = plugins.get_plugin_manager()
        mgr._hooks.setdefault("pre_tool_call", []).append(on_pre)
        mgr._hooks.setdefault("post_tool_call", []).append(on_post)
        try:
            session, _ = _make_session(hermes_session_id="obs-sess")
            hooks = session.build_option_fields()["hooks"]
            pre_cb = hooks["PreToolUse"][0].hooks[0]
            post_cb = hooks["PostToolUse"][0].hooks[0]

            asyncio.run(pre_cb({"tool_name": "Glob", "tool_input": {"pattern": "*.py"}}, "u1", None))
            assert len(pre_observed) == 1
            assert pre_observed[0]["tool_name"] == "Glob"
            assert pre_observed[0]["source"] == "claude_cli"

            asyncio.run(
                post_cb(
                    {"tool_name": "Glob", "tool_input": {"pattern": "*.py"}, "tool_response": ["a.py"]},
                    "u1",
                    None,
                )
            )
            assert len(post_observed) == 1
            assert post_observed[0]["tool_name"] == "Glob"
            assert post_observed[0]["result"] == ["a.py"]
            assert post_observed[0]["source"] == "claude_cli"
        finally:
            mgr._hooks.get("pre_tool_call", []).remove(on_pre)
            mgr._hooks.get("post_tool_call", []).remove(on_post)
