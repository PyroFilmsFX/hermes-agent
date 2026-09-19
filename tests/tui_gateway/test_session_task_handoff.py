from __future__ import annotations

import json
import multiprocessing
import os
from types import SimpleNamespace

from agent.transports import hermes_gateway_session_bridge as bridge
from tui_gateway import session_task_handoff as handoff


def test_handoff_calls_create_title_submit_once_and_is_idempotent(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    caller = {
        "session_key": "owner-stored",
        "cwd": str(root),
        "profile_home": None,
        "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="claude-sonnet", provider="claude-agent-sdk"),
    }
    child = {"session_key": "child-stored", "cwd": str(root), "running": True, "agent": None, "profile_home": None}
    calls = []

    monkeypatch.setattr(handoff, "_canonical_roots", lambda _: (str(root),))
    monkeypatch.setattr(handoff, "_find_caller", lambda _: ("owner-runtime", caller))
    monkeypatch.setattr(handoff, "_policy", lambda *_: {"enabled": True, "max_children_per_root": 3, "max_depth": 2, "rate_per_minute": 5})
    monkeypatch.setattr(handoff, "_receipt_home", lambda _: tmp_path / "home")
    monkeypatch.setattr(handoff, "_save_receipts", lambda profile_home, receipts: None)
    monkeypatch.setattr(handoff, "_load_receipts", lambda profile_home: {})
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda token: bridge.ScopedSessionCapability(token, "owner", 0))

    import tui_gateway.server as server

    def dispatch(req):
        calls.append(req["method"])
        if req["method"] == "session.create":
            server._sessions["child-runtime"] = child
            return {"result": {"session_id": "child-runtime", "stored_session_id": "child-stored"}}
        if req["method"] == "session.title":
            return {"result": {"title": "child"}}
        return {"result": {"status": "streaming"}}

    monkeypatch.setattr(server, "dispatch", dispatch)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_ok", lambda rid, result: {"result": result})
    monkeypatch.setattr(server, "_err", lambda rid, code, message, data=None: {"error": {"code": code, "message": message}})

    result = handoff.create_task_session(1, {
        "cwd": str(root), "task": "work", "title": "child", "request_id": "req-1",
        "_session_spawn_capability": "cap",
    })
    assert result["result"]["stored_session_id"] == "child-stored"
    assert calls == ["session.create", "session.title", "prompt.submit"]


def test_pending_title_is_resumable_and_does_not_submit(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    caller = {"session_key": "owner", "cwd": str(root), "profile_home": None,
              "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p")}
    calls = []
    monkeypatch.setattr(handoff, "_find_caller", lambda _: ("owner", caller))
    monkeypatch.setattr(handoff, "_policy", lambda *_: {"enabled": True, "max_children_per_root": 3, "max_depth": 2, "rate_per_minute": 5})
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda token: bridge.ScopedSessionCapability(token, "owner", 0))
    monkeypatch.setattr(handoff, "_dispatch_existing", lambda method, params, request_id: calls.append(method) or (
        {"session_id": "child", "stored_session_id": "stored"} if method == "session.create" else
        {"pending": True, "title": "child"} if method == "session.title" else {}))
    monkeypatch.setattr(handoff, "_validate_cwd", lambda raw, caller: str(root))
    monkeypatch.setattr(handoff, "_load_receipts", lambda _: {})
    monkeypatch.setattr(handoff, "_save_receipts", lambda *_: None)
    result = handoff.create_task_session(1, {"cwd": str(root), "task": "x", "title": "child", "request_id": "r", "_session_spawn_capability": "cap"})
    assert result["result"]["resumable"] is True
    assert "prompt.submit" not in calls


def test_deferred_submit_reports_accepted_not_running(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    caller = {"session_key": "owner", "cwd": str(root), "profile_home": None,
              "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p")}
    monkeypatch.setattr(handoff, "_find_caller", lambda _: ("owner", caller))
    monkeypatch.setattr(handoff, "_policy", lambda *_: {"enabled": True, "max_children_per_root": 3, "max_depth": 2, "rate_per_minute": 5})
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda token: bridge.ScopedSessionCapability(token, "owner", 0))
    monkeypatch.setattr(handoff, "_load_receipts", lambda _: {})
    monkeypatch.setattr(handoff, "_save_receipts", lambda *_: None)
    monkeypatch.setattr(handoff, "_dispatch_existing", lambda method, params, request_id: (
        {"session_id": "child", "stored_session_id": "stored"} if method == "session.create" else
        {"pending": False, "title": "child"} if method == "session.title" else {}))
    import tui_gateway.server as server
    monkeypatch.setattr(server, "_sessions", {"child": {"session_key": "stored", "running": False, "agent": None}})
    result = handoff.create_task_session(1, {"cwd": str(root), "task": "x", "title": "child", "request_id": "r", "_session_spawn_capability": "cap"})
    assert result["result"]["task_status"] == "accepted"


def test_profile_bound_capability_cannot_resolve_same_id_in_other_profile(monkeypatch):
    import tui_gateway.server as server
    record = {"session_key": "same", "profile_home": "/profile-b", "session_generation": "gen-b"}
    monkeypatch.setattr(server, "_sessions", {"runtime-b": record})
    capability = bridge.ScopedSessionCapability("cap", "runtime-b", 0, "/profile-b", "gen-b")
    monkeypatch.setattr(bridge, "authorize_scoped_capability", lambda _: capability)
    assert handoff._find_caller("same") is None


def test_deferred_build_kwargs_carry_inherited_restrictions():
    from tui_gateway import server

    current = {
        "inherited_restrictions": {
            "denied_tools": ["terminal"],
            "denied_toolsets": ["browser"],
            "permission_mode": "default",
            "budget_cap": 2.5,
        },
    }
    kwargs = server._deferred_build_agent_kwargs(current, None)
    assert kwargs["delegated_restrictions"] == current["inherited_restrictions"], (
        "deferred child builds must receive the caller's restriction envelope"
    )


def test_child_agent_restrictions_are_intersected_before_first_turn():
    from types import SimpleNamespace
    from tui_gateway import server

    child = SimpleNamespace(disabled_toolsets=["memory"], max_budget_usd=5.0, permission_mode="default")
    server._apply_delegated_restrictions(child, {
        "denied_tools": ["terminal"], "denied_toolsets": ["browser"],
        "permission_mode": "default", "budget_cap": 2.5,
    })
    assert child.denied_tools == ["terminal"]
    assert set(child.disabled_toolsets) >= {"memory", "browser"}
    assert child.max_budget_usd == 2.5
    assert child.permission_mode == "default"


def _handoff_fixture(monkeypatch, tmp_path):
    from tui_gateway import server

    root = tmp_path / "project"
    root.mkdir()
    caller = {"session_key": "owner-stored", "session_generation": "generation", "cwd": str(root),
              "profile_home": None,
              "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p")}
    monkeypatch.setattr(handoff, "_receipt_home", lambda _: tmp_path / "home")
    monkeypatch.setattr(handoff, "_find_caller", lambda _: ("owner-runtime", caller))
    monkeypatch.setattr(handoff, "_canonical_roots", lambda _: (str(root),))
    monkeypatch.setattr(handoff, "_policy", lambda *_: {"enabled": True, "max_children_per_root": 3,
                                                          "max_depth": 2, "rate_per_minute": 5})
    monkeypatch.setattr(bridge, "authorize_scoped_capability",
                        lambda token: bridge.ScopedSessionCapability(token, "owner-runtime", 0))
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_ok", lambda rid, result: {"result": result})
    monkeypatch.setattr(server, "_err", lambda rid, code, message, data=None:
                        {"error": {"code": code, "message": message}})
    return root, caller, server


def test_post_submit_failure_keeps_reservation_until_child_teardown(monkeypatch, tmp_path):
    root, _caller, server = _handoff_fixture(monkeypatch, tmp_path)
    child = {"session_key": "child-stored", "cwd": str(root), "running": True, "agent": None}
    monkeypatch.setattr(server, "_sessions", {"child-runtime": child})
    calls = []

    def dispatch(req):
        calls.append(req["method"])
        if req["method"] == "session.create":
            return {"result": {"session_id": "child-runtime", "stored_session_id": "child-stored",
                                "info": {"cwd": str(root)}}}
        if req["method"] == "session.title":
            return {"result": {}}
        raise RuntimeError("submit failed after child started")

    monkeypatch.setattr(server, "dispatch", dispatch)
    result = handoff.create_task_session(1, {"cwd": str(root), "task": "x", "title": "child",
                                              "request_id": "post-submit", "_session_spawn_capability": "cap"})
    assert result["error"]["code"] == 5008
    receipts = handoff._load_receipts(None)
    receipt = next(iter(receipts.values()))
    assert receipt["active_reservation"] is True, "a live child keeps its slot after post-submit failure"


def test_pending_title_retry_submits_using_same_real_receipt(monkeypatch, tmp_path):
    root, _caller, server = _handoff_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_sessions", {
        "child-runtime": {"session_key": "child-stored", "cwd": str(root), "running": True, "agent": None},
    })
    calls = []

    def dispatch(req):
        calls.append(req["method"])
        if req["method"] == "session.create":
            return {"result": {"session_id": "child-runtime", "stored_session_id": "child-stored",
                                "info": {"cwd": str(root)}}}
        if req["method"] == "session.title":
            return {"result": {"pending": True}}
        return {"result": {}}

    monkeypatch.setattr(server, "dispatch", dispatch)
    params = {"cwd": str(root), "task": "x", "title": "child", "request_id": "pending-retry",
              "_session_spawn_capability": "cap"}
    first = handoff.create_task_session(1, params)
    second = handoff.create_task_session(2, params)
    assert first["result"]["resumable"] is True
    assert second["result"]["task_status"] == "accepted"
    assert calls == ["session.create", "session.title", "prompt.submit"], (
        "retrying a pending receipt must continue the handoff rather than replay stale output"
    )


def test_startup_reconciliation_releases_dead_child_reservation(monkeypatch, tmp_path):
    root, _caller, server = _handoff_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_sessions", {})
    path = handoff._receipt_path(None)
    path.parent.mkdir(parents=True, exist_ok=True)
    handoff._save_receipts(None, {"receipt": {
        "root_session_id": "root", "child_session_id": "dead-runtime",
        "child_stored_session_id": "dead-stored", "active_reservation": True,
        "created_at": 1,
    }})
    handoff.reconcile_child_reservations(None)
    assert handoff._load_receipts(None)["receipt"]["active_reservation"] is False, (
        "gateway startup must release durable reservations with no live child"
    )


def test_terminal_receipt_retention_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "_receipt_home", lambda _: tmp_path / "home")
    receipts = {
        f"receipt-{index}": {
            "root_session_id": "root",
            "active_reservation": False,
            "created_at": index,
        }
        for index in range(handoff._MAX_RECEIPTS_PER_ROOT + 5)
    }
    handoff._save_receipts(None, receipts)
    stored = handoff._load_receipts(None)
    assert len(stored) <= handoff._MAX_RECEIPTS_PER_ROOT, (
        "terminal receipt retention must be bounded per root"
    )


def test_real_receipt_replay_creates_one_child_and_submits_once(monkeypatch, tmp_path):
    root, _caller, server = _handoff_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_sessions", {
        "child-runtime": {"session_key": "child-stored", "cwd": str(root), "running": True, "agent": None},
    })
    calls = []

    def dispatch(req):
        calls.append(req["method"])
        if req["method"] == "session.create":
            return {"result": {"session_id": "child-runtime", "stored_session_id": "child-stored",
                                "info": {"cwd": str(root)}}}
        return {"result": {}}

    monkeypatch.setattr(server, "dispatch", dispatch)
    params = {"cwd": str(root), "task": "x", "title": "child", "request_id": "same-request",
              "_session_spawn_capability": "cap"}
    first = handoff.create_task_session(1, params)
    second = handoff.create_task_session(2, params)
    assert first["result"]["stored_session_id"] == second["result"]["stored_session_id"]
    assert calls == ["session.create", "session.title", "prompt.submit"], (
        "real receipt replay must not create a second child or submit a second turn"
    )


def test_resume_record_carries_stable_child_identity_after_compression(monkeypatch, tmp_path):
    _root, _caller, server = _handoff_fixture(monkeypatch, tmp_path)
    handoff._save_receipts(None, {"receipt": {
        "root_session_id": "root", "child_stored_session_id": "child-stored",
        "child_session_id": "compressed-continuation", "active_reservation": True,
    }})
    record = server._deferred_session_record("compressed-continuation", cols=80, cwd=str(tmp_path),
                                             history=[], lease=None, profile_home=None)
    assert record["spawn_child_stored_session_id"] == "child-stored", (
        "compression/resume must retain the durable child identity used for reservation release"
    )


def _spawn_harness(monkeypatch, tmp_path, caller, receipts):
    """Wire create_task_session against an in-memory receipt store. Returns the dispatch call log."""
    import tui_gateway.server as server

    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    calls = []

    monkeypatch.setattr(handoff, "_find_caller", lambda _: ("owner-runtime", caller))
    monkeypatch.setattr(handoff, "_policy", lambda *_: {
        "enabled": True, "max_children_per_root": 2, "max_depth": 1, "rate_per_minute": 5})
    monkeypatch.setattr(handoff, "_validate_cwd", lambda raw, caller: str(root))
    monkeypatch.setattr(handoff, "_load_receipts", lambda _: receipts)
    monkeypatch.setattr(handoff, "_save_receipts", lambda profile_home, saved: receipts.update(saved))
    monkeypatch.setattr(bridge, "authorize_scoped_capability",
                        lambda token: bridge.ScopedSessionCapability(token, "owner-runtime", 0))
    monkeypatch.setattr(handoff, "_dispatch_existing", lambda method, params, request_id: calls.append(method) or (
        {"session_id": "child-runtime", "stored_session_id": "child-stored"} if method == "session.create" else
        {"pending": False, "title": "child"} if method == "session.title" else {}))
    monkeypatch.setattr(server, "_sessions", {"child-runtime": {"session_key": "child-stored", "running": False, "agent": None}})
    monkeypatch.setattr(server, "_ok", lambda rid, result: {"result": result})
    monkeypatch.setattr(server, "_err", lambda rid, code, message, data=None: {"error": {"code": code, "message": message}})
    return str(root), calls


def test_compression_does_not_reset_the_spawn_quota_lineage(monkeypatch, tmp_path):
    """W8 A8-23: a compressed caller kept its depth/concurrency/rate history, instead of looking
    like a brand-new root at depth 0."""
    # The caller already spawned its own children at depth 1 and filled the per-root allowance.
    receipts = {
        "a": {"root_session_id": "origin", "child_stored_session_id": "owner-origin",
              "depth": 1, "active_reservation": True, "created_at": 0},
        "b": {"root_session_id": "origin", "child_stored_session_id": "sibling",
              "depth": 1, "active_reservation": True, "created_at": 0},
    }
    caller = {
        # Compression moved the live key to a continuation id; the durable spawn id is recorded.
        "session_key": "owner-continuation-after-compression",
        "spawn_child_stored_session_id": "owner-origin",
        "cwd": str(tmp_path / "project"),
        "profile_home": None,
        "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p"),
    }
    root, calls = _spawn_harness(monkeypatch, tmp_path, caller, receipts)

    result = handoff.create_task_session(1, {
        "cwd": root, "task": "x", "title": "child", "request_id": "r-depth",
        "_session_spawn_capability": "cap"})

    assert "error" in result, "a compressed caller must not spawn past the depth limit"
    assert result["error"]["code"] == 4293
    assert "session.create" not in calls


def test_a_second_request_cannot_steal_an_in_progress_reservation(monkeypatch, tmp_path):
    """W8 A8-26: the retry saw a reservation with no child id yet, deleted it, and created a second
    child behind the same quota entry."""
    import time as _time

    receipts = {}
    caller = {"session_key": "owner-stored", "cwd": str(tmp_path / "project"), "profile_home": None,
              "agent": SimpleNamespace(api_mode="claude_agent_sdk", model="m", provider="p")}
    root, calls = _spawn_harness(monkeypatch, tmp_path, caller, receipts)

    reserved = {}

    def create_then_reenter(method, params, request_id):
        calls.append(method)
        if method == "session.create" and not reserved:
            # Same request_id arrives while this creation is still in flight.
            reserved["second"] = handoff.create_task_session(2, {
                "cwd": root, "task": "x", "title": "child", "request_id": "r-race",
                "_session_spawn_capability": "cap"})
            return {"session_id": "child-runtime", "stored_session_id": "child-stored"}
        if method == "session.title":
            return {"pending": False, "title": "child"}
        return {}

    monkeypatch.setattr(handoff, "_dispatch_existing", create_then_reenter)

    first = handoff.create_task_session(1, {
        "cwd": root, "task": "x", "title": "child", "request_id": "r-race",
        "_session_spawn_capability": "cap"})

    assert first["result"]["stored_session_id"] == "child-stored"
    assert "error" in reserved["second"], "the concurrent retry must be refused, not served a second child"
    assert reserved["second"]["error"]["code"] == 4292
    assert calls.count("session.create") == 1
    # A stale claim is still reclaimable, so a crashed spawn never strands the request_id.
    stranded = {"claim_started_at": _time.time() - handoff._CLAIM_TTL_SECONDS - 1}
    assert handoff._claim_is_live(stranded) is False
    assert handoff._claim_is_live({"claim_started_at": _time.time()}) is True


def test_compression_anchors_the_lineage_id_even_for_a_root_session():
    """A root session stores None here at create; setdefault would leave it None and the quota would
    key off the continuation id (W8 review A8-23)."""
    from types import SimpleNamespace as NS

    from tui_gateway import server  # the split module's bodies are rebound onto server's globals

    session = {"session_key": "root-original", "spawn_child_stored_session_id": None,
               "agent": NS(session_id="root-continuation")}
    server._sync_session_key_after_compress("sid", session)

    assert session["session_key"] == "root-continuation"
    assert session["spawn_child_stored_session_id"] == "root-original"
