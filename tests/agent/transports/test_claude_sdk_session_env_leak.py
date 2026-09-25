"""Tests for cross-session HERMES_SESSION_ID leak prevention in Claude Agent SDK.

In a multiplexed backend process (tui_gateway, gateway), multiple sessions are
hosted within the same Python process. When any session initializes, ambient
os.environ["HERMES_SESSION_ID"] may be set or left holding a sibling session's id.
The Claude Agent SDK launches the Claude Code CLI by merging options.env on top of
os.environ ({**os.environ, ..., **options.env}). Without an explicit override,
the spawned CLI subprocess (and its Bash tool commands) inherits the ambient
foreign session id.

These tests assert that:
1. Session A's CLI subprocess env carries Session A's id when ambient os.environ is Session B.
2. Concurrent/sequential session env builds do not cross-contaminate.
3. ContextVar resolution binds the active task's session id when not passed explicitly.
4. An engaged multi-session host masks ambient HERMES_SESSION_ID when no session is bound.
"""

from __future__ import annotations

import os

import pytest

from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
from agent.transports.claude_agent_sdk_session_config import _sdk_env_overrides
from gateway.session_context import (
    _SESSION_ID,
    _UNSET,
    _VAR_MAP,
    reset_session_vars,
    set_session_vars,
)


@pytest.fixture(autouse=True)
def _isolate_context():
    """Reset session contextvars and ambient HERMES_SESSION_ID for test isolation."""
    saved_ambient = os.environ.get("HERMES_SESSION_ID")
    saved_vars = {name: var.get() for name, var in _VAR_MAP.items()}
    try:
        yield
    finally:
        for name, val in saved_vars.items():
            _VAR_MAP[name].set(val)
        if saved_ambient is None:
            os.environ.pop("HERMES_SESSION_ID", None)
        else:
            os.environ["HERMES_SESSION_ID"] = saved_ambient


def test_cli_subprocess_env_session_a_beats_ambient_session_b(monkeypatch):
    """Session A's CLI subprocess env must carry Session A's id even when ambient
    os.environ holds Session B's id (reproduction for conductor-worker leak)."""
    monkeypatch.setenv("HERMES_SESSION_ID", "20260924_200208_c68a80")

    session = ClaudeAgentSdkSession(
        hermes_session_id="20260924_200305_74c7ad",
        cwd="/tmp",
    )
    fields = session.build_option_fields()

    # The Claude Agent SDK subprocess_cli.py builds the CLI env as:
    # {**os.environ, ..., **options.env, ...}
    cli_env = {**os.environ, **fields["env"]}

    assert fields["env"].get("HERMES_SESSION_ID") == "20260924_200305_74c7ad"
    assert cli_env.get("HERMES_SESSION_ID") == "20260924_200305_74c7ad"


def test_two_sessions_env_builds_do_not_cross_contaminate(monkeypatch):
    """Two live sessions building CLI subprocess environments must not cross-contaminate."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-wide-session")

    session_a = ClaudeAgentSdkSession(hermes_session_id="session-A", cwd="/tmp")
    session_b = ClaudeAgentSdkSession(hermes_session_id="session-B", cwd="/tmp")

    fields_a = session_a.build_option_fields()
    fields_b = session_b.build_option_fields()

    cli_env_a = {**os.environ, **fields_a["env"]}
    cli_env_b = {**os.environ, **fields_b["env"]}

    assert cli_env_a["HERMES_SESSION_ID"] == "session-A"
    assert cli_env_b["HERMES_SESSION_ID"] == "session-B"


def test_cli_subprocess_env_resolves_from_contextvar(monkeypatch):
    """When hermes_session_id is not passed to constructor, it must resolve from
    the task's active session ContextVar."""
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-ambient-session")
    set_session_vars(session_id="contextvar-session-42")

    session = ClaudeAgentSdkSession(cwd="/tmp")
    fields = session.build_option_fields()
    cli_env = {**os.environ, **fields["env"]}

    assert fields["env"].get("HERMES_SESSION_ID") == "contextvar-session-42"
    assert cli_env.get("HERMES_SESSION_ID") == "contextvar-session-42"


def test_cli_subprocess_env_masks_ambient_session_id_when_context_engaged(monkeypatch):
    """When the process has engaged session context but this task has no session bound,
    ambient HERMES_SESSION_ID must be masked to '' rather than leaking a sibling's id."""
    set_session_vars(session_id="initial-session")
    reset_session_vars()  # clears contextvars back to _UNSET for this task
    monkeypatch.setenv("HERMES_SESSION_ID", "sibling-session-id")

    session = ClaudeAgentSdkSession(cwd="/tmp")
    fields = session.build_option_fields()
    cli_env = {**os.environ, **fields["env"]}

    assert fields["env"].get("HERMES_SESSION_ID") == ""
    assert cli_env.get("HERMES_SESSION_ID") == ""


def test_configured_env_cannot_override_session_id(monkeypatch):
    """Operator env in config.yaml must not override the session's actual HERMES_SESSION_ID."""
    from agent.transports import claude_agent_sdk_session_config as config_mod

    monkeypatch.setattr(
        config_mod,
        "_configured_sdk_env",
        lambda: {"HERMES_SESSION_ID": "spoofed-session-id"},
    )

    overrides = _sdk_env_overrides(hermes_session_id="authentic-session-id")
    assert overrides["HERMES_SESSION_ID"] == "authentic-session-id"


def test_mcp_and_cli_subprocess_session_ids_match(monkeypatch):
    """Both hermes-tools MCP server and Claude CLI options env must carry the same session id."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-ambient-id")

    session = ClaudeAgentSdkSession(hermes_session_id="aligned-session-id", cwd="/tmp")
    fields = session.build_option_fields()

    cli_session_id = fields["env"].get("HERMES_SESSION_ID")
    mcp_session_id = fields["mcp_servers"]["hermes-tools"]["env"].get("HERMES_SESSION_ID")

    assert cli_session_id == "aligned-session-id"
    assert mcp_session_id == "aligned-session-id"
