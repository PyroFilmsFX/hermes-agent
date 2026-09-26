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


# --- HE conductor UI Part A (A1): name, purpose, place, build scope ---------------------------

_HEX = "484496" + "a" * 20 + "c677d2"
_JOB = "w_20260926T202751Z_0e14"


def _job_id(index: int) -> str:
    return f"w_20260926T2027{index % 60:02}Z_{index:04x}"


def _write_brief(cwd, job_id, text):
    briefs = cwd / ".claude" / "state" / "worker-spawn" / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    path = briefs / f"{job_id}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _write_marker(cwd, run_id):
    state = cwd / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tb-build-active.json").write_text(json.dumps({
        "plan": "/x/plans/OVERNIGHT-HERMES-WORKER-2026-09-26.md",
        "run_id": run_id,
        "waves_total": 7,
        "waves_done": 0,
        "done": False,
        "blocked": False,
        "lease_expires_at": datetime.now(timezone.utc).timestamp() + 3600,
    }), encoding="utf-8")


def _call_scope(server, transport, scope):
    transport.ready.clear()
    response = server.dispatch(
        {"id": 2, "method": "relay_jobs.list", "params": {"session_id": "relay-owner", "scope": scope}},
        transport=transport,
    )
    if response is not None:
        return response
    assert transport.ready.wait(timeout=3), "relay job RPC did not reply"
    return transport.frames[-1]


@pytest.mark.parametrize(
    ("build_run_id", "marker_run_id", "expected"),
    [
        ("-fix-g9", "", "fix-g9"),
        ("-impl-g9", _HEX, "impl-g9"),
        ("impl41", "", "impl41"),
        ("-rev-h0", "", "rev-h0"),
        ("wm-peercard-impl", _HEX, "wm-peercard-impl"),
        (f"{_HEX}-mkfix", _HEX, "mkfix"),
        (_HEX, _HEX, ""),
        (_HEX, "", ""),
        ("w_20260924T200208Z_c68a", "", ""),
        ("lane/../../etc passwd", "", "lane....etcpasswd"),
        ("x" * 60, "", "x" * 40),
    ],
)
def test_label_is_derived_from_build_run_id(relay_runtime, build_run_id, marker_run_id, expected):
    server, cwd, _ = relay_runtime
    record = _job(_JOB, cwd=str(cwd))
    record["build_run_id"] = build_run_id
    _write_job(cwd, record)

    [row] = server._list_relay_jobs(str(cwd), marker_run_id=marker_run_id)

    assert row["label"] == expected


def test_label_falls_back_to_run_id_when_build_run_id_is_missing(relay_runtime):
    server, cwd, _ = relay_runtime
    record = _job(_JOB, cwd=str(cwd))
    record["run_id"] = "-fix-g9"
    _write_job(cwd, record)

    [row] = server._list_relay_jobs(str(cwd))

    assert row["label"] == "fix-g9"


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        (
            "CROSS_MODEL_CONTRACT v3\nscope: |\n  Two bugs in the Hermes peer mailbox / session_send path (Python). "
            "Then more.\n",
            "Two bugs in the Hermes peer mailbox / session_send path (Python)",
        ),
        (
            "AGY WRITE-LANE CWD ADVISORY: stay inside /private/tmp/lane-g9\nAGY WRITE-LANE WATCHDOG: 20m\n\n"
            "CROSS_MODEL_CONTRACT\nscope:\n\nFix the W1 cross-model review findings listed below, then stop. More.",
            "Fix the W1 cross-model review findings listed below, then stop",
        ),
        (
            "Fix exactly these review findings in HE-G9 commit c0deb07a65. Spec: /Users/justin/_ops/plans/G9.md",
            "Fix exactly these review findings in HE-G9 commit c0deb07a65",
        ),
        (
            'Implement ONLY step D0 of /tmp/u81-design.md: §2 "Classifier (D0)", §2.1 through §2.3\nNext line.',
            'Implement ONLY step D0 of u81-design.md: §2 "Classifier (D0)", §2.1 through §2.3',
        ),
        (
            "scope: read-only review of commit c0deb0760a in /tmp/land-h0 (tests only)",
            "Read-only review of commit c0deb0760a in land-h0 (tests only)",
        ),
        ("Goal: implement HE-G1, the conductor strip.", "Implement HE-G1, the conductor strip"),
        (
            "\n\nTB_SEAT=codex\nSINGLE-SEAT DIRECTIVE: one seat\nANALYSIS ONLY\n"
            "<cross-session-message from=\"x\">\nBug:   the lease   never renews",
            "The lease never renews",
        ),
        ("task: tidy the wave ledger", "Tidy the wave ledger"),
        ("MODEL: gpt-6-luna\nEFFORT: high\nRepair the lease renewal", "Repair the lease renewal"),
        ("CROSS_MODEL_CONTRACT\nscope: |\n", ""),
    ],
)
def test_purpose_is_the_first_sentence_of_the_brief(relay_runtime, brief, expected):
    server, cwd, _ = relay_runtime
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    _write_brief(cwd, _JOB, brief)

    [row] = server._list_relay_jobs(str(cwd))

    assert row["purpose"] == expected


def test_purpose_is_capped_on_a_word_boundary(relay_runtime):
    server, cwd, _ = relay_runtime
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    _write_brief(cwd, _JOB, "Refactor " + "the wave ledger reader " * 12)

    [row] = server._list_relay_jobs(str(cwd))

    assert len(row["purpose"]) <= 100
    assert row["purpose"].endswith("…")
    body = row["purpose"][:-1]
    assert body.split(" ")[-1] in {"the", "wave", "ledger", "reader"}


def test_purpose_never_carries_a_secret_from_the_brief(relay_runtime):
    server, cwd, _ = relay_runtime
    secret = "sk-proj-" + "Ab1" * 15
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    _write_brief(cwd, _JOB, f"Rotate the key {secret} in config now")

    [row] = server._list_relay_jobs(str(cwd))

    assert row["purpose"].startswith("Rotate the key")
    assert secret not in row["purpose"]
    assert "Ab1Ab1Ab1" not in row["purpose"]


def test_purpose_reads_the_derived_brief_path_not_the_record_brief_path(relay_runtime, tmp_path):
    server, cwd, _ = relay_runtime
    outside = tmp_path / "outside-brief.txt"
    outside.write_text("Leak this outside purpose", encoding="utf-8")
    record = _job(_JOB, cwd=str(cwd))
    record["brief_path"] = str(outside)
    _write_job(cwd, record)

    [row] = server._list_relay_jobs(str(cwd))

    assert row["purpose"] == ""


def test_purpose_ignores_symlinked_briefs_and_non_job_ids(relay_runtime, tmp_path):
    server, cwd, _ = relay_runtime
    outside = tmp_path / "outside-brief.txt"
    outside.write_text("Leak this outside purpose", encoding="utf-8")
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    briefs = cwd / ".claude" / "state" / "worker-spawn" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / f"{_JOB}.txt").symlink_to(outside)
    # A job id that is not the canonical shape must never become a filename.
    _write_job(cwd, _job("w_../../x", cwd=str(cwd)), name="w_odd.json")
    (briefs / "w_../../x.txt".replace("/", "_")).write_text("Nope", encoding="utf-8")

    rows = server._list_relay_jobs(str(cwd))

    assert {row["purpose"] for row in rows} == {""}


def test_place_is_a_basename_or_main_checkout(relay_runtime, tmp_path):
    server, cwd, _ = relay_runtime
    records = {
        _job_id(1): str(cwd),
        _job_id(2): str(tmp_path / "lane-g9" / ".claude" / "worktrees" / f"lane-{_job_id(2)}"),
        _job_id(3): "/private/tmp/land-h0",
        _job_id(4): "",
    }
    for job_id, job_cwd in records.items():
        _write_job(cwd, _job(job_id, cwd=job_cwd))

    rows = {row["job_id"]: row["place"] for row in server._list_relay_jobs(str(cwd))}

    assert rows == {
        _job_id(1): "main checkout",
        _job_id(2): "lane-g9",
        _job_id(3): "land-h0",
        _job_id(4): "",
    }


def test_rows_project_effort_exit_code_and_build_match_from_the_armed_marker(relay_runtime):
    server, cwd, transport = relay_runtime
    _write_marker(cwd, _HEX)
    shapes = {
        _job_id(1): (_HEX, "succeeded", 0),
        _job_id(2): (f"{_HEX}-mkfix", "failed", 1),
        _job_id(3): ("-fix-g9", "timeout", -15),
        _job_id(4): (f"{_HEX}x", "failed", None),
    }
    for job_id, (run_id, status, exit_code) in shapes.items():
        record = _job(job_id, cwd=str(cwd), status=status)
        record.update(build_run_id=run_id, exit_code=exit_code, effort="high")
        _write_job(cwd, record)

    result = _call(server, transport)

    rows = {row["job_id"]: row for row in result["result"]["jobs"]}
    assert {job_id: row["build_match"] for job_id, row in rows.items()} == {
        _job_id(1): True, _job_id(2): True, _job_id(3): False, _job_id(4): False,
    }
    assert {job_id: row["exit_code"] for job_id, row in rows.items()} == {
        _job_id(1): 0, _job_id(2): 1, _job_id(3): -15, _job_id(4): None,
    }
    assert {row["effort"] for row in rows.values()} == {"high"}
    assert rows[_job_id(2)]["label"] == "mkfix"

    from tui_gateway.contracts.relay_jobs import RelayJobsListResult

    parsed = RelayJobsListResult.model_validate(result["result"])
    assert parsed.jobs[0].build_match in {True, False}


def test_build_scope_ignores_the_recent_window_and_caps_at_40(relay_runtime):
    server, cwd, transport = relay_runtime
    _write_marker(cwd, _HEX)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    for index in range(45):
        record = _job(_job_id(index), cwd=str(cwd), status="succeeded",
                      spawned_at=(old + timedelta(seconds=index)).isoformat(), heartbeat_at=old.isoformat())
        record["build_run_id"] = f"{_HEX}-lane{index}"
        _write_job(cwd, record)
    unmatched = _job("w_20260926T210000Z_ffff", cwd=str(cwd))
    unmatched["build_run_id"] = "impl41"
    _write_job(cwd, unmatched)

    build_rows = _call_scope(server, transport, "build")["result"]["jobs"]
    recent_rows = _call_scope(server, transport, "recent")["result"]["jobs"]

    assert len(build_rows) == 40
    assert all(row["build_match"] for row in build_rows)
    assert build_rows[0]["job_id"] == _job_id(44)
    assert [row["job_id"] for row in recent_rows] == ["w_20260926T210000Z_ffff"]


def test_build_scope_without_an_armed_marker_is_empty(relay_runtime):
    server, cwd, transport = relay_runtime
    record = _job(_JOB, cwd=str(cwd))
    record["build_run_id"] = _HEX
    _write_job(cwd, record)

    assert _call_scope(server, transport, "build")["result"]["jobs"] == []


def test_unknown_scope_is_rejected(relay_runtime):
    server, _, transport = relay_runtime

    assert "error" in _call_scope(server, transport, "everything")


@pytest.mark.parametrize(("minutes", "listed"), [(30, True), (61, False)])
def test_stale_worker_lingers_for_an_hour_after_its_last_heartbeat(relay_runtime, minutes, listed):
    server, cwd, transport = relay_runtime
    beat = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    record = _job(_JOB, cwd=str(cwd), heartbeat_at=beat.isoformat(),
                  spawned_at=(beat - timedelta(minutes=5)).isoformat())
    record["heartbeat_epoch"] = beat.timestamp()
    _write_job(cwd, record)

    rows = _call(server, transport)["result"]["jobs"]

    assert [(row["job_id"], row["status"]) for row in rows] == ([(_JOB, "stale")] if listed else [])


def test_no_row_value_is_an_absolute_path(relay_runtime, tmp_path):
    server, cwd, transport = relay_runtime
    _write_marker(cwd, _HEX)
    record = _job(_JOB, cwd=str(tmp_path / "lane-g9" / ".claude" / "worktrees" / f"lane-{_JOB}"))
    record.update(build_run_id=f"{_HEX}-fix", brief_path="/abs/brief.txt", log_path="/abs/log",
                  lane_worktree="/abs/wt", main_checkout=str(cwd))
    _write_job(cwd, record)
    _write_brief(cwd, _JOB, "/Users/justin/secret/dir/plan.md is the spec for /tmp/lane-x work")

    [row] = _call(server, transport)["result"]["jobs"]

    for key, value in row.items():
        assert not (isinstance(value, str) and value.startswith("/")), key
    assert "/Users/" not in row["purpose"] and "/tmp/" not in row["purpose"]
    assert row["purpose"] == "Plan.md is the spec for lane-x work"


def _other_workspace_brief(tmp_path, text="Leak the other workspace purpose"):
    other = tmp_path / "other-workspace"
    _write_job(other, _job(_JOB, cwd=str(other)))
    _write_brief(other, _JOB, text)
    return other / ".claude" / "state" / "worker-spawn"


def test_brief_read_survives_a_parent_symlink_swap_after_the_check(relay_runtime, tmp_path, monkeypatch):
    """A workspace writer swaps ``worker-spawn`` for a symlink just before ``briefs`` is opened.
    The brief must still come from this workspace, never the symlink target."""
    import os

    server, cwd, _ = relay_runtime
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    _write_brief(cwd, _JOB, "Own workspace purpose")
    other_spawn = _other_workspace_brief(tmp_path)
    spawn = cwd / ".claude" / "state" / "worker-spawn"
    real_open = os.open
    swapped = []

    def racing_open(path, *args, **kwargs):
        if not swapped and os.path.basename(os.fspath(path).rstrip("/")) == "briefs":
            spawn.rename(spawn.with_name("worker-spawn.real"))
            spawn.symlink_to(other_spawn, target_is_directory=True)
            swapped.append(True)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)

    rows = server._list_relay_jobs(str(cwd))

    assert swapped, "the race hook never ran"
    assert [row["purpose"] for row in rows] == ["Own workspace purpose"]


@pytest.mark.parametrize("component", [".claude", "state", "worker-spawn"])
def test_symlinked_state_component_is_refused_by_the_fd_walk(relay_runtime, tmp_path, component):
    server, cwd, _ = relay_runtime
    other_spawn = _other_workspace_brief(tmp_path)
    targets = {".claude": other_spawn.parent.parent, "state": other_spawn.parent, "worker-spawn": other_spawn}
    parents = {".claude": cwd, "state": cwd / ".claude", "worker-spawn": cwd / ".claude" / "state"}
    parents[component].mkdir(parents=True, exist_ok=True)
    (parents[component] / component).symlink_to(targets[component], target_is_directory=True)

    assert server._relay_open_spawn_dir(cwd) is None
    assert server._list_relay_jobs(str(cwd)) == []


def test_symlinked_briefs_directory_yields_no_purpose(relay_runtime, tmp_path):
    server, cwd, _ = relay_runtime
    _write_job(cwd, _job(_JOB, cwd=str(cwd)))
    other_spawn = _other_workspace_brief(tmp_path)
    (cwd / ".claude" / "state" / "worker-spawn" / "briefs").symlink_to(
        other_spawn / "briefs", target_is_directory=True)

    [row] = server._list_relay_jobs(str(cwd))

    assert row["purpose"] == ""


@pytest.mark.parametrize(
    ("build_run_id", "expected"),
    [
        (f"impl-{_HEX}", "impl"),
        (f"-impl-{_HEX}", "impl"),
        (f"fix-{_HEX}-g9", "fix-g9"),
        (f"rev_{_HEX.upper()}.b", "rev-b"),
        (f"impl{_HEX}", "impl"),
        (f"{_HEX}{_HEX}", ""),
    ],
)
def test_label_never_carries_an_embedded_32_hex_id(relay_runtime, build_run_id, expected):
    import re

    server, cwd, _ = relay_runtime
    record = _job(_JOB, cwd=str(cwd))
    record["build_run_id"] = build_run_id
    _write_job(cwd, record)

    [row] = server._list_relay_jobs(str(cwd), marker_run_id="")

    assert row["label"] == expected
    assert not re.search(r"[0-9a-fA-F]{32}", row["label"])


@pytest.mark.parametrize(("owner", "expected"), [("parent", True), ("another-session", False)])
def test_build_match_follows_the_same_nested_marker_the_strip_shows(relay_runtime, owner, expected):
    # The live conductor marker can sit only in a nested build worktree. It matches rows when
    # this session owns it (here by its Hermes session id), and never when another session does.
    server, cwd, transport = relay_runtime
    nested = cwd / ".claude" / "worktrees" / "build-362"
    _write_marker(nested, _HEX)
    marker = nested / ".claude" / "state" / "tb-build-active.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "session_id": owner}), encoding="utf-8")
    record = _job(_job_id(1), cwd=str(cwd))
    record.update(build_run_id=_HEX)
    _write_job(cwd, record)

    rows = _call(server, transport)["result"]["jobs"]
    assert [row["build_match"] for row in rows] == [expected]
