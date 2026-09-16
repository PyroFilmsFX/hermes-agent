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


def test_sdk_subagent_rpc_uses_live_gateway_owner_not_stored_agent_id(monkeypatch):
    from agent.claude_sdk_runtime_session import _on_sdk_subagent_event
    from tools import delegate_tool_registry

    transport = SimpleNamespace(write=lambda frame: True)
    owner = {"transport": transport}
    adapter = SimpleNamespace(stop_task=lambda task_id: True)
    agent = SimpleNamespace(
        session_id="stored-agent-sid",
        _tui_gateway_runtime_sid="live-gateway-sid",
        _claude_sdk_session=adapter,
        tool_progress_callback=None,
    )
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda sid: (transport, owner))
    monkeypatch.setattr(delegate_tool_registry, "_active_subagents", {})
    monkeypatch.setattr(delegate_tool_registry, "_recent_subagents", {})

    _on_sdk_subagent_event(
        agent,
        "subagent.start",
        subagent_id="sdk-task",
        goal="inspect files",
    )
    delegate_tool_registry.update_sdk_subagent(
        "subagent.text",
        task_id="sdk-task",
        text="child output",
    )

    listed = server._methods["subagent.list"]("rid", {"session_id": "live-gateway-sid"})
    tailed = server._methods["subagent.tail"](
        "rid", {"session_id": "live-gateway-sid", "subagent_id": "sdk-task"}
    )
    interrupted = server._methods["subagent.interrupt"](
        "rid", {"session_id": "live-gateway-sid", "subagent_id": "sdk-task"}
    )

    assert [row["subagent_id"] for row in listed["result"]["subagents"]] == ["sdk-task"]
    assert tailed["result"]["text"] == "child output"
    assert interrupted["result"] == {"found": True, "subagent_id": "sdk-task"}



def test_agent_task_started_never_registers_a_background_process():
    from agent.transports.claude_sdk_background_tasks import observe_sdk_message
    from tools.process_registry import process_registry

    class TaskStartedMessage:
        task_id = "agent-task-1"
        description = "Astra: bug + logging triage"
        tool_use_id = "agent-tool-1"
        task_type = "local_agent"

    observe_sdk_message(TaskStartedMessage(), session_key="sess-agent-only")
    assert process_registry.find_sdk_task("sess-agent-only", task_id="agent-task-1") is None
