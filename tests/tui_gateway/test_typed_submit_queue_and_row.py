"""A typed turn keeps its display kind and metadata through the busy queue (F5) and on the row written at
submit time (F6).

Owner-forward (``HE-OWNER-FORWARD-DESIGN-2026-09-26`` §1.3 F5/F6, tests P-0c and P-0f) needs both, and so does
``peer_message``: before this, a peer turn that hit a busy non-SDK target drained as a bare user bubble, and a
crash between the submit-time write and the turn's own persist lost the card's provenance."""

import threading
import types

from hermes_state import SessionDB
from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key", "history": [], "history_lock": threading.Lock(), "history_version": 0,
        "running": False, "transport": None, "attached_images": [], **extra,
    }


PEER_META = {"kind": "peer_message", "from_session_id": "s-a", "body": "hello"}


# ── F5: busy queue ────────────────────────────────────────────────────────────────────────────


def test_p0c_busy_submit_queues_display_kind_and_metadata(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    session = _session(running=True)

    resp = server._handle_busy_submit(
        "r1", "sid", session, "from a peer", None, queued=True,
        display_kind="peer_message", display_metadata=dict(PEER_META))

    assert resp["result"]["status"] == "queued"
    assert session["queued_prompt"] == {
        "text": "from a peer", "transport": None, "display_kind": "peer_message", "display_metadata": PEER_META}


def test_p0c_drain_passes_display_kind_and_metadata_to_the_turn(monkeypatch):
    fired = {}
    monkeypatch.setattr(
        server, "_run_prompt_submit", lambda rid, sid, session, text, **kwargs: fired.update(text=text, **kwargs))
    session = _session(queued_prompt={
        "text": "from a peer", "transport": None, "display_kind": "peer_message", "display_metadata": dict(PEER_META)})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert fired["text"] == "from a peer"
    assert fired["display_kind"] == "peer_message"
    assert fired["display_metadata"] == PEER_META


def test_p0c_drain_compute_host_passes_display_kind_and_metadata(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(
        server, "_submit_prompt_to_compute_host",
        lambda rid, sid, session, text, **kwargs: captured.update(kwargs) or {"result": {"status": "started"}})
    session = _session(queued_prompt={
        "text": "x", "transport": None, "display_kind": "peer_message", "display_metadata": dict(PEER_META)})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert captured["display_kind"] == "peer_message"
    assert captured["display_metadata"] == PEER_META


def test_p0c_typed_envelopes_never_merge_with_plain_text():
    """Plain text-only arrivals share a slot; a typed one would lose its kind (or give it to the user's text)."""
    session = _session()
    server._enqueue_prompt(session, "A", "ws-1")
    server._enqueue_prompt(session, "B", None, display_kind="peer_message", display_metadata=dict(PEER_META))
    server._enqueue_prompt(session, "C", "ws-1")

    assert session["queued_prompt"] == {"text": "A", "transport": "ws-1"}
    assert session["queued_prompts"] == [
        {"text": "B", "transport": None, "display_kind": "peer_message", "display_metadata": PEER_META},
        {"text": "C", "transport": "ws-1"},
    ]


def test_p0c_typed_copy_of_the_inflight_text_is_kept():
    """The self-duplicate scrub is for the user's own re-send; a typed turn with the same words is another act."""
    session = _session(inflight_turn={"user": "ship it"})
    server._enqueue_prompt(session, "ship it", None, display_kind="peer_message", display_metadata=dict(PEER_META))

    assert session["queued_prompt"]["display_kind"] == "peer_message"
    server._drop_queued_duplicates_of_inflight_user(session)
    assert session["queued_prompt"]["text"] == "ship it"


def test_p0c_metadata_without_a_kind_is_not_queued():
    """A plain submit's ``title_preview`` only feeds the first-turn titler; a busy session is already titled, and
    carrying it would stop ordinary busy text from merging."""
    session = _session()
    server._enqueue_prompt(session, "A", "ws-1", display_metadata={"title_preview": "Pasted 5000 chars"})
    server._enqueue_prompt(session, "B", "ws-1")

    assert session["queued_prompt"] == {"text": "A\n\nB", "transport": "ws-1"}


def test_p0c_prompt_submit_busy_path_carries_the_kind(monkeypatch):
    """End to end through ``prompt.submit``: the in-process peer mailbox (no bound transport) hits a busy target."""
    from tui_gateway.transport import bind_transport, reset_transport

    session = _session(running=True)
    monkeypatch.setitem(server._sessions, "sid-busy", session)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)
    monkeypatch.setattr(server, "_reattach_refusal", lambda rid, sid, session: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    token = bind_transport(None)
    try:
        resp = server._methods["prompt.submit"]("r", {
            "session_id": "sid-busy", "text": "hello", "queued": True,
            "display_kind": "peer_message", "display_metadata": dict(PEER_META)})
    finally:
        reset_transport(token)

    assert resp["result"]["status"] == "queued", resp
    assert session["queued_prompt"]["display_kind"] == "peer_message"
    assert session["queued_prompt"]["display_metadata"] == PEER_META


# ── F6: submit-time row ───────────────────────────────────────────────────────────────────────


def _desktop_session(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in resp, resp
    return resp["result"]["session_id"], resp["result"]["stored_session_id"]


def test_p0f_submit_time_row_carries_display_metadata(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    try:
        with session["history_lock"]:
            session["running"] = True
            server._start_inflight_turn(session, "hello", display_kind="peer_message")
        assert server._persist_session_row_for_submit(
            "rid", session, "hello", "peer_message", dict(PEER_META)) is None
        # No turn has run: the row as written at submit time already holds the provenance.
        rows = db.get_messages(key)
        assert [(r["role"], r["display_kind"]) for r in rows] == [("user", "peer_message")]
        assert rows[0]["display_metadata"] == PEER_META
        assert session["_submit_user_row"]["display_metadata"] == PEER_META
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_p0f_prompt_submit_writes_metadata_at_submit(monkeypatch, tmp_path):
    from tui_gateway.transport import bind_transport, reset_transport

    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *args: None)
    token = bind_transport(None)
    try:
        resp = server._methods["prompt.submit"]("p", {
            "session_id": sid, "text": "hello", "display_kind": "peer_message",
            "display_metadata": dict(PEER_META)})
        assert resp["result"]["status"] == "streaming", resp
        rows = db.get_messages(key)
        assert rows[-1]["display_kind"] == "peer_message"
        assert rows[-1]["display_metadata"] == PEER_META
    finally:
        reset_transport(token)
        server._sessions.pop(sid, None)
        db.close()
