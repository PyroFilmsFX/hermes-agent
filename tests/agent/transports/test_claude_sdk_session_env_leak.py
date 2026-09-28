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


def test_respawned_cli_rebuilds_hermes_tools_config_with_clean_env(monkeypatch, tmp_path):
    """A replacement CLI gets an importable MCP child and a fresh scoped capability."""
    import json
    import sys
    import sysconfig

    from agent.transports import claude_agent_sdk_session_config as config

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", "/desktop/backend/first/site-packages")
    monkeypatch.setattr(config, "_hermes_repo_root", lambda: "/hermes/repo")
    monkeypatch.setattr(config, "_provider_config", lambda: {
        "session_spawn": {"enabled": True},
        "session_send": {"enabled": True},
    })
    issued = iter(("scoped-capability-first", "scoped-capability-replacement"))
    monkeypatch.setattr(
        "agent.transports.hermes_gateway_session_bridge.issue_scoped_capability",
        lambda _session_id: next(issued),
    )

    session = ClaudeAgentSdkSession(hermes_session_id="respawn-owner", cwd="/tmp")
    first = session.build_option_fields()
    # The replacement is built after the backend environment has changed; a
    # respawn must keep the first launch's hermes-tools child contract.
    monkeypatch.setenv("PYTHONPATH", "/desktop/backend/replacement/site-packages")
    replacement = session.build_option_fields()
    first_mcp = first["mcp_servers"]["hermes-tools"]
    replacement_mcp = replacement["mcp_servers"]["hermes-tools"]

    assert replacement_mcp["command"] == first_mcp["command"] == sys.executable
    assert replacement_mcp["args"] == first_mcp["args"] == [
        "-P", "-m", "agent.transports.hermes_tools_mcp_server", "--profile", "claude-agent-sdk",
    ]
    assert replacement_mcp["env"]["PYTHONPATH"] == first_mcp["env"]["PYTHONPATH"]
    python_paths = ["/hermes/repo"]
    for key in ("purelib", "platlib"):
        site_packages = sysconfig.get_paths().get(key)
        if site_packages and os.path.isdir(site_packages) and site_packages not in python_paths:
            python_paths.append(site_packages)
    assert first_mcp["env"]["PYTHONPATH"] == replacement_mcp["env"]["PYTHONPATH"] == os.pathsep.join(
        python_paths
    )
    assert replacement["env"]["PYTHONPATH"] == "", "the SDK CLI's interpreter scrub remains active"
    capability_path = replacement_mcp["env"]["HERMES_SESSION_SPAWN_CAPABILITY_FILE"]
    assert first_mcp["env"]["HERMES_SESSION_SPAWN_CAPABILITY_FILE"] == capability_path
    assert (home / capability_path.removeprefix(str(home) + os.sep)).read_text(encoding="utf-8") == (
        "scoped-capability-replacement"
    )
    assert "scoped-capability-replacement" not in json.dumps(replacement_mcp)
    assert replacement_mcp["env"]["HERMES_SESSION_ID"] == "respawn-owner"


def test_mcp_config_imports_from_resolved_venv_interpreter(monkeypatch, tmp_path):
    """The CLI may respawn a venv symlink's resolved interpreter."""
    import asyncio
    import importlib.util
    import subprocess
    import sys

    real_interpreter = os.path.realpath(sys.executable)
    if real_interpreter == sys.executable or importlib.util.find_spec("mcp") is None:
        pytest.skip("requires a venv symlink and the MCP package")

    from agent.transports import claude_agent_sdk_session_config as config

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    mcp_config = config._build_hermes_tools_mcp_config()
    child_env = dict(mcp_config["env"])
    child_env.update({
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "PYTHONHOME": "",
        "PYTHONUTF8": "1",
    })
    result = subprocess.run(
        [real_interpreter, "-c", "import mcp, agent.transports.hermes_tools_mcp_server"],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    async def list_tools():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=real_interpreter,
            args=mcp_config["args"],
            env=child_env,
            cwd=str(tmp_path),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.list_tools()
                return {tool.name for tool in listed.tools}

    assert "read_file" in asyncio.run(list_tools())


def test_mcp_config_ignores_a_hermes_checkout_as_cwd(monkeypatch, tmp_path):
    """A session working inside another Hermes checkout (a lane worktree) must
    still launch the configured tree's server, not the cwd tree's (#66)."""
    import subprocess
    import sys

    from agent.transports import claude_agent_sdk_session_config as config

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    decoy = tmp_path / "lane-checkout"
    server = decoy / "agent" / "transports"
    server.mkdir(parents=True)
    for package in (decoy / "agent", server):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (server / "hermes_tools_mcp_server.py").write_text(
        "import sys\nsys.stderr.write('decoy tree imported')\nsys.exit(7)\n", encoding="utf-8"
    )
    mcp_config = config._build_hermes_tools_mcp_config()
    child_env = dict(mcp_config["env"])
    child_env.update({"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONUTF8": "1"})
    # The real launch, stdin closed: the server exits cleanly at EOF.
    result = subprocess.run(
        [mcp_config["command"], *mcp_config["args"]],
        env=child_env,
        cwd=str(decoy),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert "decoy tree imported" not in result.stderr
    assert "cannot start" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
