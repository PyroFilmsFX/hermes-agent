"""Session continuity for the claude-agent-sdk runtime: the persisted SDK resume
id and the digest that primes a fresh SDK session from the Hermes transcript.
Extracted from ``claude_sdk_runtime.py``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from agent.claude_sdk_runtime_state import _SdkTurnState

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.claude_sdk_runtime")


_SDK_RESUME_BINDING_PREFIX = "hermes-sdk-resume-v1:"


def _canonical_sdk_cwd(value: Optional[str] = None) -> str:
    """Return the canonical CWD identity used to bind SDK resume IDs."""
    if value is None:
        from agent.runtime_cwd import resolve_agent_cwd

        value = str(resolve_agent_cwd())
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(value)))))


def _encode_sdk_resume_binding(session_id: str, *, cwd: Optional[str] = None) -> str:
    payload = {
        "cwd": _canonical_sdk_cwd(cwd),
        "id": str(session_id),
    }
    return _SDK_RESUME_BINDING_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def _persisted_sdk_session_id(agent) -> Optional[str]:
    """The SDK session id stored on the Hermes session row (or None)."""
    # A rotation (see rotate_claude_sdk_session) stashes the live id in memory
    # so the resume survives even for agents with no session row (bare
    # AIAgent, forks). Consumed once.
    stashed = getattr(agent, "_claude_sdk_rotated_resume_id", None)
    if isinstance(stashed, str) and stashed:
        agent._claude_sdk_rotated_resume_id = None
        return stashed
    if getattr(agent, "_persist_disabled", False):
        return None
    if not (getattr(agent, "_session_db", None) and getattr(agent, "session_id", None)):
        return None
    try:
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
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                _store_sdk_session_id(agent, None)
                return None
        else:
            # Legacy raw IDs and unknown envelope versions have no trustworthy
            # creation-workspace provenance.  The session row's CWD is mutable,
            # so it cannot retroactively authorize a cross-workspace resume.
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


def _sdk_session_name(agent) -> str:
    """Peer-addressable name for this agent's Claude Code session.

    Hermes sessions are invisible to the ListAgents/SendMessage pair unless
    they carry a name — the CLI otherwise derives one from cwd, so every
    Hermes session on a box collides. Reads the operator template and fills it
    from the live session row. Never raises: a naming failure must not cost a
    turn."""
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
    must not fail a rename."""
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
    is live. Returns True when a session was rotated."""
    live = getattr(agent, "_claude_sdk_session", None)
    if live is None:
        return False
    if getattr(live, "_turn_inbox", None) is not None:
        # A turn owns the stream; closing the CLI now would kill it mid-flight (a between-turns
        # MCP refresh can fire from the late-binding thread while a turn runs). Defer to the
        # next turn boundary, where run_claude_agent_sdk_turn honours the pending flag.
        agent._claude_sdk_rename_pending = True
        logger.info("claude-agent-sdk rotation (%s) deferred: a turn is in flight", reason)
        return False
    sid = getattr(live, "_session_id", None) or getattr(live, "_resume_session_id", None)
    if isinstance(sid, str) and sid:
        agent._claude_sdk_rotated_resume_id = sid
    try:
        live.close()
    except Exception:
        logger.debug("SDK session close during rotation raised", exc_info=True)
    agent._claude_sdk_session = None
    logger.info("claude-agent-sdk session rotated (%s); next turn resumes in a fresh CLI", reason)
    return True


def _store_sdk_session_id(
    agent, value: Optional[str], *, cwd: Optional[str] = None
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
            _encode_sdk_resume_binding(value, cwd=cwd) if value is not None else None
        )
        agent._session_db.update_claude_sdk_session_id(agent.session_id, stored)
    except Exception:
        logger.debug("resume-id write failed", exc_info=True)


_CONTINUITY_DIGEST_MAX_CHARS = 4000


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
        if not (
            isinstance(m, dict)
            and m.get("display_kind") == "sdk_background_result"
        )
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
