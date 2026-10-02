"""MCP 2026 M8 task persistence, polling, and mailbox delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _CompletedTaskSession:
    async def send_request(self, request, result_type):
        if request.method == "tasks/get":
            from mcp import types
            return types.GetTaskResult(taskId=request.params.task_id, status="completed",
                                       createdAt="2026-01-01T00:00:00Z",
                                       lastUpdatedAt="2026-01-01T00:00:01Z", ttl=60)
        return {"content": [{"type": "text", "text": "completed output"}]}


class _TaskServer:
    name = "fixture"
    session = _CompletedTaskSession()

    def __init__(self):
        self._rpc_lock = asyncio.Lock()


def test_mcp_task_persists_resumes_and_enqueues_once(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tools import mcp_tool as core
    from tools import mcp_tool_handlers as handlers
    from tools import mcp_tool_loop as loop

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tui_gateway.session_mailbox.schedule_drain", lambda *args: None)
    # This test drives polling explicitly so background scheduling cannot race the assertions.
    monkeypatch.setattr(loop, "_ensure_mcp_task_poller_if_pending", lambda: None)
    handle = SimpleNamespace(result_type="task", task_id="task-1")
    assert handlers._persist_mcp_task(handle, server_name="fixture", session_id="session-1",
                                      tool_call_id="call-1") == "task-1"
    db = SessionDB()
    try:
        persisted = db._read_one("SELECT * FROM mcp_pending_tasks WHERE server = 'fixture' AND task_id = 'task-1'")
        assert persisted["session_id"] == "session-1"
        assert persisted["tool_call_id"] == "call-1"
    finally:
        db.close()

    loop._ensure_mcp_loop()
    with core._lock:
        core._servers["m8-fixture"] = _TaskServer()
    try:
        loop._run_on_mcp_loop(loop._poll_mcp_tasks_once, timeout=10)
        loop._run_on_mcp_loop(loop._poll_mcp_tasks_once, timeout=10)
        db = SessionDB()
        try:
            assert db._read_one("SELECT delivered_at FROM mcp_pending_tasks WHERE task_id = 'task-1'")[0]
            rows = db._read_all("SELECT * FROM peer_mailbox WHERE dedupe_key = ?",
                                ("mcp-task:fixture:task-1",))
            assert len(rows) == 1
            assert rows[0]["from_label"] == "MCP task"
            assert "completed output" in rows[0]["body"]
            import tui_gateway.session_mailbox as mailbox
            delivered_via = []
            monkeypatch.setattr(mailbox, "_unfit_for_delivery", lambda *_args: None)
            monkeypatch.setattr(mailbox, "_find_live", lambda *_args: ("live-session", {}))
            monkeypatch.setattr(mailbox, "_mailbox_auto_blocked", lambda *_args: False)
            monkeypatch.setattr(mailbox, "_deliver_native_claimed", lambda *_args, **_kwargs: None)
            monkeypatch.setattr(mailbox, "_submit_claimed", lambda _db, _row, _sid, **kwargs:
                                delivered_via.append(kwargs["via"]) or ("delivered-live", "ok"))
            mailbox._deliver_row(db, rows[0], profile_home=None, allow_resume=True, pol={})
            assert delivered_via == ["task"]
        finally:
            db.close()

        pending = SimpleNamespace(result_type="task", task_id="task-2")
        handlers._persist_mcp_task(pending, server_name="offline", session_id="session-2", tool_call_id="call-2")
        loop._run_on_mcp_loop(loop._poll_mcp_tasks_once, timeout=10)
        db = SessionDB()
        try:
            assert db._read_one("SELECT delivered_at FROM mcp_pending_tasks WHERE task_id = 'task-2'")[0] is None
        finally:
            db.close()
    finally:
        with core._lock:
            core._servers.pop("m8-fixture", None)


def test_mcp_task_persists_to_active_profile_db(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    import hermes_state
    from tools import mcp_tool_handlers as handlers
    from tools import mcp_tool_loop as loop

    default_home = tmp_path / "default"
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    # Keep the test fixture's redirected DB path from overriding runtime profile resolution.
    sentinel_db = tmp_path / "sentinel" / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", sentinel_db)
    monkeypatch.setattr(hermes_state, "_IMPORT_DEFAULT_DB_PATH", sentinel_db)
    monkeypatch.setattr(loop, "_ensure_mcp_task_poller_if_pending", lambda: None)
    token = set_hermes_home_override(str(profile_home))
    try:
        result = SimpleNamespace(result_type="task", task_id="profile-task")
        assert handlers._persist_mcp_task(result, server_name="fixture", session_id="profile-session",
                                          tool_call_id="profile-call") == "profile-task"
        assert get_hermes_home() == profile_home
        db = SessionDB()
        try:
            assert db.db_path == profile_home / "state.db"
            row = db._read_one("SELECT session_id, tool_call_id FROM mcp_pending_tasks WHERE task_id = ?",
                               ("profile-task",))
            assert tuple(row) == ("profile-session", "profile-call")
        finally:
            db.close()
    finally:
        reset_hermes_home_override(token)
    db = SessionDB()
    try:
        assert db._read_one("SELECT 1 FROM mcp_pending_tasks WHERE task_id = ?", ("profile-task",)) is None
    finally:
        db.close()
