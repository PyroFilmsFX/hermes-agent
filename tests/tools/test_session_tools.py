from __future__ import annotations

import json

from agent.transports import hermes_gateway_session_bridge as bridge
from tools import session_tools
from tools.registry import registry


def test_session_create_is_registered_as_a_service_gated_tool():
    entry = registry.get_entry("session_create")
    assert entry is not None
    assert entry.toolset == "session_spawn"
    assert entry.check_fn is session_tools.check_session_spawn_requirements
    assert session_tools.SESSION_CREATE_SCHEMA["parameters"]["required"] == [
        "cwd", "task", "title", "request_id"
    ]


def test_session_create_returns_clean_bridge_error(monkeypatch):
    class _Bridge:
        @classmethod
        def from_environment(cls):
            raise bridge.SessionSpawnBridgeError("owner gateway is unavailable")

    monkeypatch.setattr(bridge, "HermesGatewaySessionBridge", _Bridge)
    result = json.loads(session_tools.session_create(cwd="/tmp", task="x", title="t", request_id="r"))
    assert result == {"error": "owner gateway is unavailable"}


def test_session_create_rejects_identity_and_permission_overrides(monkeypatch):
    seen = {}

    class _Bridge:
        @classmethod
        def from_environment(cls):
            return cls()

        def create_task_session(self, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

    monkeypatch.setattr(bridge, "HermesGatewaySessionBridge", _Bridge)
    result = json.loads(session_tools.session_create(
        cwd="/tmp", task="x", title="t", request_id="r", profile="other",
        permission_mode="bypassPermissions", denied_tools=[]))
    assert result == {"ok": True}
    assert set(seen) == {"cwd", "task", "title", "request_id"}
