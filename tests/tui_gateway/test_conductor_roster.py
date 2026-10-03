"""Tests for bounded incremental reader of the conductor marker index."""

from __future__ import annotations

import json
import os
import pwd
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tui_gateway.conductor_roster import (
    Attribution,
    AttributionContext,
    Build,
    DerivedStatus,
    IndexScan,
    Row,
    StatusRead,
    _clear_cache,
    _clear_status_cache,
    attribute_build,
    derive_row_status,
    group_rows,
    read_build_status,
    read_marker_index,
)
from tui_gateway.methods_relay_jobs import _PUBLIC_JOB_KEYS, _list_relay_jobs


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
    scan1_mtime_ns = os.stat(index_file).st_mtime_ns

    # Replace file with new inode
    index_file.unlink()
    row2 = json.dumps(_open_row(m2, run_id="r2")) + "\n"
    index_file.write_text(row2, encoding="utf-8")
    # Linux may hand the new file the freed inode and this row is the same length; pin the
    # worst case (same dev/ino, size and mtime) so only the content can tell them apart.
    old_st = os.stat(index_file)
    os.utime(index_file, ns=(old_st.st_atime_ns, scan1_mtime_ns))

    scan2 = read_marker_index(index_file)
    assert len(scan2.entries) == 1
    assert scan2.entries[0]["marker_path"] == m2
    assert scan2.bytes_read == len(row2.encode("utf-8"))


def test_in_place_same_length_rewrite_is_not_served_from_cache(env):
    # Same inode, same size, same mtime: only the content changed. The cached scan must not
    # be served, and the incremental path must not read "nothing appended".
    home, _temp, index_file = env
    m1 = _valid_marker(home, "p1")
    m2 = _valid_marker(home, "p2")
    row1 = json.dumps(_open_row(m1, run_id="r1")) + "\n"
    row2 = json.dumps(_open_row(m2, run_id="r2")) + "\n"
    assert len(row1) == len(row2)

    index_file.write_text(row1, encoding="utf-8")
    assert read_marker_index(index_file).entries[0]["marker_path"] == m1
    before = os.stat(index_file)

    with open(index_file, "r+", encoding="utf-8") as fh:
        fh.write(row2)
    os.utime(index_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(index_file)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)

    scan = read_marker_index(index_file)
    assert [e["marker_path"] for e in scan.entries] == [m2]


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


def test_derive_row_status_no_status_file_derives_all_fields_with_derived_provenance():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {
        "run_id": "run-b4-test",
        "session_id": "sess-b4-test",
        "waves_total": 4,
        "waves_done": 1,
        "wave_stage": "impl",
        "waits": [
            {
                "kind": "ci",
                "ci_ref": "36533732367",
                "waiter": True,
                "what": "CI run 36533732367 on sync/land-b9",
                "since": "2026-09-29T07:05:00Z",
            }
        ],
        "blocked": True,
        "blocked_reason": "Reinstall owner verifier (admin prompt)",
        "armed_at": "2026-09-29T06:39:40Z",
    }
    jobs = [
        {
            "job_id": "w_01",
            "worker": "codex",
            "served_seat": "codex",
            "fallback_from": None,
            "marker_run_id": "run-b4-test",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
            "heartbeat_at": "2026-09-29T07:10:00Z",
        },
        {
            "job_id": "w_02",
            "worker": "gemini",  # gemini counts as agy
            "served_seat": "gemini",
            "fallback_from": None,
            "marker_run_id": "run-b4-test",
            "status": "succeeded",
            "spawned_at": "2026-09-29T06:50:00Z",
            "heartbeat_at": "2026-09-29T07:08:00Z",
        },
    ]

    res = derive_row_status(marker, None, jobs, now=ref_time)
    assert isinstance(res, DerivedStatus)
    assert res.stale is False

    # Every field derived from marker+jobs with provenance "derived"
    expected_fields = [
        "phase",
        "progress",
        "estimate",
        "gates",
        "seats",
        "other_names",
        "refusals",
        "lanes",
        "ci",
        "owner_blockers",
        "last_activity",
    ]
    for f in expected_fields:
        assert res.provenance[f] == "derived", f"Expected {f} to have provenance 'derived'"
    assert res.provenance["waves"] == "derived"
    assert all(prov == "derived" for prov in res.provenance.values())

    # Fallback values
    assert res.progress["waves"] == {"done": 1, "total": 4}
    assert res.progress["units"] is None
    assert res.progress["current_wave"]["index"] == 2
    assert res.progress["current_units"][0]["title"] == "impl"
    assert res.waves == {"done": 1, "total": 4}

    assert len(res.gates) == 1
    assert res.gates[0]["kind"] == "ci"
    assert res.gates[0]["ref"] == "36533732367"
    assert res.gates[0]["waiter"] is True
    assert res.gates[0]["label"] == "CI run 36533732367 on sync/land-b9"
    assert res.gates[0]["since"] == "2026-09-29T07:05:00Z"

    assert res.seats["codex"]["running"] == 1
    assert res.seats["codex"]["spawned"] == 1
    assert res.seats["agy"]["succeeded"] == 1  # gemini mapped to agy
    assert res.seats["agy"]["spawned"] == 1

    assert res.lanes["running"] == 1
    assert res.lanes["stale"] == 0
    assert res.lanes["job_ids"] == ["w_01", "w_02"]

    assert len(res.owner_blockers) == 1
    assert res.owner_blockers[0]["label"] == "Reinstall owner verifier (admin prompt)"
    assert res.blocked is True

    assert res.estimate is None
    assert res.refusals == []
    assert res.ci == []
    assert res.phase == "blocked"

    assert res.last_activity is not None
    assert res.last_activity["at"] == "2026-09-29T07:10:00Z"


def test_derive_row_status_fresh_status_wins_per_field():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    valid_data = _read_fixture("status_v1_valid.json")
    status = StatusRead(
        present=True,
        valid=True,
        fresh=True,
        stale=False,
        record=valid_data,
        status_at=ref_time - 60,
    )

    marker = {
        "run_id": valid_data["build"]["run_id"],
        "session_id": valid_data["build"]["session_id"],
        "waves_total": 10,
        "waves_done": 9,
        "waits": [{"kind": "external", "what": "Marker wait", "at": "2026-09-29T06:00:00Z"}],
        "blocked": True,
        "blocked_reason": "Marker blocked reason",
    }
    jobs = [
        {
            "job_id": "w_marker_lane",
            "worker": "muse",
            "marker_run_id": valid_data["build"]["run_id"],
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        }
    ]

    res = derive_row_status(marker, status, jobs, now=ref_time)
    assert res.stale is False

    # Fresh status record values win
    assert res.gates == valid_data["gates"]
    assert res.provenance["gates"] == "status"

    assert res.seats == valid_data["seats"]
    assert res.provenance["seats"] == "status"

    assert res.lanes == valid_data["lanes"]
    assert res.provenance["lanes"] == "status"

    assert res.progress == valid_data["progress"]
    assert res.provenance["progress"] == "status"

    assert res.estimate == valid_data["estimate"]
    assert res.provenance["estimate"] == "status"

    assert res.owner_blockers == valid_data["owner_blockers"]
    assert res.provenance["owner_blockers"] == "status"

    assert res.last_activity == valid_data["last_activity"]
    assert res.provenance["last_activity"] == "status"

    assert res.phase == valid_data["phase"]
    assert res.provenance["phase"] == "status"

    # Now verify missing field in status falls back per field
    partial_data = dict(valid_data)
    partial_data["estimate"] = None
    partial_data["progress"] = None
    status_partial = StatusRead(
        present=True,
        valid=True,
        fresh=True,
        stale=False,
        record=partial_data,
        status_at=ref_time - 60,
    )

    res_partial = derive_row_status(marker, status_partial, jobs, now=ref_time)
    # Estimate and progress fell back
    assert res_partial.estimate is None
    assert res_partial.provenance["estimate"] == "derived"
    assert res_partial.progress["waves"] == {"done": 9, "total": 10}
    assert res_partial.provenance["progress"] == "derived"
    # Gates and seats still won from status
    assert res_partial.gates == valid_data["gates"]
    assert res_partial.provenance["gates"] == "status"
    assert res_partial.seats == valid_data["seats"]
    assert res_partial.provenance["seats"] == "status"


def test_derive_row_status_stale_status_kept_and_flagged():
    ref_time = datetime.fromisoformat("2026-09-29T08:00:00+00:00").timestamp()
    valid_data = _read_fixture("status_v1_valid.json")
    status = StatusRead(
        present=True,
        valid=True,
        fresh=False,
        stale=True,
        record=valid_data,
        status_at=ref_time - 3600,
    )

    marker = {
        "run_id": valid_data["build"]["run_id"],
        "session_id": valid_data["build"]["session_id"],
        "waves_total": 99,
        "waves_done": 0,
        "waits": [{"kind": "date", "what": "Marker wait", "at": "2026-09-29T06:00:00Z"}],
        "blocked": True,
        "blocked_reason": "Marker blocked",
    }

    res = derive_row_status(marker, status, [], now=ref_time)
    # Flagged with stale=True
    assert res.stale is True
    assert res["stale"] is True

    # Stale status fields kept, never hidden or overwritten by marker fallbacks
    assert res.gates == valid_data["gates"]
    assert res.provenance["gates"] == "status"
    assert res.seats == valid_data["seats"]
    assert res.provenance["seats"] == "status"
    assert res.owner_blockers == valid_data["owner_blockers"]
    assert res.provenance["owner_blockers"] == "status"
    assert res.estimate == valid_data["estimate"]
    assert res.provenance["estimate"] == "status"


def test_derive_row_status_jobs_with_different_marker_run_id_excluded():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {"run_id": "run-target", "waves_total": 2, "waves_done": 0}
    jobs = [
        {
            "job_id": "w_matched",
            "worker": "codex",
            "marker_run_id": "run-target",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
        {
            "job_id": "w_other",
            "worker": "codex",
            "marker_run_id": "run-other",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
    ]

    res = derive_row_status(marker, None, jobs, now=ref_time)
    assert res.lanes["running"] == 1
    assert res.lanes["job_ids"] == ["w_matched"]
    assert res.seats["codex"]["running"] == 1
    assert res.seats["codex"]["spawned"] == 1


def test_derive_row_status_jobs_without_marker_run_id_excluded():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {"run_id": "run-target", "waves_total": 2, "waves_done": 0}
    jobs = [
        {
            "job_id": "w_matched",
            "worker": "codex",
            "marker_run_id": "run-target",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
        {
            "job_id": "w_no_brid",
            "worker": "codex",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
        {
            "job_id": "w_none_brid",
            "worker": "codex",
            "marker_run_id": None,
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
        {
            "job_id": "w_empty_brid",
            "worker": "codex",
            "marker_run_id": "",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
    ]

    res = derive_row_status(marker, None, jobs, now=ref_time)
    assert res.lanes["running"] == 1
    assert res.lanes["job_ids"] == ["w_matched"]
    assert res.seats["codex"]["running"] == 1
    assert res.seats["codex"]["spawned"] == 1


def test_derive_row_status_marker_build_run_id_precedence_for_jobs():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {
        "build_run_id": "brid-primary",
        "run_id": "legacy-run-id",
        "waves_total": 1,
        "waves_done": 0,
    }
    jobs = [
        {
            "job_id": "w_brid_matched",
            "worker": "muse",
            "marker_run_id": "brid-primary",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
        {
            "job_id": "w_legacy_excluded",
            "worker": "muse",
            "marker_run_id": "legacy-run-id",
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        },
    ]

    res = derive_row_status(marker, None, jobs, now=ref_time)
    assert res.lanes["running"] == 1
    assert res.lanes["job_ids"] == ["w_brid_matched"]


def test_derive_row_status_marker_waits_map_to_gates():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {
        "run_id": "run-waits-test",
        "waits": [
            {
                "id": "gate-1",
                "kind": "ci",
                "ci_ref": "36533732367",
                "waiter": True,
                "what": "CI run on branch",
                "since": "2026-09-29T07:00:00Z",
                "due": "2026-09-29T07:30:00Z",
                "state": "waiting",
            },
            {
                "id": "gate-2",
                "kind": "owner",
                "ref": "dec-1",
                "waiter": False,
                "label": "Owner approval needed",
                "at": "2026-09-29T07:02:00Z",
            },
        ],
    }

    res = derive_row_status(marker, None, [], now=ref_time)
    assert len(res.gates) == 2
    assert res.gates[0]["id"] == "gate-1"
    assert res.gates[0]["kind"] == "ci"
    assert res.gates[0]["ref"] == "36533732367"
    assert res.gates[0]["waiter"] is True
    assert res.gates[0]["label"] == "CI run on branch"
    assert res.gates[0]["since"] == "2026-09-29T07:00:00Z"
    assert res.gates[0]["due"] == "2026-09-29T07:30:00Z"
    assert res.gates[0]["state"] == "waiting"

    assert res.gates[1]["id"] == "gate-2"
    assert res.gates[1]["kind"] == "owner"
    assert res.gates[1]["ref"] == "dec-1"
    assert res.gates[1]["waiter"] is False
    assert res.gates[1]["label"] == "Owner approval needed"
    assert res.gates[1]["since"] == "2026-09-29T07:02:00Z"
    assert res.gates[1]["state"] == "waiting"

    assert res.provenance["gates"] == "derived"


def test_derive_row_status_blocked_marker_maps_to_owner_blocker():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    # 1. Blocked with blocked_reason
    marker_reason = {
        "run_id": "run-b1",
        "blocked": True,
        "blocked_reason": "API key quota exhausted",
        "blocked_at": "2026-09-29T07:01:00Z",
    }
    res1 = derive_row_status(marker_reason, None, [], now=ref_time)
    assert len(res1.owner_blockers) == 1
    assert res1.owner_blockers[0]["label"] == "API key quota exhausted"
    assert res1.owner_blockers[0]["since"] == "2026-09-29T07:01:00Z"
    assert res1.provenance["owner_blockers"] == "derived"
    assert res1.blocked is True

    # 2. Blocked without reason, falls back to waiting_on
    marker_waiting_on = {
        "run_id": "run-b2",
        "blocked": True,
        "waiting_on": "Review from security lead",
        "wait_since": "2026-09-29T07:02:00Z",
    }
    res2 = derive_row_status(marker_waiting_on, None, [], now=ref_time)
    assert len(res2.owner_blockers) == 1
    assert res2.owner_blockers[0]["label"] == "Review from security lead"
    assert res2.owner_blockers[0]["since"] == "2026-09-29T07:02:00Z"

    # 3. Blocked without any label fields falls back to 'Blocked'
    marker_bare_blocked = {"run_id": "run-b3", "blocked": True}
    res3 = derive_row_status(marker_bare_blocked, None, [], now=ref_time)
    assert len(res3.owner_blockers) == 1
    assert res3.owner_blockers[0]["label"] == "Blocked"

    # 4. Not blocked
    marker_unblocked = {"run_id": "run-b4", "blocked": False}
    res4 = derive_row_status(marker_unblocked, None, [], now=ref_time)
    assert res4.owner_blockers == []
    assert res4.blocked is False


def test_derive_row_status_last_activity_max_calculation():
    now = datetime.fromisoformat("2026-09-29T08:00:00+00:00").timestamp()
    t_marker = now - 500
    t_job = now - 200
    t_status = now - 350

    marker = {
        "run_id": "r-act",
        "mtime": t_marker,
    }
    jobs = [
        {
            "job_id": "w_act",
            "marker_run_id": "r-act",
            "worker": "codex",
            "heartbeat_epoch": t_job,
            "status": "running",
            "spawned_at": "2026-09-29T07:00:00Z",
        }
    ]
    status = StatusRead(
        present=True,
        valid=False,  # invalid/unreadable -> status_at considered for fallback max
        status_at=t_status,
    )

    # Job is newest (now - 200)
    res = derive_row_status(marker, status, jobs, now=now)
    assert res.last_activity is not None
    assert res.last_activity_at == t_job
    expected_iso = datetime.fromtimestamp(t_job, timezone.utc).isoformat().replace("+00:00", "Z")
    assert res.last_activity["at"] == expected_iso


def test_derive_row_status_hostile_strings_masked_in_fallbacks():
    now = datetime.fromisoformat("2026-09-29T08:00:00+00:00").timestamp()
    marker = {
        "run_id": "r-hostile",
        "blocked": True,
        "blocked_reason": "Token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and /secret/path",
        "wave_stage": "\x1b[31mRed Alert\x07" + "A" * 200,
        "waits": [
            {
                "kind": "ci",
                "what": "Key sk-ant-api03-abcdef123456789012345678901234567890 and /private/tmp/secret.txt",
            }
        ],
    }

    res = derive_row_status(marker, None, [], now=now)
    # Masked / cleaned
    assert "eyJhbGci" not in res.owner_blockers[0]["label"]
    assert "/secret/path" not in res.owner_blockers[0]["label"]

    assert "\x1b" not in res.progress["current_units"][0]["title"]
    assert len(res.progress["current_units"][0]["title"]) <= 80

    assert "sk-ant-api03-" not in res.gates[0]["label"]
    assert "/private/tmp/secret.txt" not in res.gates[0]["label"]


def test_relay_jobs_served_seat_and_fallback_from_never_in_public_dict(tmp_path):
    ws = tmp_path / "workspace"
    jobs_dir = ws / ".claude" / "state" / "worker-spawn" / "jobs"
    jobs_dir.mkdir(parents=True)

    job_data = {
        "job_id": "w_test_keys",
        "worker": "codex",
        "model": "gpt-6-sol",
        "model_resolved": "gpt-6-sol",
        "lane": "impl",
        "role": "worker",
        "status": "running",
        "spawned_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "duration_sec": 10.0,
        "served_seat": "codex",
        "fallback_from": "sonnet",
        "build_run_id": "run-test-keys",
        "marker_run_id": "marker-run-keys",
    }
    (jobs_dir / "w_test_keys.json").write_text(json.dumps(job_data), encoding="utf-8")

    # 1. Public listing (default internal=False)
    public_jobs = _list_relay_jobs(str(ws), internal=False)
    assert len(public_jobs) == 1
    public_dict = public_jobs[0]
    # Assert exact key set of the public projection
    assert set(public_dict.keys()) == _PUBLIC_JOB_KEYS
    assert "served_seat" not in public_dict
    assert "fallback_from" not in public_dict
    assert "build_run_id" not in public_dict
    assert "marker_run_id" not in public_dict

    # 2. Internal listing
    internal_jobs = _list_relay_jobs(str(ws), internal=True)
    assert len(internal_jobs) == 1
    internal_dict = internal_jobs[0]
    assert internal_dict["served_seat"] == "codex"
    assert internal_dict["fallback_from"] == "sonnet"
    assert internal_dict["build_run_id"] == "run-test-keys"
    assert internal_dict["marker_run_id"] == "marker-run-keys"


# ---------------------------------------------------------------------------
# B5 Attribution Ladder and Row Grouping Tests (§5, §2)
# ---------------------------------------------------------------------------


def test_attribution_rung_live_cli_in_isolation():
    marker = {"session_id": "claude-live-01", "run_id": "r1"}
    ctx = AttributionContext(
        live_cli_map={"claude-live-01": ("default", "hermes-live-01")},
    )
    attr = attribute_build(marker, Path("/repos/p/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "live_cli"
    assert attr.profile == "default"
    assert attr.hermes_session_id == "hermes-live-01"
    assert attr.attributed is True


def test_attribution_rung_binding_in_isolation():
    marker = {"session_id": "other-sid", "run_id": "r2"}
    status = StatusRead(
        present=True,
        valid=True,
        record={"build": {"binding_nonce": "nonce-alpha-42"}},
    )
    ctx = AttributionContext(
        bindings={"nonce-alpha-42": ("work", "hermes-bound-02")},
    )
    attr = attribute_build(marker, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr.via == "binding"
    assert attr.profile == "work"
    assert attr.hermes_session_id == "hermes-bound-02"
    assert attr.attributed is True


def test_attribution_rung_stamped_in_isolation():
    marker = {"session_id": "other-sid", "run_id": "r3"}
    status = StatusRead(
        present=True,
        valid=True,
        record={"build": {"hermes_session_id": "hermes-stamped-03"}},
    )
    ctx = AttributionContext(
        sessions={("default", "hermes-stamped-03"): True},
    )
    attr = attribute_build(marker, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr.via == "stamped"
    assert attr.profile == "default"
    assert attr.hermes_session_id == "hermes-stamped-03"
    assert attr.attributed is True


def test_attribution_rung_statedb_in_isolation():
    marker = {"session_id": "claude-db-04", "run_id": "r4"}
    ctx = AttributionContext(
        claude_sid_to_session={"claude-db-04": ("research", "hermes-statedb-04")},
    )
    attr = attribute_build(marker, Path("/repos/p/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "statedb"
    assert attr.profile == "research"
    assert attr.hermes_session_id == "hermes-statedb-04"
    assert attr.attributed is True


def test_attribution_rung_workspace_in_isolation():
    marker = {"session_id": "claude-other", "run_id": "r5", "context_path": "/repos/proj"}
    now = 1790670000.0
    ctx = AttributionContext(
        workspace_sessions=[
            {
                "profile": "default",
                "session_id": "hermes-ws-05",
                "cwd": "/repos/proj",
                "last_active_at": now - 3600.0,
            }
        ],
        common_repo_root=lambda p: "/repos/proj" if "proj" in str(p) else "",
        now=now,
    )
    attr = attribute_build(marker, Path("/repos/proj/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "workspace"
    assert attr.profile == "default"
    assert attr.hermes_session_id == "hermes-ws-05"
    assert attr.attributed is True


def test_attribution_rung_unattributed_in_isolation():
    marker = {"session_id": "claude-unknown", "run_id": "r6", "context_path": "/repos/unmatched"}
    ctx = AttributionContext(
        common_repo_root=lambda p: "",
    )
    attr = attribute_build(marker, Path("/repos/unmatched/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "unattributed"
    assert attr.profile is None
    assert attr.hermes_session_id is None
    assert attr.attributed is False


def test_attribution_precedence_when_several_match():
    now = 1790670000.0
    marker = {"session_id": "claude-shared", "run_id": "r_prec", "context_path": "/repos/proj"}
    status = StatusRead(
        present=True,
        valid=True,
        record={"build": {"binding_nonce": "nonce-shared", "hermes_session_id": "h_stamp"}},
    )
    m_path = Path("/repos/proj/.claude/state/tb-build-active.json")

    # Step 1: All active -> live_cli wins
    ctx1 = AttributionContext(
        live_cli_map={"claude-shared": ("p_live", "h_live")},
        bindings={"nonce-shared": ("p_bind", "h_bind")},
        sessions={("p_stamp", "h_stamp"): True},
        claude_sid_to_session={"claude-shared": ("p_db", "h_db")},
        workspace_sessions=[{"profile": "p_ws", "session_id": "h_ws", "cwd": "/repos/proj", "last_active_at": now}],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr1 = attribute_build(marker, m_path, status, ctx1)
    assert attr1.via == "live_cli"
    assert attr1.hermes_session_id == "h_live"

    # Step 2: Remove live_cli -> binding wins
    ctx2 = AttributionContext(
        bindings={"nonce-shared": ("p_bind", "h_bind")},
        sessions={("p_stamp", "h_stamp"): True},
        claude_sid_to_session={"claude-shared": ("p_db", "h_db")},
        workspace_sessions=[{"profile": "p_ws", "session_id": "h_ws", "cwd": "/repos/proj", "last_active_at": now}],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr2 = attribute_build(marker, m_path, status, ctx2)
    assert attr2.via == "binding"
    assert attr2.hermes_session_id == "h_bind"

    # Step 3: Remove binding -> stamped wins
    ctx3 = AttributionContext(
        sessions={("p_stamp", "h_stamp"): True},
        claude_sid_to_session={"claude-shared": ("p_db", "h_db")},
        workspace_sessions=[{"profile": "p_ws", "session_id": "h_ws", "cwd": "/repos/proj", "last_active_at": now}],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr3 = attribute_build(marker, m_path, status, ctx3)
    assert attr3.via == "stamped"
    assert attr3.hermes_session_id == "h_stamp"

    # Step 4: Remove stamped -> statedb wins
    ctx4 = AttributionContext(
        claude_sid_to_session={"claude-shared": ("p_db", "h_db")},
        workspace_sessions=[{"profile": "p_ws", "session_id": "h_ws", "cwd": "/repos/proj", "last_active_at": now}],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr4 = attribute_build(marker, m_path, None, ctx4)
    assert attr4.via == "statedb"
    assert attr4.hermes_session_id == "h_db"

    # Step 5: Remove statedb -> workspace wins
    ctx5 = AttributionContext(
        workspace_sessions=[{"profile": "p_ws", "session_id": "h_ws", "cwd": "/repos/proj", "last_active_at": now}],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr5 = attribute_build(marker, m_path, None, ctx5)
    assert attr5.via == "workspace"
    assert attr5.hermes_session_id == "h_ws"


def test_attribution_ambiguous_workspace_unattributed():
    now = 1790670000.0
    marker = {"session_id": "claude-ambig", "run_id": "r_ambig", "context_path": "/repos/proj"}
    ctx = AttributionContext(
        workspace_sessions=[
            {"profile": "default", "session_id": "session-A", "cwd": "/repos/proj", "last_active_at": now - 100},
            {"profile": "work", "session_id": "session-B", "cwd": "/repos/proj", "last_active_at": now - 200},
        ],
        common_repo_root=lambda p: "/repos/proj" if "proj" in str(p) else "",
        now=now,
    )
    attr = attribute_build(marker, Path("/repos/proj/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "unattributed"
    assert attr.profile is None
    assert attr.hermes_session_id is None


def test_attribution_workspace_old_session_ignored():
    now = 1790670000.0
    marker = {"session_id": "claude-old", "run_id": "r_old", "context_path": "/repos/proj"}
    ctx = AttributionContext(
        workspace_sessions=[
            {"profile": "default", "session_id": "session-old", "cwd": "/repos/proj", "last_active_at": now - 25 * 3600},
        ],
        common_repo_root=lambda p: "/repos/proj",
        now=now,
    )
    attr = attribute_build(marker, Path("/repos/proj/.claude/state/tb-build-active.json"), None, ctx)
    assert attr.via == "unattributed"


def test_attribution_untrusted_context_rejection():
    marker = {"session_id": "claude-attacker", "run_id": "r_bad"}
    status = StatusRead(
        present=True,
        valid=True,
        record={"build": {"hermes_session_id": "hsid-unconfirmed", "binding_nonce": "nonce-unregistered"}},
    )
    ctx = AttributionContext(
        sessions={("default", "real-session"): True},
        bindings={"registered-nonce": ("default", "real-session")},
    )
    attr = attribute_build(marker, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr.via == "unattributed"


def test_attribution_stamped_profile_isolation():
    marker_no_profile = {"session_id": "claude-x", "run_id": "r_p1"}
    status = StatusRead(
        present=True,
        valid=True,
        record={"build": {"hermes_session_id": "shared-hsid"}},
    )
    ctx = AttributionContext(
        sessions={
            ("profile_a", "shared-hsid"): True,
            ("profile_b", "shared-hsid"): True,
        }
    )
    attr_ambig = attribute_build(marker_no_profile, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr_ambig.via == "unattributed"

    marker_with_prof = {"session_id": "claude-x", "run_id": "r_p2", "profile": "profile_a"}
    attr_matched = attribute_build(marker_with_prof, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr_matched.via == "stamped"
    assert attr_matched.profile == "profile_a"
    assert attr_matched.hermes_session_id == "shared-hsid"

    marker_wrong_prof = {"session_id": "claude-x", "run_id": "r_p3", "profile": "profile_c"}
    attr_rejected = attribute_build(marker_wrong_prof, Path("/repos/p/.claude/state/tb-build-active.json"), status, ctx)
    assert attr_rejected.via == "unattributed"


def test_attribution_nested_marker_project_and_branch():
    nested_path = Path("/Users/dev/repos/myproj/.claude/worktrees/lane-362/.claude/state/tb-build-active.json")
    marker = {
        "session_id": "claude-nested",
        "run_id": "r_nested",
        "context_path": "/Users/dev/repos/myproj/.claude/worktrees/lane-362",
    }
    ctx = AttributionContext(
        live_cli_map={"claude-nested": ("default", "hermes-nested-worker")},
        common_repo_root=lambda p: "/Users/dev/repos/myproj" if "myproj" in str(p) else "",
        lane_branches={"lane-362": "cntrl-hermes-worker"},
    )
    attr = attribute_build(marker, nested_path, None, ctx)
    assert attr.is_nested is True
    assert attr.lane == "lane-362"
    assert attr.project == "/Users/dev/repos/myproj"
    assert attr.branch == "cntrl-hermes-worker"
    assert attr.via == "live_cli"
    assert attr.hermes_session_id == "hermes-nested-worker"


def test_group_rows_nested_marker_separate_when_owned_by_another_session():
    b_enclosing = {
        "marker": {"session_id": "claude-1", "run_id": "r_enc", "armed_at": "2026-09-29T06:00:00Z"},
        "marker_path": Path("/repos/myproj/.claude/state/tb-build-active.json"),
        "attribution": Attribution(via="live_cli", profile="default", hermes_session_id="session-1"),
        "last_activity_at": 1000.0,
    }
    b_nested = {
        "marker": {"session_id": "claude-2", "run_id": "r_nest", "armed_at": "2026-09-29T06:30:00Z"},
        "marker_path": Path("/repos/myproj/.claude/worktrees/lane-362/.claude/state/tb-build-active.json"),
        "attribution": Attribution(via="live_cli", profile="default", hermes_session_id="session-2", is_nested=True),
        "last_activity_at": 2000.0,
    }

    rows = group_rows([b_enclosing, b_nested])
    assert len(rows) == 2
    row_sessions = {r.hermes_session_id for r in rows}
    assert row_sessions == {"session-1", "session-2"}
    for r in rows:
        assert len(r.extra_builds) == 0


def test_group_rows_nested_marker_grouped_when_same_session():
    b_enclosing = {
        "marker": {"session_id": "claude-1", "run_id": "r_enc", "armed_at": "2026-09-29T06:00:00Z"},
        "marker_path": Path("/repos/myproj/.claude/state/tb-build-active.json"),
        "attribution": Attribution(via="live_cli", profile="default", hermes_session_id="session-1"),
        "last_activity_at": 1000.0,
    }
    b_nested = {
        "marker": {"session_id": "claude-1", "run_id": "r_nest", "armed_at": "2026-09-29T06:30:00Z"},
        "marker_path": Path("/repos/myproj/.claude/worktrees/lane-362/.claude/state/tb-build-active.json"),
        "attribution": Attribution(via="live_cli", profile="default", hermes_session_id="session-1", is_nested=True),
        "last_activity_at": 2000.0,
    }

    rows = group_rows([b_enclosing, b_nested])
    assert len(rows) == 1
    row = rows[0]
    assert row.hermes_session_id == "session-1"
    assert row.primary["marker"]["run_id"] == "r_nest"
    assert len(row.extra_builds) == 1
    assert row.extra_builds[0]["marker"]["run_id"] == "r_enc"


def test_group_rows_picks_primary_by_activity_then_armed_at():
    b1 = {
        "marker": {"session_id": "c1", "run_id": "b1", "armed_at": "2026-09-29T05:00:00Z"},
        "attribution": Attribution(via="stamped", profile="default", hermes_session_id="h1"),
        "last_activity_at": 1000.0,
    }
    b2 = {
        "marker": {"session_id": "c1", "run_id": "b2", "armed_at": "2026-09-29T01:00:00Z"},
        "attribution": Attribution(via="stamped", profile="default", hermes_session_id="h1"),
        "last_activity_at": 2000.0,
    }
    b3 = {
        "marker": {"session_id": "c1", "run_id": "b3", "armed_at": "2026-09-29T06:00:00Z"},
        "attribution": Attribution(via="stamped", profile="default", hermes_session_id="h1"),
        "last_activity_at": 1000.0,
    }

    rows = group_rows([b1, b2, b3])
    assert len(rows) == 1
    row = rows[0]
    assert row.primary["marker"]["run_id"] == "b2"
    assert len(row.extra_builds) == 2
    assert row.extra_builds[0]["marker"]["run_id"] == "b3"
    assert row.extra_builds[1]["marker"]["run_id"] == "b1"

    t1 = {
        "marker": {"session_id": "c2", "run_id": "t1", "armed_at": "2026-09-29T02:00:00Z"},
        "attribution": Attribution(via="stamped", profile="default", hermes_session_id="h2"),
        "last_activity_at": 3000.0,
    }
    t2 = {
        "marker": {"session_id": "c2", "run_id": "t2", "armed_at": "2026-09-29T04:00:00Z"},
        "attribution": Attribution(via="stamped", profile="default", hermes_session_id="h2"),
        "last_activity_at": 3000.0,
    }
    tie_rows = group_rows([t1, t2])
    assert len(tie_rows) == 1
    assert tie_rows[0].primary["marker"]["run_id"] == "t2"
    assert tie_rows[0].extra_builds[0]["marker"]["run_id"] == "t1"


def test_group_rows_unattributed_grouped_by_marker_session_id():
    u1 = {
        "marker": {"session_id": "marker-sid-A", "run_id": "u1", "armed_at": "2026-09-29T01:00:00Z"},
        "attribution": Attribution(via="unattributed"),
        "last_activity_at": 100.0,
    }
    u2 = {
        "marker": {"session_id": "marker-sid-A", "run_id": "u2", "armed_at": "2026-09-29T02:00:00Z"},
        "attribution": Attribution(via="unattributed"),
        "last_activity_at": 200.0,
    }
    u3 = {
        "marker": {"session_id": "marker-sid-B", "run_id": "u3", "armed_at": "2026-09-29T03:00:00Z"},
        "attribution": Attribution(via="unattributed"),
        "last_activity_at": 150.0,
    }

    rows = group_rows([u1, u2, u3])
    assert len(rows) == 2
    row_a = next(r for r in rows if r.primary["marker"]["session_id"] == "marker-sid-A")
    assert row_a.primary["marker"]["run_id"] == "u2"
    assert len(row_a.extra_builds) == 1
    assert row_a.extra_builds[0]["marker"]["run_id"] == "u1"

    row_b = next(r for r in rows if r.primary["marker"]["session_id"] == "marker-sid-B")
    assert row_b.primary["marker"]["run_id"] == "u3"
    assert len(row_b.extra_builds) == 0




def test_caller_run_label_on_a_job_never_joins_the_build():
    # msg 1073: job.build_run_id is the caller's run label ("mcp55"), even when it happens to
    # equal the marker's id; only marker_run_id (stamped by conductor 3.61.15) joins.
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker = {"run_id": "run-target", "waves_total": 1, "waves_done": 0}
    jobs = [
        {"job_id": "w_label_only", "worker": "codex", "build_run_id": "run-target",
         "status": "running", "spawned_at": "2026-09-29T07:00:00Z"},
        {"job_id": "w_stamped", "worker": "codex", "build_run_id": "mcp55", "marker_run_id": "run-target",
         "status": "running", "spawned_at": "2026-09-29T07:00:00Z"},
    ]
    res = derive_row_status(marker, None, jobs, now=ref_time)
    assert res.lanes["job_ids"] == ["w_stamped"]


def test_marker_without_an_id_matches_no_jobs():
    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    jobs = [{"job_id": "w_unstamped", "worker": "codex", "status": "running",
             "spawned_at": "2026-09-29T07:00:00Z"}]
    res = derive_row_status({"waves_total": 1, "waves_done": 0}, None, jobs, now=ref_time)
    assert res.lanes["job_ids"] == []
