"""Activity-aware turn lifetime — claude-agent-sdk runtime tests (#25267).

Split from ``tests/agent/test_claude_sdk_runtime.py``; the SDK message
stand-ins, fake clients and shared builders live in
``tests.agent.claude_sdk_fakes``.
"""

import asyncio
import gc
import sys
import threading
import time
import warnings
from types import SimpleNamespace

import pytest

from agent.transports.claude_agent_sdk_session import (
    ClaudeAgentSdkSession,
)
from tests.agent.claude_sdk_fakes import (
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    _text_delta_event,
    ResultMessage,
    _FakeClient,
    _plant_claude_agent_sdk_stand_in,
    _make_session,
    _make_hold_open_session,
    _make_agent,
    _ede_interrupt_ack,
    _wait_for_client,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    """Provider config, gateway contextvars and the CLI approval callback are
    reset around every test in this module — carried explicitly, never hoisted
    to a conftest (see ``isolate_provider_config``)."""
    yield from isolate_provider_config(monkeypatch)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def _close_promptly(session, timeout=1.0):
    done = threading.Event()
    def close_session():
        session.close()
        done.set()

    closer = threading.Thread(target=close_session, daemon=True)
    closer.start()
    closer.join(timeout=timeout)
    assert done.is_set(), "session.close() exceeded its bounded test timeout"
    assert not closer.is_alive()
    return True


# ---------- activity-aware turn lifetime (the 600s wall-clock fix) ----------
# Production forensics (24/7 gateway deployment, six "turn timed out after
# 600s" retires 2026-07-22 → 2026-08-09): four of six were ACTIVELY-WORKING
# turns — tool loops mid-execution, human approval taps counted as silence —
# killed by a hard wall clock over the whole turn. The lifetime is now evidence-based:
# outstanding tools and pending approvals suspend the rules; the budget only
# fires on a turn that is ALSO quiet; a post-tool quiet watchdog catches
# wedges early; a tripped turn that the CLI acks cleanly keeps its partial
# transcript and resume id instead of retiring.

class TestTurnLifetime:
    def test_timed_out_entry_wait_does_not_retire_over_a_settled_future(self, monkeypatch):
        """R10-W7-1: when the 0.1s entry observation times out and the session is
        retiring, a future the loop already completed must be harvested, never
        turned into retired_before_query (which would replay the prompt)."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_session(script=[ResultMessage(result="done")])
        session.ensure_started()
        consumed = threading.Event()
        original_consume = session._consume_turn
        real_event = threading.Event

        async def complete_consume(prompt):
            result = await original_consume(prompt)
            consumed.set()
            return result

        class _DeschedulingEvent(real_event):
            armed = True

            def wait(self, timeout=None):
                if timeout == 0.1 and _DeschedulingEvent.armed:
                    _DeschedulingEvent.armed = False
                    # The caller is descheduled past its 0.1s budget; meanwhile the
                    # loop finishes the query and a rotation marks the session retiring.
                    assert consumed.wait(timeout=2.0)
                    time.sleep(0.05)
                    session._retiring = True
                    return False
                return real_event.wait(self, timeout)

        session._consume_turn = complete_consume
        monkeypatch.setattr(turn_module.threading, "Event", _DeschedulingEvent)
        try:
            turn = session.run_turn(
                "harvest me",
                turn_timeout=1.0,
                watch_poll_interval=0.02,
                abort_grace=0.1,
            )
            assert turn.retired_before_query is not True
            assert turn.api_call_made is True
            assert turn.error is None
        finally:
            session._retiring = False
            _close_promptly(session)

    def test_completed_query_is_harvested_before_retirement_retry(self, monkeypatch):
        """A completed loop-owned result wins over a caller-side retire fence."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_session(script=[ResultMessage(result="done")])
        session.ensure_started()
        real_schedule = asyncio.run_coroutine_threadsafe
        consumed = threading.Event()
        original_consume = session._consume_turn

        async def complete_consume(prompt):
            result = await original_consume(prompt)
            consumed.set()
            return result

        def submit_then_retire(coro, loop):
            future = real_schedule(coro, loop)
            assert consumed.wait(timeout=2.0)
            session._retiring = True
            return future

        session._consume_turn = complete_consume
        monkeypatch.setattr(
            turn_module.asyncio,
            "run_coroutine_threadsafe",
            submit_then_retire,
        )
        try:
            turn = session.run_turn("completed before retire", turn_timeout=1.0)
            assert turn.final_text == "done"
            assert turn.api_call_made is True
            assert turn.retired_before_query is not True, (
                "a completed query was discarded and authorized replay"
            )
        finally:
            session._retiring = False
            _close_promptly(session)

    def test_paused_bootstrap_is_finalized_on_the_loop_thread(self, monkeypatch, recwarn):
        """Retirement between coroutine entry and bootstrap publication is safe."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        entered = threading.Event()
        allow_bootstrap = threading.Event()
        real_threading = threading
        real_event = threading.Event
        real_schedule = asyncio.run_coroutine_threadsafe
        schedule_returned = threading.Event()
        bootstrap_coroutines = []
        consume_coroutines = []
        warnings_seen = []

        class PausingEvent:
            def __init__(self):
                self._event = real_event()

            def set(self):
                self._event.set()
                entered.set()
                assert allow_bootstrap.wait(timeout=2.0)

            def wait(self, timeout=None):
                return self._event.wait(timeout)

            def is_set(self):
                return self._event.is_set()

        monkeypatch.setattr(
            turn_module,
            "threading",
            SimpleNamespace(
                Event=PausingEvent,
                Lock=real_threading.Lock,
                RLock=real_threading.RLock,
            ),
        )

        def submit_then_retire(coro, loop):
            bootstrap_coroutines.append(coro)
            future = real_schedule(coro, loop)
            assert entered.wait(timeout=2.0)
            session._retiring = True
            schedule_returned.set()
            return future

        monkeypatch.setattr(
            turn_module.asyncio,
            "run_coroutine_threadsafe",
            submit_then_retire,
        )
        async def tracked_consume(*_args):
            await asyncio.sleep(5.0)

        def make_consume(*_args):
            coro = tracked_consume()
            consume_coroutines.append(coro)
            return coro

        session._consume_turn_with_admission = make_consume
        turn_holder = {}
        unawaited = []
        original_unraisablehook = sys.unraisablehook
        sys.unraisablehook = lambda args: unawaited.append(args)

        def run():
            try:
                turn_holder["turn"] = session.run_turn(
                    "paused bootstrap", turn_timeout=0.2, abort_grace=0.05
                )
            except BaseException as exc:
                turn_holder["error"] = exc

        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                assert entered.wait(timeout=2.0)
                assert schedule_returned.wait(timeout=2.0)
                # Keep bootstrap paused while the caller observes the retire
                # fence; the pre-fix caller-side close must hit a running
                # coroutine here.
                time.sleep(0.05)
                allow_bootstrap.set()
                worker.join(timeout=2.0)
                gc.collect()
                warnings_seen.extend(caught)
        finally:
            session._retiring = False
            _close_promptly(session)
            gc.collect()
            sys.unraisablehook = original_unraisablehook

        assert not worker.is_alive()
        assert not isinstance(turn_holder.get("error"), ValueError), (
            "caller closed a loop-owned coroutine during bootstrap"
        )
        assert not any(
            "was never awaited" in str(item.message) for item in warnings_seen
        )
        assert not any("was never awaited" in str(item.message) for item in recwarn)
        assert not any(
            "was never awaited" in str(item.exc_value) for item in unawaited
        )
        assert all(
            getattr(coro, "cr_frame", None) is None
            for coro in bootstrap_coroutines + consume_coroutines
        ), "submitted coroutine was left open after retirement"
    def test_cancelled_not_started_bootstrap_has_no_unawaited_consumer(self, monkeypatch):
        """Cancelling before the loop callback runs finalizes both coroutine layers."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        loop_blocked = threading.Event()
        allow_loop = threading.Event()
        real_schedule = asyncio.run_coroutine_threadsafe
        warnings_seen = []
        bootstrap_coroutines = []
        consume_coroutines = []

        def block_loop():
            loop_blocked.set()
            allow_loop.wait(timeout=2.0)

        session._loop.call_soon_threadsafe(block_loop)
        assert loop_blocked.wait(timeout=2.0)

        def cancel_before_loop_callback(coro, loop):
            if not bootstrap_coroutines:
                bootstrap_coroutines.append(coro)
            future = real_schedule(coro, loop)
            future.cancel()
            return future

        monkeypatch.setattr(
            turn_module.asyncio,
            "run_coroutine_threadsafe",
            cancel_before_loop_callback,
        )
        async def tracked_consume(*_args):
            await asyncio.sleep(5.0)

        def make_consume(*_args):
            coro = tracked_consume()
            consume_coroutines.append(coro)
            return coro

        session._consume_turn_with_admission = make_consume
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                turn = session.run_turn("cancelled bootstrap", turn_timeout=0.2)
                assert turn.should_retire is True
            finally:
                allow_loop.set()
                gc.collect()
                warnings_seen.extend(caught)
                _close_promptly(session)
        assert all(
            getattr(coro, "cr_frame", None) is None
            for coro in bootstrap_coroutines + consume_coroutines
        ), [getattr(coro, "cr_frame", None) for coro in bootstrap_coroutines + consume_coroutines]
        assert not consume_coroutines, "cancelled bootstrap created an orphan consumer"
        assert not any(
            "was never awaited" in str(item.message) for item in warnings_seen
        )

    def test_interrupted_registration_releases_caller_owned_reservation(self):
        """An interrupt after insertion cannot strand admission ownership."""
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()

        class InsertThenInterrupt(set):
            def add(self, item):
                super().add(item)
                raise KeyboardInterrupt("interrupted after reservation insertion")

        session._admission_reservations = InsertThenInterrupt()
        try:
            with pytest.raises(KeyboardInterrupt, match="after reservation insertion"):
                session.run_turn("reservation race", turn_timeout=0.2)
            assert session._admission_inflight == 0
            assert session._admission_done.is_set(), (
                "interrupted registration left the admission event unset"
            )
            _close_promptly(session, timeout=0.5)
        finally:
            _close_promptly(session)

    def test_admission_reservation_take_and_release_are_interrupt_safe(self, monkeypatch):
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        original_clear = session._admission_done.clear
        original_set = session._admission_done.set
        take_interrupt = True
        release_interrupt = True

        def interrupting_clear():
            nonlocal take_interrupt
            original_clear()
            if take_interrupt:
                take_interrupt = False
                raise KeyboardInterrupt("interrupted while taking reservation")

        def interrupting_set():
            nonlocal release_interrupt
            if release_interrupt:
                release_interrupt = False
                raise KeyboardInterrupt("interrupted while releasing reservation")
            original_set()

        monkeypatch.setattr(session._admission_done, "clear", interrupting_clear)
        try:
            with pytest.raises(KeyboardInterrupt, match="taking reservation"):
                session._reserve_turn_admission()
            assert session._admission_inflight == 0
            assert session._admission_done.is_set(), (
                "interrupted reservation take left admission event unset"
            )

            monkeypatch.setattr(session._admission_done, "set", interrupting_set)
            reservation = session._reserve_turn_admission()
            with pytest.raises(KeyboardInterrupt, match="releasing reservation"):
                session._release_turn_admission(reservation)
            session._release_turn_admission(reservation)
            assert session._admission_inflight == 0
            assert session._admission_done.is_set()
            assert reservation.released is True
        finally:
            _close_promptly(session)

    def test_admission_released_when_scheduling_raises(self, monkeypatch):
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        original_asyncio = turn_module.asyncio

        def raise_keyboard_interrupt(coroutine, loop):
            raise KeyboardInterrupt("schedule interrupted")

        monkeypatch.setattr(
            turn_module,
            "asyncio",
            SimpleNamespace(run_coroutine_threadsafe=raise_keyboard_interrupt),
        )
        try:
            with pytest.raises(KeyboardInterrupt, match="schedule interrupted"):
                session.run_turn("schedule gap", turn_timeout=0.2)
        finally:
            monkeypatch.setattr(turn_module, "asyncio", original_asyncio)
            _close_promptly(session)
        try:
            assert session._admission_inflight == 0
        finally:
            _close_promptly(session)

    def test_admission_reservation_is_unchanged_if_event_clear_raises(self, monkeypatch):
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        original_clear = session._admission_done.clear

        def raise_during_clear():
            raise RuntimeError("injected admission publication failure")

        monkeypatch.setattr(session._admission_done, "clear", raise_during_clear)
        try:
            with pytest.raises(RuntimeError, match="injected admission"):
                session._reserve_turn_admission()
            assert session._admission_inflight == 0
            assert session._admission_done.is_set()
        finally:
            monkeypatch.setattr(session._admission_done, "clear", original_clear)
            _close_promptly(session)

    def test_admission_released_when_preclaim_preparation_raises(self, monkeypatch):
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, _holder = _make_hold_open_session(script=[])

        def raise_projector():
            raise RuntimeError("projector preparation failed")

        monkeypatch.setattr(turn_module, "ClaudeSdkEventProjector", raise_projector)
        try:
            turn = session.run_turn("preclaim failure", turn_timeout=0.2)
            assert turn.should_retire is True
            assert "projector preparation failed" in turn.error
            assert session._admission_inflight == 0
            assert _close_promptly(session)
        finally:
            _close_promptly(session)

    def test_normal_turn_reserves_and_releases_once(self):
        session, _holder = _make_session(
            script=[AssistantMessage(content=[TextBlock("done")]), ResultMessage(result="done")]
        )
        session.ensure_started()
        reserved = []
        released = []
        original_reserve = session._reserve_turn_admission
        original_release = session._release_turn_admission

        def reserve(reservation):
            reservation = original_reserve(reservation)
            reserved.append(reservation)
            return reservation

        def release(reservation):
            released.append(reservation)
            return original_release(reservation)

        session._reserve_turn_admission = reserve
        session._release_turn_admission = release
        try:
            turn = session.run_turn("normal turn", turn_timeout=1.0)
            assert turn.error is None
            assert turn.final_text == "done"
            assert len(reserved) == 1
            assert released
            assert all(item is reserved[0] for item in released)
            assert reserved[0].released is True
            assert session._admission_inflight == 0
        finally:
            _close_promptly(session)

    def test_admission_releases_after_claim_while_turn_is_running(self):
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        admission_released = threading.Event()
        original_release = session._release_turn_admission

        def release(reservation):
            original_release(reservation)
            if reservation is not None and reservation.released:
                admission_released.set()

        session._release_turn_admission = release
        turn_holder = {}
        turn_thread = threading.Thread(
            target=lambda: turn_holder.setdefault(
                "turn",
                session.run_turn(
                    "hold open",
                    turn_timeout=30.0,
                    post_tool_quiet_timeout=0.0,
                    watch_poll_interval=0.02,
                ),
            ),
            daemon=True,
        )
        try:
            turn_thread.start()
            assert admission_released.wait(timeout=2.0)
            assert session._admission_inflight == 0
            assert session._turn_inbox is not None
            assert turn_thread.is_alive()
        finally:
            _close_promptly(session)
            turn_thread.join(timeout=2.0)
            assert not turn_thread.is_alive()

        assert turn_holder["turn"].error is not None
        assert "SDK message stream ended before this turn's result" in turn_holder[
            "turn"
        ].error

    def test_close_during_owned_turn_delivers_stream_end(self):
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        admission_released = threading.Event()
        original_release = session._release_turn_admission

        def release(reservation):
            original_release(reservation)
            if reservation is not None and reservation.released:
                admission_released.set()

        session._release_turn_admission = release
        turn_holder = {}
        turn_thread = threading.Thread(
            target=lambda: turn_holder.setdefault(
                "turn",
                session.run_turn(
                    "close me",
                    turn_timeout=30.0,
                    post_tool_quiet_timeout=0.0,
                    watch_poll_interval=0.02,
                ),
            ),
            daemon=True,
        )
        try:
            turn_thread.start()
            assert admission_released.wait(timeout=2.0)
            assert session._turn_inbox is not None
            _close_promptly(session, timeout=2.0)
            turn_thread.join(timeout=2.0)
            assert not turn_thread.is_alive()
            turn = turn_holder["turn"]
            assert turn.error is not None
            assert "SDK message stream ended before this turn's result" in turn.error
        finally:
            if turn_thread.is_alive():
                _close_promptly(session, timeout=2.0)
                turn_thread.join(timeout=2.0)
            assert not turn_thread.is_alive()

    def test_timeout_after_bootstrap_releases_admission(self):
        session, _holder = _make_hold_open_session(script=[])
        session.ensure_started()
        try:
            turn = session.run_turn(
                "timeout after bootstrap",
                turn_timeout=0.2,
                watch_poll_interval=0.02,
                abort_grace=0.05,
            )
            assert turn.should_retire is True
            assert session._admission_inflight == 0
        finally:
            _close_promptly(session)

    def test_stopped_loop_retires_before_query_and_releases_admission(self):
        session, holder = _make_session(script=[])
        stopped_loop = asyncio.new_event_loop()
        session._client = object()
        session._loop = stopped_loop
        try:
            turn = session.run_turn("stopped loop", turn_timeout=0.2)
            assert turn.should_retire is True
            assert turn.retired_before_query is True
            assert session._admission_inflight == 0
            assert "client" not in holder
        finally:
            session._client = None
            session._loop = None
            stopped_loop.close()
            _close_promptly(session)

    def test_loop_stops_between_admission_check_and_schedule(self, monkeypatch):
        import agent.transports.claude_agent_sdk_session as session_module
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, holder = _make_hold_open_session(script=[])
        session.ensure_started()
        monkeypatch.setattr(session_module, "_SDK_DISCONNECT_TIMEOUT_S", 0.05)
        schedule_entered = threading.Event()
        allow_schedule = threading.Event()
        loop_stopped = threading.Event()
        real_schedule = asyncio.run_coroutine_threadsafe

        def stop_loop():
            session._loop.stop()
            loop_stopped.set()

        def gated_schedule(coro, loop):
            schedule_entered.set()
            assert allow_schedule.wait(timeout=2.0)
            return real_schedule(coro, loop)

        monkeypatch.setattr(
            turn_module.asyncio, "run_coroutine_threadsafe", gated_schedule
        )
        turn_holder = {}
        turn_thread = threading.Thread(
            target=lambda: turn_holder.setdefault(
                "turn",
                session.run_turn("stopped between check and schedule", turn_timeout=5.0),
            ),
            daemon=True,
        )
        try:
            turn_thread.start()
            assert schedule_entered.wait(timeout=2.0)
            session._loop.call_soon_threadsafe(stop_loop)
            assert loop_stopped.wait(timeout=2.0)
            allow_schedule.set()
            turn_thread.join(timeout=1.0)
            assert not turn_thread.is_alive()
            assert turn_holder["turn"].retired_before_query is True
            assert turn_holder["turn"].error == (
                "SDK session is rotating; rebuilding CLI"
            )
            assert session._admission_inflight == 0
            assert holder["client"].queried == []
        finally:
            allow_schedule.set()
            turn_thread.join(timeout=2.0)
            _close_promptly(session)

    def test_active_turn_survives_past_turn_timeout(self):
        # RED on the pre-fix tree: the old hard wall clock kills this turn at
        # 0.5s with "turn timed out after 0s" even though tool results are
        # landing every 50ms. GREEN: activity extends the turn to completion.
        session, holder = _make_hold_open_session(script=[])
        stop = threading.Event()

        def feeder():
            client = _wait_for_client(holder)
            for i in range(18):  # ~0.9s of beats at 50ms, > 0.5s budget
                if stop.is_set():
                    return
                time.sleep(0.05)
                client.feed(
                    AssistantMessage(
                        content=[ToolUseBlock(id=f"t{i}", name="Read", input={})]
                    ),
                    UserMessage(
                        content=[ToolResultBlock(tool_use_id=f"t{i}", content="ok")]
                    ),
                )
            client.feed(
                AssistantMessage(content=[TextBlock("long job done")]),
                ResultMessage(result="long job done", uuid="uuid-long"),
            )

        thread = threading.Thread(target=feeder, daemon=True)
        thread.start()
        try:
            turn = session.run_turn(
                "big task",
                turn_timeout=0.5,
                post_tool_quiet_timeout=0.0,
                watch_poll_interval=0.02,
            )
        finally:
            stop.set()
            thread.join(timeout=5)
            _close_promptly(session)
        assert turn.error is None
        assert turn.should_retire is False
        assert turn.final_text == "long job done"

    def test_outstanding_tool_suspends_budget(self):
        # A single long-running tool emits NOTHING on the stream. The issued
        # ToolUseBlock keeps the turn suspended past the budget until its
        # result lands. RED on the pre-fix tree (dies at 0.3s).
        session, holder = _make_hold_open_session(
            script=[
                AssistantMessage(
                    content=[ToolUseBlock(id="slow", name="Bash", input={})]
                ),
            ]
        )

        def feeder():
            client = _wait_for_client(holder)
            time.sleep(0.9)  # 3x the budget, tool still "running"
            client.feed(
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="slow", content="done")]
                ),
                AssistantMessage(content=[TextBlock("tool finished")]),
                ResultMessage(result="tool finished", uuid="uuid-slow"),
            )

        thread = threading.Thread(target=feeder, daemon=True)
        thread.start()
        try:
            turn = session.run_turn(
                "run the slow tool",
                turn_timeout=0.3,
                post_tool_quiet_timeout=0.0,
                watch_poll_interval=0.02,
            )
        finally:
            thread.join(timeout=5)
            _close_promptly(session)
        assert turn.error is None
        assert turn.final_text == "tool finished"

    def test_live_background_task_suspends_budget_after_tool_result(self, monkeypatch):
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        watch.note_tools_issued(1)
        watch.note_tools_resolved(1)
        watch.note_task_started("task-1")

        clock["now"] += 601.0
        assert watch.check(budget=600.0, quiet=0.0) is None

    def test_terminal_background_task_releases_budget_gate(self, monkeypatch):
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        watch.note_task_started("task-1")
        clock["now"] += 601.0
        assert watch.check(budget=600.0, quiet=0.0) is None

        watch.note_task_terminal("task-1")
        assert watch.check(budget=600.0, quiet=0.0) == "budget"

    def test_live_background_task_has_a_hard_watchdog_bound(self, monkeypatch):
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        watch.note_task_started("task-1")
        clock["now"] += session_mod._TASK_MAX_SUSPEND + 1.0

        assert watch.check(budget=600.0, quiet=0.0) == "budget"

    def test_background_task_record_survives_a_normal_turn_end(self):
        """Review P1-1: a foreground turn may end while a background Agent
        Task still runs. Its record must survive so the later terminal Task
        notification completes the row (not a premature ``interrupted``)."""
        session, _holder = _make_session(script=[ResultMessage(result="done")])
        events = []
        session._on_subagent_event = lambda event, name, preview, args, **kw: (
            events.append((event, kw))
        )
        session._sdk_task_records = {
            "bg-task": {
                "goal": "background work",
                "parent_tool_id": None,
                "child_session_id": None,
            }
        }

        class TaskNotificationMessage:
            def __init__(self, **data):
                self.__dict__.update(data)

        try:
            turn = session.run_turn("finish", turn_timeout=30.0)
            assert "bg-task" in session._sdk_task_records
            assert [e for e, _kw in events if e == "subagent.complete"] == []

            session._notify_task_message(
                TaskNotificationMessage(
                    task_id="bg-task", status="completed", summary="all done"
                )
            )
        finally:
            _close_promptly(session)

        assert turn.error is None
        completes = [kw for e, kw in events if e == "subagent.complete"]
        assert len(completes) == 1
        assert completes[0]["status"] == "completed"
        assert completes[0]["summary"] == "all done"
        assert session._sdk_task_records == {}

    def test_outstanding_task_tool_does_not_bypass_the_task_cap(self, monkeypatch):
        """Review P1-2: a Task whose ToolResult never arrives is bounded by
        _TASK_MAX_SUSPEND like any live Task, not suspended forever."""
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        watch.note_tools_issued(1, ids=["toolu_task"])
        watch.note_task_started("task-1", parent_tool_id="toolu_task")

        clock["now"] += 601.0
        assert watch.check(budget=600.0, quiet=0.0) is None
        clock["now"] += session_mod._TASK_MAX_SUSPEND
        assert watch.check(budget=600.0, quiet=0.0) == "budget"

    def test_ordinary_outstanding_tool_still_suspends_past_the_task_cap(
        self, monkeypatch
    ):
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        # Ordinary tools have their OWN cap (turn_tool_max_suspend, 2026-09-28
        # review P1-1); widen it so this still proves the Task cap never
        # releases an ordinary in-flight tool.
        watch.tool_max_suspend = 2 * session_mod._TASK_MAX_SUSPEND
        watch.note_tools_issued(2, ids=["toolu_task", "toolu_bash"])
        watch.note_task_started("task-1", parent_tool_id="toolu_task")
        clock["now"] += session_mod._TASK_MAX_SUSPEND + 601.0
        # The Bash call is still in flight: the Task cap does not release it.
        assert watch.check(budget=600.0, quiet=0.0) is None

        watch.note_tools_resolved(1, ids=["toolu_bash"])
        assert watch.check(budget=600.0, quiet=0.0) == "budget"

    def test_task_tool_ids_flow_from_the_stream_into_the_watch(self):
        from agent.transports.claude_agent_sdk_session_watchdog import _TurnWatch
        from agent.transports.claude_sdk_event_projector import ClaudeSdkEventProjector

        watch = _TurnWatch()
        out = {"messages": [], "tool_iterations": 0, "model": None}
        step = ClaudeAgentSdkSession._project_message_step
        projector = ClaudeSdkEventProjector()
        step(projector, watch, AssistantMessage(content=[
            ToolUseBlock(id="toolu_task", name="Task", input={"prompt": "x"}),
        ]), out)
        assert watch.outstanding_tools == 1
        assert watch.outstanding_tool_ids == {"toolu_task"}
        step(projector, watch, UserMessage(content=[
            ToolResultBlock(tool_use_id="toolu_task", content="launched"),
        ]), out)
        assert watch.outstanding_tools == 0
        assert watch.outstanding_tool_ids == set()

    def test_post_tool_quiet_trips_on_wedge_clean_ack(self):
        # Wedge signature: a tool result lands, then the stream goes silent
        # (alive, no _EOS). The quiet watchdog trips fast, the CLI acks the
        # interrupt with the EDE shape — and the clean ack preserves the
        # partial transcript AND the resume id (no retire), while the trip
        # text WINS over the masked EDE ack (never "SDK result error ...").
        session, holder = _make_hold_open_session(
            script=[
                SystemMessage(session_id="sdk-wedge-1"),
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Grep", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="hits")]
                ),
            ],
            interrupt_ack=[_ede_interrupt_ack()],
        )
        try:
            turn = session.run_turn(
                "search",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.2,
                watch_poll_interval=0.05,
            )
        finally:
            _close_promptly(session)
        assert turn.error is not None
        assert "turn timed out" in turn.error
        assert "after a tool result" in turn.error
        assert "SDK result error" not in turn.error  # W22 mask + trip wins
        assert turn.interrupted is True
        # should_retire=False is the load-bearing assertion: the runtime
        # persists the resume id ONLY for non-retiring turns. thread_id just
        # has to be present for it to persist (its exact value tracks the
        # last session_id-bearing message — here the ack's own default).
        assert turn.should_retire is False  # clean ack — resumable
        assert turn.thread_id
        assert holder["client"].interrupted is True
        # Partial transcript survived the trip.
        roles = [m["role"] for m in turn.projected_messages]
        assert "assistant" in roles and "tool" in roles

    def test_post_tool_watchdog_resets_on_activity(self):
        # Assistant output after the tool result DISARMS the quiet watchdog
        # (codex-parity reset semantics). The post-disarm silence here (0.7s)
        # EXCEEDS the quiet limit (0.25s) — with the disarm neutered this
        # turn trips; with it, the turn completes untouched.
        session, holder = _make_hold_open_session(script=[])

        def feeder():
            client = _wait_for_client(holder)
            client.feed(
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Read", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="data")]
                ),
            )
            time.sleep(0.1)  # armed, under the limit
            client.feed(AssistantMessage(content=[TextBlock("thinking done")]))
            time.sleep(0.7)  # SILENCE past the limit — trips iff still armed
            client.feed(ResultMessage(result="thinking done", uuid="uuid-r"))

        thread = threading.Thread(target=feeder, daemon=True)
        thread.start()
        try:
            turn = session.run_turn(
                "go",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.25,
                watch_poll_interval=0.02,
            )
        finally:
            thread.join(timeout=5)
            _close_promptly(session)
        assert turn.error is None
        assert turn.final_text == "thinking done"
        # The load-bearing pin: a DISARMED watchdog never fires an interrupt
        # at all. (Without this, a broken disarm can hide behind the
        # completion-during-grace rescue, which still delivers the pre-trip
        # text — resilient, but the needless interrupt+reconnect cycle is
        # exactly what the disarm exists to avoid.)
        assert holder["client"].interrupted is False

    def test_stream_deltas_disarm_quiet_watchdog(self):
        # Streaming posture — the only posture where the quiet watchdog
        # defaults ON: partial deltas after a tool result prove the model
        # call is alive and DISARM the watchdog. The post-delta silence
        # (0.7s) exceeds the quiet limit (0.25s) — trips iff the StreamEvent
        # branch's disarm is broken.
        session, holder = _make_hold_open_session(script=[])
        session._streaming = True  # the __init__ snapshot, forced for the test

        def feeder():
            client = _wait_for_client(holder)
            client.feed(
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Bash", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="ok")]
                ),
            )
            for _ in range(3):
                time.sleep(0.05)
                client.feed(_text_delta_event("chunk "))
            time.sleep(0.7)  # silence past the limit — armed would trip
            client.feed(
                AssistantMessage(content=[TextBlock("streamed answer")]),
                ResultMessage(result="streamed answer", uuid="uuid-sd"),
            )

        thread = threading.Thread(target=feeder, daemon=True)
        thread.start()
        try:
            turn = session.run_turn(
                "stream it",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.25,
                watch_poll_interval=0.02,
            )
        finally:
            thread.join(timeout=5)
            _close_promptly(session)
        assert turn.error is None
        assert turn.final_text == "streamed answer"
        assert holder["client"].interrupted is False  # watchdog never fired

    def test_hard_trip_retires_and_clears_interrupt_event(self):
        # The CLI ignores the interrupt for the whole grace: hard-cancel,
        # retire (today's shape, now the rare fallback) — and the interrupt
        # event must NOT leak into the next turn on this session object.
        # RED on the pre-fix tree: the old timeout branch left the event set.
        session, holder = _make_hold_open_session(script=[])  # total silence
        try:
            turn = session.run_turn(
                "hello?",
                turn_timeout=0.2,
                post_tool_quiet_timeout=0.0,
                watch_poll_interval=0.05,
                abort_grace=0.2,
            )
            assert turn.error is not None
            assert "turn timed out after" in turn.error
            assert turn.should_retire is True
            assert turn.interrupted is True
            assert holder["client"].interrupted is True
            assert session._interrupt_event.is_set() is False
        finally:
            _close_promptly(session)

    def test_retiring_session_rejects_admission_before_query(self):
        from agent.claude_sdk_runtime import rotate_claude_sdk_session

        session, _holder = _make_hold_open_session(script=[])
        agent = _make_agent()
        agent._claude_sdk_session = session
        close_entered = threading.Event()
        allow_close = threading.Event()
        original_close = session.close

        def blocked_close():
            close_entered.set()
            allow_close.wait(timeout=5.0)
            original_close()

        session.close = blocked_close
        rotation = threading.Thread(
            target=rotate_claude_sdk_session,
            args=(agent, "test"),
            daemon=True,
        )
        rotation.start()
        turn_holder = {}
        turn_thread = None
        try:
            assert close_entered.wait(timeout=2.0)

            def run_turn():
                turn_holder["turn"] = session.run_turn(
                    "losing turn", turn_timeout=5.0, watch_poll_interval=0.02
                )

            turn_thread = threading.Thread(target=run_turn, daemon=True)
            turn_thread.start()
            turn_thread.join(timeout=1.0)
            assert not turn_thread.is_alive()
            turn = turn_holder["turn"]
            assert turn.retired_before_query is True
            assert turn.should_retire is True
            assert turn.error == "SDK session is rotating; rebuilding CLI"
            assert turn.api_call_made is False
            # The pre-start fence is load-bearing: a stale reference must not
            # resurrect a client or reader on an already retiring session.
            assert session._client is None
        finally:
            allow_close.set()
            if turn_thread is not None:
                turn_thread.join(timeout=2.0)
            rotation.join(timeout=2.0)
            _close_promptly(session)

    def test_close_during_startup_reservation_reaps_published_resources(self, monkeypatch):
        """A close racing loop publication owns the eventual SDK resources."""
        session, holder = _make_session(script=[])
        loop_ready = threading.Event()
        allow_publication = threading.Event()
        startup_holder = {}
        close_holder = {}
        close_returned = threading.Event()
        import agent.transports.claude_agent_sdk_session as session_module

        monkeypatch.setattr(session_module, "_SDK_DISCONNECT_TIMEOUT_S", 0.05)

        def gated_start_loop_thread():
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run_loop():
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=run_loop, name="claude-sdk-loop", daemon=True
            )
            thread.start()
            assert ready.wait(timeout=2.0)
            loop_ready.set()
            assert allow_publication.wait(timeout=2.0)
            with session._turn_callback_lock:
                session._loop = loop
                session._loop_thread = thread

        session._start_loop_thread = gated_start_loop_thread
        startup = threading.Thread(
            target=lambda: startup_holder.setdefault("result", session.ensure_started()),
            daemon=True,
        )
        closer = None
        try:
            startup.start()
            assert loop_ready.wait(timeout=2.0)

            closer = threading.Thread(
                target=lambda: (session.close(), close_returned.set()),
                daemon=True,
            )
            closer.start()
            assert _wait_until(lambda: session._closed)
            assert close_returned.wait(timeout=1.0)

            allow_publication.set()
            startup.join(timeout=2.0)
            closer.join(timeout=2.0)
            assert not startup.is_alive()
            assert not closer.is_alive()
            assert startup_holder["result"] is None
            assert session._client is None
            assert session._loop is None
            assert session._loop_thread is None
            assert "client" not in holder
            turn = session.run_turn("after close", turn_timeout=0.2)
            assert turn.retired_before_query is True
        finally:
            allow_publication.set()
            startup.join(timeout=2.0)
            if closer is not None:
                closer.join(timeout=2.0)
            _close_promptly(session)

    def test_production_loop_starter_fences_close_and_reaps_its_thread(self, monkeypatch):
        """A close racing production loop publication cannot leak its thread."""
        session, _holder = _make_session(script=[])
        monkeypatch.setattr(
            "agent.transports.claude_agent_sdk_session._SDK_DISCONNECT_TIMEOUT_S",
            0.05,
        )
        publication_entered = threading.Event()
        allow_publication = threading.Event()
        loop_read_entered = threading.Event()
        allow_loop_read = threading.Event()
        publication_done = threading.Event()
        starter_holder = {}
        published_resources = {}

        class GatedSession(ClaudeAgentSdkSession):
            def __getattribute__(self, name):
                if name == "_loop":
                    gate = object.__getattribute__(self, "_gate_loop_read")
                    if gate and not object.__getattribute__(self, "_loop_read_fired"):
                        object.__setattr__(self, "_loop_read_fired", True)
                        loop_read_entered.set()
                        assert allow_loop_read.wait(timeout=2.0)
                        # The teardown's first null check must retain this
                        # snapshot while startup publishes concurrently.
                        return None
                return object.__getattribute__(self, name)

            def __setattr__(self, name, value):
                if name in {"_loop", "_loop_thread"} and value is not None:
                    published_resources[name] = value
                object.__setattr__(self, name, value)

        session.__class__ = GatedSession
        session._gate_loop_read = True
        session._loop_read_fired = False

        original_lock = session._turn_callback_lock
        starter_thread = None

        class GateLock:
            def __enter__(self):
                caller = sys._getframe(1).f_code.co_name
                if (
                    threading.current_thread() is starter_thread
                    and caller == "_start_loop_thread"
                ):
                    publication_entered.set()
                    assert allow_publication.wait(timeout=2.0)
                original_lock.__enter__()
                return self

            def __exit__(self, *args):
                result = original_lock.__exit__(*args)
                caller = sys._getframe(1).f_code.co_name
                if (
                    threading.current_thread() is starter_thread
                    and caller == "_start_loop_thread"
                ):
                    publication_done.set()
                return result

            def acquire(self, *args, **kwargs):
                return original_lock.acquire(*args, **kwargs)

            def release(self):
                return original_lock.release()

        session._turn_callback_lock = GateLock()
        starter_thread = threading.Thread(
            target=lambda: starter_holder.setdefault("result", session.ensure_started()),
            daemon=True,
        )
        teardown = threading.Thread(target=session._stop_loop_thread, daemon=True)
        try:
            starter_thread.start()
            assert publication_entered.wait(timeout=2.0)
            teardown.start()
            assert loop_read_entered.wait(timeout=2.0)
            session._closed = True
            allow_publication.set()
            # On the unfixed path startup publishes while teardown is paused
            # after its first null check; on the fixed path teardown owns the
            # lifecycle lock and startup waits. Either way, release the read
            # only after the unfixed publication has crossed the boundary.
            publication_done.wait(timeout=0.5)
            allow_loop_read.set()
            starter_thread.join(timeout=2.0)
            teardown.join(timeout=2.0)
            assert not starter_thread.is_alive()
            assert not teardown.is_alive()
            assert starter_holder["result"] is None
            assert session._loop is None
            assert session._loop_thread is None
            assert not any(
                thread.name == "claude-sdk-loop" and thread.is_alive()
                for thread in threading.enumerate()
            ), "closed startup left the production loop thread alive"

            closed_session, _closed_holder = _make_session(script=[])
            closed_session._closed = True
            assert closed_session.ensure_started() is None
            closed_session._start_loop_thread()
            assert closed_session._loop is None
            assert closed_session._loop_thread is None
            _close_promptly(closed_session)
        finally:
            allow_publication.set()
            allow_loop_read.set()
            starter_thread.join(timeout=2.0)
            teardown.join(timeout=2.0)
            leaked_thread = published_resources.get("_loop_thread")
            leaked_loop = published_resources.get("_loop")
            if leaked_thread is not None and leaked_thread.is_alive() and leaked_loop is not None:
                leaked_loop.call_soon_threadsafe(leaked_loop.stop)
                leaked_thread.join(timeout=2.0)
            _close_promptly(session)

    def test_late_startup_disconnect_fallback_is_bounded(self, monkeypatch):
        session, _holder = _make_session(script=[])
        monkeypatch.setattr(
            "agent.transports.claude_agent_sdk_session._SDK_DISCONNECT_TIMEOUT_S",
            0.05,
        )

        class HangingClient:
            async def disconnect(self):
                await asyncio.Event().wait()

        client = HangingClient()
        loop = asyncio.new_event_loop()
        session._client = client
        session._loop = loop
        started = threading.Event()

        def cleanup():
            session._cleanup_startup_resources(client=client, loop=loop)
            started.set()

        worker = threading.Thread(target=cleanup, daemon=True)
        start = time.monotonic()
        try:
            worker.start()
            assert started.wait(timeout=0.5), (
                "late-startup cleanup did not return within its disconnect deadline"
            )
            assert time.monotonic() - start < 0.5
        finally:
            worker.join(timeout=0.2)
            if not loop.is_closed():
                loop.close()
            _close_promptly(session)

    def test_runtime_error_retire_path_clears_turn_watch(self, monkeypatch):
        """G7.10: the RuntimeError retire branch must not leave _turn_watch set."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, holder = _make_hold_open_session(script=[])
        session.ensure_started()
        original_stop = session._stop_loop_thread

        def stop_then_raise(coro, loop):
            coro.close()
            original_stop()  # the loop is gone by the time the caller handles it
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(
            turn_module.asyncio, "run_coroutine_threadsafe", stop_then_raise
        )
        try:
            turn = session.run_turn(
                "late loss",
                turn_timeout=0.4,
                watch_poll_interval=0.02,
                abort_grace=0.1,
            )
            assert turn.retired_before_query is True
            assert session._turn_watch is None
        finally:
            _close_promptly(session)

    def test_retire_after_submit_never_closes_running_bootstrap(self, monkeypatch):
        """G7.6: once submitted, the loop owns bootstrap_turn; the caller must
        cancel the future, not close a coroutine the loop is already running."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, holder = _make_hold_open_session(script=[])
        session.ensure_started()
        consume_started = threading.Event()
        consume_finished = threading.Event()
        outcome = {}
        real_schedule = asyncio.run_coroutine_threadsafe
        original_consume = session._consume_turn
        loop_errors = []

        async def observed_consume(prompt):
            consume_started.set()
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                outcome["cancelled"] = True
                raise
            except GeneratorExit:
                outcome["closed_by_caller"] = True
                raise
            finally:
                consume_finished.set()
            return await original_consume(prompt)

        def submit_then_retire(coro, loop):
            future = real_schedule(coro, loop)
            # Let the loop actually enter bootstrap_turn -> _consume_turn before
            # the caller thread re-checks the retire fence.
            assert consume_started.wait(timeout=2.0)
            session._retiring = True
            return future

        session._consume_turn = observed_consume
        session._loop.call_soon_threadsafe(
            session._loop.set_exception_handler,
            lambda _loop, ctx: loop_errors.append(ctx.get("message") or str(ctx)),
        )
        monkeypatch.setattr(
            turn_module.asyncio, "run_coroutine_threadsafe", submit_then_retire
        )
        try:
            turn = session.run_turn(
                "late retire",
                turn_timeout=0.4,
                watch_poll_interval=0.02,
                abort_grace=0.1,
            )
            assert turn.should_retire is True
            assert getattr(turn, "retired_before_query", False) is not True
            assert turn.api_call_made is True
            assert consume_finished.wait(timeout=2.0)
            assert outcome.get("closed_by_caller") is not True
            assert outcome.get("cancelled") is True
            assert not any("already awaited" in str(e) for e in loop_errors), loop_errors
        finally:
            session._retiring = False
            _close_promptly(session)

    def test_close_between_admission_and_scheduling_returns_retired(self, monkeypatch):
        """Closing after the fence does not stop the captured loop too early."""
        import agent.transports.claude_agent_sdk_session_turn as turn_module

        session, holder = _make_hold_open_session(script=[])
        session.ensure_started()
        schedule_entered = threading.Event()
        allow_schedule = threading.Event()
        stop_entered = threading.Event()
        allow_stop = threading.Event()
        consume_started = threading.Event()
        consume_loop_was_running = []
        schedule_gate_used = threading.Event()
        real_schedule = asyncio.run_coroutine_threadsafe
        original_consume = session._consume_turn
        original_stop = session._stop_loop_thread

        def gated_schedule(coro, loop):
            if not schedule_gate_used.is_set():
                schedule_gate_used.set()
                schedule_entered.set()
                assert allow_schedule.wait(timeout=2.0)
            return real_schedule(coro, loop)

        def gated_stop_loop_thread():
            stop_entered.set()
            assert allow_stop.wait(timeout=2.0)
            original_stop()

        async def observed_consume(prompt):
            consume_started.set()
            consume_loop_was_running.append(loop_is_running())
            return await original_consume(prompt)

        def loop_is_running():
            return session._loop is not None and session._loop.is_running()

        session._consume_turn = observed_consume
        session._stop_loop_thread = gated_stop_loop_thread
        monkeypatch.setattr(
            turn_module.asyncio, "run_coroutine_threadsafe", gated_schedule
        )
        turn_holder = {}
        turn_thread = threading.Thread(
            target=lambda: turn_holder.setdefault(
                "turn",
                session.run_turn(
                    "losing turn",
                    turn_timeout=0.4,
                    watch_poll_interval=0.02,
                    abort_grace=0.1,
                ),
            ),
            daemon=True,
        )
        closer = None
        try:
            turn_thread.start()
            assert schedule_entered.wait(timeout=2.0)

            closer = threading.Thread(target=session.close, daemon=True)
            closer.start()
            assert _wait_until(lambda: session._closed)
            if stop_entered.wait(timeout=0.3):
                allow_stop.set()
                closer.join(timeout=2.0)
                assert not closer.is_alive()

            allow_schedule.set()
            turn_thread.join(timeout=2.0)
            allow_stop.set()
            closer.join(timeout=2.0)
            assert not turn_thread.is_alive()
            assert not closer.is_alive()
            assert turn_holder["turn"].retired_before_query is True
            assert turn_holder["turn"].api_call_made is False
            assert holder["client"].queried == []
        finally:
            allow_schedule.set()
            allow_stop.set()
            turn_thread.join(timeout=2.0)
            if closer is not None:
                closer.join(timeout=2.0)
            _close_promptly(session)

    def test_reader_cancellation_wakes_pending_claim(self, monkeypatch):
        # Hold the reader after it dequeues the claim but before it can
        # acknowledge claim_ack.  Looking only for _turn_inbox observes the
        # claim after acknowledgement and misses this shutdown race.
        claim_dequeued = threading.Event()
        allow_ack = threading.Event()
        real_queue = asyncio.Queue

        class GatedQueue(real_queue):
            async def get(self):
                item = await super().get()
                if isinstance(item, tuple) and item and item[0] == "claim":
                    claim_dequeued.set()
                    await asyncio.to_thread(allow_ack.wait)
                return item

        monkeypatch.setattr(asyncio, "Queue", GatedQueue)
        session, _holder = _make_hold_open_session(script=[])
        turn_holder = {}

        def run_turn():
            turn_holder["turn"] = session.run_turn(
                "cancelled reader", turn_timeout=5.0, watch_poll_interval=0.02
            )

        turn_thread = threading.Thread(target=run_turn, daemon=True)
        turn_thread.start()
        try:
            assert claim_dequeued.wait(timeout=2.0)
            session._stop_reader()
            turn_thread.join(timeout=1.0)
            assert not turn_thread.is_alive()
            turn = turn_holder["turn"]
            assert turn.should_retire is True
            assert turn.error is not None
            assert session._client.queried == []
        finally:
            allow_ack.set()
            turn_thread.join(timeout=2.0)
            _close_promptly(session)

    def test_completion_during_grace_delivered_in_full(self):
        # The answer text streamed BEFORE the trip; the success ack's uuid
        # then completes the turn inside the grace. Completion wins: no trip
        # error, no retire, the PRE-TRIP text is delivered. (The ack's own
        # result= text is never projected — projection stops at the
        # interrupt — so final_text here is the earlier AssistantMessage's.)
        session, holder = _make_hold_open_session(
            script=[
                AssistantMessage(content=[TextBlock("the answer")]),
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Bash", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="ok")]
                ),
            ],
            interrupt_ack=[
                ResultMessage(result=None, uuid="uuid-late")
            ],
        )
        try:
            turn = session.run_turn(
                "answer then wedge",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.2,
                watch_poll_interval=0.05,
            )
        finally:
            _close_promptly(session)
        assert turn.error is None
        assert turn.should_retire is False
        assert turn.interrupted is False
        assert turn.final_text == "the answer"

    def test_success_ack_without_prior_text_stays_a_trip(self):
        # Negative control for the completion lane's final_text gate: a
        # SUCCESS ack with a uuid but NO answer text anywhere (pure
        # tool-work turn) must remain a trip — voiding it here would turn
        # the timeout into a silent empty delivery.
        session, holder = _make_hold_open_session(
            script=[
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Bash", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="ok")]
                ),
            ],
            interrupt_ack=[
                ResultMessage(result=None, uuid="uuid-empty")
            ],
        )
        try:
            turn = session.run_turn(
                "tool work then wedge",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.2,
                watch_poll_interval=0.05,
            )
        finally:
            _close_promptly(session)
        assert turn.error is not None
        assert "turn timed out" in turn.error
        assert turn.interrupted is True
        assert turn.should_retire is False  # clean ack still preserves resume

    def test_coroutine_timeout_error_classified_not_spun(self):
        # py3.11 unifies the TimeoutError family: a TimeoutError RAISED BY
        # the turn coroutine (socket/pipe timeout under the CLI) must be
        # classified as a turn failure — the pre-fix tree misread it as the
        # turn hitting its own 600s wall ("turn timed out after 600s").
        class TimeoutRaisingClient(_FakeClient):
            async def query(self, text):
                raise TimeoutError("socket write timed out")

        holder = {}

        def factory(options=None):
            holder["client"] = TimeoutRaisingClient(options=options)
            return holder["client"]

        session = ClaudeAgentSdkSession(cwd="/tmp", client_factory=factory)
        try:
            turn = session.run_turn("hi", watch_poll_interval=0.05)
        finally:
            _close_promptly(session)
        assert turn.error is not None
        assert "claude-agent-sdk turn failed" in turn.error
        assert "socket write timed out" in turn.error
        assert "turn timed out after" not in turn.error

    def test_pending_approval_suspends_watchdogs(self, monkeypatch):
        # A human being asked is not the turn being silent: while the
        # session's own can_use_tool bridge awaits the approval callback,
        # both rules stand down — the quiet watchdog (armed by the tool
        # result) must NOT trip during a 0.5s approval on a 0.15s quiet
        # limit. After the tap the turn completes.
        _plant_claude_agent_sdk_stand_in(monkeypatch)
        released = threading.Event()

        def slow_approval(preview, prompt, **kwargs):
            time.sleep(0.5)
            released.set()
            return "once"

        session, holder = _make_hold_open_session(
            script=[
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Write", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="ok")]
                ),
            ],
            approval_callback=slow_approval,
            permission_mode="default",
        )

        def feeder():
            # The approval prompt fires on the session loop while the turn
            # is in flight — the exact production shape (SDK invoking
            # can_use_tool mid-turn). Event-based sync: wait until the tool
            # result actually ARMED the watchdog, then fire the approval
            # immediately (a sleep here left a ~36ms margin against the
            # quiet limit — flake fuel on loaded CI).
            _wait_for_client(holder)
            deadline = time.monotonic() + 5
            while True:
                watch = session._turn_watch
                if watch is not None and watch.post_tool_armed:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("watchdog never armed")
                time.sleep(0.01)
            cb = session._make_can_use_tool()
            fut = asyncio.run_coroutine_threadsafe(
                cb("Write", {"file_path": "/x"}, SimpleNamespace(tool_use_id="t1")),
                session._loop,
            )
            fut.result(timeout=5)
            holder["client"].feed(
                AssistantMessage(content=[TextBlock("written")]),
                ResultMessage(result="written", uuid="uuid-appr"),
            )

        thread = threading.Thread(target=feeder, daemon=True)
        thread.start()
        try:
            turn = session.run_turn(
                "write it",
                turn_timeout=30.0,
                post_tool_quiet_timeout=0.15,
                watch_poll_interval=0.02,
            )
        finally:
            thread.join(timeout=5)
            _close_promptly(session)
        assert released.is_set()  # the approval really took 0.5s
        assert turn.error is None
        assert turn.final_text == "written"

    def test_orphaned_approval_decrements_its_own_watch(self, monkeypatch):
        # The F9 shape: an approval wait that OUTLIVES its turn must
        # decrement the watch it suspended — never a later turn's. The
        # callback captures the watch object at entry; a stale decrement
        # lands on the dead watch.
        _plant_claude_agent_sdk_stand_in(monkeypatch)
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        release = threading.Event()

        def blocking_cb(preview, prompt, **kwargs):
            release.wait(5)
            return "once"

        session, holder = _make_hold_open_session(
            script=[], approval_callback=blocking_cb,
            permission_mode="default",
        )
        try:
            session.ensure_started()
            w1 = session_mod._TurnWatch()
            session._turn_watch = w1
            cb = session._make_can_use_tool()
            fut = asyncio.run_coroutine_threadsafe(
                cb("Bash", {"command": "true"}, SimpleNamespace(tool_use_id="t1")),
                session._loop,
            )
            deadline = time.monotonic() + 5
            while w1.approvals_pending != 1:
                if time.monotonic() > deadline:
                    raise AssertionError("approval never registered on w1")
                time.sleep(0.01)
            # Turn 1 ends; turn 2 installs a fresh watch while the approval
            # is still pending.
            w2 = session_mod._TurnWatch()
            session._turn_watch = w2
            release.set()
            fut.result(timeout=5)
            assert w1.approvals_pending == 0  # its OWN watch decremented
            assert w2.approvals_pending == 0  # the new turn's never touched
        finally:
            release.set()
            _close_promptly(session)


class TestTurnLifetimeConfig:
    def _patch_block(self, monkeypatch, block):
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {"agent": {"claude_agent_sdk": block}},
            raising=False,
        )

    def test_turn_timeout_reader_validation(self, monkeypatch):
        from agent.transports.claude_agent_sdk_session_config import _configured_turn_timeout

        self._patch_block(monkeypatch, {"turn_timeout": 1500})
        assert _configured_turn_timeout() == 1500.0
        self._patch_block(monkeypatch, {"turn_timeout": "900"})
        assert _configured_turn_timeout() == 900.0
        # 0 = unlimited does NOT exist for the budget; bools are not seconds;
        # garbage and negatives fall back — all with a warning.
        for bad in (0, -5, True, False, "plenty", [600]):
            self._patch_block(monkeypatch, {"turn_timeout": bad})
            assert _configured_turn_timeout() is None
        self._patch_block(monkeypatch, {})
        assert _configured_turn_timeout() is None

    def test_post_tool_quiet_reader_validation(self, monkeypatch):
        from agent.transports.claude_agent_sdk_session_config import (
            _configured_post_tool_quiet_timeout,
        )

        self._patch_block(monkeypatch, {"post_tool_quiet_timeout": 120})
        assert _configured_post_tool_quiet_timeout() == 120.0
        # 0 = explicitly disabled IS a valid value for the quiet watchdog.
        self._patch_block(monkeypatch, {"post_tool_quiet_timeout": 0})
        assert _configured_post_tool_quiet_timeout() == 0.0
        for bad in (-1, True, "off"):
            self._patch_block(monkeypatch, {"post_tool_quiet_timeout": bad})
            assert _configured_post_tool_quiet_timeout() is None

    def test_configured_turn_timeout_reaches_the_watchdog(self, monkeypatch):
        # End-to-end: config.yaml (not the signature default) is what the
        # budget rule enforces. A 0.2s configured budget kills a silent turn
        # fast even though run_turn was called with no explicit timeout.
        self._patch_block(monkeypatch, {"turn_timeout": 0.2})
        session, holder = _make_hold_open_session(script=[])
        try:
            turn = session.run_turn(
                "hi", watch_poll_interval=0.05, abort_grace=0.2
            )
        finally:
            _close_promptly(session)
        assert turn.error is not None
        assert "turn timed out after" in turn.error

    def test_configured_quiet_reaches_the_watchdog(self, monkeypatch):
        # End-to-end for the second knob: with streaming OFF the quiet
        # watchdog defaults to disabled — a configured value must still
        # reach and arm it.
        self._patch_block(monkeypatch, {"post_tool_quiet_timeout": 0.2})
        session, holder = _make_hold_open_session(
            script=[
                AssistantMessage(
                    content=[ToolUseBlock(id="t1", name="Grep", input={})]
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="t1", content="x")]
                ),
            ],
            interrupt_ack=[_ede_interrupt_ack()],
        )
        try:
            turn = session.run_turn(
                "hi", turn_timeout=30.0, watch_poll_interval=0.05
            )
        finally:
            _close_promptly(session)
        assert turn.error is not None
        assert "after a tool result" in turn.error

    def test_turnwatch_check_semantics(self, monkeypatch):
        # Deterministic unit coverage of the verdict rules (no threads).
        # Module-LOCAL time shadow — patching stdlib time.monotonic
        # process-wide would freeze asyncio loop clocks in concurrent tests.
        from agent.transports import claude_agent_sdk_session_watchdog as session_mod

        clock = {"now": 1000.0}
        monkeypatch.setattr(
            session_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        watch = session_mod._TurnWatch()
        # Idle budget: fires once idle >= budget with nothing outstanding
        # (the old `elapsed >= budget AND idle >= 30` rule was the 2026-09-28 defect).
        clock["now"] += 599.0
        assert watch.check(budget=600.0, quiet=0.0) is None
        clock["now"] += 2.0  # elapsed 601, idle 601
        assert watch.check(budget=600.0, quiet=0.0) == "budget"
        watch.tick()  # activity: idle 0 — over budget but alive
        assert watch.check(budget=600.0, quiet=0.0) is None
        # Outstanding tool suspends everything.
        clock["now"] += 700.0
        watch.note_tools_issued(1)
        assert watch.check(budget=600.0, quiet=0.0) is None
        watch.note_tools_resolved(1)
        assert watch.check(budget=600.0, quiet=0.0) == "budget"
        # Pending approval suspends everything.
        watch.approval_begin()
        clock["now"] += 700.0
        assert watch.check(budget=600.0, quiet=0.0) is None
        watch.approval_end()  # ticks: idle resets
        assert watch.check(budget=600.0, quiet=0.0) is None
        # Post-tool quiet: armed + idle >= quiet, before the budget.
        watch.arm_post_tool()
        clock["now"] += 91.0
        assert watch.check(budget=60000.0, quiet=90.0) == "post_tool_quiet"
        # Disabled quiet (0) never fires the post-tool rule.
        assert watch.check(budget=60000.0, quiet=0.0) is None
        watch.disarm_post_tool()
        assert watch.check(budget=60000.0, quiet=90.0) is None
        # Rebaseline absorbs a process stall.
        watch.arm_post_tool()
        clock["now"] += 500.0
        watch.rebaseline()
        assert watch.check(budget=60000.0, quiet=90.0) is None


# ---------- idle-based turn lifetime (2026-09-28 hotfix) ----------
# Production 2026-09-21..27 (thinkbot agent.log): orchestrator turns retired at
# 654s..7978s while quiet only ~30s — the rule was `elapsed >= 600 AND idle >=
# min(30, budget)`, so ten minutes of wall clock plus any 30s pause killed a
# working turn. Wall clock alone must never retire a turn now: only
# turn_idle_timeout of silence with nothing outstanding does. Fake clock, no
# real sleeping. The literals 900/300 are the production defaults (pinned by
# test_production_defaults) so these tests also run, and FAIL, on the old tree.

_TWO_HOURS = 2 * 60 * 60.0


def _watch_with_fake_clock(monkeypatch):
    from agent.transports import claude_agent_sdk_session_watchdog as wd

    clock = {"now": 1000.0}
    monkeypatch.setattr(wd, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    return wd._TurnWatch(), clock


def _advance(watch, clock, seconds, *, budget, quiet, poll=5.0):
    """Advance the fake clock in poll-sized steps, checking at each — the
    run_turn poll loop's view. Returns the first non-None verdict."""
    end = clock["now"] + seconds
    while clock["now"] < end:
        clock["now"] = min(end, clock["now"] + poll)
        verdict = watch.check(budget=budget, quiet=quiet)
        if verdict is not None:
            return verdict
    return None


class TestIdleTurnLifetime:
    def test_production_defaults(self):
        from agent.transports import claude_agent_sdk_session_watchdog as wd

        assert wd._DEFAULT_TURN_IDLE_TIMEOUT == 900.0
        assert wd._DEFAULT_POST_TOOL_QUIET_STREAMING == 300.0
        # The quiet wedge rule stays an EARLY catcher, below the idle limit.
        assert wd._DEFAULT_POST_TOOL_QUIET_STREAMING < wd._DEFAULT_TURN_IDLE_TIMEOUT

    def test_two_hour_turn_with_a_tool_every_five_minutes_is_never_retired(
        self, monkeypatch
    ):
        # (a) Each 5-minute cycle: a tool runs 30s, then the model is silent
        # for 270s (worst case: no partial messages) before the next call.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        for n in range(int(_TWO_HOURS // 300)):
            tool_id = f"toolu_{n}"
            watch.tick()  # the AssistantMessage carrying the ToolUseBlock
            watch.note_tools_issued(1, ids=[tool_id])
            watch.disarm_post_tool()
            assert _advance(watch, clock, 30.0, budget=900.0, quiet=300.0) is None
            watch.tick()  # the UserMessage carrying the ToolResultBlock
            watch.note_tools_resolved(1, ids=[tool_id])
            watch.arm_post_tool()
            assert _advance(watch, clock, 270.0, budget=900.0, quiet=300.0) is None, (
                f"retired at {clock['now'] - watch.started:.0f}s of a working turn"
            )
        assert clock["now"] - watch.started >= _TWO_HOURS

    def test_two_hour_turn_streaming_a_delta_every_minute_is_never_retired(
        self, monkeypatch
    ):
        # (b) Long thinking / prose with partial messages on: one StreamEvent
        # (thinking or text delta) a minute, no tools at all.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        while clock["now"] - watch.started < _TWO_HOURS:
            assert _advance(watch, clock, 60.0, budget=900.0, quiet=300.0) is None, (
                f"retired at {clock['now'] - watch.started:.0f}s of a streaming turn"
            )
            watch.tick()  # the StreamEvent delta

    def test_silence_past_the_idle_limit_with_nothing_outstanding_retires(
        self, monkeypatch
    ):
        # (c) A genuinely idle turn IS retired — at the idle limit, not before.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        assert _advance(watch, clock, 895.0, budget=900.0, quiet=0.0) is None
        assert _advance(watch, clock, 10.0, budget=900.0, quiet=0.0) == "budget"
        # The same limit applies deep into a long turn: activity resets it.
        clock["now"] += 5000.0
        watch.tick()
        assert _advance(watch, clock, 895.0, budget=900.0, quiet=0.0) is None
        assert _advance(watch, clock, 10.0, budget=900.0, quiet=0.0) == "budget"

    def test_idle_trip_message_names_idle_seconds_and_the_limit(self):
        # (c) The retire reason names the idle time and the limit, keeps the
        # "turn timed out" needle, and fits the gateway's 200-char cut.
        session, _holder = _make_session(script=[])
        message = session._format_trip_error("budget", 900.0, 300.0, 7978.4, 912.2)
        assert message.startswith("turn timed out after 912s idle")
        assert "idle limit 900s" in message
        assert "turn ran 7978s" in message
        assert "budget" not in message
        assert len(message) <= 200
        capped = session._format_trip_error(
            "max_seconds", 900.0, 300.0, 3600.0, 4.0, 3600.0
        )
        assert "turn_max_seconds cap 3600s" in capped and len(capped) <= 200

    def test_outstanding_tool_or_live_task_suspends_the_idle_rule(self, monkeypatch):
        # (d) Hours of silence while a tool runs or a background Task lives.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        watch.note_tools_issued(1, ids=["toolu_bash"])
        assert _advance(watch, clock, _TWO_HOURS, budget=900.0, quiet=300.0) is None
        assert watch.liveness()[0] == 0.0
        watch.note_tools_resolved(1, ids=["toolu_bash"])
        watch.tick()
        watch.note_task_started("task-1")
        assert _advance(watch, clock, _TWO_HOURS, budget=900.0, quiet=300.0) is None
        watch.note_task_terminal("task-1")
        assert _advance(watch, clock, 905.0, budget=900.0, quiet=0.0) == "budget"

    def test_absolute_cap_is_opt_in(self, monkeypatch):
        # turn_max_seconds: 0 = off (a busy 2h turn survives); set, it caps
        # even an active turn.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        for _ in range(int(_TWO_HOURS // 60)):
            clock["now"] += 60.0
            watch.tick()
            assert watch.check(budget=900.0, quiet=300.0, max_seconds=0.0) is None
        assert watch.check(budget=900.0, quiet=300.0, max_seconds=3600.0) == "max_seconds"

    def test_injected_burst_messages_tick_the_turn_watch(self):
        # Any CLI output is liveness, including messages the reader routes
        # AWAY from the claimed turn: while an injected burst (task
        # notification / peer turn) is open, the reader hands its messages to
        # _handle_unsolicited, so _consume_turn's own tick never sees them.
        session, holder = _make_hold_open_session(script=[])
        routed = []
        original = session._handle_unsolicited

        def spy_unsolicited(message):
            routed.append(message)
            return original(message)

        session._handle_unsolicited = spy_unsolicited
        turn_box = {}
        runner = threading.Thread(
            target=lambda: turn_box.setdefault(
                "turn", session.run_turn("hold", turn_timeout=600.0)
            ),
            daemon=True,
        )
        try:
            runner.start()
            deadline = time.monotonic() + 5.0
            while (
                session._turn_inbox is None or session._turn_watch is None
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            watch = session._turn_watch
            assert watch is not None and session._turn_inbox is not None
            session._unsolicited_burst_open = True  # an injected turn is mid-burst
            watch.last_activity -= 10_000.0
            stale = watch.last_activity
            burst_message = SystemMessage(subtype="status", data={})
            holder["client"].feed(burst_message)
            while not routed and time.monotonic() < deadline:
                time.sleep(0.01)
            assert routed and routed[0] is burst_message  # really took the burst path
            assert watch.last_activity > stale
        finally:
            _close_promptly(session)
            runner.join(timeout=10.0)
        assert not runner.is_alive()

    def test_tool_outstanding_just_under_the_tool_cap_is_not_retired(
        self, monkeypatch
    ):
        # Review P1-1: a long tool suspends the idle rule up to the cap.
        watch, clock = _watch_with_fake_clock(monkeypatch)
        watch.note_tools_issued(1, ids=["toolu_bash"])
        assert _advance(
            watch, clock, 3 * 3600.0 + 59 * 60.0, budget=900.0, quiet=300.0
        ) is None

    def test_silent_tool_stuck_past_the_tool_cap_is_retired(self, monkeypatch):
        # Review P1-1: a tool whose result never arrives no longer holds the
        # turn forever — past the 4 h cap the idle rule applies again.
        from agent.transports import claude_agent_sdk_session_watchdog as wd

        watch, clock = _watch_with_fake_clock(monkeypatch)
        assert watch.tool_max_suspend == wd._TOOL_MAX_SUSPEND == 4 * 3600.0
        watch.note_tools_issued(1, ids=["toolu_wedged"])
        verdict = _advance(
            watch, clock, 4 * 3600.0 + 900.0 + 5.0, budget=900.0, quiet=0.0
        )
        assert verdict == "budget"
        assert watch.liveness()[0] > 900.0  # turn_liveness sees it too
        # A resolved-then-reissued tool restarts its own cap (0 -> 1 stamp).
        watch.note_tools_resolved(1, ids=["toolu_wedged"])
        watch.tick()
        watch.note_tools_issued(1, ids=["toolu_next"])
        assert _advance(watch, clock, 3600.0, budget=900.0, quiet=0.0) is None

    def test_configured_tool_cap_is_honoured_by_the_watch(self, monkeypatch):
        watch, clock = _watch_with_fake_clock(monkeypatch)
        watch.tool_max_suspend = 600.0
        watch.note_tools_issued(1, ids=["toolu_bash"])
        assert _advance(watch, clock, 595.0, budget=900.0, quiet=0.0) is None
        assert _advance(watch, clock, 400.0, budget=900.0, quiet=0.0) == "budget"


class TestIdleTurnLifetimeConfig:
    """(e) config.yaml values reach the watchdog: turn_idle_timeout, the
    deprecated turn_timeout alias, turn_max_seconds, post_tool_quiet_timeout."""

    _patch_block = TestTurnLifetimeConfig._patch_block

    def _captured_limits(self, monkeypatch, block, *, streaming=False):
        from agent.transports import claude_agent_sdk_session_watchdog as wd

        self._patch_block(monkeypatch, block)
        seen = {}

        def spy_check(watch, **kwargs):
            seen.update(
                kwargs,
                idle_limit=watch.idle_limit,
                tool_max_suspend=watch.tool_max_suspend,
            )
            return "budget"  # trip at once: no real waiting on the limits

        monkeypatch.setattr(wd._TurnWatch, "check", spy_check)
        session, _holder = _make_hold_open_session(
            script=[], interrupt_ack=[_ede_interrupt_ack()]
        )
        session._streaming = streaming
        try:
            turn = session.run_turn("hi", watch_poll_interval=0.01, abort_grace=0.5)
        finally:
            _close_promptly(session)
        return seen, turn

    def test_turn_idle_timeout_and_max_seconds_are_honoured(self, monkeypatch):
        seen, turn = self._captured_limits(
            monkeypatch,
            {
                "turn_idle_timeout": 1234,
                "turn_max_seconds": 7200,
                "turn_tool_max_suspend": 5400,
            },
        )
        assert seen["tool_max_suspend"] == 5400.0
        assert seen["budget"] == 1234.0 and seen["idle_limit"] == 1234.0
        assert seen["max_seconds"] == 7200.0
        assert "idle limit 1234s" in turn.error

    def test_idle_timeout_wins_over_the_deprecated_alias(self, monkeypatch):
        seen, _turn = self._captured_limits(
            monkeypatch, {"turn_idle_timeout": 1800, "turn_timeout": 600}
        )
        assert seen["budget"] == 1800.0

    def test_deprecated_turn_timeout_maps_to_the_idle_limit(self, monkeypatch):
        seen, turn = self._captured_limits(monkeypatch, {"turn_timeout": 1500})
        assert seen["budget"] == 1500.0
        assert "idle limit 1500s" in turn.error

    def test_defaults_without_config(self, monkeypatch):
        seen, _turn = self._captured_limits(monkeypatch, {}, streaming=True)
        assert seen["budget"] == 900.0
        assert seen["max_seconds"] == 0.0
        assert seen["quiet"] == 300.0
        assert seen["tool_max_suspend"] == 4 * 3600.0

    def test_configured_quiet_is_honoured(self, monkeypatch):
        seen, _turn = self._captured_limits(
            monkeypatch, {"post_tool_quiet_timeout": 45}, streaming=True
        )
        assert seen["quiet"] == 45.0

    def test_new_key_reader_validation(self, monkeypatch):
        from agent.transports.claude_agent_sdk_session_config import (
            _configured_turn_idle_timeout,
            _configured_turn_max_seconds,
        )

        self._patch_block(monkeypatch, {"turn_idle_timeout": "1200"})
        assert _configured_turn_idle_timeout() == 1200.0
        for bad in (0, -5, True, "plenty"):
            self._patch_block(monkeypatch, {"turn_idle_timeout": bad})
            assert _configured_turn_idle_timeout() is None
        self._patch_block(monkeypatch, {"turn_max_seconds": 0})
        assert _configured_turn_max_seconds() == 0.0
        self._patch_block(monkeypatch, {"turn_max_seconds": 14400})
        assert _configured_turn_max_seconds() == 14400.0
        for bad in (-1, True, "forever"):
            self._patch_block(monkeypatch, {"turn_max_seconds": bad})
            assert _configured_turn_max_seconds() is None

    def test_non_finite_values_are_rejected(self, monkeypatch):
        # Review P2: NaN passes every range check and would silently disable
        # the rule it feeds (`idle >= nan` is never true); Inf likewise.
        from agent.transports import claude_agent_sdk_session_config as cfg_mod

        readers = (
            ("turn_idle_timeout", cfg_mod._configured_turn_idle_timeout),
            ("turn_timeout", cfg_mod._configured_turn_timeout),
            ("turn_max_seconds", cfg_mod._configured_turn_max_seconds),
            ("turn_tool_max_suspend", cfg_mod._configured_turn_tool_max_suspend),
            ("post_tool_quiet_timeout", cfg_mod._configured_post_tool_quiet_timeout),
        )
        for key, reader in readers:
            for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
                self._patch_block(monkeypatch, {key: bad})
                assert reader() is None, (key, bad)
        self._patch_block(monkeypatch, {"turn_tool_max_suspend": 7200})
        assert cfg_mod._configured_turn_tool_max_suspend() == 7200.0
        self._patch_block(monkeypatch, {"turn_tool_max_suspend": 0})
        assert cfg_mod._configured_turn_tool_max_suspend() is None
