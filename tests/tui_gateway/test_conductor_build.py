"""Read-only projection of an armed tb-build marker and existing relay-job rows."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

import tui_gateway.server as server
from tui_gateway.contracts.conductor_build import ConductorBuildResult


def _marker(**overrides):
    now = datetime.now(timezone.utc)
    return {
        "boot_required": False,
        "plan": "/x/plans/FAKE-PLAN.md",
        "resolved_plan": "/x/plans/FAKE-PLAN.md",
        "started_at": "2026-01-01T00:00:00+00:00",
        "blocks": 0,
        "done": False,
        "blocked": False,
        "waiting_on": "",
        "wait_since": 0,
        "wait_allow_count": 0,
        "waits": [],
        "waves_total": 7,
        "waves_done": 2,
        "deferred": [],
        "build_base_tree": None,
        "wave_base_tree": None,
        "wave_receipt": {"armed": 0, "closed": 0},
        "context_path": "/x/c.md",
        "session_id": "sess-fake",
        "run_id": "run-fake",
        "state_root": "/x",
        "armed_via": "mcp",
        "lease_expires_at": now.timestamp() + 3600,
        "lease_not_after": now.timestamp() + 86400,
        "lease_renewals": 0,
        "completion_mode": "row-ledger",
        "ledger_path": "/x/l.json",
        "ledger_id": "L",
        "armed_at": now.isoformat(),
        "_revision": 1,
        "delegate_blocks": 0,
        **overrides,
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    state = tmp_path / ".claude" / "state"
    (state / "worker-spawn" / "jobs").mkdir(parents=True)
    marker = state / "tb-build-active.json"
    marker.write_text(json.dumps(_marker()), encoding="utf-8")
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (object(), {"cwd": str(tmp_path)}))
    return tmp_path, marker


def _result(workspace, sid="session"):
    return server._methods["conductor_build.get"](1, {"session_id": sid})


def test_active_build_projects_basename_wave_and_no_local_paths(workspace):
    result = _result(workspace)["result"]
    build = result["build"]
    assert build["plan"] == "FAKE-PLAN"
    assert build["wave_current"] == 3
    assert build["waves_total"] == 7
    assert build["waves_done"] == 2
    assert build["state"] == "active"
    assert build["usd"] is None and build["usd_source"] == "missing"
    assert build["stage"] is None and build["unit_id"] is None
    assert not {"context_path", "ledger_path", "state_root", "resolved_plan"} & build.keys()
    assert ConductorBuildResult.model_validate(result).build.plan == "FAKE-PLAN"


@pytest.mark.parametrize(
    ("overrides", "state"),
    [
        ({"waiting_on": "x" * 40 + "\nsecret\t" + "y" * 50}, "waiting"),
        ({"blocked": True}, "blocked"),
        # Owner not a live Hermes session and the lease has run out: idle, not "lease expired".
        ({"lease_expires_at": time.time() - 60}, "idle"),
        ({"waves_done": 99}, "active"),
    ],
)
def test_state_and_marker_text_projection(workspace, overrides, state):
    root, marker = workspace
    marker.write_text(json.dumps(_marker(**overrides)), encoding="utf-8")
    build = _result(workspace)["result"]["build"]
    assert build["state"] == state
    if "waiting_on" in overrides:
        assert len(build["waiting_on"]) <= 80
        assert "\n" not in build["waiting_on"] and "\t" not in build["waiting_on"]
    if overrides.get("waves_done", 0) > 7:
        assert build["wave_current"] == 7


@pytest.mark.parametrize("change", ["done", "symlink", "fifo", "oversize", "state_symlink"])
def test_invalid_or_retired_markers_are_not_shown(workspace, change):
    root, marker = workspace
    if change == "done":
        marker.write_text(json.dumps(_marker(done=True)), encoding="utf-8")
    elif change == "old":
        os.utime(marker, (time.time() - 12 * 3600 - 1,) * 2)
    elif change == "symlink":
        marker.unlink()
        marker.symlink_to(root / "elsewhere")
    elif change == "fifo":
        marker.unlink()
        os.mkfifo(marker)
    elif change == "oversize":
        marker.write_text(" " * (256 * 1024 + 1), encoding="utf-8")
    else:
        state = root / ".claude" / "state"
        backup = root / "state-real"
        state.rename(backup)
        state.symlink_to(backup, target_is_directory=True)
    assert _result(workspace)["result"]["build"] is None


def test_torn_marker_is_reported_unreadable_so_client_can_keep_last_value(workspace):
    _, marker = workspace
    marker.write_text('{"run_id":', encoding="utf-8")
    assert _result(workspace)["result"] == {"build": None, "unreadable": True}


def test_absent_marker_is_a_readable_empty_snapshot(workspace):
    _, marker = workspace
    marker.unlink()
    assert _result(workspace)["result"] == {"build": None, "unreadable": False}


@pytest.mark.parametrize("waves_total", ["missing", None, "unknown", 0, -1])
def test_marker_without_positive_wave_total_has_no_wave_numbers(workspace, waves_total):
    _, marker = workspace
    marker_data = _marker()
    if waves_total == "missing":
        marker_data.pop("waves_total", None)
    else:
        marker_data["waves_total"] = waves_total
    marker.write_text(json.dumps(marker_data), encoding="utf-8")

    build = _result(workspace)["result"]["build"]
    assert build["wave_current"] is None
    assert build["waves_total"] is None


def test_reuses_relay_reader_and_projects_lane_counts(workspace, monkeypatch):
    root, _ = workspace
    jobs = root / ".claude" / "state" / "worker-spawn" / "jobs"
    now = datetime.now(timezone.utc)
    records = [
        {"job_id": "w_run", "worker": "codex", "status": "running", "spawned_at": now.isoformat(),
         "lease_expires_epoch": now.timestamp() + 100, "heartbeat_epoch": now.timestamp(),
         "build_run_id": "run-fake"},
        {"job_id": "w_other", "worker": "claude", "status": "running", "spawned_at": now.isoformat(),
         "lease_expires_epoch": now.timestamp() + 100, "heartbeat_epoch": now.timestamp(),
         "build_run_id": "run-fake-child"},
        {"job_id": "w_stale", "worker": "grok", "status": "running", "spawned_at": now.isoformat(),
         "lease_expires_epoch": now.timestamp() + 100, "heartbeat_epoch": now.timestamp() - 600},
    ]
    for record in records:
        (jobs / f"{record['job_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (object(), {"cwd": str(root)}))
    build = _result(workspace)["result"]["build"]
    assert build["lanes_running"] == 2
    assert build["lanes_stale"] == 1
    # The shared reader now projects build_match against the marker's run id, so the
    # exact and child run ids count and the unlabelled stale lane does not.
    assert build["lanes_build_matched"] == 2


def test_build_lane_match_uses_exact_or_child_run_id_when_reader_has_metadata(workspace, monkeypatch):
    # bind_module rebinds the handler onto server globals, so patch the server copy.
    seen = {}

    def reader(_cwd, **kwargs):
        seen.update(kwargs)
        return [
            {"status": "running", "build_match": True},
            {"status": "running", "build_match": True},
            {"status": "running", "build_match": False},
        ]

    monkeypatch.setattr(server, "_relay_job_reader", reader)
    assert _result(workspace)["result"]["build"]["lanes_build_matched"] == 2
    assert seen.get("marker_run_id") == "run-fake"


def test_lanes_build_matched_counts_matching_job_records(workspace):
    root, _ = workspace
    jobs = root / ".claude" / "state" / "worker-spawn" / "jobs"
    now = datetime.now(timezone.utc)
    for job_id, run_id, status in [
        ("w_20260926T100000Z_0001", "run-fake", "running"),
        ("w_20260926T100000Z_0002", "run-fake-mkfix", "succeeded"),
        ("w_20260926T100000Z_0003", "run-fakeish", "running"),
        ("w_20260926T100000Z_0004", "impl41", "running"),
    ]:
        (jobs / f"{job_id}.json").write_text(json.dumps({
            "job_id": job_id, "worker": "codex", "status": status, "spawned_at": now.isoformat(),
            "heartbeat_at": now.isoformat(), "heartbeat_epoch": now.timestamp(), "build_run_id": run_id,
        }), encoding="utf-8")
    build = _result(workspace)["result"]["build"]
    assert build["lanes_build_matched"] == 2
    assert build["lanes_running"] == 3


def test_method_is_long_owned_session_only_and_registered():
    assert "conductor_build.get" in server._LONG_HANDLERS
    assert "conductor_build.get" in server._methods
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (None, None))
    try:
        assert server._methods["conductor_build.get"](2, {"session_id": "other"})["error"]["code"] == 4001
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Nested build worktree markers and aged ("idle") markers.
# ---------------------------------------------------------------------------

from types import SimpleNamespace

SDK_ID = "sdk-this-session"
HERMES_ID = "hermes-this-session"


def _own_session(root):
    agent = SimpleNamespace(session_id=HERMES_ID,
                            _claude_sdk_session=SimpleNamespace(_session_id=SDK_ID, _resume_session_id=None))
    return {"cwd": str(root), "session_key": HERMES_ID, "agent": agent}


def _nested(root, name, **overrides):
    state = root / ".claude" / "worktrees" / name / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "tb-build-active.json"
    marker.write_text(json.dumps(_marker(**overrides)), encoding="utf-8")
    return marker


@pytest.fixture
def owned(workspace, monkeypatch):
    root, marker = workspace
    marker.unlink()
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (object(), _own_session(root)))
    return root, marker


@pytest.mark.parametrize("owner", [SDK_ID, HERMES_ID])
def test_nested_marker_owned_by_this_session_shows(owned, owner):
    root, _ = owned
    _nested(root, "build-362", session_id=owner, plan="/x/plans/NESTED-PLAN.md")
    build = _result(owned)["result"]["build"]
    assert build is not None and build["plan"] == "NESTED-PLAN"
    assert build["session_id"] == owner


def test_nested_marker_owned_by_another_session_is_not_shown(owned):
    root, _ = owned
    # Exactly one fresh, non-done marker exists, but it is someone else's build.
    _nested(root, "build-362", session_id="some-other-session")
    assert _result(owned)["result"] == {"build": None, "unreadable": False}


def test_symlinked_worktree_dir_is_refused(owned, tmp_path_factory):
    root, _ = owned
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    _nested(elsewhere, "real", session_id=SDK_ID)
    (root / ".claude" / "worktrees").mkdir(parents=True)
    (root / ".claude" / "worktrees" / "build-1").symlink_to(elsewhere / ".claude" / "worktrees" / "real",
                                                           target_is_directory=True)
    assert _result(owned)["result"]["build"] is None


@pytest.mark.parametrize("link_at", [".claude", "state"])
def test_symlinked_component_inside_worktree_is_refused(owned, tmp_path_factory, link_at):
    root, _ = owned
    marker = _nested(root, "build-1", session_id=SDK_ID)
    real_claude = marker.parent.parent
    target = real_claude if link_at == ".claude" else marker.parent
    moved = tmp_path_factory.mktemp("moved") / "x"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    assert _result(owned)["result"]["build"] is None


def test_nested_scan_is_bounded_to_32_newest_worktree_dirs(owned, monkeypatch):
    root, _ = owned
    now = time.time()
    for index in range(40):
        _nested(root, f"wt-{index:02d}", session_id="other")
        # wt-00 is the oldest worktree dir; the rest are progressively newer.
        os.utime(root / ".claude" / "worktrees" / f"wt-{index:02d}", (now - 1000 + index,) * 2)
    _nested(root, "wt-00", session_id=SDK_ID, plan="/x/plans/OLDEST.md")
    os.utime(root / ".claude" / "worktrees" / "wt-00", (now - 5000,) * 2)
    opened = []
    real = server._build_worktree_marker

    def spy(worktrees_fd, name, cache_path):
        opened.append(name)
        return real(worktrees_fd, name, cache_path)

    monkeypatch.setattr(server, "_build_worktree_marker", spy)
    assert _result(owned)["result"]["build"] is None
    assert len(opened) == 32
    assert "wt-00" not in opened


def test_root_marker_owned_by_this_session_beats_nested(owned):
    root, marker = owned
    marker.write_text(json.dumps(_marker(session_id=SDK_ID, plan="/x/plans/ROOT.md")), encoding="utf-8")
    _nested(root, "build-9", session_id=SDK_ID, plan="/x/plans/NESTED.md")
    assert _result(owned)["result"]["build"]["plan"] == "ROOT"


def test_owned_nested_beats_unowned_root_and_newest_nested_wins(owned):
    root, marker = owned
    marker.write_text(json.dumps(_marker(session_id="other", plan="/x/plans/ROOT.md")), encoding="utf-8")
    older = _nested(root, "build-1", session_id=SDK_ID, plan="/x/plans/OLDER.md")
    _nested(root, "build-2", session_id=HERMES_ID, plan="/x/plans/NEWER.md")
    os.utime(older, (time.time() - 60,) * 2)
    assert _result(owned)["result"]["build"]["plan"] == "NEWER"


def test_unowned_root_marker_still_shows_when_no_nested_marker_is_owned(owned):
    # The session's own workspace root keeps its pre-discovery behaviour.
    root, marker = owned
    marker.write_text(json.dumps(_marker(session_id="other", plan="/x/plans/ROOT.md")), encoding="utf-8")
    _nested(root, "build-1", session_id="other", plan="/x/plans/NESTED.md")
    assert _result(owned)["result"]["build"]["plan"] == "ROOT"


HOUR = 3600


def _age(marker, seconds):
    os.utime(marker, (time.time() - seconds,) * 2)


def test_live_owner_with_old_marker_and_expired_lease_shows_normally_with_hint(owned):
    # The day the marker verbs were refused: the session worked, the file never moved.
    root, marker = owned
    marker.write_text(json.dumps(_marker(session_id=SDK_ID, lease_expires_at=time.time() - 15 * HOUR)),
                      encoding="utf-8")
    _age(marker, 16 * HOUR)
    result = _result(owned)["result"]
    build = result["build"]
    assert build["state"] == "active"
    assert build["idle_since"] is None
    assert build["marker_stale_since"] == pytest.approx(time.time() - 16 * HOUR, abs=5)
    assert ConductorBuildResult.model_validate(result).build.marker_stale_since is not None


def test_nested_marker_of_this_session_is_never_idle(owned):
    root, _ = owned
    marker = _nested(root, "build-362", session_id=SDK_ID, lease_expires_at=time.time() - 15 * HOUR)
    _age(marker, 16 * HOUR)
    build = _result(owned)["result"]["build"]
    assert build["state"] == "active" and build["marker_stale_since"] is not None


def test_other_busy_hermes_session_counts_as_a_live_owner(workspace, monkeypatch):
    _, marker = workspace
    marker.write_text(json.dumps(_marker(session_id="other-hermes", lease_expires_at=time.time() - 60)),
                      encoding="utf-8")
    monkeypatch.setitem(server._sessions, "sid-other", {"session_key": "other-hermes", "running": True})
    assert _result(workspace)["result"]["build"]["state"] == "active"


@pytest.mark.parametrize("record", [None, {"session_key": "sess-fake", "running": False, "transport": None}],
                         ids=["owner_gone", "owner_idle"])
def test_owner_gone_or_idle_and_lease_expired_is_idle(workspace, monkeypatch, record):
    _, marker = workspace
    expired = time.time() - 2 * HOUR
    marker.write_text(json.dumps(_marker(lease_expires_at=expired)), encoding="utf-8")
    if record is not None:
        monkeypatch.setitem(server._sessions, "sid-idle", record)
    result = _result(workspace)["result"]
    build = result["build"]
    assert build["state"] == "idle"
    assert build["idle_since"] == pytest.approx(expired, abs=1)
    assert build["marker_stale_since"] is None
    assert ConductorBuildResult.model_validate(result).build.state == "idle"


def test_owner_gone_with_valid_lease_shows_normally(workspace):
    _, marker = workspace
    _age(marker, 16 * HOUR)  # file age alone never idles or hides a build
    build = _result(workspace)["result"]["build"]
    assert build["state"] == "active"
    assert build["idle_since"] is None
    assert build["marker_stale_since"] is not None


@pytest.mark.parametrize("owner", ["gone", "live"])
@pytest.mark.parametrize("age", [0, 16 * HOUR, 9 * 24 * HOUR])
def test_done_marker_never_shows(owned, owner, age):
    root, marker = owned
    session_id = SDK_ID if owner == "live" else "gone"
    marker.write_text(json.dumps(_marker(done=True, session_id=session_id)), encoding="utf-8")
    _age(marker, age)
    assert _result(owned)["result"] == {"build": None, "unreadable": False}


@pytest.mark.parametrize(("expired_ago", "shown"), [(6 * 24 * HOUR, True), (8 * 24 * HOUR, False)])
def test_abandoned_guard_hides_only_week_old_expired_leases_of_gone_owners(workspace, expired_ago, shown):
    _, marker = workspace
    marker.write_text(json.dumps(_marker(lease_expires_at=time.time() - expired_ago)), encoding="utf-8")
    build = _result(workspace)["result"]["build"]
    assert (build is not None) is shown
    if shown:
        assert build["state"] == "idle"


def test_fresh_build_has_no_idle_or_stale_hint(workspace):
    build = _result(workspace)["result"]["build"]
    assert build["idle_since"] is None and build["marker_stale_since"] is None


def test_integral_float_wave_total_projects(workspace):
    # bind_module rebinds the handler onto server globals; the float check must not need a
    # module-level ``math`` import there.
    _, marker = workspace
    marker.write_text(json.dumps(_marker(waves_total=7.0)), encoding="utf-8")
    assert _result(workspace)["result"]["build"]["waves_total"] == 7
