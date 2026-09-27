"""Create a manager proposal for the desktop owner-forward confirmation card.

This tool records no state and never calls the gateway. The desktop renders its
result and owns the separate confirmation and delivery flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

from hermes_owner_grant.scopes import CLASS_DEFAULT_TTL_MS, ScopeError, parse_scope
from tools.registry import registry, tool_error

_DEFAULT_MAX_CHARS = 8000
_HARD_MAX_CHARS = 32_000
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
RESULT_TEXT = "Proposal shown to the owner; nothing is sent until they confirm."


def _owner_forward_policy() -> dict[str, Any]:
    """Read the existing profile's owner-forward config and clamp its text limit."""
    try:
        from hermes_cli.config import load_config_readonly

        raw = load_config_readonly().get("owner_forward")
    except Exception:
        raw = None
    config = raw if isinstance(raw, dict) else {}
    enabled = config.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    try:
        max_chars = min(_HARD_MAX_CHARS, max(1, int(config.get("max_chars", _DEFAULT_MAX_CHARS))))
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    return {"enabled": bool(enabled), "max_chars": max_chars}


def check_owner_forward_available() -> bool:
    """Gate on the configured owner-forward service; desktop session gating is its toolset."""
    if not _owner_forward_policy()["enabled"] or not os.environ.get("HERMES_OWNER_GRANT_BACKEND"):
        return False
    try:
        from hermes_owner_grant.anchor import load_trusted_anchor

        load_trusted_anchor()
    except Exception:
        return False
    return True


def _error(message: str) -> str:
    return tool_error(message)


def owner_forward_propose(
    targets: list[dict[str, str]],
    text: str,
    scopes: list[str] | None = None,
    subject: str | None = None,
) -> str:
    """Return a validated proposal for the owner to review and confirm in Desktop."""
    policy = _owner_forward_policy()
    if not policy["enabled"]:
        return _error("Owner-forward proposals are disabled for this profile.")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 5:
        return _error("targets must contain between 1 and 5 profile/session pairs.")

    clean_targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        from hermes_cli.profiles import validate_profile_name

        for target in targets:
            if not isinstance(target, dict) or set(target) != {"profile", "session_id"}:
                return _error("each target must contain exactly profile and session_id.")
            profile, session_id = target["profile"], target["session_id"]
            if not isinstance(profile, str) or not isinstance(session_id, str):
                return _error("target profile and session_id must be strings.")
            validate_profile_name(profile)
            if not _SESSION_ID_RE.fullmatch(session_id):
                return _error("target session_id has an invalid format.")
            key = (profile, session_id)
            if key in seen:
                return _error("targets must be distinct.")
            seen.add(key)
            clean_targets.append({"profile": profile, "session_id": session_id})
    except ValueError as exc:
        return _error(str(exc))

    if not isinstance(text, str) or not text.strip():
        return _error("text must contain at least one non-whitespace character.")
    if text.lstrip().startswith("/"):
        return _error("a proposal cannot start with '/'.")
    try:
        encoded_text = text.encode("utf-8")
    except UnicodeEncodeError:
        return _error("text must be valid UTF-8.")
    if len(text) > policy["max_chars"]:
        return _error(f"text must be at most {policy['max_chars']} characters.")

    if scopes is None:
        scopes = []
    if not isinstance(scopes, list) or not all(isinstance(value, str) for value in scopes):
        return _error("scopes must be a list of scope strings.")
    if len(set(scopes)) != len(scopes):
        return _error("scopes must not contain duplicates.")
    scope_details = []
    try:
        for value in scopes:
            scope = parse_scope(value)
            scope_details.append({
                "scope": scope.value,
                "class": scope.scope_class,
                "ttl_ms": CLASS_DEFAULT_TTL_MS[scope.scope_class],
                "single_use": scope.single_use,
            })
    except (ScopeError, KeyError) as exc:
        return _error(f"invalid scope: {exc}")
    if any(item["class"] == "prod" for item in scope_details) and not (
        isinstance(subject, str) and subject.strip()
    ):
        return _error("subject is required when a prod scope is proposed.")
    if subject is not None and not isinstance(subject, str):
        return _error("subject must be a string.")

    result = {
        "owner_forward_proposal": 1,
        "proposal_id": str(uuid.uuid4()),
        "targets": clean_targets,
        "text_sha256": hashlib.sha256(encoded_text).hexdigest(),
        "text_len": len(encoded_text),
        "scopes": scope_details,
        "subject": subject,
        "status": "awaiting_owner",
        "message": RESULT_TEXT,
    }
    return json.dumps(result, ensure_ascii=False)


OWNER_FORWARD_PROPOSE_SCHEMA = {
    "name": "owner_forward_propose",
    "description": (
        "Propose forwarding text to one or more Hermes sessions for the desktop owner to review. "
        "This only shows a proposal card; nothing is sent unless the owner confirms. "
        "Never tell the user it was sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "description": "Target profile name."},
                        "session_id": {"type": "string", "description": "Stored Hermes session id."},
                    },
                    "required": ["profile", "session_id"],
                    "additionalProperties": False,
                },
                "description": "One to five profile-qualified target sessions.",
            },
            "text": {"type": "string", "description": "The text to propose for forwarding."},
            "scopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional owner-grant scopes from the hermes_owner_grant catalog.",
            },
            "subject": {"type": "string", "description": "Required for prod scopes."},
        },
        "required": ["targets", "text"],
        "additionalProperties": False,
    },
}


registry.register(
    name="owner_forward_propose",
    toolset="desktop_ui",
    schema=OWNER_FORWARD_PROPOSE_SCHEMA,
    handler=lambda args, **kw: owner_forward_propose(
        targets=args.get("targets"),
        text=args.get("text"),
        scopes=args.get("scopes"),
        subject=args.get("subject"),
    ),
    check_fn=check_owner_forward_available,
)
