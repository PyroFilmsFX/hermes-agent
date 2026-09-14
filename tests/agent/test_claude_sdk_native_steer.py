"""Native /steer on the claude-agent-sdk lane.

Background: the SDK owns tool execution, so Hermes' tool-batch drain points are
never reached on this lane and a /steer stranded in ``_pending_steer`` until the
turn finalizer handed it back — which the gateway only redelivers when nothing
else is queued. Steers were silently lost.

The fix routes an in-flight steer through ``ClaudeSDKClient.query()``, the SDK's
own streaming-input contract. These tests pin the two properties that would
regress silently:

  1. EXACTLY-ONCE — an accepted native steer must not ALSO be stashed, or the
     model sees it twice (once injected, once redelivered as the next turn).
  2. FALL-BACK — when there is no live turn to steer into, the native path must
     decline so the ordinary stash still runs. Declining must not swallow.
"""

from __future__ import annotations

import asyncio
import threading
import types


# --------------------------------------------------------------------------
# run_agent.steer() routing
# --------------------------------------------------------------------------

def _agent(api_mode="claude_agent_sdk", sdk_session=None):
    return types.SimpleNamespace(
        api_mode=api_mode,
        _claude_sdk_session=sdk_session,
        _pending_steer=None,
        _pending_steer_lock=None,   # exercises the no-lock stub branch
    )


def _session(result):
    """Fake transport whose steer() returns `result` or raises if it's an Exception."""
    calls: list[str] = []

    def _steer(text):
        calls.append(text)
        if isinstance(result, Exception):
            raise result
        return result

    return types.SimpleNamespace(steer=_steer, calls=calls)


def test_accepted_native_steer_is_not_also_stashed():
    """Exactly-once: native delivery suppresses the pending-steer stash."""
    from run_agent import AIAgent

    sess = _session(True)
    agent = _agent(sdk_session=sess)

    assert AIAgent.steer(agent, "  turn left  ") is True
    assert sess.calls == ["turn left"], "text should reach the transport stripped"
    assert agent._pending_steer is None, (
        "an accepted native steer must NOT also be stashed — the turn finalizer "
        "would redeliver it as a second user turn"
    )


def test_declined_native_steer_falls_back_to_stash():
    """No live turn -> transport declines -> ordinary stash still happens."""
    from run_agent import AIAgent

    sess = _session(False)
    agent = _agent(sdk_session=sess)

    assert AIAgent.steer(agent, "turn right") is True
    assert sess.calls == ["turn right"]
    assert agent._pending_steer == "turn right", "declined steer must not be lost"


def test_native_steer_exception_falls_back_to_stash():
    """A raising transport must degrade to the stash, never drop the text."""
    from run_agent import AIAgent

    sess = _session(RuntimeError("client gone"))
    agent = _agent(sdk_session=sess)

    assert AIAgent.steer(agent, "still important") is True
    assert agent._pending_steer == "still important"


def test_non_sdk_lane_never_consults_the_transport():
    """Other lanes keep the old behaviour byte-for-byte."""
    from run_agent import AIAgent

    sess = _session(True)
    agent = _agent(api_mode="chat_completions", sdk_session=sess)

    assert AIAgent.steer(agent, "hello") is True
    assert sess.calls == [], "non-SDK lanes must not reach the SDK transport"
    assert agent._pending_steer == "hello"


def test_empty_steer_rejected_before_any_routing():
    from run_agent import AIAgent

    sess = _session(True)
    agent = _agent(sdk_session=sess)

    assert AIAgent.steer(agent, "   ") is False
    assert sess.calls == []
    assert agent._pending_steer is None


# --------------------------------------------------------------------------
# transport-level steer()
# --------------------------------------------------------------------------

def _transport(turn_inbox, client=True, loop=True):
    from agent.transports import claude_agent_sdk_session as mod

    queried: list[object] = []
    fake_client = types.SimpleNamespace(query=lambda t: queried.append(t))
    stub = types.SimpleNamespace(
        _turn_inbox=turn_inbox,
        _client=fake_client if client else None,
        _loop=object() if loop else None,
        _interrupt_commit_lock=threading.Lock(),
        _pending_steer_results=0,
        _terminal_result_committed=False,
    )
    return mod, stub, queried


def test_transport_declines_when_no_turn_is_in_flight(monkeypatch):
    """Without a claimed turn there is nothing to steer INTO.

    Sending anyway would open an unclaimed turn whose output the reader routes
    to the unsolicited path — a reply appearing from nowhere.
    """
    mod, stub, queried = _transport(turn_inbox=None)
    called = []
    monkeypatch.setattr(
        mod.asyncio, "run_coroutine_threadsafe",
        lambda *a, **kw: called.append(a), raising=False,
    )

    assert mod.ClaudeAgentSdkSession.steer(stub, "mid-turn note") is False
    assert queried == [] and called == []


def test_transport_schedules_query_on_a_live_turn(monkeypatch):
    """With a turn claimed, the steer is scheduled onto the session loop."""
    mod, stub, queried = _transport(turn_inbox=object())
    scheduled = []

    class _Fut:
        def add_done_callback(self, cb):
            scheduled.append(cb)

    monkeypatch.setattr(
        mod.asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: _Fut(), raising=False,
    )

    assert mod.ClaudeAgentSdkSession.steer(stub, "  actually, stop  ") is True
    assert len(queried) == 1
    async def collect(stream):
        return [message async for message in stream]

    assert asyncio.run(collect(queried[0])) == [{
        "type": "user",
        "message": {"role": "user", "content": "actually, stop"},
        "parent_tool_use_id": None,
        "origin": {"kind": "human"},
    }]
    assert len(scheduled) == 1, "future must carry a done-callback so the "
    "exception is retrieved, not logged at teardown"


def test_transport_declines_when_client_or_loop_missing(monkeypatch):
    mod, stub, queried = _transport(turn_inbox=object(), client=False)
    assert mod.ClaudeAgentSdkSession.steer(stub, "note") is False

    mod, stub, queried = _transport(turn_inbox=object(), loop=False)
    assert mod.ClaudeAgentSdkSession.steer(stub, "note") is False
    assert queried == []


def test_transport_declines_empty_text():
    mod, stub, queried = _transport(turn_inbox=object())
    assert mod.ClaudeAgentSdkSession.steer(stub, "") is False
    assert mod.ClaudeAgentSdkSession.steer(stub, "   \n ") is False
    assert queried == []


def test_mid_turn_steer_result_stays_owned_by_live_turn():
    """A human-origin result from query(steer) is not a background result."""
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import (
        ResultMessage,
        StreamEvent,
        _FakeClient,
    )

    def result(text, uuid, origin=None):
        message = ResultMessage(result=text, uuid=uuid)
        message.origin = origin
        return message

    class SteerClient(_FakeClient):
        async def query(self, prompt):
            if isinstance(prompt, str):
                self.queried.append(prompt)
                if len(self.queried) == 1:
                    self._pending.append(StreamEvent(event={
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "working"},
                    }))
                    self._pending.append(result("original", "original-result"))
                else:
                    self._pending.append(result(
                        "steered", "steer-result", {"kind": "human"},
                    ))
            else:
                payload = [message async for message in prompt]
                self.queried.append(payload)
                self._pending.append(result(
                    "steered", "steer-result", {"kind": "human"},
                ))

    holder = {}
    delivered = []
    steer_sent = False

    def factory(options=None):
        holder["client"] = SteerClient(options=options)
        return holder["client"]

    def on_delta(_text):
        nonlocal steer_sent
        if not steer_sent:
            steer_sent = True
            assert session.steer("correct course") is True

    session = ClaudeAgentSdkSession(
        cwd="/tmp",
        model="claude-opus-4-8",
        client_factory=factory,
        on_stream_delta=on_delta,
        on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items)),
    )
    try:
        turn = session.run_turn("initial")
    finally:
        session.close()

    assert holder["client"].queried[0] == "initial"
    assert holder["client"].queried[1] == [{
        "type": "user",
        "message": {"role": "user", "content": "correct course"},
        "parent_tool_use_id": None,
        "origin": {"kind": "human"},
    }], "steers must use the SDK message stream with human origin"
    assert turn.final_text == "steered", "steer result must close the live host turn"
    assert turn.turn_id == "steer-result", "live accounting must use the steer result once"
    assert delivered == [], "steer result must not use unsolicited delivery"
    assert session._unsolicited_results == 0


def test_two_steers_keep_foreground_ownership_until_both_results():
    """Every accepted steer result belongs to the same host turn."""
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import ResultMessage, StreamEvent, _FakeClient

    def result(text, uuid, origin=None):
        message = ResultMessage(result=text, uuid=uuid)
        message.origin = origin
        return message

    class TwoSteerClient(_FakeClient):
        async def query(self, prompt):
            if isinstance(prompt, str):
                self.queried.append(prompt)
                if len([item for item in self.queried if isinstance(item, str)]) == 1:
                    self._pending.append(StreamEvent(event={
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "working"},
                    }))
                    self._pending.append(result("original", "original-result"))
                elif not any(isinstance(item, list) for item in self.queried):
                    steer_number = len([
                        item for item in self.queried if isinstance(item, str)
                    ]) - 1
                    self._pending.append(result(
                        f"steer-{steer_number}", f"steer-{steer_number}-result",
                        {"kind": "human"},
                    ))
                else:
                    self._pending.append(result("next host answer", "next-result"))
                return
            payload = [message async for message in prompt]
            self.queried.append(payload)
            steer_number = len([item for item in self.queried if isinstance(item, list)])
            self._pending.append(result(
                f"steer-{steer_number}", f"steer-{steer_number}-result",
                {"kind": "human"},
            ))

    holder = {}
    sent = 0

    def factory(options=None):
        holder["client"] = TwoSteerClient(options=options)
        return holder["client"]

    def on_delta(_text):
        nonlocal sent
        if sent == 0:
            sent = 1
            assert session.steer("correction-1") is True
            sent = 2
            assert session.steer("correction-2") is True

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory,
        on_stream_delta=on_delta,
    )
    try:
        turn = session.run_turn("initial")
        next_turn = session.run_turn("next host question")
    finally:
        session.close()

    assert turn.final_text == "steer-2", (
        "the foreground turn must remain open until the second steer result"
    )
    assert turn.turn_id == "steer-2-result"
    assert next_turn.final_text == "next host answer", (
        "the following host turn must receive its own result"
    )


def test_steer_admitted_during_result_projection_remains_foreground_owned(monkeypatch):
    """A steer admitted in the projection/commit gap stays in this turn."""
    from agent.transports import claude_agent_sdk_session_turn as turn_module
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import ResultMessage, _FakeClient

    def result(text, uuid, origin=None):
        message = ResultMessage(result=text, uuid=uuid)
        message.origin = origin
        return message

    class BarrierClient(_FakeClient):
        async def query(self, prompt):
            if isinstance(prompt, str):
                self.queried.append(prompt)
                self._pending.append(result("original", "original-result"))
            else:
                self.queried.append([message async for message in prompt])
                self._pending.append(result(
                    "barrier steer", "barrier-result", {"kind": "human"},
                ))

    holder = {}

    def factory(options=None):
        holder["client"] = BarrierClient(options=options)
        return holder["client"]

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory,
    )
    original_project = turn_module.ClaudeSdkTurnMixin._project_message_step
    admitted = False

    def project(projector, watch, message, out):
        nonlocal admitted
        projection = original_project(projector, watch, message, out)
        if type(message).__name__ == "ResultMessage" and not admitted:
            admitted = True
            assert session.steer("during projection") is True
        return projection

    monkeypatch.setattr(
        turn_module.ClaudeSdkTurnMixin,
        "_project_message_step",
        staticmethod(project),
    )
    try:
        turn = session.run_turn("initial")
    finally:
        session.close()

    assert turn.final_text == "barrier steer", (
        "a steer admitted during projection must stay owned by the live turn"
    )
    assert turn.turn_id == "barrier-result"


def test_steer_after_terminal_result_is_not_claimed_by_next_turn():
    """A steer racing result acceptance falls back instead of opening a stray query."""
    mod, stub, queried = _transport(turn_inbox=object())
    stub._terminal_result_committed = True

    called = []
    stub._client.query = lambda text: called.append(text)
    assert mod.ClaudeAgentSdkSession.steer(stub, "late correction") is False
    assert queried == [] and called == []


def test_late_human_result_is_not_classified_as_unsolicited():
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import ResultMessage

    delivered = []
    session = ClaudeAgentSdkSession(
        cwd="/tmp",
        on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items)),
    )
    message = ResultMessage(result="steered", uuid="late-steer")
    message.origin = {"kind": "human"}
    session._unsolicited_text.append("stale steer text")

    session._handle_unsolicited(message)

    assert delivered == []
    assert session._unsolicited_results == 0
    assert session._unsolicited_text == []
