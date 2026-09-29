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
        _turn_callback_lock=threading.RLock(),
        _turn_claim_requested=False,
        _rename_claim_requested=False,
        _unsolicited_burst_open=False,
        _native_peer_in_flight=False,
        _closed=False,
        _retiring=False,
        _stream_ended=None,
        _client=fake_client if client else None,
        _loop=object() if loop else None,
        _interrupt_commit_lock=threading.Lock(),
        _pending_steer_results=0,
        _terminal_result_committed=False,
        is_live=lambda: True,
    )
    stub._native_peer_since = 0.0
    stub._native_peer_msg_id = None
    stub._native_peer_seen = False
    stub._native_peer_gen = 0
    stub._native_peer_written = False
    stub.idle_notifications = []
    stub._notify_idle_boundary = lambda: stub.idle_notifications.append(True)
    for name in ("native_peer_idle", "woken_turn_active", "_native_peer_pending_locked",
                 "_clear_native_peer_locked", "_expire_native_peer"):
        setattr(stub, name, types.MethodType(getattr(mod.ClaudeAgentSdkSession, name), stub))
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


def test_peer_message_uses_sdk_stream_with_peer_origin_when_idle(monkeypatch):
    mod, stub, queried = _transport(turn_inbox=None)
    scheduled = []

    class _Fut:
        def add_done_callback(self, cb):
            scheduled.append(cb)
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(
        mod.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _Fut(), raising=False,
    )
    origin = {"kind": "peer", "subkind": "peer-send-message", "msg_id": "7", "body": "hello"}

    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, " hello ", origin) is True

    async def collect(stream):
        return [message async for message in stream]

    assert asyncio.run(collect(queried[0])) == [{
        "type": "user", "message": {"role": "user", "content": "hello"},
        "parent_tool_use_id": None, "origin": origin,
    }]
    assert len(scheduled) == 1
    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "later", origin) is False
    assert len(queried) == 1, "a second native peer waits for the first result"


def test_peer_message_declines_while_a_turn_is_in_flight(monkeypatch):
    mod, stub, queried = _transport(turn_inbox=object())
    scheduled = []

    class _Fut:
        def add_done_callback(self, cb):
            scheduled.append(cb)
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(
        mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: _Fut(), raising=False,
    )
    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "hello", {"kind": "peer"}) is False
    assert queried == [] and scheduled == []


def test_peer_message_declines_when_scheduled_query_fails(monkeypatch):
    mod, stub, queried = _transport(turn_inbox=None)

    class _Fut:
        def add_done_callback(self, cb):
            pass
        def result(self, timeout=None):
            raise RuntimeError("SDK query failed")
        def cancel(self):
            return True

    monkeypatch.setattr(
        mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: _Fut(), raising=False,
    )
    assert mod.ClaudeAgentSdkSession.send_peer_message(
        stub, "hello", {"kind": "peer", "msg_id": "row-1"}
    ) is False


class _SettlingFuture:
    """A run_coroutine_threadsafe future the test settles by hand."""

    def __init__(self, first_wait_times_out=True):
        self._callbacks, self._exc, self._done, self._cancelled = [], None, False, False
        self.first_wait_times_out = first_wait_times_out
        self.waits = []

    def add_done_callback(self, cb):
        self._callbacks.append(cb)
        if self._done:
            cb(self)

    def settle(self, exc=None):
        self._done, self._exc = True, exc
        for cb in self._callbacks:
            cb(self)

    def result(self, timeout=None):
        self.waits.append(timeout)
        if not self._done:
            raise TimeoutError("still writing")
        if self._exc:
            raise self._exc
        return None

    def cancel(self):
        if self._done:
            return False
        self._cancelled = True
        self.settle()
        return True

    def cancelled(self):
        return self._cancelled

    def exception(self):
        return self._exc


def test_slow_write_that_completes_is_delivered_once(monkeypatch):
    """A write still going after 5 s is waited on, not reported early either way."""
    mod, stub, _queried = _transport(turn_inbox=None)
    future = _SettlingFuture()
    original_result = future.result

    def result(timeout=None):
        if len(future.waits) == 1:  # the second wait sees the write land
            future.settle()
        return original_result(timeout)

    future.result = result
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: future, raising=False)
    monkeypatch.setattr(mod.threading, "Timer", lambda *a, **kw: types.SimpleNamespace(daemon=True, start=lambda: None))

    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "hello", {"kind": "peer", "msg_id": "row-2"}) is True
    assert future.waits == [5.0, mod._NATIVE_PEER_WRITE_MAX_SECONDS - 5.0]
    assert stub._native_peer_written is True and stub.woken_turn_active() is True


def test_write_stuck_past_the_limit_is_cancelled_and_not_delivered(monkeypatch):
    """Codex recheck P1: a write reported as delivered and failing later would lose the row."""
    mod, stub, _queried = _transport(turn_inbox=None)
    future = _SettlingFuture()
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: future, raising=False)

    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "hello", {"kind": "peer", "msg_id": "row-2"}) is False
    assert future.cancelled() is True
    assert stub._native_peer_in_flight is False
    assert stub.native_peer_idle() is True


def test_unconfirmed_write_never_expires_into_a_second_admission(monkeypatch):
    """Codex recheck P1: the grace can't release a claim whose write is still pending."""
    mod, stub, _queried = _transport(turn_inbox=None)
    stub._native_peer_in_flight = True
    stub._native_peer_written = False
    stub._native_peer_since = mod.time.monotonic() - mod._NATIVE_PEER_ADMIT_GRACE_SECONDS - 60
    assert stub.native_peer_idle() is False
    assert stub._native_peer_in_flight is True


def test_late_failure_of_an_old_write_leaves_the_new_claim(monkeypatch):
    mod, stub, _queried = _transport(turn_inbox=None)
    old = _SettlingFuture()
    old_cbs = []
    old.add_done_callback = lambda cb: old_cbs.append(cb)
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: old, raising=False)
    old.result = lambda timeout=None: None  # the scheduling wait returns; settlement comes later
    monkeypatch.setattr(mod.threading, "Timer", lambda *a, **kw: types.SimpleNamespace(daemon=True, start=lambda: None))
    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "one", {"kind": "peer", "msg_id": "row-1"}) is True

    # The CLI finished that peer turn; a second row is admitted under a new generation.
    stub._native_peer_in_flight = False
    new = _SettlingFuture(first_wait_times_out=False)
    new.settle()
    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", lambda *a, **kw: new, raising=False)
    assert mod.ClaudeAgentSdkSession.send_peer_message(stub, "two", {"kind": "peer", "msg_id": "row-3"}) is True

    old._done, old._exc = True, RuntimeError("late failure")
    for cb in old_cbs:
        cb(old)
    assert stub._native_peer_in_flight is True
    assert stub._native_peer_msg_id == "row-3"


def test_grace_expiry_wakes_queued_work(monkeypatch):
    """Codex recheck P1: expiry must notify the idle boundary so a queued owner prompt drains."""
    mod, stub, _queried = _transport(turn_inbox=None)
    stub._native_peer_gen = 4
    stub._native_peer_in_flight = True
    stub._native_peer_written = True
    stub._native_peer_since = mod.time.monotonic() - mod._NATIVE_PEER_ADMIT_GRACE_SECONDS - 1

    stub._expire_native_peer(3)  # a stale timer does nothing
    assert stub._native_peer_in_flight is True and stub.idle_notifications == []

    stub._expire_native_peer(4)
    assert stub._native_peer_in_flight is False
    assert stub.idle_notifications == [True]


def test_peer_claim_expires_when_the_cli_never_starts_a_turn(monkeypatch):
    """A confirmed input that never became a CLI turn cannot hold owner input back forever."""
    mod, stub, _queried = _transport(turn_inbox=None)
    stub._native_peer_in_flight = True
    stub._native_peer_written = True
    stub._native_peer_since = mod.time.monotonic()
    assert stub.woken_turn_active() is True
    assert stub.native_peer_idle() is False

    stub._native_peer_since -= mod._NATIVE_PEER_ADMIT_GRACE_SECONDS + 1
    assert stub.woken_turn_active() is False
    assert stub.native_peer_idle() is True
    assert stub._native_peer_in_flight is False


def test_open_burst_keeps_the_peer_claim_past_the_grace(monkeypatch):
    mod, stub, _queried = _transport(turn_inbox=None)
    stub._native_peer_in_flight = True
    stub._native_peer_written = True
    stub._native_peer_since = mod.time.monotonic() - mod._NATIVE_PEER_ADMIT_GRACE_SECONDS - 1
    stub._unsolicited_burst_open = True
    assert stub.woken_turn_active() is True
    assert stub._native_peer_in_flight is True


def test_peer_message_declines_when_cli_stream_has_ended(monkeypatch):
    mod, stub, queried = _transport(turn_inbox=None)
    stub._stream_ended = object()
    stub.is_live = lambda: stub._stream_ended is None

    class _Fut:
        def add_done_callback(self, _cb):
            pass
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(mod.asyncio, "run_coroutine_threadsafe", lambda *_a, **_kw: _Fut())

    assert mod.ClaudeAgentSdkSession.send_peer_message(
        stub, "hello", {"kind": "peer", "msg_id": "dead-cli"}
    ) is False
    assert queried == []


def test_session_liveness_rejects_a_dead_cli_child(monkeypatch):
    import threading

    mod, stub, _queried = _transport(turn_inbox=None)
    stub._turn_callback_lock = threading.RLock()
    stub._closed = False
    stub._retiring = False
    stub._stream_ended = None
    stub._loop = types.SimpleNamespace(is_closed=lambda: False, is_running=lambda: True)
    monkeypatch.setattr(mod, "_sdk_child_pid", lambda _client: 123)
    monkeypatch.setattr(mod, "_own_sdk_child_process", lambda _pid: None)

    assert mod.ClaudeAgentSdkSession.is_live(stub) is False


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


def test_steer_results_are_durable_before_turn_release_without_end_duplicates(tmp_path):
    """Each settled live steer reaches state.db before the foreground turn ends."""
    from types import SimpleNamespace

    from agent.claude_sdk_runtime_continuity import _persist_turn
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import (
        AssistantMessage, ResultMessage, StreamEvent, TextBlock, _FakeClient,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("steer-durability", source="test")
    db.append_message("steer-durability", "user", "initial")
    messages = db.get_messages_as_conversation("steer-durability")
    agent = SimpleNamespace(
        _session_db=db, _session_db_created=True, _persist_disabled=False,
        session_id="steer-durability", _session_persist_lock=None,
        _flushed_db_message_ids=set(), _flushed_db_message_session_id=None,
        _last_flushed_db_idx=0, _persist_user_message_idx=None,
        _persist_user_message_override=None, _persist_user_message_timestamp=None,
        _pending_cli_user_message=None,
    )
    agent._ensure_db_session = lambda: None
    agent._flush_messages_to_session_db = AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
    agent._flush_messages_to_session_db_unlocked = AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)

    def on_steer_settled(steer_text, projected_messages):
        assert session._turn_inbox is not None, "the host turn must still own the stream at persistence"
        if steer_text:
            messages.append({"role": "user", "content": steer_text})
        messages.extend(projected_messages)
        assert agent._flush_messages_to_session_db(messages) is True
        if steer_text:
            saved = db.get_messages("steer-durability")
            assert any(row["role"] == "assistant" and row["content"] == f"answer to {steer_text}" for row in saved)

    def result(text, uuid, origin=None):
        message = ResultMessage(result=text, uuid=uuid)
        message.origin = origin
        return message

    class DurableTwoSteerClient(_FakeClient):
        async def query(self, prompt):
            if isinstance(prompt, str):
                self.queried.append(prompt)
                self._pending.append(StreamEvent(event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "working"},
                }))
                self._pending.append(AssistantMessage(content=[TextBlock("initial progress")]))
                self._pending.append(result("initial settled", "initial-result"))
                return
            payload = [message async for message in prompt]
            self.queried.append(payload)
            text = payload[0]["message"]["content"]
            self._pending.append(AssistantMessage(content=[TextBlock(f"answer to {text}")]))
            self._pending.append(result(f"answer to {text}", f"{text}-result", {"kind": "human"}))

    holder = {}
    sent = 0

    def factory(options=None):
        holder["client"] = DurableTwoSteerClient(options=options)
        return holder["client"]

    def on_delta(_text):
        nonlocal sent
        if sent == 0:
            sent = 1
            assert session.steer("correction-1") is True
            assert session.steer("correction-2") is True

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory,
        on_stream_delta=on_delta,
    )
    session._on_steer_settled = on_steer_settled
    try:
        turn = session.run_turn("initial")
        assert turn.turn_id == "correction-2-result"
        _persist_turn(agent, SimpleNamespace(
            turn=turn, messages=messages, failover_reason=None,
            turn_session_cwd=None,
        ))
    finally:
        session.close()

    saved = db.get_messages("steer-durability")
    assert [(row["role"], row["content"]) for row in saved if row["role"] in {"user", "assistant"}] == [
        ("user", "initial"),
        ("assistant", "initial progress"),
        ("user", "correction-1"),
        ("assistant", "answer to correction-1"),
        ("user", "correction-2"),
        ("assistant", "answer to correction-2"),
    ]
    assert all(message.get(_DB_PERSISTED_MARKER) for message in messages if message.get("role") in {"user", "assistant"})


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
