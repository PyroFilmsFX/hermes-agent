"""Real loopback HTTP/WebSocket handshake coverage for session spawning."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(not hasattr(asyncio, "run"), reason="asyncio unavailable")
def test_real_gateway_attach_route_and_bridge_client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    import uvicorn

    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    live = {"session_key": "owner-stored", "session_generation": "gen-owner", "profile_home": str(tmp_path),
            "cwd": str(tmp_path), "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p"),
            "transport": server._stdio_transport}
    old_sessions = server._sessions
    server._sessions = {"owner-runtime": live}
    from hermes_cli.active_sessions import try_acquire_active_session
    lease, refusal = try_acquire_active_session(
        session_id="owner-stored", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "owner-runtime", "shared_runtime_url": f"http://127.0.0.1:{port}"})
    assert refusal is None and lease is not None
    cap = bridge.issue_scoped_capability("owner-runtime")
    assert cap
    monkeypatch.setattr(handoff := __import__("tui_gateway.session_task_handoff", fromlist=["x"]), "_dispatch_existing",
                        lambda method, params, request_id, transport=None: (
                            {"session_id": "child-runtime", "stored_session_id": "child-stored"}
                            if method == "session.create" else {}))
    # Spawning ships disabled by default (open security findings); this test exercises the
    # opted-in path, so enable the policy explicitly.
    monkeypatch.setattr(handoff, "_policy", lambda profile_home=None: {
        "enabled": True, "max_children_per_root": 3, "max_depth": 2, "rate_per_minute": 5})
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not running.started and time.time() < deadline:
            time.sleep(0.01)
        client = bridge.HermesGatewaySessionBridge(cap, "owner-runtime", tmp_path)
        # Discovery and the scoped websocket are real HTTP/WS traffic; only the
        # final session handlers are isolated so this test remains model-free.
        result = client.create_task_session(cwd=str(tmp_path), task="work", title="child", request_id="r1")
        assert result["stored_session_id"] == "child-stored"
        with pytest.raises(bridge.SessionSpawnBridgeError, match="permits session.task_create and session.send only"):
            client._rpc("session.list", {})
    finally:
        running.should_exit = True
        thread.join(timeout=5)
        lease.release()
        server._sessions = old_sessions


@pytest.mark.parametrize("ticket", [None, "", "invalid"])
def test_spawn_websocket_rejects_missing_or_invalid_ticket_before_accept(ticket, monkeypatch):
    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import ws

    class FakeWebSocket:
        query_params = {} if ticket is None else {"session_spawn_ticket": ticket}
        client = SimpleNamespace(host="127.0.0.1", port=1)
        headers = {"host": "127.0.0.1"}
        accepted = False
        closed = None

        async def accept(self, **_kwargs):
            self.accepted = True

        async def close(self, **kwargs):
            self.closed = kwargs

        async def send_text(self, raw):
            self.sent.append(raw)

    monkeypatch.setattr(bridge, "consume_scoped_transport_ticket", lambda value: None)
    fake = FakeWebSocket()
    asyncio.run(ws.handle_ws(fake, required_spawn_scope=True))
    assert not fake.accepted, "spawn WebSocket auth must complete before accept"
    assert fake.closed and fake.closed["code"] == 4401


def test_spawn_websocket_scope_rejects_session_list_even_with_valid_ticket(monkeypatch):
    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import ws

    class FakeWebSocket:
        query_params = {"session_spawn_ticket": "valid"}
        client = SimpleNamespace(host="127.0.0.1", port=1)
        headers = {"host": "127.0.0.1"}
        sent = []

        async def accept(self, **_kwargs):
            self.accepted = True

        async def close(self, **kwargs):
            self.closed = kwargs

        async def send_text(self, raw):
            self.sent.append(raw)

        async def receive_text(self):
            if getattr(self, "received", False):
                from starlette.websockets import WebSocketDisconnect
                raise WebSocketDisconnect(code=1000)
            self.received = True
            return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.list", "params": {}})

    capability = bridge.ScopedSessionCapability("cap", "owner", 0)
    monkeypatch.setattr(bridge, "consume_scoped_transport_ticket", lambda value: capability)
    monkeypatch.setattr(ws.server, "resolve_skin", lambda: {})
    monkeypatch.setattr(ws.server, "register_live_transport", lambda transport: None)
    monkeypatch.setattr(ws.server, "unregister_live_transport", lambda transport: None)
    monkeypatch.setattr(ws.server, "_ensure_skin_watcher", lambda: None)
    fake = FakeWebSocket()
    asyncio.run(ws.handle_ws(fake, required_spawn_scope=True))
    assert any("session-spawn connection permits session.task_create and session.send only" in json.loads(raw).get("error", {}).get("message", "")
               for raw in fake.sent), "a valid scoped ticket must not unlock session.list"


@pytest.mark.parametrize("auth_required", [False, True])
@pytest.mark.parametrize("path", ["/api/session-attach", "/api/session-send-queue"])
def test_production_web_server_middleware_accepts_only_capability_principal_for_session_bridge(
    monkeypatch, tmp_path, auth_required, path,
):
    import httpx
    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_cli import web_server

    capability = bridge.ScopedSessionCapability("cap", "owner-runtime", 0, str(tmp_path))
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda presented: capability if presented == "cap" else None)
    monkeypatch.setattr(bridge, "authorize_scoped_capability_for_queue",
                        lambda presented: capability if presented == "cap" else None)
    monkeypatch.setattr(web_server.app.state, "auth_required", auth_required, raising=False)

    async def exercise():
        transport = httpx.ASGITransport(app=web_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            headers = {"X-Hermes-Session-Spawn-Capability": "cap"}
            if path == "/api/session-attach":
                return await client.get(path, headers=headers)
            return await client.post(path, json={"target": "target", "body": "hello"}, headers=headers)

    response = asyncio.run(exercise())
    assert response.status_code != 401, "real web_server middleware must admit a capability-scoped attach principal"


def test_session_create_does_not_replace_missing_validated_cwd_with_completion_fallback(monkeypatch):
    from tui_gateway import server

    cwd = os.path.realpath(os.getcwd())
    stat = os.stat(cwd)
    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {
        "_cwd_identity": {"realpath": cwd, "st_dev": stat.st_dev, "st_ino": stat.st_ino},
    }})
    assert response["error"]["code"] == 4004, (
        "a validated spawn cwd is required; session.create must not fall back to the launch cwd"
    )


def test_attach_route_is_reachable_behind_the_dashboard_spa_catch_all(tmp_path):
    """``hermes serve`` mounts the SPA catch-all before the gateway module registers the attach
    route; the bridge probe must still reach the route, not the catch-all's API 404."""
    from fastapi import FastAPI
    import uvicorn

    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_cli.web_server_dashboard import mount_spa
    from tui_gateway import server

    app = FastAPI()
    mount_spa(app)  # the serve order: SPA first, gateway import later from the lifespan
    server.register_session_spawn_routes(app)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    live = {"session_key": "owner-stored", "session_generation": "gen-owner", "profile_home": str(tmp_path),
            "cwd": str(tmp_path), "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p"),
            "transport": server._stdio_transport}
    old_sessions = server._sessions
    server._sessions = {"owner-runtime": live}
    from hermes_cli.active_sessions import try_acquire_active_session
    lease, refusal = try_acquire_active_session(
        session_id="owner-stored", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "owner-runtime", "shared_runtime_url": f"http://127.0.0.1:{port}"})
    assert refusal is None and lease is not None
    cap = bridge.issue_scoped_capability("owner-runtime")
    assert cap
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not running.started and time.time() < deadline:
            time.sleep(0.01)
        assert bridge.HermesGatewaySessionBridge(cap, "owner-stored", tmp_path).is_reachable() is True
    finally:
        running.should_exit = True
        thread.join(timeout=5)
        server._sessions = old_sessions
        bridge.revoke_scoped_capability(cap)


@pytest.mark.parametrize(("failure", "expected_code"), [
    ("capability", "capability_invalid"),
    ("profile", "profile_mismatch"),
    ("session", "session_identity_mismatch"),
    ("registry", "lease_registry_unavailable"),
    ("lease", "lease_not_found"),
    ("live", "lease_live_session_mismatch"),
])
def test_attach_route_returns_distinct_refusal_codes(monkeypatch, tmp_path, failure, expected_code):
    import httpx
    from fastapi import FastAPI
    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    live = {"session_key": "owner-key", "session_generation": "generation", "profile_home": str(tmp_path)}
    monkeypatch.setattr(server, "_sessions", {"owner-runtime": live})
    token = bridge.issue_scoped_capability("owner-runtime")
    assert token
    lease_rows = [{"session_id": "owner-key", "lease_id": "lease",
                   "metadata": {"live_session_id": "owner-runtime"}}]
    def registry_snapshot(*_args, **_kwargs):
        if failure == "registry":
            raise OSError("registry unavailable")
        if failure == "lease":
            return []
        if failure == "live":
            return [{**lease_rows[0], "metadata": {"live_session_id": "other-runtime"}}]
        return lease_rows

    monkeypatch.setattr("hermes_cli.active_sessions.active_session_registry_snapshot", registry_snapshot)

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/session-attach", params={
                "session_id": "other-key" if failure == "session" else "owner-key",
                "lease_id": "lease", "profile_home": str(tmp_path / "wrong") if failure == "profile" else str(tmp_path),
            }, headers={"X-Hermes-Session-Spawn-Capability": "invalid" if failure == "capability" else token})

    response = asyncio.run(exercise())
    assert response.status_code == 403
    assert response.json()["code"] == expected_code


def test_attach_accepts_rotated_session_key_for_same_live_generation(monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    live = {"session_key": "initial-key", "session_generation": "generation", "profile_home": str(tmp_path)}
    monkeypatch.setattr(server, "_sessions", {"owner-runtime": live})
    token = bridge.issue_scoped_capability("owner-runtime")
    assert token
    live["session_key"] = "compressed-key"
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot",
        lambda *_args, **_kwargs: [{"session_id": "compressed-key", "lease_id": "lease",
                                    "metadata": {"live_session_id": "owner-runtime"}}],
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/session-attach", params={
                "session_id": "compressed-key", "lease_id": "lease", "profile_home": str(tmp_path),
            }, headers={"X-Hermes-Session-Spawn-Capability": token})

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert response.json()["session_id"] == "compressed-key"


def test_authenticated_attach_refusal_fallback_queues_durably(monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_state import SessionDB
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("target", "desktop")
    live = {"session_key": "owner-key", "session_generation": "generation", "profile_home": None}
    monkeypatch.setattr(server, "_sessions", {"owner-runtime": live})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    token = bridge.issue_scoped_capability("owner-runtime")
    assert token

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/api/session-send-queue", json={"target": "target", "body": "hello"},
                                     headers={"X-Hermes-Session-Spawn-Capability": token})

    try:
        response = asyncio.run(exercise())
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        [row] = db.peer_mailbox_pending("target")
        assert row["from_session_id"] == "owner-key"
        assert row["body"] == "hello"
        from tools import session_tools
        monkeypatch.setattr(session_tools, "session_send_enabled", lambda: False)
        disabled = asyncio.run(exercise())
        assert disabled.status_code == 403
        assert disabled.json()["code"] == "session_send_disabled"
        assert len(db.peer_mailbox_pending("target")) == 1
        unauthorized = asyncio.run(_post_queue(app, "invalid"))
        assert unauthorized.status_code == 403
        assert len(db.peer_mailbox_pending("target")) == 1
    finally:
        db.close()


async def _post_queue(app, token):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post("/api/session-send-queue", json={"target": "target", "body": "forged"},
                                 headers={"X-Hermes-Session-Spawn-Capability": token})


def _principal_request(path, token):
    return SimpleNamespace(url=SimpleNamespace(path=path),
                           headers={"X-Hermes-Session-Spawn-Capability": token}, state=SimpleNamespace())


def test_expired_capability_401s_attach_but_queues_durably_with_owner_warning(monkeypatch, tmp_path, caplog):
    """b3-30: a >1 h old SDK session's capability 401'd attach and session_send reported ``failed``.

    The production middleware must still refuse the attach (full authority is expired) but admit the
    queue-only route, which durably enqueues and logs the refusal in the owner's (backend) log."""
    import logging

    import httpx
    from fastapi import FastAPI
    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_cli import web_server
    from hermes_state import SessionDB
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("target", "desktop")
    live = {"session_key": "owner-key", "session_generation": "generation", "profile_home": None}
    monkeypatch.setattr(server, "_sessions", {"owner-runtime": live})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    now = [1000.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    token = bridge.issue_scoped_capability("owner-runtime")
    assert token
    now[0] += bridge._CAPABILITY_TTL_SECONDS + 5
    try:
        assert web_server._session_attach_capability_principal(
            _principal_request("/api/session-attach", token)) is False, "expired capability keeps attach closed"
        assert web_server._session_attach_capability_principal(
            _principal_request("/api/session-send-queue", token)) is True, "queue-only route admits it"
        assert web_server._session_attach_capability_principal(
            _principal_request("/api/session-send-queue", "forged")) is False
        with caplog.at_level(logging.WARNING):
            response = asyncio.run(_post_queue(app, token))
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        [row] = db.peer_mailbox_pending("target")
        assert row["from_session_id"] == "owner-key"
        assert any(record.levelno == logging.WARNING and "target" in record.getMessage()
                   and "owner-key" in record.getMessage() for record in caplog.records), caplog.text
        bridge.revoke_scoped_capability(token)
        assert asyncio.run(_post_queue(app, token)).status_code == 403
        assert len(db.peer_mailbox_pending("target")) == 1
    finally:
        db.close()


def test_queue_route_pins_sender_to_the_capability_owner_and_marks_the_row(monkeypatch, tmp_path):
    """b3-30 follow-up (a)+(b): body-supplied sender identity is ignored; the row records the capability
    owner's generation/profile so delivery can re-validate it."""
    import httpx
    from fastapi import FastAPI
    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_state import SessionDB
    from tui_gateway import server

    app = FastAPI()
    server.register_session_spawn_routes(app)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("target", "desktop")
    db.create_session("victim", "desktop")
    live = {"session_key": "owner-key", "session_generation": "generation", "profile_home": None}
    monkeypatch.setattr(server, "_sessions", {"owner-runtime": live})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    token = bridge.issue_scoped_capability("owner-runtime")

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/api/session-send-queue", json={
                "target": "target", "body": "hello", "from_session_id": "victim", "from_label": "victim",
                "from": "victim", "sender": "victim",
            }, headers={"X-Hermes-Session-Spawn-Capability": token})

    try:
        response = asyncio.run(exercise())
        assert response.json()["status"] == "queued"
        [row] = db.peer_mailbox_pending("target")
        assert row["from_session_id"] == "owner-key"
        assert "victim" not in str(row.get("from_label") or "")
        marker = json.loads(row["sender_auth"])
        assert marker["owner_session_id"] == "owner-runtime"
        assert marker["session_generation"] == "generation"
        assert marker["profile_home"] == bridge._capabilities[token].profile_home
    finally:
        bridge.revoke_scoped_capability(token)
        db.close()


def test_sdk_turn_start_rotates_the_published_capability(monkeypatch, tmp_path):
    """An SDK session older than the TTL gets a fresh capability in its file at each turn start."""
    from agent.transports import claude_agent_sdk_session_config as config
    from agent.transports import hermes_gateway_session_bridge as bridge
    from tui_gateway import server

    monkeypatch.setattr(server, "_sessions", {"owner": {"session_generation": "g", "session_key": "owner"}})
    now = [1000.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    path = tmp_path / "runtime" / "session-spawn" / "owner.cap"
    old = bridge.issue_scoped_capability("owner")
    config._publish_session_spawn_capability(path, old)
    now[0] += bridge._CAPABILITY_TTL_SECONDS + 60
    assert bridge.authorize_scoped_capability(old) is None
    fresh = config.refresh_session_spawn_capability("owner", path)
    assert fresh and fresh != old
    assert path.read_text(encoding="utf-8") == fresh
    assert bridge.authorize_scoped_capability(fresh) is not None
    assert config.refresh_session_spawn_capability("", path) is None
    assert config.refresh_session_spawn_capability("owner", None) is None
    now[0] += bridge._ROTATION_GRACE_SECONDS + 1
    assert bridge.authorize_scoped_capability_for_queue(old) is None, "rotated-out token is revoked after grace"


def test_sdk_session_rotates_capability_at_turn_boundaries(monkeypatch, tmp_path):
    from agent.transports import claude_agent_sdk_session as sdk_session
    from agent.transports import claude_agent_sdk_session_config as config

    calls = []
    monkeypatch.setattr(config, "refresh_session_spawn_capability",
                        lambda owner, path: calls.append((owner, path)) or "fresh")
    session = sdk_session.ClaudeAgentSdkSession.__new__(sdk_session.ClaudeAgentSdkSession)
    assert session.refresh_session_spawn_capability() is False, "no published capability, nothing to rotate"
    session._session_spawn_capability = ("owner", str(tmp_path / "owner.cap"))
    session._session_spawn_capability_refreshed_at = time.monotonic()
    assert session.refresh_session_spawn_capability() is False, "just published at spawn"
    session._session_spawn_capability_refreshed_at = time.monotonic() - 3600
    assert session.refresh_session_spawn_capability() is True
    assert calls == [("owner", str(tmp_path / "owner.cap"))]
