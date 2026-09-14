"""A claude-agent-sdk background result (a peer SendMessage the CLI answered, a finished
background Agent task) must be SHOWN in the desktop chat as the agent's own message.

Before this lane existed the event fell through the generic process formatter and was
re-injected as "[IMPORTANT: Background process unknown exited (exit code ?) Output: ]" —
the reply was persisted but never displayed, and the model got an empty notice. Seen live
2026-09-09 ("the send message tool sorta works but sends nothing").
"""
from __future__ import annotations

import contextlib
import queue
import threading
import types

import pytest

from tools.process_registry_notifications import format_process_notification
from tui_gateway import server


class _Db:
    def __init__(self):
        self.rows = []

    def append_message(self, **kw):
        self.rows.append(kw)
        return len(self.rows)

    def append_messages_batch(self, session_id, messages, **_kwargs):
        """Small transactional test double for SessionDB.append_messages_batch()."""
        before = list(self.rows)
        try:
            for message in messages:
                message_id = self.append_message(
                    session_id=session_id,
                    role=message["role"],
                    content=message.get("content"),
                    tool_calls=message.get("tool_calls"),
                    tool_call_id=message.get("tool_call_id"),
                    timestamp=message.get("timestamp"),
                    display_kind=message.get("display_kind"),
                    display_metadata=message.get("display_metadata"),
                )
                message["_row_id"] = message_id
        except Exception:
            self.rows = before
            raise
        return len(messages)


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(session_id="hs-1", messages=[]),
        "session_key": "sk-1", "history": [], "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "_notification_emitted": set(), **extra,
    }


def _event(**over):
    evt = {"type": "sdk_background_result", "payloads": ["PEER-OK from the CLI"], "session_key": "sk-1",
           "parent_session_id": "hs-1", "model": "m", "dispatched_at": 1.0, "completed_at": 2.0}
    evt.update(over)
    return evt


@pytest.fixture()
def wired(monkeypatch):
    emitted, db = [], _Db()
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))
    monkeypatch.setattr(server, "_get_usage", lambda agent: {"model": "m"})

    @contextlib.contextmanager
    def _db(session):
        yield db

    monkeypatch.setattr(server, "_session_db", _db)
    # The ownership check resolves compression-rotated keys through the SHARED db handle
    # (_get_db caches server._db); opening the real one here would leak that cache into
    # later tests that expect a fresh profile db. Resolve keys as themselves instead.
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return emitted, db


def _registry():
    return types.SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda sid: False)


def test_result_is_persisted_appended_and_painted(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    ok = server._notif_handle_event("ui-1", session, _event(), session["_notification_emitted"], reg,
                                    format_process_notification, None)
    assert ok is True
    # Persisted as the agent's own answer, marked so the continuity digest never re-presents it.
    assert db.rows and db.rows[0]["role"] == "assistant" and db.rows[0]["content"] == "PEER-OK from the CLI"
    assert db.rows[0]["display_kind"] == "sdk_background_result"
    # Durable UI history retains the answer without replaying it to the model.
    assert session["history"][-1]["content"] == "PEER-OK from the CLI" and session["history_version"] == 1
    assert session["agent"].messages == []
    # Painted as a completed message — never re-injected as a prompt.
    kinds = [e[0] for e in emitted]
    assert kinds == ["message.start", "message.complete"]
    assert emitted[1][2]["text"] == "PEER-OK from the CLI" and emitted[1][2]["status"] == "complete"
    assert not any(e[0] == "status.update" for e in emitted)
    # The turn claim is released so the user can type again.
    assert session["running"] is False
    assert reg.completion_queue.empty()


def test_busy_session_requeues_instead_of_dropping(wired):
    emitted, db = wired
    session, reg = _session(running=True), _registry()
    server._notif_handle_event("ui-1", session, _event(), session["_notification_emitted"], reg,
                               format_process_notification, None)
    assert db.rows == [] and emitted == []
    assert reg.completion_queue.qsize() == 1  # retried once the turn ends, never lost


def test_duplicate_event_is_delivered_once(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    for _ in range(2):
        server._notif_handle_event("ui-1", session, _event(), session["_notification_emitted"], reg,
                                   format_process_notification, None)
    assert len(db.rows) == 1 and [e[0] for e in emitted] == ["message.start", "message.complete"]


def test_parent_session_id_alone_proves_ownership(wired):
    """The SDK callback fires on the SDK loop thread where the session-key contextvar can be
    unset; the hermes session id it carries must be enough."""
    emitted, db = wired
    session, reg = _session(), _registry()
    server._notif_handle_event("ui-1", session, _event(session_key=""), session["_notification_emitted"], reg,
                               format_process_notification, None)
    assert db.rows and emitted


def test_foreign_result_is_not_adopted(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    server._notif_handle_event("ui-1", session, _event(session_key="other", parent_session_id="hs-other"),
                               session["_notification_emitted"], reg, format_process_notification, None)
    assert db.rows == [] and emitted == []


def test_empty_key_foreign_parent_is_not_consumed_by_this_tab(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    server._notif_handle_event(
        "ui-1", session, _event(session_key="", parent_session_id="hs-foreign"),
        session["_notification_emitted"], reg, format_process_notification, None,
    )
    assert db.rows == [] and emitted == [] and reg.completion_queue.empty()


def test_rotated_parent_event_resolves_to_child_but_unrelated_parent_is_rejected(wired, monkeypatch):
    emitted, db = wired
    session = _session(agent=types.SimpleNamespace(session_id="C", messages=[]), session_key="C")
    reg = _registry()
    monkeypatch.setattr(server, "_notif_resolve_event_key", lambda key, _session=None: {"P": "C"}.get(key, key))

    server._notif_handle_event(
        "ui-1", session, _event(parent_session_id="P", payloads=["from parent"]),
        session["_notification_emitted"], reg, format_process_notification, None,
    )
    server._notif_handle_event(
        "ui-1", session, _event(parent_session_id="unrelated", payloads=["foreign"]),
        session["_notification_emitted"], reg, format_process_notification, None,
    )

    assert [row["content"] for row in db.rows] == ["from parent"]
    assert session["history"][-1]["content"] == "from parent"
    assert not any(event == "message.complete" and payload["text"] == "foreign"
                   for event, _sid, payload in emitted)


def test_item_delivery_ack_retries_only_unacknowledged_items(wired, monkeypatch):
    emitted, db = wired
    session, reg = _session(), _registry()
    evt = _event(
        payloads=["assistant text"],
        items=[
            {
                "kind": "peer_in", "text": "incoming", "from": "peer-id",
                "name": "Peer", "from_session": "peer-s", "uuid": "peer-1",
            },
            {"kind": "text", "text": "assistant text"},
        ],
    )
    failed = {"value": False}
    original_emit = server._emit

    def fail_second_item(event, sid, payload=None):
        if event == "message.complete" and payload and payload.get("display_kind") == "sdk_background_result" and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("desktop write failed")
        return original_emit(event, sid, payload)

    monkeypatch.setattr(server, "_emit", fail_second_item)
    server._notif_handle_event(
        "ui-1", session, evt, session["_notification_emitted"], reg,
        format_process_notification, None,
    )
    assert evt["delivered_ids"] == ["peer-1"]
    assert reg.completion_queue.qsize() == 1
    assert [row["content"] for row in db.rows] == ["incoming", "assistant text"]

    monkeypatch.setattr(server, "_emit", original_emit)
    retry = reg.completion_queue.get_nowait()
    server._notif_handle_event(
        "ui-1", session, retry, session["_notification_emitted"], reg,
        format_process_notification, None,
    )
    assert retry["delivered_ids"] == ["peer-1", "text:1"]
    assert [row["content"] for row in db.rows].count("incoming") == 1
    assert [row["content"] for row in db.rows].count("assistant text") == 1


def test_items_emit_and_persist_peer_tool_text_shapes(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    evt = _event(
        payloads=["final text"],
        items=[
            {
                "kind": "peer_in", "text": "incoming", "from": "peer-id",
                "name": "Peer", "from_session": "peer-s", "uuid": "peer-1",
            },
            {
                "kind": "tool", "tool_use_id": "tool-1", "name": "SendMessage",
                "args": {"to": "peer-id", "message": "outgoing"},
                "result": "failed", "is_error": True,
            },
            {"kind": "peer_out", "text": "outgoing", "to": "peer-id", "tool_use_id": "tool-1"},
            {"kind": "text", "text": "final text"},
        ],
    )
    assert server._notif_handle_event(
        "ui-1", session, evt, session["_notification_emitted"], reg,
        format_process_notification, None,
    ) is True

    assert [row["role"] for row in db.rows] == ["user", "assistant", "tool", "assistant", "assistant"]
    assert db.rows[0]["display_kind"] == "peer_message"
    assert db.rows[0]["display_metadata"] == {
        "direction": "in", "peer": "Peer", "peer_session": "peer-s",
        "msg_id": "peer-1", "completed_at": 2.0,
    }
    assert db.rows[1]["tool_calls"][0]["function"]["name"] == "SendMessage"
    assert db.rows[2]["tool_call_id"] == "tool-1"
    assert db.rows[2]["content"] == "[error] failed"
    assert db.rows[1]["display_metadata"]["source"] == "sdk_background_result"
    assert db.rows[2]["display_metadata"]["source"] == "sdk_background_result"
    assert db.rows[3]["display_metadata"] == {
        "direction": "out", "peer": "peer-id", "msg_id": "tool-1", "completed_at": 2.0,
    }
    assert [event for event, _sid, _payload in emitted] == [
        "message.start", "message.complete", "message.start", "tool.start",
        "tool.complete", "message.complete", "message.start", "message.complete",
        "message.start", "message.complete",
    ]
    assert emitted[1][2]["display_metadata"]["direction"] == "in"
    assert emitted[4][2]["tool_id"] == "tool-1"
    assert emitted[4][2]["is_error"] is True
    assert emitted[4][2]["error"] is True
    assert emitted[7][2]["display_metadata"]["direction"] == "out"
    assert emitted[9][2]["background"] is True
    # UI-only projections are durable/display history, never the next model prompt.
    assert session["agent"].messages == []


def test_agent_owned_sdk_answer_stays_in_display_history_only(wired):
    emitted, db = wired
    session, reg = _session(), _registry()
    evt = _event(
        payloads=["agent answer"],
        items=[
            {"kind": "lifecycle", "event": "woken", "source": "task-notification"},
            {"kind": "peer_in", "text": "incoming", "uuid": "peer-2"},
            {"kind": "tool", "tool_use_id": "tool-2", "name": "TaskGet", "args": {}, "result": "ok"},
            {"kind": "text", "text": "agent answer"},
        ],
    )
    assert server._notif_handle_event(
        "ui-1", session, evt, session["_notification_emitted"], reg,
        format_process_notification, None,
    ) is True
    assert len(session["history"]) == 5
    assert session["agent"].messages == []


def test_failed_persistence_retries_without_duplicate_history(wired, monkeypatch):
    emitted, db = wired
    session, reg, evt = _session(), _registry(), _event()
    append = db.append_message
    attempts = []

    def fail_once(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("database temporarily unavailable")
        return append(**kwargs)

    monkeypatch.setattr(db, "append_message", fail_once)
    for _ in range(2):
        server._notif_handle_event("ui-1", session, evt, session["_notification_emitted"], reg,
                                   format_process_notification, None)
    assert len(db.rows) == 1
    assert len(session["history"]) == 1
    assert len(attempts) == 2
    assert evt["persisted_ids"] == ["text:0"]
    assert evt["delivered_ids"] == ["text:0"]


def test_failed_tool_uses_shared_completion_cleanup(wired, monkeypatch):
    emitted, db = wired
    session, reg = _session(), _registry()
    monkeypatch.setitem(server._sessions, "ui-1", session)
    session["edit_snapshots"] = {"failed-tool": {}}
    evt = _event(items=[{
        "kind": "tool", "tool_use_id": "failed-tool", "name": "TaskGet",
        "args": {}, "result": "boom", "is_error": True,
    }])
    server._notif_handle_event("ui-1", session, evt, session["_notification_emitted"], reg,
                               format_process_notification, None)
    assert "failed-tool" not in session["tool_started_at"]
    assert "failed-tool" not in session["edit_snapshots"]
    payload = next(payload for event, _, payload in emitted if event == "tool.complete")
    assert payload["is_error"] is True


def test_retry_after_tool_complete_failure_does_not_duplicate_tool_rows(wired, monkeypatch):
    emitted, db = wired
    session, reg = _session(), _registry()
    evt = _event(items=[
        {"kind": "tool", "tool_use_id": "tool-retry", "name": "TaskGet", "args": {}, "result": "boom", "is_error": True},
    ])
    original_emit = server._emit
    failed = {"value": False}

    def fail_tool_complete(event, sid, payload=None):
        if event == "tool.complete" and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("desktop write failed")
        return original_emit(event, sid, payload)

    monkeypatch.setattr(server, "_emit", fail_tool_complete)
    server._notif_handle_event("ui-1", session, evt, session["_notification_emitted"], reg,
                               format_process_notification, None)
    assert evt["persisted_ids"] == ["tool:tool-retry"]
    assert reg.completion_queue.qsize() == 1
    monkeypatch.setattr(server, "_emit", original_emit)
    retry = reg.completion_queue.get_nowait()
    server._notif_handle_event("ui-1", session, retry, session["_notification_emitted"], reg,
                               format_process_notification, None)
    assert len(db.rows) == 2
    assert len(session["history"]) == 2


def test_identical_tool_and_text_rows_from_separate_events_restore_by_row_identity(wired):
    _emitted, db = wired
    session, reg = _session(), _registry()
    events = [
        _event(items=[{"kind": "tool", "tool_use_id": "tool-1", "name": "TaskGet",
                       "args": {}, "result": "ok"}, {"kind": "text", "text": "ok"}]),
        _event(items=[{"kind": "tool", "tool_use_id": "tool-2", "name": "TaskGet",
                       "args": {}, "result": "ok"}, {"kind": "text", "text": "ok"}]),
    ]

    for event in events:
        server._notif_handle_event(
            "ui-1", session, event, session["_notification_emitted"], reg,
            format_process_notification, None,
        )

    restored = server._restore_sdk_display_rows(session["history"], [])
    assert len(restored) == 6
    assert len({row["_row_id"] for row in restored}) == 6
    assert all(isinstance(row_id, int) for row_id in events[0]["persisted_row_ids"] + events[1]["persisted_row_ids"])
    assert all(row["timestamp"] == 2.0 for row in restored)
    assert [row.get("tool_call_id") for row in restored if row["role"] == "tool"] == ["tool-1", "tool-2"]
    assert [row["content"] for row in restored if row.get("display_kind") == "sdk_background_result"] == ["ok", "ok"]


def test_tool_event_persistence_is_atomic_and_retry_writes_every_row_once(wired, monkeypatch):
    _emitted, db = wired
    session, reg = _session(), _registry()
    event = _event(items=[{
        "kind": "tool", "tool_use_id": "tool-atomic", "name": "TaskGet",
        "args": {}, "result": "ok",
    }])
    original_append = db.append_message

    def fail_tool_result(**kwargs):
        if kwargs.get("role") == "tool":
            raise RuntimeError("tool result write failed")
        return original_append(**kwargs)

    monkeypatch.setattr(db, "append_message", fail_tool_result)
    server._notif_handle_event(
        "ui-1", session, event, session["_notification_emitted"], reg,
        format_process_notification, None,
    )

    assert db.rows == []
    assert session["history"] == []
    assert event.get("persisted_ids", []) == []
    assert reg.completion_queue.qsize() == 1

    monkeypatch.setattr(db, "append_message", original_append)
    retry = reg.completion_queue.get_nowait()
    server._notif_handle_event(
        "ui-1", session, retry, session["_notification_emitted"], reg,
        format_process_notification, None,
    )

    assert [row["role"] for row in db.rows] == ["assistant", "tool"]
    assert len(session["history"]) == 2
    assert retry["persisted_ids"] == ["tool:tool-atomic"]
    assert retry["delivered_ids"] == ["tool:tool-atomic"]


def test_continuity_digest_omits_all_sdk_display_rows(wired):
    from agent.claude_sdk_runtime_continuity import _render_continuity_digest

    digest = _render_continuity_digest([
        {"role": "user", "content": "real question"},
        {"role": "system", "content": "woken by task", "display_kind": "session_lifecycle"},
        {"role": "user", "content": "peer message", "display_kind": "peer_message"},
        {"role": "assistant", "content": "tool result", "display_metadata": {"source": "sdk_background_result"}},
        {"role": "assistant", "content": "own answer", "display_kind": "sdk_background_result"},
    ])
    assert "USER: real question" in digest
    assert all(value not in digest for value in ("woken by task", "peer message", "tool result", "own answer"))


def test_prompt_handoff_filters_sdk_display_projections(wired, monkeypatch):
    captured = {}
    agent = types.SimpleNamespace(messages=[])

    def run_conversation(message, **kwargs):
        captured.update(kwargs)
        return {"messages": []}

    agent.run_conversation = run_conversation
    session = _session(agent)
    st = server._TurnRun(agent, None, None, True)
    st.history = [
        {"role": "user", "content": "real question"},
        {"role": "system", "content": "woken", "display_kind": "session_lifecycle"},
        {"role": "assistant", "content": "peer", "display_kind": "peer_message"},
        {"role": "tool", "content": "tool", "display_metadata": {"source": "sdk_background_result"}},
        {"role": "assistant", "content": "own answer", "display_kind": "sdk_background_result"},
    ]
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *_: (threading.Event(), types.SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server, "_get_usage", lambda *_: {})

    server._invoke_agent("ui-1", session, st, "prompt", "prompt", None, [], None, None)

    assert captured["conversation_history"] == [{"role": "user", "content": "real question"}]
    assert server._restore_sdk_display_rows(
        st.history, captured["conversation_history"] + [{"role": "assistant", "content": "new answer"}]
    ) == [
        {"role": "user", "content": "real question"},
        {"role": "system", "content": "woken", "display_kind": "session_lifecycle"},
        {"role": "assistant", "content": "peer", "display_kind": "peer_message"},
        {"role": "tool", "content": "tool", "display_metadata": {"source": "sdk_background_result"}},
        {"role": "assistant", "content": "own answer", "display_kind": "sdk_background_result"},
        {"role": "assistant", "content": "new answer"},
    ]


def test_formatter_refuses_the_event_type():
    """Any other consumer must never render the phantom 'Background process unknown exited' block."""
    assert format_process_notification(_event()) is None
