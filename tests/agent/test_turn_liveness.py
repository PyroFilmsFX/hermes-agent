"""Unit coverage for agent/turn_liveness.py config resolution (#95548/#95663).

AGENTS.md rejects new non-secret ``HERMES_*`` env knobs: the watchdog's
behavioral settings live in ``agent.turn_liveness`` in config.yaml, and the
resolver must validate them — a typo must never crash durable-turn startup,
and NaN/Inf must never silently disable the timeout or freeze the watcher
thread.
"""

from __future__ import annotations

import logging
import pytest

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


# ---------- effective_idle_seconds and downstream timeout tests ----------

def test_effective_idle_seconds_sdk_lane():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from agent.turn_liveness import effective_idle_seconds

    # (1) Stale agent clock, fresh SDK watch -> min is SDK watch idle
    watch = MagicMock()
    watch.liveness.return_value = (5.0, 900.0)
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 2000.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )
    assert effective_idle_seconds(agent) == 5.0

    # (2) Genuinely idle SDK turn -> min is 1900.0
    watch.liveness.return_value = (1900.0, 900.0)
    assert effective_idle_seconds(agent) == 1900.0

    # (3) Non-SDK agent -> unchanged (agent clock idle)
    agent._claude_sdk_session = None
    assert effective_idle_seconds(agent) == 2000.0

    # (4) Torn read (RuntimeError) -> 0.0 idle (skips sample)
    agent._claude_sdk_session = SimpleNamespace(_turn_watch=watch)
    watch.liveness.side_effect = RuntimeError("torn read")
    assert effective_idle_seconds(agent) == 0.0

    # (5) Missing / unreadable agent -> None
    assert effective_idle_seconds(None) is None
    assert effective_idle_seconds(SimpleNamespace()) is None
    assert effective_idle_seconds(SimpleNamespace(get_activity_summary=lambda: {"seconds_since_activity": None})) is None


@pytest.mark.asyncio
async def test_gateway_run_sdk_agent_fresh_watch_does_not_timeout(monkeypatch):
    """A gateway run with an SDK agent whose agent clock is stale but whose SDK watch
    ticked recently is NOT timed out."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext

    class _Runner(GatewayTurnMixin):
        def _agent_activity_summary(self, a):
            return a.get_activity_summary() if a and hasattr(a, "get_activity_summary") else {}

        @staticmethod
        def _reaper_kwargs(w):
            return {
                "task_id": w.task_id, "process_baseline": w.process_baseline,
                "worker_done": w.worker_done, "timeout_fired": w.timeout_fired,
                "cleanup_lock": w.cleanup_lock, "is_still_current": w.is_current,
            }

        async def _run_agent_backup_interrupt_check(self, *a, **k):
            pass

        def _run_agent_timeout_result(self, w, ctx):
            return {"failed": True, "timed_out": True}

    watch = MagicMock()
    watch.liveness.return_value = (5.0, 900.0)
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 2000.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    poll_count = 0
    async def _mock_wait(fs, timeout=None):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return set(), set(fs)
        fut.set_result({"final_response": "done", "failed": False})
        return set(fs), set()

    monkeypatch.setattr(asyncio, "wait", _mock_wait)

    worker = GatewayRunner._RunAgentWorker(
        executor_task=fut,
        agent_timeout=1800.0,
        agent_warning=None,
        task_id="sdk-task",
        process_baseline=frozenset(),
        worker_done=threading.Event(),
        timeout_fired=threading.Event(),
        cleanup_lock=threading.Lock(),
        is_current=lambda: True,
    )
    turn_ctx = TurnContext(
        session_key="sdk-session", session_id="sdk-session",
        agent_holder=[agent], source=MagicMock(),
        result_holder=[{}], tools_holder=[[]],
    )

    runner = _Runner()
    res = await runner._run_agent_await_turn_worker(
        worker, turn_ctx, asyncio.Event(), MagicMock(done=lambda: False)
    )
    assert res == {"final_response": "done", "failed": False}


def test_watch_gateway_turn_inactivity_sdk_agent_fresh_watch_does_not_timeout():
    """_watch_gateway_turn_inactivity does not fire timeout for SDK agent with fresh watch."""
    import threading
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from gateway.run import _watch_gateway_turn_inactivity

    watch = MagicMock()
    watch.liveness.return_value = (5.0, 900.0)
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 2000.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )
    worker_done = threading.Event()
    timeout_fired = threading.Event()
    cleanup_lock = threading.Lock()

    t = threading.Thread(
        target=_watch_gateway_turn_inactivity,
        kwargs={
            "agent_holder": [agent], "task_id": "sdk-inactivity",
            "process_baseline": frozenset(), "timeout": 100.0,
            "worker_done": worker_done, "timeout_fired": timeout_fired,
            "cleanup_lock": cleanup_lock, "poll_interval": 0.01,
        },
    )
    t.start()
    time.sleep(0.05)
    worker_done.set()
    t.join(timeout=1.0)
    assert not timeout_fired.is_set()


@pytest.mark.asyncio
async def test_gateway_run_sdk_agent_genuinely_idle_times_out():
    """A gateway run with a genuinely idle SDK turn still times out."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext

    class _Runner(GatewayTurnMixin):
        def _agent_activity_summary(self, a):
            return a.get_activity_summary() if a and hasattr(a, "get_activity_summary") else {}

        @staticmethod
        def _reaper_kwargs(w):
            return {
                "task_id": w.task_id, "process_baseline": w.process_baseline,
                "worker_done": w.worker_done, "timeout_fired": w.timeout_fired,
                "cleanup_lock": w.cleanup_lock, "is_still_current": w.is_current,
            }

        async def _run_agent_backup_interrupt_check(self, *a, **k):
            pass

        def _run_agent_timeout_result(self, w, ctx):
            return {"failed": True, "timed_out": True}

    watch = MagicMock()
    watch.liveness.return_value = (1900.0, 900.0)  # genuinely idle past 1800s
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 2000.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    worker = GatewayRunner._RunAgentWorker(
        executor_task=fut,
        agent_timeout=1800.0,
        agent_warning=None,
        task_id="idle-sdk-task",
        process_baseline=frozenset(),
        worker_done=threading.Event(),
        timeout_fired=threading.Event(),
        cleanup_lock=threading.Lock(),
        is_current=lambda: True,
    )
    turn_ctx = TurnContext(
        session_key="idle-sdk-session", session_id="idle-sdk-session",
        agent_holder=[agent], source=MagicMock(),
        result_holder=[{}], tools_holder=[[]],
    )

    runner = _Runner()
    res = await runner._run_agent_await_turn_worker(
        worker, turn_ctx, asyncio.Event(), MagicMock(done=lambda: False)
    )
    assert res == {"failed": True, "timed_out": True}


@pytest.mark.asyncio
async def test_gateway_run_non_sdk_agent_unchanged(monkeypatch):
    """Off the SDK lane the gateway timeout behaviour is unchanged:
    stale agent clock times out, fresh agent clock succeeds."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext

    class _Runner(GatewayTurnMixin):
        def _agent_activity_summary(self, a):
            return a.get_activity_summary() if a and hasattr(a, "get_activity_summary") else {}

        @staticmethod
        def _reaper_kwargs(w):
            return {
                "task_id": w.task_id, "process_baseline": w.process_baseline,
                "worker_done": w.worker_done, "timeout_fired": w.timeout_fired,
                "cleanup_lock": w.cleanup_lock, "is_still_current": w.is_current,
            }

        async def _run_agent_backup_interrupt_check(self, *a, **k):
            pass

        def _run_agent_timeout_result(self, w, ctx):
            return {"failed": True, "timed_out": True}

    runner = _Runner()

    # (1) Stale non-SDK agent times out
    stale_agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 2000.0},
        _claude_sdk_session=None,
    )
    loop = asyncio.get_running_loop()
    fut1 = loop.create_future()
    worker1 = GatewayRunner._RunAgentWorker(
        executor_task=fut1, agent_timeout=1800.0, agent_warning=None,
        task_id="stale-non-sdk", process_baseline=frozenset(),
        worker_done=threading.Event(), timeout_fired=threading.Event(),
        cleanup_lock=threading.Lock(), is_current=lambda: True,
    )
    ctx1 = TurnContext(
        session_key="stale-non-sdk", session_id="stale-non-sdk",
        agent_holder=[stale_agent], source=MagicMock(),
        result_holder=[{}], tools_holder=[[]],
    )
    res1 = await runner._run_agent_await_turn_worker(
        worker1, ctx1, asyncio.Event(), MagicMock(done=lambda: False)
    )
    assert res1 == {"failed": True, "timed_out": True}

    # (2) Fresh non-SDK agent completes normally
    fresh_agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 10.0},
        _claude_sdk_session=None,
    )
    fut2 = loop.create_future()
    poll_count = 0
    async def _mock_wait(fs, timeout=None):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return set(), set(fs)
        fut2.set_result({"final_response": "done", "failed": False})
        return set(fs), set()

    monkeypatch.setattr(asyncio, "wait", _mock_wait)

    worker2 = GatewayRunner._RunAgentWorker(
        executor_task=fut2, agent_timeout=1800.0, agent_warning=None,
        task_id="fresh-non-sdk", process_baseline=frozenset(),
        worker_done=threading.Event(), timeout_fired=threading.Event(),
        cleanup_lock=threading.Lock(), is_current=lambda: True,
    )
    ctx2 = TurnContext(
        session_key="fresh-non-sdk", session_id="fresh-non-sdk",
        agent_holder=[fresh_agent], source=MagicMock(),
        result_holder=[{}], tools_holder=[[]],
    )
    res2 = await runner._run_agent_await_turn_worker(
        worker2, ctx2, asyncio.Event(), MagicMock(done=lambda: False)
    )
    assert res2 == {"final_response": "done", "failed": False}



def test_orphan_reaper_sdk_agent_fresh_watch_defers(monkeypatch):
    """The TUI detached orphan reaper does NOT time out / reap an SDK agent whose
    agent clock is stale but whose SDK watch ticked recently."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tui_gateway import server

    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)

    watch = MagicMock()
    watch.liveness.return_value = (5.0, 900.0)  # fresh SDK watch
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 700.0},  # stale agent clock
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )
    session = {"agent": agent, "running": True}
    assert server._ws_orphan_turn_activity_is_fresh(session) is True


def test_orphan_reaper_sdk_agent_genuinely_idle_times_out(monkeypatch):
    """The TUI detached orphan reaper times out / reaps a genuinely idle SDK turn."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tui_gateway import server

    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)

    watch = MagicMock()
    watch.liveness.return_value = (650.0, 900.0)  # genuinely idle past 600s
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 700.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )
    session = {"agent": agent, "running": True}
    assert server._ws_orphan_turn_activity_is_fresh(session) is False


def test_orphan_reaper_non_sdk_agent_unchanged(monkeypatch):
    """Off the SDK lane the orphan reaper behavior is unchanged:
    stale agent clock is NOT fresh, fresh agent clock IS fresh."""
    from types import SimpleNamespace
    from tui_gateway import server

    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)

    # Stale non-SDK agent
    stale_agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 700.0},
        _claude_sdk_session=None,
    )
    assert server._ws_orphan_turn_activity_is_fresh({"agent": stale_agent, "running": True}) is False

    # Fresh non-SDK agent
    fresh_agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 10.0},
        _claude_sdk_session=None,
    )
    assert server._ws_orphan_turn_activity_is_fresh({"agent": fresh_agent, "running": True}) is True


def test_orphan_reaper_sdk_torn_read_defers_reap(monkeypatch):
    """A torn read on the SDK watch reports 0.0 idle and defers reap (skips sample)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tui_gateway import server

    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)

    watch = MagicMock()
    watch.liveness.side_effect = RuntimeError("torn read")
    agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 700.0},
        _claude_sdk_session=SimpleNamespace(_turn_watch=watch),
    )
    session = {"agent": agent, "running": True}
    assert server._ws_orphan_turn_activity_is_fresh(session) is True

