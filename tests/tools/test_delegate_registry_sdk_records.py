from types import SimpleNamespace

from tools import delegate_tool_registry as registry


def test_sdk_record_has_separate_kind_and_retains_mirrored_text(monkeypatch):
    monkeypatch.setattr(registry, "_active_subagents", {})
    monkeypatch.setattr(registry, "_recent_subagents", {})
    session = SimpleNamespace()

    registry.update_sdk_subagent(
        "subagent.start",
        task_id="task-1",
        goal="inspect the project",
        sdk_session=session,
    )
    registry.update_sdk_subagent(
        "subagent.text", task_id="task-1", text="child output\n"
    )
    record = registry._active_subagents["task-1"]
    assert record["kind"] == "sdk"
    assert record["goal"] == "inspect the project"
    assert record["transcript"] == "child output\n"
    assert record["sdk_session"] is session

    registry.update_sdk_subagent("subagent.complete", task_id="task-1", status="completed")
    assert "task-1" not in registry._active_subagents


def test_sdk_record_does_not_replace_native_record(monkeypatch):
    native = {"kind": "native", "subagent_id": "same-id", "agent": object()}
    monkeypatch.setattr(registry, "_active_subagents", {"same-id": native})
    registry.update_sdk_subagent("subagent.start", task_id="same-id", goal="sdk")
    assert registry._active_subagents["same-id"] is native


def test_sdk_session_stop_task_passthrough_runs_on_session_loop():
    import asyncio

    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class Client:
        def __init__(self):
            self.stopped = []

        async def stop_task(self, task_id):
            self.stopped.append(task_id)

    client = Client()
    session = object.__new__(ClaudeAgentSdkSession)
    session._client = client
    session._loop = object()
    session._run_coro = lambda coro, *, timeout: asyncio.run(coro)

    assert session.stop_task("task-1") is True
    assert client.stopped == ["task-1"]
