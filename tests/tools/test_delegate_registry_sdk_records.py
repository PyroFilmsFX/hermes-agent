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
    import threading
    import time

    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class Client:
        def __init__(self):
            self.stopped = []
            self.thread_id = None
            self.done = threading.Event()

        async def stop_task(self, task_id):
            self.stopped.append(task_id)
            self.thread_id = threading.get_ident()
            self.done.set()

    client = Client()
    loop = asyncio.new_event_loop()
    loop_thread_id = []
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop_thread_id.append(threading.get_ident())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert ready.wait(2)
    session = object.__new__(ClaudeAgentSdkSession)
    session._client = client
    session._loop = loop

    try:
        caller_thread_id = threading.get_ident()
        started = time.monotonic()
        assert session.stop_task("task-1") is True
        assert time.monotonic() - started < 0.5
        assert client.done.wait(2)
        assert client.stopped == ["task-1"]
        assert client.thread_id == loop_thread_id[0]
        assert client.thread_id != caller_thread_id
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()
