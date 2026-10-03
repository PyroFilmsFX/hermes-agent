"""Conformance tests for conductor per-build status record fixtures (tb-build-status/v1).

These fixtures serve as the golden set shared between conductor-worker (the writer)
and Hermes (the reader). Every tests/fixtures/conductor/status_v1_*.json file is loaded
and evaluated against the reader contract enforced by tui_gateway/conductor_roster.py:
- valid: parsed successfully, valid=True, reason="ok", evaluates as fresh or stale
- mismatched: join keys differ from marker, valid=False, reason="mismatched"
- v2_only: unknown/future schema, valid=False, reason="unknown_schema"
- oversize: exceeds 64 KiB limit, valid=False, reason="unreadable"
- hostile: parsed with secrets masked, control/ANSI stripped, caps enforced, and
  all absolute paths dropped or stripped
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tui_gateway.conductor_roster import (
    StatusRead,
    _clear_status_cache,
    read_build_status,
)


_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "conductor"

_STANDARD_MARKER = {
    "run_id": "cc546d7b99dc45d6a829da8e2c77a673",
    "session_id": "6d739ad6-1234-5678-9abc-def012345678",
}

_ABS_PATH_CHECK = re.compile(
    r"(?:^|(?<=\s))(?:/(?:[^\s/]+/)+[^\s/]*|/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]*)+|~/(?:[^\s/]+/)*[^\s/]*|[A-Za-z]:[/\\][^\s]*|\\\\[^\s]+)(?=\s|$)"
)


def _setup_status_file(tmp_path: Path, fixture_path: Path, mtime: float | None = None) -> Path:
    """Copy fixture to tmp_path as tb-build-status.json and return marker path."""
    status_file = tmp_path / "tb-build-status.json"
    shutil.copyfile(fixture_path, status_file)
    if mtime is not None:
        os.utime(status_file, (mtime, mtime))
    return tmp_path / "tb-build-active.json"


def _assert_no_absolute_paths(obj: Any, path: str = "") -> None:
    """Recursively assert that no string in the coerced record contains an absolute path."""
    if isinstance(obj, str):
        assert not obj.startswith(("/", "~", "\\\\")), f"Path starting with slash/tilde found at {path}: {obj!r}"
        assert not re.match(r"^[A-Za-z]:[/\\]", obj), f"Windows drive path found at {path}: {obj!r}"
        assert not _ABS_PATH_CHECK.search(obj), f"Embedded absolute path found at {path}: {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_absolute_paths(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _assert_no_absolute_paths(item, f"{path}[{idx}]")


def test_fixture_directory_contains_expected_files():
    """Ensure fixtures directory exists and contains all required golden fixture files."""
    assert _FIXTURES_DIR.is_dir(), f"Fixtures directory {_FIXTURES_DIR} not found"
    fixture_files = sorted(_FIXTURES_DIR.glob("status_v1_*.json"))
    assert len(fixture_files) >= 5, f"Expected at least 5 fixtures, found: {[f.name for f in fixture_files]}"

    names = {f.name for f in fixture_files}
    assert "status_v1_valid.json" in names
    assert "status_v1_mismatched.json" in names
    assert "status_v1_v2_only.json" in names
    assert "status_v1_oversize.json" in names
    assert "status_v1_hostile.json" in names


def test_all_fixtures_reader_verdict_conformance(tmp_path):
    """Load every status_v1_*.json fixture and assert the reader's verdict matches filename."""
    fixtures = sorted(_FIXTURES_DIR.glob("status_v1_*.json"))
    assert len(fixtures) > 0

    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()

    for fixture in fixtures:
        _clear_status_cache()
        sub_tmp = tmp_path / fixture.stem
        sub_tmp.mkdir(parents=True, exist_ok=True)
        marker_file = _setup_status_file(sub_tmp, fixture, mtime=ref_time - 60)

        res = read_build_status(_STANDARD_MARKER, marker_file, now=ref_time)
        assert isinstance(res, StatusRead)
        assert res.present is True

        stem = fixture.stem
        if "valid" in stem and "v2" not in stem and "mismatched" not in stem and "oversize" not in stem and "hostile" not in stem:
            assert res.valid is True, f"{fixture.name} should be valid"
            assert res.reason == "ok"
            assert res.record is not None
            assert res.fresh is True
            assert res.stale is False
        elif "mismatched" in stem:
            assert res.valid is False, f"{fixture.name} should be invalid (mismatched)"
            assert res.reason == "mismatched"
            assert res.record is None
        elif "v2_only" in stem:
            assert res.valid is False, f"{fixture.name} should be invalid (unknown_schema)"
            assert res.reason == "unknown_schema"
            assert res.record is None
        elif "oversize" in stem:
            assert res.valid is False, f"{fixture.name} should be invalid (unreadable)"
            assert res.reason == "unreadable"
            assert res.record is None
        elif "hostile" in stem:
            assert res.valid is True, f"{fixture.name} should be valid with coerced sanitization"
            assert res.reason == "ok"
            assert res.record is not None
            _assert_no_absolute_paths(res.record)


def test_fixture_valid_detailed_conformance(tmp_path):
    """Detailed assertions on status_v1_valid.json golden fixture."""
    _clear_status_cache()
    fixture = _FIXTURES_DIR / "status_v1_valid.json"
    raw_data = json.loads(fixture.read_text(encoding="utf-8"))

    ref_time = datetime.fromisoformat("2026-09-29T07:15:00+00:00").timestamp()
    marker_file = _setup_status_file(tmp_path, fixture, mtime=ref_time - 120)

    # 1. Fresh read evaluation
    res_fresh = read_build_status(_STANDARD_MARKER, marker_file, now=ref_time)
    assert res_fresh.present is True
    assert res_fresh.valid is True
    assert res_fresh.reason == "ok"
    assert res_fresh.seq == 42
    assert res_fresh.fresh is True
    assert res_fresh.stale is False
    assert res_fresh.record is not None
    assert res_fresh.data is res_fresh.record

    # Verify key fields
    rec = res_fresh.record
    assert rec["schema"] == "tb-build-status/v1"
    assert rec["seq"] == 42
    assert rec["conductor_version"] == "3.62.0"
    assert rec["build"]["run_id"] == "cc546d7b99dc45d6a829da8e2c77a673"
    assert rec["build"]["session_id"] == "6d739ad6-1234-5678-9abc-def012345678"
    assert rec["build"]["build_id"] == "b_123"
    assert rec["build"]["plan_title"] == "Hermes worker plan b9/b10"
    assert rec["build"]["branch"] == "cntrl-hermes-worker"
    assert rec["phase"] == "build"

    # Progress
    assert rec["progress"]["waves"]["done"] == 1
    assert rec["progress"]["waves"]["total"] == 4
    assert rec["progress"]["units"]["done"] == 7
    assert rec["progress"]["units"]["total"] == 22
    assert rec["progress"]["current_wave"]["id"] == "W2"
    assert rec["progress"]["current_wave"]["title"] == "b10 binding"
    assert len(rec["progress"]["current_units"]) == 1
    assert rec["progress"]["current_units"][0]["id"] == "H5"
    assert rec["progress"]["current_units"][0]["seat"] == "agy"
    assert len(rec["progress"]["remaining_waves"]) == 1
    assert rec["progress"]["remaining_waves"][0]["id"] == "W3"

    # Estimate
    assert rec["estimate"]["unit"] == "work_hours"
    assert rec["estimate"]["p50"] == 6.5
    assert rec["estimate"]["p90"] == 10.0
    assert rec["estimate"]["basis"] == "reforecast"

    # Gates
    assert len(rec["gates"]) == 1
    assert rec["gates"][0]["id"] == "g-7209"
    assert rec["gates"][0]["kind"] == "ci"
    assert rec["gates"][0]["state"] == "waiting"
    assert rec["gates"][0]["waiter"] is True

    # Seats and §15 refused_min lower bound
    assert rec["seats"]["agy"]["spawned"] == 12
    assert rec["seats"]["agy"]["running"] == 2
    assert rec["seats"]["agy"]["succeeded"] == 8
    assert rec["seats"]["agy"]["failed"] == 1
    assert rec["seats"]["agy"]["refused"] == 3
    assert rec["seats"]["agy"]["refused_min"] == 3
    assert rec["seats"]["codex"]["spawned"] == 4
    assert rec["seats"]["sonnet"]["spawned"] == 6
    assert rec["seats"]["opus"]["spawned"] == 2
    assert rec["other_names"] == ["grok"]

    # Refusals
    assert len(rec["refusals"]) == 1
    assert rec["refusals"][0]["seat"] == "agy"
    assert rec["refusals"][0]["code"] == "stale_daemon"
    assert rec["refusals"][0]["count"] == 3
    assert rec["refusals"][0]["note"] == "tb-workers daemon predates 3.61.10"

    # Lanes, CI, Blockers, Activity
    assert rec["lanes"]["running"] == 3
    assert rec["lanes"]["cap"] == 6
    assert rec["lanes"]["job_ids"] == ["w_20260929T065856Z_06fd"]
    assert rec["ci"][0]["kind"] == "run"
    assert rec["ci"][0]["ref"] == "36533732367"
    assert rec["ci"][0]["state"] == "pending"
    assert rec["owner_blockers"][0]["id"] == "ob-1"
    assert rec["owner_blockers"][0]["action"] == "approve"
    assert rec["last_activity"]["verb"] == "wave-done"

    # 2. Stale evaluation: evaluate at now = status_at + 31 minutes (>1800s)
    stale_time = res_fresh.status_at + 1860.0
    _clear_status_cache()
    res_stale = read_build_status(_STANDARD_MARKER, marker_file, now=stale_time)
    assert res_stale.present is True
    assert res_stale.valid is True
    assert res_stale.reason == "ok"
    assert res_stale.fresh is False
    assert res_stale.stale is True
    assert res_stale.record is not None  # Stale data is never erased/hidden


def test_fixture_mismatched_conformance(tmp_path):
    """Detailed assertions on status_v1_mismatched.json fixture."""
    _clear_status_cache()
    fixture = _FIXTURES_DIR / "status_v1_mismatched.json"
    marker_file = _setup_status_file(tmp_path, fixture)

    res = read_build_status(_STANDARD_MARKER, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "mismatched"
    assert res.record is None


def test_fixture_v2_only_conformance(tmp_path):
    """Detailed assertions on status_v1_v2_only.json fixture."""
    _clear_status_cache()
    fixture = _FIXTURES_DIR / "status_v1_v2_only.json"
    marker_file = _setup_status_file(tmp_path, fixture)

    res = read_build_status(_STANDARD_MARKER, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "unknown_schema"
    assert res.record is None


def test_fixture_oversize_conformance(tmp_path):
    """Detailed assertions on status_v1_oversize.json fixture (> 64 KiB)."""
    _clear_status_cache()
    fixture = _FIXTURES_DIR / "status_v1_oversize.json"
    file_size = fixture.stat().st_size
    assert file_size > 64 * 1024, f"Fixture size {file_size} must exceed 64 KiB"

    marker_file = _setup_status_file(tmp_path, fixture)

    res = read_build_status(_STANDARD_MARKER, marker_file)
    assert res.present is True
    assert res.valid is False
    assert res.reason == "unreadable"
    assert res.record is None


def test_fixture_hostile_detailed_conformance(tmp_path):
    """Detailed assertions on status_v1_hostile.json fixture (sanitization and safety)."""
    _clear_status_cache()
    fixture = _FIXTURES_DIR / "status_v1_hostile.json"
    marker_file = _setup_status_file(tmp_path, fixture)

    res = read_build_status(_STANDARD_MARKER, marker_file)
    assert res.present is True
    assert res.valid is True
    assert res.reason == "ok"
    assert res.seq == 50
    assert res.record is not None
    rec = res.record

    # 1. Whole field dropped when starting with absolute path prefix
    assert rec["build"]["plan_title"] is None, "Absolute path in plan_title must be dropped"
    assert rec["build"]["branch"] is None, "Absolute path in branch must be dropped"
    assert rec["refusals"][0]["note"] is None, "Absolute path note in refusal must be dropped"

    # 2. Embedded absolute paths stripped from labels
    assert "/private/tmp/secret.txt" not in rec["gates"][0]["label"]
    assert rec["gates"][0]["label"] == "Gate with path inside text"

    assert "/Users/justin/confidential.txt" not in rec["owner_blockers"][0]["label"]
    assert rec["owner_blockers"][0]["label"] == "Blocker with path"

    assert "/var/log/syslog" not in rec["last_activity"]["what"]
    assert rec["last_activity"]["what"] == "Activity Success with"

    # 3. Secrets masked via mask_stored_text
    assert "sk-ant-api03-" not in rec["progress"]["current_units"][0]["title"]
    assert "[REDACTED:anthropic-key:" in rec["progress"]["current_units"][0]["title"]

    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in rec["refusals"][1]["note"]
    assert "[REDACTED:jwt:" in rec["refusals"][1]["note"]

    # 4. ANSI sequences and control characters stripped
    title = rec["progress"]["current_units"][0]["title"]
    assert "\x1b" not in title
    assert "\u0000" not in title
    assert "\u0007" not in title
    assert "ANSI" in title

    what = rec["last_activity"]["what"]
    assert "\x1b" not in what
    assert "\b" not in what
    assert "Success" in what

    # 5. String length caps enforced
    assert len(rec["progress"]["current_wave"]["title"]) <= 80
    assert len(rec["gates"][1]["label"]) <= 120

    # 6. Unknown enum values coerced to "other"
    assert rec["gates"][0]["kind"] == "other"
    assert rec["gates"][0]["state"] == "other"
    assert rec["owner_blockers"][0]["action"] == "other"

    # 7. Invalid/wrong types blanked without rejecting record
    assert rec["build"]["build_id"] is None  # was int 99999
    assert rec["phase"] is None  # was int 12345
    assert rec["progress"]["waves"] is None  # done was str, total was negative
    assert rec["progress"]["units"] is None  # was str "not_a_dict"
    assert rec["estimate"] is None  # p50 > p90
    assert rec["lanes"] is None  # was str "invalid_lanes"
    assert rec["ci"] == []  # was str "invalid_ci"
    assert rec["other_names"] == []  # was str "not_a_list"
    assert rec["seats"]["agy"]["spawned"] is None  # was str "bad_spawned"
    assert rec["seats"]["agy"]["refused"] == 5
    assert rec["seats"]["agy"]["refused_min"] == 5

    # 8. Strict recursive verification: no absolute paths anywhere in the coerced record
    _assert_no_absolute_paths(rec)
