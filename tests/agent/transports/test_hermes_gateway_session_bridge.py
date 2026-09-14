from __future__ import annotations

import json
import os
import sys

from agent.transports import hermes_gateway_session_bridge as bridge


class _Socket:
    def __init__(self):
        self.sent = []
        self.messages = [
            json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}}),
            json.dumps({"jsonrpc": "2.0", "id": "reply", "result": {"ok": True}}),
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        self.messages[1] = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}})

    def recv(self, timeout=None):
        return self.messages.pop(0)


def test_bridge_uses_cooperative_attach_and_scopes_rpc(monkeypatch, tmp_path):
    socket = _Socket()
    monkeypatch.setattr(bridge, "ws_connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr(bridge, "_discover_url", lambda owner, home, token="": "ws://127.0.0.1:4311/api/ws?internal=ticket")
    monkeypatch.setattr(
        "hermes_cli.shared_session_attach.discover_attach_url",
        lambda session_id, registry_home: "ws://127.0.0.1:4311/api/ws?internal=ticket",
    )

    client = bridge.HermesGatewaySessionBridge("secret", "owner", tmp_path)
    assert client.is_reachable()
    assert client.create_task_session(cwd="/tmp/project", task="work", title="child", request_id="r1") == {"ok": True}
    params = socket.sent[0]["params"]
    assert "_session_spawn_capability" not in params
    assert params["cwd"] == "/tmp/project"
    assert params["task"] == "work"
    assert "owner_session_id" not in params


def test_capability_revocation_is_fail_closed(monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(server, "_sessions", {"owner": {"session_generation": "g", "session_key": "owner"}})
    token = bridge.issue_scoped_capability("owner")
    assert token
    assert bridge.authorize_scoped_capability(token) is not None
    bridge.revoke_scoped_capability(token)
    assert bridge.authorize_scoped_capability(token) is None


def test_bridge_uses_scoped_handshake_and_never_sends_capability_as_rpc_param(monkeypatch, tmp_path):
    socket = _Socket()
    seen = {}
    def connect(*args, **kwargs):
        seen["url"] = args[0]
        return socket
    monkeypatch.setattr(bridge, "ws_connect", connect)
    monkeypatch.setattr(
        bridge,
        "_discover_url",
        lambda owner, home, token="": "ws://127.0.0.1:4311/api/ws?session_spawn_ticket=one-time",
    )
    client = bridge.HermesGatewaySessionBridge("secret", "owner", tmp_path)
    client.create_task_session(cwd="/tmp/project", task="work", title="child", request_id="r1")
    request = socket.sent[0]
    assert request["method"] == "session.task_create"
    assert "_session_spawn_capability" not in request["params"]
    assert "session_spawn_ticket=one-time" in seen["url"]


def test_mcp_config_is_byte_shape_compatible_without_capability(monkeypatch):
    from agent.transports import claude_agent_sdk_session_config as config

    monkeypatch.setattr(config, "_hermes_repo_root", lambda: "/repo")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    actual = config._build_hermes_tools_mcp_config()
    expected_env = {key: os.environ[key] for key in config._MCP_ENV_ALLOWLIST if os.environ.get(key)}
    expected_env["PYTHONPATH"] = "/repo" + os.pathsep + os.environ.get("PYTHONPATH", "")
    expected = {"type": "stdio", "command": sys.executable, "args": [
        "-m", "agent.transports.hermes_tools_mcp_server", "--profile", "claude-agent-sdk",
    ], "env": expected_env}
    assert actual == expected
    assert "HERMES_SESSION_SPAWN_CAPABILITY" not in json.dumps(actual)


def test_mcp_config_secret_never_enters_command_or_json(monkeypatch):
    from agent.transports import claude_agent_sdk_session_config as config

    monkeypatch.setattr(config, "_hermes_repo_root", lambda: "/repo")
    monkeypatch.setattr(config, "_provider_config", lambda: {"session_spawn": {"enabled": True}})
    monkeypatch.setattr(
        "agent.transports.hermes_gateway_session_bridge.issue_scoped_capability",
        lambda _: "do-not-put-this-in-argv",
    )
    actual = config._build_hermes_tools_mcp_config("owner")
    assert "do-not-put-this-in-argv" not in json.dumps(actual)
    assert "HERMES_SESSION_SPAWN_CAPABILITY_FILE" in actual["env"]


def test_mcp_config_without_issued_capability_preserves_baseline_with_session_id(monkeypatch):
    from agent.transports import claude_agent_sdk_session_config as config

    monkeypatch.setattr(config, "_hermes_repo_root", lambda: "/repo")
    monkeypatch.setattr(config, "_provider_config", lambda: {"session_spawn": {"enabled": True}})
    monkeypatch.setattr("agent.transports.hermes_gateway_session_bridge.issue_scoped_capability", lambda _: None)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    actual = config._build_hermes_tools_mcp_config("owner")
    expected_env = {key: os.environ[key] for key in config._MCP_ENV_ALLOWLIST if os.environ.get(key)}
    expected_env["PYTHONPATH"] = "/repo" + os.pathsep + os.environ.get("PYTHONPATH", "")
    expected_env["HERMES_SESSION_ID"] = "owner"
    expected = {"type": "stdio", "command": sys.executable, "args": [
        "-m", "agent.transports.hermes_tools_mcp_server", "--profile", "claude-agent-sdk",
    ], "env": expected_env}
    assert actual == expected, "no capability must leave the pre-spawn MCP config byte-identical"


def test_capability_expiry_is_terminal_until_explicit_reissue(monkeypatch):
    from tui_gateway import server

    now = [100.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    monkeypatch.setattr(server, "_sessions", {
        "owner": {"session_key": "owner-key", "session_generation": "generation"},
    })
    token = bridge.issue_scoped_capability("owner")
    assert token
    now[0] += bridge._CAPABILITY_TTL_SECONDS + 1
    assert bridge.authorize_scoped_capability(token) is None, (
        "an expired capability must be removed, not silently renewed"
    )


def test_capability_binds_current_owner_key_exactly(monkeypatch):
    from tui_gateway import server

    record = {"session_key": "owner-key", "session_generation": "generation", "profile_home": "/owner"}
    monkeypatch.setattr(server, "_sessions", {"owner": record})
    token = bridge.issue_scoped_capability("owner")
    assert token
    record["session_key"] = "rotated-key"
    assert bridge.authorize_scoped_capability(token) is None, (
        "authorization must reject a capability after owner session-key rotation"
    )


def test_launch_profile_capability_uses_effective_owner_home(monkeypatch, tmp_path):
    from tui_gateway import server

    owner_home = tmp_path / "launch-home"
    owner_home.mkdir()
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: owner_home)
    monkeypatch.setattr(server, "_sessions", {
        "owner": {"session_key": "owner-key", "session_generation": "generation", "profile_home": None},
    })
    token = bridge.issue_scoped_capability("owner")
    record = bridge._capabilities[token]
    assert record.profile_home == str(owner_home.resolve()), (
        "launch-profile capabilities must bind to the canonical effective owner home"
    )


def test_transport_ticket_is_single_outstanding_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])
    capability = bridge.ScopedSessionCapability("cap", "owner", 0)
    first = bridge.issue_scoped_transport_ticket(capability)
    second = bridge.issue_scoped_transport_ticket(capability)
    assert bridge.consume_scoped_transport_ticket(first) is None, "replaced transport tickets must be invalid"
    now[0] += bridge._TRANSPORT_TICKET_TTL_SECONDS + 1
    assert bridge.consume_scoped_transport_ticket(second) is None, "expired transport tickets must be invalid"
