"""Turn-lifetime watchdog for the claude-agent-sdk session.

The activity-aware idle/quiet rules (``_TurnWatch``), their defaults, the
stream-end sentinel and the future-result swallowers used by interrupt/steer.
Extracted from ``claude_agent_sdk_session.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional
from agent.transports.claude_agent_sdk_session_availability import (
    _safe_sdk_error_text,
)

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.transports.claude_agent_sdk_session")


# ---------- turn-lifetime defaults ----------
# The turn IDLE limit (agent.claude_agent_sdk.turn_idle_timeout; the legacy
# turn_timeout key is read as an alias). Wall-clock elapsed never retires a
# turn: production 2026-09-21..27 lost orchestrator turns at 654s..7978s that
# were quiet for only ~30s (the old rule was elapsed >= 600s AND idle >= 30s).
# A turn is idle only when NO evidence of work exists (see _TurnWatch.check).
# 900s sits above the CLI's own per-request API timeout, so one silent model
# call (long thinking with partial messages off) cannot read as a wedge.
_DEFAULT_TURN_IDLE_TIMEOUT = 900.0
# Import alias for callers of the pre-idle name.
_DEFAULT_TURN_TIMEOUT = _DEFAULT_TURN_IDLE_TIMEOUT


# Post-tool quiet watchdog default WHEN streaming is on. It was 90s (codex
# parity, openclaw #81697); production 2026-09-26 killed a working turn 92s
# after a tool result (large-context time-to-first-token), so it is widened
# to 300s — still well under the idle limit, so it remains the early wedge
# catcher. With streaming OFF there is no liveness signal between a tool
# result and the next complete AssistantMessage — thinking is
# indistinguishable from wedged — so the watchdog defaults to DISABLED there
# (operator opt-in via post_tool_quiet_timeout).
_DEFAULT_POST_TOOL_QUIET_STREAMING = 300.0


# After a watchdog trip we interrupt the CLI and give _consume_turn this long
# to unwind on the interrupt-ack ResultMessage — a clean unwind preserves the
# partial transcript and the resumable session id; only expiry hard-cancels.
_TURN_ABORT_GRACE = 15.0


# An inter-poll gap this many times the poll interval means the PROCESS was
# stalled (swap/OOM descheduling on a memory-constrained host), not the turn
# — re-baseline instead of tripping on time nobody was actually waiting.
_POLL_STALL_FACTOR = 5.0


# Upper bound on how long a compaction may suspend the watchdog. compact_boundary
# is NOT guaranteed -- measured 2026-08-16, a compaction that started at 03:57:17
# never produced one -- so an unbounded gate would trade a killed turn for a hung
# one. 600s is an order of magnitude above the ~90-125s compactions observed in
# production while still landing well inside the gateway's 1800s ceiling.
_COMPACTION_MAX_SUSPEND = 600.0


# A background agent can outlive its Task tool result, so its lifecycle
# suspends the foreground turn watchdog. Bound that suspension so a lost
# terminal Task message cannot keep the turn alive indefinitely.
_TASK_MAX_SUSPEND = 4 * 60 * 60.0


class _TurnWatch:
    """Activity evidence for one in-flight turn.

    Threading contract: mutating calls happen on the session's loop thread
    (message drain, projections, approval bridge) with ONE sanctioned
    exception — rebaseline(), called from the run_turn poll thread, also
    writes last_activity. That dual-writer race is benign by construction:
    float stores are GIL-atomic (never torn), and a lost update leaves
    last_activity merely STALE, which the stall detector re-baselines and
    the two-poll debounce absorbs before any verdict — trips can only be
    DELAYED by it, never wrongly fired. Everything else is single-writer;
    the poll thread otherwise only READS. No lock, deliberately: a lock
    shared with the loop thread would risk stalling the SDK stream drain.

    Evidence gate: a turn with tool calls outstanding (issued ToolUseBlocks
    minus resolved ToolResultBlocks — server tools never enter the count,
    they resolve inside their own assistant message) or an approval prompt
    awaiting a human tap is PROVABLY working/waiting and is never tripped.
    If the CLI never resolves an issued tool id (interrupted mid-tool), the
    suspension persists and the gateway's 1800s inactivity ceiling remains
    the backstop — documented, deliberate."""

    def __init__(self) -> None:
        now = time.monotonic()
        self.started = now
        self.last_activity = now
        self.post_tool_armed = False
        self.outstanding_tools = 0
        self.approvals_pending = 0
        self.compaction_active = 0
        self.compaction_started = 0.0
        self.active_tasks: dict[str, float] = {}
        self.task_gate_started = 0.0
        # Ids of issued-but-unresolved tool calls, and the Task tool call
        # that launched each live task: an outstanding Task call is bounded
        # by _TASK_MAX_SUSPEND, an ordinary outstanding tool is not.
        self.outstanding_tool_ids: set[str] = set()
        self.task_parent_tools: dict[str, str] = {}
        # The idle limit run_turn enforces, published for liveness() readers
        # (agent/turn_liveness.py keeps its own limit above it). 0 = unset.
        self.idle_limit = 0.0

    # -- loop-thread writers --

    def tick(self) -> None:
        self.last_activity = time.monotonic()

    def note_tools_issued(self, count: int, ids: Iterable[Any] = ()) -> None:
        if count > 0:
            self.outstanding_tools += count
            self.outstanding_tool_ids.update(i for i in ids if i)

    def note_tools_resolved(self, count: int, ids: Iterable[Any] = ()) -> None:
        if count > 0:
            self.outstanding_tools = max(0, self.outstanding_tools - count)
            self.outstanding_tool_ids.difference_update(ids)
            if self.outstanding_tools == 0:
                self.outstanding_tool_ids.clear()

    def note_task_started(
        self, task_id: str, parent_tool_id: Optional[str] = None
    ) -> None:
        if task_id:
            started = time.monotonic()
            self.active_tasks[task_id] = started
            if parent_tool_id:
                self.task_parent_tools[task_id] = parent_tool_id
            if len(self.active_tasks) == 1:
                self.task_gate_started = started

    def note_task_terminal(self, task_id: str) -> None:
        started = self.active_tasks.pop(task_id, None)
        self.task_parent_tools.pop(task_id, None)
        if started == self.task_gate_started:
            self.task_gate_started = min(self.active_tasks.values(), default=0.0)

    def clear_tasks(self) -> None:
        self.active_tasks.clear()
        self.task_parent_tools.clear()
        self.task_gate_started = 0.0

    def _outstanding_tools_are_live_tasks(self) -> bool:
        """Every outstanding tool call is the Task call of a live task."""
        task_calls = self.outstanding_tool_ids & set(self.task_parent_tools.values())
        return bool(task_calls) and self.outstanding_tools <= len(task_calls)

    def arm_post_tool(self) -> None:
        self.post_tool_armed = True

    def disarm_post_tool(self) -> None:
        self.post_tool_armed = False

    def approval_begin(self) -> None:
        self.approvals_pending += 1
        self.tick()

    def approval_end(self) -> None:
        self.approvals_pending = max(0, self.approvals_pending - 1)
        self.tick()

    def compaction_begin(self) -> None:
        """PreCompact fired: the CLI is about to go silent, legitimately.

        Earliest start wins -- a re-entrant PreCompact must not restart the
        bounding clock, or a pathological loop could extend the suspension
        indefinitely, which is exactly what the bound exists to prevent.
        """
        if self.compaction_active == 0:
            self.compaction_started = time.monotonic()
        self.compaction_active += 1
        self.tick()

    def compaction_end(self) -> None:
        """compact_boundary arrived: resume normal watchdog rules.

        tick() is load-bearing, not hygiene. Without it a turn that compacted
        for 91s would resume already 91s idle and trip on the very next poll --
        the same kill, one poll later.
        """
        self.compaction_active = max(0, self.compaction_active - 1)
        self.tick()

    # -- caller-thread readers --

    def rebaseline(self) -> None:
        """After a detected process stall: the elapsed gap was spent
        descheduled, not waiting — restamp so neither rule fires on it."""
        self.last_activity = time.monotonic()

    def _suspended(self, now: float) -> bool:
        """True while the turn is PROVABLY working or waiting: an approval
        awaits a human, a tool is outstanding, a live Task runs, or a
        compaction is in progress. No idle rule may fire meanwhile."""
        if self.approvals_pending > 0:
            return True
        # An ordinary outstanding tool suspends indefinitely (documented
        # above). When the only outstanding calls are live Tasks' own Task
        # calls, the Task cap below bounds the suspension instead: a Task
        # whose ToolResult never arrives must not hold the turn forever.
        if self.outstanding_tools > 0 and not self._outstanding_tools_are_live_tasks():
            return True
        if self.task_gate_started and now - self.task_gate_started < _TASK_MAX_SUSPEND:
            return True
        # A compacting CLI is indistinguishable from a wedged one: between
        # PreCompact and compact_boundary it emits nothing at all. Without this
        # gate the post_tool_quiet rule reads that silence as a wedge and
        # interrupts the CLI mid-compaction, so the terminal ResultMessage never
        # arrives -- surfacing next turn as "discarding N stale unsolicited
        # text(s)" and, to the user, as a turn that simply died. Bounded, so a
        # boundary that never arrives cannot hang the turn instead.
        return (
            self.compaction_active > 0
            and (now - self.compaction_started) < _COMPACTION_MAX_SUSPEND
        )

    def liveness(self) -> tuple[float, float]:
        """``(idle_seconds, idle_limit)`` for outside watchdogs (read-only,
        lock-free like check()). idle is 0.0 while a gate suspends the turn."""
        now = time.monotonic()
        idle = 0.0 if self._suspended(now) else max(0.0, now - self.last_activity)
        return idle, float(self.idle_limit)

    def check(
        self, *, budget: float, quiet: float, max_seconds: float = 0.0
    ) -> Optional[str]:
        """Returns None (keep waiting), "max_seconds", "post_tool_quiet", or
        "budget".

        ``budget`` is the IDLE budget (turn_idle_timeout): "budget" means the
        turn went that long with no activity and nothing outstanding. Elapsed
        wall clock never trips it; only an explicitly configured
        ``max_seconds`` (turn_max_seconds, 0 = off) caps a turn absolutely."""
        now = time.monotonic()
        if max_seconds > 0 and now - self.started >= max_seconds:
            return "max_seconds"
        if self._suspended(now):
            return None
        idle = now - self.last_activity
        if quiet > 0 and self.post_tool_armed and idle >= quiet:
            return "post_tool_quiet"
        if idle >= budget:
            return "budget"
        return None


def _swallow_interrupt_result(future: Any) -> None:
    """Done-callback for the fire-and-forget client.interrupt() future: the
    SDK's control request times out after 60s on a wedged CLI and an
    unretrieved exception would log 'Future exception was never retrieved'
    at teardown — retrieve and demote it."""
    try:
        future.result()
    except Exception as exc:
        logger.debug(
            "SDK interrupt control request failed: %s",
            _safe_sdk_error_text(exc),
        )


_RENAME_ACK_PREFIX = "Session renamed to:"


def _is_rename_ack(result_text: Any, buffered: list, pending_name: Optional[str] = None) -> bool:
    """True for the CLI's ``/rename`` acknowledgement (deterministic text, proven 2026-09-09).

    Swallowed only when a rename ack is pending AND the text matches the exact
    expected ack for that target name.
    """
    if not pending_name:
        return False
    expected = f"{_RENAME_ACK_PREFIX} {pending_name}".strip()
    candidates = [result_text, *buffered]
    return any(isinstance(t, str) and t.strip() == expected for t in candidates)


def _swallow_steer_result(future: Any) -> None:
    """Same contract as _swallow_interrupt_result, for the fire-and-forget
    steer query(). The caller has already returned True by the time this
    resolves, so a failure here can only be logged, not surfaced."""
    try:
        future.result()
    except Exception:
        logger.debug("SDK steer query failed after scheduling", exc_info=True)


class _StreamEnd:
    """Reader-loop sentinel: the SDK message stream ended (CLI exited or the
    transport tore down). Routed to the in-flight turn so it fails fast and
    retires cleanly instead of waiting out its full turn_timeout on a dead
    stream."""

    def __init__(self, error: Optional[str] = None) -> None:
        self.error = error
