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
        ({"lease_expires_at": 1}, "lease_expired"),
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


@pytest.mark.parametrize("change", ["done", "old", "symlink", "fifo", "oversize", "state_symlink"])
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
    # The #41 reader intentionally projects no build_run_id, so this source cannot
    # identify a specific build until that shared projection grows the field.
    assert build["lanes_build_matched"] == 0


def test_build_lane_match_uses_exact_or_child_run_id_when_reader_has_metadata(workspace, monkeypatch):
    # bind_module rebinds the handler onto server globals, so patch the server copy.
    monkeypatch.setattr(server, "_relay_job_reader", lambda _cwd: [
        {"status": "running", "build_run_id": "run-fake"},
        {"status": "running", "build_run_id": "run-fake-wave-3"},
        {"status": "running", "build_run_id": "another-run"},
    ])
    assert _result(workspace)["result"]["build"]["lanes_build_matched"] == 2


def test_method_is_long_owned_session_only_and_registered():
    assert "conductor_build.get" in server._LONG_HANDLERS
    assert "conductor_build.get" in server._methods
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server, "_current_session_steer_authority", lambda _sid: (None, None))
    try:
        assert server._methods["conductor_build.get"](2, {"session_id": "other"})["error"]["code"] == 4001
    finally:
        monkeypatch.undo()
