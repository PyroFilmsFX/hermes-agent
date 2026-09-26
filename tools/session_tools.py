"""Service-gated session-spawn tool for the Claude Agent SDK MCP lane."""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry

SESSION_CREATE_SCHEMA = {
    "name": "session_create",
    "description": (
        "Create a sibling Hermes session in an existing project workspace and seed one first task. "
        "The new session inherits the caller's profile, model, provider, and permission posture."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Existing project directory on the owner backend."},
            "task": {"type": "string", "description": "The one first task for the sibling session."},
            "title": {"type": "string", "description": "Durable title for the sibling session."},
            "request_id": {"type": "string", "description": "Stable retry key for idempotent creation."},
        },
        "required": ["cwd", "task", "title", "request_id"],
        "additionalProperties": False,
    },
}


SESSION_SEND_SCHEMA = {
    "name": "session_send",
    "description": (
        "Send a message to another Hermes session by stored session id (or its title as a hint). "
        "The message is stored durably and delivered through the target's native Claude peer channel "
        "when available, otherwise as its next turn. Dead targets are resumed on demand or queued until "
        "they next start. A live Claude sender may receive a native_peer name for direct SendMessage. "
        "Returns delivered-native, delivered-live, resumed-and-delivered, queued, queue_full (too many of "
        "your messages are still queued; nothing was written), or failed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Stored Hermes session id (preferred) or session title."},
            "body": {"type": "string", "description": "The message; it arrives labelled with this session as sender."},
            "request_id": {"type": "string", "description": "Optional stable retry key; a retry never sends twice."},
        },
        "required": ["target", "body"],
        "additionalProperties": False,
    },
}


def _bridge_tool_enabled(policy_key: str) -> bool:
    """Owner config gate for one bridge tool (``agent.claude_agent_sdk.<policy_key>.enabled``)."""
    from agent.transports.claude_agent_sdk_session_config import _provider_config

    policy = _provider_config().get(policy_key)
    return not (isinstance(policy, dict) and not bool(policy.get("enabled", True)))


def session_spawn_enabled() -> bool:
    try:
        return _bridge_tool_enabled("session_spawn")
    except Exception:
        return False


def session_send_enabled() -> bool:
    try:
        return _bridge_tool_enabled("session_send")
    except Exception:
        return False


def check_session_spawn_requirements() -> bool:
    """Construction-time reachability gate; invocation rechecks authorization."""
    try:
        from agent.transports.hermes_gateway_session_bridge import bridge_available_from_environment

        return session_spawn_enabled() and bridge_available_from_environment()
    except Exception:
        return False


def check_session_send_requirements() -> bool:
    try:
        from agent.transports.hermes_gateway_session_bridge import bridge_available_from_environment

        return session_send_enabled() and bridge_available_from_environment()
    except Exception:
        return False


def session_create(**kwargs: Any) -> str:
    """Dispatch through the scoped owner bridge; never accept identity arguments."""
    from agent.transports.hermes_gateway_session_bridge import (
        HermesGatewaySessionBridge,
        SessionSpawnBridgeError,
    )

    try:
        values = {key: kwargs.get(key) for key in ("cwd", "task", "title", "request_id")}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            return json.dumps({"error": "cwd, task, title, and request_id are required"})
        result = HermesGatewaySessionBridge.from_environment().create_task_session(**values)
        return json.dumps(result, ensure_ascii=False)
    except SessionSpawnBridgeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - MCP tool boundary
        return json.dumps({"error": f"session_create failed: {exc}"})


def session_send(**kwargs: Any) -> str:
    """Durable peer message through the scoped owner bridge; the sender is never an argument."""
    from agent.transports.hermes_gateway_session_bridge import (
        HermesGatewaySessionBridge,
        SessionSpawnBridgeError,
    )

    target, body, request_id = kwargs.get("target"), kwargs.get("body"), kwargs.get("request_id")
    if not isinstance(target, str) or not target.strip() or not isinstance(body, str) or not body.strip():
        return json.dumps({"status": "failed", "error": "target and body are required"})
    try:
        result = HermesGatewaySessionBridge.from_environment().send_to_session(
            target=target.strip(), body=body, request_id=request_id.strip() if isinstance(request_id, str) else "")
        return json.dumps(result, ensure_ascii=False)
    except SessionSpawnBridgeError as exc:
        return json.dumps({"status": "failed", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - MCP tool boundary
        return json.dumps({"status": "failed", "error": f"session_send failed: {exc}"})


registry.register(
    name="session_create",
    toolset="session_spawn",
    schema=SESSION_CREATE_SCHEMA,
    handler=lambda args, **_: session_create(**args),
    check_fn=check_session_spawn_requirements,
)

registry.register(
    name="session_send",
    toolset="session_spawn",
    schema=SESSION_SEND_SCHEMA,
    handler=lambda args, **_: session_send(**args),
    check_fn=check_session_send_requirements,
)


__all__ = [
    "SESSION_CREATE_SCHEMA", "SESSION_SEND_SCHEMA", "check_session_send_requirements",
    "check_session_spawn_requirements", "session_create", "session_send", "session_send_enabled",
    "session_spawn_enabled",
]
