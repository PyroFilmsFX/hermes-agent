"""M8 task poller: runs only on the MCP loop and never runs forever (W2 fix P1-3).

- The poller task is created on ``_mcp_loop`` even when the caller is on another running loop
  (``@resource`` expansion runs ``_ensure_mcp_loop`` on the agent/gateway loop).
- A row whose server stays gone past a bounded window is parked with ``expired_at`` (kept, never
  deleted) and the poller exits once nothing is pollable.
- A server that reconnects inside the window resumes its parked row.
- A failing pending-task check stops the poller with a log line instead of looping.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest


class _CompletedTaskSession:
    async def send_request(self, request, result_type):
        if request.method == "tasks/get":
            from mcp import types
            return types.GetTaskResult(taskId=request.params.task_id, status="completed",
                                       createdAt="2026-01-01T00:00:00Z",
                                       lastUpdatedAt="2026-01-01T00:00:01Z", ttl=60)
        return {"content": [{"type": "text", "text": "late output"}]}


class _TaskServer:
    def __init__(self, name: str):
        self.name = name
        self.session = _CompletedTaskSession()
        self._rpc_lock = asyncio.Lock()


@pytest.fixture
def task_env(monkeypatch, tmp_path):
    """Isolated profile DB, no mailbox drain, fast poll interval, and no background poller started
    implicitly (tests drive the poller themselves)."""
    from tools import mcp_tool_loop as loop

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tui_gateway.session_mailbox.schedule_drain", lambda *args: None)
    monkeypatch.setattr(loop, "_task_poll_interval", lambda: 0.02)
    real_ensure = loop._ensure_mcp_task_poller_if_pending
    monkeypatch.setattr(loop, "_ensure_mcp_task_poller_if_pending", lambda: None)
    loop._ensure_mcp_loop()
    yield SimpleNamespace(loop=loop, real_ensure=real_ensure)
    missing = getattr(loop, "_mcp_task_server_missing_since", None)
    if isinstance(missing, dict):
        missing.clear()
    loop._mcp_task_backoff.clear()


def _persist(server: str, task_id: str) -> None:
    from tools import mcp_tool_handlers as handlers
    handle = SimpleNamespace(result_type="task", task_id=task_id)
    assert handlers._persist_mcp_task(handle, server_name=server, session_id="s-" + task_id,
                                      tool_call_id="c-" + task_id) == task_id


def _row(task_id: str) -> dict:
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        row = db._read_one("SELECT * FROM mcp_pending_tasks WHERE task_id = ?", (task_id,))
        return dict(row) if row is not None else None
    finally:
        db.close()


def test_poller_is_created_on_the_mcp_loop_when_called_from_another_loop(task_env, monkeypatch) -> None:
    from tools import mcp_tool as origin

    loop = task_env.loop
    _persist("gone", "t-wrong-loop")
    ran_on: list = []
    release = threading.Event()

    async def fake_poller():
        ran_on.append(asyncio.get_running_loop())
        while not release.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(loop, "_poll_mcp_tasks", fake_poller)

    async def agent_side():  # a foreign running loop, like the agent/gateway loop
        task_env.real_ensure()
        await asyncio.sleep(0.2)
        return asyncio.get_running_loop()

    foreign = asyncio.run(agent_side())
    try:
        key = loop._profile_task_key()
        task = loop._run_on_mcp_loop(lambda: _get_poller(loop, key), timeout=5)
        assert task is not None
        assert task.get_loop() is origin._mcp_loop
        assert task.get_loop() is not foreign
        assert ran_on == [origin._mcp_loop]
    finally:
        release.set()
        loop._run_on_mcp_loop(lambda: _await_pollers(loop), timeout=5)


async def _get_poller(loop, key):
    for _ in range(50):
        task = loop._mcp_task_pollers.get(key)
        if task is not None:
            return task
        await asyncio.sleep(0.01)
    return None


async def _await_pollers(loop):
    tasks = [t for t in loop._mcp_task_pollers.values() if not t.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=2)


def test_missing_server_row_expires_and_poller_exits(task_env, monkeypatch) -> None:
    loop = task_env.loop
    monkeypatch.setattr(loop, "_task_server_gone_expiry", lambda: 0.15, raising=False)
    _persist("gone-forever", "t-expire")

    loop._run_on_mcp_loop(loop._poll_mcp_tasks, timeout=30)  # returns: the poller exited

    row = _row("t-expire")
    assert row is not None, "an expired row is parked, never deleted"
    assert row["delivered_at"] is None
    assert row["expired_at"] is not None
    assert loop._has_pending_mcp_tasks() is False


def test_reconnect_before_expiry_resumes_the_parked_row(task_env, monkeypatch) -> None:
    from tools import mcp_tool as core

    loop = task_env.loop
    monkeypatch.setattr(loop, "_task_server_gone_expiry", lambda: 60.0, raising=False)
    _persist("late", "t-resume")

    loop._run_on_mcp_loop(loop._poll_mcp_tasks_once, timeout=5)  # server absent: parked, not expired
    row = _row("t-resume")
    assert row["expired_at"] is None and row["delivered_at"] is None
    assert ("late", "t-resume") in loop._mcp_task_server_missing_since

    with core._lock:
        core._servers["late-key"] = _TaskServer("late")
    try:
        loop._run_on_mcp_loop(loop._poll_mcp_tasks_once, timeout=5)
    finally:
        with core._lock:
            core._servers.pop("late-key", None)

    row = _row("t-resume")
    assert row["delivered_at"] is not None and row["expired_at"] is None
    assert ("late", "t-resume") not in loop._mcp_task_server_missing_since


def test_pending_check_exception_stops_the_poller(task_env, monkeypatch, caplog) -> None:
    loop = task_env.loop

    async def no_op_poll():
        return None

    def broken_check():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(loop, "_poll_mcp_tasks_once", no_op_poll)
    monkeypatch.setattr(loop, "_has_pending_mcp_tasks", broken_check)
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        loop._run_on_mcp_loop(loop._poll_mcp_tasks, timeout=3)
    assert any("pending" in rec.getMessage().lower() and rec.levelno >= logging.WARNING
               for rec in caplog.records), [r.getMessage() for r in caplog.records]


def test_repeated_poll_failures_stop_the_poller(task_env, monkeypatch, caplog) -> None:
    loop = task_env.loop
    calls: list[int] = []

    async def failing_poll():
        calls.append(1)
        raise RuntimeError("poll broke")

    monkeypatch.setattr(loop, "_poll_mcp_tasks_once", failing_poll)
    monkeypatch.setattr(loop, "_has_pending_mcp_tasks", lambda: True)
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        loop._run_on_mcp_loop(loop._poll_mcp_tasks, timeout=3)
    assert 1 < len(calls) <= 10
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
