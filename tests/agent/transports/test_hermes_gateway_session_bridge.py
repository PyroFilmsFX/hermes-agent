from __future__ import annotations

import json
import os
import sys

import pytest

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


def test_capability_survives_compression_session_key_rotation(monkeypatch):
    from tui_gateway import server

    record = {"session_key": "owner-key", "session_generation": "generation", "profile_home": "/owner"}
    monkeypatch.setattr(server, "_sessions", {"owner": record})
    token = bridge.issue_scoped_capability("owner")
    assert token
    record["session_key"] = "rotated-key"
    assert bridge.authorize_scoped_capability(token) is not None


def test_capability_rejects_replacement_live_generation(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(server, "_sessions", {
        "owner": {"session_key": "owner-key", "session_generation": "generation"},
    })
    token = bridge.issue_scoped_capability("owner")
    assert token
    server._sessions["owner"] = {"session_key": "owner-key", "session_generation": "replacement"}
    assert bridge.authorize_scoped_capability(token) is None


@pytest.mark.parametrize(("owners", "expected"), [
    ([], "owner lease not found in session registry"),
    ([{"session_id": "owner"}, {"session_id": "owner"}], "owner session has multiple registry leases"),
])
def test_discover_url_reports_registry_match_count_separately(monkeypatch, tmp_path, owners, expected):
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot",
        lambda *_args, **_kwargs: owners,
    )
    try:
        bridge._discover_url("owner", tmp_path, "token")
    except bridge.SessionSpawnBridgeError as exc:
        assert str(exc) == expected
    else:
        raise AssertionError("missing owner lease should fail")


@pytest.mark.parametrize("code", [
    "capability_invalid", "profile_mismatch", "session_identity_mismatch",
    "lease_not_found", "lease_live_session_mismatch",
])
def test_discover_url_reports_gateway_refusal_code(monkeypatch, tmp_path, code):
    from hermes_cli import active_sessions

    monkeypatch.setattr(active_sessions, "active_session_registry_snapshot", lambda *_args, **_kwargs: [{
        "session_id": "owner-key", "lease_id": "lease",
        "metadata": {"live_session_id": "owner", "shared_runtime_url": "http://127.0.0.1:4311"},
    }])

    class _Response:
        status_code = 403
        text = json.dumps({"error": "attach refused", "code": code})

        def raise_for_status(self):
            import httpx

            raise httpx.HTTPStatusError("forbidden", request=None, response=self)

        def json(self):
            return json.loads(self.text)

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: _Response())
    try:
        bridge._discover_url("owner", tmp_path, "token")
    except bridge.SessionSpawnBridgeError as exc:
        assert str(exc) == f"owner gateway attach refused: {code}"
    else:
        raise AssertionError("gateway refusal should fail")


def test_discover_url_separates_http_status_from_transport_failure(monkeypatch, tmp_path):
    from hermes_cli import active_sessions

    monkeypatch.setattr(active_sessions, "active_session_registry_snapshot", lambda *_args, **_kwargs: [{
        "session_id": "owner-key", "lease_id": "lease",
        "metadata": {"live_session_id": "owner", "shared_runtime_url": "http://127.0.0.1:4311"},
    }])
    import httpx

    class _Response:
        status_code = 503

        def raise_for_status(self):
            raise httpx.HTTPStatusError("unavailable", request=None, response=self)

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: _Response())
    with pytest.raises(bridge.SessionSpawnBridgeError, match="owner gateway returned HTTP 503 during attach"):
        bridge._discover_url("owner", tmp_path, "token")

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with pytest.raises(bridge.SessionSpawnBridgeError, match="owner gateway transport failed during attach"):
        bridge._discover_url("owner", tmp_path, "token")


@pytest.mark.parametrize("code", [
    "session_identity_mismatch", "lease_live_session_mismatch", "lease_not_found",
])
def test_session_send_queues_after_authenticated_attach_refusal(monkeypatch, tmp_path, code):
    client = bridge.HermesGatewaySessionBridge("capability", "owner", tmp_path)
    monkeypatch.setattr(
        client, "_rpc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(bridge.SessionSpawnBridgeError(
            f"owner gateway attach refused: {code}", code=code,
            endpoint="http://127.0.0.1:4311")),
    )

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "queued", "message_id": 23}

    import httpx

    seen = {}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return _Response()

    monkeypatch.setattr(httpx, "post", post)
    result = client.send_to_session(target="target", body="hello", request_id="request")
    assert result == {"status": "queued", "message_id": 23}
    assert seen["json"] == {"target": "target", "body": "hello", "request_id": "request"}
    assert seen["headers"] == {"X-Hermes-Session-Spawn-Capability": "capability"}


def test_session_send_does_not_queue_if_sender_capability_cannot_be_authenticated(monkeypatch, tmp_path):
    client = bridge.HermesGatewaySessionBridge("capability", "owner", tmp_path)
    monkeypatch.setattr(
        client, "_rpc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(bridge.SessionSpawnBridgeError(
            "owner gateway attach refused: capability_invalid", code="capability_invalid",
            endpoint="http://127.0.0.1:4311")),
    )

    import httpx

    posted = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: posted.append((args, kwargs)))
    try:
        client.send_to_session(target="target", body="hello")
    except bridge.SessionSpawnBridgeError as exc:
        assert str(exc) == "owner gateway attach refused: capability_invalid"
    else:
        raise AssertionError("unauthenticated fallback should fail")
    assert posted == []


@pytest.mark.parametrize("code", [
    "capability_invalid", "profile_mismatch", "lease_registry_unavailable",
    "owner_lease_not_found", "owner_lease_ambiguous", "attach_transport", "unexpected_refusal",
])
def test_session_send_does_not_queue_after_non_rotation_attach_failure(monkeypatch, tmp_path, code):
    client = bridge.HermesGatewaySessionBridge("capability", "owner", tmp_path)
    monkeypatch.setattr(
        client, "_rpc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(bridge.SessionSpawnBridgeError(
            "owner gateway attach refused", code=code, endpoint="http://127.0.0.1:4311")),
    )
    posted = []
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: posted.append((args, kwargs)))
    with pytest.raises(bridge.SessionSpawnBridgeError, match="owner gateway attach refused"):
        client.send_to_session(target="target", body="hello")
    assert posted == []


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
