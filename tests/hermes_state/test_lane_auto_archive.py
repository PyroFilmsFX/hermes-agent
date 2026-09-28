"""Tests for conductor lane auto-archive sweep and predicate parity with desktop."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB, is_conductor_lane


def test_conductor_lane_predicate_shared_fixture():
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "conductor_lane_cases.json"
    assert fixture_path.exists(), f"Fixture missing: {fixture_path}"

    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    for case in cases:
        path = case["path"]
        branch = case.get("branch")
        is_main = case.get("isMain", False)
        expected = case["expected"]
        reason = case["reason"]

        result = is_conductor_lane(path, branch=branch, is_main=is_main)
        assert result == expected, f"Failed on {reason} ({path}, branch={branch}, isMain={is_main}): got {result}, expected {expected}"

        # Dict target shape parity
        dict_target = {"path": path, "branch": branch, "is_main": is_main}
        assert is_conductor_lane(dict_target) == expected, f"Dict target failed on {reason}"

        if not is_main and not branch:
            assert is_conductor_lane(path) == expected, f"String path failed on {reason}"


def test_lane_auto_archive_sweep(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    now = time.time()

    def _insert_session(
        sid: str,
        *,
        cwd: str,
        ended_at: float | None = None,
        end_reason: str | None = None,
        pinned: int = 0,
        branch: str | None = None,
    ):
        db.create_session(sid, "cli", cwd=cwd)
        db._write_sql(
            "UPDATE sessions SET ended_at = ?, end_reason = ?, pinned = ?, git_branch = ? WHERE id = ?",
            (ended_at, end_reason, pinned, branch, sid),
        )

    # 1. Old ended conductor lane session -> should be ARCHIVED
    _insert_session(
        "s_lane_old",
        cwd="/tmp/lane-feature-1",
        ended_at=now - 7 * 3600,
        end_reason="normal",
        pinned=0,
    )

    # 2. Nested worktree conductor lane session -> should be ARCHIVED
    _insert_session(
        "s_lane_nested_old",
        cwd="/private/tmp/lane-feature-1/.claude/worktrees/lane-w123",
        ended_at=now - 10 * 3600,
        end_reason="completed",
        pinned=0,
    )

    # 3. Old ended recoverable end reasons -> should be KEPT
    for idx, reason in enumerate([
        "agent_close",
        "ws_orphan_reap",
        "superseded_by_resume",
        "startup_orphan_reap",
    ]):
        _insert_session(
            f"s_recoverable_{idx}",
            cwd="/tmp/lane-recoverable",
            ended_at=now - 8 * 3600,
            end_reason=reason,
            pinned=0,
        )

    # 4. Old ended lane session, but PINNED -> should be KEPT
    _insert_session(
        "s_pinned",
        cwd="/tmp/lane-pinned",
        ended_at=now - 8 * 3600,
        end_reason="normal",
        pinned=1,
    )

    # 5. Old ended session, but NOT a conductor lane -> should be KEPT
    _insert_session(
        "s_non_lane",
        cwd="/Users/justin/work/my-project",
        ended_at=now - 8 * 3600,
        end_reason="normal",
        pinned=0,
    )

    # 6. Fresh ended conductor lane session (< 6 hours old) -> should be KEPT
    _insert_session(
        "s_fresh",
        cwd="/tmp/lane-fresh",
        ended_at=now - 2 * 3600,
        end_reason="normal",
        pinned=0,
    )

    # 7. Unended conductor lane session -> should be KEPT
    _insert_session(
        "s_unended",
        cwd="/tmp/lane-active",
        ended_at=None,
        end_reason=None,
        pinned=0,
    )

    # Run lane auto-archive
    res = db.maybe_auto_archive(
        auto_archive=False,
        auto_archive_lanes=True,
        lane_archive_hours=6.0,
        lane_min_interval_hours=0,
    )

    assert res["lane_archived"] == 2
    assert res["archived"] == 2
    assert res["skipped"] is False

    assert bool(db.get_session("s_lane_old")["archived"]) is True
    assert bool(db.get_session("s_lane_nested_old")["archived"]) is True

    # All others must stay unarchived
    for sid in [
        "s_recoverable_0",
        "s_recoverable_1",
        "s_recoverable_2",
        "s_recoverable_3",
        "s_pinned",
        "s_non_lane",
        "s_fresh",
        "s_unended",
    ]:
        assert bool(db.get_session(sid)["archived"]) is False, f"Session {sid} was unexpectedly archived"

    db.close()


def test_lane_auto_archive_hourly_min_interval(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)

    now = 1000000.0
    monkeypatch.setattr(time, "time", lambda: now)

    # First run executes
    res1 = db.maybe_auto_archive(
        auto_archive=False,
        auto_archive_lanes=True,
        lane_archive_hours=6.0,
        lane_min_interval_hours=1,
    )
    assert res1["skipped"] is False
    assert float(db.get_meta("last_auto_archive_lanes")) == now

    # Second run within 1 hour is skipped
    now += 1800.0  # 30 min later
    res2 = db.maybe_auto_archive(
        auto_archive=False,
        auto_archive_lanes=True,
        lane_archive_hours=6.0,
        lane_min_interval_hours=1,
    )
    assert res2["skipped"] is True

    # Third run after > 1 hour executes
    now += 1801.0  # > 1 hour total
    res3 = db.maybe_auto_archive(
        auto_archive=False,
        auto_archive_lanes=True,
        lane_archive_hours=6.0,
        lane_min_interval_hours=1,
    )
    assert res3["skipped"] is False

    db.close()
