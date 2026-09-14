"""Turn-visibility relays of ``ClaudeAgentSdkSession``: stream deltas, interim
assistant text, tool-iteration and tool-started notifications, plus the tool
preview. Extracted from ``claude_agent_sdk_session.py``; every method resolves
through ``ClaudeAgentSdkSession``'s MRO unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable, Optional

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.transports.claude_agent_sdk_session")


def _tool_preview(name: str, args: dict) -> str:
    """Short human preview of a tool call for progress breadcrumbs."""
    for key in ("command", "file_path", "path", "url", "query", "prompt"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return name


class ClaudeSdkNotifyMixin:
    """Per-turn visibility callbacks and their relays (see module docstring)."""

    def set_turn_visibility_callbacks(
        self,
        *,
        on_interim_assistant: Optional[Callable[[str], None]],
        on_tool_iteration: Optional[Callable[[], None]],
    ) -> None:
        """Atomically install current-turn, runtime-owned visibility hooks."""
        with self._turn_callback_lock:
            self._on_interim_assistant = on_interim_assistant
            self._on_tool_iteration = on_tool_iteration

    def _forward_stream_delta(self, message: Any) -> None:
        """Relay top-level deltas to display and child deltas to subagent.text."""
        event = getattr(message, "event", None) or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text")
        if not text:
            return
        parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
        if parent_tool_use_id:
            task_id = self._sdk_task_for_parent(parent_tool_use_id)
            if task_id:
                record = self._sdk_subagent_tasks().get(task_id)
                if record is not None:
                    record["streamed_text"] = str(record.get("streamed_text") or "") + str(text)
                self._emit_sdk_subagent(
                    "subagent.text", task_id, "Agent", str(text), None,
                    parent_tool_id=parent_tool_use_id,
                )
            return
        if self._on_stream_delta is None:
            return
        try:
            self._on_stream_delta(text)
        except Exception:  # pragma: no cover - display callback
            logger.debug("stream delta callback raised", exc_info=True)

    def _notify_interim_assistant(self, message: Any) -> None:
        """Relay completed assistant prose and thinking visibility."""
        if getattr(message, "parent_tool_use_id", None):
            return
        with self._turn_callback_lock:
            callback = self._on_interim_assistant
        if type(message).__name__ != "AssistantMessage":
            return
        blocks = list(getattr(message, "content", None) or [])
        thinking = "\n".join(
            str(getattr(block, "thinking", "") or "")
            for block in blocks
            if type(block).__name__ == "ThinkingBlock" and getattr(block, "thinking", "")
        ).strip()
        if thinking and self._on_tool_started is not None:
            try:
                self._on_tool_started("reasoning.available", thinking[:500], {})
            except Exception:  # pragma: no cover - display callback
                logger.debug("reasoning callback raised", exc_info=True)
        if callback is None:
            return
        text = "\n".join(
            str(getattr(block, "text", "") or "")
            for block in blocks
            if type(block).__name__ == "TextBlock" and getattr(block, "text", "")
        ).strip()
        if not text:
            return
        try:
            callback(text)
        except Exception:  # pragma: no cover - display callback
            logger.debug("interim assistant callback raised", exc_info=True)

    def _notify_tool_iteration(self) -> None:
        with self._turn_callback_lock:
            callback = self._on_tool_iteration
        if callback is None:
            return
        try:
            callback()
        except Exception:  # pragma: no cover - display callback
            logger.debug("tool-iteration callback raised", exc_info=True)

    def _notify_tool_started(self, message: Any) -> None:
        """Bridge ToolUseBlocks to Hermes tool-progress (gateway breadcrumbs),
        mirroring codex_runtime._codex_note_to_tool_progress (#38835)."""
        self._notify_child_text(message)
        if type(message).__name__ != "AssistantMessage":
            return
        parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
        for block in getattr(message, "content", None) or []:
            if type(block).__name__ != "ToolUseBlock":
                continue
            name = getattr(block, "name", "") or "unknown"
            args = getattr(block, "input", None) or {}
            if not isinstance(args, dict):
                args = {"input": args}
            preview = _tool_preview(name, args)
            if parent_tool_use_id:
                task_id = self._sdk_task_for_parent(parent_tool_use_id)
                if task_id:
                    self._emit_sdk_subagent(
                        "subagent.tool", task_id, name, preview, args,
                        parent_tool_id=parent_tool_use_id,
                    )
                continue
            if self._on_tool_started is None:
                continue
            try:
                self._on_tool_started(name, preview, args)
            except Exception:  # pragma: no cover - display callback
                logger.debug("tool-progress callback raised", exc_info=True)

    def _notify_tool_use(self, message: Any) -> None:
        """Open a stable-id tool card per top-level ToolUseBlock. Subagent
        streams (parent_tool_use_id set) stay quiet, like the deltas."""
        if type(message).__name__ != "AssistantMessage":
            return
        if getattr(message, "parent_tool_use_id", None):
            return
        for block in getattr(message, "content", None) or []:
            if type(block).__name__ != "ToolUseBlock":
                continue
            tool_use_id = str(getattr(block, "id", "") or "")
            if not tool_use_id:
                continue
            name = getattr(block, "name", "") or "unknown"
            args = getattr(block, "input", None) or {}
            if not isinstance(args, dict):
                args = {"input": args}
            if name == "Agent":
                self._sdk_agent_tools()[tool_use_id] = {
                    "goal": self._agent_tool_goal(args),
                    "args": args,
                }
            self._open_tool_cards[tool_use_id] = (name, args)
            if self._on_tool_use is None:
                continue
            try:
                self._on_tool_use(tool_use_id, name, args)
            except Exception:  # pragma: no cover - display callback
                logger.debug("tool-use card callback raised", exc_info=True)

    def _notify_tool_results(self, message: Any) -> None:
        """Close the matching tool card per ToolResultBlock (UserMessage echo)."""
        if type(message).__name__ != "UserMessage":
            return
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        from agent.transports.claude_sdk_event_projector import (
            _flatten_tool_result_content,
        )
        for block in content:
            if type(block).__name__ != "ToolResultBlock":
                continue
            tool_use_id = str(getattr(block, "tool_use_id", "") or "")
            card = self._open_tool_cards.pop(tool_use_id, None)
            if card is None or self._on_tool_result is None:
                continue
            name, args = card
            result = _flatten_tool_result_content(getattr(block, "content", None))
            is_error = bool(getattr(block, "is_error", False))
            # The installed SDK carries tool_use_result on the UserMessage
            # envelope (claude_agent_sdk.types.UserMessage), not on the block;
            # older/fabricated shapes may put it on the block, so fall back.
            tool_use_result = getattr(message, "tool_use_result", None)
            if not isinstance(tool_use_result, dict):
                tool_use_result = getattr(block, "tool_use_result", None)
            if not isinstance(tool_use_result, dict):
                tool_use_result = getattr(block, "metadata", None)
            if not isinstance(tool_use_result, dict):
                tool_use_result = None
            truncated = None
            if len(result) > 4000:
                truncated = {"shown": 4000, "total": len(result)}
            callback_kwargs = {
                "is_error": is_error,
                "error": result if is_error else None,
                "tool_use_result": tool_use_result,
                "truncated": truncated,
            }
            try:
                import inspect

                parameters = inspect.signature(self._on_tool_result).parameters
                accepts_keywords = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                ) or all(key in parameters for key in callback_kwargs)
                if accepts_keywords:
                    self._on_tool_result(
                        tool_use_id, name, args, result, **callback_kwargs
                    )
                else:
                    self._on_tool_result(tool_use_id, name, args, result)
            except Exception:  # pragma: no cover - display callback
                logger.debug("tool-result card callback raised", exc_info=True)

    # ---------- Claude Code Agent-task projection ----------

    def _sdk_agent_tools(self) -> dict[str, dict[str, Any]]:
        tools = getattr(self, "_sdk_agent_tool_uses", None)
        if tools is None:
            tools = self._sdk_agent_tool_uses = {}
        return tools

    def _sdk_subagent_tasks(self) -> dict[str, dict[str, Any]]:
        tasks = getattr(self, "_sdk_task_records", None)
        if tasks is None:
            tasks = self._sdk_task_records = {}
        return tasks

    @staticmethod
    def _agent_tool_goal(args: dict) -> str:
        return str(
            args.get("description")
            or args.get("subagent_type")
            or args.get("prompt")
            or ""
        ).strip()

    def _sdk_task_for_parent(self, parent_tool_id: str) -> Optional[str]:
        for task_id, record in self._sdk_subagent_tasks().items():
            if record.get("parent_tool_id") == parent_tool_id:
                return task_id
        return None

    def _emit_sdk_subagent(
        self,
        event_type: str,
        task_id: str,
        name: str,
        preview: str,
        args: Optional[dict] = None,
        *,
        parent_tool_id: Optional[str] = None,
        goal: str = "",
        child_session_id: Optional[str] = None,
        usage: Optional[dict] = None,
        status: Optional[str] = None,
        summary: str = "",
    ) -> None:
        callback = getattr(self, "_on_subagent_event", None)
        if not callable(callback):
            return
        record = self._sdk_subagent_tasks().get(str(task_id), {})
        resolved_goal = str(goal or record.get("goal") or "")
        kwargs: dict[str, Any] = {
            "subagent_id": str(task_id),
            "goal": resolved_goal,
        }
        parent = parent_tool_id or record.get("parent_tool_id")
        if parent:
            kwargs["parent_tool_id"] = str(parent)
        child_sid = child_session_id or record.get("child_session_id")
        if child_sid:
            kwargs["child_session_id"] = str(child_sid)
        if usage is not None:
            kwargs["usage"] = usage
            for key in ("input_tokens", "output_tokens", "reasoning_tokens", "api_calls"):
                if usage.get(key) is not None:
                    kwargs[key] = usage[key]
        if status:
            kwargs["status"] = status
        if summary:
            kwargs["summary"] = summary
        try:
            callback(event_type, name, preview, args or {}, **kwargs)
        except Exception:  # pragma: no cover - progress/UI callback
            logger.debug("SDK subagent callback raised", exc_info=True)

    @staticmethod
    def _sdk_usage_dict(usage: Any) -> dict:
        if isinstance(usage, dict):
            return dict(usage)
        if usage is None:
            return {}
        values = {}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "api_calls",
                    "total_tokens", "tool_uses", "duration_ms"):
            value = getattr(usage, key, None)
            if value is not None:
                values[key] = value
        return values

    def _notify_task_message(self, message: Any) -> None:
        """Project SDK Task* SystemMessage subclasses into subagent.* events."""
        name = type(message).__name__
        if name not in {
            "TaskStartedMessage", "TaskProgressMessage",
            "TaskNotificationMessage", "TaskUpdatedMessage",
        }:
            return
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            data = {}

        def value(key: str, default: Any = None) -> Any:
            found = getattr(message, key, None)
            return default if found is None else found

        task_id = str(value("task_id", data.get("task_id") or "") or "")
        if not task_id:
            return
        tasks = self._sdk_subagent_tasks()
        if name == "TaskStartedMessage":
            parent_tool_id = value("tool_use_id", data.get("tool_use_id"))
            agent_tool = self._sdk_agent_tools().get(str(parent_tool_id or ""), {})
            goal = str(
                value("description", data.get("description"))
                or agent_tool.get("goal")
                or ""
            ).strip()
            child_session_id = value("session_id", data.get("session_id"))
            record = {
                "goal": goal,
                "parent_tool_id": str(parent_tool_id) if parent_tool_id else None,
                "child_session_id": str(child_session_id) if child_session_id else None,
            }
            tasks[task_id] = record
            self._emit_sdk_subagent(
                "subagent.start", task_id, "Agent", goal, None,
                parent_tool_id=record["parent_tool_id"],
                child_session_id=record["child_session_id"],
            )
            return
        record = tasks.get(task_id)
        if record is None:
            return
        if name == "TaskProgressMessage":
            usage = self._sdk_usage_dict(value("usage", data.get("usage")))
            description = str(value("description", data.get("description")) or record.get("goal") or "")
            self._emit_sdk_subagent(
                "subagent.progress", task_id, "Agent", description, None,
                usage=usage, parent_tool_id=record.get("parent_tool_id"),
            )
            return
        if name == "TaskUpdatedMessage":
            patch = value("patch", data.get("patch"))
            if isinstance(patch, dict):
                patch_status = patch.get("status")
            else:
                patch_status = getattr(patch, "status", None)
            if not self._sdk_terminal_status(patch_status):
                return
            raw_status = patch_status
        else:
            raw_status = value("status", data.get("status"))
            if raw_status is None:
                raw_status = value("subtype", data.get("subtype"))
            if not self._sdk_terminal_status(raw_status):
                return
        status = self._sdk_status(raw_status)
        summary = str(value("summary", data.get("summary")) or "")
        self._emit_sdk_subagent(
            "subagent.complete", task_id, "Agent", str(value("description", "") or record.get("goal") or ""), None,
            parent_tool_id=record.get("parent_tool_id"), status=status, summary=summary,
        )
        tasks.pop(task_id, None)

    def _observe_sdk_lifecycle(self, message: Any) -> None:
        """Observe every SDK message before turn-specific interrupt gates."""
        from agent.transports.claude_sdk_background_tasks import observe_sdk_message

        observe_sdk_message(
            message,
            session_key=(
                getattr(self, "_sdk_registry_session_key", "")
                or getattr(self, "_hermes_session_id", "")
                or ""
            ),
            stop_task=getattr(self, "stop_task", None),
        )
        self._notify_task_message(message)

    def _finalize_sdk_tasks(self, status: str = "interrupted") -> None:
        """Finish each locally live SDK task exactly once at stream teardown."""
        tasks = self._sdk_subagent_tasks()
        from tools import delegate_tool_registry
        from tools.process_registry import process_registry

        session_key = (
            getattr(self, "_sdk_registry_session_key", "")
            or getattr(self, "_hermes_session_id", "")
            or ""
        )
        for task_id, record in list(tasks.items()):
            tasks.pop(task_id, None)
            self._emit_sdk_subagent(
                "subagent.complete", task_id, "Agent",
                str(record.get("goal") or ""), None,
                parent_tool_id=record.get("parent_tool_id"),
                status=status,
            )
            with contextlib.suppress(Exception):
                delegate_tool_registry.update_sdk_subagent(
                    "subagent.complete", task_id=task_id, status=status,
                )
        if session_key:
            with contextlib.suppress(Exception):
                process_registry.finalize_sdk_tasks(session_key, status="stopped")

    @staticmethod
    def _sdk_terminal_status(status: Any) -> bool:
        return str(status or "").strip().lower() in {
            "complete", "completed", "success", "succeeded", "done",
            "fail", "failed", "error", "killed", "stop", "stopped",
            "interrupted", "cancelled", "canceled",
        }

    @staticmethod
    def _sdk_status(status: Any) -> str:
        value = str(status or "").strip().lower()
        if value in {"complete", "completed", "success", "succeeded", "done"}:
            return "completed"
        if value in {"fail", "failed", "error"}:
            return "failed"
        return "interrupted"

    def _notify_child_text(self, message: Any) -> None:
        if type(message).__name__ != "AssistantMessage":
            return
        parent_tool_id = getattr(message, "parent_tool_use_id", None)
        if not parent_tool_id:
            return
        task_id = self._sdk_task_for_parent(parent_tool_id)
        if not task_id:
            return
        record = self._sdk_subagent_tasks().get(task_id)
        if record is None:
            return
        for block in getattr(message, "content", None) or []:
            if type(block).__name__ != "TextBlock":
                continue
            text = str(getattr(block, "text", "") or "")
            if text:
                streamed = str(record.get("streamed_text") or "")
                record.pop("streamed_text", None)
                if streamed == text:
                    continue
                if streamed and text.startswith(streamed):
                    text = text[len(streamed):]
                if not text:
                    continue
                self._emit_sdk_subagent(
                    "subagent.text", task_id, "Agent", text, None,
                    parent_tool_id=parent_tool_id,
                )
