"""CLI-side tool and subagent hook passthrough for ``ClaudeAgentSdkSession``.

Translates CLI-side tool calls (PreToolUse, PostToolUse) into Hermes plugin
hooks (pre_tool_call, post_tool_call) and CLI subagent lifecycle events
(SubagentStart, SubagentStop) into subagent_start / subagent_stop.

Observe-only: callbacks swallow exceptions and return {} so the CLI's
decision and execution remain untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("agent.transports.claude_agent_sdk_session")


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sdk_exposes_subagent_hooks() -> bool:
    """Return whether the installed claude_agent_sdk exposes SubagentStart/Stop."""
    try:
        import claude_agent_sdk.types as sdk_types

        if hasattr(sdk_types, "SubagentStartHookInput"):
            return True
        hook_event = getattr(sdk_types, "HookEvent", None)
        if hook_event is not None:
            import typing

            for arg in typing.get_args(hook_event):
                val = typing.get_args(arg)
                if (val and val[0] == "SubagentStart") or arg == "SubagentStart":
                    return True
        return False
    except Exception:
        try:
            import claude_agent_sdk

            return hasattr(claude_agent_sdk, "SubagentStartHookInput")
        except Exception:
            return False


class ClaudeSdkHooksMixin:
    """Plugin hook bridges for CLI-side tool use and subagents."""

    def _extract_session_id(self, input_data: Any) -> str:
        sid = _get_val(input_data, "session_id")
        if sid:
            return str(sid)
        return str(
            getattr(self, "_session_id", None)
            or getattr(self, "session_id", None)
            or getattr(self, "_hermes_session_id", None)
            or ""
        )

    def _build_plugin_hooks(self) -> Optional[dict]:
        """PreToolUse/PostToolUse -> pre_tool_call/post_tool_call, and subagent lifecycle."""
        try:
            from claude_agent_sdk import HookMatcher
        except Exception:  # pragma: no cover - SDK predates hooks
            logger.debug("claude-agent-sdk: HookMatcher unavailable", exc_info=True)
            return None

        async def _on_pre_tool_use(
            input_data: Any, tool_use_id: Optional[str] = None, context: Any = None
        ) -> dict:
            try:
                from hermes_cli.lifecycle import invoke_hook

                tool_name = str(_get_val(input_data, "tool_name", "") or "")
                tool_input = _get_val(input_data, "tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                session_id = self._extract_session_id(input_data)
                t_id = tool_use_id or _get_val(input_data, "tool_use_id", None)

                kw: dict[str, Any] = {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "args": tool_input,
                    "session_id": session_id,
                    "source": "claude_cli",
                }
                if t_id:
                    kw["tool_call_id"] = str(t_id)
                agent_id = _get_val(input_data, "agent_id")
                if agent_id:
                    kw["subagent_id"] = str(agent_id)
                invoke_hook("pre_tool_call", **kw)
            except Exception:
                logger.debug("pre_tool_call plugin hook failed", exc_info=True)
            return {}

        async def _on_post_tool_use(
            input_data: Any, tool_use_id: Optional[str] = None, context: Any = None
        ) -> dict:
            try:
                from hermes_cli.lifecycle import invoke_hook

                tool_name = str(_get_val(input_data, "tool_name", "") or "")
                tool_input = _get_val(input_data, "tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                tool_response = _get_val(input_data, "tool_response", None)
                session_id = self._extract_session_id(input_data)
                t_id = tool_use_id or _get_val(input_data, "tool_use_id", None)

                kw: dict[str, Any] = {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "args": tool_input,
                    "tool_response": tool_response,
                    "result": tool_response,
                    "session_id": session_id,
                    "source": "claude_cli",
                }
                if t_id:
                    kw["tool_call_id"] = str(t_id)
                agent_id = _get_val(input_data, "agent_id")
                if agent_id:
                    kw["subagent_id"] = str(agent_id)
                invoke_hook("post_tool_call", **kw)
            except Exception:
                logger.debug("post_tool_call plugin hook failed", exc_info=True)
            return {}

        async def _on_subagent_start(
            input_data: Any, tool_use_id: Optional[str] = None, context: Any = None
        ) -> dict:
            try:
                from hermes_cli.lifecycle import invoke_hook

                session_id = self._extract_session_id(input_data)
                agent_id = str(_get_val(input_data, "agent_id", "") or "")
                agent_type = str(_get_val(input_data, "agent_type", "") or "")

                invoke_hook(
                    "subagent_start",
                    session_id=session_id,
                    parent_session_id=session_id,
                    agent_id=agent_id,
                    child_subagent_id=agent_id,
                    child_session_id=agent_id,
                    agent_type=agent_type,
                    child_role=agent_type,
                    source="claude_cli",
                )
            except Exception:
                logger.debug("subagent_start plugin hook failed", exc_info=True)
            return {}

        async def _on_subagent_stop(
            input_data: Any, tool_use_id: Optional[str] = None, context: Any = None
        ) -> dict:
            try:
                from hermes_cli.lifecycle import invoke_hook

                session_id = self._extract_session_id(input_data)
                agent_id = str(_get_val(input_data, "agent_id", "") or "")
                agent_type = str(_get_val(input_data, "agent_type", "") or "")
                transcript_path = _get_val(input_data, "agent_transcript_path", None)
                stop_hook_active = bool(_get_val(input_data, "stop_hook_active", False))

                invoke_hook(
                    "subagent_stop",
                    session_id=session_id,
                    parent_session_id=session_id,
                    agent_id=agent_id,
                    child_subagent_id=agent_id,
                    child_session_id=agent_id,
                    agent_type=agent_type,
                    child_role=agent_type,
                    agent_transcript_path=transcript_path,
                    stop_hook_active=stop_hook_active,
                    source="claude_cli",
                )
            except Exception:
                logger.debug("subagent_stop plugin hook failed", exc_info=True)
            return {}

        hooks: dict[str, list[Any]] = {
            "PreToolUse": [HookMatcher(hooks=[_on_pre_tool_use])],
            "PostToolUse": [HookMatcher(hooks=[_on_post_tool_use])],
        }
        if _sdk_exposes_subagent_hooks():
            hooks["SubagentStart"] = [HookMatcher(hooks=[_on_subagent_start])]
            hooks["SubagentStop"] = [HookMatcher(hooks=[_on_subagent_stop])]

        return hooks
