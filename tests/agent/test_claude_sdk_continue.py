"""D62 "continue, don't replay": a turn that dies mid-work continues the SAME Claude
session (L2 CLAUDE_CODE_RESUME_INTERRUPTED_TURN, else one nudge), bounded, never
after a user stop / auth, and never re-sends the user's prompt. Plus L1 env and
the usage-limit park/auto-resume."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.agent.claude_sdk_fakes import _make_agent, isolate_provider_config

PROMPT = "hello"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


def _config(monkeypatch, **block):
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg, "load_config_readonly",
        lambda *a, **k: {"agent": {"claude_agent_sdk": dict(block)}}, raising=False,
    )


def _state():
    from agent.claude_sdk_runtime_state import _SdkTurnState

    messages = [{"role": "user", "content": PROMPT}]
    return _SdkTurnState(
        user_input=PROMPT, original_user_message=PROMPT,
        messages=messages, messages_before_attempt=list(messages),
    )


def _turn(**kw):
    base = dict(
        interrupted=False, error=None, thread_id="sdk-A", turn_id="t", projected_messages=[],
        tool_iterations=0, final_text="", should_retire=False, stream_ended=False,
        api_call_made=True, fatal_reason=None, retired_before_query=False,
        api_error_status=None, api_error_kind=None, api_retries=None, rate_limit_rejected=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _death(**kw):
    """A CLI death after the query, mid-work (a tool already ran)."""
    return _turn(
        error="SDK message stream ended unexpectedly", should_retire=True, stream_ended=True,
        projected_messages=[{"role": "assistant", "tool_calls": [{"id": "call-1"}]}],
        tool_iterations=1, **kw,
    )


def _answer(**kw):
    return _turn(final_text="done", projected_messages=[{"role": "assistant", "content": "done"}], **kw)


def _scripted(monkeypatch, agent, per_session_turns, *, persisted=None):
    """Each created session plays its own list of turns; records create kwargs + inputs."""
    import agent.claude_sdk_runtime_session as mod

    created, stored = [], []
    scripts = iter(per_session_turns)

    def create_session(agent_, **kwargs):
        turns = iter(next(scripts))
        session = MagicMock()
        session._cwd = "/tmp"
        session.inputs = []

        def run_turn(user_input):
            session.inputs.append(user_input)
            return next(turns)

        session.run_turn.side_effect = run_turn
        created.append((kwargs, session))
        agent_._claude_sdk_session = session
        return session

    store = {"id": persisted}

    def _store(agent_, value, **_kw):
        store["id"] = value
        stored.append(value)

    monkeypatch.setattr(mod, "_persisted_sdk_session_id", lambda _a: store["id"])
    monkeypatch.setattr(mod, "_store_sdk_session_id", _store)
    monkeypatch.setattr(mod, "_create_session", create_session)
    monkeypatch.setattr(mod, "_background_result_sink", lambda _a: None)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    agent._claude_sdk_session = None
    agent._current_streamed_assistant_text = ""
    agent._sdk_issued_tool_effect = False
    agent._claude_sdk_continue_requested = False
    return mod, created, stored


def _sent_inputs(created):
    return [value for _kw, session in created for value in session.inputs]


def test_mid_work_kill_resumes_same_session_with_l2_and_never_resends_prompt(monkeypatch):
    from agent.transports.claude_agent_sdk_session_turn import RESUME_INTERRUPTED_TURN

    agent = _make_agent()
    mod, created, stored = _scripted(monkeypatch, agent, [[_death()], [_answer()]])
    state = _state()

    assert mod._run_sdk_attempts(agent, state) is None

    assert len(created) == 2
    kwargs, session = created[1]
    assert kwargs["resume_id"] == "sdk-A"
    assert kwargs.get("resume_interrupted_turn") is True
    assert session.inputs == [RESUME_INTERRUPTED_TURN]
    assert _sent_inputs(created).count(PROMPT) == 1  # only the original send
    assert None not in stored and "sdk-A" in stored  # the id is never cleared
    assert state.turn.error is None
    # The dead attempt's work stays in the transcript ahead of the continuation.
    assert state.turn.projected_messages[0]["tool_calls"][0]["id"] == "call-1"


def test_nudge_fallback_when_cli_declines_l2(monkeypatch):
    agent = _make_agent()
    declined = _turn(resume_declined=True, api_call_made=False)
    mod, created, _ = _scripted(monkeypatch, agent, [[_death()], [declined, _answer()]])
    state = _state()

    mod._run_sdk_attempts(agent, state)

    assert len(created) == 2  # the nudge rides the same live L2 session
    nudge = created[1][1].inputs[1]
    assert isinstance(nudge, str) and "Continue where you left off" in nudge
    assert PROMPT not in _sent_inputs(created)[1:]
    assert state.turn.error is None


def test_nudge_when_l2_disabled(monkeypatch):
    _config(monkeypatch, resume_interrupted_turn=False)
    agent = _make_agent()
    mod, created, _ = _scripted(monkeypatch, agent, [[_death()], [_answer()]])

    mod._run_sdk_attempts(agent, _state())

    kwargs, session = created[1]
    assert "resume_interrupted_turn" not in kwargs and kwargs["resume_id"] == "sdk-A"
    assert "Continue where you left off" in session.inputs[0]


def test_continue_cap_bounds_automatic_continues(monkeypatch):
    agent = _make_agent()
    mod, created, _ = _scripted(monkeypatch, agent, [[_death()], [_death()], [_death()], [_death()]])
    state = _state()

    mod._run_sdk_attempts(agent, state)

    assert len(created) == 3  # original + 2 continues (default cap)
    assert "after 2 automatic continues" in state.turn.error
    assert _sent_inputs(created).count(PROMPT) == 1


@pytest.mark.parametrize("case", ["user_interrupt", "auth"])
def test_no_continue_on_user_stop_or_auth(monkeypatch, case):
    agent = _make_agent()
    dead = _death(fatal_reason="auth") if case == "auth" else _death(interrupted=True)
    if case == "user_interrupt":
        agent._interrupt_requested = True
    mod, created, _ = _scripted(monkeypatch, agent, [[dead], [_answer()]])

    mod._run_sdk_attempts(agent, _state())

    assert len(created) == 1


def test_watchdog_trip_continues_instead_of_ending(monkeypatch, caplog):
    agent = _make_agent()
    trip = _turn(interrupted=True, watchdog_trip=True, error="turn idle for 900s; interrupted",
                 tool_iterations=2)
    mod, created, _ = _scripted(monkeypatch, agent, [[trip], [_answer()]])
    state = _state()

    mod._run_sdk_attempts(agent, state)

    assert len(created) == 2 and created[1][0]["resume_id"] == "sdk-A"
    assert state.turn.error is None
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == ["claude-agent-sdk: watchdog trip: turn idle for 900s; interrupted"]


def test_transient_error_after_side_effects_continues_not_replays(monkeypatch):
    agent = _make_agent()
    failure = _turn(error="Claude API error (server_error): HTTP 503", api_error_status=503,
                    api_error_kind="server_error", tool_iterations=1)
    mod, created, _ = _scripted(monkeypatch, agent, [[failure], [_answer()]])

    mod._run_sdk_attempts(agent, _state())

    assert len(created) == 2 and created[1][0]["resume_id"] == "sdk-A"
    assert PROMPT not in _sent_inputs(created)[1:]


@pytest.mark.parametrize("opt_in", [False, True])
def test_prompt_replay_only_when_opted_in(monkeypatch, opt_in):
    _config(monkeypatch, continue_max_per_turn=0, transient_retry_replay=opt_in)
    agent = _make_agent()
    failure = _turn(error="Claude API error (server_error): HTTP 503", api_error_status=503,
                    api_error_kind="server_error", should_retire=True)
    mod, created, _ = _scripted(monkeypatch, agent, [[failure], [_answer()]])

    mod._run_sdk_attempts(agent, _state())

    assert len(created) == (2 if opt_in else 1)
    if opt_in:
        assert created[1][1].inputs == [PROMPT]  # the legacy replay re-sends; only by opt-in


def test_l1_retry_watchdog_env(monkeypatch):
    from agent.transports.claude_agent_sdk_session_config import _sdk_env_overrides

    assert _sdk_env_overrides(sdk_cwd="/tmp/proj", task_env={})["CLAUDE_CODE_RETRY_WATCHDOG"] == "1"
    assert "CLAUDE_CODE_RETRY_WATCHDOG" not in _sdk_env_overrides(task_env={})  # aux one-shots
    _config(monkeypatch, retry_watchdog=False)
    assert "CLAUDE_CODE_RETRY_WATCHDOG" not in _sdk_env_overrides(sdk_cwd="/tmp/proj", task_env={})


# ---------- usage-limit park / auto-resume ----------

@pytest.fixture
def park_db(monkeypatch, tmp_path):
    from agent import claude_sdk_usage_park as usage_park

    monkeypatch.setattr(usage_park, "_db_path", lambda home=None: tmp_path / "state.db")
    return usage_park


def _limit(resets_at, kind="five_hour"):
    return _turn(error="Claude API error (rate_limit): HTTP 429", api_error_status=429,
                 api_error_kind="rate_limit",
                 rate_limit_rejected={"rate_limit_type": kind, "resets_at": resets_at})


def test_five_hour_limit_parks_then_resumes_same_session_without_resend(monkeypatch, park_db):
    from agent.transports.claude_agent_sdk_session_turn import RESUME_INTERRUPTED_TURN

    resets_at = time.time() + 3 * 3600
    agent = _make_agent()
    mod, created, stored = _scripted(monkeypatch, agent, [[_limit(resets_at)]])
    state = _state()

    mod._run_sdk_attempts(agent, state)

    assert len(created) == 1
    assert state.turn.error.startswith("Paused: usage limit, resumes at ")
    record = park_db.get("sess-1")
    assert record["sdk_session_id"] == "sdk-A" and record["resets_at"] == pytest.approx(resets_at)
    assert resets_at <= record["resume_at"] <= resets_at + 90
    assert stored[-1] == "sdk-A"
    assert park_db.due(now=resets_at - 1) == []

    # At reset the gateway continues the session: the runtime L2-resumes the same id.
    agent2 = _make_agent()
    mod, created2, _ = _scripted(monkeypatch, agent2, [[_answer()]], persisted="sdk-A")

    def dispatch(rec):
        agent2._claude_sdk_continue_requested = True
        mod._run_sdk_attempts(agent2, _state_with("[System note: continue]"))
        return True

    assert park_db.dispatch_due(dispatch, now=record["resume_at"] + 1) == ["sess-1"]
    kwargs, session = created2[0]
    assert kwargs["resume_id"] == "sdk-A" and kwargs.get("resume_interrupted_turn") is True
    assert session.inputs == [RESUME_INTERRUPTED_TURN]
    assert park_db.get("sess-1") is None


def _state_with(text):
    from agent.claude_sdk_runtime_state import _SdkTurnState

    messages = [{"role": "user", "content": PROMPT}, {"role": "user", "content": text}]
    return _SdkTurnState(user_input=text, original_user_message=text,
                         messages=messages, messages_before_attempt=list(messages))


def test_park_survives_restart(park_db, tmp_path):
    resets_at = time.time() + 5 * 3600
    park_db.park("sess-9", "sdk-9", resets_at, "seven_day", stagger_max=90)
    import importlib
    import sqlite3

    # A new process only has the file: read it back through a fresh connection.
    rows = sqlite3.connect(str(tmp_path / "state.db")).execute(
        "SELECT session_key, sdk_session_id FROM sdk_usage_parks").fetchall()
    assert rows == [("sess-9", "sdk-9")]
    fresh = importlib.reload(park_db)
    fresh._db_path = lambda home=None: tmp_path / "state.db"
    assert [r["session_key"] for r in fresh.due(now=resets_at + 91)] == ["sess-9"]


def test_stagger_spreads_sessions(park_db):
    resets_at = time.time() + 3600
    a = park_db.park("sess-a", "sdk-a", resets_at, "five_hour", stagger_max=90)
    b = park_db.park("sess-b", "sdk-b", resets_at, "five_hour", stagger_max=90)
    assert a["resume_at"] != b["resume_at"]
    for record in (a, b):
        assert resets_at <= record["resume_at"] <= resets_at + 90
    # Deterministic per session: a restart recomputes the same moment.
    assert park_db.stagger_seconds("sess-a", resets_at, 90) == pytest.approx(a["resume_at"] - resets_at)


def test_stop_cancels_pause(park_db):
    resets_at = time.time() + 3600
    park_db.park("sess-s", "sdk-s", resets_at, "five_hour", stagger_max=0)
    assert park_db.cancel("sess-s") is True
    fired = []
    assert park_db.dispatch_due(lambda rec: fired.append(rec) or True, now=resets_at + 10) == []
    assert fired == []


def test_refused_continuation_keeps_park_and_backs_off(park_db):
    now = time.time()
    park_db.park("sess-refused", "sdk-refused", now - 10, "five_hour", stagger_max=0, now=now - 20)

    assert park_db.dispatch_due(lambda _rec: False, now=now) == []

    retained = park_db.get("sess-refused")
    assert retained is not None
    assert retained["resume_at"] > now
    assert park_db.due(now=now) == []


@pytest.mark.parametrize("failure", [
    _turn(error="You're out of extra usage", api_error_status=400, api_error_kind="invalid_request"),
    _turn(error="Claude API error (rate_limit): overage", api_error_status=429,
          rate_limit_rejected={"rate_limit_type": "overage"}),
    _turn(error="Claude API error (rate_limit): HTTP 429", api_error_status=429, api_error_kind="rate_limit"),
])
def test_billing_or_no_reset_is_not_parked(monkeypatch, park_db, failure):
    monkeypatch.setattr(park_db, "_account_window_reset", lambda now: None)
    agent = _make_agent()
    mod, created, _ = _scripted(monkeypatch, agent, [[failure], [_answer()], [_answer()]])
    state = _state()

    mod._run_sdk_attempts(agent, state)

    assert park_db.due(now=time.time() + 10 * 86400) == []
    assert not str(getattr(state.turn, "error", "") or "").startswith("Paused")
