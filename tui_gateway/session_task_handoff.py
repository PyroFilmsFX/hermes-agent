"""Gateway-side implementation of the scoped SDK session-create seam."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home, profile_name_for_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_RECEIPT_FILENAME = "session_spawn_receipts.json"
_MAX_RECEIPTS_PER_ROOT = 256
_MAX_TASK_CHARS = 8192
_receipt_lock = threading.RLock()


@contextlib.contextmanager
def _receipt_guard(profile_home: str | None):
    path = _receipt_path(profile_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _receipt_lock, path.with_suffix(path.suffix + ".lock").open("a+b") as lock_file:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            # Windows has no fcntl equivalent in this receipt path; refuse the concurrent whole-file
            # rewrite rather than racing. A native lock can replace this fail-closed branch later.
            raise RuntimeError("session spawn refuses concurrent Windows receipts without an interprocess lock")
        try:
            yield
        finally:
            if os.name != "nt":
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _error(rid: Any, code: int, message: str) -> dict:
    from tui_gateway import server

    return server._err(rid, code, message)


def _policy(profile_home: str | None = None) -> dict[str, int | bool]:
    defaults: dict[str, int | bool] = {
        "enabled": True, "max_children_per_root": 3, "max_depth": 2, "rate_per_minute": 5,
    }
    try:
        from agent.transports.claude_agent_sdk_session_config import _provider_config

        if profile_home:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
            token = set_hermes_home_override(profile_home)
            try:
                raw = _provider_config().get("session_spawn")
            finally:
                reset_hermes_home_override(token)
        else:
            raw = _provider_config().get("session_spawn")
        if not isinstance(raw, dict):
            return defaults
        out = dict(defaults)
        if "enabled" in raw:
            value = raw["enabled"]
            out["enabled"] = value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)
        for key in ("max_children_per_root", "max_depth", "rate_per_minute"):
            value = raw.get(key, defaults[key])
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = defaults[key]
            out[key] = max(0, value)
        return out
    except Exception:
        return defaults


def _receipt_home(profile_home: str | None) -> Path:
    return Path(profile_home) if profile_home else Path(get_hermes_home())


def _receipt_path(profile_home: str | None) -> Path:
    return _receipt_home(profile_home) / "runtime" / _RECEIPT_FILENAME


def _load_receipts(profile_home: str | None) -> dict[str, dict]:
    try:
        raw = json.loads(_receipt_path(profile_home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    receipts = raw.get("receipts") if isinstance(raw, dict) else None
    return receipts if isinstance(receipts, dict) else {}


def _save_receipts(profile_home: str | None, receipts: dict[str, dict]) -> None:
    path = _receipt_path(profile_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    for root in {str(row.get("root_session_id") or "") for row in receipts.values()}:
        terminal = [(key, row) for key, row in receipts.items()
                    if str(row.get("root_session_id") or "") == root and not row.get("active_reservation")]
        if len(terminal) > _MAX_RECEIPTS_PER_ROOT:
            terminal.sort(key=lambda item: float(item[1].get("created_at") or 0))
            for key, _row in terminal[:-_MAX_RECEIPTS_PER_ROOT]:
                receipts.pop(key, None)
    atomic_json_write(path, {"receipts": receipts}, indent=2, mode=0o600)


def _find_caller(owner_session_id: str) -> tuple[str, dict] | None:
    from tui_gateway import server

    with server._sessions_lock:
        if (direct := server._sessions.get(owner_session_id)) is not None:
            return owner_session_id, direct
    return None


def _effective_restrictions(caller: dict) -> dict[str, Any]:
    agent = caller.get("agent")
    inherited = caller.get("inherited_restrictions")
    if not isinstance(inherited, dict):
        inherited = {}
    denied_tools = set(inherited.get("denied_tools") or ())
    denied_tools.update(getattr(agent, "denied_tools", ()) or ())
    denied_toolsets = set(inherited.get("denied_toolsets") or ())
    denied_toolsets.update(getattr(agent, "disabled_toolsets", ()) or ())
    permission_mode = str(getattr(agent, "permission_mode", "") or
                          getattr(agent, "_permission_mode", "") or
                          inherited.get("permission_mode") or "")
    budget_cap = getattr(agent, "max_budget_usd", None)
    if budget_cap is None:
        budget_cap = inherited.get("budget_cap")
    return {
        "permission_mode": permission_mode,
        "denied_tools": sorted(denied_tools),
        "denied_toolsets": sorted(denied_toolsets),
        "budget_cap": budget_cap,
    }


def _cwd_identity(path: str) -> dict[str, Any]:
    stat = os.stat(path)
    return {"realpath": os.path.realpath(path), "st_dev": stat.st_dev, "st_ino": stat.st_ino}


def _cwd_identity_matches(path: str, identity: dict[str, Any]) -> bool:
    try:
        stat = os.stat(path)
        return (os.path.realpath(path) == identity.get("realpath") and
                stat.st_dev == identity.get("st_dev") and stat.st_ino == identity.get("st_ino"))
    except (OSError, TypeError):
        return False


def _canonical_roots(caller: dict) -> tuple[str, ...]:
    from tui_gateway import server
    from tui_gateway import git_probe

    candidates = [caller.get("cwd"), server._default_session_cwd(), server._launch_configured_cwd(), os.getcwd()]
    roots: list[str] = []
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        candidate = os.path.realpath(os.path.abspath(str(candidate)))
        root = git_probe.common_repo_root(candidate) or git_probe.repo_root(candidate) or candidate
        root = os.path.realpath(root)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _validate_cwd(raw: Any, caller: dict) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("cwd is required")
    cwd = os.path.realpath(os.path.abspath(os.path.expanduser(raw.strip())))
    if not os.path.isdir(cwd):
        raise ValueError("cwd must be an existing directory")
    roots = _canonical_roots(caller)
    if not roots:
        raise ValueError("owner gateway has no canonical workspace root")
    if not any(os.path.commonpath((cwd, root)) == root for root in roots):
        raise ValueError("cwd is outside the owner gateway's canonical workspace roots")
    return cwd


def _caller_model_provider(caller: dict) -> tuple[str, str]:
    agent = caller.get("agent")
    override = caller.get("model_override") if isinstance(caller.get("model_override"), dict) else {}
    model = str(getattr(agent, "model", "") or override.get("model") or "")
    provider = str(getattr(agent, "provider", "") or override.get("provider") or "")
    return model, provider


# A reservation is exclusive between reserve and child identity. A crashed or wedged creation must
# not strand the request_id forever, so the claim expires — generously, since session.create waits on
# a real backend build.
_CLAIM_TTL_SECONDS = 180.0


def _claim_is_live(receipt: dict) -> bool:
    """True while another in-flight request owns this receipt's reservation."""
    if receipt.get("child_session_id"):
        return False
    started = receipt.get("claim_started_at")
    if not isinstance(started, (int, float)):
        return False  # pre-claim receipt (older build): reclaimable, as before
    return (time.time() - float(started)) < _CLAIM_TTL_SECONDS


def _caller_lineage_id(caller: dict, capability: Any, profile_home: str | None) -> str:
    """The caller's DURABLE spawn identity.

    Compression gives a session a continuation ``session_key``; keying the quota off that made every
    compressed child look like a fresh root at depth 0 with no siblings and no rate history, so a
    caller could pass any depth/concurrency/rate limit by compressing first (W8 review A8-23). The
    session record carries the original id (``spawn_child_stored_session_id``), and the receipts
    themselves are the fallback map.
    """
    recorded = str(caller.get("spawn_child_stored_session_id") or "")
    if recorded:
        return recorded
    key = str(caller.get("session_key") or capability.owner_session_id)
    with contextlib.suppress(Exception):
        return child_lineage_identity(key, profile_home) or key
    return key


def _root_depth(receipts: dict[str, dict], stored_id: str) -> tuple[str, int]:
    current = stored_id
    depth = 0
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        parent = next((row for row in receipts.values() if row.get("child_stored_session_id") == current), None)
        if not parent:
            return current, depth
        current = str(parent.get("root_session_id") or current)
        depth = int(parent.get("depth") or 0)
        return current, depth
    return stored_id, depth


def _render_peer_name(stored_id: str, title: str, profile: str, model: str, child: dict | None) -> str:
    agent = (child or {}).get("agent")
    if agent is not None:
        with contextlib.suppress(Exception):
            from agent.claude_sdk_runtime_continuity import _sdk_session_name
            if name := _sdk_session_name(agent):
                return name
    from agent.transports.claude_agent_sdk_session_config import (
        _configured_session_name_template,
        render_sdk_session_name,
    )

    return render_sdk_session_name(
        _configured_session_name_template(), title=title, session=stored_id, profile=profile, model=model)


def _peer_is_ready(child: dict | None) -> bool:
    agent = (child or {}).get("agent")
    probe = getattr(agent, "peer_is_ready", None)
    if callable(probe):
        with contextlib.suppress(Exception):
            return bool(probe())
    return False


def _dispatch_existing(method: str, params: dict, request_id: str, transport=None) -> dict:
    from tui_gateway import server

    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    response = server.dispatch(request, transport=transport) if transport is not None else server.dispatch(request)
    if response is None:
        raise RuntimeError(f"{method} did not return a response")
    if response.get("error"):
        error = response["error"]
        raise RuntimeError(error.get("message", f"{method} failed") if isinstance(error, dict) else str(error))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} returned an invalid response")
    return result


def _dispatch_owned(method: str, params: dict, request_id: str, transport=None) -> dict:
    return (_dispatch_existing(method, params, request_id, transport)
            if transport is not None else _dispatch_existing(method, params, request_id))


def create_task_session(rid: Any, params: dict) -> dict:
    """Authorize, reserve, then create/title/submit exactly one child turn."""
    from agent.transports.hermes_gateway_session_bridge import authorize_scoped_capability
    from tui_gateway import server

    capability = authorize_scoped_capability(params.get("_session_spawn_capability"))
    if capability is None:
        return _error(rid, 4403, "session-spawn capability is invalid or revoked")
    caller_ref = _find_caller(capability.owner_session_id)
    if caller_ref is None:
        return _error(rid, 4403, "owner session is unavailable")
    caller_sid, caller = caller_ref
    caller_agent = caller.get("agent")
    if getattr(caller_agent, "api_mode", "") != "claude_agent_sdk":
        return _error(rid, 4403, "session-spawn is available only to Claude Agent SDK sessions")

    profile_home = caller.get("profile_home") or None
    policy = _policy(profile_home)
    if not policy["enabled"]:
        return _error(rid, 4033, "session spawning is disabled by owner policy")
    try:
        cwd = _validate_cwd(params.get("cwd"), caller)
    except ValueError as exc:
        return _error(rid, 4004, str(exc))
    task = params.get("task")
    title = params.get("title")
    request_id = params.get("request_id")
    if not all(isinstance(value, str) and value.strip() for value in (task, title, request_id)):
        return _error(rid, 4004, "task, title, and request_id are required")
    task, title, request_id = task.strip(), title.strip(), request_id.strip()
    task = task[:_MAX_TASK_CHARS]

    profile = profile_name_for_home(profile_home) or server._current_profile_name()
    root_session_id = _caller_lineage_id(caller, capability, profile_home)
    model, provider = _caller_model_provider(caller)
    caller_generation = str(caller.get("session_generation") or capability.session_generation)
    receipt_key = json.dumps([str(profile_home or ""), capability.owner_session_id,
                              caller_generation, request_id], separators=(",", ":"))
    receipt_profile = str(profile_home or "")

    cwd_identity = _cwd_identity(cwd)
    retry_existing = None
    receipt = None
    with _receipt_guard(profile_home):
        receipts = _load_receipts(profile_home)
        existing = receipts.get(receipt_key)
        if existing is not None:
            if any(existing.get(key) != value for key, value in {
                "cwd": cwd, "task": task, "title": title,
            }.items()):
                return _error(rid, 4093, "request_id was already used for a different task")
            result = existing.get("result")
            if isinstance(result, dict) and not (result.get("resumable") and existing.get("child_session_id")):
                return server._ok(rid, result)
            if existing.get("child_session_id"):
                retry_existing = existing
                receipt = existing
            elif _claim_is_live(existing):
                # Another request with this request_id is between reserve and child identity. Dropping
                # its receipt here let both create a child behind one quota entry (W8 review A8-26).
                return _error(rid, 4292, "a spawn for this request_id is already in progress")
            else:
                receipts.pop(receipt_key, None)
                _save_receipts(profile_home, receipts)

        if retry_existing is None:
            root_id, caller_depth = _root_depth(receipts, root_session_id)
            children = [row for row in receipts.values() if row.get("root_session_id") == root_id and row.get("active_reservation")]
            if caller_depth >= int(policy["max_depth"]):
                return _error(rid, 4293, "session spawn depth limit reached")
            if len(children) >= int(policy["max_children_per_root"]):
                return _error(rid, 4294, "session spawn concurrency limit reached")
            now = time.time()
            recent = [row for row in receipts.values() if row.get("root_session_id") == root_id and now - float(row.get("created_at") or 0) < 60]
            if len(recent) >= int(policy["rate_per_minute"]):
                return _error(rid, 4295, "session spawn rate limit reached")

            receipt = {
                "request_id": request_id, "root_session_id": root_id, "creator_session_id": root_session_id,
                "depth": caller_depth + 1, "cwd": cwd, "task": task, "title": title,
                "profile_home": receipt_profile, "created_at": now, "task_status": "created",
                "active_reservation": True, "caller_generation": caller_generation,
                # Held until child identity lands (or the claim goes stale); see _claim_is_live.
                "claim_started_at": now,
            }
            receipts[receipt_key] = receipt
            _save_receipts(profile_home, receipts)

    create_params = {"cwd": cwd, "title": title, "source": "claude-agent-sdk-session-spawn",
                     "_cwd_identity": cwd_identity, "_delegated_by": root_session_id,
                     "_delegated_restrictions": _effective_restrictions(caller)}
    if profile_home:
        create_params["profile"] = profile
    if model:
        create_params["model"] = model
    if provider:
        create_params["provider"] = provider

    try:
        owner_transport = caller.get("transport")
        if retry_existing is None:
            created = _dispatch_owned("session.create", create_params, f"{rid}:create", owner_transport)
            runtime_id = str(created.get("session_id") or "")
            stored_id = str(created.get("stored_session_id") or "")
            if not runtime_id or not stored_id:
                raise RuntimeError("session.create returned incomplete identity")
            with _receipt_guard(profile_home):
                receipts = _load_receipts(profile_home)
                receipt = receipts.get(receipt_key, receipt)
                receipt.update({"child_stored_session_id": stored_id, "child_session_id": runtime_id})
                receipts[receipt_key] = receipt
                _save_receipts(profile_home, receipts)
            title_result = _dispatch_owned("session.title", {"session_id": runtime_id, "title": title}, f"{rid}:title", owner_transport)
        else:
            created = {"info": {"cwd": cwd}}
            runtime_id = str(retry_existing.get("child_session_id") or "")
            stored_id = str(retry_existing.get("child_stored_session_id") or "")
            title_result = {"pending": False}
        if title_result.get("pending"):
            result = {"stored_session_id": stored_id, "session_id": runtime_id, "profile": profile,
                      "cwd": (created.get("info") or {}).get("cwd", cwd), "peer_name": title,
                      "task_status": "accepted", "peer_status": "starting", "resumable": True}
            with _receipt_guard(profile_home):
                receipts = _load_receipts(profile_home)
                receipt = receipts.get(receipt_key, receipt)
                receipt.update({"task_status": "accepted", "result": result})
                receipts[receipt_key] = receipt
                _save_receipts(profile_home, receipts)
            return server._ok(rid, result)
        with server._sessions_lock:
            child = server._sessions.get(runtime_id)
        actual_cwd = os.path.realpath(str((created.get("info") or {}).get("cwd") or cwd))
        if not _cwd_identity_matches(actual_cwd, cwd_identity):
            raise RuntimeError("cwd changed during session creation")
        submitted = _dispatch_owned(
            "prompt.submit", {"session_id": runtime_id, "text": task, "surface": "sdk-session-spawn"}, f"{rid}:submit", owner_transport)
        child_profile = profile_name_for_home((child or {}).get("profile_home")) or profile
        result = {
            "stored_session_id": stored_id, "session_id": runtime_id, "profile": child_profile,
            "cwd": actual_cwd, "peer_name": _render_peer_name(stored_id, title, child_profile, model, child),
            "task_status": "running" if (child or {}).get("execution_started") else "accepted",
            "peer_status": "ready" if _peer_is_ready(child) else "starting",
        }
        with _receipt_guard(profile_home):
            receipts = _load_receipts(profile_home)
            receipt = receipts.get(receipt_key, receipt)
            receipt.update({"child_stored_session_id": stored_id, "child_session_id": runtime_id,
                            "task_status": result["task_status"], "result": result})
            receipts[receipt_key] = receipt
            _save_receipts(profile_home, receipts)
        return server._ok(rid, result)
    except Exception as exc:  # noqa: BLE001 - preserve a durable failed receipt
        with _receipt_guard(profile_home):
            receipts = _load_receipts(profile_home)
            receipt = receipts.get(receipt_key)
            if receipt is not None:
                # Release the exclusive claim with the failure: a retry of this request_id must be
                # able to reserve again without waiting out the TTL (W8 review A8-26).
                receipt.update({"task_status": "failed", "error": str(exc), "claim_started_at": None})
                if receipt.get("child_stored_session_id") and receipt.get("child_session_id"):
                    failed_result = {
                        "stored_session_id": receipt["child_stored_session_id"],
                        "session_id": receipt["child_session_id"],
                        "profile": profile,
                        "cwd": cwd,
                        "peer_name": _render_peer_name(
                            receipt["child_stored_session_id"], title, profile, model, None),
                        "task_status": "failed",
                        "peer_status": "unavailable",
                    }
                    receipt["result"] = failed_result
                if receipt.get("child_session_id"):
                    # A child may still be live after title/submit failure; keep its reservation until teardown.
                    receipts[receipt_key] = receipt
                else:
                    receipts.pop(receipt_key, None)
                _save_receipts(profile_home, receipts)
        logger.warning("session spawn failed for %s: %s", request_id, exc)
        return _error(rid, 5008, f"session spawn failed: {exc}")


def release_child_reservation(stored_session_id: str | None, profile_home: str | None = None) -> None:
    """Release only the active concurrency slot; retain the receipt for idempotency and audit."""
    wanted = str(stored_session_id or "")
    if not wanted:
        return
    with _receipt_guard(profile_home):
        receipts = _load_receipts(profile_home)
        changed = False
        for receipt in receipts.values():
            if (receipt.get("active_reservation") and
                    wanted in {str(receipt.get("child_stored_session_id") or ""),
                               str(receipt.get("child_session_id") or "")}):
                receipt["active_reservation"] = False
                changed = True
        if changed:
            _save_receipts(profile_home, receipts)


def reconcile_child_reservations(profile_home: str | None = None) -> None:
    """Drop reservations whose child is not live in this gateway after a restart."""
    from tui_gateway import server

    with server._sessions_lock:
        live_runtime = set(server._sessions)
        live_stored = {str(row.get("session_key") or "") for row in server._sessions.values()}
    with _receipt_guard(profile_home):
        receipts = _load_receipts(profile_home)
        changed = False
        for receipt in receipts.values():
            if not receipt.get("active_reservation"):
                continue
            runtime = str(receipt.get("child_session_id") or "")
            stored = str(receipt.get("child_stored_session_id") or "")
            if runtime and runtime not in live_runtime and stored not in live_stored:
                receipt["active_reservation"] = False
                receipt["reconciled_at"] = time.time()
                changed = True
        if changed:
            _save_receipts(profile_home, receipts)


def child_lineage_identity(stored_session_id: str, profile_home: str | None = None) -> str:
    """Return the original durable child id across compression-created continuation ids."""
    wanted = str(stored_session_id or "")
    if not wanted:
        return ""
    receipts = _load_receipts(profile_home)
    for receipt in receipts.values():
        if wanted in {str(receipt.get("child_stored_session_id") or ""),
                      str(receipt.get("child_session_id") or "")}:
            return str(receipt.get("child_stored_session_id") or wanted)
    return wanted


__all__ = ["child_lineage_identity", "create_task_session", "reconcile_child_reservations", "release_child_reservation"]
