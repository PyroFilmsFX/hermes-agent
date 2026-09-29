"""Tests for bounded incremental reader of the conductor marker index."""

from __future__ import annotations

import json
import os
import pwd
import tempfile
from pathlib import Path

import pytest

from tui_gateway.conductor_roster import (
    IndexScan,
    _clear_cache,
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
