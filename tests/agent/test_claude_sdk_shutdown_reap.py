"""Backend shutdown must fence new SDK CLI spawns and reap the live ones.

Incident 2026-09-25 16:31:23: while the desktop backend (``hermes serve``) was
shutting down, a worker thread spawned a fresh Claude CLI child. The backend
exited without terminating it, so the child ran on as an orphan (ppid=1),
replaying a session turn and sending duplicate peer messages.

Two contracts are pinned here, with fake processes (no real ``claude``):

(a) once shutdown has begun, ``ensure_started()`` refuses with a clear
    "shutting down" outcome and never builds/connects a client;
(b) the shutdown reap terminates every live registered CLI child
    (SIGTERM, then SIGKILL after a bounded grace) and waits on each.
"""

from __future__ import annotations

import threading

import psutil
import pytest

from agent.transports import claude_agent_sdk_session_child as C
from tests.agent.claude_sdk_fakes import (
    _FakeClient,
    _make_session,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


@pytest.fixture(autouse=True)
def _fresh_shutdown_state():
    C._reset_sdk_shutdown_state_for_tests()
    yield
    C._reset_sdk_shutdown_state_for_tests()


class _FakeProcess:
    """psutil.Process stand-in; ``stubborn`` ignores SIGTERM."""

    def __init__(self, pid: int, *, stubborn: bool = False) -> None:
        self.pid = pid
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False
        self.waits: list[float] = []
        self._alive = True

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def is_running(self) -> bool:
        return self._alive

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._alive:
            raise psutil.TimeoutExpired(timeout, pid=self.pid)
        return 0


class _ProcClient(_FakeClient):
    """Fake SDK client exposing a CLI pid the way the real transport does."""

    def __init__(self, pid: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._transport = type("T", (), {})()
        self._transport._process = type("P", (), {"pid": pid})()


def _session_with_pid(pid: int):
    holder: dict = {}

    def factory(options=None):
        holder["client"] = _ProcClient(pid, options=options)
        return holder["client"]

    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory
    )
    return session, holder


# ---------- (a) no spawn after shutdown begins ----------


def test_ensure_started_refused_once_shutdown_begins():
    session, holder = _make_session()
    C.begin_sdk_shutdown()

    with pytest.raises(C.SdkShuttingDownError, match="shutting down"):
        session.ensure_started()

    assert "client" not in holder, "a client (CLI process) was built during shutdown"
    assert session._client is None
    assert session._loop_thread is None


def test_run_turn_returns_shutting_down_outcome_without_spawning():
    session, holder = _make_session()
    C.begin_sdk_shutdown()

    result = session.run_turn("hello")

    assert "client" not in holder
    assert result.error and "shutting down" in result.error
    assert result.api_call_made is False
    # Not a "rotating" retirement: the runtime must not rebuild and retry.
    assert not getattr(result, "retired_before_query", False)
    assert getattr(result, "fatal_reason", None) is None


def test_shutdown_racing_client_build_never_connects():
    """Shutdown begins after the pre-check but before connect(): no spawn."""
    connected = threading.Event()
    holder: dict = {}

    class _RacingClient(_FakeClient):
        async def connect(self):
            connected.set()

    def factory(options=None):
        C.begin_sdk_shutdown()  # the race: fence goes up mid-startup
        holder["client"] = _RacingClient(options=options)
        return holder["client"]

    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory
    )
    with pytest.raises(C.SdkShuttingDownError):
        session.ensure_started()

    assert not connected.is_set(), "connect() spawned the CLI after shutdown began"
    assert session._client is None
    session.close()


def test_normal_startup_unaffected_before_shutdown():
    session, holder = _make_session()
    try:
        assert session.ensure_started() == "pending"
        assert "client" in holder
    finally:
        session.close()


# ---------- (b) shutdown reaps live children ----------


def test_reap_terminates_live_children_and_waits(monkeypatch):
    session, _ = _session_with_pid(4242)
    assert session.ensure_started() == "pending"
    proc = _FakeProcess(4242)
    monkeypatch.setattr(
        C, "_own_sdk_child_process", lambda pid: proc if pid == 4242 else None
    )

    reaped = C.reap_sdk_children(grace=0.5)

    assert proc.terminated is True
    assert proc.killed is False
    assert proc.waits, "reap must wait on the terminated child"
    assert reaped == 1
    assert C.sdk_shutdown_begun() is True
    session.close()


def test_reap_escalates_to_sigkill_after_bounded_grace(monkeypatch):
    session, _ = _session_with_pid(5151)
    assert session.ensure_started() == "pending"
    proc = _FakeProcess(5151, stubborn=True)
    monkeypatch.setattr(
        C, "_own_sdk_child_process", lambda pid: proc if pid == 5151 else None
    )

    import time

    started = time.monotonic()
    reaped = C.reap_sdk_children(grace=0.2)
    elapsed = time.monotonic() - started

    assert proc.terminated is True
    assert proc.killed is True, "stubborn child not SIGKILLed after grace"
    assert proc.is_running() is False
    assert reaped == 1
    assert elapsed < 5.0, "reap grace is not bounded"
    session.close()


def test_reap_covers_every_registered_session(monkeypatch):
    first, _ = _session_with_pid(7001)
    second, _ = _session_with_pid(7002)
    assert first.ensure_started() == "pending"
    assert second.ensure_started() == "pending"
    procs = {7001: _FakeProcess(7001), 7002: _FakeProcess(7002, stubborn=True)}
    monkeypatch.setattr(C, "_own_sdk_child_process", lambda pid: procs.get(pid))

    assert C.reap_sdk_children(grace=0.2) == 2

    assert all(p.terminated for p in procs.values())
    assert procs[7002].killed is True
    first.close()
    second.close()


def test_closed_session_is_not_reaped(monkeypatch):
    session, _ = _session_with_pid(8080)
    assert session.ensure_started() == "pending"
    session.close()
    seen: list[int] = []
    monkeypatch.setattr(C, "_own_sdk_child_process", lambda pid: seen.append(pid))

    assert C.reap_sdk_children(grace=0.1) == 0
    assert seen == []


# ---------- wiring: the backend shutdown paths run the fence + reap ----------


def test_tui_gateway_shutdown_fences_before_flush_and_reaps(monkeypatch):
    import tui_gateway.server as server

    calls: list[str] = []
    monkeypatch.setattr(C, "begin_sdk_shutdown", lambda: calls.append("fence"))
    monkeypatch.setattr(
        C, "reap_sdk_children", lambda *a, **k: calls.append("reap") or 0
    )
    monkeypatch.setattr(server, "_flush_sessions_before_exit", lambda: calls.append("flush"))
    monkeypatch.setattr(server, "_release_gateway_wake_owner", lambda: None)
    monkeypatch.setattr(server, "_sessions", {})

    server._shutdown_sessions()

    assert calls == ["fence", "flush", "reap"]
