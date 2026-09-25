"""Always-on cross-session messaging (cntrl carry, inventory #11): queue, resume-on-send, drain on start,
at-most-once, cost guards, and pinned residency exempt from the reapers."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def gw(monkeypatch, tmp_path):
    import tui_gateway.server as server
    from tui_gateway import session_mailbox as mb

    db = SessionDB(tmp_path / "state.db")
    for sid in ("target", "sender", "other", "third"):
        db.create_session(sid, "desktop")
    db.create_session("tg-chat", "telegram")
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 900.0, 2))
    pol = dict(mb._DEFAULTS, startup_delay_s=0, retry_interval_s=0)
    monkeypatch.setattr(mb, "policy", lambda: pol)
    mb._gate.reset()
    submits: list = []
    resumes: list = []
    refuse: dict = {"submit": None}

    def submit(rid, params):
        if refuse["submit"]:
            return {"error": {"code": 4090, "message": refuse["submit"]}}
        submits.append(params)
        return {"result": {"status": "streaming"}}

    def resume(rid, params):
        resumes.append(params)
        sid = f"rt-{len(resumes)}"
        sessions[sid] = {"session_key": params["session_id"], "profile_home": None,
                         "transport": server._detached_ws_transport}
        return {"result": {"session_id": sid}}

    monkeypatch.setitem(server._methods, "prompt.submit", submit)
    monkeypatch.setitem(server._methods, "session.resume", resume)
    drains: list = []
    monkeypatch.setattr(mb, "schedule_drain", lambda key, home=None, delay_s=0.5: drains.append(key))
    yield SimpleNamespace(server=server, mb=mb, db=db, sessions=sessions, pol=pol, submits=submits,
                          resumes=resumes, refuse=refuse, drains=drains)
    db.close()


def _send(gw, target="target", body="ping", **kw):
    return gw.mb.send_message(target=target, body=body, from_session_id=kw.pop("from_session_id", "sender"),
                              from_label=kw.pop("from_label", "manager"), **kw)


def test_live_target_gets_the_message_as_a_queued_turn(gw):
    gw.sessions["live-1"] = {"session_key": "target", "profile_home": None}
    result = _send(gw)
    assert result["status"] == "delivered-live"
    assert gw.resumes == []
    [submitted] = gw.submits
    assert submitted["session_id"] == "live-1" and submitted["queued"] is True
    assert "[peer message from manager (session sender)]" in submitted["text"] and "ping" in submitted["text"]
    assert gw.db.peer_mailbox_get(result["message_id"])["status"] == "delivered"


def _claude_live_session(send_peer_message, *, running=False):
    agent = SimpleNamespace(api_mode="claude_agent_sdk", _claude_sdk_session=SimpleNamespace(
        send_peer_message=send_peer_message))
    return {"session_key": "target", "profile_home": None, "agent": agent, "running": running}


def test_idle_live_claude_target_uses_native_peer_injection(gw):
    received = []
    gw.sessions["live-claude"] = _claude_live_session(
        lambda body, origin: received.append((body, origin)) or True)

    result = _send(gw)

    assert result["status"] == "delivered-native"
    assert gw.submits == []
    assert received == [("ping", {
        "kind": "peer", "subkind": "peer-send-message", "from": "manager",
        "fromSession": "sender", "msg_id": str(result["message_id"]), "body": "ping",
    })]
    row = gw.db.peer_mailbox_get(result["message_id"])
    assert row["status"] == "delivered" and row["delivered_via"] == "native"


def test_busy_live_claude_target_uses_sdk_boundary_queue_once(gw):
    received = []
    gw.sessions["live-claude"] = _claude_live_session(
        lambda body, origin: received.append((body, origin)) or True, running=True)

    result = _send(gw)
    assert result["status"] == "delivered-native"
    assert len(received) == 1 and gw.submits == []
    assert gw.mb.drain_session("target") == 0
    assert gw.db.peer_mailbox_get(result["message_id"])["delivered_via"] == "native"


def test_native_claude_injection_failure_falls_back_to_live_submit_once(gw):
    def fail(_body, _origin):
        raise RuntimeError("SDK unavailable")

    gw.sessions["live-claude"] = _claude_live_session(fail)
    result = _send(gw)

    assert result["status"] == "delivered-live"
    assert len(gw.submits) == 1
    assert "ping" in gw.submits[0]["text"]
    assert gw.db.peer_mailbox_get(result["message_id"])["delivered_via"] == "live"


def test_native_peer_hint_requires_live_claude_sender_and_target(gw, monkeypatch):
    from agent import claude_sdk_runtime_continuity

    sender = SimpleNamespace(api_mode="claude_agent_sdk", _claude_sdk_session=object())
    gw.sessions["sender-live"] = {"session_key": "sender", "profile_home": None, "agent": sender}
    gw.sessions["target-live"] = _claude_live_session(lambda _body, _origin: True)
    monkeypatch.setattr(claude_sdk_runtime_continuity, "_sdk_session_name", lambda _agent: "hermes:manager desk")

    result = _send(gw)
    assert result["native_peer"] == "hermes:manager desk"

    gw.sessions["target-live"]["agent"].api_mode = "openai"
    without_native_target = _send(gw, body="ordinary target")
    assert "native_peer" not in without_native_target

    gw.sessions["sender-live"]["agent"].api_mode = "openai"
    gw.sessions["target-live"]["agent"].api_mode = "claude_agent_sdk"
    without_native_sender = _send(gw, body="ordinary sender")
    assert "native_peer" not in without_native_sender


def test_dead_target_is_resumed_on_send_and_delivered(gw):
    result = _send(gw)
    assert result["status"] == "resumed-and-delivered"
    assert [r["session_id"] for r in gw.resumes] == ["target"]
    assert gw.resumes[0]["_peer_mailbox_internal"] is True, "the internal resume must not re-trigger a drain"
    assert gw.submits[0]["session_id"] == "rt-1"
    assert gw.sessions["rt-1"]["_peer_mailbox_woken"] is True
    row = gw.db.peer_mailbox_get(result["message_id"])
    assert row["status"] == "delivered" and row["delivered_via"] == "resume"
    assert gw.drains == ["target"], "other mail queued for the woken session rides along"


def test_queued_when_resume_is_off_then_drained_when_the_session_starts(gw):
    gw.pol["resume_on_send"] = False
    first = _send(gw, body="one")
    second = _send(gw, body="two")
    assert first["status"] == second["status"] == "queued"
    assert gw.submits == [] and gw.resumes == []
    # The owner opens the session: session.resume registers it, the hook drains the mailbox into it.
    gw.sessions["opened"] = {"session_key": "target", "profile_home": None}
    ctx = SimpleNamespace(lazy=False, found={"id": "target", "pinned": 0}, target="target", profile_home=None)
    gw.mb.after_session_resume(ctx, {"session_id": "target"}, {"result": {"session_id": "opened"}})
    assert gw.drains == ["target"]
    assert gw.mb.drain_session("target") == 2
    assert [s["text"].split("\n")[1] for s in gw.submits] == ["one", "two"], "arrival order preserved"
    assert gw.mb.drain_session("target") == 0, "a drained row is never delivered twice"


def test_request_id_retry_is_deduped_and_reports_the_original_outcome(gw):
    first = _send(gw, request_id="r-1")
    retry = _send(gw, request_id="r-1")
    assert first["status"] == "resumed-and-delivered"
    assert retry["duplicate"] is True and retry["status"] == "resumed-and-delivered"
    assert retry["message_id"] == first["message_id"]
    assert len(gw.submits) == 1 and len(gw.resumes) == 1


def test_claimed_row_cannot_be_delivered_by_a_concurrent_drain(gw):
    gw.pol["resume_on_send"] = False
    queued = _send(gw)
    assert gw.db.peer_mailbox_claim(queued["message_id"], "other-process:abcd")
    gw.sessions["opened"] = {"session_key": "target", "profile_home": None}
    assert gw.mb.drain_session("target") == 0
    assert gw.submits == []


def test_refused_submit_requeues_then_fails_after_max_attempts(gw):
    gw.pol["max_attempts"] = 2
    gw.sessions["live-1"] = {"session_key": "target", "profile_home": None}
    gw.refuse["submit"] = "active session limit reached"
    first = _send(gw)
    assert first["status"] == "queued" and "active session limit" in first["detail"]
    assert gw.mb.drain_session("target") == 0
    row = gw.db.peer_mailbox_get(first["message_id"])
    assert row["status"] == "failed" and row["attempts"] == 2


def test_stale_claim_of_a_dead_process_fails_instead_of_redelivering(gw, monkeypatch):
    gw.pol["resume_on_send"] = False
    queued = _send(gw)
    assert gw.db.peer_mailbox_claim(queued["message_id"], "999999:dead")
    import tui_gateway.session_task_handoff as handoff
    monkeypatch.setattr(handoff, "_owner_process_alive", lambda owner: not owner.startswith("999999"))
    assert gw.mb.recover_stale_claims(gw.db) == 1
    row = gw.db.peer_mailbox_get(queued["message_id"])
    assert row["status"] == "failed" and "outcome unknown" in row["last_error"]
    gw.sessions["opened"] = {"session_key": "target", "profile_home": None}
    assert gw.mb.drain_session("target") == 0 and gw.submits == []


def test_live_claim_of_a_sibling_process_is_left_alone(gw):
    gw.pol["resume_on_send"] = False
    queued = _send(gw)
    assert gw.db.peer_mailbox_claim(queued["message_id"], f"{os.getppid()}:sibling")
    assert gw.mb.recover_stale_claims(gw.db) == 0
    assert gw.db.peer_mailbox_get(queued["message_id"])["status"] == "claimed"


def test_resume_concurrency_cap_defers_extra_wakes(gw):
    gw.pol["max_concurrent_resumes"] = 1
    assert _send(gw, target="target")["status"] == "resumed-and-delivered"
    capped = _send(gw, target="other")
    assert capped["status"] == "queued" and "concurrency cap" in capped["detail"]
    assert [r["session_id"] for r in gw.resumes] == ["target"]
    # Once the woken session is reaped, the retry pass may wake the next one.
    gw.sessions.clear()
    assert gw.mb.drain_pending() == 1
    assert [r["session_id"] for r in gw.resumes] == ["target", "other"]


def test_per_target_rate_limit_blocks_rapid_rewakes(gw):
    assert _send(gw, body="a")["status"] == "resumed-and-delivered"
    gw.sessions.clear()  # the woken session was reaped right after its turn
    again = _send(gw, body="b")
    assert again["status"] == "queued" and "rate-limited" in again["detail"]
    assert len(gw.resumes) == 1


def test_failed_resume_is_reported_and_requeued(gw, monkeypatch):
    monkeypatch.setitem(gw.server._methods, "session.resume",
                        lambda rid, params: {"error": {"code": 5000, "message": "resume blew up"}})
    result = _send(gw)
    assert result["status"] == "queued" and "resume blew up" in result["detail"]
    assert gw.db.peer_mailbox_get(result["message_id"])["attempts"] == 1


def test_unknown_self_and_platform_targets_fail_without_queueing(gw):
    assert _send(gw, target="nope")["status"] == "failed"
    assert _send(gw, target="sender")["status"] == "failed"
    assert _send(gw, target="tg-chat")["status"] == "failed"
    assert gw.db.peer_mailbox_pending() == []


def test_title_and_rendered_peer_name_resolve_to_the_stored_id(gw):
    gw.db.set_session_title("target", "manager desk")
    gw.sessions["live-1"] = {"session_key": "target", "profile_home": None}
    assert _send(gw, target="hermes:manager desk")["target_session_id"] == "target"


def test_disabled_policy_refuses(gw):
    gw.pol["enabled"] = False
    assert _send(gw)["status"] == "failed"


def test_startup_drain_resumes_targets_with_queued_mail(gw):
    gw.pol["resume_on_send"] = False
    _send(gw, body="while you were out")
    gw.mb._gate.reset()
    assert gw.mb.drain_pending() == 1
    assert [r["session_id"] for r in gw.resumes] == ["target"]
    assert "while you were out" in gw.submits[0]["text"]


def test_scoped_rpc_uses_the_capability_owner_as_sender(gw, monkeypatch):
    from agent.transports import hermes_gateway_session_bridge as bridge
    gw.sessions["owner-rt"] = {"session_key": "sender", "profile_home": None, "pending_title": "manager"}
    gw.sessions["live-1"] = {"session_key": "target", "profile_home": None}
    monkeypatch.setattr(bridge, "authorize_scoped_capability",
                        lambda token: bridge.ScopedSessionCapability(token, "owner-rt", 0) if token == "cap" else None)
    monkeypatch.setattr(gw.server, "_ok", lambda rid, result: {"result": result})
    monkeypatch.setattr(gw.server, "_err", lambda rid, code, msg, data=None: {"error": {"code": code, "message": msg}})
    ok = gw.mb.send_rpc(1, {"target": "target", "body": "hi", "_session_spawn_capability": "cap",
                            "from_session_id": "forged"})
    assert ok["result"]["status"] == "delivered-live"
    assert "(session sender)" in gw.submits[0]["text"] and "forged" not in gw.submits[0]["text"]
    assert gw.mb.send_rpc(1, {"target": "target", "body": "hi", "_session_spawn_capability": "bad"})["error"]["code"] == 4403


# ── B-lite residency ─────────────────────────────────────────────────────


def test_pinned_resident_session_is_exempt_from_idle_and_lru_reaping(gw, monkeypatch):
    server = gw.server
    detached = {"session_key": "target", "transport": server._detached_ws_transport,
                "last_active": 0.0, "created_at": 0.0}
    resident = dict(detached, pinned_resident=True)
    assert server._session_is_lru_evictable("a", detached)
    assert server._session_is_evictable("a", detached, now=10 ** 9)
    assert not server._session_is_lru_evictable("b", resident)
    assert not server._session_is_evictable("b", resident, now=10 ** 9)
    gw.pol["pinned_resident"] = False  # owner switched residency off: pinned sessions reap normally again
    assert server._session_is_lru_evictable("b", resident)


def test_pinned_resident_session_is_never_armed_for_ws_orphan_reap(gw, monkeypatch):
    server = gw.server
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20.0)
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    gw.sessions["res"] = {"session_key": "target", "transport": server._detached_ws_transport, "pinned_resident": True}
    server._schedule_ws_orphan_reap("res")
    assert "res" not in server._pending_ws_reaps


def test_resume_of_a_pinned_row_stamps_residency(gw):
    gw.sessions["opened"] = {"session_key": "target", "profile_home": None, "_peer_mailbox_woken": True}
    ctx = SimpleNamespace(lazy=False, found={"id": "target", "pinned": 1}, target="target", profile_home=None)
    gw.mb.after_session_resume(ctx, {"_peer_mailbox_internal": True}, {"result": {"session_id": "opened"}})
    assert gw.sessions["opened"]["pinned_resident"] is True
    assert "_peer_mailbox_woken" not in gw.sessions["opened"], "a resident never counts against the wake cap"
    assert gw.drains == []


def test_startup_revives_pinned_sessions_up_to_the_cap(gw):
    for sid in ("target", "other", "third"):
        gw.db.set_session_pinned(sid, True)
    gw.pol["max_resident_sessions"] = 2
    revived = gw.mb.revive_resident_sessions()
    assert len(revived) == 2 and len(gw.resumes) == 2
    assert all(s.get("pinned_resident") for s in gw.sessions.values())


def test_startup_cold_resumes_fresh_unpinned_markers_once(gw, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker, record_turn_start

    continued = []
    def resume(target, profile_home):
        sid = f"cold-{target}"
        gw.sessions[sid] = {"session_key": target, "profile_home": profile_home}
        continued.append(target)
        return sid, ""

    monkeypatch.setattr(gw.mb, "_resume", resume)
    record_turn_start(gw.server._hermes_home, "target", "finish this")
    assert gw.mb.resume_interrupted_sessions() == ["target"]
    assert continued == ["target"]
    assert gw.sessions["cold-target"]["_peer_mailbox_woken"] is True
    assert read_turn_marker(gw.server._hermes_home, "target") is not None
    assert gw.mb.resume_interrupted_sessions() == []
    assert continued == ["target"], "a live resumed session owns the marker's continuation"


def test_startup_clears_stale_and_over_attempt_markers(gw, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker, record_turn_start

    monkeypatch.setattr(gw.server, "_auto_continue_config", lambda: (True, 0.0, 2))
    record_turn_start(gw.server._hermes_home, "target", "old", attempts=0)
    record_turn_start(gw.server._hermes_home, "other", "loop", attempts=2)
    assert gw.mb.resume_interrupted_sessions() == []
    assert read_turn_marker(gw.server._hermes_home, "target") is None
    assert read_turn_marker(gw.server._hermes_home, "other") is None
    assert gw.resumes == []


def test_startup_interrupted_resumes_obey_concurrency_cap(gw, monkeypatch):
    from tui_gateway.turn_marker import record_turn_start

    for key in ("target", "other", "third"):
        gw.db.create_session(key, "desktop")
        record_turn_start(gw.server._hermes_home, key, f"resume {key}")
    gw.pol["max_concurrent_resumes"] = 2
    monkeypatch.setattr(gw.mb, "_resume", lambda target, home: (
        (gw.sessions.setdefault(f"cold-{target}", {"session_key": target, "profile_home": home}) and f"cold-{target}"), ""))
    resumed = gw.mb.resume_interrupted_sessions()
    assert len(resumed) == 2
    assert sum(bool(s.get("_peer_mailbox_woken")) for s in gw.sessions.values()) == 2
    released = next(key for key, session in gw.sessions.items() if session.get("_peer_mailbox_woken"))
    gw.sessions.pop(released)
    assert len(gw.mb.resume_interrupted_sessions()) == 1
    assert sum(bool(s.get("_peer_mailbox_woken")) for s in gw.sessions.values()) == 2


def test_mailbox_resume_emits_woken_lifecycle_row(gw):
    """Cold resume emits a lifecycle woken row with source: peer-mailbox."""
    res = _send(gw, target="target", body="wake up", from_session_id="sender", from_label="alice")
    assert res["status"] == gw.mb.STATUS_RESUMED

    # Check database messages for target
    msgs = gw.db.get_messages("target")
    lifecycle_rows = [m for m in msgs if m.get("display_kind") == "session_lifecycle"]
    assert len(lifecycle_rows) == 1
    row = lifecycle_rows[0]
    meta = row.get("display_metadata") or {}
    assert meta.get("event") == "woken"
    assert meta.get("source") == "peer-mailbox"
    assert meta.get("by") == "alice"
    assert "woken by peer message: alice" in row.get("content", "")


def test_peer_mailbox_settled_emitted(gw, monkeypatch):
    """peer_mailbox.settled is emitted when rows change state."""
    events = []
    monkeypatch.setattr(gw.server, "_emit", lambda ev, sid, payload=None: events.append((ev, sid, payload)))
    monkeypatch.setattr(gw.server, "_broadcast_global_event", lambda ev, payload=None: events.append((ev, "broadcast", payload)))

    res = _send(gw, target="target", body="hello", from_session_id="sender")
    settled = [p for ev, sid, p in events if ev == "peer_mailbox.settled"]
    assert len(settled) >= 1
    last = settled[-1]
    assert last["msg_id"] == str(res["message_id"])
    assert last["status"] == gw.mb.STATUS_RESUMED


def test_peer_mailbox_rpc_list_retry_cancel(gw):
    """peer_mailbox.list, retry, and cancel RPC methods."""
    gw.pol["resume_on_send"] = False
    send_res = _send(gw, target="target", body="queued mail", from_session_id="sender")
    msg_id = send_res["message_id"]

    # 1. list
    list_res = gw.server._methods["peer_mailbox.list"]("r1", {"session_id": "target"})
    messages = list_res["result"]["messages"]
    assert any(m["id"] == msg_id for m in messages)

    # 2. cancel queued message
    wrong_cancel = gw.server._methods["peer_mailbox.cancel"](
        "r2-wrong", {"message_id": msg_id, "session_id": "unrelated"}
    )
    assert "error" in wrong_cancel
    assert gw.db.peer_mailbox_get(msg_id)["status"] == "queued"
    cancel_res = gw.server._methods["peer_mailbox.cancel"](
        "r2", {"message_id": msg_id, "session_id": "target"}
    )
    assert cancel_res["result"]["cancelled"] is True
    row = gw.db.peer_mailbox_get(msg_id)
    assert row["status"] == "failed"

    # Cannot cancel already-failed message
    err_res = gw.server._methods["peer_mailbox.cancel"](
        "r3", {"message_id": msg_id, "session_id": "target"}
    )
    assert "error" in err_res

    # 3. retry a queued message
    send_res2 = _send(gw, target="target", body="queued mail 2", from_session_id="sender")
    msg_id2 = send_res2["message_id"]
    gw.pol["resume_on_send"] = True
    wrong_retry = gw.server._methods["peer_mailbox.retry"](
        "r4-wrong", {"message_id": msg_id2, "session_id": "unrelated"}
    )
    assert "error" in wrong_retry
    assert gw.db.peer_mailbox_get(msg_id2)["status"] == "queued"
    retry_res = gw.server._methods["peer_mailbox.retry"](
        "r4", {"message_id": msg_id2, "session_id": "target"}
    )
    assert retry_res["result"]["status"] in (gw.mb.STATUS_RESUMED, gw.mb.STATUS_DELIVERED_LIVE)


def test_peer_mailbox_list_requires_session_scope_and_filters_both_directions(gw):
    gw.pol["resume_on_send"] = False
    to_target = _send(gw, target="target", body="target mail", from_session_id="sender")
    unrelated = _send(gw, target="other", body="other mail", from_session_id="third")

    unscoped = gw.server._methods["peer_mailbox.list"]("r1", {})
    assert "error" in unscoped

    target_rows = gw.server._methods["peer_mailbox.list"](
        "r2", {"session_id": "target"}
    )["result"]["messages"]
    sender_rows = gw.server._methods["peer_mailbox.list"](
        "r3", {"session_id": "sender"}
    )["result"]["messages"]
    assert [row["id"] for row in target_rows] == [to_target["message_id"]]
    assert [row["id"] for row in sender_rows] == [to_target["message_id"]]
    assert all(row["id"] != unrelated["message_id"] for row in target_rows + sender_rows)


def test_peer_mailbox_list_clamps_limit(gw, monkeypatch):
    import tui_gateway.session_mailbox as mailbox

    received = []
    monkeypatch.setattr(
        mailbox, "list_messages",
        lambda **kwargs: received.append(kwargs["limit"]) or [],
    )
    for index, requested in enumerate((-5, 999)):
        gw.server._methods["peer_mailbox.list"](
            f"r{index}", {"session_id": "target", "limit": requested}
        )
    assert received == [1, 200]


def test_session_set_pinned_persists_across_restart(gw):
    """session.set_pinned updates DB and survives revival."""
    gw.db.create_session("pinned-sess", "desktop")

    # Set pinned via RPC
    res = gw.server._methods["session.set_pinned"]("r1", {"session_id": "pinned-sess", "pinned": True})
    assert res["result"]["pinned"] is True

    # Check DB persistence
    row = gw.db.get_session("pinned-sess")
    assert row["pinned"] == 1

    # Candidate list for residency
    candidates = gw.db.list_resident_session_candidates()
    assert "pinned-sess" in candidates

    # Revive resident sessions picks it up
    revived = gw.mb.revive_resident_sessions()
    assert "pinned-sess" in revived

    # Unpin via RPC
    res2 = gw.server._methods["session.set_pinned"]("r2", {"session_id": "pinned-sess", "pinned": False})
    assert res2["result"]["pinned"] is False
    assert gw.db.get_session("pinned-sess")["pinned"] == 0



def test_prompt_submit_honors_peer_message_kind_only_for_the_in_process_mailbox():
    """A client request always runs with a transport bound; only the mailbox submits with none.
    A client must not be able to mint a peer_message card "from" another session."""
    from tui_gateway.methods_prompt import _submit_display
    from tui_gateway.transport import bind_transport, reset_transport

    params = {"display_kind": "peer_message", "display_metadata": {"direction": "in", "peer": "hermes:x"}}
    token = bind_transport(None)
    try:
        assert _submit_display(params) == ("peer_message", params["display_metadata"])
    finally:
        reset_transport(token)
    token = bind_transport(object())
    try:
        assert _submit_display(params) == (None, None)
        assert _submit_display({"display_kind": "hidden"}) == ("hidden", None)
    finally:
        reset_transport(token)
