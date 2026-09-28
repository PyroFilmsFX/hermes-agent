"""Unit coverage for agent/turn_liveness.py config resolution (#95548/#95663).

AGENTS.md rejects new non-secret ``HERMES_*`` env knobs: the watchdog's
behavioral settings live in ``agent.turn_liveness`` in config.yaml, and the
resolver must validate them — a typo must never crash durable-turn startup,
and NaN/Inf must never silently disable the timeout or freeze the watcher
thread.
"""

from __future__ import annotations

import logging

from agent.turn_liveness import (
    DEFAULT_TURN_LIVENESS_POLL_S,
    DEFAULT_TURN_LIVENESS_TIMEOUT_S,
    resolve_turn_liveness_settings,
)


def test_defaults_when_config_missing_or_unset():
    # No config at all.
    assert resolve_turn_liveness_settings(None) == (
        DEFAULT_TURN_LIVENESS_TIMEOUT_S,
        DEFAULT_TURN_LIVENESS_POLL_S,
    )
    assert resolve_turn_liveness_settings({}) == (
        DEFAULT_TURN_LIVENESS_TIMEOUT_S,
        DEFAULT_TURN_LIVENESS_POLL_S,
    )
    # Section present but keys absent.
    assert resolve_turn_liveness_settings(
        {"agent": {"turn_liveness": {}}}
    ) == (DEFAULT_TURN_LIVENESS_TIMEOUT_S, DEFAULT_TURN_LIVENESS_POLL_S)


def test_precedence_explicit_values_win_over_defaults():
    timeout, poll = resolve_turn_liveness_settings(
        {"agent": {"turn_liveness": {"timeout_s": 30, "poll_s": 5}}}
    )
    assert timeout == 30.0
    assert poll == 5.0


def test_precedence_numeric_strings_accepted():
    # YAML may hand back strings; numeric coercion is part of the contract.
    timeout, poll = resolve_turn_liveness_settings(
        {"agent": {"turn_liveness": {"timeout_s": "45", "poll_s": "2.5"}}}
    )
    assert timeout == 45.0
    assert poll == 2.5


def test_timeout_zero_is_documented_opt_out():
    timeout, poll = resolve_turn_liveness_settings(
        {"agent": {"turn_liveness": {"timeout_s": 0, "poll_s": 15}}}
    )
    assert timeout is None
    assert poll == 15.0
    # Negative is the same documented "disabled" path.
    assert resolve_turn_liveness_settings(
        {"agent": {"turn_liveness": {"timeout_s": -1}}}
    )[0] is None


def test_typo_does_not_crash_and_falls_back_to_default(caplog):
    # The old raw float() env parsing raised ValueError into durable-turn
    # startup on a typo. The resolver must warn + default instead.
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        timeout, poll = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"timeout_s": "oops", "poll_s": "typo"}}}
        )
    assert timeout == DEFAULT_TURN_LIVENESS_TIMEOUT_S
    assert poll == DEFAULT_TURN_LIVENESS_POLL_S
    assert len(caplog.records) == 2
    assert all("agent.turn_liveness" in r.getMessage() for r in caplog.records)


def test_nan_timeout_falls_back_to_default_not_disabled(caplog):
    # float("nan") > 0 is False, so the old code silently disabled the
    # watchdog on NaN. The resolver must reject it and keep the default.
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        timeout, poll = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"timeout_s": float("nan")}}}
        )
    assert timeout == DEFAULT_TURN_LIVENESS_TIMEOUT_S  # NOT None
    assert poll == DEFAULT_TURN_LIVENESS_POLL_S
    assert len(caplog.records) == 1


def test_inf_poll_falls_back_to_default_not_frozen_watcher(caplog):
    # float("inf") poll made Event.wait(inf) hang the watcher thread
    # forever. The resolver must reject it.
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        timeout, poll = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"poll_s": float("inf")}}}
        )
    assert timeout == DEFAULT_TURN_LIVENESS_TIMEOUT_S
    assert poll == DEFAULT_TURN_LIVENESS_POLL_S
    assert len(caplog.records) == 1


def test_inf_timeout_falls_back_to_default(caplog):
    # inf timeout would never fire (idle < inf always true) — silent
    # disablement. Rejected.
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        timeout, _ = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"timeout_s": float("inf")}}}
        )
    assert timeout == DEFAULT_TURN_LIVENESS_TIMEOUT_S
    assert len(caplog.records) == 1


def test_non_positive_poll_falls_back_to_default(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        _, poll = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"poll_s": 0}}}
        )
    assert poll == DEFAULT_TURN_LIVENESS_POLL_S
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        _, poll = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": {"poll_s": -3}}}
        )
    assert poll == DEFAULT_TURN_LIVENESS_POLL_S


def test_malformed_sections_fall_back_to_defaults(caplog):
    # Non-dict `agent` or non-dict `turn_liveness` must not raise.
    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        result = resolve_turn_liveness_settings({"agent": "nonsense"})
    assert result == (DEFAULT_TURN_LIVENESS_TIMEOUT_S, DEFAULT_TURN_LIVENESS_POLL_S)

    with caplog.at_level(logging.WARNING, logger="agent.turn_liveness"):
        result = resolve_turn_liveness_settings(
            {"agent": {"turn_liveness": "nonsense"}}
        )
    assert result == (DEFAULT_TURN_LIVENESS_TIMEOUT_S, DEFAULT_TURN_LIVENESS_POLL_S)
    # The malformed section itself is surfaced as a warning.
    assert len(caplog.records) >= 1


# ---------- SDK lane: never fire before the transport's idle limit (2026-09-28) ----------
# Production 2026-09-25/27: this watchdog aborted SDK-lane turns at ~600s ("last activity:
# 'executing tool: Bash'") — the agent activity clock never sees stream deltas, a long-running
# CLI tool, a live background Task or an approval. It now reads the SDK turn watch and keeps its
# limit above the SDK idle limit, so the transport's clean unwind always gets first claim.


def _sdk_lane(monkeypatch, *, sdk_idle_limit=900.0, liveness_timeout=600.0):
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import activity_tracking, turn_liveness
    from agent.transports import claude_agent_sdk_session_watchdog as wd

    clock = SimpleNamespace(now=1000.0)
    timer = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(activity_tracking, "time", timer)
    monkeypatch.setattr(turn_liveness, "time", timer)
    monkeypatch.setattr(wd, "time", timer)
    agent = activity_tracking.ActivityTrackingMixin()
    agent.show_commentary = False
    agent._touch_activity("executing tool: Bash")
    watch = wd._TurnWatch()
    watch.idle_limit = sdk_idle_limit
    agent._claude_sdk_session = SimpleNamespace(_turn_watch=watch)
    abort = MagicMock(return_value=True)
    watchdog = turn_liveness.TurnLivenessWatchdog(
        agent, session_id="sdk", timeout_s=liveness_timeout, poll_s=15,
        stop_event=threading.Event(), activity_lock=agent._liveness_activity_lock(),
        is_turn_active=lambda: True, commit_abort=abort, deactivate_turn=MagicMock(),
    )
    return clock, watch, watchdog, abort


def test_sdk_lane_stream_activity_keeps_the_liveness_watchdog_quiet(monkeypatch):
    clock, watch, watchdog, abort = _sdk_lane(monkeypatch)
    for _ in range(120):  # 2h of one streamed delta a minute, agent clock untouched
        clock.now += 60.0
        watch.tick()
        assert watchdog._tick() is None
    abort.assert_not_called()


def test_sdk_lane_outstanding_tool_keeps_the_liveness_watchdog_quiet(monkeypatch):
    clock, watch, watchdog, abort = _sdk_lane(monkeypatch)
    watch.note_tools_issued(1, ids=["toolu_bash"])
    for _ in range(3 * 240):  # a 3h Bash call, polled every 15s
        clock.now += 15.0
        assert watchdog._tick() is None
    abort.assert_not_called()


def test_sdk_lane_liveness_never_fires_before_the_sdk_idle_limit(monkeypatch):
    # (f) Total silence: the SDK watch trips at its idle limit first; this watchdog stays a
    # backstop above it (limit + margin), even with a shorter turn_liveness.timeout_s.
    from agent.turn_liveness import _TRANSPORT_IDLE_MARGIN_S

    clock, watch, watchdog, abort = _sdk_lane(monkeypatch, sdk_idle_limit=900.0)
    while clock.now - 1000.0 < 900.0 + _TRANSPORT_IDLE_MARGIN_S - 15.0:
        clock.now += 15.0
        assert watchdog._tick() is None, f"fired at {clock.now - 1000.0:.0f}s"
    assert watch.check(budget=900.0, quiet=0.0) == "budget"  # the SDK rule tripped first
    abort.assert_not_called()
    clock.now += 30.0
    assert watchdog._tick() is False  # a wedge the SDK could not unwind is still caught
    abort.assert_called_once()


def test_liveness_keeps_its_own_limit_off_the_sdk_lane(monkeypatch):
    clock, _watch, watchdog, abort = _sdk_lane(monkeypatch)
    watchdog._agent._claude_sdk_session = None
    clock.now += 601.0
    assert watchdog._tick() is False
    abort.assert_called_once()


def test_sdk_lane_wedged_tool_is_bounded_by_the_tool_cap(monkeypatch):
    # Review P1-1: past turn_tool_max_suspend (4 h) a never-resolving tool stops suspending, so
    # this watchdog regains its backstop instead of deferring forever.
    from agent.turn_liveness import _TRANSPORT_IDLE_MARGIN_S

    clock, watch, watchdog, abort = _sdk_lane(monkeypatch)
    watch.note_tools_issued(1, ids=["toolu_wedged"])
    clock.now += 4 * 3600.0 - 15.0
    assert watchdog._tick() is None
    clock.now += 900.0 + _TRANSPORT_IDLE_MARGIN_S + 30.0
    assert watchdog._tick() is False
    abort.assert_called_once()
