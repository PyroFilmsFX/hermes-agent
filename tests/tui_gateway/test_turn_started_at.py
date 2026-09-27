"""Tests for turn_started_at reporting on live sessions and events.

Covers:
* turn_started_at is set when a turn starts and cleared when it ends.
* peer-woken turns report turn_started_at.
* background turns report turn_started_at.
* session.active_list rows (_session_live_item) include turn_started_at.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import server


class _InlineThread:
    """Run the turn synchronously so tests observe final state."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(session_id="stored-1", model="test-model"),
        "session_key": "stored-1",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        "turn_started_at": None,
        **extra,
    }


@pytest.fixture()
def gw(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("stored-1", "desktop")
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    return db, sessions


def test_session_live_item_turn_started_at_field():
    sess = _session()
    # When idle:
    item_idle = server._session_live_item("rt-1", sess)
    assert "turn_started_at" in item_idle
    assert item_idle["turn_started_at"] is None

    # When running:
    sess["running"] = True
    start = time.time()
    server._start_inflight_turn(sess, "test prompt")
    item_running = server._session_live_item("rt-1", sess)
    assert "turn_started_at" in item_running
    assert isinstance(item_running["turn_started_at"], float)
    assert item_running["turn_started_at"] >= start


def test_turn_start_and_end_clears_turn_started_at(gw, monkeypatch):
    db, sessions = gw
    sid = "rt-turn"
    sess = _session()
    sessions[sid] = sess

    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda evt, target_sid, payload=None: emitted.append((evt, target_sid, payload)))

    from tui_gateway import prompt_turn

    # Mock agent run
    def fake_invoke(s_id, s_sess, st, prompt, run_msg, streamer, images, display_kind, display_metadata, turn_author, text):
        # Mid-turn assertion
        assert s_sess.get("running") is True
        item = server._session_live_item(s_id, s_sess)
        assert isinstance(item["turn_started_at"], float)
        assert item["turn_started_at"] > 0
        st.result = {"status": "complete"}

    monkeypatch.setattr(server, "_invoke_agent", fake_invoke)

    # Submit a prompt directly through server facade
    server._run_prompt_submit("req-1", sid, sess, "hello")

    # Post-turn assertion: running is False and turn_started_at is None
    assert sess.get("running") is False
    item = server._session_live_item(sid, sess)
    assert item["turn_started_at"] is None


def test_peer_woken_turn_reports_turn_started_at(gw, monkeypatch):
    db, sessions = gw
    sid = "rt-peer"
    sess = _session()
    sessions[sid] = sess

    seen_turn_started_at: list = []

    def fake_invoke(s_id, s_sess, st, prompt, run_msg, streamer, images, display_kind, display_metadata, turn_author, text):
        item = server._session_live_item(s_id, s_sess)
        seen_turn_started_at.append(item.get("turn_started_at"))
        st.result = {"status": "complete"}

    monkeypatch.setattr(server, "_invoke_agent", fake_invoke)
    monkeypatch.setattr(db, "peer_mailbox_mark_delivered", lambda *a, **k: True)

    from tui_gateway import session_mailbox as mb
    row = {
        "id": 123,
        "target_session_id": "stored-1",
        "from_session_id": "sender-1",
        "body": "wake up",
    }
    mb._submit_claimed(db, row, sid, via="live", ok_status="delivered", pol={"max_attempts": 3})

    assert len(seen_turn_started_at) == 1
    assert isinstance(seen_turn_started_at[0], float)
    assert seen_turn_started_at[0] > 0
    # Cleared on end
    assert server._session_live_item(sid, sess)["turn_started_at"] is None
