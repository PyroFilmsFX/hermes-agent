"""A session whose transport is the disconnected-WS sentinel must not have its event frames dropped
while a live client is connected.

2026-09-10: a desktop websocket reconnect detached two sessions; the user then ran 7-8 minute
tool-heavy turns in both tabs and saw nothing — the replay ring had collected 266 and 221 deltas
that no socket ever received. prompt.submit only re-binds when the request carries a transport.
"""
from __future__ import annotations

import threading

import pytest

from tui_gateway import server


class _RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


@pytest.fixture()
def live_peer():
    peer = _RecordingTransport()
    server.register_live_transport(peer)
    try:
        yield peer
    finally:
        server.unregister_live_transport(peer)


def _install_session(sid: str, transport) -> None:
    with server._sessions_lock:
        server._sessions[sid] = {
            "transport": transport, "session_key": sid, "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": True,
        }


@pytest.fixture()
def cleanup_sessions():
    yield
    with server._sessions_lock:
        for sid in ("detached-1", "bound-1"):
            server._sessions.pop(sid, None)


def _kinds(peer: _RecordingTransport, sid: str) -> list[str]:
    return [f["params"]["type"] for f in peer.frames if f.get("method") == "event" and f["params"].get("session_id") == sid]


def test_detached_session_events_reach_the_live_client(live_peer, cleanup_sessions):
    _install_session("detached-1", server._detached_ws_transport)
    server._emit("message.start", "detached-1")
    server._emit("message.delta", "detached-1", {"text": "streamed"})
    server._emit("message.complete", "detached-1", {"text": "streamed", "status": "complete"})
    assert _kinds(live_peer, "detached-1") == ["message.start", "message.delta", "message.complete"]
    delta = next(f for f in live_peer.frames if f["params"].get("type") == "message.delta")
    assert delta["params"]["payload"]["text"] == "streamed"


def test_bound_session_is_not_fanned_out(live_peer, cleanup_sessions):
    owner = _RecordingTransport()
    _install_session("bound-1", owner)
    server._emit("message.delta", "bound-1", {"text": "mine"})
    assert _kinds(owner, "bound-1") == ["message.delta"]
    assert _kinds(live_peer, "bound-1") == []


def test_detached_session_without_live_peers_does_not_crash(cleanup_sessions, monkeypatch):
    _install_session("detached-1", server._detached_ws_transport)
    # No live transports registered: the frame falls back to the drop sentinel, silently.
    assert server._emit("message.delta", "detached-1", {"text": "nobody home"}) in (True, False, None)


def test_one_wedged_peer_does_not_block_the_others(cleanup_sessions):
    class _Wedged:
        def write(self, obj):
            raise RuntimeError("socket closed")

        def close(self):
            pass

    bad, good = _Wedged(), _RecordingTransport()
    server.register_live_transport(bad)
    server.register_live_transport(good)
    try:
        _install_session("detached-1", server._detached_ws_transport)
        server._emit("message.delta", "detached-1", {"text": "x"})
        assert _kinds(good, "detached-1") == ["message.delta"]
    finally:
        server.unregister_live_transport(bad)
        server.unregister_live_transport(good)
