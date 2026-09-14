"""Project Claude Agent SDK background tasks into Hermes' process registry."""

from __future__ import annotations

import threading
import weakref
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


def _observe_task_message(message: Any, session_key: str, stop_task) -> None:
    name = type(message).__name__
    if not name.startswith("Task"):
        return
    task_id = _task_id(message)
    tool_use_id = _tool_use_id(message)
    if name == "TaskStartedMessage":
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
        if process_registry.find_sdk_task(session_key, task_id=task_id, tool_use_id=tool_use_id) is None:
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
    _observe_task_message(message, key, callback)


__all__ = ["observe_sdk_message", "register_sdk_session_control"]
