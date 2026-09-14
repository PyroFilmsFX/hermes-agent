"""Process-view and stop RPC contracts for SDK-owned tasks."""

from types import SimpleNamespace

from tools.process_registry import ProcessRegistry
from tui_gateway import server


def test_process_view_lists_sdk_task_and_kill_uses_session_scoped_stop_task(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(server, "_tools_mod", lambda _: SimpleNamespace(process_registry=registry))
    stop_calls = []
    process = registry.register_sdk_task(
        session_key="session-a",
        command="python server.py",
        task_id="task-a",
        stop_task=stop_calls.append,
    )
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: ({"session_key": "session-a"}, None))

    listed = server._session_processes({"session_key": "session-a"})
    assert listed[0]["session_id"] == process.id
    assert listed[0]["sdk_owned"] is True

    result = server._methods["process.kill"]("rid", {"process_id": process.id})
    assert result["result"]["killed"] is True
    assert stop_calls == ["task-a"]
