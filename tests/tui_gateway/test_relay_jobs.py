"""Read-only projection of conductor relay worker jobs for a session workspace."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def relay_runtime(monkeypatch, tmp_path):
    from tui_gateway import server

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    class Transport:
        def __init__(self):
            self.frames = []
            self.ready = threading.Event()

        def write(self, frame):
            self.frames.append(frame)
            self.ready.set()

    transport = Transport()
    owner = {"session_key": "parent", "history": [], "transport": transport, "cwd": str(cwd)}
    monkeypatch.setattr(server, "_sessions", {"relay-owner": owner})
    return server, cwd, transport


def _job(job_id, *, status="running", cwd="", main_checkout="", spawned_at=None, heartbeat_at=None):
    now = datetime.now(timezone.utc)
    return {
        "job_id": job_id,
        "worker": "codex",
        "model": "gpt-6-sol",
        "model_resolved": "gpt-6-sol",
        "lane": "impl",
        "role": "worker",
        "status": status,
        "spawned_at": spawned_at or now.isoformat(),
        "heartbeat_at": heartbeat_at or now.isoformat(),
        "heartbeat_epoch": now.timestamp(),
        "duration_sec": 12.5,
        "cwd": cwd,
        "lane_worktree": "",
        "main_checkout": main_checkout,
        "run_id": "run-1",
    }


def _write_job(cwd, record, name=None):
    jobs = cwd / ".claude" / "state" / "worker-spawn" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    path = jobs / (name or f"{record['job_id']}.json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _call(server, transport):
    response = server.dispatch(
        {"id": 1, "method": "relay_jobs.list", "params": {"session_id": "relay-owner"}},
        transport=transport,
    )
    if response is not None:
        return response
    assert transport.ready.wait(timeout=3), "relay job RPC did not reply"
    return transport.frames[-1]


def test_lists_running_and_recent_terminal_jobs_scoped_to_session_workspace(relay_runtime, monkeypatch):
    server, cwd, transport = relay_runtime
    jobs = cwd / ".claude" / "state" / "worker-spawn" / "jobs"
    inside = cwd / "nested"
    inside.mkdir()
    outside = cwd.parent / "other"
    outside.mkdir()
    _write_job(cwd, _job("w_running", cwd=str(inside)))
    recent = (datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    _write_job(cwd, _job("w_recent", status="succeeded", main_checkout=str(cwd), heartbeat_at=recent))
    _write_job(cwd, _job("w_old", status="failed", cwd=str(cwd), heartbeat_at=old))
    _write_job(cwd, _job("w_foreign", cwd=str(outside)))

    result = _call(server, transport)

    assert "error" not in result
    rows = result["result"]["jobs"]
    assert {row["job_id"] for row in rows} == {"w_running", "w_recent", "w_foreign"}


def test_caps_newest_first_and_skips_corrupt_json(relay_runtime):
    server, cwd, transport = relay_runtime
    jobs = cwd / ".claude" / "state" / "worker-spawn" / "jobs"
    jobs.mkdir(parents=True)
    for index in range(25):
        spawned = (datetime.now(timezone.utc) - timedelta(seconds=25 - index)).isoformat()
        _write_job(cwd, _job(f"w_{index:02}", cwd=str(cwd), spawned_at=spawned))
    (jobs / "w_corrupt.json").write_text("{", encoding="utf-8")
    (jobs / "w_partial.json").write_text(json.dumps({"job_id": "w_partial", "status": "running"}), encoding="utf-8")

    result = _call(server, transport)

    assert "error" not in result
    rows = result["result"]["jobs"]
    assert len(rows) == 20
    assert rows[0]["job_id"] == "w_24"
    assert rows[-1]["job_id"] == "w_05"


def test_reads_job_json_without_creating_lock_or_sidecar_files(relay_runtime, monkeypatch):
    server, cwd, transport = relay_runtime
    path = _write_job(cwd, _job("w_readonly", cwd=str(cwd)))
    sidecars = {suffix: path.with_suffix(suffix) for suffix in (".heartbeat", ".meta", ".lock")}
    for sidecar in sidecars.values():
        sidecar.write_text("untouched", encoding="utf-8")
    before = {sidecar: sidecar.read_text(encoding="utf-8") for sidecar in sidecars.values()}

    from pathlib import Path

    original_open = Path.open

    def read_sidecar(sidecar):
        with original_open(sidecar, encoding="utf-8") as stream:
            return stream.read()


    def json_only_open(target, *args, **kwargs):
        assert target == path
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", json_only_open)

    result = _call(server, transport)

    assert "error" not in result
    assert {sidecar: read_sidecar(sidecar) for sidecar in sidecars.values()} == before


def test_skips_job_files_larger_than_256_kb(relay_runtime):
    server, cwd, transport = relay_runtime
    path = _write_job(cwd, _job("w_large", cwd=str(cwd)))
    path.write_text(json.dumps(_job("w_large", cwd=str(cwd))) + " " * (256 * 1024), encoding="utf-8")

    result = _call(server, transport)

    assert result["result"]["jobs"] == []


def test_fifo_job_file_does_not_block_listing(relay_runtime):
    import os

    server, cwd, _ = relay_runtime
    jobs = cwd / ".claude" / "state" / "worker-spawn" / "jobs"
    jobs.mkdir(parents=True)
    os.mkfifo(jobs / "w_fifo.json")
    result = []
    thread = threading.Thread(target=lambda: result.append(server._list_relay_jobs(str(cwd))), daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive(), "relay job listing blocked while opening a FIFO"
    assert result == [[]]


def test_ignores_symlinked_job_file(relay_runtime, tmp_path):
    server, cwd, transport = relay_runtime
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(_job("w_linked", cwd=str(cwd))), encoding="utf-8")
    jobs = cwd / ".claude" / "state" / "worker-spawn" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "w_linked.json").symlink_to(target)

    result = _call(server, transport)

    assert result["result"]["jobs"] == []


def test_refuses_symlinked_jobs_directory(relay_runtime, tmp_path):
    server, cwd, transport = relay_runtime
    outside = tmp_path / "outside-jobs"
    outside.mkdir()
    (outside / "w_linked.json").write_text(json.dumps(_job("w_linked", cwd=str(cwd))), encoding="utf-8")
    jobs_parent = cwd / ".claude" / "state" / "worker-spawn"
    jobs_parent.mkdir(parents=True)
    (jobs_parent / "jobs").symlink_to(outside, target_is_directory=True)

    result = _call(server, transport)

    assert result["result"]["jobs"] == []


def test_workspace_job_store_scopes_real_lane_records(relay_runtime, tmp_path):
    server, cwd, transport = relay_runtime
    main_checkout = tmp_path / "hermes-cntrl"
    lane_cwd = tmp_path / "lane-41" / ".claude" / "worktrees" / "lane-w"
    _write_job(cwd, _job("w_lane", cwd=str(lane_cwd), main_checkout=str(main_checkout)))
    other_workspace = tmp_path / "other-workspace"
    _write_job(other_workspace, _job("w_other", cwd=str(lane_cwd), main_checkout=str(main_checkout)))

    result = _call(server, transport)

    assert {row["job_id"] for row in result["result"]["jobs"]} == {"w_lane"}


def test_running_job_with_expired_lease_or_old_heartbeat_is_stale(relay_runtime):
    server, cwd, transport = relay_runtime
    now = datetime.now(timezone.utc)
    expired = _job("w_expired", cwd=str(cwd))
    expired["lease_expires_epoch"] = now.timestamp() - 1
    old_heartbeat = _job("w_idle", cwd=str(cwd))
    old_heartbeat["heartbeat_epoch"] = now.timestamp() - 301
    _write_job(cwd, expired)
    _write_job(cwd, old_heartbeat)

    result = _call(server, transport)

    assert {row["job_id"]: row["status"] for row in result["result"]["jobs"]} == {
        "w_expired": "stale",
        "w_idle": "stale",
    }


def test_relay_job_listing_uses_long_handler_pool(relay_runtime):
    server, _, _ = relay_runtime

    assert "relay_jobs.list" in server._LONG_HANDLERS
