"""Scoped WebSocket bridge used by the hermes-tools session-spawn MCP tool.

The MCP server is a child process, so an in-process callback cannot safely reach
the owner gateway.  The parent registers a short-lived, session-bound capability;
the child discovers the owner's authenticated ``/api/ws`` endpoint through the
cooperative-attach handshake and can call only the internal task-create RPC.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover - websockets is a pinned dependency
    ws_connect = None  # type: ignore[assignment]

_CAPABILITY_ENV = "HERMES_SESSION_SPAWN_CAPABILITY"
_SESSION_ENV = "HERMES_SESSION_ID"
_CAPABILITY_TTL_SECONDS = 3600.0
_TRANSPORT_TICKET_TTL_SECONDS = 30.0
_MAX_TRANSPORT_TICKETS = 128
_RPC_TIMEOUT_SECONDS = 15.0


class SessionSpawnBridgeError(RuntimeError):
    """A scoped bridge could not authenticate or reach its owner gateway."""


@dataclass(frozen=True)
class ScopedSessionCapability:
    token: str
    owner_session_id: str
    issued_at: float
    profile_home: str = ""
    session_generation: str = ""
    owner_session_key: str = ""


_capability_lock = threading.RLock()
_capabilities: dict[str, ScopedSessionCapability] = {}
_transport_tickets: dict[str, tuple[ScopedSessionCapability, float]] = {}
_transport_ticket_by_owner: dict[tuple[str, str], str] = {}


def capability_file_path(owner_session_id: str, profile_home: str | Path | None = None) -> Path:
    home = Path(profile_home or get_hermes_home()).resolve()
    return home / "runtime" / "session-spawn" / f"{str(owner_session_id).strip()}.cap"


def _effective_owner_home(record: dict) -> str:
    """Canonical state home used by issuance, discovery, authorization, and cleanup."""
    return str(Path(record.get("profile_home") or get_hermes_home()).resolve())


def _live_owner(owner_session_id: str) -> tuple[dict, str, str] | None:
    with contextlib.suppress(Exception):
        from tui_gateway import server
        with server._sessions_lock:
            runtime_id = owner_session_id if owner_session_id in server._sessions else ""
            record = server._sessions.get(owner_session_id)
            if record is None:
                runtime_id, record = next(((sid, item) for sid, item in server._sessions.items()
                                           if item.get("session_key") == owner_session_id), ("", None))
            if record is not None and not record.get("_finalized"):
                generation = str(record.get("session_generation") or "")
                if not generation:
                    generation = secrets.token_urlsafe(18)
                    record["session_generation"] = generation
                return record, generation, runtime_id
    return None


def issue_scoped_capability(owner_session_id: str) -> str | None:
    """Register a capability for one live Hermes session and return its secret.

    The secret is the only new environment value sent to the MCP child.  The
    server-side registry is authoritative; a child cannot choose its owner by
    changing RPC parameters.
    """
    owner = str(owner_session_id or "").strip()
    if not owner:
        return None
    live = _live_owner(owner)
    if live is None:
        return None
    record, generation, runtime_id = live
    profile_home = _effective_owner_home(record)
    token = secrets.token_urlsafe(32)
    capability = ScopedSessionCapability(token, runtime_id, time.time(), profile_home, generation,
                                         str(record.get("session_key") or ""))
    with _capability_lock:
        _capabilities[token] = capability
    return token


def revoke_scoped_capability(token: str | None) -> None:
    if token:
        with _capability_lock:
            record = _capabilities.pop(str(token), None)
            if record is not None:
                with contextlib.suppress(OSError):
                    capability_file_path(record.owner_session_id, record.profile_home).unlink()
                with contextlib.suppress(OSError):
                    capability_file_path(record.owner_session_key, record.profile_home).unlink()


def revoke_scoped_capabilities_for_session(session: dict | None) -> None:
    if not session:
        return
    sid = str(session.get("_sid") or session.get("session_id") or "")
    generation = str(session.get("session_generation") or "")
    path = capability_file_path(sid, session.get("profile_home")) if sid else None
    with _capability_lock:
        for token, record in list(_capabilities.items()):
            if ((sid and record.owner_session_id == sid) or
                    (generation and record.session_generation == generation)):
                _capabilities.pop(token, None)
                with contextlib.suppress(OSError):
                    capability_file_path(record.owner_session_id, record.profile_home).unlink()
                with contextlib.suppress(OSError):
                    capability_file_path(record.owner_session_key, record.profile_home).unlink()
                if path is not None:
                    with contextlib.suppress(OSError):
                        path.unlink()
        for ticket, (record, _expires_at) in list(_transport_tickets.items()):
            if record.owner_session_id == sid or record.session_generation == generation:
                _transport_tickets.pop(ticket, None)
                _transport_ticket_by_owner.pop((record.owner_session_id, record.session_generation), None)


def authorize_scoped_capability(token: str | None) -> ScopedSessionCapability | None:
    """Resolve a capability in the owner gateway process, failing closed."""
    presented = str(token or "")
    if not presented:
        return None
    with _capability_lock:
        record = _capabilities.get(presented)
        if record is None:
            _capabilities.pop(presented, None)
            return None
        # Keep the comparison explicit: callers must never use a partially
        # matched token as identity.
        if not hmac.compare_digest(record.token, presented):
            return None
        live = _live_owner(record.owner_session_id)
        if live is None or live[1] != record.session_generation:
            _capabilities.pop(presented, None)
            return None
        current, _, _ = live
        if _effective_owner_home(current) != record.profile_home:
            _capabilities.pop(presented, None)
            return None
        if str(current.get("session_key") or "") != record.owner_session_key:
            _capabilities.pop(presented, None)
            return None
        if time.time() - record.issued_at >= _CAPABILITY_TTL_SECONDS:
            _capabilities.pop(presented, None)
            return None
        return record


def capability_from_environment() -> tuple[str, str, Path] | None:
    token = os.environ.get(_CAPABILITY_ENV, "").strip()
    if not token and (path := os.environ.get("HERMES_SESSION_SPAWN_CAPABILITY_FILE", "").strip()):
        with contextlib.suppress(OSError):
            token = Path(path).read_text(encoding="utf-8").strip()
    owner = os.environ.get(_SESSION_ENV, "").strip()
    if not token or not owner:
        return None
    return token, owner, get_hermes_home()


def _discover_url(owner_session_id: str, registry_home: Path, token: str = "") -> str:
    from hermes_cli.active_sessions import active_session_registry_snapshot
    import httpx
    from urllib.parse import urlencode, urlsplit
    import ipaddress

    owners = [entry for entry in active_session_registry_snapshot(registry_home, strict=True)
              if entry.get("session_id") == owner_session_id or
              (entry.get("metadata") or {}).get("live_session_id") == owner_session_id]
    if len(owners) != 1:
        raise SessionSpawnBridgeError("owner gateway attachment was refused")
    owner = owners[0]
    endpoint = (owner.get("metadata") or {}).get("shared_runtime_url")
    if not isinstance(endpoint, str) or not endpoint:
        raise SessionSpawnBridgeError("owner gateway does not advertise cooperative attachment")
    endpoint_parts = urlsplit(endpoint)
    try:
        is_loopback = ipaddress.ip_address(endpoint_parts.hostname or "").is_loopback
    except ValueError:
        is_loopback = endpoint_parts.hostname == "localhost"
    if (endpoint_parts.scheme != "http" or not is_loopback or not endpoint_parts.port or
            endpoint_parts.username or endpoint_parts.password or endpoint_parts.path not in ("", "/") or
            endpoint_parts.query or endpoint_parts.fragment):
        raise SessionSpawnBridgeError("owner gateway returned a non-local attachment endpoint")
    query = urlencode({"session_id": owner.get("session_id"), "lease_id": owner.get("lease_id"),
                       "profile_home": str(registry_home.resolve())})
    try:
        response = httpx.get(endpoint.rstrip("/") + "/api/session-attach?" + query,
                             headers={"X-Hermes-Session-Spawn-Capability": token},
                             trust_env=False, follow_redirects=False, timeout=3.0)
        response.raise_for_status()
        reply = response.json()
    except Exception as exc:  # noqa: BLE001
        raise SessionSpawnBridgeError("owner gateway attachment was refused") from exc
    url = reply.get("websocket_url") if isinstance(reply, dict) else None
    ws_parts = urlsplit(url) if isinstance(url, str) else None
    try:
        ws_loopback = ipaddress.ip_address(ws_parts.hostname or "").is_loopback if ws_parts else False
    except ValueError:
        ws_loopback = bool(ws_parts and ws_parts.hostname == "localhost")
    if (not ws_parts or ws_parts.scheme != "ws" or not ws_loopback or not ws_parts.port or
            ws_parts.path != "/api/session-spawn-ws" or ws_parts.username or ws_parts.password):
        raise SessionSpawnBridgeError("owner gateway returned an invalid scoped endpoint")
    return url


def scoped_bridge_available() -> bool:
    """Probe the owner handshake without changing the MCP schema later."""
    context = capability_from_environment()
    if context is None:
        return False
    token, owner, home = context
    if authorize_scoped_capability(token) is None:
        # In the MCP child the registry is intentionally empty.  This probe is
        # for child-side construction; the owner validates the token on RPC.
        # Presence plus a successful cooperative handshake is sufficient here.
        pass
    try:
        _discover_url(owner, home, token)
    except SessionSpawnBridgeError:
        return False
    return True


class HermesGatewaySessionBridge:
    """One child-process client bound to one owner session and capability."""

    def __init__(self, token: str, owner_session_id: str, registry_home: Path | None = None):
        self.token = str(token or "").strip()
        self.owner_session_id = str(owner_session_id or "").strip()
        self.registry_home = Path(registry_home or get_hermes_home())
        if not self.token or not self.owner_session_id:
            raise SessionSpawnBridgeError("session-spawn capability is missing")

    @classmethod
    def from_environment(cls) -> "HermesGatewaySessionBridge":
        context = capability_from_environment()
        if context is None:
            raise SessionSpawnBridgeError("session-spawn capability is missing")
        token, owner, home = context
        return cls(token, owner, home)

    def is_reachable(self) -> bool:
        try:
            _discover_url(self.owner_session_id, self.registry_home, self.token)
        except SessionSpawnBridgeError:
            return False
        return True

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = _discover_url(self.owner_session_id, self.registry_home, self.token)
        if ws_connect is None:  # pragma: no cover - dependency is pinned
            raise SessionSpawnBridgeError("WebSocket bridge is unavailable")

        request_id = secrets.token_hex(8)
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {
                **params,
            },
        }
        try:
            with ws_connect(
                url,
                open_timeout=3.0,
                close_timeout=3.0,
                max_size=4 * 1024 * 1024,
            ) as websocket:
                deadline = time.monotonic() + _RPC_TIMEOUT_SECONDS
                websocket.send(json.dumps(request, ensure_ascii=False))
                while time.monotonic() < deadline:
                    raw = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    if isinstance(payload, dict) and payload.get("id") == request_id:
                        if payload.get("error"):
                            error = payload["error"]
                            message = error.get("message", "gateway refused request") if isinstance(error, dict) else str(error)
                            raise SessionSpawnBridgeError(str(message))
                        result = payload.get("result")
                        if not isinstance(result, dict):
                            raise SessionSpawnBridgeError("gateway returned an invalid task result")
                        return result
                raise SessionSpawnBridgeError("owner gateway timed out")
        except SessionSpawnBridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - clean MCP error boundary
            raise SessionSpawnBridgeError("owner gateway disconnected") from exc

    def create_task_session(self, *, cwd: str, task: str, title: str, request_id: str) -> dict[str, Any]:
        return self._rpc(
            "session.task_create",
            {"cwd": cwd, "task": task, "title": title, "request_id": request_id},
        )


def bridge_available_from_environment() -> bool:
    """Availability predicate used by the service-gated MCP registration."""
    try:
        return HermesGatewaySessionBridge.from_environment().is_reachable()
    except SessionSpawnBridgeError:
        return False


def issue_scoped_transport_ticket(capability: ScopedSessionCapability) -> str:
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with _capability_lock:
        for old_ticket, (_old_capability, expires_at) in list(_transport_tickets.items()):
            if expires_at <= now:
                _transport_tickets.pop(old_ticket, None)
        owner_key = (capability.owner_session_id, capability.session_generation)
        if old_ticket := _transport_ticket_by_owner.pop(owner_key, None):
            _transport_tickets.pop(old_ticket, None)
        while len(_transport_tickets) >= _MAX_TRANSPORT_TICKETS:
            oldest = min(_transport_tickets, key=lambda key: _transport_tickets[key][1])
            old_capability, _ = _transport_tickets.pop(oldest)
            _transport_ticket_by_owner.pop((old_capability.owner_session_id, old_capability.session_generation), None)
        _transport_tickets[ticket] = (capability, now + _TRANSPORT_TICKET_TTL_SECONDS)
        _transport_ticket_by_owner[owner_key] = ticket
    return ticket


def consume_scoped_transport_ticket(ticket: str | None) -> ScopedSessionCapability | None:
    if not ticket:
        return None
    with _capability_lock:
        entry = _transport_tickets.pop(str(ticket), None)
        if entry is None:
            return None
        capability, expires_at = entry
        _transport_ticket_by_owner.pop((capability.owner_session_id, capability.session_generation), None)
    if time.monotonic() >= expires_at:
        return None
    return authorize_scoped_capability(capability.token)


__all__ = [
    "HermesGatewaySessionBridge",
    "SessionSpawnBridgeError",
    "ScopedSessionCapability",
    "authorize_scoped_capability",
    "bridge_available_from_environment",
    "capability_from_environment",
    "capability_file_path",
    "consume_scoped_transport_ticket",
    "issue_scoped_capability",
    "issue_scoped_transport_ticket",
    "revoke_scoped_capabilities_for_session",
    "revoke_scoped_capability",
    "scoped_bridge_available",
]
