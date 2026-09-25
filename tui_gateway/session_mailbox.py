"""Always-on cross-session messaging — fork-local (cntrl carry; inventory #11, audit 2026-09-23 §4).

A message sent to a stored Hermes session is never dropped because the session has no live process:

1. ``send_message`` writes it to the durable ``peer_mailbox`` table (``hermes_state_peer_mailbox``) FIRST.
2. It then tries the target's live transport:
   - idle live Claude SDK target          -> native peer-origin SDK input -> ``delivered-native``
   - busy live Claude SDK target          -> native SDK input, held to its next boundary
   - live non-Claude target               -> ``prompt.submit(queued=True)`` -> ``delivered-live``
   - target not live, resume allowed      -> ``session.resume`` (cold) + submit -> ``resumed-and-delivered``
   - resume capped / rate-limited / off   -> ``queued`` (delivered later, see 3)
   - unknown target / attempts exhausted  -> ``failed``
3. Queued rows drain when the target next resumes (any ``session.resume``), at gateway startup, and on a
   slow retry tick — all bounded by the same resume caps.

Cost guards: at most ``max_concurrent_resumes`` mailbox-woken sessions exist at once (they are ordinary
detached sessions, so the WS-orphan/idle reapers retire them after their turn), one in-flight resume per
target, and a per-target minimum interval between resumes.

B-lite residency: sessions pinned in the sidebar (``sessions.pinned``) are revived at gateway startup (up
to ``max_resident_sessions``) and exempt from the idle/LRU/WS-orphan reapers while live.

Config (``config.yaml``, all optional)::

    peer_mailbox:
      enabled: true
      resume_on_send: true
      max_concurrent_resumes: 2
      per_target_resume_interval_s: 120
      max_attempts: 5
      pinned_resident: true
      max_resident_sessions: 3
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

STATUS_DELIVERED_LIVE = "delivered-live"
STATUS_DELIVERED_NATIVE = "delivered-native"
STATUS_RESUMED = "resumed-and-delivered"
STATUS_QUEUED = "queued"
STATUS_FAILED = "failed"

# Sources a peer message may wake. Messaging-platform rows belong to their adapters, and ``cli`` rows to an
# interactive terminal that a gateway-side resume could race on the same transcript.
_ADDRESSABLE_SOURCES = frozenset({"tui", "desktop", "claude-agent-sdk-session-spawn"})

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "resume_on_send": True,
    "max_concurrent_resumes": 2,
    "per_target_resume_interval_s": 120.0,
    "max_attempts": 5,
    "max_body_chars": 16000,
    "startup_delay_s": 30.0,
    "retry_interval_s": 60.0,
    "pinned_resident": True,
    "max_resident_sessions": 3,
}

# Claim owner for this process incarnation (pid for liveness checks + nonce against pid reuse).
_OWNER = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def policy() -> dict[str, Any]:
    """Effective ``peer_mailbox`` policy (defaults merged; bad values fall back)."""
    out = dict(_DEFAULTS)
    raw: Any = None
    with contextlib.suppress(Exception):
        from tui_gateway import server
        raw = (server._load_cfg() or {}).get("peer_mailbox")
    if not isinstance(raw, dict):
        return out
    for key, default in _DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        try:
            if isinstance(default, bool):
                out[key] = _truthy(value)
            elif isinstance(default, int):
                out[key] = max(0, int(value))
            else:
                out[key] = max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    return out


# ── storage access ──────────────────────────────────────────────────────


@contextlib.contextmanager
def _open_db(profile_home: str | None) -> Iterator[Any]:
    """The target profile's SessionDB (a registry-shared handle for a secondary profile)."""
    from tui_gateway import server

    if not profile_home:
        yield server._get_db()
        return
    from hermes_state_registry import acquire, release_or_close

    db = acquire(Path(profile_home) / "state.db")
    try:
        yield db
    finally:
        with contextlib.suppress(Exception):
            release_or_close(db)


def _tip(db, session_id: str) -> str:
    """Compression tip of ``session_id`` (messages always land on the live end of a lineage)."""
    with contextlib.suppress(Exception):
        return str(db.resolve_resume_session_id(session_id) or session_id)
    return session_id


def _resolve_target(db, target: str) -> dict | None:
    """Stored id is identity; a title or rendered ``hermes:<title>`` peer name is only a hint."""
    raw = str(target or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if raw.lower().startswith("hermes:"):
        candidates.append(raw.split(":", 1)[1].strip())
    for candidate in candidates:
        with contextlib.suppress(Exception):
            if row := db.get_session(candidate):
                return row
        with contextlib.suppress(Exception):
            if row := db.get_session_by_title(candidate):
                return row
    return None


# ── gateway calls ───────────────────────────────────────────────────────


def _find_live(session_key: str, profile_home: str | None) -> tuple[str, dict] | None:
    from tui_gateway import server

    return server._find_live_session_by_key(session_key, profile_home or None)


def emit_settled(msg_id: Any, status: str, attempts: int = 0, sender_sid: str = "") -> None:
    """Emit peer_mailbox.settled event to sender session and broadcast."""
    from tui_gateway import server

    payload = {
        "msg_id": str(msg_id),
        "status": status,
        "attempts": int(attempts),
    }
    if sender_sid:
        server._emit("peer_mailbox.settled", sender_sid, payload)
    server._broadcast_global_event("peer_mailbox.settled", payload)


def _emit_mailbox_woken_lifecycle(sid: str, row: dict, profile_home: str | None = None) -> None:
    from tui_gateway import server

    now = time.time()
    sender_label = str(row.get("from_label") or row.get("from_session_id") or "another session")
    metadata = {
        "event": "woken",
        "source": "peer-mailbox",
        "by": sender_label,
        "from": sender_label,
        "from_session_id": str(row.get("from_session_id") or ""),
        "completed_at": now,
        "msg_id": str(row.get("id") or ""),
    }
    label = f"woken by peer message: {sender_label}"
    lifecycle_row = {
        "role": "system",
        "content": label,
        "display_kind": "session_lifecycle",
        "display_metadata": metadata,
        "timestamp": now,
    }
    with contextlib.suppress(Exception):
        with _open_db(profile_home) as sdb:
            if sdb is not None:
                target_sid = _tip(sdb, str(row.get("target_session_id") or ""))
                sdb.append_messages_batch(target_sid, [lifecycle_row])
    with server._sessions_lock:
        session = server._sessions.get(sid)
        if session is not None:
            history = list(session.get("history") or [])
            history.append(lifecycle_row)
            session["history"] = history
            session["history_version"] = int(session.get("history_version", 0)) + 1


def _submit(sid: str, text: str, *, display_kind: str | None = None, display_metadata: dict | None = None) -> dict:
    """``prompt.submit(queued=True)`` with NO bound transport: a peer turn must not re-point the session's
    client transport (a live desktop window keeps streaming it), and it never interrupts a turn in flight."""
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport

    token = bind_transport(None)
    try:
        params: dict[str, Any] = {"session_id": sid, "text": text, "queued": True}
        if display_kind:
            params["display_kind"] = display_kind
        if display_metadata:
            params["display_metadata"] = display_metadata
        return server._methods["prompt.submit"](f"peer-mailbox:{sid}", params)
    finally:
        reset_transport(token)


def _resume(target: str, profile_home: str | None) -> tuple[str, str]:
    """Cold ``session.resume`` bound to the detached-WS sink → ``(runtime sid, error)``. The sink keeps the
    woken session quiet and reapable once idle; a desktop that opens it later reattaches normally."""
    from hermes_constants import profile_name_for_home
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport

    params: dict[str, Any] = {"session_id": target, "_peer_mailbox_internal": True}
    if profile_home:
        params["profile"] = profile_name_for_home(profile_home) or ""
    token = bind_transport(server._detached_ws_transport)
    try:
        response = server._methods["session.resume"](f"peer-mailbox-resume:{target}", params)
    except Exception as exc:  # noqa: BLE001 - reported to the sender as a delivery error
        return "", str(exc)
    finally:
        reset_transport(token)
    if not isinstance(response, dict) or response.get("error"):
        error = (response or {}).get("error") if isinstance(response, dict) else None
        return "", (error.get("message") if isinstance(error, dict) else str(error or "resume failed"))
    return str((response.get("result") or {}).get("session_id") or ""), ""


# ── cost guard ──────────────────────────────────────────────────────────


class _ResumeGate:
    """Bounds resume-on-send: one in-flight resume per target, a per-target minimum interval, and a global
    cap on concurrently live mailbox-woken sessions (plus in-flight resumes)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._last: dict[str, float] = {}

    def acquire(self, target: str, pol: dict, live_woken: int) -> str:
        """'' when the caller may resume ``target`` (then it MUST call :meth:`release`), else the refusal."""
        now = time.monotonic()
        with self._lock:
            if target in self._inflight:
                return "a resume of this session is already in progress"
            last = self._last.get(target)
            interval = float(pol["per_target_resume_interval_s"])
            if last is not None and now - last < interval:
                return f"resume rate-limited for this session (retry after {interval - (now - last):.0f}s)"
            if live_woken + len(self._inflight) >= int(pol["max_concurrent_resumes"]):
                return "resume concurrency cap reached; delivery deferred"
            self._inflight.add(target)
            self._last[target] = now
            return ""

    def release(self, target: str) -> None:
        with self._lock:
            self._inflight.discard(target)

    def reset(self) -> None:
        with self._lock:
            self._inflight.clear()
            self._last.clear()


_gate = _ResumeGate()


def _live_woken_count() -> int:
    from tui_gateway import server

    with server._sessions_lock:
        return sum(1 for s in server._sessions.values()
                   if isinstance(s, dict) and s.get("_peer_mailbox_woken") and not s.get("pinned_resident")
                   and not s.get("_finalized"))


# ── delivery ────────────────────────────────────────────────────────────


def _envelope(row: dict) -> str:
    """What the receiving model sees for a mailbox delivery, on EVERY transport (see
    agent.transports.claude_sdk_peer_envelope: the CLI drops stream-json origin)."""
    from agent.transports.claude_sdk_peer_envelope import build

    return build(sender=str(row.get("from_session_id") or ""), label=str(row.get("from_label") or ""),
                 msg_id=row.get("id"), body=str(row.get("body") or ""))


def _live_claude_sdk(session: dict) -> Any | None:
    """Return the live Claude SDK transport owned by this gateway session, if any."""
    agent = session.get("agent")
    if getattr(agent, "api_mode", "") != "claude_agent_sdk":
        return None
    return getattr(agent, "_claude_sdk_session", None)


def _peer_origin(row: dict) -> dict[str, Any]:
    return {
        "kind": "peer",
        "subkind": "peer-send-message",
        "from": str(row.get("from_label") or row.get("from_session_id") or "another session"),
        "fromSession": str(row.get("from_session_id") or ""),
        "msg_id": str(row.get("id") or ""),
        "body": str(row.get("body") or ""),
    }


def _deliver_native_claimed(db, row: dict, live: tuple[str, dict], *, pol: dict) -> tuple[str, str] | None:
    """Inject a peer-origin message into an idle, live Claude SDK session.

    ``None`` means this is not a native target. The SDK holds input for a busy CLI turn
    until its next boundary; a declined/failed native attempt falls through to the live prompt path.
    """
    sid, session = live
    sdk_session = _live_claude_sdk(session)
    if sdk_session is None:
        return None
    if not db.peer_mailbox_claim(row["id"], _OWNER):
        return STATUS_QUEUED, "another delivery of this message is in progress"
    try:
        # The CLI ignores origin on stream-json input: the envelope is the only peer marking the
        # model will see, so native and live deliveries carry the same text.
        accepted = sdk_session.send_peer_message(_envelope(row), _peer_origin(row))
    except Exception:
        logger.debug("peer mailbox native delivery failed for %s", sid, exc_info=True)
        accepted = False
    if not accepted:
        # The same claimed row falls back to the Hermes queue; its dedupe key remains intact.
        return _submit_claimed(db, row, sid, via="live", ok_status=STATUS_DELIVERED_LIVE, pol=pol)
    if not db.peer_mailbox_mark_delivered(row["id"], _OWNER, "native"):
        logger.warning("peer mailbox: row %s accepted natively but its claim was lost before settlement", row["id"])
    emit_settled(row["id"], STATUS_DELIVERED_NATIVE, int(row.get("attempts") or 0), str(row.get("from_session_id") or ""))
    return STATUS_DELIVERED_NATIVE, "accepted by the live Claude session"


def _submit_claimed(db, row: dict, sid: str, *, via: str, ok_status: str, pol: dict) -> tuple[str, str]:
    """Deliver a row WE hold the claim on. A refused submit returns the row to the queue (or fails it once
    ``max_attempts`` refusals accumulated); an accepted one is marked delivered and never resent."""
    meta = {
        "direction": "in",
        "peer": str(row.get("from_label") or row.get("from_session_id") or "peer"),
        "from": str(row.get("from_label") or row.get("from_session_id") or ""),
        "from_session_id": str(row.get("from_session_id") or ""),
        "to": str(row.get("target_session_id") or ""),
        "msg_id": str(row.get("id") or ""),
        "via": via,
        "status": ok_status,
        "attempts": int(row.get("attempts") or 0),
    }
    try:
        response = _submit(sid, _envelope(row), display_kind="peer_message", display_metadata=meta)
    except Exception as exc:  # noqa: BLE001 - raised before acceptance
        response = {"error": {"message": str(exc)}}
    if isinstance(response, dict) and response.get("error"):
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        status = db.peer_mailbox_release(row["id"], _OWNER, f"delivery refused: {message}",
                                         max_attempts=int(pol["max_attempts"]))
        res_status = STATUS_FAILED if status == "failed" else STATUS_QUEUED
        attempts = int(row.get("attempts") or 0) + 1
        emit_settled(row["id"], res_status, attempts, str(row.get("from_session_id") or ""))
        return res_status, f"delivery refused: {message}"
    if not db.peer_mailbox_mark_delivered(row["id"], _OWNER, via):
        logger.warning("peer mailbox: row %s accepted but its claim was lost before settlement", row["id"])
    emit_settled(row["id"], ok_status, int(row.get("attempts") or 0), str(row.get("from_session_id") or ""))
    return ok_status, "accepted as the session's next turn"


def _deliver_row(db, row: dict, *, profile_home: str | None, allow_resume: bool, pol: dict) -> tuple[str, str]:
    target = _tip(db, str(row["target_session_id"]))
    live = _find_live(target, profile_home)
    if live is not None:
        native = _deliver_native_claimed(db, row, live, pol=pol)
        if native is not None:
            return native
        if not db.peer_mailbox_claim(row["id"], _OWNER):
            return STATUS_QUEUED, "another delivery of this message is in progress"
        return _submit_claimed(db, row, live[0], via="live", ok_status=STATUS_DELIVERED_LIVE, pol=pol)
    with contextlib.suppress(Exception):
        if not db.get_session(target):
            db.peer_mailbox_fail(row["id"], "target session no longer exists")
            emit_settled(row["id"], STATUS_FAILED, int(row.get("attempts") or 0), str(row.get("from_session_id") or ""))
            return STATUS_FAILED, "target session no longer exists"
    if not allow_resume:
        return STATUS_QUEUED, "target is not live; queued until it next starts"
    refusal = _gate.acquire(target, pol, _live_woken_count())
    if refusal:
        return STATUS_QUEUED, refusal
    try:
        if not db.peer_mailbox_claim(row["id"], _OWNER):
            return STATUS_QUEUED, "another delivery of this message is in progress"
        sid, error = _resume(target, profile_home)
        if not sid:
            status = db.peer_mailbox_release(row["id"], _OWNER, f"resume failed: {error}",
                                             max_attempts=int(pol["max_attempts"]))
            res_status = STATUS_FAILED if status == "failed" else STATUS_QUEUED
            attempts = int(row.get("attempts") or 0) + 1
            emit_settled(row["id"], res_status, attempts, str(row.get("from_session_id") or ""))
            return res_status, f"resume failed: {error}"
        from tui_gateway import server
        with server._sessions_lock:
            if (session := server._sessions.get(sid)) is not None and not session.get("pinned_resident"):
                session["_peer_mailbox_woken"] = True
        _emit_mailbox_woken_lifecycle(sid, row, profile_home)
        return _submit_claimed(db, row, sid, via="resume", ok_status=STATUS_RESUMED, pol=pol)
    finally:
        _gate.release(target)


def _status_of_existing(row: dict) -> str:
    status = row.get("status")
    if status == "delivered":
        return {
            "resume": STATUS_RESUMED,
            "native": STATUS_DELIVERED_NATIVE,
        }.get(row.get("delivered_via"), STATUS_DELIVERED_LIVE)
    return STATUS_FAILED if status == "failed" else STATUS_QUEUED


def send_message(
    *, target: str, body: str, from_session_id: str = "", from_label: str = "", request_id: str = "",
    profile_home: str | None = None,
) -> dict:
    """Queue durably, then try to deliver. Always returns ``{"status": ...}`` — never raises."""
    pol = policy()
    if not pol["enabled"]:
        return {"status": STATUS_FAILED, "error": "peer mailbox is disabled by owner policy"}
    if not isinstance(body, str) or not body.strip():
        return {"status": STATUS_FAILED, "error": "body is required"}
    if pol["max_body_chars"] and len(body) > int(pol["max_body_chars"]):
        return {"status": STATUS_FAILED, "error": f"body exceeds {pol['max_body_chars']} characters"}
    try:
        with _open_db(profile_home) as db:
            if db is None:
                return {"status": STATUS_FAILED, "error": "session store is unavailable"}
            found = _resolve_target(db, target)
            if not found:
                return {"status": STATUS_FAILED, "error": f"no stored session matches {target!r}"}
            source = str(found.get("source") or "")
            if source not in _ADDRESSABLE_SOURCES:
                return {"status": STATUS_FAILED, "error": f"session source {source!r} cannot receive peer messages"}
            tip = _tip(db, str(found["id"]))
            if from_session_id and tip in {from_session_id, _tip(db, from_session_id)}:
                return {"status": STATUS_FAILED, "error": "a session cannot message itself"}
            dedupe_key = f"{from_session_id or 'operator'}:{request_id}" if request_id else None
            row, created = db.peer_mailbox_enqueue(
                target_session_id=tip, body=body, from_session_id=from_session_id, from_label=from_label,
                dedupe_key=dedupe_key, target_hint=str(target))
            base = {"message_id": row["id"], "target_session_id": tip, "target_title": found.get("title") or ""}
            if not created:
                return {**base, "status": _status_of_existing(row), "duplicate": True,
                        "detail": row.get("last_error") or "already accepted under this request_id"}
            status, detail = _deliver_row(db, row, profile_home=profile_home,
                                          allow_resume=bool(pol["resume_on_send"]), pol=pol)
            if status == STATUS_QUEUED:
                emit_settled(row["id"], STATUS_QUEUED, int(row.get("attempts") or 0), str(from_session_id or ""))
    except Exception as exc:  # noqa: BLE001 - tool boundary: report, never raise
        logger.warning("peer mailbox send failed", exc_info=True)
        return {"status": STATUS_FAILED, "error": f"peer mailbox error: {exc}"}
    if status == STATUS_RESUMED:
        schedule_drain(tip, profile_home)  # anything else queued for the woken session rides along
    result = {**base, "status": status, "detail": detail}
    if (sender := _find_live(from_session_id, profile_home)) is not None:
        sender_agent = sender[1].get("agent")
        if _live_claude_sdk(sender[1]) is not None:
            target_live = _find_live(tip, profile_home)
            if target_live is not None and _live_claude_sdk(target_live[1]) is not None:
                with contextlib.suppress(Exception):
                    target_agent = target_live[1].get("agent")
                    from agent.claude_sdk_runtime_continuity import _sdk_session_name
                    native_peer = _sdk_session_name(target_agent)
                    if native_peer.startswith("hermes:"):
                        result["native_peer"] = native_peer
    return result


def drain_session(session_key: str, profile_home: str | None = None) -> int:
    """Deliver every queued row addressed to ``session_key`` (or a lineage ancestor) into its LIVE session.
    Never resumes. Returns the number of rows accepted."""
    pol = policy()
    if not pol["enabled"] or not session_key:
        return 0
    delivered = 0
    with _open_db(profile_home) as db:
        if db is None:
            return 0
        targets = [t for t in db.peer_mailbox_pending_targets(limit=200)
                   if t == session_key or _tip(db, t) == session_key]
        for target in targets:
            for row in db.peer_mailbox_pending(target, limit=50):
                live = _find_live(session_key, profile_home)
                if live is None:
                    return delivered
                native = _deliver_native_claimed(db, row, live, pol=pol)
                if native is not None:
                    status, _ = native
                else:
                    if not db.peer_mailbox_claim(row["id"], _OWNER):
                        continue
                    status, _ = _submit_claimed(db, row, live[0], via="drain", ok_status=STATUS_DELIVERED_LIVE,
                                                pol=pol)
                delivered += status in (STATUS_DELIVERED_LIVE, STATUS_DELIVERED_NATIVE)
    return delivered


def schedule_drain(session_key: str, profile_home: str | None = None, delay_s: float = 0.5) -> None:
    """Drain off the calling RPC's path (a resume response must not wait on peer turns)."""
    def _run() -> None:
        try:
            drain_session(session_key, profile_home)
        except Exception:
            logger.debug("peer mailbox drain failed for %s", session_key, exc_info=True)

    timer = threading.Timer(delay_s, _run)
    timer.daemon = True
    timer.start()


def recover_stale_claims(db) -> int:
    """Settle claims whose owner process is gone as ``failed``: the turn may already have been accepted,
    and at-most-once forbids guessing. A claim by a live sibling process is left alone."""
    from tui_gateway.session_task_handoff import _owner_process_alive

    settled = 0
    for row in db.peer_mailbox_claimed():
        owner = str(row.get("claim_owner") or "")
        if owner == _OWNER:
            continue
        pid = owner.split(":", 1)[0]
        stale = pid == str(os.getpid()) or not _owner_process_alive(owner)
        if stale and db.peer_mailbox_fail(row["id"], "delivery outcome unknown: the delivering process exited "
                                                     "mid-delivery (not retried to avoid a duplicate turn)"):
            settled += 1
    return settled


def drain_pending(profile_home: str | None = None, limit: int = 20) -> int:
    """Startup/retry pass: for each target with queued mail, deliver (resuming within the caps)."""
    pol = policy()
    if not pol["enabled"]:
        return 0
    delivered = 0
    with _open_db(profile_home) as db:
        if db is None:
            return 0
        recover_stale_claims(db)
        for target in db.peer_mailbox_pending_targets(limit=limit):
            rows = db.peer_mailbox_pending(target, limit=1)
            if not rows:
                continue
            status, _ = _deliver_row(db, rows[0], profile_home=profile_home, allow_resume=True, pol=pol)
            if status in (STATUS_DELIVERED_LIVE, STATUS_DELIVERED_NATIVE, STATUS_RESUMED):
                delivered += 1 + drain_session(_tip(db, target), profile_home)
    return delivered


# ── B-lite residency (pinned sessions) ──────────────────────────────────


def session_is_resident(session: dict | None) -> bool:
    """True for a live pinned session the reapers must leave alone."""
    return bool(session and session.get("pinned_resident")) and bool(policy()["pinned_resident"])


def after_session_resume(ctx: Any, params: dict, response: dict) -> None:
    """``session.resume`` hook: stamp pinned residency and drain queued peer mail into the new session."""
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict) or getattr(ctx, "lazy", False):
        return
    pol = policy()
    from tui_gateway import server

    sid = str(result.get("session_id") or "")
    found = getattr(ctx, "found", None) or {}
    if pol["pinned_resident"] and found.get("pinned"):
        with server._sessions_lock:
            if (session := server._sessions.get(sid)) is not None:
                session["pinned_resident"] = True
                session.pop("_peer_mailbox_woken", None)
    if pol["enabled"] and not params.get("_peer_mailbox_internal"):
        home = getattr(ctx, "profile_home", None)
        schedule_drain(str(getattr(ctx, "target", "") or ""), str(home) if home else None)


def revive_resident_sessions() -> list[str]:
    """Cold-resume up to ``max_resident_sessions`` pinned sessions at startup (launch profile). The agent is
    built in the background; the Claude CLI itself still starts on the first turn (lazy)."""
    pol = policy()
    limit = int(pol["max_resident_sessions"])
    if not pol["pinned_resident"] or limit <= 0:
        return []
    from tui_gateway import server

    db = server._get_db()
    if db is None:
        return []
    revived: list[str] = []
    seen: set[str] = set()
    for candidate in db.list_resident_session_candidates(limit=limit * 3):
        tip = _tip(db, candidate)
        if tip in seen:
            continue
        seen.add(tip)
        if len(seen) > limit:
            break
        live = _find_live(tip, None)
        sid = live[0] if live is not None else _resume(tip, None)[0]
        if not sid:
            continue
        with server._sessions_lock:
            if (session := server._sessions.get(sid)) is not None:
                session["pinned_resident"] = True
        revived.append(tip)
    if revived:
        logger.info("peer mailbox: %d pinned session(s) resident: %s", len(revived), ", ".join(revived))
    return revived


def resume_interrupted_sessions() -> list[str]:
    """Cold-resume fresh interrupted launch-profile sessions through the mailbox resume gate."""
    from tui_gateway import server
    from tui_gateway.turn_marker import clear_turn_marker, read_turn_markers

    db = server._get_db()
    if db is None:
        return []
    home = server._hermes_home
    _enabled, freshness_secs, max_attempts = server._auto_continue_config()
    pol = policy()
    resumed: list[str] = []
    now = time.time()
    for session_key, marker in read_turn_markers(home).items():
        if not marker.get("auto_continue", True):
            continue
        if (not _enabled or now - float(marker.get("started_at") or 0) > freshness_secs
                or int(marker.get("attempts") or 0) >= max_attempts):
            clear_turn_marker(home, session_key)
            continue
        target = _tip(db, session_key)
        try:
            row = db.get_session(target)
        except Exception:
            continue
        if not row or row.get("source") == "bot_room":
            continue
        # A pinned revive or a desktop reconnect may have already resumed this marker and scheduled its
        # after-resume continuation. Do not dispatch a second resume for the same conversation.
        if _find_live(target, None) is not None:
            continue
        refusal = _gate.acquire(target, pol, _live_woken_count())
        if refusal:
            logger.info("interrupted-session resume deferred for %s: %s", target, refusal)
            continue
        try:
            # Preserve the marker's original lineage id through session.resume so its existing
            # after-resume hook reads the same marker key even when compression rotated to ``target``.
            sid, error = _resume(session_key, None)
            if not sid:
                logger.info("interrupted-session resume failed for %s: %s", target, error)
                continue
            with server._sessions_lock:
                if (session := server._sessions.get(sid)) is not None and not session.get("pinned_resident"):
                    session["_peer_mailbox_woken"] = True
            resumed.append(target)
        finally:
            _gate.release(target)
    if resumed:
        logger.info("peer mailbox: resumed %d interrupted session(s): %s", len(resumed), ", ".join(resumed))
    return resumed


# ── startup + retry loop ────────────────────────────────────────────────

_startup_lock = threading.Lock()
_startup_ran = False
_stop = threading.Event()


def schedule_startup_delivery() -> None:
    """Once per process: after ``startup_delay_s`` (lets a reconnecting desktop resume its own sessions
    first) revive pinned residents, then drain queued mail and keep retrying every ``retry_interval_s``."""
    global _startup_ran
    pol = policy()
    with _startup_lock:
        if _startup_ran:
            return
        _startup_ran = True

    def _loop() -> None:
        if _stop.wait(float(pol["startup_delay_s"])):
            return
        with contextlib.suppress(Exception):
            revive_resident_sessions()
        with contextlib.suppress(Exception):
            resume_interrupted_sessions()
        while not _stop.is_set():
            try:
                # A previous pass may have deferred markers at the concurrency cap. Revisit them as
                # mailbox-woken sessions finish and release their residency slots.
                resume_interrupted_sessions()
                drain_pending()
            except Exception:
                logger.debug("peer mailbox retry pass failed", exc_info=True)
            interval = float(policy()["retry_interval_s"])
            if interval <= 0 or _stop.wait(interval):
                return

    atexit.register(_stop.set)
    threading.Thread(target=_loop, name="hermes-peer-mailbox", daemon=True).start()


# ── RPC ─────────────────────────────────────────────────────────────────


def send_rpc(rid: Any, params: dict) -> dict:
    """``session.send``. Via the scoped session-spawn bridge the sender IS the capability's owner session
    (never a parameter); an admitted unscoped client (the desktop) already holds ``prompt.submit`` on every
    session, so its messages are labelled ``operator``."""
    from tui_gateway import server

    token = params.get("_session_spawn_capability")
    if token is not None:
        from agent.transports.hermes_gateway_session_bridge import authorize_scoped_capability

        capability = authorize_scoped_capability(token)
        if capability is None:
            return server._err(rid, 4403, "session capability is invalid or revoked")
        with server._sessions_lock:
            caller = server._sessions.get(capability.owner_session_id)
        if caller is None:
            return server._err(rid, 4403, "owner session is unavailable")
        from_session_id = str(caller.get("session_key") or capability.owner_session_id)
        from_label = ""
        with contextlib.suppress(Exception):
            from_label = server._session_live_title(caller, from_session_id) or ""
        from_label = from_label or from_session_id
        profile_home = caller.get("profile_home") or None
    else:
        from_session_id = ""
        from_label = "operator"  # a client never names the sender
        home = server._profile_home(params.get("profile")) if params.get("profile") else None
        profile_home = str(home) if home else None
    target, body = params.get("target"), params.get("body")
    request_id = params.get("request_id")
    if not isinstance(target, str) or not target.strip():
        return server._err(rid, 4004, "target is required")
    if not isinstance(body, str):
        return server._err(rid, 4004, "body is required")
    result = send_message(
        target=target.strip(), body=body, from_session_id=from_session_id, from_label=str(from_label),
        request_id=request_id.strip() if isinstance(request_id, str) else "", profile_home=profile_home)
    return server._ok(rid, result)


def list_messages(session_id: str | None = None, limit: int = 50, pending_only: bool = False, profile_home: str | None = None) -> list[dict]:
    with _open_db(profile_home) as db:
        if db is None:
            return []
        tip = _tip(db, session_id)
        rows = db.peer_mailbox_for_session(tip, limit=limit, pending_only=pending_only)
        out = []
        for r in rows:
            row_dict = dict(r)
            if session_id:
                row_dict["direction"] = "out" if str(row_dict.get("from_session_id")) == session_id else "in"
            out.append(row_dict)
        return out


def _row_belongs_to_session(db, row: dict, session_id: str) -> bool:
    session_id = str(session_id or "").strip()
    if not session_id:
        return False
    allowed = {session_id, _tip(db, session_id)}
    return str(row.get("target_session_id") or "") in allowed or str(
        row.get("from_session_id") or ""
    ) in allowed


def retry_message(message_id: int, session_id: str, profile_home: str | None = None) -> tuple[str, str]:
    pol = policy()
    with _open_db(profile_home) as db:
        if db is None:
            return STATUS_FAILED, "database unavailable"
        row = db.peer_mailbox_get(message_id)
        if not row:
            return STATUS_FAILED, "message not found"
        if not _row_belongs_to_session(db, row, session_id):
            return STATUS_FAILED, "message does not belong to session"
        if row.get("status") == "delivered":
            return row.get("delivered_via", "delivered"), "message is already delivered"
        if row.get("status") == "claimed":
            return STATUS_QUEUED, "delivery already in progress"
        status, detail = _deliver_row(db, row, profile_home=profile_home, allow_resume=True, pol=pol)
        return status, detail


def cancel_message(message_id: int, session_id: str, profile_home: str | None = None) -> bool:
    with _open_db(profile_home) as db:
        if db is None:
            return False
        row = db.peer_mailbox_get(message_id)
        if not row or not _row_belongs_to_session(db, row, session_id):
            return False
        if row.get("status") != "queued":
            return False
        ok = db.peer_mailbox_cancel(message_id, reason="cancelled by user")
        if ok:
            emit_settled(message_id, STATUS_FAILED, int(row.get("attempts") or 0), str(row.get("from_session_id") or ""))
        return ok


__all__ = [
    "STATUS_DELIVERED_LIVE", "STATUS_FAILED", "STATUS_QUEUED", "STATUS_RESUMED", "after_session_resume",
    "cancel_message", "drain_pending", "drain_session", "emit_settled", "list_messages", "policy",
    "recover_stale_claims", "resume_interrupted_sessions", "retry_message", "revive_resident_sessions",
    "schedule_drain", "schedule_startup_delivery", "send_message", "send_rpc", "session_is_resident",
]
