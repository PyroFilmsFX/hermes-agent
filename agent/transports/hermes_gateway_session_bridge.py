"""Scoped WebSocket bridge used by the hermes-tools session-spawn MCP tool.

The MCP server is a child process, so an in-process callback cannot safely reach
the owner gateway.  The parent registers a short-lived, session-bound capability;
the child discovers the owner's authenticated ``/api/ws`` endpoint through the
cooperative-attach handshake and can call only the internal task-create RPC.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
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
# Idle TTL: every successful full authorization slides it (a long turn that keeps using the
# capability never expires mid-turn), and the SDK session rotates the published capability at each
# turn start.  Before b3-30 this was a fixed TTL from issuance and nothing re-issued it, so every
# SDK session older than an hour got HTTP 401 from the owner gateway on session_send.
_CAPABILITY_TTL_SECONDS = 3600.0
# A rotated-out capability keeps full authority this long so an in-flight attach/RPC is not cut.
_ROTATION_GRACE_SECONDS = 120.0
_TRANSPORT_TICKET_TTL_SECONDS = 30.0
_MAX_TRANSPORT_TICKETS = 128
_RPC_TIMEOUT_SECONDS = 15.0
# Rotation-shaped refusals from the attach route itself (explicit JSON codes).
_QUEUE_FALLBACK_CODES = frozenset({"session_identity_mismatch", "lease_live_session_mismatch", "lease_not_found"})
# 4xx statuses that are NOT caller errors: auth refusals (an idle-expired capability is a 401 from
# the dashboard auth middleware), timeouts, and throttling.  Every other 4xx is a caller error.
_QUEUEABLE_4XX = frozenset({401, 403, 408, 425, 429})

logger = logging.getLogger(__name__)


class SessionSpawnBridgeError(RuntimeError):
    """A scoped bridge could not authenticate or reach its owner gateway."""

    def __init__(self, message: str, *, code: str = "", endpoint: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint
        self.status_code = status_code


@dataclass(frozen=True)
class ScopedSessionCapability:
    token: str
    owner_session_id: str
    issued_at: float
    profile_home: str = ""
    session_generation: str = ""
    owner_session_key: str = ""
    last_used: float = 0.0  # sliding idle-TTL anchor; 0 = unused since issuance
    revoke_after: float = 0.0  # set when rotated out: hard end of the grace window


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
        now = time.time()
        for old_token, old in list(_capabilities.items()):
            if old.revoke_after and now >= old.revoke_after:
                _capabilities.pop(old_token, None)
        _capabilities[token] = capability
    return token


def owner_capability_reissuable(owner_session_id: str, session_generation: str, profile_home: str) -> bool:
    """Whether a fresh capability COULD be issued to this exact owner: it is still a live, unfinalized
    session of the same generation whose effective profile home is unchanged. The same checks
    ``issue_scoped_capability``/``authorize_scoped_capability`` apply; used to re-validate rows enqueued
    through the queue-only route before they are delivered."""
    owner = str(owner_session_id or "").strip()
    if not owner or not session_generation or not profile_home:
        return False
    live = _live_owner(owner)
    if live is None or live[1] != str(session_generation):
        return False
    return _effective_owner_home(live[0]) == str(profile_home)


def rotate_scoped_capability(owner_session_id: str, previous_token: str | None = None,
                             *, grace_seconds: float = _ROTATION_GRACE_SECONDS) -> str | None:
    """Issue a fresh capability and retire ``previous_token`` after a grace window.

    The previous token keeps full authority for ``grace_seconds`` so a child call that already read
    it is not cut mid-flight; after that it is revoked (queue-only authentication included).
    """
    token = issue_scoped_capability(owner_session_id)
    if token and previous_token and previous_token != token:
        retire_scoped_capability(previous_token, grace_seconds=grace_seconds)
    return token


def retire_scoped_capability(token: str | None, *, grace_seconds: float = _ROTATION_GRACE_SECONDS) -> None:
    """Schedule revocation of a rotated-out capability after ``grace_seconds``."""
    if not token:
        return
    with _capability_lock:
        if (record := _capabilities.get(str(token))) is not None and not record.revoke_after:
            _capabilities[str(token)] = dataclasses.replace(
                record, revoke_after=time.time() + max(0.0, float(grace_seconds)))


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


def _authorize(token: str | None, *, queue_only: bool) -> ScopedSessionCapability | None:
    presented = str(token or "")
    if not presented:
        return None
    with _capability_lock:
        record = _capabilities.get(presented)
        if record is None:
            return None
        # Keep the comparison explicit: callers must never use a partially
        # matched token as identity.
        if not hmac.compare_digest(record.token, presented):
            return None
        now = time.time()
        if record.revoke_after and now >= record.revoke_after:
            _capabilities.pop(presented, None)
            return None
        live = _live_owner(record.owner_session_id)
        if live is None or live[1] != record.session_generation:
            _capabilities.pop(presented, None)
            return None
        current, _, _ = live
        if _effective_owner_home(current) != record.profile_home:
            _capabilities.pop(presented, None)
            return None
        if queue_only:
            # Idle expiry does not end sender authentication for a durable, non-delivering enqueue:
            # the record is still bound to the live generation and profile and is still revocable.
            return record
        if now - max(record.issued_at, record.last_used) >= _CAPABILITY_TTL_SECONDS:
            return None  # idle-expired: kept (not popped) so the queue-only route can authenticate
        refreshed = dataclasses.replace(record, last_used=now)
        _capabilities[presented] = refreshed
        return refreshed


def authorize_scoped_capability(token: str | None) -> ScopedSessionCapability | None:
    """Resolve a capability in the owner gateway process, failing closed.

    A successful authorization slides the idle TTL."""
    return _authorize(token, queue_only=False)


def authorize_scoped_capability_for_queue(token: str | None) -> ScopedSessionCapability | None:
    """Queue-only sender authentication for ``/api/session-send-queue``.

    Accepts an idle-expired capability, never a revoked, rotated-out (past grace), foreign-generation,
    or foreign-profile one, and never extends the capability's full authority."""
    return _authorize(token, queue_only=True)


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
    if not owners:
        raise SessionSpawnBridgeError("owner lease not found in session registry", code="owner_lease_not_found")
    if len(owners) > 1:
        raise SessionSpawnBridgeError("owner session has multiple registry leases", code="owner_lease_ambiguous")
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
        response = getattr(exc, "response", None)
        if response is not None and int(getattr(response, "status_code", 0) or 0) == 403:
            code = "attach_refused"
            with contextlib.suppress(Exception):
                body = response.json()
                if isinstance(body, dict) and body.get("code") in {
                    "capability_invalid", "profile_mismatch", "session_identity_mismatch",
                    "lease_registry_unavailable", "lease_not_found", "lease_live_session_mismatch",
                }:
                    code = body["code"]
            raise SessionSpawnBridgeError(f"owner gateway attach refused: {code}", code=code,
                                          endpoint=endpoint, status_code=403) from exc
        if response is not None:
            status_code = int(getattr(response, "status_code", 0) or 0)
            raise SessionSpawnBridgeError(f"owner gateway returned HTTP {status_code} during attach",
                                          code=f"attach_http_{status_code}", endpoint=endpoint,
                                          status_code=status_code) from exc
        raise SessionSpawnBridgeError("owner gateway transport failed during attach", code="attach_transport") from exc
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

    def send_to_session(self, *, target: str, body: str, request_id: str = "") -> dict[str, Any]:
        """Durable peer message (cntrl carry): the gateway reports delivered-live / resumed-and-delivered /
        queued / failed. The sender identity is the capability's owner, never a parameter."""
        params: dict[str, Any] = {"target": target, "body": body}
        if request_id:
            params["request_id"] = request_id
        try:
            return self._rpc("session.send", params)
        except SessionSpawnBridgeError as exc:
            if not _attach_refusal_is_queueable(exc):
                raise
            logger.warning(
                "session_send: owner gateway refused attach (HTTP %s, code=%s) for target=%s; queueing durably",
                exc.status_code or "?", exc.code or "?", target)
            import httpx

            try:
                response = httpx.post(
                    exc.endpoint.rstrip("/") + "/api/session-send-queue",
                    json=params,
                    headers={"X-Hermes-Session-Spawn-Capability": self.token},
                    trust_env=False,
                    follow_redirects=False,
                    timeout=3.0,
                )
                response.raise_for_status()
                result = response.json()
            except Exception as queue_exc:  # noqa: BLE001
                queue_status = getattr(getattr(queue_exc, "response", None), "status_code", None) or "transport"
                logger.warning("session_send: durable queue fallback failed (HTTP %s) for target=%s after attach HTTP %s",
                               queue_status, target, exc.status_code or "?")
                raise SessionSpawnBridgeError(
                    f"durable peer queue fallback failed (attach HTTP {exc.status_code or '?'}, queue {queue_status})",
                    code="queue_fallback_failed") from queue_exc
            if not isinstance(result, dict):
                raise SessionSpawnBridgeError("durable peer queue returned an invalid result", code="queue_fallback_invalid")
            return result


def _attach_refusal_is_queueable(exc: SessionSpawnBridgeError) -> bool:
    """Whether an attach refusal falls back to the capability-authenticated durable queue route.

    Queue on rotation-shaped refusal codes and on any non-2xx attach status that is not a caller
    error: 401 (an idle-expired capability, refused by the dashboard auth middleware), a 403 without
    a specific refusal code, 408/425/429, and 5xx.  Explicit misuse codes (``capability_invalid``,
    ``profile_mismatch``, ``lease_registry_unavailable``), caller-error 4xx, transport failures, and
    pre-HTTP discovery errors stay distinct non-queued outcomes."""
    if not exc.endpoint:
        return False
    if exc.code in _QUEUE_FALLBACK_CODES:
        return True
    status = int(exc.status_code or 0)
    if status and exc.code in {"attach_refused", f"attach_http_{status}"}:
        return status >= 500 or status in _QUEUEABLE_4XX
    return False


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
    "authorize_scoped_capability_for_queue",
    "bridge_available_from_environment",
    "capability_from_environment",
    "capability_file_path",
    "consume_scoped_transport_ticket",
    "issue_scoped_capability",
    "issue_scoped_transport_ticket",
    "revoke_scoped_capabilities_for_session",
    "owner_capability_reissuable",
    "revoke_scoped_capability",
    "rotate_scoped_capability",
    "scoped_bridge_available",
]
