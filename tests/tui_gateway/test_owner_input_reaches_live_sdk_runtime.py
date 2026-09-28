"""Owner input typed while a Claude Agent SDK worker is busy reaches that live runtime.

Live repro (2026-09-28, session 20260924_200237_d5276f): after a backend restart a peer-mailbox wake
started a continuous-execution turn on the SDK lane. The owner typed two lines into the desktop tab; the
composer sent them as ``session.redirect`` (its busy path). ``AIAgent.redirect()`` had no Claude SDK branch —
the SDK runs the whole turn, so Hermes never enters the ``_model_request_active`` bracket or
``_executing_tools`` — and always answered False. The gateway returned ``rejected`` (nothing logged), and the
desktop parked the text in its client-only queue, which only drains when that tab sees the turn end: an hour
later for a continuous worker. Peer messages to the same session kept arriving because the mailbox has its own
native SDK injection.

Contracts pinned here:

(a) text typed while the worker is busy is delivered to the live turn as a steer, or — when the turn cannot
    take it in place — queued on the LIVE ui_session and dispatched as a user turn when that turn ends;
(b) after a peer-wake resume following a backend restart, the desktop's stale-id redirect recovers (404 →
    ``session.resume`` of the stored id) onto the SAME live runtime the peer wake started, and the redirect
    reaches that runtime's SDK turn.
"""

from __future__ import annotations

import threading
import types

import pytest

from agent.interrupt_control import InterruptControlMixin
from tui_gateway import server


class _SdkWorker(InterruptControlMixin):
    """The Claude Agent SDK lane as the gateway sees it: the SDK owns the whole turn, so Hermes never sets
    ``_model_request_active`` or ``_executing_tools``. ``accepts`` is what the live SDK session's steer
    answers (False: no claimed turn to steer into yet, or its terminal result already committed)."""

    api_mode = "claude_agent_sdk"
    _supports_active_turn_redirect = True

    def __init__(self, *, accepts: bool = True) -> None:
        self._interrupt_requested = False
        self._interrupt_message = None
        self._executing_tools = False
        self._model_request_active = threading.Event()
        self._pending_redirect_lock = threading.Lock()
        self._pending_redirect = None
        self._pending_steer_lock = threading.Lock()
        self._pending_steer = None
        self.steered: list[str] = []
        self._claude_sdk_session = types.SimpleNamespace(
            steer=lambda text: self.steered.append(text) or accepts)


def _session(agent, **extra):
    return {
        "agent": agent,
        "session_key": "20260924_200237_d5276f",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def _redirect(sid: str, text: str) -> dict:
    return server.handle_request(
        {"id": "1", "method": "session.redirect", "params": {"session_id": sid, "text": text}})


@pytest.fixture
def live(monkeypatch):
    added: list[str] = []

    def register(sid: str, session: dict) -> dict:
        with server._sessions_lock:
            server._sessions[sid] = session
        added.append(sid)
        return session

    yield register
    with server._sessions_lock:
        for sid in added:
            server._sessions.pop(sid, None)


# ── (a) owner types while the worker is busy ────────────────────────────────


def test_sdk_agent_redirect_is_the_sdks_own_steer():
    worker = _SdkWorker()
    assert worker.redirect("approve #288") is True
    assert worker.steered == ["approve #288"]
    # Never the Hermes stash: that strands on this lane until the turn finalizer hands it back.
    assert worker._pending_steer is None and worker._pending_redirect is None


def test_sdk_agent_redirect_declines_while_a_stop_is_pending():
    worker = _SdkWorker()
    worker._interrupt_requested = True
    assert worker.redirect("approve #288") is False
    assert worker.steered == []


def test_owner_redirect_reaches_the_busy_sdk_turn(live):
    worker = _SdkWorker()
    session = live("e8199749", _session(worker, running=True))
    session["inflight_turn"] = {"user": "<cross-session-message ...>", "assistant": ""}

    resp = _redirect("e8199749", "approve #288")

    assert resp["result"] == {"status": "redirected", "text": "approve #288"}
    assert worker.steered == ["approve #288"]
    assert session["inflight_turn"]["corrections"] == ["approve #288"]
    assert session.get("queued_prompt") is None


def test_owner_redirect_the_turn_cannot_take_is_queued_on_the_live_runtime_and_runs_when_it_ends(
        live, monkeypatch):
    worker = _SdkWorker(accepts=False)
    session = live("e8199749", _session(worker, running=True))

    resp = _redirect("e8199749", "approve #289")

    # Not "rejected": that parked the words in a client-only queue keyed to one tab's view of the turn.
    assert resp["result"] == {"status": "queued", "text": "approve #289"}
    assert session["queued_prompt"]["text"] == "approve #289"

    fired: list[str] = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **kw: fired.append((sid, text)))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _s: False)
    session["running"] = False  # the peer-started turn settles
    assert server._drain_queued_prompt("r2", "e8199749", session) is True
    assert fired == [("e8199749", "approve #289")]


def test_owner_redirect_to_an_idle_sdk_session_is_still_rejected_not_queued(live):
    # No live turn: nothing to redirect and no phantom queued turn — the client sends it as a normal prompt.
    worker = _SdkWorker(accepts=False)
    session = live("e8199749", _session(worker, running=False))

    resp = _redirect("e8199749", "hello")

    assert resp["result"] == {"status": "rejected", "text": "hello"}
    assert session.get("queued_prompt") is None


def test_busy_prompt_submit_to_sdk_worker_steers_without_killing_the_turn(live, monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    worker = _SdkWorker()

    def _no_hard_interrupt(*_a, **_k):
        raise AssertionError("a typed correction must not hard-interrupt a live SDK worker")

    worker.interrupt = _no_hard_interrupt
    session = live("e8199749", _session(worker, running=True))

    resp = server._handle_busy_submit("r1", "e8199749", session, "approve #288", "ws-desktop")

    assert resp["result"]["status"] == "redirected"
    assert worker.steered == ["approve #288"]
    assert session.get("queued_prompt") is None


# ── (b) peer-wake resume after a backend restart ────────────────────────────


class _DB:
    def __init__(self, *_a, **_k):
        pass

    def close(self):
        pass

    def get_session(self, target):
        return {"id": target, "cwd": ""} if target == "20260924_200237_d5276f" else None

    def get_session_by_title(self, _target):
        return None

    def resolve_resume_session_id(self, target):
        return target

    def reopen_session(self, _target):
        pass

    def get_resume_conversations(self, _target):
        return ([], [])

    def get_ancestor_display_prefix(self, _target):
        return []

    def get_messages_as_conversation(self, _target, **_kwargs):
        return []


@pytest.fixture
def restarted_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_state_registry.acquire", _DB)
    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_profile_home", lambda _p: None)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(server, "_child_run_active", lambda _key: False)
    monkeypatch.setattr(server, "_live_session_payload",
                        lambda sid, session, **_k: {"session_id": sid, "message_count": 0, "messages": [],
                                                    "info": {}, "running": bool(session.get("running"))})


def test_desktop_redirect_after_peer_wake_recovers_onto_the_same_live_sdk_runtime(restarted_gateway, live):
    # The restarted backend has no df1aceee (the tab's pre-restart runtime). The peer mailbox woke the
    # stored session under its own ui_session, bound to the detached sink, and its turn is running.
    worker = _SdkWorker()
    live("e8199749", _session(worker, running=True, profile_home=None, _peer_mailbox_woken=True,
                              transport=server._detached_ws_transport, last_active=0.0))

    stale = _redirect("df1aceee", "approve #288")
    assert "error" in stale, stale  # the desktop's resolver resumes the stored id and retries once

    resumed = server.handle_request({"id": "2", "method": "session.resume",
                                     "params": {"session_id": "20260924_200237_d5276f"}})
    assert "error" not in resumed, resumed
    assert resumed["result"]["session_id"] == "e8199749"  # the live peer-woken runtime, not a new idle one
    assert resumed["result"]["running"] is True

    retried = _redirect(resumed["result"]["session_id"], "approve #288")

    assert retried["result"] == {"status": "redirected", "text": "approve #288"}
    assert worker.steered == ["approve #288"]
