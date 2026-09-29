"""Tests for mcp.tools.call JSON-RPC method (MCP 2026 unit M9b).

Asserts:
- unknown session -> error (4001)
- disconnected server -> error (4018)
- tool not exposed by server -> error (4018)
- oversize arguments (>64 KiB) rejected -> error (4000)
- destructive tool routes through the calling session's approval path (prompt invoked, denied -> not executed)
- read-only tool on untrusted server auto-allows per M2 rule without prompting
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tui_gateway.server as server
from tools import mcp_tool
from tools.approval_context import get_current_session_key
from tools.mcp_tool_handlers import _make_tool_handler
from tools.mcp_tool_registration import _record_tool_trust_metadata
from tools.mcp_tool_scope import _server_key
from tools.registry import registry

_CONSENT = "tools.approval_prompt.request_elicitation_consent"


def _call(method: str, params: dict | None = None) -> dict:
    handler = server._methods[method]
    return handler(1, params or {})


@pytest.fixture
def mock_session():
    sid = "sess-m9b-test"
    record = {
        "id": sid,
        "session_key": sid,
        "cwd": "/tmp",
        "profile_home": None,
        "running": False,
    }
    server._sessions[sid] = record
    try:
        yield sid, record
    finally:
        server._sessions.pop(sid, None)


def test_unknown_session_rejected() -> None:
    # Missing session_id
    resp = _call("mcp.tools.call", {"server": "srv", "name": "tool1"})
    assert resp.get("error", {}).get("code") == 4001
    assert "session_id required" in resp["error"]["message"]

    # Session not in memory
    resp = _call("mcp.tools.call", {"session_id": "nonexistent_sid", "server": "srv", "name": "tool1"})
    assert resp.get("error", {}).get("code") == 4001
    assert "not found" in resp["error"]["message"]


def test_disconnected_server_rejected(mock_session) -> None:
    sid, _ = mock_session
    resp = _call("mcp.tools.call", {
        "session_id": sid,
        "server": "nonexistent_srv",
        "name": "tool1",
    })
    assert resp.get("error", {}).get("code") == 4018
    assert "is not connected" in resp["error"]["message"]


def test_tool_not_exposed_rejected(mock_session, monkeypatch) -> None:
    sid, _ = mock_session
    srv_name = "test_server"
    key = _server_key(srv_name)

    mock_task = SimpleNamespace(
        session=SimpleNamespace(),
        _tools=[SimpleNamespace(name="exposed_tool")],
        _is_recycled_stdio=lambda: False,
    )
    monkeypatch.setitem(mcp_tool._servers, key, mock_task)

    resp = _call("mcp.tools.call", {
        "session_id": sid,
        "server": srv_name,
        "name": "unexposed_tool",
    })
    assert resp.get("error", {}).get("code") == 4018
    assert "is not exposed" in resp["error"]["message"]


def test_oversize_arguments_rejected(mock_session) -> None:
    sid, _ = mock_session
    large_payload = "x" * (65 * 1024)
    resp = _call("mcp.tools.call", {
        "session_id": sid,
        "server": "test_server",
        "name": "any_tool",
        "arguments": {"data": large_payload},
    })
    assert resp.get("error", {}).get("code") == 4000
    assert "arguments exceed 64 KiB limit" in resp["error"]["message"]


def test_destructive_tool_denied_routes_through_session(mock_session, monkeypatch) -> None:
    sid, _ = mock_session
    srv_name = "destruct_srv"
    tool_name = "delete_all"
    key = _server_key(srv_name)

    tool_obj = SimpleNamespace(
        name=tool_name,
        annotations={"destructiveHint": True},
    )
    mock_task = SimpleNamespace(
        session=SimpleNamespace(),
        _tools=[tool_obj],
        _is_recycled_stdio=lambda: False,
    )
    monkeypatch.setitem(mcp_tool._servers, key, mock_task)

    # Configure server trust metadata: confirm_destructive = True
    config = {"trust": "full", "confirm_destructive": True}
    _record_tool_trust_metadata(srv_name, config, [tool_obj], key=key)

    # Register handler
    handler = _make_tool_handler(srv_name, tool_name, 5.0)
    prefixed_name = f"mcp__{srv_name}__{tool_name}"
    registry.register(prefixed_name, "mcp", {"type": "function", "function": {"name": prefixed_name}}, handler, override=True)

    session_seen = None

    def fake_consent(*args, **kwargs):
        nonlocal session_seen
        session_seen = get_current_session_key()
        return "deny"

    with patch(_CONSENT, side_effect=fake_consent) as ask:
        resp = _call("mcp.tools.call", {
            "session_id": sid,
            "server": srv_name,
            "name": tool_name,
            "arguments": {"force": True},
        })

    ask.assert_called_once()
    assert session_seen == sid
    assert "result" in resp
    result = resp["result"]
    assert result.get("isError") is True
    assert any("was NOT run" in str(c.get("text", "")) for c in result.get("content", []))


def test_read_only_tool_auto_allows_per_m2_rule(mock_session, monkeypatch) -> None:
    sid, _ = mock_session
    srv_name = "ro_srv"
    tool_name = "fetch_status"
    key = _server_key(srv_name)

    tool_obj = SimpleNamespace(
        name=tool_name,
        annotations={"readOnlyHint": True},
    )
    mock_task = SimpleNamespace(
        session=SimpleNamespace(),
        _tools=[tool_obj],
        _is_recycled_stdio=lambda: False,
    )
    monkeypatch.setitem(mcp_tool._servers, key, mock_task)

    # Configure server as untrusted, but tool is readOnlyHint
    config = {"trust": "untrusted"}
    _record_tool_trust_metadata(srv_name, config, [tool_obj], key=key)

    # Handler succeeds directly
    def custom_handler(args):
        # Verify M2 gate check returns None (no error)
        from tools.mcp_tool_handlers import _trust_gate_check
        gate_err = _trust_gate_check(srv_name, tool_name)
        if gate_err:
            return gate_err
        return json.dumps({"result": "status_ok"})

    prefixed_name = f"mcp__{srv_name}__{tool_name}"
    registry.register(prefixed_name, "mcp", {"type": "function", "function": {"name": prefixed_name}}, custom_handler, override=True)

    with patch(_CONSENT, return_value="deny") as ask:
        resp = _call("mcp.tools.call", {
            "session_id": sid,
            "server": srv_name,
            "name": tool_name,
            "arguments": {},
        })

    ask.assert_not_called()
    assert "result" in resp
    result = resp["result"]
    assert result.get("isError") is not True
    assert result["content"] == [{"type": "text", "text": "status_ok"}]
