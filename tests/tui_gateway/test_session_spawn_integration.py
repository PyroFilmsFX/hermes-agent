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
        with pytest.raises(bridge.SessionSpawnBridgeError, match="permits session.task_create only"):
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
    assert any("session-spawn connection permits session.task_create only" in json.loads(raw).get("error", {}).get("message", "")
               for raw in fake.sent), "a valid scoped ticket must not unlock session.list"


@pytest.mark.parametrize("auth_required", [False, True])
def test_production_web_server_middleware_accepts_only_capability_principal_for_attach(monkeypatch, tmp_path, auth_required):
    import httpx
    from agent.transports import hermes_gateway_session_bridge as bridge
    from hermes_cli import web_server

    capability = bridge.ScopedSessionCapability("cap", "owner-runtime", 0, str(tmp_path))
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda presented: capability if presented == "cap" else None)
    monkeypatch.setattr(web_server.app.state, "auth_required", auth_required, raising=False)

    async def exercise():
        transport = httpx.ASGITransport(app=web_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/session-attach", headers={"X-Hermes-Session-Spawn-Capability": "cap"})

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
