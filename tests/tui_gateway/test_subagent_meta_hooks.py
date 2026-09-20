"""The generic seams a plugin uses to say what a subagent really is, and to serve its transcript.

Core cannot know that a relay wrapper running as `sonnet` actually drives codex gpt-6-astra at xhigh
under job w_2026…; the plugin that owns the child does. These two hooks are that seam, and core keeps
ownership of the row's identity either way.
"""

from __future__ import annotations

import pytest

from tui_gateway import methods_subagents as subagents


@pytest.fixture
def hooks(monkeypatch):
    """Route invoke_hook to callbacks this test registers, without a real plugin manager."""
    registered: dict[str, list] = {}

    def invoke_hook(hook_name, **kwargs):
        return [callback(**kwargs) for callback in registered.get(hook_name, [])]

    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "invoke_hook", invoke_hook)
    return registered


def _record(**overrides):
    return {
        "subagent_id": "task-9", "kind": "sdk", "goal": "review the diff", "status": "running",
        "tool_count": 2, "last_tool": "Bash", "started_at": 1.0,
        "subagent_meta": {"sdk_agent_id": "task-9", "sdk_parent_session_id": "parent-uuid",
                          "subagent_type": "conductor:codex-worker", "requested_model": "sonnet",
                          "source": "claude-agent-sdk"},
        **overrides,
    }


def test_a_plugin_names_the_real_worker_behind_a_relay(hooks):
    hooks["subagent_metadata"] = [lambda **kw: {
        "display_name": "Codex worker", "worker": "codex", "model": "gpt-6-astra",
        "effort": "xhigh", "job_id": "w_20260916T204142Z_5638"}]

    meta = subagents._enriched_meta(_record(), "session-1")

    assert meta["display_name"] == "Codex worker"
    assert (meta["model"], meta["effort"]) == ("gpt-6-astra", "xhigh")
    assert meta["job_id"] == "w_20260916T204142Z_5638"
    # The base identity is still there: enrichment adds, it does not replace.
    assert meta["requested_model"] == "sonnet", "what the Task asked for stays visible"
    assert meta["sdk_agent_id"] == "task-9"


def test_a_plugin_cannot_rewrite_the_identity_core_tracks(hooks):
    hooks["subagent_metadata"] = [lambda **kw: {
        "sdk_agent_id": "someone-elses-agent", "sdk_parent_session_id": "other-session",
        "source": "not-the-sdk", "subagent_type": "impostor", "display_name": "fine to set"}]

    meta = subagents._enriched_meta(_record(), "session-1")

    assert meta["sdk_agent_id"] == "task-9"
    assert meta["sdk_parent_session_id"] == "parent-uuid"
    assert meta["source"] == "claude-agent-sdk"
    assert meta["subagent_type"] == "conductor:codex-worker"
    assert meta["display_name"] == "fine to set"


def test_a_raising_plugin_never_breaks_the_roster(hooks):
    def explode(**kwargs):
        raise RuntimeError("plugin is broken")

    hooks["subagent_metadata"] = [explode]

    meta = subagents._enriched_meta(_record(), "session-1")

    assert meta["subagent_type"] == "conductor:codex-worker"


def test_a_plugin_can_serve_the_transcript_for_a_child_it_owns(hooks):
    hooks["subagent_tail"] = [lambda **kw: {"text": "worker log line\nsecond line", "state": "ready",
                                            "source": "conductor"}]

    served = subagents._plugin_tail(_record(), "session-1", {}, 16384)

    assert served["available"] is True
    assert served["source"] == "conductor"
    assert "worker log line" in served["text"]


def test_an_empty_plugin_answer_defers_to_the_built_in_readers(hooks):
    hooks["subagent_tail"] = [lambda **kw: None, lambda **kw: {"text": ""}]

    assert subagents._plugin_tail(_record(), "session-1", {}, 16384) is None


def test_a_plugin_tail_is_clipped_and_marked_truncated(hooks):
    hooks["subagent_tail"] = [lambda **kw: {"text": "x" * 500}]

    served = subagents._plugin_tail(_record(), "session-1", {}, 100)

    assert len(served["text"]) == 100
    assert served["truncated"] is True
