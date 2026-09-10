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
    # In the live history AND the agent's message list, so replay and the next turn both see it.
    assert session["history"][-1]["content"] == "PEER-OK from the CLI" and session["history_version"] == 1
    assert session["agent"].messages[-1]["display_kind"] == "sdk_background_result"
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


def test_formatter_refuses_the_event_type():
    """Any other consumer must never render the phantom 'Background process unknown exited' block."""
    assert format_process_notification(_event()) is None
