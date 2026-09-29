"""Owner turns win admission over peer mail and notification work on the SDK lane."""

from __future__ import annotations

import contextlib
import queue
import threading
from types import SimpleNamespace

from tui_gateway import server
from tui_gateway import session_mailbox as mailbox


def _session(sdk=None, **extra):
    agent = SimpleNamespace(api_mode="claude_agent_sdk", _claude_sdk_session=sdk) if sdk else SimpleNamespace()
    return {
        "agent": agent, "session_key": "owner-priority", "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False,
        "transport": None, "attached_images": [], "image_counter": 0, "cols": 80,
        "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", **extra,
    }


def test_owner_entry_drains_before_earlier_peer_entry(monkeypatch):
    fired = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda _r, _sid, _s, text, **_kw: fired.append(text))
    session = _session(queued_prompt={"text": "peer first", "transport": None, "display_kind": "peer_message"},
                       queued_prompts=[{"text": "owner next", "transport": None},
                                       {"text": "peer later", "transport": None, "display_kind": "peer_message"}])

    assert server._drain_queued_prompt("r", "sid", session)
    assert fired == ["owner next"]
    assert session["queued_prompt"]["text"] == "peer first"
    session["running"] = False
    assert server._drain_queued_prompt("r", "sid", session)
    assert fired == ["owner next", "peer first"]


def test_notification_claim_requeues_behind_owner_prompt():
    event = {"type": "completion", "session_id": "process-1"}
    deferred = []
    session = _session(queued_prompt={"text": "owner", "transport": None})

    assert server._notif_claim_turn(session) is False
    server._notif_dispatch_completions("sid", session, [(event, "done")],
                                       SimpleNamespace(completion_queue=queue.Queue()), deferred)
    assert deferred == [event]
    assert session["running"] is False


def test_notification_waits_for_unclaimed_sdk_turn():
    sdk = _WokenSdk()
    session = _session(sdk)
    assert server._notif_claim_turn(session) is False
    sdk.active = False
    assert server._notif_claim_turn(session) is True
    server._notif_release_turn(session)


def test_owner_rpc_waiting_for_admission_blocks_auto_starts(monkeypatch):
    session = _session()
    entered, release = threading.Event(), threading.Event()

    class _Refused:
        reason = "busy"

        def __str__(self):
            return "busy"

    def wait_for_slot(*_args):
        entered.set()
        assert release.wait(5)
        return _Refused()

    monkeypatch.setattr(server, "_ensure_active_session_slot", wait_for_slot)
    server._sessions["owner-priority"] = session
    result = {}
    thread = threading.Thread(target=lambda: result.update(server.handle_request({
        "id": "r", "method": "prompt.submit", "params": {
            "session_id": "owner-priority", "text": "owner"}})))
    try:
        thread.start()
        assert entered.wait(5)
        assert mailbox._mailbox_auto_blocked(session)
        assert server._notif_claim_turn(session) is False
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert result.get("error") and not session.get("_owner_submit_waiting")
    finally:
        release.set()
        thread.join(5)
        server._sessions.pop("owner-priority", None)


class _WokenSdk:
    def __init__(self):
        self.active = True
        self.interrupts = 0
        self.callback = None

    def woken_turn_active(self):
        return self.active

    def interrupt_woken_turn(self):
        self.interrupts += 1
        return True

    def set_idle_boundary_callback(self, callback):
        self.callback = callback

    def finish(self):
        self.active = False
        self.callback()


def test_owner_submit_preempts_unclaimed_woken_turn_and_runs_next(monkeypatch):
    sdk = _WokenSdk()
    session = _session(sdk)
    fired = []
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_a: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda _s, **_kw: contextlib.nullcontext())
    monkeypatch.setattr(server, "_start_session_work", lambda target, **_kw: target())
    monkeypatch.setattr(server, "_run_prompt_submit", lambda _r, _sid, _s, text, **_kw: fired.append(text))
    monkeypatch.setattr(mailbox, "drain_session", lambda *_a, **_kw: 0)
    server._sessions["owner-priority"] = session
    try:
        response = server.handle_request({"id": "r", "method": "prompt.submit", "params": {
            "session_id": "owner-priority", "text": "owner work"}})
        assert response["result"]["status"] == "queued"
        assert sdk.interrupts == 1 and fired == []
        sdk.finish()
        assert fired == ["owner work"]
        assert session["queued_prompt"] is None
    finally:
        server._sessions.pop("owner-priority", None)


def test_owner_redirect_during_unclaimed_woken_turn_is_queued(monkeypatch):
    sdk = _WokenSdk()
    session = _session(sdk)
    fired = []
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda _s, **_kw: contextlib.nullcontext())
    monkeypatch.setattr(server, "_start_session_work", lambda target, **_kw: target())
    monkeypatch.setattr(server, "_run_prompt_submit", lambda _r, _sid, _s, text, **_kw: fired.append(text))
    monkeypatch.setattr(mailbox, "drain_session", lambda *_a, **_kw: 0)
    server._sessions["owner-priority"] = session
    try:
        response = server.handle_request({"id": "r", "method": "session.redirect", "params": {
            "session_id": "owner-priority", "text": "redirected owner"}})
        assert response["result"]["status"] == "queued"
        assert sdk.interrupts == 1 and fired == []
        sdk.finish()
        assert fired == ["redirected owner"]
    finally:
        server._sessions.pop("owner-priority", None)


def test_stop_woken_turn_holds_mailbox_and_notifications_until_owner_submit(monkeypatch, tmp_path):
    from hermes_state import SessionDB

    sdk = _WokenSdk()
    sdk.native_peer_idle = lambda: not sdk.active
    sdk.send_peer_message = lambda _text, _origin: True
    session = _session(sdk)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("owner-priority", "desktop")
    db.create_session("sender", "desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_a: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda _s, **_kw: contextlib.nullcontext())
    monkeypatch.setattr(server, "_start_session_work", lambda target, **_kw: target())
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_a, **_kw: None)
    monkeypatch.setattr(mailbox, "schedule_drain", lambda *_a, **_kw: None)
    server._sessions["owner-priority"] = session
    try:
        sent = mailbox.send_message(target="owner-priority", body="peer", from_session_id="sender")
        assert sent["status"] == "queued"
        assert server._interrupt_session_turn("owner-priority", session, hold_auto_started=True) is False
        assert sdk.interrupts == 1 and session["_owner_stop_hold"] is True
        sdk.finish() if sdk.callback else None
        assert mailbox.drain_session("owner-priority") == 0
        assert server._notif_claim_turn(session) is False
        assert db.peer_mailbox_get(sent["message_id"])["status"] == "queued"
        response = server.handle_request({"id": "owner", "method": "prompt.submit", "params": {
            "session_id": "owner-priority", "text": "resume owner"}})
        assert response["result"]["status"] in ("queued", "streaming")
        assert session["_owner_stop_hold"] is False
        sdk.active = False
        session["running"] = False
        assert mailbox.drain_session("owner-priority") == 1
        assert db.peer_mailbox_get(sent["message_id"])["status"] == "delivered"
    finally:
        server._sessions.pop("owner-priority", None)
        db.close()
