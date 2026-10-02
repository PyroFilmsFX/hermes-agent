"""Session creation and per-turn wiring for the claude-agent-sdk runtime.

What used to be closures nested in ``run_claude_agent_sdk_turn`` are small
owner objects and module-level functions that take the agent explicitly; the
session receives the per-agent ones bound with ``functools.partial``. Lifetime
split kept visible: ``_make_visibility_callbacks`` refreshes every turn, while
``_create_session`` (approval callback, prompt append, budget, bridge inputs) is
session-creation work. Extracted from ``claude_sdk_runtime.py``.
"""

from __future__ import annotations

import copy
import functools
import inspect
import math
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent.redact import redact_sensitive_text
from agent.claude_sdk_runtime_compaction import _on_compact_boundary, _on_compaction
from agent.claude_sdk_runtime_fallback import (
    ClaudeSdkTurnEffects,
    _consume_agent_interrupt,
    _retire_live_sdk_session,
)
from agent.claude_sdk_runtime_continuity import (
    _continuity_digest_source,
    _claude_sdk_session_lock,
    _clear_claude_sdk_session_if_current,
    _publish_claude_sdk_session,
    _sdk_session_name,
    rotate_claude_sdk_session,
    rotate_claude_sdk_session_on_model_change,
    _canonical_sdk_cwd,
    _sdk_task_list_id,
    _task_tools_enabled,
    _persisted_sdk_session_id,
    _render_continuity_digest,
    _store_sdk_session_id,
    _persist_steer_boundary,
)
from agent.claude_sdk_runtime_prompt import build_system_prompt_append
from agent.claude_sdk_runtime_context import _coerce_usage_int
from agent.claude_sdk_runtime_state import _SdkTurnState
from agent.claude_sdk_runtime_usage import _record_claude_sdk_usage
from agent.claude_sdk_runtime_tools import (
    _hybrid_bridge_enabled,
    _snapshot_agent_tools_with_mcp_refresh,
)
from agent.transports.claude_agent_sdk_session_turn import _claim_child_exit_emission

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.claude_sdk_runtime")


def _configured_transient_retry_policy() -> tuple[int, tuple[float, ...], float]:
    """Read bounded Claude SDK transient retry settings from config.yaml."""
    from agent.transports.claude_agent_sdk_session import _provider_config

    config = _provider_config()
    retries = config.get("transient_retry_max_retries", 2)
    if isinstance(retries, bool) or not isinstance(retries, int):
        retries = 2
    retries = max(0, min(retries, 10))

    raw_backoff = config.get("transient_retry_backoff_seconds", [2, 8])
    if not isinstance(raw_backoff, (list, tuple)):
        raw_backoff = [2, 8]
    backoff = []
    for value in raw_backoff:
        if isinstance(value, bool):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds >= 0:
            backoff.append(seconds)
    if not backoff:
        backoff = [2.0, 8.0]

    cap = config.get("transient_retry_max_wait_seconds", 60)
    if isinstance(cap, bool):
        cap = 60
    try:
        cap = max(0.0, min(float(cap), 60.0))
    except (TypeError, ValueError, OverflowError):
        cap = 60.0
    return retries, tuple(backoff), cap


def _seconds_list(raw: Any, default: list) -> tuple[float, ...]:
    """A non-negative seconds list from config (bools/garbage dropped; empty -> default)."""
    if not isinstance(raw, (list, tuple)):
        raw = default
    out = []
    for value in raw:
        if isinstance(value, bool):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds >= 0 and math.isfinite(seconds):
            out.append(seconds)
    return tuple(out or (float(v) for v in default))


def _configured_continue_policy() -> tuple[bool, int, tuple[float, ...], bool]:
    """D62 "continue, don't replay" settings from config.yaml (agent.claude_agent_sdk).

    ``(resume_interrupted_turn, continue_max_per_turn, continue_backoff_seconds,
    transient_retry_replay)``: L2 on by default, at most N (default 2, clamped
    0..5) automatic continues per user turn, and the legacy prompt replay
    (#31/U8.2) opt-in only.
    """
    from agent.transports.claude_agent_sdk_session import _provider_config

    config = _provider_config()

    def _flag(key: str, default: bool) -> bool:
        value = config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return bool(value)

    count = config.get("continue_max_per_turn", 2)
    if isinstance(count, bool) or not isinstance(count, int):
        count = 2
    return (
        _flag("resume_interrupted_turn", True),
        max(0, min(count, 5)),
        _seconds_list(config.get("continue_backoff_seconds", [2, 8]), [2, 8]),
        _flag("transient_retry_replay", False),
    )


def _continue_nudge(reason: str) -> str:
    """The fallback continuation: ONE real user-role note on the same Claude session.

    Appended as a new user message, so the cached prefix and strict role
    alternation are untouched (the CLI merges it with an unanswered user tail).
    The user's prompt is never re-sent."""
    return (
        f"[System note: your previous turn was interrupted ({reason}) before it finished. "
        "Continue where you left off. Some steps may already be complete: check their "
        "results before redoing anything, and do not repeat actions that already took effect.]"
    )


def _maybe_park_usage_limit(agent, turn: Any, state: Any, remaining_wait: float) -> bool:
    """Park the session on a usage limit whose reset is beyond the in-turn window (D62)."""
    from agent import claude_sdk_usage_park as usage_park

    try:
        enabled, stagger_max = usage_park.configured_policy()
        if not enabled or not usage_park.real_rate_limit_signal(turn):
            return False
        now = time.time()
        resets_at = usage_park.reset_time(turn, now)
        if resets_at is None or resets_at - now <= remaining_wait:
            return False
        session_key = str(getattr(agent, "session_id", "") or "")
        sdk_id = getattr(turn, "thread_id", None) or _persisted_sdk_session_id(agent)
        if not session_key or not isinstance(sdk_id, str) or not sdk_id:
            return False
        rate_limit = getattr(turn, "rate_limit_rejected", None)
        reason = str((rate_limit or {}).get("rate_limit_type") or "rate_limit") if isinstance(rate_limit, dict) else "rate_limit"
        record = usage_park.park(session_key, sdk_id, resets_at, reason, stagger_max=stagger_max, now=now)
    except Exception:
        logger.warning("claude-agent-sdk: usage-limit park failed; failing as before", exc_info=True)
        return False
    _store_sdk_session_id(agent, sdk_id, cwd=state.turn_session_cwd)
    text = usage_park.paused_status_text(record["resume_at"])
    turn.usage_parked = record
    turn.error = (
        f"{text}. The Claude session is kept and continues automatically then; "
        "Stop cancels the pause."
    )
    emit = getattr(agent, "_emit_status", None)
    if callable(emit):
        try:
            emit(text)
        except Exception:
            logger.debug("failed to emit usage pause status", exc_info=True)
    logger.warning("claude-agent-sdk: %s (session %s parked)", text, session_key)
    return True


def _projects_tool_use(turn: Any) -> bool:
    """Whether the failed attempt projected a tool call in any wire shape."""
    for message in getattr(turn, "projected_messages", None) or ():
        if not isinstance(message, dict):
            continue
        if message.get("tool_calls") or message.get("tool_use"):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            return True
    return False


def _sdk_attempt_effects(
    agent, turn: Any, state: Optional[_SdkTurnState] = None
) -> ClaudeSdkTurnEffects:
    """The observable effects a failed SDK attempt left behind.

    This is the ONE replay-safety ledger for every same-provider re-send of a
    turn (the transient-API retry and the post-query CLI-death replay) and the
    same ``ClaudeSdkTurnEffects`` shape the provider hand-off uses. It is the
    union of both former predicates: a stop (turn or agent flag), a tool
    effect / tool iteration / projected tool_use, streamed or final assistant
    text, interim assistant prose a surface already accepted (the only trace
    of shown prose with streaming off), any projected row, and a transcript
    mutation all make it unsafe.
    """
    try:
        tool_iterations = int(getattr(turn, "tool_iterations", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        # An unreadable counter cannot prove that no tool ran.
        tool_iterations = 1
    return ClaudeSdkTurnEffects(
        tool=(
            bool(getattr(agent, "_sdk_issued_tool_effect", False))
            or tool_iterations > 0
            or _projects_tool_use(turn)
        ),
        streamed=(
            bool(getattr(agent, "_current_streamed_assistant_text", ""))
            or getattr(agent, "_sdk_interim_delivered", False) is True
            or bool(getattr(turn, "final_text", ""))
        ),
        projected=bool(getattr(turn, "projected_messages", None)),
        interrupted=bool(
            getattr(turn, "interrupted", False)
            or getattr(agent, "_interrupt_requested", False)
        ),
        mutated=(
            state is not None and state.messages != state.messages_before_attempt
        ),
    )


def _sdk_attempt_replay_safe(
    agent, turn: Any, state: Optional[_SdkTurnState] = None
) -> bool:
    """A failed SDK attempt may be re-sent only if it left no trace at all."""
    return _sdk_attempt_effects(agent, turn, state).replay_safe


# Anthropic-shaped usage keys that add up across the attempts of one turn.
_SUMMED_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _attempt_cost_usd(turn: Any) -> Optional[float]:
    """The SDK-reported cost of one attempt, or None when absent/invalid."""
    raw = getattr(turn, "total_cost_usd", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cost = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return cost if cost >= 0 and math.isfinite(cost) else None


def _spent_cost_usd(attempts: list) -> float:
    return sum(_attempt_cost_usd(t) or 0.0 for t in attempts)


def _resend_exceeds_budget(spent_attempts: list, turn: Any) -> bool:
    """Whether re-sending ``turn`` would take the TURN past max_budget_usd.

    The CLI enforces the cap per query, and a retry rebuilds the session, so
    the cap would silently reset per attempt. A re-send is predicted to cost
    what the failed attempt cost; skip it when that would cross the cap.
    """
    cap = _configured_max_budget_usd()
    if cap is None:
        return False
    spent = _spent_cost_usd([*spent_attempts, turn])
    return spent + (_attempt_cost_usd(turn) or 0.0) > cap


def _fold_attempt_spend(prior: list, turn: Any) -> None:
    """Fold the usage and cost of re-sent attempts into ``turn``.

    Post-turn accounting records one TurnResult, so without this a retried
    turn would bill only its last attempt. Token counts and cost are summed;
    context pressure (``iterations``) stays the final attempt's own.
    """
    if not prior or turn is None:
        return
    attempts = [*prior, turn]
    usages = [
        u for u in (getattr(t, "token_usage_last", None) for t in attempts)
        if isinstance(u, dict) and u
    ]
    if usages:
        final = getattr(turn, "token_usage_last", None)
        merged = dict(final) if isinstance(final, dict) else {}
        for key in _SUMMED_USAGE_KEYS:
            if any(key in u for u in usages):
                merged[key] = sum(_coerce_usage_int(u.get(key)) for u in usages)
        if "iterations" not in merged and isinstance(final, dict) and final:
            # Keep the context-pressure read on the final attempt instead of
            # letting it fall back to the (now summed) aggregate.
            merged["iterations"] = [{
                key: final[key] for key in _SUMMED_USAGE_KEYS if key in final
            }]
        turn.token_usage_last = merged
        turn.token_usage_total = dict(merged)
    costs = [c for c in (_attempt_cost_usd(t) for t in attempts) if c is not None]
    if costs:
        turn.total_cost_usd = sum(costs)
    if any(getattr(t, "api_call_made", True) for t in prior):
        turn.api_call_made = True


def _record_spent_attempts(agent, spent_attempts: list) -> None:
    """Account re-sent attempts when the turn ends without a TurnResult."""
    if not spent_attempts:
        return
    try:
        carrier = copy.copy(spent_attempts[-1])
        _fold_attempt_spend(spent_attempts[:-1], carrier)
        if getattr(carrier, "api_call_made", True):
            _record_claude_sdk_usage(agent, carrier)
    except Exception:
        logger.debug("claude-sdk re-sent attempt accounting failed", exc_info=True)


class _TurnVisibility:
    """Visibility callbacks fenced to one exact Hermes turn.

    Created once per turn. The epoch / turn-id pair it records on the agent lets
    a late callback from a superseded turn (or a replaced session) recognise that
    it is stale and drop itself; the iteration counter and the stream-sink flag
    start fresh with it.
    """

    def __init__(self, agent) -> None:
        self._agent = agent
        self._turn_id = str(getattr(agent, "_current_turn_id", "") or "")
        lock = getattr(agent, "_sdk_visibility_lock", None)
        if lock is None:
            lock = threading.RLock()
            agent._sdk_visibility_lock = lock
        self._lock = lock
        with lock:
            agent._sdk_visibility_epoch = getattr(agent, "_sdk_visibility_epoch", 0) + 1
            self._epoch = agent._sdk_visibility_epoch
            agent._sdk_visibility_turn_id = self._turn_id
            agent._sdk_visibility_iteration_count = 0
            agent._sdk_stream_sink_accepted = False

    def is_current(self) -> bool:
        agent = self._agent
        with self._lock:
            return (
                getattr(agent, "_sdk_visibility_epoch", None) == self._epoch
                and getattr(agent, "_sdk_visibility_turn_id", None) == self._turn_id
                and getattr(agent, "_current_turn_id", None) == self._turn_id
                and not getattr(agent, "_interrupt_requested", False)
            )

    def on_tool_iteration(self) -> None:
        agent = self._agent
        with self._lock:
            if not self.is_current():
                return
            agent._sdk_visibility_iteration_count += 1
        try:
            agent._touch_activity("completed SDK tool iteration")
        except Exception:
            logger.debug("claude-sdk iteration activity update failed", exc_info=True)

    def relay_interim_assistant(self, text: str) -> None:
        agent = self._agent
        if not self.is_current() or not isinstance(text, str):
            return
        visible = agent._strip_think_blocks(text).strip()
        if visible:
            from agent.redact import redact_sensitive_text
            visible = redact_sensitive_text(visible)
        if not visible or visible == "(empty)" or agent._interim_text_was_delivered(visible):
            return
        callback = getattr(agent, "interim_assistant_callback", None)
        if callback is None:
            return
        # Mirror the native lane (run_agent.py::_emit_interim_assistant_
        # message): compute the flag instead of hardcoding it. With
        # `agent.claude_agent_sdk.streaming` on, this prose has already
        # been painted by the delta sink, and a False here makes the
        # surface re-render it as fresh commentary on top of the
        # streaming buffer instead of sealing the segment — the text
        # visibly appears, is dropped, then reappears.
        # Two signals, not one: the accumulator says the text was
        # released, the flag says a surface accepted it. Sealing a segment
        # nobody painted would drop the prose from the UI entirely.
        already_streamed = bool(
            getattr(agent, "_sdk_stream_sink_accepted", False)
        ) and agent._interim_content_was_streamed(visible)
        try:
            callback(visible, already_streamed=already_streamed)
            # Shown prose is a replay guard even with streaming off, where
            # no delta text ever reaches _current_streamed_assistant_text.
            agent._sdk_interim_delivered = True
            agent._record_delivered_interim_text(visible)
        except Exception:
            logger.debug("interim assistant relay raised", exc_info=True)


def _make_visibility_callbacks(agent):
    """Create visibility callbacks fenced to this exact Hermes turn."""
    visibility = _TurnVisibility(agent)
    return visibility.relay_interim_assistant, visibility.on_tool_iteration


def _approval_bypass_active(agent) -> bool:
    """Resolve live trusted bypass posture for the foreign SDK thread."""
    try:
        from tools.approval import is_approval_bypass_active_for_session

        ctx = getattr(agent, "_sdk_approval_turn_ctx", None)
        session_key = (
            ctx.get("session_key", "") if type(ctx) is dict else ""
        )
        return is_approval_bypass_active_for_session(session_key)
    except Exception:
        return False


def _on_tool_started(agent, tool_name: str, preview: str, args: dict) -> None:
    if tool_name == "reasoning.available":
        progress_callback = getattr(agent, "tool_progress_callback", None)
        if progress_callback is None:
            return
        try:
            progress_callback("reasoning.available", "_thinking", preview, None)
        except Exception:
            logger.debug("claude-sdk reasoning callback raised", exc_info=True)
        return
    # Claude SDK tool calls bypass the native tool executor, so mirror
    # its shared activity updates here. The gateway heartbeat reads
    # get_activity_summary(), which derives its useful current action
    # from these fields; without this, an active SDK turn remains
    # stuck at its initial "initializing" state.
    agent._sdk_issued_tool_effect = True
    agent._current_tool = tool_name
    try:
        agent._touch_activity(f"executing tool: {tool_name}")
    except Exception:
        logger.debug("claude-sdk activity update failed", exc_info=True)
    progress_callback = getattr(agent, "tool_progress_callback", None)
    if progress_callback is None:
        return
    try:
        progress_callback("tool.started", tool_name, preview, args)
    except Exception:
        logger.debug(
            "claude-sdk tool-progress callback raised", exc_info=True
        )


def _on_tool_use(agent, tool_use_id: str, tool_name: str, args: dict) -> None:
    # Stable-id tool CARD (desktop/TUI tool rows). The progress breadcrumb
    # (_on_tool_started) is dropped by the gateway whenever a name is present
    # (tool_progress._on_tool_progress), so this is the only path that puts
    # "Running Bash: …" on screen for this lane — the same pair the codex
    # bridge fires (make_codex_app_server_event_bridge). (cntrl carry)
    callback = getattr(agent, "tool_start_callback", None)
    if callback is None:
        return
    try:
        callback(tool_use_id, tool_name, args)
    except Exception:
        logger.debug("claude-sdk tool_start_callback raised", exc_info=True)


def _on_tool_result(
    agent,
    tool_use_id: str,
    tool_name: str,
    args: dict,
    result: str,
    *,
    is_error: bool = False,
    error: str | None = None,
    tool_use_result: dict | None = None,
    truncated: dict | None = None,
) -> None:
    callback = getattr(agent, "tool_complete_callback", None)
    if callback is None:
        return
    class _SdkToolResult(str):
        def __new__(cls, value: str):
            wrapped = str.__new__(cls, value)
            wrapped._sdk_tool_result = True
            wrapped._sdk_is_error = is_error
            wrapped._sdk_error = error
            wrapped._sdk_tool_use_result = tool_use_result
            wrapped._sdk_truncated = truncated
            return wrapped

    wrapped_result = _SdkToolResult(result)
    callback_kwargs = {
        "is_error": is_error,
        "error": error,
        "tool_use_result": tool_use_result,
        "truncated": truncated,
    }
    try:
        import inspect

        parameters = inspect.signature(callback).parameters
        accepts_keywords = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ) or all(key in parameters for key in callback_kwargs)
        if accepts_keywords:
            callback(tool_use_id, tool_name, args, wrapped_result, **callback_kwargs)
        else:
            callback(tool_use_id, tool_name, args, wrapped_result)
    except Exception:
        logger.debug("claude-sdk tool_complete_callback raised", exc_info=True)


def _on_sdk_subagent_event(agent, event_type: str, tool_name: str = "", preview: str = "",
                           args: Optional[dict] = None, **kwargs) -> None:
    """Bridge SDK Task/child events into the shared registry and feed."""
    from tools.delegate_tool_registry import update_sdk_subagent

    session = getattr(agent, "_claude_sdk_session", None)
    live_session_id = str(getattr(agent, "_tui_gateway_runtime_sid", None) or "")
    if not live_session_id:
        try:
            from gateway.session_context import get_session_env
            live_session_id = str(get_session_env("HERMES_UI_SESSION_ID", "") or "")
        except Exception:
            live_session_id = ""
    if live_session_id:
        agent._tui_gateway_runtime_sid = live_session_id
    owner_agent_session_id = getattr(agent, "session_id", None)
    update_sdk_subagent(
        event_type,
        task_id=str(kwargs.get("subagent_id") or ""),
        goal=str(kwargs.get("goal") or ""),
        sdk_session=session,
        owner_session_id=live_session_id or owner_agent_session_id,
        owner_agent_session_id=owner_agent_session_id,
        owner_agent=agent,
        parent_tool_id=kwargs.get("parent_tool_id"),
        child_session_id=kwargs.get("child_session_id"),
        subagent_meta=kwargs.get("subagent_meta"),
        tool_name=tool_name,
        text=preview,
        status=str(kwargs.get("status") or "running"),
    )
    callback = getattr(agent, "tool_progress_callback", None)
    if callback is None:
        return
    try:
        callback(event_type, tool_name, preview, args, **kwargs)
    except Exception:
        logger.debug("claude-sdk subagent-progress callback raised", exc_info=True)


def _relay_stream_delta(agent, text: str) -> None:
    # Late-bound: the gateway assigns stream_delta_callback per turn
    # AFTER the session exists (and clears it between turns).
    # Fan out to BOTH display sinks, mirroring the native runtimes
    # (run_agent.py: [self.stream_delta_callback, self._stream_callback]).
    # `stream_delta_callback` is the CLI/TUI sink. `_stream_callback` is
    # the one the JSON-RPC gateway installs via run_conversation's
    # `stream_callback=` kwarg, and that is the sink the DESKTOP listens
    # on (it feeds the `message.delta` notification). Relaying only to
    # the first meant the desktop never streamed on this runtime, no
    # matter how the operator set display.streaming.
    callbacks = [
        cb
        for cb in (
            getattr(agent, "stream_delta_callback", None),
            getattr(agent, "_stream_callback", None),
        )
        if cb is not None
    ]
    if not callbacks:
        return
    # Record BEFORE the sinks run, deliberately: a sink that raises
    # *after* handing text to the user must still count as streamed, or
    # the turn fails over and replays output the user already saw
    # (pinned by test_stream_relay_records_delivery_before_display_
    # callback). The accumulator answers "was this released?", not
    # "did a surface paint it?".
    agent._record_streamed_assistant_text(text)
    for cb in callbacks:
        try:
            cb(text)
        except Exception:
            logger.debug("stream delta relay raised", exc_info=True)
        else:
            # Separate signal for the interim relay: sealing a segment
            # is only safe once a sink actually accepted a delta.
            agent._sdk_stream_sink_accepted = True


@dataclass
class _BackgroundResultDelivery:
    """Enqueue a finished background answer burst for DIRECT platform delivery.

    The completion is the AGENT'S OWN finished answer — it must go straight to
    the platform outbound lane, never back into the model as a synthetic
    delegation (2026-08-06 self-echo: the model recognized its own text, refused
    to "relay" it, and the report never left the box). The watcher delivers each
    payload as its own outbound message, in order.

    The creation-time snapshots survive as FALLBACKS only — the SDK session
    outlives hermes session rotations, so anything read at creation can be stale
    by the time a background completion fires. Parent/route are resolved AT
    DELIVERY TIME: a completion firing after a hermes session rotation must carry
    the LIVE session id, not the creation-time snapshot — the gateway classifies a
    rotated-away parent as permanently gone and drops the delivery.
    """

    agent: Any
    session_key: str
    parent_session_id: Any
    model: Any

    def __call__(
        self,
        texts: list[str],
        items: Optional[list[dict]] = None,
        delivery_id: Optional[str] = None,
    ) -> None:
        agent = self.agent
        try:
            from tools.approval_context import (
                get_current_session_key as _live_key_fn,
            )

            _live_key = _live_key_fn() or ""
        except Exception:
            _live_key = ""
        # This callback fires on the SDK loop thread, where the
        # get_current_session_key contextvar may be unset — an empty
        # live read falls back to the creation-time snapshot rather
        # than losing the route.
        session_key = _live_key or self.session_key
        parent_session_id = (
            getattr(agent, "session_id", None) or self.parent_session_id
        )
        model = getattr(agent, "model", None) or self.model
        try:
            import time as _time

            from tools.process_registry import process_registry

            now = _time.time()
            event = {
                "type": "sdk_background_result",
                "payloads": list(texts),
                "session_key": session_key,
                "parent_session_id": parent_session_id,
                "model": model,
                "dispatched_at": now,
                "completed_at": now,
            }
            if items is not None:
                event["items"] = list(items)
            if delivery_id is not None:
                event["delivery_id"] = delivery_id
            process_registry.completion_queue.put(event)
        except Exception:
            logger.warning(
                "claude-sdk background-result enqueue failed — "
                "answer may be lost", exc_info=True,
            )

    def lifecycle(self, event: str, **metadata: Any) -> None:
        """Enqueue one lifecycle item without changing the payload burst."""
        self([], [{"kind": "lifecycle", "event": event, **metadata}])


def _build_approval_callback(agent):
    """The per-session approval callback: the CLI's thread-local one, else the gateway bridge."""
    try:
        from tools.terminal_tool import _get_approval_callback
        approval_callback = _get_approval_callback()
    except Exception:
        approval_callback = None
    if approval_callback is None:
        # Gateway turns have no thread-local CLI callback — without this
        # bridge the SDK denies every un-allowlisted tool silently, no
        # prompt reaching the user, even though the gateway registers a
        # notify channel around every turn (production finding on a 24/7
        # telegram deployment). The builder returns None for surfaces
        # that are not gateway-shaped, so CLI posture is unchanged; the
        # context_provider hands it the per-turn snapshot refreshed by
        # run_claude_agent_sdk_turn, so cron-ness and the session key are
        # resolved per CALL (a cron-born session must not be frozen into
        # forever-deny).
        try:
            from tools.approval_sdk_gateway import build_sdk_gateway_approval_callback
            approval_callback = build_sdk_gateway_approval_callback(
                context_provider=lambda: (
                    getattr(agent, "_sdk_approval_turn_ctx", None) or {}
                ),
            )
        except Exception:
            approval_callback = None
    return approval_callback


def _background_result_sink(agent) -> Optional[_BackgroundResultDelivery]:
    """Delivery half of the stream-ownership fix, config-gated (default OFF).

    When the CLI finishes a background Agent task between turns, the session
    captures the answer burst and this callback enqueues it as an
    "sdk_background_result" completion event; the gateway watcher sends it
    DIRECTLY on the platform outbound lane (completion_queue →
    _async_delegation_watcher → adapter send). In-memory at-least-once, same as
    the watcher's requeue semantics. Default OFF per the block's
    upstream-conservative contract (every default falsy — pinned by
    test_canonical_defaults); gateway-bot deployments opt in.
    """
    from agent.transports.claude_agent_sdk_session import _provider_flag

    if not _provider_flag("deliver_background_results", default=False):
        return None
    # Creation-time snapshots survive as FALLBACKS only — the SDK
    # session outlives hermes session rotations, so anything read
    # here can be stale by the time a background completion fires.
    try:
        from tools.approval_context import get_current_session_key

        _bg_session_key = get_current_session_key() or ""
    except Exception:
        _bg_session_key = ""
    return _BackgroundResultDelivery(
        agent=agent,
        session_key=_bg_session_key,
        parent_session_id=getattr(agent, "session_id", None),
        model=getattr(agent, "model", None),
    )


def _gateway_unsolicited_start_sink(agent, prompt: str) -> None:
    """Mark a CLI-injected peer/task turn while it is still running, for restart recovery."""
    runtime_sid = str(getattr(agent, "_tui_gateway_runtime_sid", "") or "")
    if not runtime_sid or not prompt.strip():
        return
    try:
        from tui_gateway import server
        with server._sessions_lock:
            session = server._sessions.get(runtime_sid)
        if not isinstance(session, dict) or session.get("_finalized"):
            return
        session_key = str(session.get("session_key") or "")
        if session_key:
            server._record_turn_marker(session, prompt)
    except Exception:
        logger.debug("could not persist CLI-injected turn marker", exc_info=True)


def _gateway_unsolicited_header_sink(agent, delivery_id: str, header_items: list[dict]) -> None:
    """Relay woken/peer header to desktop before stream deltas begin."""
    runtime_sid = str(getattr(agent, "_tui_gateway_runtime_sid", "") or "")
    if not runtime_sid:
        return
    try:
        from tui_gateway import server
        with server._sessions_lock:
            session = server._sessions.get(runtime_sid)
        if not isinstance(session, dict) or session.get("_finalized"):
            return
        if hasattr(server, "_notif_deliver_sdk_header"):
            server._notif_deliver_sdk_header(runtime_sid, session, header_items, delivery_id)
    except Exception:
        logger.debug("could not deliver unsolicited SDK header", exc_info=True)


def _configured_max_budget_usd() -> Optional[float]:
    """agent.claude_agent_sdk.max_budget_usd from config.yaml.

    Forwarded to the SDK's ``max_budget_usd`` option: the query stops with an
    ``error_max_budget_usd`` result once exceeded (which run_turn already
    surfaces as "SDK turn ended: error_max_budget_usd"). None/absent — the
    canonical default — means no budget, i.e. current behavior. Non-numeric
    or non-positive values are ignored with a warning rather than passed
    through: a 0 cap would fail every turn instantly, and a typo must never
    become a silent behavior change."""
    from agent.transports.claude_agent_sdk_session import _provider_config

    raw = _provider_config().get("max_budget_usd")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        # YAML `true` would float() to 1.0 — a nonsense budget, reject it.
        logger.warning(
            "agent.claude_agent_sdk.max_budget_usd=%r is not a number — "
            "ignoring (no budget cap).", raw,
        )
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "agent.claude_agent_sdk.max_budget_usd=%r is not a number — "
            "ignoring (no budget cap).", raw,
        )
        return None
    if value <= 0:
        logger.warning(
            "agent.claude_agent_sdk.max_budget_usd=%r must be positive — "
            "ignoring (no budget cap).", raw,
        )
        return None
    return value


def _resolve_sdk_task_store_root(child_cwd: str, child_env: dict[str, str]) -> "Path":
    """Resolve the Claude task root exactly as the child CLI sees HOME/config-dir/cwd."""
    from pathlib import Path

    cwd = Path(child_cwd).expanduser().resolve()
    home = str(child_env.get("HOME") or Path.home())
    home_path = cwd if home == "~" else cwd / home[2:] if home.startswith("~/") else Path(home)
    if not home_path.is_absolute():
        home_path = cwd / home_path
    config_dir = str(child_env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config_dir:
        if config_dir == "~":
            config_dir = str(home_path)
        elif config_dir.startswith("~/"):
            config_dir = str(home_path / config_dir[2:])
        config_path = Path(config_dir)
        if not config_path.is_absolute():
            config_path = cwd / config_path
        return config_path.resolve()
    return (home_path / ".claude").resolve()


def _create_session(
    agent,
    *,
    resume_id: Optional[str],
    on_interim_assistant,
    on_tool_iteration,
    resume_interrupted_turn: bool = False,
) -> Any:
    """Build the SDK session for this agent (session-creation work, not per turn)."""
    from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    cwd = str(resolve_agent_cwd())
    context_cwd = resolve_context_cwd()
    approval_callback = _build_approval_callback(agent)

    # The appended system prompt is part of the cached prefix. Build it once
    # per logical conversation and reuse it when the transport is rebuilt
    # (rotation, rename, retire/retry): re-reading memory, project context and
    # the date there would silently break the cache. Only a deliberate input
    # change (model switch, working directory, project-context setting) —
    # each of which starts a new cache anyway — recomposes it.
    append_inputs = (
        getattr(agent, "model", None),
        str(context_cwd) if context_cwd is not None else None,
        not bool(getattr(agent, "skip_context_files", False)),
    )
    frozen_append = getattr(agent, "_claude_sdk_frozen_append", None)
    if (
        isinstance(frozen_append, tuple)
        and len(frozen_append) == 2
        and frozen_append[0] == append_inputs
        and isinstance(frozen_append[1], str)
    ):
        append = frozen_append[1]
    else:
        append = build_system_prompt_append(
            platform=getattr(agent, "platform", None),
            session_id=getattr(agent, "session_id", None),
            model=append_inputs[0],
            cwd=append_inputs[1],
            include_project_context=append_inputs[2],
        )
        agent._claude_sdk_frozen_append = (append_inputs, append)

    on_unsolicited_result = _background_result_sink(agent)
    task_list_id = _sdk_task_list_id(agent) if _task_tools_enabled() else None
    from agent.transports.claude_agent_sdk_session_config import (
        _configured_sdk_env,
        _effective_sdk_task_env,
        _sdk_env_overrides,
    )

    configured_sdk_env = _configured_sdk_env()
    # Same rule for the child's task environment: resolve once per
    # conversation so a config edit cannot change a resumed child's env.
    frozen_task_env = getattr(agent, "_claude_sdk_frozen_task_env", None)
    if isinstance(frozen_task_env, dict):
        task_env = dict(frozen_task_env)
    else:
        task_env = _effective_sdk_task_env(task_list_id=task_list_id)
        agent._claude_sdk_frozen_task_env = dict(task_env)
    task_enabled = str(task_env.get("CLAUDE_CODE_ENABLE_TODO_TOOLS", "")).strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }
    if task_enabled:
        task_list_id = task_env.get("CLAUDE_CODE_TASK_LIST_ID") or None
    else:
        task_list_id = None
    task_config_dir = configured_sdk_env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    effective_child_env = dict(os.environ)  # control-plane-env: models the SDK child env to resolve its task root; not spawned
    effective_child_env.update(
        _sdk_env_overrides(task_list_id=task_list_id, task_env=task_env, sdk_cwd=cwd)
    )
    task_store_root = _resolve_sdk_task_store_root(cwd, effective_child_env)
    try:
        from gateway.session_context import get_session_env
        runtime_sid = str(get_session_env("HERMES_UI_SESSION_ID", "") or "")
    except Exception:
        runtime_sid = ""
    if runtime_sid:
        agent._tui_gateway_runtime_sid = runtime_sid
    agent._claude_sdk_task_list_id = task_list_id
    agent._claude_sdk_task_config_dir = task_config_dir
    agent._claude_sdk_task_store_root = str(task_store_root)

    session = ClaudeAgentSdkSession(
        **({"resume_interrupted_turn": True} if resume_interrupted_turn else {}),
        cwd=cwd,
        model=getattr(agent, "model", None) or None,
        approval_callback=approval_callback,
        approval_bypass_provider=functools.partial(_approval_bypass_active, agent),
        on_tool_started=functools.partial(_on_tool_started, agent),
        on_tool_use=functools.partial(_on_tool_use, agent),
        on_tool_result=functools.partial(_on_tool_result, agent),
        system_prompt_append=append,
        hermes_session_id=getattr(agent, "session_id", None),
        hermes_lineage=getattr(agent, "_claude_sdk_hermes_lineage", None),
        task_list_id=task_list_id,
        task_env=task_env,
        # Peer-addressable CLI session name (ListAgents/SendMessage).
        session_name=_sdk_session_name(agent),
        resume_session_id=resume_id,
        on_stream_delta=functools.partial(_relay_stream_delta, agent),
        on_interim_assistant=on_interim_assistant,
        on_tool_iteration=on_tool_iteration,
        on_unsolicited_result=on_unsolicited_result,
        # Mark a CLI-injected turn only when a result sink exists: that delivery path is what
        # retires the marker. Without one the result is dropped, and an uncleared marker would
        # replay an already-finished turn after the next restart.
        on_unsolicited_start=(
            functools.partial(_gateway_unsolicited_start_sink, agent)
            if on_unsolicited_result is not None else None
        ),
        on_unsolicited_header=(
            functools.partial(_gateway_unsolicited_header_sink, agent)
            if on_unsolicited_result is not None else None
        ),
        on_compaction=functools.partial(_on_compaction, agent),
        on_compact_boundary=functools.partial(_on_compact_boundary, agent),
        # Operator budget cap (agent.claude_agent_sdk.max_budget_usd);
        # None = no budget. Read per session creation so a config edit
        # applies on the next session, same as the append snapshot.
        max_budget_usd=_configured_max_budget_usd(),
        # Hybrid MCP bridge inputs (ported from PR #56413). Passing the
        # live agent + its OpenAI-format tool list activates an in-process
        # MCP server that exposes the full Hermes tool registry — so
        # proxified third-party MCP servers become reachable from inside
        # the SDK loop, not just the ~25 curated stdio tools.
        #
        # Off by default (agent.claude_agent_sdk.hybrid_mcp_bridge:
        # false) so a green-field upgrade is byte-identical to fcava's
        # stdio-only behaviour — the wide bridge exposes agent-level
        # tools whose enablement is a security choice. Operators opt in
        # explicitly.
        #
        # agent.tools is a snapshot taken at agent build time and never
        # re-reads the registry (see tools/mcp_tool.py::refresh_agent_mcp_tools
        # docstring). If an HTTP MCP finished connecting AFTER that snapshot
        # (e.g. slow initial handshake, or /reload-mcp), its tools would be
        # invisible to the hybrid bridge. Force a refresh here so the bridge
        # sees the current registry — the same call turn_context.py does
        # between turns, but pulled forward so it also applies to the
        # session-creation build.
        agent=(agent if _hybrid_bridge_enabled() else None),
        tools=(
            _snapshot_agent_tools_with_mcp_refresh(agent)
            if _hybrid_bridge_enabled()
            else None
        ),
    )
    _publish_claude_sdk_session(agent, session)
    # The SDK session owns Task* parsing; this bridge supplies the Hermes
    # owner/registry identity without changing native delegate registration.
    agent._claude_sdk_session._on_subagent_event = functools.partial(
        _on_sdk_subagent_event, agent
    )
    from agent.transports.claude_sdk_background_tasks import register_sdk_session_control

    register_sdk_session_control(
        getattr(agent, "_gateway_session_key", None) or getattr(agent, "session_id", None),
        agent._claude_sdk_session,
    )
    if resume_id and task_list_id and getattr(
        agent, "_claude_sdk_todo_snapshot_bootstrapped", False
    ) is not True:
        # The Claude task store is authoritative. Bootstrap only after the SDK
        # session object exists and only on a true resume; later tool activity
        # refreshes the same read-only snapshot path.
        try:
            bootstrap = getattr(agent, "_claude_sdk_todo_snapshot_bootstrap", None)
            if callable(bootstrap):
                snapshot = bootstrap(
                    task_list_id=task_list_id, task_store_root=str(task_store_root)
                )
                if snapshot is not None:
                    agent._claude_sdk_todo_snapshot_bootstrapped = True
        except Exception:
            logger.debug("SDK task snapshot bootstrap failed", exc_info=True)
    # The prologue persisted Hermes' native composed prompt — a prompt
    # this runtime never sends. Overwrite the snapshot with the
    # EFFECTIVE prompt so the audit trail tells the truth.
    try:
        if getattr(agent, "_session_db", None) and agent.session_id:
            agent._session_db.update_system_prompt(
                agent.session_id, "[claude_code preset]\n\n" + (append or "")
            )
    except Exception:
        logger.debug("effective-prompt snapshot failed", exc_info=True)
    return session


def _refresh_turn_visibility(agent, state: _SdkTurnState) -> None:
    """Per-turn: fresh visibility callbacks, pushed onto an already-live session.

    Also the workspace fence. A cached agent can be reused after the workspace
    moved; its live SDK session is still bound to the OLD cwd, and the child
    CLI's cwd is fixed at spawn. Retire it here — before the callbacks are
    pushed onto a session we are about to discard — so the attempt loop creates
    a fresh session in the current workspace.
    """
    state.on_interim_assistant, state.on_tool_iteration = _make_visibility_callbacks(agent)
    live_session = getattr(agent, "_claude_sdk_session", None)
    live_cwd = getattr(live_session, "_cwd", None) if live_session is not None else None
    if isinstance(live_cwd, str):
        try:
            workspace_moved = _canonical_sdk_cwd(live_cwd) != _canonical_sdk_cwd()
        except Exception:
            # resolve_agent_cwd() raises for real: a deleted launch directory
            # (its docstring keeps that OSError deliberate) and a refusal
            # terminal scope both land here, and the reuse path never resolved
            # a cwd before this fence existed. The fence is an optimisation —
            # the resume binding still declines a foreign session — so keep the
            # live session rather than killing the turn.
            logger.debug(
                "claude-agent-sdk: workspace fence skipped (cwd unresolvable)",
                exc_info=True,
            )
            workspace_moved = False
        if workspace_moved:
            logger.info(
                "claude-agent-sdk: retiring live session after workspace change"
            )
            try:
                live_session.close()
            except Exception:
                logger.debug("workspace-change session close failed", exc_info=True)
            _clear_claude_sdk_session_if_current(agent, live_session)
            live_session = None
    if live_session is not None:
        try:
            live_session.set_turn_visibility_callbacks(
                on_interim_assistant=state.on_interim_assistant,
                on_tool_iteration=state.on_tool_iteration,
            )
            # A background completion can beat this turn's runtime callback
            # setup on a reused session. Reinstall the enabled sink here so
            # any retained completion is replayed before the host turn starts.
            background_sink = _background_result_sink(agent)
            if background_sink is not None:
                live_session.set_unsolicited_result_callback(background_sink)
        except Exception:
            logger.debug("claude-sdk visibility callback refresh failed", exc_info=True)


def _intact_session_error(
    error: Any,
    *,
    replayed: bool,
    after_transient_retry: bool = False,
    over_budget: bool = False,
    continued: int = 0,
) -> str:
    """The user-facing error for a post-query CLI death that is not replayed.

    ``replayed``: the turn's one automatic re-send already ran (so the replay
    budget is spent); ``after_transient_retry``: that re-send was a
    transient-API retry; ``over_budget``: a replay would cross the turn's
    max_budget_usd. The recovery facts lead: gateways truncate the error
    near 200 chars.
    """
    if continued:
        why = f"exited again after {continued} automatic continue{'s' if continued != 1 else ''}"
    elif over_budget:
        why = "exited mid-turn and max_budget_usd leaves no room to replay it"
    elif not replayed:
        why = "exited mid-turn after tools ran or output was shown, so the turn was not replayed"
    elif after_transient_retry:
        why = "exited during the automatic retry of a transient API error"
    else:
        why = "exited again during the automatic retry"
    cause = str(error or "SDK message stream ended unexpectedly")
    return (
        f"Claude CLI {why}. The Claude session is intact and your next message "
        f"continues it. ({cause})"
    )


def _run_sdk_attempts(agent, state: _SdkTurnState) -> Optional[Dict[str, Any]]:
    """Drive the turn through the session: resume when an id is persisted,
    retire and retry ONCE with the continuity digest on a failed or stale
    resume.

    Sets ``state.turn`` / ``state.resumed``. Returns the failed result dict
    when the session RAISED (a dead turn, not a recoverable partial), else
    None.
    """
    user_input = state.user_input
    messages = state.messages
    turn = None
    resumed = False
    send_input = user_input
    retry_retired_before_query = False
    recovering_dead_turn = False
    # A replay-safe post-query CLI death re-sends the turn ONCE on the same
    # Claude session: the id it resumes (None = the persisted one).
    replaying_post_query_death = False
    replay_resume_id: Optional[str] = None

    def _emit_child_exited(session, reason: Any, turn: Any = None) -> None:
        sink = _background_result_sink(agent)
        if (
            sink is None
            or getattr(turn, "interrupted", False)
            or getattr(agent, "_interrupt_requested", False)
            or not _claim_child_exit_emission(session)
        ):
            return
        safe_reason = redact_sensitive_text(
            str(reason or "SDK message stream ended unexpectedly"), force=True
        ).strip()
        if len(safe_reason) > 240:
            safe_reason = safe_reason[:237].rstrip() + "..."
        exit_code = None
        for owner in (turn, session, getattr(session, "_client", None)):
            for attribute in ("exit_code", "returncode", "return_code"):
                value = getattr(owner, attribute, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    exit_code = value
                    break
            if exit_code is not None:
                break
        sink.lifecycle("child_exited", exit_code=exit_code, reason=safe_reason)

    max_transient_retries, retry_backoff, max_retry_wait = (
        _configured_transient_retry_policy()
    )
    transient_retries = 0
    total_retry_wait = 0.0
    force_fresh_retry = False
    # D62 "continue, don't replay": a turn that dies mid-work continues the SAME
    # Claude session: L2 (the CLI re-runs its interrupted turn itself), else ONE
    # user-role nudge. Bounded per user turn; never after a user stop, auth,
    # billing or startup failure. The prompt is never re-sent by this path.
    from agent.transports.claude_agent_sdk_session_turn import RESUME_INTERRUPTED_TURN

    l2_enabled, max_continues, continue_backoff, replay_opt_in = _configured_continue_policy()
    continues = 0
    continue_id: Optional[str] = None
    continue_mode: Optional[str] = None
    continue_reason = ""
    nudge_input: Any = None
    continued_projection: list = []
    attempt_resume_id: Optional[str] = None
    if getattr(agent, "_claude_sdk_continue_requested", False) is True:
        # A gateway continue (error-card Retry / usage-limit resume): this
        # turn's input IS the continuation note. Try L2 first on the persisted
        # session; the note is the nudge when the CLI declines.
        agent._claude_sdk_continue_requested = False
        persisted = _persisted_sdk_session_id(agent)
        if persisted:
            continue_id, continue_reason = persisted, "retry"
            continue_mode = "l2" if l2_enabled else "nudge"
            nudge_input = user_input

    def _schedule_continue(dead_turn: Any, dead_session: Any, reason: str) -> bool:
        nonlocal continues, continue_id, continue_mode, continue_reason, nudge_input
        if continues >= max_continues or getattr(agent, "_interrupt_requested", False):
            return False
        if getattr(dead_turn, "fatal_reason", None) is not None:
            return False  # auth / startup stay terminal
        sid = (
            getattr(dead_turn, "thread_id", None)
            or attempt_resume_id
            or _persisted_sdk_session_id(agent)
        )
        if not isinstance(sid, str) or not sid:
            return False  # nothing to continue: the CLI never announced a session
        if dead_session is not None:
            # The dead client may still hold the interrupted turn's frames.
            try:
                dead_session.close()
            except Exception:
                pass
            _clear_claude_sdk_session_if_current(agent, dead_session)
        _store_sdk_session_id(agent, sid, cwd=state.turn_session_cwd)
        continues += 1
        continue_id, continue_reason, nudge_input = sid, reason, None
        continue_mode = "l2" if l2_enabled else "nudge"
        # The dead attempt's projected work stays in the transcript: the
        # continuation adds to it rather than redoing it.
        continued_projection.extend(getattr(dead_turn, "projected_messages", None) or [])
        emit = getattr(agent, "_emit_status", None)
        if callable(emit):
            try:
                emit(f"Interrupted, continuing… ({continues}/{max_continues})")
            except Exception:
                logger.debug("failed to emit continue status", exc_info=True)
        sink = _background_result_sink(agent)
        if sink is not None:
            sink.lifecycle("continuing", attempt=continues, reason=reason, mode=continue_mode)
        _log = logger.info if reason == "watchdog" else logger.warning
        _log(
            "claude-agent-sdk: turn interrupted (%s); continuing session (%s/%s, %s)",
            reason, continues, max_continues, continue_mode,
        )
        wait = continue_backoff[min(continues - 1, len(continue_backoff) - 1)]
        if wait:
            time.sleep(wait)
        if getattr(agent, "_interrupt_requested", False):
            dead_turn.interrupted = True
            return False
        return True

    # Every attempt a later attempt superseded: its spend is folded into the
    # turn's accounting, and max_budget_usd applies to the turn's total.
    spent_attempts: list = []
    # ONE replay budget per user turn. A failed attempt that already reached
    # the API is re-sent by exactly ONE mechanism, never both:
    #   * a post-query CLI death: ONE replay on the same Claude session, only
    #     from attempt 0 (so never after a transient retry or any recovery);
    #   * a transient API failure: up to N retries (N =
    #     transient_retry_max_retries, clamped 0..10, default 2), never after a
    #     post-query-death replay.
    # So such re-sends total at most max(1, N). Both re-send only a
    # replay-safe attempt (_sdk_attempt_replay_safe). The only other re-send is
    # at most ONE legacy recovery (a pre-query retirement / stream end, or a
    # failed resume retried fresh), all gated on attempt 0. run_turn therefore
    # runs at most max(2, N + 2) times per user turn: the loop bound below.
    for attempt in range(max(2, max_transient_retries + 2) + 2 * max_continues + 1):
        if turn is not None:
            spent_attempts.append(turn)
            turn = None
        if getattr(agent, "_claude_sdk_rename_pending", False) is True:
            # A title change landed while a turn was live; apply it now by rebuilding the CLI
            # with the new --name (the resume id is kept, so the conversation continues).
            agent._claude_sdk_rename_pending = False
            rotate_claude_sdk_session(agent, "session renamed")
        # The CLI binds --model at process start, so a model switch under a live
        # session is invisible to it until the process is rebuilt. Check every
        # attempt, at the same boundary the rename rotation uses.
        rotate_claude_sdk_session_on_model_change(agent)
        # Snapshot the identity slot once. Rotation publishes/clears the same
        # lock; keep the lock only across this read, never across construction,
        # run_turn, or close().
        with _claude_sdk_session_lock(agent):
            live_session = getattr(agent, "_claude_sdk_session", None)
        stream_ended = (
            inspect.getattr_static(live_session, "_stream_ended", None)
            if live_session is not None
            else None
        )
        closed = (
            inspect.getattr_static(live_session, "_closed", False) is True
            if live_session is not None
            else False
        )
        retiring = (
            inspect.getattr_static(live_session, "_retiring", False) is True
            if live_session is not None
            else False
        )
        if attempt == 0 and live_session is not None and (
            stream_ended is not None or closed or retiring
        ):
            # A gateway record can outlive the CLI stream. Retire only an
            # explicitly dead adapter: a starting client can have no child pid yet.
            _retire_live_sdk_session(agent)
            if getattr(agent, "_interrupt_requested", False):
                _consume_agent_interrupt(agent)
                return {
                    "final_response": "claude-agent-sdk turn interrupted",
                    "messages": messages,
                    "api_calls": 0,
                    "completed": False,
                    "partial": True,
                    "interrupted": True,
                    "failed": False,
                    "error": None,
                }
            live_session = None
        attempt_resume_id = None
        if live_session is None:
            if continue_id:
                # D62: continue the same Claude session (never a fresh one).
                resume_id = continue_id
            elif replaying_post_query_death:
                # The post-query-death replay resumes the dead attempt's own
                # Claude session (None = the persisted id).
                resume_id = replay_resume_id or _persisted_sdk_session_id(agent)
            elif force_fresh_retry:
                # A transient-API retry starts fresh (with the digest): the
                # failed attempt's id was cleared before the backoff.
                resume_id = None
            else:
                resume_id = (
                    _persisted_sdk_session_id(agent)
                    if attempt == 0 or retry_retired_before_query
                    else None
                )
            force_fresh_retry = False
            attempt_resume_id = resume_id
            resumed = bool(resume_id)
            send_input = user_input
            if not resume_id and len(messages) > 1:
                digest = _render_continuity_digest(_continuity_digest_source(agent, messages))
                if digest:
                    if isinstance(user_input, list):
                        send_input = [
                            {"type": "text", "text": digest},
                            *user_input,
                        ]
                    else:
                        send_input = digest + user_input
                    if recovering_dead_turn:
                        sink = _background_result_sink(agent)
                        if sink is not None:
                            sink.lifecycle(
                                "resumed",
                                digest_messages=len(messages) - 1,
                                resume_id_present=False,
                            )
            elif resume_id and recovering_dead_turn:
                sink = _background_result_sink(agent)
                if sink is not None:
                    sink.lifecycle(
                        "resumed",
                        digest_messages=0,
                        resume_id_present=True,
                    )
            session = _create_session(
                agent,
                resume_id=resume_id,
                on_interim_assistant=state.on_interim_assistant,
                on_tool_iteration=state.on_tool_iteration,
                **(
                    {"resume_interrupted_turn": True}
                    if continue_id and continue_mode == "l2"
                    else {}
                ),
            )
            # Keep compatibility with narrow test seams and third-party
            # wrappers that perform the assignment but return nothing.
            if session is None:
                session = agent._claude_sdk_session
        else:
            session = live_session
        if continue_id:
            # A freshly spawned L2 session consumes the CLI's own re-run and
            # sends nothing; otherwise (L2 off or declined) ONE nudge.
            send_input = (
                RESUME_INTERRUPTED_TURN
                if continue_mode == "l2" and live_session is None
                else (nudge_input if nudge_input is not None else _continue_nudge(continue_reason))
            )

        # Keep the exact object used for this attempt. Rotation/cleanup may
        # replace the agent slot while this turn is unwinding.
        turn_session_cwd = getattr(session, "_cwd", None)
        if not isinstance(turn_session_cwd, str):
            # The live session is the authority, but it can be absent here (a
            # retired client, a stand-in). Resolve the fallback NOW rather than
            # leaving None for the persist phase to fill in: persist runs after
            # run_turn, so a workspace that moved mid-turn would be sampled
            # post-move and the binding would always match itself. Sampling at
            # turn start is what makes the check able to fail.
            try:
                from agent.runtime_cwd import resolve_agent_cwd

                turn_session_cwd = str(resolve_agent_cwd())
            except Exception:
                logger.debug(
                    "claude-agent-sdk: turn workspace unresolvable", exc_info=True
                )
                turn_session_cwd = None
        state.turn_session_cwd = turn_session_cwd
        hook = getattr(agent, "_note_transport_activity", None)
        if callable(hook):
            try:
                # SDK stream activity reaches the agent's liveness generation (turn_liveness race).
                session._turn_activity_hook = hook
            except Exception:
                pass
        # The SDK reader settles multiple queries inside this one host turn.
        # Refresh the target list every run because a cached SDK session can
        # span many Hermes turns, each with a different live message list.
        session._on_steer_settled = functools.partial(
            _persist_steer_boundary, agent, state.messages
        )
        try:
            turn = session.run_turn(user_input=send_input)
            if getattr(turn, "watchdog_trip", False):
                logger.warning(
                    "claude-agent-sdk: watchdog trip: %s",
                    redact_sensitive_text(
                        str(getattr(turn, "error", None) or "turn timed out"), force=True
                    ),
                )
        except Exception as exc:
            safe_exc = redact_sensitive_text(str(exc), force=True)
            _emit_child_exited(session, safe_exc)
            recovering_dead_turn = True
            interrupted = bool(getattr(agent, "_interrupt_requested", False))
            # A PreCompact hook may have opened a transient user-visible status.
            # This exception bypasses the normal terminal edge below; clear it
            # here so a later unrelated turn cannot announce stale completion.
            if getattr(agent, "_sdk_compaction_pending", False):
                agent._sdk_compaction_pending = False
                try:
                    emit = getattr(agent, "_emit_status", None)
                    if callable(emit):
                        emit("⚠️ Context compaction interrupted")
                except Exception:
                    logger.debug("failed to close interrupted compaction status", exc_info=True)
            # Do not use logger.exception here: it appends the raw exception
            # string after the redacted message to the log record.
            logger.error("claude-agent-sdk turn failed: %s", safe_exc)
            try:
                session.close()
            except Exception:
                pass
            _clear_claude_sdk_session_if_current(agent, session)
            # The sample above was taken BEFORE close(). Tearing the transport
            # down is not instantaneous, so a stop admitted during cleanup is
            # still a stop against this turn — re-read the flag now that the
            # session is gone. Deciding on the stale pre-close sample would
            # replay the prompt the user just asked to abandon and leave the
            # agent-level flag set to poison the next turn. Sticky OR: a stop
            # observed before close stays observed regardless of cleanup.
            interrupted = interrupted or bool(
                getattr(agent, "_interrupt_requested", False)
            )
            if interrupted:
                # The session close above consumes transport-local interrupt
                # state, and the session object is discarded either way, so
                # no live transport survives to carry it. Consume the agent
                # layer too: this dead turn honored the user's stop and must
                # not reject the next message.
                _consume_agent_interrupt(agent)
            if resumed and attempt == 0 and not interrupted:
                # A raising RESUMED session is a suspect resume — clear the
                # id and give the turn one fresh chance (digest included).
                # Never replay a turn that concurrently received /stop.
                _store_sdk_session_id(agent, None)
                resumed = False
                continue
            # No TurnResult reaches post-turn accounting on this path: record
            # what the superseded attempts already spent.
            _record_spent_attempts(agent, spent_attempts)
            return {
                "final_response": f"claude-agent-sdk turn failed: {safe_exc}",
                "messages": messages,
                "api_calls": 0,
                "completed": False,
                "partial": True,
                "interrupted": interrupted,
                # run_turn consumes its own exceptions into TurnResult, so
                # anything RAISING here is a dead turn, not a recoverable
                # partial — mark it failed so one-shot runs exit nonzero
                # (mirrors conversation_loop's generic non-retryable return).
                "failed": not interrupted,
                "error": safe_exc,
            }

        if continue_id and getattr(turn, "resume_declined", False):
            # L2 not applicable (the CLI found no interrupted turn): nudge on
            # the same live session. Not a new continue.
            continue_mode = "nudge"
            continue

        if getattr(turn, "retired_before_query", False):
            interrupted = bool(
                getattr(turn, "interrupted", False)
                or getattr(agent, "_interrupt_requested", False)
            )
            if interrupted:
                turn.interrupted = True
            try:
                session.close()
            except Exception:
                pass
            # The stop can arrive during cleanup. Re-read after close so a
            # retired-before-query prompt is never replayed by attempt 1.
            interrupted = interrupted or bool(
                getattr(turn, "interrupted", False)
                or getattr(agent, "_interrupt_requested", False)
            )
            if interrupted:
                turn.interrupted = True
            _clear_claude_sdk_session_if_current(agent, session)
            if attempt == 0 and not interrupted:
                # Rotation already stashed the cwd-bound resume envelope. Keep
                # it intact and consume it when the replacement session builds;
                # this is a benign pre-query retirement, not a failed resume.
                retry_retired_before_query = True
                resumed = False
                continue
        elif getattr(turn, "should_retire", False):
            stream_ended_before_query = bool(
                getattr(turn, "stream_ended", False)
                and getattr(turn, "api_call_made", None) is False
            )
            # The CLI died after the query was submitted. Its Claude session
            # is on disk and resumable (auth failures stay terminal).
            post_query_death = bool(
                getattr(turn, "stream_ended", False)
                and not stream_ended_before_query
                and getattr(turn, "fatal_reason", None) is None
            )
            _emit_child_exited(session, getattr(turn, "error", None), turn)
            recovering_dead_turn = True
            _log = logger.info if getattr(turn, "watchdog_trip", False) else logger.warning
            _log(
                "claude-agent-sdk session retired (turn error: %s)",
                redact_sensitive_text(str(turn.error or ""), force=True),
            )
            try:
                session.close()
            except Exception:
                pass
            _clear_claude_sdk_session_if_current(agent, session)
            if (
                getattr(turn, "watchdog_trip", False)
                and not getattr(agent, "_interrupt_requested", False)
            ):
                # A hard watchdog kill is not a user stop: continue, not end.
                if _schedule_continue(turn, None, "watchdog"):
                    resumed = False
                    continue
                if getattr(turn, "interrupted", False) and getattr(agent, "_interrupt_requested", False):
                    break
            if post_query_death:
                # Never clear a resumable session on a CLI death: keep the id
                # (refreshing it from the dead turn when it announced one) so
                # the replay, or the user's next message, resumes it.
                dead_turn_id = getattr(turn, "thread_id", None)
                if isinstance(dead_turn_id, str) and dead_turn_id:
                    _store_sdk_session_id(
                        agent, dead_turn_id, cwd=state.turn_session_cwd
                    )
                else:
                    dead_turn_id = attempt_resume_id
                interrupted = bool(
                    getattr(turn, "interrupted", False)
                    or getattr(agent, "_interrupt_requested", False)
                )
                replay_safe = _sdk_attempt_replay_safe(agent, turn, state)
                # The shared budget is untouched only on the first attempt:
                # a transient retry, a legacy recovery, or this replay itself
                # all advance ``attempt``.
                if not interrupted and _schedule_continue(turn, None, "cli_exit"):
                    resumed = False
                    continue
                interrupted = interrupted or bool(getattr(agent, "_interrupt_requested", False))
                budget_untouched = (
                    attempt == 0 and transient_retries == 0 and continues == 0 and replay_opt_in
                )
                over_budget = (
                    budget_untouched
                    and replay_safe
                    and _resend_exceeds_budget(spent_attempts, turn)
                )
                if (
                    budget_untouched
                    and not interrupted
                    and replay_safe
                    and not over_budget
                ):
                    replaying_post_query_death = True
                    replay_resume_id = dead_turn_id
                    resumed = False
                    continue
                if not interrupted:
                    turn.error = _intact_session_error(
                        turn.error,
                        replayed=replay_safe,
                        after_transient_retry=transient_retries > 0,
                        over_budget=over_budget,
                        continued=continues,
                    )
                break
            # A pre-query stream end did not touch the remote conversation, so
            # its persisted id remains valid for the replacement adapter.
            if not stream_ended_before_query and not getattr(turn, "watchdog_trip", False):
                # A watchdog-killed session is resumable (D62): keep its id.
                _store_sdk_session_id(agent, None)
            if (
                stream_ended_before_query
                and attempt == 0
                and not getattr(turn, "interrupted", False)
                and not getattr(agent, "_interrupt_requested", False)
            ):
                retry_retired_before_query = True
                resumed = False
                continue
            if (
                resumed
                and attempt == 0
                and not getattr(turn, "interrupted", False)
                and not getattr(agent, "_interrupt_requested", False)
            ):
                # Stale/failed resume: one fresh retry with digest. Never for
                # an INTERRUPTED retire (user /stop that killed the CLI, or a
                # hard watchdog trip) — re-running the stopped turn in full
                # would evaporate the stop and deliver the answer anyway.
                resumed = False
                continue

        if (
            getattr(turn, "watchdog_trip", False)
            and not getattr(turn, "should_retire", False)
            and not getattr(agent, "_interrupt_requested", False)
        ):
            # Clean-ack watchdog trip (partial transcript kept): continue it.
            if _schedule_continue(turn, session, "watchdog"):
                resumed = False
                continue
            break

        # D0 classifies the SDK's structured API signals. A transient failure
        # CONTINUES the same session (D62); a usage limit beyond the in-turn
        # window parks it; the legacy prompt replay is an opt-in last resort.
        if (
            getattr(turn, "error", None)
            # The shared budget: a post-query-death replay already spent it.
            and not replaying_post_query_death
            and not getattr(turn, "interrupted", False)
        ):
            api_signals = {
                "api_error_kind": getattr(turn, "api_error_kind", None),
                "api_error_status": getattr(turn, "api_error_status", None),
                "api_retries": getattr(turn, "api_retries", None),
                "rate_limit_rejected": getattr(turn, "rate_limit_rejected", None),
            }
            # Generic runtime failures have their own provider-failover path.
            # This retry policy is for API failures observed on the SDK stream.
            if not any(value is not None for value in api_signals.values()):
                break
            from agent.claude_sdk_transient import classify_sdk_api_failure

            verdict, error_class, wait_hint = classify_sdk_api_failure({
                "turn": turn,
                "agent": agent,
                "result_text": getattr(turn, "result_text", None) or turn.error,
                **api_signals,
            })
            if verdict == "transient" and error_class == "rate_limit" and _maybe_park_usage_limit(
                agent, turn, state, max(0.0, max_retry_wait - total_retry_wait)
            ):
                break
            if verdict == "transient" and _resend_exceeds_budget(
                spent_attempts, turn
            ):
                logger.warning(
                    "claude-agent-sdk: not retrying transient %s failure: the "
                    "turn's max_budget_usd leaves no room for another attempt",
                    error_class,
                )
                break
            if verdict == "transient":
                hinted = min(
                    max(0.0, float(wait_hint or 0.0)),
                    max(0.0, max_retry_wait - total_retry_wait),
                )
                if hinted and continues < max_continues:
                    time.sleep(hinted)
                    total_retry_wait += hinted
                if _schedule_continue(turn, session, error_class):
                    resumed = False
                    continue
                if getattr(turn, "interrupted", False) or getattr(agent, "_interrupt_requested", False):
                    turn.interrupted = True
                    break
            if (
                verdict == "transient"
                and replay_opt_in
                and transient_retries < max_transient_retries
                and _sdk_attempt_replay_safe(agent, turn, state)
            ):
                # Opt-in last resort (transient_retry_replay): the #31/U8.2
                # prompt replay in a fresh CLI session, replay-safe only.
                if getattr(agent, "_claude_sdk_session", None) is session:
                    try:
                        session.close()
                    except Exception:
                        pass
                    _clear_claude_sdk_session_if_current(agent, session)
                _store_sdk_session_id(agent, None)
                retry_index = transient_retries
                transient_retries += 1
                remaining_wait = max(0.0, max_retry_wait - total_retry_wait)
                requested_wait = (
                    wait_hint if wait_hint is not None
                    else retry_backoff[min(retry_index, len(retry_backoff) - 1)]
                )
                wait_seconds = min(max(0.0, float(requested_wait)), remaining_wait)
                emit = getattr(agent, "_emit_status", None)
                if callable(emit):
                    try:
                        emit(f"Retrying ({transient_retries}/{max_transient_retries})…")
                    except Exception:
                        logger.debug("failed to emit transient retry status", exc_info=True)
                logger.warning(
                    "claude-agent-sdk: retrying transient %s failure (%s/%s) in %.1fs",
                    error_class, transient_retries, max_transient_retries, wait_seconds,
                )
                if wait_seconds:
                    time.sleep(wait_seconds)
                    total_retry_wait += wait_seconds
                if getattr(agent, "_interrupt_requested", False):
                    # The failed turn may not be replayed after a stop arrives
                    # during its bounded backoff.
                    turn.interrupted = True
                    break
                force_fresh_retry = True
                resumed = False
                recovering_dead_turn = True
                continue
        break

    _fold_attempt_spend(spent_attempts, turn)
    if turn is not None and continued_projection:
        turn.projected_messages = [
            *continued_projection, *(getattr(turn, "projected_messages", None) or [])
        ]
    if turn is not None and continues and not getattr(turn, "error", None):
        sink = _background_result_sink(agent)
        if sink is not None:
            sink.lifecycle("continued", attempts=continues, reason=continue_reason)
    state.turn = turn
    state.resumed = resumed
    return None
