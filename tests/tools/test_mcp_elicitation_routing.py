"""Which surface an MCP elicitation goes to (W2 fix P1-2).

The ``mcp.elicitation.request`` event path is taken only when an attached client advertised
``client.capabilities {mcp_elicitation: true}``. A TUI (or any other peer) that did not would drop
the event, and the request would time out declined without the owner ever seeing it, so every
other case takes the approval consent path the CLI / TUI / messaging surfaces answer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("mcp.types")

from tui_gateway import server as gateway_server  # noqa: E402
from tui_gateway import server_requests  # noqa: E402

from tools import mcp_tool_sampling as sampling  # noqa: E402
from tools.mcp_tool_sampling import ElicitationHandler  # noqa: E402


class _Peer:
    """A connected client transport; records every frame written to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True


def _form_params():
    return SimpleNamespace(mode="form", message="pick a name",
                           requested_schema={"type": "object", "properties": {"name": {"type": "string"}}})


@pytest.fixture
def live_peers():
    """Attach peers for one test and always detach them (the registry is process-global)."""
    attached: list = []

    def attach(peer, **capabilities):
        gateway_server.register_live_transport(peer)
        attached.append(peer)
        if capabilities:
            resp = gateway_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "client.capabilities", "params": capabilities},
                            transport=peer)
            assert resp is not None and "result" in resp, resp
        return peer

    with gateway_server._live_transports_lock:
        before = set(gateway_server._live_transports)
    gateway_server._live_transports.clear()
    try:
        yield attach
    finally:
        for peer in attached:
            gateway_server.unregister_live_transport(peer)
        with gateway_server._live_transports_lock:
            gateway_server._live_transports.update(before)


def _run(handler, *, respond_desktop: bool):
    """Drive one elicitation. The emit hook answers a desktop request like the renderer would; the
    consent hook stands in for the approval prompt. Returns (result, emitted payloads, consent calls)."""
    emitted: list[dict] = []
    consent_calls: list[tuple] = []

    def fake_emit(payload):
        emitted.append(payload)
        if respond_desktop:
            sampling.respond_elicitation(payload["request_id"], "accept", {"name": "desk"})

    def fake_consent(*args, **kwargs):
        consent_calls.append((args, kwargs))
        return "accept"

    with patch.object(sampling, "_emit_elicitation_request", fake_emit), \
            patch("tools.approval_prompt.request_elicitation_consent", fake_consent):
        result = asyncio.run(handler(context=None, params=_form_params()))
    return result, emitted, consent_calls


def test_desktop_that_advertised_elicitation_gets_the_event(live_peers) -> None:
    live_peers(_Peer(), server_requests=True, mcp_elicitation=True)
    assert sampling._has_connected_desktop_clients() is True

    result, emitted, consent_calls = _run(ElicitationHandler("srv", {"timeout": 5}), respond_desktop=True)

    assert len(emitted) == 1 and emitted[0]["server"] == "srv"
    assert consent_calls == []
    assert result.action == "accept" and result.content == {"name": "desk"}


def test_tui_only_takes_the_consent_path(live_peers) -> None:
    """A TUI peer answers server→client requests but has no mcp.elicitation.request handler."""
    live_peers(_Peer(), server_requests=True)
    assert sampling._has_connected_desktop_clients() is False

    result, emitted, consent_calls = _run(ElicitationHandler("srv", {"timeout": 5}), respond_desktop=False)

    assert emitted == []
    assert len(consent_calls) == 1
    assert result.action == "accept"


def test_peer_without_capabilities_takes_the_consent_path(live_peers) -> None:
    """Any other attached peer (a compute-host relay, an older desktop build) never said it renders the form."""
    live_peers(_Peer())

    result, emitted, consent_calls = _run(ElicitationHandler("srv", {"timeout": 5}), respond_desktop=False)

    assert emitted == [] and len(consent_calls) == 1
    assert result.action == "accept"


def test_nothing_attached_takes_the_consent_path(live_peers) -> None:
    assert sampling._has_connected_desktop_clients() is False

    result, emitted, consent_calls = _run(ElicitationHandler("srv", {"timeout": 5}), respond_desktop=False)

    assert emitted == [] and len(consent_calls) == 1
    assert result.action == "accept"


def test_desktop_plus_tui_uses_the_event_path(live_peers) -> None:
    live_peers(_Peer(), server_requests=True)
    live_peers(_Peer(), server_requests=True, mcp_elicitation=True)

    result, emitted, consent_calls = _run(ElicitationHandler("srv", {"timeout": 5}), respond_desktop=True)

    assert len(emitted) == 1 and consent_calls == []
    assert result.action == "accept"


def test_disconnect_forgets_the_advertisement(live_peers) -> None:
    peer = live_peers(_Peer(), server_requests=True, mcp_elicitation=True)
    assert sampling._has_connected_desktop_clients() is True
    gateway_server.unregister_live_transport(peer)
    assert server_requests.handles_mcp_elicitation(peer) is False
    assert sampling._has_connected_desktop_clients() is False
