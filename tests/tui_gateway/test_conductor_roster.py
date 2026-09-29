"""Tests for bounded incremental reader of the conductor marker index."""

from __future__ import annotations

import json
import os
import pwd
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tui_gateway.conductor_roster import (
    IndexScan,
    StatusRead,
    _clear_cache,
    _clear_status_cache,
    read_build_status,
    read_marker_index,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    temp_dir = (tmp_path / "temp").resolve()
    temp_dir.mkdir()

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_dir))
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: type("Pw", (), {"pw_dir": str(home)})())

    index_dir = home / ".claude" / "state"
    index_dir.mkdir(parents=True)
    index_file = index_dir / "tb-marker-index.jsonl"

    _clear_cache()
    yield home, temp_dir, index_file
    _clear_cache()


def _valid_marker(home: Path, name: str = "proj", build_id: str | None = None) -> str:
    if build_id:
        return str(home / name / ".claude" / "state" / "builds" / build_id / "tb-build-active.json")
    return str(home / name / ".claude" / "state" / "tb-build-active.json")


def _open_row(marker_path: str, run_id: str = "run-1", state_root: str = "/root", session_id: str = "sess-1") -> dict:
    return {
        "session_id": session_id,
        "marker_path": marker_path,
        "context_path": state_root,
        "state_root": state_root,
        "run_id": run_id,
    }


def _closed_row(marker_path: str, run_id: str = "run-1", at: str = "2026-09-29T10:00:00Z") -> dict:
    return {
        "closed": True,
        "run_id": run_id,
        "marker_path": marker_path,
        "at": at,
    }


def test_fold_latest_wins_per_marker_path(env):
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")

    rows = [
        _open_row(m1, run_id="run-1-initial"),
        _open_row(m2, run_id="run-2"),
        _open_row(m1, run_id="run-1-updated"),
    ]
    index_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    scan = read_marker_index(index_file)
    assert isinstance(scan, IndexScan)
    assert len(scan.entries) == 2
    # Newest-appended first: m1 was appended at line 3, m2 at line 2
    assert scan.entries[0]["marker_path"] == m1
    assert scan.entries[0]["run_id"] == "run-1-updated"
    assert scan.entries[1]["marker_path"] == m2
    assert scan.entries[1]["run_id"] == "run-2"
    assert scan.closed == 0
    assert scan.skipped == 0


def test_closed_row_hides_open_one(env):
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")

    rows = [
        _open_row(m1, run_id="r1"),
        _open_row(m2, run_id="r2"),
        _closed_row(m1, run_id="r1"),
    ]
    index_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    scan1 = read_marker_index(index_file)
    assert len(scan1.entries) == 1
    assert scan1.entries[0]["marker_path"] == m2
    assert scan1.closed == 1
    assert scan1.skipped == 0

    # Re-opening m1 unhides it
    with index_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_open_row(m1, run_id="r1-new")) + "\n")

    scan2 = read_marker_index(index_file)
    assert len(scan2.entries) == 2
    assert scan2.entries[0]["marker_path"] == m1
    assert scan2.entries[0]["run_id"] == "r1-new"
    assert scan2.closed == 0


def test_path_rule_rejections_counted_in_skipped(env):
    home, temp_dir, index_file = env

    valid_m = _valid_marker(home, "valid")
    valid_namespaced = _valid_marker(home, "valid_ns", build_id="b-123.x")

    # 1. relative
    rel_m = "relative/p/.claude/state/tb-build-active.json"

    # 2. non-realpath via symlink
    real_dir = home / "real_dir"
    real_dir.mkdir()
    sym_dir = home / "sym_dir"
    sym_dir.symlink_to(real_dir)
    symlink_m = str(sym_dir / ".claude" / "state" / "tb-build-active.json")

    # 3. wrong suffix
    wrong_suffix_m1 = str(home / "p" / ".claude" / "state" / "tb-build-inactive.json")
    wrong_suffix_m2 = str(home / "p" / "other" / "tb-build-active.json")

    # 4. bad build id (starts with dot, contains bad chars, or is . / ..)
    bad_id_dot = str(home / "p" / ".claude" / "state" / "builds" / ".bad" / "tb-build-active.json")
    bad_id_ddot = str(home / "p" / ".claude" / "state" / "builds" / ".." / "tb-build-active.json")
    bad_id_space = str(home / "p" / ".claude" / "state" / "builds" / "bad id" / "tb-build-active.json")

    # 5. under tmp (tempfile.gettempdir(), /private/tmp, /tmp)
    tmp_m1 = str(temp_dir / "p" / ".claude" / "state" / "tb-build-active.json")
    tmp_m2 = "/private/tmp/p/.claude/state/tb-build-active.json"
    tmp_m3 = "/tmp/p/.claude/state/tb-build-active.json"

    # 6. outside home
    outside_m = "/Users/other_person/p/.claude/state/tb-build-active.json"

    # Bad json and non-dict rows
    invalid_rows = [
        _open_row(rel_m),
        _open_row(symlink_m),
        _open_row(wrong_suffix_m1),
        _open_row(wrong_suffix_m2),
        _open_row(bad_id_dot),
        _open_row(bad_id_ddot),
        _open_row(bad_id_space),
        _open_row(tmp_m1),
        _open_row(tmp_m2),
        _open_row(tmp_m3),
        _open_row(outside_m),
        {"closed": True, "marker_path": outside_m, "run_id": "r"},
        _open_row(valid_m),
        _open_row(valid_namespaced),
    ]

    content = "\n".join(json.dumps(r) for r in invalid_rows)
    content += "\n{not valid json\n[1, 2, 3]\n\n"
    index_file.write_text(content, encoding="utf-8")

    scan = read_marker_index(index_file)
    assert len(scan.entries) == 2
    assert {e["marker_path"] for e in scan.entries} == {valid_m, valid_namespaced}
    # 12 path rejections + 2 bad lines = 14 skipped
    assert scan.skipped == 14


def test_tail_cap_2mib_and_cut_first_line(env):
    home, _temp, index_file = env
    m_old = _valid_marker(home, "old_project")
    m_new1 = _valid_marker(home, "new_project_1")
    m_new2 = _valid_marker(home, "new_project_2")

    line_old = json.dumps(_open_row(m_old, run_id="r-old")) + "\n"
    line_new1 = json.dumps(_open_row(m_new1, run_id="r-new1")) + "\n"
    line_new2 = json.dumps(_open_row(m_new2, run_id="r-new2")) + "\n"

    # Pad file so that total size > 2 MiB
    padding_line = json.dumps(_open_row(m_old, run_id="pad")) + "\n"
    target_padding_bytes = (2 * 1024 * 1024) + 5000

    chunks = []
    current_bytes = 0
    while current_bytes < target_padding_bytes:
        chunks.append(padding_line)
        current_bytes += len(padding_line.encode("utf-8"))

    # Append lines that will be in the 2 MiB tail
    chunks.append(line_new1)
    chunks.append(line_new2)

    total_content = "".join(chunks)
    index_file.write_text(total_content, encoding="utf-8")
    file_size = index_file.stat().st_size
    assert file_size > 2 * 1024 * 1024

    scan = read_marker_index(index_file)
    # Assert bytes_read was capped to at most 2 MiB
    assert scan.bytes_read <= 2 * 1024 * 1024
    # Cut first line must be cleanly skipped without triggering skipped count
    assert scan.skipped == 0
    # Rows in tail are present
    entry_markers = [e["marker_path"] for e in scan.entries]
    assert m_new1 in entry_markers
    assert m_new2 in entry_markers


def test_incremental_read_after_append(env):
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")
    m3 = _valid_marker(home, "p3")

    row1 = json.dumps(_open_row(m1, run_id="r1")) + "\n"
    row2 = json.dumps(_open_row(m2, run_id="r2")) + "\n"
    index_file.write_text(row1 + row2, encoding="utf-8")

    scan1 = read_marker_index(index_file)
    assert len(scan1.entries) == 2
    assert scan1.bytes_read == len((row1 + row2).encode("utf-8"))

    # Unchanged read: 0 bytes read
    scan_unchanged = read_marker_index(index_file)
    assert len(scan_unchanged.entries) == 2
    assert scan_unchanged.bytes_read == 0

    # Incremental append
    row3 = json.dumps(_open_row(m3, run_id="r3")) + "\n"
    with index_file.open("a", encoding="utf-8") as f:
        f.write(row3)

    scan2 = read_marker_index(index_file)
    assert len(scan2.entries) == 3
    # Assert only the appended bytes were read
    assert scan2.bytes_read == len(row3.encode("utf-8"))
    assert scan2.entries[0]["marker_path"] == m3


def test_inode_change_triggers_full_reread(env):
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")

    row1 = json.dumps(_open_row(m1, run_id="r1")) + "\n"
    index_file.write_text(row1, encoding="utf-8")

    scan1 = read_marker_index(index_file)
    assert len(scan1.entries) == 1
    assert scan1.entries[0]["marker_path"] == m1

    # Replace file with new inode
    index_file.unlink()
    row2 = json.dumps(_open_row(m2, run_id="r2")) + "\n"
    index_file.write_text(row2, encoding="utf-8")

    scan2 = read_marker_index(index_file)
    assert len(scan2.entries) == 1
    assert scan2.entries[0]["marker_path"] == m2
    assert scan2.bytes_read == len(row2.encode("utf-8"))


def test_partial_trailing_line_ignored_then_picked_up(env):
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")

    row1_line = json.dumps(_open_row(m1, run_id="r1")) + "\n"
    row2_full = json.dumps(_open_row(m2, run_id="r2")) + "\n"
    row2_part1 = row2_full[:25]
    row2_part2 = row2_full[25:]

    index_file.write_text(row1_line + row2_part1, encoding="utf-8")

    scan1 = read_marker_index(index_file)
    assert len(scan1.entries) == 1
    assert scan1.entries[0]["marker_path"] == m1

    # Complete the trailing line
    with index_file.open("a", encoding="utf-8") as f:
        f.write(row2_part2)

    scan2 = read_marker_index(index_file)
    assert len(scan2.entries) == 2
    assert scan2.entries[0]["marker_path"] == m2
    assert scan2.entries[1]["marker_path"] == m1


def test_state_root_cap_64(env):
    home, _temp, index_file = env

    rows = []
    # Write 70 distinct state_roots
    for i in range(70):
        sr = str(home / f"proj_{i:02d}")
        m = str(home / f"proj_{i:02d}" / ".claude" / "state" / "tb-build-active.json")
        rows.append(_open_row(m, run_id=f"r-{i}", state_root=sr))

    index_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    scan = read_marker_index(index_file)
    assert len(scan.entries) == 64
    distinct_roots = {e["state_root"] for e in scan.entries}
    assert len(distinct_roots) == 64
    # The 64 newest-appended state roots (proj_06 .. proj_69)
    expected_roots = {str(home / f"proj_{i:02d}") for i in range(6, 70)}
    assert distinct_roots == expected_roots
    assert scan.entries[0]["state_root"] == str(home / "proj_69")


def test_index_path_symlink_refused(env, tmp_path):
    home, _temp, index_file = env
    m = _valid_marker(home, "p1")
    index_file.write_text(json.dumps(_open_row(m)) + "\n", encoding="utf-8")

    symlink_index = tmp_path / "symlink_index.jsonl"
    symlink_index.symlink_to(index_file)

    scan = read_marker_index(symlink_index)
    assert scan.entries == ()
    assert scan.bytes_read == 0
    assert scan.closed == 0
    assert scan.skipped == 0


def test_default_path_uses_passwd_home(env):
    home, _temp, index_file = env
    m = _valid_marker(home, "p1")
    index_file.write_text(json.dumps(_open_row(m)) + "\n", encoding="utf-8")

    # Call with path=None and home=None -> should read from passwd home
    scan = read_marker_index()
    assert len(scan.entries) == 1
    assert scan.entries[0]["marker_path"] == m


_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "conductor"


def _read_fixture(name: str) -> dict:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_read_build_status_valid_parse(tmp_path):
    _clear_status_cache()
    status_content = (_FIXTURES_DIR / "status_v1_valid.json").read_text(encoding="utf-8")
    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(status_content, encoding="utf-8")

    marker_file = tmp_path / "tb-build-active.json"
    marker = {
        "run_id": "cc546d7b99dc45d6a829da8e2c77a673",
        "session_id": "6d739ad6-1234-5678-9abc-def012345678",
    }

    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    os.utime(status_file, (ref_time - 120, ref_time - 120))

    res = read_build_status(marker, marker_file, now=ref_time)
    assert isinstance(res, StatusRead)
    assert res.present is True
    assert res.valid is True
    assert res.reason == "ok"
    assert res.seq == 42
    assert res.fresh is True
    assert res.stale is False
    assert res.status_at is not None
    assert res.record is not None
    assert res.data is res.record

    assert res.record["build"]["run_id"] == "cc546d7b99dc45d6a829da8e2c77a673"
    assert res.record["build"]["session_id"] == "6d739ad6-1234-5678-9abc-def012345678"
    assert res.record["build"]["plan_title"] == "Hermes worker plan b9/b10"
    assert res.record["build"]["branch"] == "cntrl-hermes-worker"
    assert res.record["phase"] == "build"
    assert res.record["progress"]["waves"]["done"] == 1
    assert res.record["progress"]["waves"]["total"] == 4
    assert res.record["estimate"]["p50"] == 6.5
    assert res.record["estimate"]["p90"] == 10.0
    assert len(res.record["gates"]) == 1
    assert res.record["gates"][0]["label"] == "CI run 36533732367 on sync/land-b9"
    assert res.record["seats"]["agy"]["refused"] == 3
    assert res.record["seats"]["agy"]["refused_min"] == 3
    assert res.record["other_names"] == ["grok"]
    assert res.record["refusals"][0]["seat"] == "agy"
    assert res.record["lanes"]["running"] == 3
    assert res.record["ci"][0]["kind"] == "run"
    assert res.record["owner_blockers"][0]["id"] == "ob-1"
    assert res.record["last_activity"]["verb"] == "wave-done"


def test_read_build_status_mismatched_ignored(tmp_path):
    _clear_status_cache()
    status_content = (_FIXTURES_DIR / "status_v1_mismatched.json").read_text(encoding="utf-8")
    (tmp_path / "tb-build-status.json").write_text(status_content, encoding="utf-8")
    marker_file = tmp_path / "tb-build-active.json"

    # Mismatched run_id and session_id
    marker = {"run_id": "expected-run-id", "session_id": "expected-session-id"}
    res = read_build_status(marker, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "mismatched"

    # Matching run_id but mismatched session_id
    marker_mismatched_sid = {"run_id": "mismatched-run-id-999", "session_id": "other-sid"}
    res_sid = read_build_status(marker_mismatched_sid, marker_file)
    assert res_sid.present is True
    assert res_sid.valid is False
    assert res_sid.reason == "mismatched"

    # Matching session_id but mismatched run_id
    marker_mismatched_rid = {"run_id": "other-rid", "session_id": "mismatched-session-id-999"}
    res_rid = read_build_status(marker_mismatched_rid, marker_file)
    assert res_rid.present is True
    assert res_rid.valid is False
    assert res_rid.reason == "mismatched"


def test_read_build_status_build_run_id_fallback_join(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    data["build"]["run_id"] = "brid-join-target"
    (tmp_path / "tb-build-status.json").write_text(json.dumps(data), encoding="utf-8")
    marker_file = tmp_path / "tb-build-active.json"

    # Marker has only build_run_id, no run_id
    marker_only_brid = {
        "build_run_id": "brid-join-target",
        "session_id": data["build"]["session_id"],
    }
    res1 = read_build_status(marker_only_brid, marker_file)
    assert res1.valid is True
    assert res1.reason == "ok"
    assert res1.record["build"]["run_id"] == "brid-join-target"

    # Marker has both build_run_id and run_id; build_run_id takes precedence
    marker_both = {
        "build_run_id": "brid-join-target",
        "run_id": "old-stale-run-id",
        "session_id": data["build"]["session_id"],
    }
    res2 = read_build_status(marker_both, marker_file)
    assert res2.valid is True
    assert res2.reason == "ok"


def test_read_build_status_lower_seq_ignored(tmp_path):
    _clear_status_cache()
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": "run-seq-test", "session_id": "sess-seq-test"}

    data = _read_fixture("status_v1_valid.json")
    data["build"]["run_id"] = "run-seq-test"
    data["build"]["session_id"] = "sess-seq-test"
    data["seq"] = 5
    data["build"]["plan_title"] = "Seq 5 plan"
    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    res1 = read_build_status(marker, marker_file)
    assert res1.valid is True
    assert res1.seq == 5
    assert res1.record["build"]["plan_title"] == "Seq 5 plan"

    # Write a lower seq (seq = 3); must be ignored and last good read kept
    data["seq"] = 3
    data["build"]["plan_title"] = "Rolled back plan"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    res2 = read_build_status(marker, marker_file)
    assert res2.valid is True
    assert res2.seq == 5
    assert res2.record["build"]["plan_title"] == "Seq 5 plan"

    # Write a higher seq (seq = 8); accepted
    data["seq"] = 8
    data["build"]["plan_title"] = "Seq 8 plan"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    res3 = read_build_status(marker, marker_file)
    assert res3.valid is True
    assert res3.seq == 8
    assert res3.record["build"]["plan_title"] == "Seq 8 plan"

    # New run_id resets seq high-water mark
    marker_new = {"run_id": "run-new-lifecycle", "session_id": "sess-seq-test"}
    data["build"]["run_id"] = "run-new-lifecycle"
    data["seq"] = 0
    data["build"]["plan_title"] = "New run plan"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    res4 = read_build_status(marker_new, marker_file)
    assert res4.valid is True
    assert res4.seq == 0
    assert res4.record["build"]["plan_title"] == "New run plan"


def test_read_build_status_stale_flagged_not_hidden(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    written_dt = datetime.fromisoformat("2026-09-29T06:00:00+00:00")
    data["written_at"] = "2026-09-29T06:00:00Z"
    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    # Mtime 50 minutes ago
    mtime = written_dt.timestamp() + 100
    os.utime(status_file, (mtime, mtime))

    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    # now is 50 minutes after status_at (over the 30 min threshold)
    now = mtime + (50 * 60)
    res = read_build_status(marker, marker_file, now=now)

    assert res.present is True
    assert res.valid is True
    assert res.reason == "ok"
    assert res.fresh is False
    assert res.stale is True
    assert res.record is not None  # Never hidden
    assert res.record["build"]["plan_title"] == "Hermes worker plan b9/b10"


def test_read_build_status_future_written_at_clamped(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    now_dt = datetime.fromisoformat("2026-09-29T08:00:00+00:00")
    now_ts = now_dt.timestamp()

    # written_at is 2 hours into the future
    data["written_at"] = "2026-09-29T10:00:00Z"
    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(json.dumps(data), encoding="utf-8")

    # Mtime also set in the future
    future_mtime = now_ts + 7200
    os.utime(status_file, (future_mtime, future_mtime))

    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file, now=now_ts)
    assert res.valid is True
    assert res.status_at == now_ts  # Clamped to now

    # Future written_at with old mtime (e.g. 2 hours old) cannot make record look fresh
    old_mtime = now_ts - 7200
    os.utime(status_file, (old_mtime, old_mtime))
    res_old_mtime = read_build_status(marker, marker_file, now=now_ts)
    # status_at = min(written_at, mtime + 5 min) -> old_mtime + 300
    assert res_old_mtime.status_at == old_mtime + 300.0
    assert res_old_mtime.fresh is False
    assert res_old_mtime.stale is True


def test_read_build_status_wrong_typed_field_blanks_only_itself(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    data["phase"] = 12345
    data["progress"]["waves"]["done"] = "wrong_type"
    data["estimate"] = {"unit": "work_hours", "p50": 20.0, "p90": 5.0}  # p50 > p90
    data["gates"] = "not_a_list"
    data["seats"]["agy"]["spawned"] = "bad"
    data["lanes"] = 999
    data["ci"] = "bad_ci"
    data["build"]["plan_title"] = 888

    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(json.dumps(data), encoding="utf-8")
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file)
    assert res.valid is True
    assert res.reason == "ok"
    assert res.record["phase"] is None
    assert res.record["progress"]["waves"]["done"] is None
    assert res.record["progress"]["waves"]["total"] == 4
    assert res.record["estimate"] is None
    assert res.record["gates"] == []
    assert res.record["seats"]["agy"]["spawned"] is None
    assert res.record["seats"]["agy"]["refused"] == 3
    assert res.record["lanes"] is None
    assert res.record["ci"] == []
    assert res.record["build"]["plan_title"] is None
    assert res.record["build"]["run_id"] == data["build"]["run_id"]


def test_read_build_status_each_cap_enforced(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    data["build"]["plan_title"] = "P" * 200
    data["progress"]["current_wave"]["title"] = "W" * 150
    data["progress"]["current_units"] = [
        {"id": f"u{i}", "title": "T" * 150, "seat": "agy"} for i in range(15)
    ]
    data["progress"]["remaining_waves"] = [
        {"index": i, "id": f"w{i}", "title": "R" * 150, "units": 1} for i in range(20)
    ]
    data["gates"] = [
        {"id": f"g{i}", "kind": "ci", "label": "G" * 200, "state": "waiting"} for i in range(25)
    ]
    data["refusals"] = [
        {"seat": "agy", "code": f"c{i}", "count": 1, "note": "N" * 200} for i in range(15)
    ]
    data["lanes"]["job_ids"] = [f"job_{i}" for i in range(20)]
    data["ci"] = [
        {"kind": "run", "ref": f"ref_{i}", "url": "https://github.com/a/b", "state": "pending"}
        for i in range(10)
    ]
    data["owner_blockers"] = [
        {"id": f"ob{i}", "action": "approve", "label": "B" * 200} for i in range(12)
    ]
    data["other_names"] = [f"name_{i}" for i in range(15)]
    data["last_activity"]["what"] = "A" * 150

    status_file = tmp_path / "tb-build-status.json"
    status_file.write_text(json.dumps(data), encoding="utf-8")
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file)
    assert res.valid is True
    rec = res.record

    # List caps
    assert len(rec["progress"]["current_units"]) == 8
    assert len(rec["progress"]["remaining_waves"]) == 12
    assert len(rec["gates"]) == 16
    assert len(rec["refusals"]) == 10
    assert len(rec["lanes"]["job_ids"]) == 12
    assert len(rec["ci"]) == 6
    assert len(rec["owner_blockers"]) == 8
    assert len(rec["other_names"]) == 8

    # String caps
    assert len(rec["build"]["plan_title"]) == 120
    assert len(rec["progress"]["current_wave"]["title"]) == 80
    assert len(rec["progress"]["current_units"][0]["title"]) == 80
    assert len(rec["progress"]["remaining_waves"][0]["title"]) == 80
    assert len(rec["gates"][0]["label"]) == 120
    assert len(rec["refusals"][0]["note"]) == 120
    assert len(rec["owner_blockers"][0]["label"]) == 120
    assert len(rec["last_activity"]["what"]) == 80


def test_read_build_status_hostile_strings_masked_stripped_capped(tmp_path):
    _clear_status_cache()
    status_content = (_FIXTURES_DIR / "status_v1_hostile.json").read_text(encoding="utf-8")
    (tmp_path / "tb-build-status.json").write_text(status_content, encoding="utf-8")
    marker_file = tmp_path / "tb-build-active.json"
    marker = {
        "run_id": "cc546d7b99dc45d6a829da8e2c77a673",
        "session_id": "6d739ad6-1234-5678-9abc-def012345678",
    }

    res = read_build_status(marker, marker_file)
    assert res.valid is True
    rec = res.record

    # Absolute paths dropped in free-text fields
    assert rec["build"]["plan_title"] is None
    assert rec["build"]["branch"] is None
    assert rec["refusals"][0]["note"] is None

    # Embedded absolute paths stripped from labels
    assert "/private/tmp/secret.txt" not in rec["gates"][0]["label"]
    assert "/Users/justin/confidential.txt" not in rec["owner_blockers"][0]["label"]

    # Secret masking: fake Anthropic key and Bearer token masked
    assert "sk-ant-api03-" not in rec["progress"]["current_units"][0]["title"]
    assert "[REDACTED:anthropic-key:" in rec["progress"]["current_units"][0]["title"]
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in rec["owner_blockers"][0]["label"]

    # ANSI escapes and control characters stripped
    assert "\x1b" not in rec["progress"]["current_units"][0]["title"]
    assert "\x00" not in rec["progress"]["current_units"][0]["title"]
    assert "\x07" not in rec["progress"]["current_units"][0]["title"]
    assert "\x1b" not in rec["last_activity"]["what"]
    assert "\x08" not in rec["last_activity"]["what"]

    # 10k-char label capped
    assert len(rec["progress"]["current_wave"]["title"]) <= 80
    assert len(rec["gates"][1]["label"]) <= 120


def test_read_build_status_symlinked_status_file_refused(tmp_path):
    _clear_status_cache()
    data = _read_fixture("status_v1_valid.json")
    target_file = tmp_path / "real_status.json"
    target_file.write_text(json.dumps(data), encoding="utf-8")

    symlink_file = tmp_path / "tb-build-status.json"
    symlink_file.symlink_to(target_file)

    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "unreadable"


def test_read_build_status_oversize_refused(tmp_path):
    _clear_status_cache()
    oversize_file = _FIXTURES_DIR / "status_v1_oversize.json"
    assert oversize_file.stat().st_size > 64 * 1024

    status_file = tmp_path / "tb-build-status.json"
    status_file.write_bytes(oversize_file.read_bytes())

    data = _read_fixture("status_v1_valid.json")
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "unreadable"


def test_read_build_status_v2_only_unknown_schema(tmp_path):
    _clear_status_cache()
    status_content = (_FIXTURES_DIR / "status_v1_v2_only.json").read_text(encoding="utf-8")
    (tmp_path / "tb-build-status.json").write_text(status_content, encoding="utf-8")

    data = _read_fixture("status_v1_valid.json")
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": data["build"]["run_id"], "session_id": data["build"]["session_id"]}

    res = read_build_status(marker, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "unknown_schema"


def test_read_build_status_missing(tmp_path):
    _clear_status_cache()
    marker_file = tmp_path / "tb-build-active.json"
    marker = {"run_id": "r1", "session_id": "s1"}

    res = read_build_status(marker, marker_file)
    assert res.present is False
    assert res.valid is False
    assert res.reason == "missing"

