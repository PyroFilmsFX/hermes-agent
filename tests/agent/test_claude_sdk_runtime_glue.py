"""Runtime glue and orchestration — claude-agent-sdk runtime tests (#25267).

Split from ``tests/agent/test_claude_sdk_runtime.py``; the SDK message
stand-ins, fake clients and shared builders live in
``tests.agent.claude_sdk_fakes``.
"""

from pathlib import Path
from unittest.mock import MagicMock

import logging

import pytest

from agent.claude_sdk_runtime import run_claude_agent_sdk_turn
from agent.turn_runtime_handoff import RuntimeHandoffState, run_whole_turn_runtime
from tests.agent.claude_sdk_fakes import (
    TextBlock,
    AssistantMessage,
    ResultMessage,
    _make_session,
    _make_turn,
    _make_agent,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    """Provider config, gateway contextvars and the CLI approval callback are
    reset around every test in this module — carried explicitly, never hoisted
    to a conftest (see ``isolate_provider_config``)."""
    # These cases pin the legacy #31/U8.2 prompt-replay path, which D62 keeps as an
    # opt-in last resort (transient_retry_replay: true) with continuing disabled.
    # The continue path itself is covered by test_claude_sdk_continue.py.
    import agent.claude_sdk_runtime_session as runtime_session

    import agent.claude_sdk_usage_park as usage_park

    monkeypatch.setattr(
        runtime_session, "_configured_continue_policy", lambda: (False, 0, (0.0,), True)
    )
    monkeypatch.setattr(usage_park, "configured_policy", lambda: (False, 90.0))
    yield from isolate_provider_config(monkeypatch)


# ---------- runtime glue ----------

class TestRuntimeGlue:
    @staticmethod
    def _state():
        from agent.claude_sdk_runtime_state import _SdkTurnState

        messages = [{"role": "user", "content": "hello"}]
        return _SdkTurnState(
            user_input="hello", original_user_message="hello",
            messages=messages, messages_before_attempt=list(messages),
        )

    @staticmethod
    def _scripted_sessions(monkeypatch, agent, turns):
        import time
        import agent.claude_sdk_runtime_session as session_mod

        sessions = []
        turn_queue = iter(turns)

        def create_session(agent_, **_kwargs):
            turn = next(turn_queue)
            session = MagicMock()
            session._cwd = "/tmp"
            session.run_turn.return_value = turn
            sessions.append(session)
            agent_._claude_sdk_session = session
            return session

        monkeypatch.setattr(session_mod, "_persisted_sdk_session_id", lambda _agent: None)
        monkeypatch.setattr(session_mod, "_store_sdk_session_id", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(session_mod, "_create_session", create_session)
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        agent._claude_sdk_session = None
        agent._current_streamed_assistant_text = ""
        agent._sdk_issued_tool_effect = False
        return session_mod, sessions

    def test_retries_replay_safe_transient_failure_once(self, monkeypatch):
        from types import SimpleNamespace

        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=False, error="Claude API error (server_error): HTTP 503",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=503, api_error_kind="server_error",
            api_retries=None, rate_limit_rejected=None,
        )
        success = SimpleNamespace(
            interrupted=False, error=None, thread_id="sdk-session-2",
            turn_id="turn-2", projected_messages=[], tool_iterations=0,
            final_text="answer", should_retire=False,
        )
        _mod, sessions = self._scripted_sessions(monkeypatch, agent, [failure, success])
        state = self._state()

        result = _mod._run_sdk_attempts(agent, state)

        assert result is None
        assert state.turn.final_text == "answer"
        assert len(sessions) == 2
        assert [m["role"] for m in state.messages].count("user") == 1
        agent._emit_status.assert_called_once_with("Retrying (1/2)…")

    def test_permanent_failure_is_not_retried(self, monkeypatch):
        from types import SimpleNamespace

        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=False, error="SDK result error: invalid request",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=400, api_error_kind="invalid_request",
            api_retries=None, rate_limit_rejected=None,
        )
        mod, sessions = self._scripted_sessions(monkeypatch, agent, [failure])

        mod._run_sdk_attempts(agent, self._state())

        assert len(sessions) == 1

    @pytest.mark.parametrize("effect", ["tool", "stream"])
    def test_transient_failure_after_visible_effect_is_not_retried(self, monkeypatch, effect):
        from types import SimpleNamespace

        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=False, error="Claude API error (server_error): HTTP 503",
            thread_id="sdk-session-1", turn_id="turn-1",
            projected_messages=[], tool_iterations=int(effect == "tool"),
            final_text="", should_retire=True, api_error_status=503,
            api_error_kind="server_error", api_retries=None,
            rate_limit_rejected=None,
        )
        mod, sessions = self._scripted_sessions(monkeypatch, agent, [failure])
        if effect == "stream":
            agent._current_streamed_assistant_text = "already shown"

        mod._run_sdk_attempts(agent, self._state())

        assert len(sessions) == 1

    def test_interrupt_during_transient_failure_is_not_retried(self, monkeypatch):
        from types import SimpleNamespace

        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=True, error="Claude API error (server_error): HTTP 503",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=503, api_error_kind="server_error",
            api_retries=None, rate_limit_rejected=None,
        )
        mod, sessions = self._scripted_sessions(monkeypatch, agent, [failure])

        mod._run_sdk_attempts(agent, self._state())

        assert len(sessions) == 1

    def test_retry_after_hint_is_honoured_and_capped(self, monkeypatch):
        import time
        from types import SimpleNamespace

        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg, "load_config_readonly",
            lambda *args, **kwargs: {"agent": {"claude_agent_sdk": {
                "transient_retry_max_retries": 1,
                "transient_retry_max_wait_seconds": 3,
            }}},
            raising=False,
        )
        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=False, error="Claude API error (rate_limit): HTTP 429",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=429, api_error_kind="rate_limit",
            api_retries=None, rate_limit_rejected={"resets_at": time.time() + 100},
        )
        success = SimpleNamespace(
            interrupted=False, error=None, thread_id="sdk-session-2",
            turn_id="turn-2", projected_messages=[], tool_iterations=0,
            final_text="answer", should_retire=False,
        )
        mod, _sessions = self._scripted_sessions(monkeypatch, agent, [failure, success])
        waits = []
        import time
        import threading
        _me = threading.current_thread()
        # time.sleep is process-global: record only this test's thread, not stray background pollers.
        monkeypatch.setattr(time, "sleep", lambda s: waits.append(s) if threading.current_thread() is _me else None)

        mod._run_sdk_attempts(agent, self._state())

        assert waits == [3]

    def test_exhausted_retries_keep_original_transient_error(self, monkeypatch):
        from types import SimpleNamespace

        agent = _make_agent()
        failure = SimpleNamespace(
            interrupted=False, error="Claude API error (server_error): HTTP 503",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=503, api_error_kind="server_error",
            api_retries=None, rate_limit_rejected=None,
        )
        mod, sessions = self._scripted_sessions(monkeypatch, agent, [failure, failure, failure])
        state = self._state()

        result = mod._run_sdk_attempts(agent, state)

        assert len(sessions) == 3
        assert result is None
        assert state.turn.error == "Claude API error (server_error): HTTP 503"
        from agent.claude_sdk_transient import classify_sdk_api_failure
        assert classify_sdk_api_failure({
            "api_error_status": state.turn.api_error_status,
            "api_error_kind": state.turn.api_error_kind,
        })[:2] == ("transient", "server_error")

    # ---- ONE replay budget per user turn: #31 transient retry x U8.2 replay ----

    @staticmethod
    def _transient_503():
        from types import SimpleNamespace

        return SimpleNamespace(
            interrupted=False, error="Claude API error (server_error): HTTP 503",
            thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            api_error_status=503, api_error_kind="server_error",
            api_retries=None, rate_limit_rejected=None,
        )

    @staticmethod
    def _post_query_death():
        from types import SimpleNamespace

        return SimpleNamespace(
            interrupted=False,
            error="SDK message stream ended before this turn's result",
            thread_id="sdk-session-1", turn_id=None, projected_messages=[],
            tool_iterations=0, final_text="", should_retire=True,
            stream_ended=True, api_call_made=True, fatal_reason=None,
        )

    @staticmethod
    def _answer():
        from types import SimpleNamespace

        return SimpleNamespace(
            interrupted=False, error=None, thread_id="sdk-session-1",
            turn_id="turn-2", projected_messages=[], tool_iterations=0,
            final_text="answer", should_retire=False,
        )

    def test_post_query_death_during_transient_retry_is_not_replayed(self, monkeypatch):
        agent = _make_agent()
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent,
            [self._transient_503(), self._post_query_death(), self._answer()],
        )
        state = self._state()

        result = mod._run_sdk_attempts(agent, state)

        assert result is None
        assert len(sessions) == 2, "the transient retry spent the budget"
        assert "session is intact" in state.turn.error
        assert "next message continues it" in state.turn.error
        assert "retry of a transient API error" in state.turn.error
        agent._emit_status.assert_called_once_with("Retrying (1/2)…")

    def test_transient_failure_during_post_query_replay_is_not_retried(self, monkeypatch):
        agent = _make_agent()
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent,
            [self._post_query_death(), self._transient_503(), self._answer()],
        )
        state = self._state()

        result = mod._run_sdk_attempts(agent, state)

        assert result is None
        assert len(sessions) == 2, "the post-query-death replay spent the budget"
        assert state.turn.error == "Claude API error (server_error): HTTP 503"
        agent._emit_status.assert_not_called()

    @pytest.mark.parametrize("max_retries", [0, 1, 2, 3])
    def test_replay_budget_is_shared_and_bounded(self, monkeypatch, max_retries):
        """Re-sends of an attempt that reached the API total at most
        max(1, N): N transient retries OR one post-query-death replay."""
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg, "load_config_readonly",
            lambda *args, **kwargs: {"agent": {"claude_agent_sdk": {
                "transient_retry_max_retries": max_retries,
            }}},
            raising=False,
        )
        # Every transient retry it allows is spent, then the CLI dies.
        agent = _make_agent()
        script = [self._transient_503() for _ in range(max_retries)]
        script += [self._post_query_death(), self._answer(), self._answer()]
        mod, sessions = self._scripted_sessions(monkeypatch, agent, script)
        state = self._state()
        mod._run_sdk_attempts(agent, state)
        if max_retries:
            assert len(sessions) == max_retries + 1
            assert "session is intact" in state.turn.error
        else:
            # N = 0 still leaves the one post-query-death replay.
            assert len(sessions) == 2
            assert state.turn.final_text == "answer"

        # Every re-send spent on a death, then transient failures forever.
        agent = _make_agent()
        script = [self._post_query_death()]
        script += [self._transient_503() for _ in range(max_retries + 2)]
        mod, sessions = self._scripted_sessions(monkeypatch, agent, script)
        state = self._state()
        mod._run_sdk_attempts(agent, state)
        assert len(sessions) == 2
        assert len(sessions) - 1 <= max(1, max_retries)

    # ---- review P1-3: a re-sent attempt's spend stays in the turn ----

    @staticmethod
    def _with_spend(turn, *, input_tokens, output_tokens, cost):
        turn.token_usage_last = {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }
        turn.token_usage_total = dict(turn.token_usage_last)
        turn.total_cost_usd = cost
        turn.billing_mode = "sdk_reported_metered"
        turn.api_call_made = True
        return turn

    @pytest.mark.parametrize("first", ["transient", "post_query_death"])
    def test_resent_attempt_spend_is_accumulated(self, monkeypatch, first):
        from agent.claude_sdk_runtime_usage import _claude_sdk_billing_accounting

        agent = _make_agent()
        failed = (
            self._transient_503() if first == "transient"
            else self._post_query_death()
        )
        failed = self._with_spend(failed, input_tokens=100, output_tokens=20, cost=0.5)
        answer = self._with_spend(
            self._answer(), input_tokens=50, output_tokens=10, cost=0.25
        )
        answer.token_usage_last["iterations"] = [{"input_tokens": 50}]
        mod, sessions = self._scripted_sessions(monkeypatch, agent, [failed, answer])
        state = self._state()

        mod._run_sdk_attempts(agent, state)

        assert len(sessions) == 2
        usage = state.turn.token_usage_last
        assert usage["input_tokens"] == 150
        assert usage["output_tokens"] == 30
        # Context pressure stays the final attempt's own last iteration.
        assert usage["iterations"] == [{"input_tokens": 50}]
        assert state.turn.total_cost_usd == pytest.approx(0.75)
        assert _claude_sdk_billing_accounting(state.turn)[3] == pytest.approx(0.75)

    @pytest.mark.parametrize("first", ["transient", "post_query_death"])
    def test_no_resend_when_turn_budget_would_be_exceeded(self, monkeypatch, first):
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg, "load_config_readonly",
            lambda *args, **kwargs: {"agent": {"claude_agent_sdk": {
                "max_budget_usd": 1.0,
            }}},
            raising=False,
        )
        agent = _make_agent()
        failed = (
            self._transient_503() if first == "transient"
            else self._post_query_death()
        )
        # Spent 0.6 of a 1.0 cap: a replay costing the same would exceed it.
        failed = self._with_spend(failed, input_tokens=100, output_tokens=20, cost=0.6)
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent, [failed, self._answer()]
        )
        state = self._state()

        mod._run_sdk_attempts(agent, state)

        assert len(sessions) == 1
        if first == "post_query_death":
            assert "session is intact" in state.turn.error
            assert "max_budget_usd" in state.turn.error
        else:
            assert state.turn.error == "Claude API error (server_error): HTTP 503"

    def test_resend_within_turn_budget_still_runs(self, monkeypatch):
        import hermes_cli.config as cfg

        monkeypatch.setattr(
            cfg, "load_config_readonly",
            lambda *args, **kwargs: {"agent": {"claude_agent_sdk": {
                "max_budget_usd": 1.0,
            }}},
            raising=False,
        )
        agent = _make_agent()
        failed = self._with_spend(
            self._transient_503(), input_tokens=100, output_tokens=20, cost=0.3
        )
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent, [failed, self._answer()]
        )
        state = self._state()

        mod._run_sdk_attempts(agent, state)

        assert len(sessions) == 2
        assert state.turn.final_text == "answer"

    def test_prior_spend_is_recorded_when_the_retry_raises(self, monkeypatch):
        agent = _make_agent()
        failed = self._with_spend(
            self._transient_503(), input_tokens=100, output_tokens=20, cost=0.5
        )
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent, [failed, self._answer()]
        )
        recorded = []
        monkeypatch.setattr(
            mod, "_record_claude_sdk_usage",
            lambda _agent, turn: recorded.append(
                (dict(turn.token_usage_last), turn.total_cost_usd)
            ) or {},
        )
        real_create = mod._create_session

        def create_then_raise(agent_, **kwargs):
            session = real_create(agent_, **kwargs)
            if len(sessions) == 2:
                session.run_turn.side_effect = RuntimeError("CLI crashed")
            return session

        monkeypatch.setattr(mod, "_create_session", create_then_raise)

        result = mod._run_sdk_attempts(agent, self._state())

        assert result is not None and result["failed"] is True
        assert recorded == [({"input_tokens": 100, "output_tokens": 20}, 0.5)]

    # ---- review P2: delivered interim prose (streaming off) is visible ----

    @pytest.mark.parametrize("first", ["transient", "post_query_death"])
    def test_delivered_interim_prose_blocks_resend(self, monkeypatch, first):
        agent = _make_agent()
        failed = (
            self._transient_503() if first == "transient"
            else self._post_query_death()
        )
        mod, sessions = self._scripted_sessions(
            monkeypatch, agent, [failed, self._answer()]
        )
        # Streaming off: no delta text, but the interim relay showed prose.
        agent._sdk_interim_delivered = True

        mod._run_sdk_attempts(agent, self._state())

        assert len(sessions) == 1

    def test_interim_relay_marks_prose_as_delivered(self):
        from agent.claude_sdk_runtime_session import _TurnVisibility

        agent = _make_agent()
        agent._current_turn_id = "turn-1"
        agent._sdk_visibility_lock = None
        agent._sdk_interim_delivered = False
        agent._strip_think_blocks = lambda text: text
        agent._interim_text_was_delivered = lambda _text: False
        agent._interim_content_was_streamed = lambda _text: False
        agent.interim_assistant_callback = lambda *_a, **_k: None
        visibility = _TurnVisibility(agent)

        visibility.relay_interim_assistant("Looking into it now.")

        assert agent._sdk_interim_delivered is True

    def test_stream_ended_pre_query_retries_once_but_post_query_never_retries(self, monkeypatch):
        from types import SimpleNamespace

        import agent.claude_sdk_runtime_session as session_mod
        from agent.claude_sdk_runtime_state import _SdkTurnState

        def run_with_api_call(api_call_made):
            agent = _make_agent()
            agent._claude_sdk_session = None
            created = []

            def create_session(agent_, **_kwargs):
                turn = SimpleNamespace(
                    interrupted=False, error="SDK message stream ended before this turn",
                    thread_id="sdk-session-1", turn_id="turn-1", projected_messages=[],
                    tool_iterations=0, final_text="", should_retire=True, stream_ended=True,
                    api_call_made=api_call_made,
                )
                session = SimpleNamespace(_cwd="/tmp", run_turn=lambda **_kw: turn, close=lambda: None)
                created.append(session)
                agent_._claude_sdk_session = session
                return session

            monkeypatch.setattr(session_mod, "_persisted_sdk_session_id", lambda _agent: None)
            monkeypatch.setattr(session_mod, "_create_session", create_session)
            state = _SdkTurnState(
                user_input="hello", original_user_message="hello",
                messages=[{"role": "user", "content": "hello"}], messages_before_attempt=[],
            )
            failure = session_mod._run_sdk_attempts(agent, state)
            return created, failure

        before_query, failure = run_with_api_call(False)
        assert len(before_query) == 2
        assert failure is None

        after_query, failure = run_with_api_call(True)
        assert len(after_query) == 1
        assert failure is None

    def test_turn_result_carries_last_reasoning(self):
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            projected_messages=[
                {"role": "assistant", "content": None, "reasoning": "first thought"},
                {"role": "assistant", "content": "answer", "reasoning": "final thought"},
            ]
        )
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        assert result["last_reasoning"] == "final thought"

    def test_turn_result_reports_sdk_iteration_count(self):
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            num_turns=4,
        )
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        assert result["iteration_count"] == 4

    def test_real_session_result_num_turns_reaches_runtime_accounting(self):
        session, _ = _make_session(script=[ResultMessage(result="ok", num_turns=3)])
        session._cwd = str(Path.cwd())
        agent = _make_agent()
        agent._claude_sdk_session = session
        try:
            result = run_claude_agent_sdk_turn(
                agent,
                user_message="hi",
                original_user_message="hi",
                messages=[{"role": "user", "content": "hi"}],
                effective_task_id="task-1",
            )
        finally:
            session.close()

        assert result["iteration_count"] == 3, (
            "accepted ResultMessage.num_turns must reach runtime accounting"
        )

    def test_whole_turn_budget_consumes_sdk_iterations(self):
        agent = _make_agent()
        agent.api_mode = "claude_agent_sdk"
        agent.max_iterations = 3
        agent._run_claude_agent_sdk_turn.return_value = {
            "api_calls": 1,
            "num_turns": 4,
            "failed": False,
            "interrupted": False,
        }
        agent.iteration_budget = MagicMock()
        agent.iteration_budget.remaining = 0
        verdict = run_whole_turn_runtime(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
            _should_review_memory=False,
            active_system_prompt="system",
            api_call_count=0,
            _runtime_handoff=RuntimeHandoffState(),
        )
        assert verdict.action == "return"
        assert verdict.api_call_count == 4
        assert agent.iteration_budget.consume.call_count == 4

    def test_reasoning_progress_uses_native_event_shape(self):
        from agent.claude_sdk_runtime_session import _on_tool_started

        agent = _make_agent()
        progress = []
        agent.tool_progress_callback = lambda *args: progress.append(args)
        _on_tool_started(agent, "reasoning.available", "thinking text", {})
        assert progress == [("reasoning.available", "_thinking", "thinking text", None)]

    def test_turn_contract(self):
        agent = _make_agent()
        messages = [{"role": "user", "content": "hi"}]
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["final_response"] == "SDK_ASSISTANT"
        assert result["completed"] is True
        assert result["agent_persisted"] is True
        assert result["cost_status"] == "included"
        assert result["cost_source"] == "claude-subscription"
        # Projected messages spliced after the (pre-appended) user turn.
        assert messages[-1]["content"] == "SDK_ASSISTANT"
        # Skill-nudge counter parity with the codex path.
        assert agent._iters_since_skill == 2

    def test_terminal_commit_consumes_late_agent_interrupt_without_retiring(self):
        agent = _make_agent()
        session = agent._claude_sdk_session

        def completed_then_stopped(*_args, **_kwargs):
            agent._interrupt_requested = True
            return _make_turn(terminal_result_accepted=True)

        session.run_turn.side_effect = completed_then_stopped
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert result["completed"] is True
        assert result["partial"] is False
        assert result["interrupted"] is False
        assert result["sdk_effects"]["interrupted"] is False
        assert agent._interrupt_requested is False
        assert agent._claude_sdk_session is session
        session.consume_interrupt.assert_called_once_with()
        session.close.assert_not_called()

    def test_terminal_error_with_late_stop_stays_interrupted_and_cannot_fail_over(self):
        agent = _make_agent()
        session = agent._claude_sdk_session

        def failed_then_stopped(*_args, **_kwargs):
            agent._interrupt_requested = True
            return _make_turn(
                terminal_result_accepted=True,
                error="SDK result error (subtype=error): rate limit",
                api_error_status=429,
                final_text="",
                projected_messages=[],
            )

        session.run_turn.side_effect = failed_then_stopped
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert result["interrupted"] is True
        assert result["failed"] is False
        assert result.get("failover_reason") is None
        assert result["sdk_effects"]["interrupted"] is True
        assert agent._interrupt_requested is False
        assert agent._claude_sdk_session is None
        session.close.assert_called_once_with()

    def test_nonterminal_retire_with_stop_consumes_agent_interrupt(self):
        agent = _make_agent()
        session = agent._claude_sdk_session

        def retired_then_stopped(*_args, **_kwargs):
            agent._interrupt_requested = True
            return _make_turn(
                should_retire=True,
                error="SDK message stream ended before this turn's result",
                projected_messages=[],
                final_text="",
                token_usage_last=None,
            )

        session.run_turn.side_effect = retired_then_stopped
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert result["interrupted"] is True
        assert result["failed"] is False
        assert agent._interrupt_requested is False

    def test_raising_turn_with_stop_consumes_agent_interrupt(self):
        agent = _make_agent()
        session = agent._claude_sdk_session

        def raised_then_stopped(*_args, **_kwargs):
            agent._interrupt_requested = True
            raise RuntimeError("SDK transport exploded")

        session.run_turn.side_effect = raised_then_stopped
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert result["interrupted"] is True
        assert result["failed"] is False
        assert agent._interrupt_requested is False

    def test_compact_boundary_completes_once_before_turn_end(self, monkeypatch):
        """The stream boundary is primary; terminal completion is fallback only."""
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        callbacks = {}

        class SpySession:
            def __init__(self, **kwargs):
                callbacks.update(kwargs)

            def context_usage(self):
                return None

            def set_turn_visibility_callbacks(self, **kwargs):
                pass

            def run_turn(self, user_input):
                callbacks["on_compaction"]("auto")
                callbacks["on_compact_boundary"]("auto")
                return _make_turn()

            def close(self):
                pass

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        emitted = []
        agent._emit_status = emitted.append
        agent.status_callback = lambda kind, text: emitted.append((kind, text))

        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert agent._sdk_compaction_pending is False
        completions = [item for item in emitted if isinstance(item, tuple)]
        assert len(completions) == 1
        assert completions[0][0] == "compaction"
        assert "compaction complete" in completions[0][1].lower()

    def test_empty_rich_input_rejects_before_digest_or_session_creation(self):
        agent = _make_agent()
        agent._claude_sdk_session = None
        result = run_claude_agent_sdk_turn(
            agent,
            user_message=[],
            original_user_message=[],
            messages=[
                {"role": "assistant", "content": "prior answer"},
                {"role": "user", "content": []},
            ],
            effective_task_id="task-1",
        )
        assert result["api_calls"] == 0
        assert result["partial"] is True
        assert result["interrupted"] is False
        assert agent._claude_sdk_session is None

    def test_empty_rejection_consumes_interrupts_and_next_turn_runs(self):
        agent = _make_agent()
        session = agent._claude_sdk_session
        agent._interrupt_requested = True
        rejected = run_claude_agent_sdk_turn(
            agent,
            user_message="   ",
            original_user_message="   ",
            messages=[{"role": "user", "content": "   "}],
            effective_task_id="task-1",
        )
        assert rejected["api_calls"] == 0
        assert agent._interrupt_requested is False
        session.consume_interrupt.assert_called_once()
        session.run_turn.assert_not_called()

        accepted = run_claude_agent_sdk_turn(
            agent,
            user_message="continue",
            original_user_message="continue",
            messages=[{"role": "user", "content": "continue"}],
            effective_task_id="task-2",
        )
        assert accepted["api_calls"] == 1
        session.run_turn.assert_called_once_with(user_input="continue")

    def test_native_image_with_continuity_digest_preserves_blocks(
        self, monkeypatch
    ):
        import agent.transports.claude_agent_sdk_session as sdk_session_mod

        seen = {}

        class SpySession:
            def __init__(self, **kwargs):
                pass

            def run_turn(self, user_input):
                seen["input"] = user_input
                return _make_turn()

        monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", SpySession)
        agent = _make_agent()
        agent._claude_sdk_session = None
        image = {
            "type": "image_url",
            "image_url": {"url": "https://example.test/photo.png"},
        }
        run_claude_agent_sdk_turn(
            agent,
            user_message=[image],
            original_user_message=[image],
            messages=[
                {"role": "assistant", "content": "prior answer"},
                {"role": "user", "content": [image]},
            ],
            effective_task_id="task-1",
        )
        assert seen["input"][0]["type"] == "text"
        assert "continuity" in seen["input"][0]["text"].lower()
        assert seen["input"][1] == {
            "type": "image",
            "source": {
                "type": "url",
                "url": "https://example.test/photo.png",
            },
        }

    def test_transport_local_result_is_visible_without_usage_count(self):
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            api_call_made=False,
            error="local rejection",
            final_text="local rejection",
            projected_messages=[],
            token_usage_last=None,
        )
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="non-empty local input",
            original_user_message="non-empty local input",
            messages=[{"role": "user", "content": "non-empty local input"}],
            effective_task_id="task-1",
        )
        assert result["final_response"] == "local rejection"
        assert result["api_calls"] == 0
        assert result["partial"] is True
        assert agent.session_api_calls == 0

    def test_retire_closes_session(self):
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            should_retire=True, error="turn timed out after 600s",
            projected_messages=[], final_text="", token_usage_last=None,
        )
        stale = agent._claude_sdk_session
        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )
        stale.close.assert_called_once()
        assert agent._claude_sdk_session is None
        assert result["partial"] is True

    def test_interrupted_retire_does_not_emit_child_exited(self, monkeypatch):
        import agent.claude_sdk_runtime_session as session_mod

        events = []
        sink = MagicMock()
        sink.lifecycle.side_effect = lambda event, **kwargs: events.append(event)
        monkeypatch.setattr(session_mod, "_background_result_sink", lambda _agent: sink)

        agent = _make_agent()
        session = agent._claude_sdk_session
        session._child_exit_emission_lock = None
        session._child_exited_emitted = False
        agent._interrupt_requested = True
        session.run_turn.return_value = _make_turn(
            interrupted=True,
            should_retire=True,
            error="user stopped",
            projected_messages=[],
            final_text="",
        )

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert result["interrupted"] is True
        assert events == []

    def test_noninterrupted_retire_emits_one_child_exited(self, monkeypatch):
        import agent.claude_sdk_runtime_session as session_mod

        events = []
        sink = MagicMock()
        sink.lifecycle.side_effect = lambda event, **kwargs: events.append(event)
        monkeypatch.setattr(session_mod, "_background_result_sink", lambda _agent: sink)

        agent = _make_agent()
        session = agent._claude_sdk_session
        session._child_exit_emission_lock = None
        session._child_exited_emitted = False
        session.run_turn.return_value = _make_turn(
            should_retire=True,
            error="stream ended",
            projected_messages=[],
            final_text="",
        )

        run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert events == ["child_exited"]

    def test_resumed_lifecycle_requires_dead_turn_recovery(self, monkeypatch):
        import agent.claude_sdk_runtime_session as session_mod

        events = []
        sink = MagicMock()
        sink.lifecycle.side_effect = lambda event, **kwargs: events.append(event)
        monkeypatch.setattr(session_mod, "_background_result_sink", lambda _agent: sink)
        monkeypatch.setattr(session_mod, "_persisted_sdk_session_id", lambda _agent: None)
        monkeypatch.setattr(session_mod, "_render_continuity_digest", lambda _messages: "prior context")

        agent = _make_agent()
        agent._claude_sdk_session = None
        created = MagicMock()
        created._cwd = "/tmp"
        created.run_turn.return_value = _make_turn()

        def create_session(agent_, **_kwargs):
            agent_._claude_sdk_session = created
            return created

        monkeypatch.setattr(session_mod, "_create_session", create_session)
        run_claude_agent_sdk_turn(
            agent,
            user_message="new question",
            original_user_message="new question",
            messages=[
                {"role": "user", "content": "prior question"},
                {"role": "user", "content": "new question"},
            ],
            effective_task_id="task-1",
        )

        assert "resumed" not in events


# ---------- background review spawns only when routed off this runtime ----------


class TestBackgroundReviewRouting:
    @staticmethod
    def _route(monkeypatch, routed):
        import agent.background_review as br

        monkeypatch.setattr(
            br, "_resolve_review_runtime", lambda _agent: {"routed": routed}
        )

    def _run(self, agent, *, want_memory=False):
        return run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
            should_review_memory=want_memory,
        )

    def test_unrouted_memory_nudge_does_not_spawn(self, monkeypatch):
        self._route(monkeypatch, False)
        agent = _make_agent()
        self._run(agent, want_memory=True)
        agent._spawn_background_review.assert_not_called()

    def test_unrouted_skip_warns_once_per_process(self, monkeypatch, caplog):
        # An unrouted review means auto-capture is silently dead; say so
        # loudly once, then stay quiet. (cntrl carry)
        import agent.claude_sdk_runtime as rt

        self._route(monkeypatch, False)
        monkeypatch.setattr(rt, "_UNROUTED_REVIEW_WARNED", False)
        with caplog.at_level(logging.DEBUG, logger="agent.claude_sdk_runtime"):
            for _ in range(2):
                agent = _make_agent()
                self._run(agent, want_memory=True)
                agent._spawn_background_review.assert_not_called()
        skipped = [r for r in caplog.records if "background review skipped" in r.getMessage()]
        assert [r.levelno for r in skipped] == [logging.WARNING, logging.DEBUG]

    def test_unrouted_skill_nudge_does_not_spawn_but_counter_still_ticks(
        self, monkeypatch
    ):
        self._route(monkeypatch, False)
        agent = _make_agent()
        agent._skill_nudge_interval = 1
        agent.valid_tool_names = set()
        self._run(agent)
        agent._spawn_background_review.assert_not_called()
        assert agent._iters_since_skill == 0

    def test_routed_memory_nudge_spawns_the_review(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        self._run(agent, want_memory=True)
        agent._spawn_background_review.assert_called_once()
        assert agent._spawn_background_review.call_args.kwargs["review_memory"]

    def test_routed_skill_nudge_spawns_the_review(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        agent._skill_nudge_interval = 1
        agent.valid_tool_names = set()
        self._run(agent)
        agent._spawn_background_review.assert_called_once()
        assert agent._spawn_background_review.call_args.kwargs["review_skills"]

    def test_skill_review_does_not_need_foreground_skill_manage(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        agent._skill_nudge_interval = 1
        agent.valid_tool_names = set()
        self._run(agent)
        agent._spawn_background_review.assert_called_once()
        assert agent._spawn_background_review.call_args.kwargs["review_skills"]

    def test_skip_background_review_blocks_routed_skill_review(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        agent.skip_background_review = True
        agent._skill_nudge_interval = 1
        agent.valid_tool_names = set()
        self._run(agent)
        agent._spawn_background_review.assert_not_called()

    def test_dead_turn_never_spawns_even_when_routed(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        agent._claude_sdk_session.run_turn.return_value = _make_turn(
            interrupted=True, final_text=""
        )
        self._run(agent, want_memory=True)
        agent._spawn_background_review.assert_not_called()

    def test_broken_resolver_falls_back_to_skipping(self, monkeypatch):
        import agent.background_review as br

        def _boom(_agent):
            raise RuntimeError("no config")

        monkeypatch.setattr(br, "_resolve_review_runtime", _boom)
        agent = _make_agent()
        result = self._run(agent, want_memory=True)
        agent._spawn_background_review.assert_not_called()
        assert result["final_response"] == "SDK_ASSISTANT"

    def test_raising_spawn_does_not_take_the_turn_down(self, monkeypatch):
        self._route(monkeypatch, True)
        agent = _make_agent()
        agent._spawn_background_review.side_effect = RuntimeError("thread fail")
        result = self._run(agent, want_memory=True)
        agent._spawn_background_review.assert_called_once()
        assert result["final_response"] == "SDK_ASSISTANT"


# ---------- provider wiring ----------


class TestProviderWiring:
    def test_profile_registered_with_aliases(self):
        from providers import get_provider_profile

        profile = get_provider_profile("claude-agent-sdk")
        assert profile is not None
        assert profile.api_mode == "claude_agent_sdk"
        assert profile.auth_type == "oauth_external"
        assert get_provider_profile("claude-sdk") is profile
        # The anthropic profile keeps its own alias namespace untouched.
        anthropic = get_provider_profile("claude")
        assert anthropic is not None and anthropic.name == "anthropic"

    def test_runtime_resolution_short_circuit(self):
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="claude-agent-sdk")
        assert runtime["provider"] == "claude-agent-sdk"
        assert runtime["api_mode"] == "claude_agent_sdk"
        # No credential-pool machinery, no metered key.
        assert runtime["api_key"] == "claude-subscription-oauth"

    def test_api_mode_accepted_by_agent_init(self):
        from hermes_cli.runtime_provider import _parse_api_mode

        assert _parse_api_mode("claude_agent_sdk") == "claude_agent_sdk"


class TestModelAttribution:
    """F3 (#65982 independent verification): with model.default unset the
    usage rows carried model='unknown' while the SDK's own AssistantMessage
    knew the real id — capture it and back-fill the attribution."""

    def test_session_captures_model_last_from_assistant_message(self):
        script = [
            AssistantMessage(
                content=[TextBlock("hey")], model="claude-opus-4-8-20260115"
            ),
            ResultMessage(
                result="hey", usage={"input_tokens": 1, "output_tokens": 1}
            ),
        ]
        session, _holder = _make_session(script=script)
        try:
            turn = session.run_turn("hi")
        finally:
            session.close()
        assert turn.model_last == "claude-opus-4-8-20260115"

    def test_usage_row_backfills_model_from_turn(self):
        from agent.claude_sdk_runtime_usage import _record_claude_sdk_usage

        agent = _make_agent()
        agent.model = ""
        db = MagicMock()
        agent._session_db = db
        agent._session_db_created = True
        agent.session_id = "sess-attr-1"
        turn = _make_turn(model_last="claude-opus-4-8-20260115")
        _record_claude_sdk_usage(agent, turn)
        kwargs = db.update_token_counts.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-8-20260115"

    def test_explicit_agent_model_still_wins(self):
        from agent.claude_sdk_runtime_usage import _record_claude_sdk_usage

        agent = _make_agent()
        agent.model = "claude-sonnet-5"
        db = MagicMock()
        agent._session_db = db
        agent._session_db_created = True
        agent.session_id = "sess-attr-2"
        turn = _make_turn(model_last="claude-opus-4-8-20260115")
        _record_claude_sdk_usage(agent, turn)
        kwargs = db.update_token_counts.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5"

    def test_explicit_metered_turn_is_not_recorded_as_subscription_included(self):
        from agent.claude_sdk_runtime_usage import _record_claude_sdk_usage

        agent = _make_agent()
        db = MagicMock()
        agent._session_db = db
        agent._session_db_created = True
        agent.session_id = "sess-metered-1"
        turn = _make_turn(
            billing_mode="sdk_reported_metered",
            total_cost_usd=0.25,
        )
        result = _record_claude_sdk_usage(agent, turn)
        kwargs = db.update_token_counts.call_args.kwargs
        assert kwargs["billing_mode"] == "sdk_reported_metered"
        assert kwargs["actual_cost_usd"] == 0.25
        assert kwargs["cost_status"] == "reported"
        assert result["cost_status"] == "reported"
        assert result["actual_cost_usd"] == 0.25

    def test_missing_billing_evidence_is_not_recorded_as_included(self):
        from agent.claude_sdk_runtime_usage import _record_claude_sdk_usage

        agent = _make_agent()
        db = MagicMock()
        agent._session_db = db
        agent._session_db_created = True
        agent.session_id = "sess-unverified-1"
        turn = _make_turn(billing_mode=None)
        result = _record_claude_sdk_usage(agent, turn)
        kwargs = db.update_token_counts.call_args.kwargs
        assert kwargs["billing_mode"] == "unknown"
        assert kwargs["cost_status"] == "unknown"
        assert kwargs["cost_source"] == "claude-agent-sdk-unverified"
        assert result["cost_status"] == "unknown"


class TestTurnOrchestrationOrder:
    """The orderings run_claude_agent_sdk_turn's phases must keep, pinned
    beside the per-topic suites: (i) attempt effects reset BEFORE session
    startup; (iii) terminal/stop state reconciled BEFORE the provider-fallback
    decision. (ii) record-before-sink and (iv) flush-before-persist are pinned
    by test_stream_relay_records_delivery_before_display_callback and
    test_resume_id_persisted_after_flush_and_gated_on_persist_disabled."""

    def test_session_startup_sees_the_reset_attempt_ledger(self, monkeypatch):
        import agent.claude_sdk_runtime_session as session_mod

        agent = _make_agent()
        agent._claude_sdk_session = None
        agent._current_streamed_assistant_text = "prior turn output"
        agent._sdk_issued_tool_effect = True
        seen = {}

        def fake_create_session(
            agent_, *, resume_id, on_interim_assistant, on_tool_iteration
        ):
            seen["streamed"] = agent_._current_streamed_assistant_text
            seen["tool_effect"] = agent_._sdk_issued_tool_effect
            seen["callbacks"] = (on_interim_assistant, on_tool_iteration)
            agent_._claude_sdk_session = MagicMock()
            agent_._claude_sdk_session.run_turn.return_value = _make_turn()

        monkeypatch.setattr(session_mod, "_create_session", fake_create_session)

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert seen["streamed"] == ""
        assert seen["tool_effect"] is False
        assert all(callable(cb) for cb in seen["callbacks"])
        assert result["completed"] is True

    def test_stopped_errored_turn_never_hands_off_to_another_provider(self):
        agent = _make_agent()
        stopped_turn = _make_turn(
            interrupted=True,
            error="HTTP 429 rate limit exceeded",
            should_retire=True,
            final_text="",
            projected_messages=[],
        )

        def run_turn(**_kwargs):
            # The /stop lands DURING the turn: the agent-level flag is raised
            # while the session is still running, then the turn retires.
            agent._interrupt_requested = True
            return stopped_turn

        agent._claude_sdk_session.run_turn.side_effect = run_turn

        result = run_claude_agent_sdk_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}],
            effective_task_id="task-1",
        )

        assert "failover_reason" not in result
        assert result["interrupted"] is True
        assert result["failed"] is False
        assert result["sdk_effects"]["interrupted"] is True
        assert agent._claude_sdk_session is None
        assert agent._interrupt_requested is False
