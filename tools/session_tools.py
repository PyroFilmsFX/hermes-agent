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


def check_session_spawn_requirements() -> bool:
    """Construction-time reachability gate; invocation rechecks authorization."""
    try:
        from agent.transports.claude_agent_sdk_session_config import _provider_config
        from agent.transports.hermes_gateway_session_bridge import bridge_available_from_environment

        policy = _provider_config().get("session_spawn")
        if isinstance(policy, dict) and not bool(policy.get("enabled", True)):
            return False
        return bridge_available_from_environment()
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


registry.register(
    name="session_create",
    toolset="session_spawn",
    schema=SESSION_CREATE_SCHEMA,
    handler=lambda args, **_: session_create(**args),
    check_fn=check_session_spawn_requirements,
)


__all__ = ["SESSION_CREATE_SCHEMA", "check_session_spawn_requirements", "session_create"]
