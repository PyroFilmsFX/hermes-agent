"""Contracts for projecting Claude Agent SDK background tasks into Hermes."""

from dataclasses import dataclass

from agent.transports.claude_sdk_background_tasks import observe_sdk_message
from tools.process_registry import ProcessRegistry


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AssistantMessage:
    content: list


@dataclass
class TaskStartedMessage:
    task_id: str
    description: str
    tool_use_id: str | None = None


@dataclass
class TaskUpdatedMessage:
    task_id: str
    status: str
    patch: dict | None = None


@dataclass
class TaskNotificationMessage:
    task_id: str
    status: str
    output_file: str = ""


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: object = ""
    is_error: bool = False


@dataclass
class UserMessage:
    content: list


def test_background_bash_and_task_lifecycle_are_projected_and_scoped(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("agent.transports.claude_sdk_background_tasks.process_registry", registry)

    stop_calls = []
    observe_sdk_message(
        AssistantMessage([
            ToolUseBlock(
                id="toolu-bash",
                name="Bash",
                input={"command": "npm run dev", "run_in_background": True},
            )
        ]),
        session_key="session-a",
        stop_task=stop_calls.append,
    )
    started = registry.list_sessions(session_key="session-a")
    assert len(started) == 1
    assert started[0]["sdk_owned"] is True
    assert started[0]["status"] == "running"
    assert started[0]["command"] == "npm run dev"

    observe_sdk_message(
        TaskStartedMessage("task-7", "npm run dev", tool_use_id="toolu-bash"),
        session_key="session-a",
        stop_task=stop_calls.append,
    )
    observe_sdk_message(
        TaskUpdatedMessage("task-7", "running"),
        session_key="session-a",
        stop_task=stop_calls.append,
    )
    observe_sdk_message(
        TaskNotificationMessage("task-7", "completed", output_file="/tmp/task-7.txt"),
        session_key="session-a",
        stop_task=stop_calls.append,
    )

    visible_a = registry.list_sessions(session_key="session-a")
    assert visible_a[0]["status"] == "exited"
    assert visible_a[0]["exit_code"] == 0
    assert visible_a[0]["output_file"] == "/tmp/task-7.txt"
    assert registry.list_sessions(session_key="session-b") == []


def test_failed_background_bash_result_finishes_provisional_record(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("agent.transports.claude_sdk_background_tasks.process_registry", registry)

    observe_sdk_message(
        AssistantMessage([
            ToolUseBlock(
                id="toolu-denied",
                name="Bash",
                input={"command": "rm -rf /tmp/nope", "run_in_background": True},
            )
        ]),
        session_key="session-a",
    )
    observe_sdk_message(
        UserMessage([
            ToolResultBlock("toolu-denied", "permission denied", is_error=True),
        ]),
        session_key="session-a",
    )

    records = registry.list_sessions(session_key="session-a")
    assert records[0]["status"] == "exited"
    assert records[0]["exit_code"] == 1


def test_provisional_sdk_record_expires_without_task_started(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("agent.transports.claude_sdk_background_tasks.process_registry", registry)
    observe_sdk_message(
        AssistantMessage([
            ToolUseBlock("toolu-never-started", "Bash", {
                "command": "npm run dev", "run_in_background": True,
            }),
        ]),
        session_key="session-a",
    )
    record = registry.list_sessions(session_key="session-a")[0]
    session = registry.get(record["session_id"])
    session.started_at = 0

    registry.prune_sdk_tasks(now=31)

    assert registry.list_sessions(session_key="session-a") == []


def test_successful_background_bash_result_resolves_task_id(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr("agent.transports.claude_sdk_background_tasks.process_registry", registry)
    observe_sdk_message(
        AssistantMessage([
            ToolUseBlock("toolu-started-late", "Bash", {
                "command": "npm run dev", "run_in_background": True,
            }),
        ]),
        session_key="session-a",
    )
    observe_sdk_message(
        UserMessage([
            ToolResultBlock("toolu-started-late", {"task_id": "task-late"}),
        ]),
        session_key="session-a",
    )

    records = registry.list_sessions(session_key="session-a")
    assert records[0]["sdk_task_id"] == "task-late"
