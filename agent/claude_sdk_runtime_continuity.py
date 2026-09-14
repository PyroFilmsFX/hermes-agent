"""Session continuity for the claude-agent-sdk runtime: the persisted SDK resume
id and the digest that primes a fresh SDK session from the Hermes transcript.
Extracted from ``claude_sdk_runtime.py``.
"""

from __future__ import annotations

from contextlib import nullcontext
import json
import hashlib
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.claude_sdk_runtime_state import _SdkTurnState

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.claude_sdk_runtime")


_SDK_RESUME_BINDING_PREFIX = "hermes-sdk-resume-v1:"
_SDK_TASK_LIST_BY_SESSION: dict[str, str] = {}


def _canonical_sdk_cwd(value: Optional[str] = None) -> str:
    """Return the canonical CWD identity used to bind SDK resume IDs."""
    if value is None:
        from agent.runtime_cwd import resolve_agent_cwd

        value = str(resolve_agent_cwd())
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(value)))))


def _task_list_profile_namespace() -> str:
    """Use the resolved Hermes store as part of the task-list identity namespace."""
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home().expanduser().resolve())
    except Exception:
        return str(Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")).resolve())


def _derive_sdk_task_list_id(hermes_session_id: Optional[str]) -> Optional[str]:
    """Derive a bounded, filesystem-safe and profile-scoped Claude task-list id."""
    raw = str(hermes_session_id or "").strip()
    if not raw:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._") or "session"
    digest = hashlib.sha256(
        f"{raw}\0{_task_list_profile_namespace()}".encode("utf-8")
    ).hexdigest()[:16]
    prefix_limit = 64 - len("hermes-") - len(digest) - 1
    return f"hermes-{safe[:prefix_limit]}-{digest}"


def _task_tools_enabled() -> bool:
    try:
        from agent.transports.claude_agent_sdk_session_config import _provider_flag

        return _provider_flag("task_tools")
    except Exception:
        return False


def _encode_sdk_resume_binding(
    session_id: str,
    *,
    cwd: Optional[str] = None,
    task_list: Optional[str] = None,
) -> str:
    task_list = task_list or _SDK_TASK_LIST_BY_SESSION.get(str(session_id))
    payload = {
        "cwd": _canonical_sdk_cwd(cwd),
        "id": str(session_id),
    }
    if task_list:
        task_list = str(task_list)
        payload["task_list"] = task_list
        _SDK_TASK_LIST_BY_SESSION[str(session_id)] = task_list
    return _SDK_RESUME_BINDING_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def _sdk_task_list_id(agent) -> Optional[str]:
    """Return or derive the task-list id without changing the Claude task store."""
    if (
        getattr(agent, "_persist_disabled", False)
        and getattr(agent, "_session_db", None) is None
        and not isinstance(
            getattr(agent, "__dict__", {}).get("_claude_sdk_rotated_resume_id"), str
        )
    ):
        fork_id = getattr(agent, "_claude_sdk_fork_task_list_id", None)
        if not isinstance(fork_id, str) or not fork_id:
            session_id = str(getattr(agent, "session_id", "") or "")
            fork_id = _derive_sdk_task_list_id(f"{session_id}-fork-{uuid.uuid4().hex}")
            agent._claude_sdk_fork_task_list_id = fork_id
        agent._claude_sdk_task_list_id = fork_id
        return fork_id
    current = getattr(agent, "_claude_sdk_task_list_id", None)
    if isinstance(current, str) and current:
        return current
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is not None and session_id:
        try:
            raw = (db.get_session(session_id) or {}).get("claude_sdk_session_id")
            if isinstance(raw, str) and raw.startswith(_SDK_RESUME_BINDING_PREFIX):
                payload = json.loads(raw[len(_SDK_RESUME_BINDING_PREFIX) :])
                task_list = payload.get("task_list")
                if isinstance(task_list, str) and task_list:
                    agent._claude_sdk_task_list_id = task_list
                    _SDK_TASK_LIST_BY_SESSION[str(payload.get("id") or "")] = agent._claude_sdk_task_list_id
                    return agent._claude_sdk_task_list_id
        except Exception:
            logger.debug("task-list id read failed", exc_info=True)
    derived = _derive_sdk_task_list_id(session_id)
    if derived:
        agent._claude_sdk_task_list_id = derived
    return derived


def _persisted_sdk_session_id(agent) -> Optional[str]:
    """The SDK session id stored on the Hermes session row (or None)."""
    # A rotation (see rotate_claude_sdk_session) stashes the live id in memory
    # so the resume survives even for agents with no session row (bare
    # AIAgent, forks). Consumed once. (cntrl carry)
    stashed = getattr(agent, "_claude_sdk_rotated_resume_id", None)
    if isinstance(stashed, str) and stashed:
        agent._claude_sdk_rotated_resume_id = None
        raw = stashed
        from_stash = True
    else:
        from_stash = False
        if getattr(agent, "_persist_disabled", False):
            return None
        if not (
            getattr(agent, "_session_db", None) and getattr(agent, "session_id", None)
        ):
            return None
        raw = None
    try:
        if raw is None:
            row = agent._session_db.get_session(agent.session_id) or {}
            raw = row.get("claude_sdk_session_id") or None
        if not isinstance(raw, str) or not raw:
            return None
        current_cwd = _canonical_sdk_cwd()
        if raw.startswith(_SDK_RESUME_BINDING_PREFIX):
            try:
                payload = json.loads(raw[len(_SDK_RESUME_BINDING_PREFIX) :])
                session_id = payload["id"]
                bound_cwd = payload["cwd"]
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("empty SDK session id")
                if not isinstance(bound_cwd, str) or not bound_cwd:
                    raise ValueError("empty SDK resume cwd")
                task_list = payload.get("task_list")
                if not isinstance(task_list, str) or not task_list:
                    task_list = _derive_sdk_task_list_id(getattr(agent, "session_id", None))
                    if task_list and _task_tools_enabled():
                        _store_sdk_session_id(
                            agent, session_id, cwd=bound_cwd, task_list=task_list
                        )
                if task_list:
                    agent._claude_sdk_task_list_id = task_list
                    _SDK_TASK_LIST_BY_SESSION[session_id] = agent._claude_sdk_task_list_id
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                if not from_stash:
                    _store_sdk_session_id(agent, None)
                return None
        else:
            # Legacy raw IDs and unknown envelope versions have no trustworthy
            # creation-workspace provenance.  The session row's CWD is mutable,
            # so it cannot retroactively authorize a cross-workspace resume.
            if not from_stash:
                _store_sdk_session_id(agent, None)
            return None
        if _canonical_sdk_cwd(bound_cwd) != current_cwd:
            logger.info(
                "claude-agent-sdk: declining resume id bound to a different cwd"
            )
            return None
        return session_id
    except Exception:
        logger.debug("resume-id read failed", exc_info=True)
        return None


_SESSION_LOCK_INIT = threading.Lock()


def _claude_sdk_session_lock(agent):
    """Return the agent-level session publication lock, creating it atomically.

    Two first-time callers must never install different locks (a private lock
    on one side would let publish and identity-clear race), so the lazy
    creation is double-checked under a module-level init lock.
    """
    lock = getattr(agent, "_claude_sdk_session_lock", None)
    if lock is None:
        with _SESSION_LOCK_INIT:
            lock = getattr(agent, "_claude_sdk_session_lock", None)
            if lock is None:
                lock = threading.RLock()
                agent._claude_sdk_session_lock = lock
    return lock


def _publish_claude_sdk_session(agent, session: Any) -> None:
    """Publish a newly built session under the agent's identity lock."""
    with _claude_sdk_session_lock(agent):
        agent._claude_sdk_session = session


def _clear_claude_sdk_session_if_current(agent, expected: Any) -> bool:
    """Detach an SDK session only if the agent still points at that object."""
    with _claude_sdk_session_lock(agent):
        if getattr(agent, "_claude_sdk_session", None) is not expected:
            return False
        agent._claude_sdk_session = None
        return True


def _sdk_session_name(agent) -> str:
    """Peer-addressable name for this agent's Claude Code session.

    Hermes sessions are invisible to the ListAgents/SendMessage pair unless
    they carry a name — the CLI otherwise derives one from cwd, so every
    Hermes session on a box collides. Reads the operator template and fills it
    from the live session row. Never raises: a naming failure must not cost a
    turn. (cntrl carry)"""
    try:
        from agent.transports.claude_agent_sdk_session_config import (
            _configured_session_name_template,
            render_sdk_session_name,
        )

        session_id = str(getattr(agent, "session_id", "") or "")
        title = ""
        db = getattr(agent, "_session_db", None)
        if db is not None and session_id:
            try:
                title = str((db.get_session(session_id) or {}).get("title") or "")
            except Exception:
                logger.debug("session title read failed for SDK naming", exc_info=True)
        profile = ""
        try:
            from hermes_cli.profiles import get_active_profile_name

            profile = str(get_active_profile_name() or "")
        except Exception:
            logger.debug("profile read failed for SDK naming", exc_info=True)
        return render_sdk_session_name(
            _configured_session_name_template(),
            title=title,
            session=session_id,
            profile=profile,
            model=str(getattr(agent, "model", "") or ""),
        )
    except Exception:
        logger.debug("SDK session naming failed", exc_info=True)
        return ""


def rename_claude_sdk_session(agent, *, busy: bool = False) -> str:
    """Apply a Hermes title change to the spawned CLI session's peer-visible name.

    Recomputes the name from the (already updated) session row via _sdk_session_name, then:
    live + idle → instant ``/rename`` (ClaudeAgentSdkSession.rename); live + busy, or the
    rename could not be issued → mark pending so the NEXT turn rotates the session, which
    rebuilds the CLI with the new ``--name`` and resumes; no live session → nothing to do,
    the next build reads the row. Returns the computed name. Never raises: a naming miss
    must not fail a rename. (cntrl carry)"""
    try:
        name = _sdk_session_name(agent)
    except Exception:
        logger.debug("SDK rename: name computation failed", exc_info=True)
        return ""
    live = getattr(agent, "_claude_sdk_session", None)
    if live is None or not name:
        return name
    applied = False
    if not busy:
        try:
            applied = bool(live.rename(name))
        except Exception:
            logger.debug("SDK rename failed; deferring to rotation", exc_info=True)
    if not applied:
        agent._claude_sdk_rename_pending = True
        logger.info("claude-agent-sdk: rename to %r deferred to the next turn (session busy)", name)
    return name


def rotate_claude_sdk_session(agent, reason: str = "tool surface changed") -> bool:
    """Close the live SDK session so the NEXT turn rebuilds the CLI with fresh
    ``mcp_servers`` / ``plugins`` / ``cli_path`` — the live-reload the SDK
    lacks (its MCP list is fixed at process start). Unlike an error retire the
    persisted resume id is KEPT, so the conversation continues in the rebuilt
    CLI; only the process is new. Safe between turns; a no-op when no session
    is live. Returns True when a session was rotated. (cntrl carry)"""
    live = getattr(agent, "_claude_sdk_session", None)
    if live is None:
        return False
    # The per-session RLock is also used by the turn's admission path. Marking the
    # session retiring under it closes the admission window; close() must happen
    # after releasing it because close waits on the SDK loop thread.
    admission_lock = getattr(live, "_turn_callback_lock", None)
    with nullcontext() if admission_lock is None else admission_lock:
        if (
            getattr(live, "_turn_inbox", None) is not None
            or getattr(live, "_turn_claim_requested", False) is True
        ):
            # A turn owns the stream as soon as admission is requested; closing the CLI now
            # would kill it mid-flight (a between-turns MCP refresh can fire from the
            # late-binding thread while a turn runs). Defer to the next turn boundary,
            # where run_claude_agent_sdk_turn honours the pending flag.
            agent._claude_sdk_rename_pending = True
            logger.info("claude-agent-sdk rotation (%s) deferred: a turn is in flight", reason)
            return False
        live._retiring = True
        sid = getattr(live, "_session_id", None) or getattr(live, "_resume_session_id", None)
        if isinstance(sid, str) and sid:
            bound_cwd = getattr(live, "_cwd", None)
            if not isinstance(bound_cwd, str):
                bound_cwd = _canonical_sdk_cwd()
            agent._claude_sdk_rotated_resume_id = _encode_sdk_resume_binding(
                sid, cwd=bound_cwd
            )
    try:
        live.close()
    except Exception:
        logger.debug("SDK session close during rotation raised", exc_info=True)
    # A concurrent turn may have retired this object and published its
    # replacement while close() was waiting on the SDK loop.  Never erase a
    # newer session from the agent slot when the old rotation completes.
    _clear_claude_sdk_session_if_current(agent, live)
    logger.info("claude-agent-sdk session rotated (%s); next turn resumes in a fresh CLI", reason)
    return True


def _store_sdk_session_id(
    agent,
    value: Optional[str],
    *,
    cwd: Optional[str] = None,
    task_list: Optional[str] = None,
) -> None:
    """Persist (or clear, with None) the SDK session id on the session row."""
    if getattr(agent, "_persist_disabled", False):
        # A review/curator fork shares the parent's session_id — it must
        # never write its own resume id onto the parent's row.
        return
    if not (getattr(agent, "_session_db", None) and getattr(agent, "session_id", None)):
        return
    try:
        if value is not None and cwd is None:
            # The workspace this id belongs to was never sampled. Encoding it
            # here would canonicalise whatever cwd happens to be live at
            # persist time, which then matches on every subsequent turn and
            # makes the check a silent no-op. Write nothing rather than a
            # guess. Deliberately not a clear: callers that mean "clear" pass
            # None explicitly, and the read path already re-validates a stored
            # binding's cwd on every turn, so an older binding for this same
            # workspace is exactly what should still be resumable.
            logger.debug("resume-id not written: turn workspace unknown")
            return
        stored = (
            _encode_sdk_resume_binding(
                value,
                cwd=cwd,
                task_list=(task_list or _sdk_task_list_id(agent))
                if _task_tools_enabled()
                else None,
            )
            if value is not None
            else None
        )
        agent._session_db.update_claude_sdk_session_id(agent.session_id, stored)
    except Exception:
        logger.debug("resume-id write failed", exc_info=True)


_CONTINUITY_DIGEST_MAX_CHARS = 4000

_SDK_DISPLAY_ONLY_KINDS = frozenset(
    {"peer_message", "session_lifecycle", "sdk_background_result"}
)


def _is_sdk_display_only_row(message: Any) -> bool:
    """Whether a durable SDK delivery row is for the UI, not model context.

    Background peer/lifecycle/tool projections share the session transcript for
    display and recovery, but replaying them into a provider prompt would
    mutate the cached prefix and can create invalid role sequences.
    """
    if not isinstance(message, dict):
        return False
    if message.get("display_kind") in _SDK_DISPLAY_ONLY_KINDS:
        return True
    metadata = message.get("display_metadata")
    return isinstance(metadata, dict) and metadata.get("source") == "sdk_background_result"


def _render_continuity_digest(prior_messages: List[Dict[str, Any]]) -> str:
    """Bounded text preamble for a FRESH SDK session that has prior Hermes
    history (resume impossible: no stored id, or the stored one went stale).
    Reuses _digest_history's compaction, then flattens to capped text."""
    # Projected background results are the agent's OWN answers, already
    # delivered outbound; re-presenting them here is the double-presentation
    # pathology the background lane exists to kill. Filter before the
    # compaction pass — _digest_history may rebuild dicts and drop the mark.
    prior_messages = [
        m for m in (prior_messages or [])
        if not _is_sdk_display_only_row(m)
    ]
    try:
        from agent.background_review import _digest_history

        msgs = _digest_history(list(prior_messages or []), tail=8)
    except Exception:  # pragma: no cover - compaction is best-effort
        msgs = list(prior_messages or [])[-8:]
    lines: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        text = str(content).replace("\n", " ").strip()
        if text:
            lines.append(f"{role.upper()}: {text[:400]}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > _CONTINUITY_DIGEST_MAX_CHARS:
        body = body[-_CONTINUITY_DIGEST_MAX_CHARS:]
    return (
        "[Continuity digest — the runtime restarted and the live model "
        "context was lost; recent turns from the stored transcript, oldest "
        "first:]\n" + body + "\n[End digest. The user's new message follows.]\n\n"
    )


def _persist_turn(agent, state: _SdkTurnState) -> None:
    """Flush the projected rows FIRST, then persist the SDK resume id."""
    turn = state.turn
    messages = state.messages
    if turn.projected_messages:
        messages.extend(turn.projected_messages)
        # Early-return path bypasses conversation_loop's per-step persistence;
        # flush the new projected rows ourselves (idempotent via the intrinsic
        # _DB_PERSISTED_MARKER — the user turn was flushed at turn start).
        if getattr(agent, "_session_db", None) is not None:
            try:
                agent._flush_messages_to_session_db(messages)
            except Exception:
                logger.debug(
                    "claude-sdk projected-message flush failed", exc_info=True
                )

    if not getattr(turn, "should_retire", False) and state.failover_reason is None:
        # Persist the SDK session id for restart/eviction/interrupt resume.
        # AFTER the flush on purpose: the flush's _ensure_db_session retry is
        # what (re)creates the session row when turn-start persistence hit a
        # transient lock — storing first would silently discard the id.
        thread_id = getattr(turn, "thread_id", None)
        if thread_id:
            _store_sdk_session_id(agent, thread_id, cwd=state.turn_session_cwd)
