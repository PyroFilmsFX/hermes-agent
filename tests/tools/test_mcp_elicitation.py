"""Tests for the MCP elicitation handler in tools.mcp_tool_sampling.

These tests exercise ElicitationHandler in isolation -- the underlying
approval system and the MCP transport layer are mocked, so no real MCP
server or user input is required.

Tests skip cleanly if the optional `mcp` SDK is not installed (it is an
optional dependency under the `[mcp]` extra).
"""

import asyncio
from unittest.mock import patch

import pytest


pytest.importorskip("mcp.types")

from mcp.types import ElicitResult  # noqa: E402  -- after importorskip

from tools.mcp_tool_sampling import ElicitationHandler  # noqa: E402


def _form_params(message="please confirm", schema=None):
    """Build a stand-in for ElicitRequestFormParams.

    We use a plain object (not the SDK type directly) so the test doesn't
    couple to optional Pydantic validation -- the handler reads fields via
    getattr() and tolerates duck-typed inputs.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        mode="form",
        message=message,
        requested_schema=schema or {},
    )


def _url_params(message="open this url", url="https://example.com/auth", elicitation_id="e1"):
    from types import SimpleNamespace
    return SimpleNamespace(
        mode="url",
        message=message,
        url=url,
        elicitation_id=elicitation_id,
    )




class TestElicitationHandlerFormMode:
    def test_user_accepts_once_returns_accept(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params(
            "authorize a payment of $0.50",
            {"properties": {"approved": {"type": "boolean"}}},
        )

        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            result = asyncio.run(handler(context=None, params=params))

        assert isinstance(result, ElicitResult)
        assert result.action == "accept"
        assert result.content == {}
        assert handler.metrics["accepted"] == 1
        assert handler.metrics["declined"] == 0



    def test_cancel_propagates_through(self):
        """request_elicitation_consent returns 'cancel' when the gateway
        wait times out (resolved=False). The handler should propagate
        that as ElicitResult(action='cancel') so the server can
        distinguish 'no answer' from 'no'."""
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch("tools.approval_prompt.request_elicitation_consent", return_value="cancel"):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "cancel"
        assert handler.metrics["errors"] == 1


class TestElicitationHandlerFailureModes:
    def test_url_mode_is_declined_without_prompting(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _url_params()

        # If the handler tried to prompt, this would raise AssertionError
        # because the side_effect treats the call as a test failure.
        with patch(
            "tools.approval_prompt.request_elicitation_consent",
            side_effect=AssertionError("URL mode must not prompt"),
        ):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"
        assert handler.metrics["declined"] == 1

    def test_exception_in_approval_fails_closed_to_decline(self):
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch(
            "tools.approval_prompt.request_elicitation_consent",
            side_effect=RuntimeError("approval system blew up"),
        ):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"
        assert handler.metrics["errors"] == 1

    def test_timeout_returns_cancel(self, monkeypatch):
        # Shrink the outer grace window so the test budget is just the
        # handler timeout. Default grace is 5s, which makes stall durations
        # tight and the test flaky.
        monkeypatch.setattr(
            ElicitationHandler, "_OUTER_TIMEOUT_GRACE_SECONDS", 0
        )
        # _safe_numeric clamps `timeout` to a minimum of 1s, so the
        # effective wait_for budget is 1s here. Stall longer than that
        # so the wait_for reliably fires TimeoutError.
        handler = ElicitationHandler("pay", {"timeout": 0.05})
        params = _form_params()

        def stall(*_args, **_kwargs):
            import time as _t
            _t.sleep(2)
            return "accept"

        with patch("tools.approval_prompt.request_elicitation_consent", side_effect=stall):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "cancel"
        assert handler.metrics["errors"] == 1




class TestElicitationHandlerContextBridge:
    """The MCP recv-loop task that fires elicitation callbacks does NOT
    inherit the agent's contextvars (HERMES_SESSION_PLATFORM etc.). The
    handler reads the ``call_context`` thunk's snapshot -- a snapshot captured
    by the MCP tool wrapper around ``session.call_tool`` -- and replays
    it before invoking the approval router so gateway-session detection
    survives the task hop. Regression tests for that bridge."""

    def test_captured_context_is_replayed_in_consent_call(self):
        """The captured context's contextvar values must be observable
        when ``request_elicitation_consent`` runs -- otherwise the
        gateway-platform detection in approval.py sees an empty platform
        string and falls back to the CLI path (the bug this fixes)."""
        import contextvars

        probe: contextvars.ContextVar[str] = contextvars.ContextVar(
            "elicitation_test_probe", default=""
        )
        seen: list[str] = []

        def fake_consent(*_args, **_kwargs):
            seen.append(probe.get())
            return "accept"

        token = probe.set("gateway:telegram")
        try:
            captured = contextvars.copy_context()
        finally:
            probe.reset(token)
        assert probe.get() == "", (
            "Sanity check: the probe must be empty outside the captured "
            "context, otherwise the test would pass even without replay."
        )

        handler = ElicitationHandler("pay", {"timeout": 5}, call_context=lambda: captured)
        params = _form_params()

        with patch("tools.approval_prompt.request_elicitation_consent", side_effect=fake_consent):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "accept"
        assert seen == ["gateway:telegram"], (
            f"Expected the captured contextvar to be visible inside the "
            f"consent call; got {seen!r}"
        )

    def test_missing_captured_context_falls_back_to_direct_call(self):
        """With the default call_context (or one whose task has not entered a tool
        call) the handler must still invoke the consent router -- just
        without the contextvar replay. Otherwise CLI/TUI sessions, which
        don't set HERMES_SESSION_PLATFORM, would break."""
        handler = ElicitationHandler("pay", {"timeout": 5})
        params = _form_params()

        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as m:
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "accept"
        assert m.call_count == 1


    def test_pending_call_context_none_does_not_crash(self):
        """The ``call_context`` thunk returns None between tool
        calls. An elicitation arriving in that window must not crash."""
        handler = ElicitationHandler("pay", {"timeout": 5}, call_context=lambda: None)
        params = _form_params()

        with patch("tools.approval_prompt.request_elicitation_consent", return_value="decline"):
            result = asyncio.run(handler(context=None, params=params))

        assert result.action == "decline"


class TestRequestedSchemaFieldName:
    """The requested schema must be read off the *real* SDK model.

    Every other test in this file builds a duck-typed ``SimpleNamespace``
    stand-in for the params object. That keeps them cheap, but it means none
    of them can catch the handler reading a field name the SDK model does not
    actually have -- the stand-in simply has whatever name the test wrote.

    The SDK spells this field ``requestedSchema`` on mcp 1.x and
    ``requested_schema`` on 2.0 (which renamed model fields to snake_case and
    kept camelCase only as a serialization alias, which pydantic does not
    expose to attribute access). Constructing with the camelCase spelling
    works on both -- 2.0 accepts it as the alias -- so this test pins the
    behaviour to the real model on whichever SDK is installed.
    """

    def test_real_sdk_params_schema_reaches_the_consent_description(self):
        from mcp.types import ElicitRequestFormParams

        params = ElicitRequestFormParams(
            message="authorize a payment of $0.50",
            requestedSchema={
                "type": "object",
                "properties": {
                    "card_number": {
                        "type": "string",
                        "description": "card to charge",
                    },
                },
            },
        )
        handler = ElicitationHandler("pay", {"timeout": 5})
        captured: dict = {}

        def _capture(*args, **kwargs):
            captured["description"] = kwargs.get("description") or (
                args[1] if len(args) > 1 else ""
            )
            return "decline"

        with patch("tools.approval_prompt.request_elicitation_consent", _capture):
            asyncio.run(handler(context=None, params=params))

        # An empty schema renders the generic "Approval requested by ..."
        # fallback, so the field name is what proves the schema was read.
        assert "card_number" in (captured.get("description") or ""), captured



# ── Desktop forwarding tests (M4a) ──────────────────────────────────────────

import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

from tools import mcp_tool_discovery as _discovery
from tools import mcp_tool_loop as _loop
from tools.mcp_tool_discovery import register_mcp_servers
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.mcp_tool_sampling import (
    _pending_elicitations,
    call_tool_with_elicitation,
    cancel_all_pending_elicitations,
    validate_elicitation_content,
)
from tui_gateway import server as gateway_server


class MockPeer:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._event = threading.Event()

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        self._event.set()
        return True

    def wait_for_event(self, event_type: str, timeout: float = 5.0) -> dict:
        start = time.time()
        while time.time() - start < timeout:
            for i, f in enumerate(self.frames):
                params = f.get("params", {})
                if params.get("type") == event_type:
                    self.frames.pop(i)
                    return params.get("payload") or {}
            self._event.wait(timeout=0.05)
            self._event.clear()
        raise TimeoutError(f"Event {event_type} not received within {timeout}s")


def _attach_elicitation_client(peer) -> None:
    """Attach *peer* as a desktop client: live transport + client.capabilities {mcp_elicitation: true}.
    Only such a client takes the mcp.elicitation.request event path (W2 fix P1-2)."""
    gateway_server.register_live_transport(peer)
    resp = gateway_server.dispatch({"jsonrpc": "2.0", "id": 0, "method": "client.capabilities",
                                    "params": {"server_requests": True, "mcp_elicitation": True}}, transport=peer)
    assert resp is not None and "result" in resp, resp


@pytest.fixture
def fixture_server_config() -> dict[str, Any]:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "mcp2026_server.py"
    return {
        "command": sys.executable,
        "args": [str(fixture_path), "--stdio"],
        "cwd": str(fixture_path.parent.parent.parent),
        "protocol": "2026-07-28",
    }


@pytest.fixture(autouse=True)
def cleanup():
    yield
    cancel_all_pending_elicitations()
    shutdown_mcp_servers(names={"fixture"})


def test_schema_validator():
    """Unit tests for flat requestedSchema validation."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "score": {"type": "number"},
            "active": {"type": "boolean"},
            "role": {"type": "string", "enum": ["admin", "user"]},
        },
        "required": ["name", "role"],
    }
    # Valid
    ok, err = validate_elicitation_content({"name": "Alice", "role": "admin", "age": 30, "score": 9.5, "active": True}, schema)
    assert ok and err is None

    # Missing required
    ok, err = validate_elicitation_content({"role": "admin"}, schema)
    assert not ok and "Missing required field" in err

    # Wrong string type
    ok, err = validate_elicitation_content({"name": 123, "role": "admin"}, schema)
    assert not ok and "expected string" in err

    # Wrong integer type (float)
    ok, err = validate_elicitation_content({"name": "Bob", "role": "user", "age": 25.5}, schema)
    assert not ok and "expected integer" in err

    # Wrong integer type (bool)
    ok, err = validate_elicitation_content({"name": "Bob", "role": "user", "age": True}, schema)
    assert not ok and "expected integer" in err

    # Wrong boolean type
    ok, err = validate_elicitation_content({"name": "Bob", "role": "user", "active": "yes"}, schema)
    assert not ok and "expected boolean" in err

    # Enum mismatch
    ok, err = validate_elicitation_content({"name": "Bob", "role": "guest"}, schema)
    assert not ok and "not in enum" in err


def test_form_roundtrip_returns_typed_content(fixture_server_config: dict) -> None:
    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None and server.session is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )

        payload = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        req_id = payload["request_id"]
        assert payload["server"] == "fixture"
        assert payload["mode"] == "form"
        assert payload["message"] == "Provide a short answer"
        assert "properties" in payload["requestedSchema"]

        resp = gateway_server.handle_request({
            "id": 1,
            "method": "mcp.elicitation.respond",
            "params": {
                "request_id": req_id,
                "action": "accept",
                "content": {"answer": "typed-42"},
            },
        })
        assert "result" in resp and resp["result"]["ok"] is True

        tool_res = fut.result(timeout=5.0)
        parsed = json.loads(tool_res.content[0].text)
        assert parsed == {"action": "accept", "answer": "typed-42"}
    finally:
        gateway_server.unregister_live_transport(peer)


def test_invalid_content_rejected_and_request_stays_open(fixture_server_config: dict) -> None:
    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )

        payload = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        req_id = payload["request_id"]

        # 1. Missing required field
        err_resp1 = gateway_server.handle_request({
            "id": 2,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id, "action": "accept", "content": {}},
        })
        assert "error" in err_resp1
        assert err_resp1["error"]["code"] == 4000
        assert "Missing required field" in err_resp1["error"]["message"]
        assert req_id in _pending_elicitations

        # 2. Invalid type (int instead of string)
        err_resp2 = gateway_server.handle_request({
            "id": 3,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id, "action": "accept", "content": {"answer": 999}},
        })
        assert "error" in err_resp2
        assert err_resp2["error"]["code"] == 4000
        assert "expected string" in err_resp2["error"]["message"]
        assert req_id in _pending_elicitations

        # 3. Valid answer unblocks the still-open request
        ok_resp = gateway_server.handle_request({
            "id": 4,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id, "action": "accept", "content": {"answer": "valid_answer"}},
        })
        assert "result" in ok_resp and ok_resp["result"]["ok"] is True

        tool_res = fut.result(timeout=5.0)
        parsed = json.loads(tool_res.content[0].text)
        assert parsed == {"action": "accept", "answer": "valid_answer"}
    finally:
        gateway_server.unregister_live_transport(peer)


def test_decline_and_cancel(fixture_server_config: dict) -> None:
    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None

        # Test decline
        fut1 = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )
        p1 = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        resp1 = gateway_server.handle_request({
            "id": 10,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": p1["request_id"], "action": "decline"},
        })
        assert "result" in resp1
        res1 = fut1.result(timeout=5.0)
        assert json.loads(res1.content[0].text)["action"] == "decline"

        # Test cancel
        fut2 = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )
        p2 = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        resp2 = gateway_server.handle_request({
            "id": 11,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": p2["request_id"], "action": "cancel"},
        })
        assert "result" in resp2
        res2 = fut2.result(timeout=5.0)
        assert json.loads(res2.content[0].text)["action"] == "cancel"
    finally:
        gateway_server.unregister_live_transport(peer)


def test_no_connected_client_uses_consent_path(fixture_server_config: dict) -> None:
    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    with gateway_server._live_transports_lock:
        assert len(gateway_server._live_transports) == 0

    loop = _loop._running_loop()
    assert loop is not None
    with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as mock_consent:
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )
        tool_res = fut.result(timeout=5.0)
        assert mock_consent.call_count == 1
        parsed = json.loads(tool_res.content[0].text)
        assert parsed["action"] == "accept"


def test_elicitation_timeout_declines(fixture_server_config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    # Short timeout config (0.2s)
    monkeypatch.setattr("tools.mcp_tool_sampling._get_elicitation_timeout", lambda fallback=300.0: 0.2)

    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None
        start = time.time()
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )
        peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        # Intentionally do not respond -> should time out and decline
        res = fut.result(timeout=5.0)
        elapsed = time.time() - start
        assert elapsed < 3.0
        assert json.loads(res.content[0].text)["action"] == "decline"
    finally:
        gateway_server.unregister_live_transport(peer)


def test_shutdown_cancels_pending(fixture_server_config: dict) -> None:
    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_form"), loop
        )
        peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        assert len(_pending_elicitations) == 1

        # Shutdown while request is pending
        shutdown_mcp_servers(names={"fixture"})

        # The pending request is cancelled and unblocked
        with pytest.raises((Exception, asyncio.CancelledError)):
            fut.result(timeout=5.0)
        assert len(_pending_elicitations) == 0
    finally:
        gateway_server.unregister_live_transport(peer)


def test_url_mode_forwards_url_and_never_opens_it(fixture_server_config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure Hermes never opens URLs itself
    import webbrowser
    def _fail_open(*args, **kwargs):
        raise AssertionError("webbrowser.open called; Hermes must never open URLs itself")
    monkeypatch.setattr(webbrowser, "open", _fail_open)

    register_mcp_servers({"fixture": fixture_server_config})
    server = _discovery._get_connected_server_for_call("fixture")
    assert server is not None

    peer = MockPeer()
    _attach_elicitation_client(peer)
    try:
        loop = _loop._running_loop()
        assert loop is not None

        # 1. Accept in URL mode
        fut = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_url"), loop
        )
        payload = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        req_id = payload["request_id"]
        assert payload["mode"] == "url"
        assert payload["url"] == "https://example.com/fixture"
        assert payload["message"] == "Continue on the fixture site"

        # Action cancel rejected for URL mode
        err_cancel = gateway_server.handle_request({
            "id": 20,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id, "action": "cancel"},
        })
        assert "error" in err_cancel
        assert err_cancel["error"]["code"] == 4000

        # Action accept allowed
        resp = gateway_server.handle_request({
            "id": 21,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id, "action": "accept"},
        })
        assert "result" in resp and resp["result"]["ok"] is True

        tool_res = fut.result(timeout=5.0)
        assert tool_res.content[0].text == "accept"

        # 2. Decline in URL mode
        fut_dec = asyncio.run_coroutine_threadsafe(
            call_tool_with_elicitation(server, "elicit_url"), loop
        )
        payload_dec = peer.wait_for_event("mcp.elicitation.request", timeout=5.0)
        req_id_dec = payload_dec["request_id"]
        gateway_server.handle_request({
            "id": 22,
            "method": "mcp.elicitation.respond",
            "params": {"request_id": req_id_dec, "action": "decline"},
        })
        tool_res_dec = fut_dec.result(timeout=5.0)
        assert tool_res_dec.content[0].text == "decline"
    finally:
        gateway_server.unregister_live_transport(peer)
