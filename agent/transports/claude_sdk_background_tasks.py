"""Project Claude Agent SDK background tasks into Hermes' process registry."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Mapping
from typing import Any, Callable, Optional

from tools.process_registry import process_registry


_controllers: dict[str, weakref.ReferenceType] = {}
_controllers_lock = threading.Lock()


def register_sdk_session_control(session_key: str, session: Any) -> None:
    """Make the live adapter reachable by a session-scoped process kill."""
    key = str(session_key or "")
    if not key or session is None:
        return
    try:
        reference = weakref.ref(session)
    except TypeError:
        # The production adapter is weak-referenceable; this fallback keeps the
        # test seam useful for small duck-typed adapters.
        reference = lambda: session
    setattr(session, "_sdk_registry_session_key", key)
    with _controllers_lock:
        _controllers[key] = reference


def _stop_task_callback(session_key: str) -> Optional[Callable[[str], Any]]:
    with _controllers_lock:
        reference = _controllers.get(str(session_key or ""))
    session = reference() if reference is not None else None
    callback = getattr(session, "stop_task", None)
    return callback if callable(callback) else None


def _value(message: Any, name: str, default: Any = None) -> Any:
    return getattr(message, name, default)


def _task_id(message: Any) -> str:
    return str(_value(message, "task_id", "") or "")


def _tool_use_id(message: Any) -> str:
    return str(_value(message, "tool_use_id", "") or "")


# task_type values the Claude CLI stamps on Task* lifecycle messages.
SHELL_TASK_TYPES = frozenset({"local_bash"})
AGENT_TASK_TYPES = frozenset({"local_agent", "remote_agent", "in_process_teammate", "local_workflow"})


def sdk_task_type(message: Any) -> str:
    """The CLI's task_type for a Task* message ('' when absent)."""
    found = _value(message, "task_type", None)
    if not found:
        data = _value(message, "data", None)
        if isinstance(data, Mapping):
            found = data.get("task_type") or data.get("taskType")
    return str(found or "").strip()


def classify_sdk_task(message: Any, *, agent_tool_ids: Any = ()) -> str:
    """Route one SDK task to exactly one feed: 'shell', 'agent' or 'other'.

    Background shell commands belong in the process view, Agent tasks in the
    subagent feed — never both. The CLI's task_type decides; without it, an
    Agent tool invocation with the same tool_use_id marks an agent, and a
    provisional background Bash record marks a shell task.
    """
    task_type = sdk_task_type(message)
    if task_type in SHELL_TASK_TYPES:
        return "shell"
    if task_type in AGENT_TASK_TYPES:
        return "agent"
    if task_type:
        return "other"
    tool_use_id = _tool_use_id(message)
    if tool_use_id and tool_use_id in agent_tool_ids:
        return "agent"
    return "unknown"


def _task_metadata(value: Any) -> tuple[str, str]:
    """Find task identity/status in the SDK's versioned result envelopes."""
    if isinstance(value, Mapping):
        task_id = str(value.get("task_id") or value.get("taskId") or "")
        status = str(value.get("status") or value.get("subtype") or "")
        if task_id or status:
            return task_id, status
        for nested in value.values():
            task_id, status = _task_metadata(nested)
            if task_id or status:
                return task_id, status
    elif isinstance(value, (list, tuple)):
        for nested in value:
            task_id, status = _task_metadata(nested)
            if task_id or status:
                return task_id, status
    else:
        task_id = str(_value(value, "task_id", "") or _value(value, "taskId", "") or "")
        status = str(_value(value, "status", "") or _value(value, "subtype", "") or "")
        if task_id or status:
            return task_id, status
    return "", ""


def _result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("output") or value.get("content") or value.get("message") or "")
    return str(value or "")


def _result_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value in {"complete", "completed", "success", "succeeded", "done"}:
        return "completed"
    if value in {"fail", "failed", "error"}:
        return "failed"
    if value in {"stop", "stopped", "killed", "cancelled", "canceled", "interrupted"}:
        return "stopped"
    return "running"


def _observe_tool_use(message: Any, session_key: str, stop_task) -> None:
    if type(message).__name__ != "AssistantMessage":
        return
    for block in getattr(message, "content", None) or []:
        if type(block).__name__ != "ToolUseBlock":
            continue
        name = str(_value(block, "name", "") or "")
        args = _value(block, "input", None) or {}
        if name != "Bash" or not isinstance(args, dict) or not args.get("run_in_background"):
            continue
        command = str(args.get("command") or "Bash")
        process_registry.register_sdk_task(
            session_key=session_key,
            command=command,
            tool_use_id=str(_value(block, "id", "") or ""),
            output_file=str(args.get("output_file") or ""),
            stop_task=stop_task,
        )


def _observe_tool_result(message: Any, session_key: str) -> None:
    if type(message).__name__ != "UserMessage":
        return
    envelope = _value(message, "tool_use_result", None)
    for block in getattr(message, "content", None) or []:
        if type(block).__name__ != "ToolResultBlock":
            continue
        tool_use_id = str(_value(block, "tool_use_id", "") or "")
        if not tool_use_id:
            continue
        block_result = _value(block, "tool_use_result", None) or _value(block, "metadata", None)
        metadata = block_result or envelope or _value(block, "content", None)
        task_id, status = _task_metadata(metadata)
        output = _result_text(_value(block, "content", ""))
        if bool(_value(block, "is_error", False)):
            process_registry.update_sdk_task(
                session_key=session_key, tool_use_id=tool_use_id,
                status="failed", output=output, exit_code=1,
            )
            continue
        if task_id:
            process_registry.update_sdk_task(
                session_key=session_key, task_id=task_id, tool_use_id=tool_use_id,
                status=_result_status(status), output=output,
            )


def _observe_task_message(message: Any, session_key: str, stop_task) -> None:
    name = type(message).__name__
    if not name.startswith("Task"):
        return
    task_id = _task_id(message)
    tool_use_id = _tool_use_id(message)
    kind = classify_sdk_task(message)
    if name == "TaskStartedMessage":
        provisional = (
            process_registry.find_sdk_task(session_key, tool_use_id=tool_use_id)
            if tool_use_id else None
        )
        # Only shell work belongs in the process view; Agent tasks are shown
        # by the subagent feed. An untyped task counts only when it resolves a
        # provisional record from a run_in_background Bash call.
        if kind != "shell" and not (kind == "unknown" and provisional is not None):
            return
        process_registry.register_sdk_task(
            session_key=session_key,
            command=str(_value(message, "description", "") or "SDK task"),
            task_id=task_id,
            tool_use_id=tool_use_id,
            stop_task=stop_task,
        )
        return
    if name == "TaskProgressMessage":
        process_registry.update_sdk_task(
            session_key=session_key,
            task_id=task_id,
            tool_use_id=tool_use_id,
            status="running",
            command=str(_value(message, "description", "") or ""),
        )
        return
    if name == "TaskNotificationMessage":
        # A notification can be the first observable event when the SDK emits
        # the task lifecycle faster than the reader sees TaskStartedMessage.
        if (
            kind == "shell"
            and process_registry.find_sdk_task(session_key, task_id=task_id, tool_use_id=tool_use_id) is None
        ):
            process_registry.register_sdk_task(
                session_key=session_key,
                command=str(_value(message, "summary", "SDK task") or "SDK task"),
                task_id=task_id,
                tool_use_id=tool_use_id,
                stop_task=stop_task,
            )
        process_registry.update_sdk_task(
            session_key=session_key,
            task_id=task_id,
            tool_use_id=tool_use_id,
            status=str(_value(message, "status", "") or ""),
            output_file=str(_value(message, "output_file", "") or ""),
            output=_value(message, "summary", "") or "",
        )
        return
    if name == "TaskUpdatedMessage":
        patch = _value(message, "patch", None) or {}
        process_registry.update_sdk_task(
            session_key=session_key,
            task_id=task_id,
            tool_use_id=tool_use_id,
            status=str(_value(message, "status", None) or patch.get("status") or ""),
            command=str(patch.get("description") or "") if isinstance(patch, dict) else "",
            output_file=str(patch.get("output_file") or "") if isinstance(patch, dict) else "",
            output=patch.get("output", "") if isinstance(patch, dict) else "",
            exit_code=patch.get("exit_code") if isinstance(patch, dict) else None,
        )
        return
    if name in {"TaskOutputBlock", "TaskStopBlock"}:
        process_registry.update_sdk_task(
            session_key=session_key,
            task_id=task_id,
            tool_use_id=tool_use_id,
            status="stopped" if name == "TaskStopBlock" else "running",
            output=str(_value(message, "output", "") or _value(message, "content", "") or ""),
        )


def observe_sdk_message(message: Any, *, session_key: str, stop_task=None) -> None:
    """Record SDK Bash/task lifecycle messages without touching the transcript."""
    key = str(session_key or "")
    if not key:
        return
    callback = stop_task or _stop_task_callback(key)
    _observe_tool_use(message, key, callback)
    _observe_tool_result(message, key)
    _observe_task_message(message, key, callback)
    process_registry.prune_sdk_tasks()


__all__ = ["observe_sdk_message", "register_sdk_session_control"]
