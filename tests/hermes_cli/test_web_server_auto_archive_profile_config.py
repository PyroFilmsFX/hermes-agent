"""Invariant: a profile's OWN ``sessions:`` config governs its store's auto-archive.

``_maybe_auto_archive_for_profile`` sweeps an arbitrary profile's store, but the config it
read came from a zero-arg ``load_config()`` — i.e. the PROCESS HERMES_HOME. On a host serving
several profiles, the launch profile's ``auto_archive`` / ``auto_archive_days`` silently
decided every other profile's retention.

Real stores, real ``config.yaml`` files and the real ``_check_gateway_running`` predicate;
only the profile -> home mapping is redirected at temp dirs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import web_server_sessions as wss


def _write_config(home: Path, *, auto_archive_days: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "sessions:\n"
        "  auto_archive: true\n"
        f"  auto_archive_days: {auto_archive_days}\n"
        "  min_interval_hours: 0\n",
        encoding="utf-8",
    )


@pytest.fixture
def two_profile_homes(tmp_path, monkeypatch):
    """Launch profile 'a' never archives; served profile 'b' archives immediately."""
    from hermes_state import SessionDB

    home_a, home_b = tmp_path / "a", tmp_path / "b"
    _write_config(home_a, auto_archive_days=3650)
    _write_config(home_b, auto_archive_days=0)

    db = SessionDB(db_path=home_b / "state.db")
    db.create_session("s1", "cli")
    db.append_message("s1", "user", "hello")
    db.close()

    # Process home is the LAUNCH profile's.
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    monkeypatch.setattr(
        wss, "_session_db_path_for_profile",
        lambda profile: (home_b if profile == "b" else home_a) / "state.db")
    monkeypatch.setattr(wss, "_last_auto_archive_check", {})
    return home_b


def _archived(db_path: Path) -> bool:
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    try:
        return bool((db.get_session("s1") or {}).get("archived"))
    finally:
        db.close()


def test_auto_archive_uses_the_swept_profiles_own_retention_config(two_profile_homes):
    home_b = two_profile_homes

    wss._maybe_auto_archive_for_profile("b")

    assert _archived(home_b / "state.db"), (
        "profile b's store was swept with another profile's sessions.auto_archive_days")


def test_lane_only_sweep_on_a_read_poll_never_opens_the_store_writable_when_nothing_is_due(tmp_path, monkeypatch):
    """GET session-list polls trigger the opportunistic sweep: with only the lane sweep on and no lane due,
    the store is probed read-only and never opened writable (a writable open writes/checkpoints the WAL)."""
    import time as _time

    from hermes_state import SessionDB

    home = tmp_path / "p"
    home.mkdir()
    (home / "config.yaml").write_text("sessions:\n  auto_archive: false\n  auto_archive_lanes: true\n", encoding="utf-8")
    db = SessionDB(db_path=home / "state.db")
    db.create_session("not-a-lane", "cli", cwd="/Users/me/code/app")
    db.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(wss, "_session_db_path_for_profile", lambda profile: home / "state.db")
    monkeypatch.setattr(wss, "_last_auto_archive_check", {})
    opened = []
    real_open = wss._open_session_db_for_profile
    monkeypatch.setattr(wss, "_open_session_db_for_profile",
                        lambda profile, *, read_only: opened.append(read_only) or real_open(profile, read_only=read_only))

    wss._maybe_auto_archive_for_profile(None)
    assert opened == [True]

    # A lane that is due: the sweep opens writable and archives it.
    db = SessionDB(db_path=home / "state.db")
    db.create_session("lane", "cli", cwd="/tmp/lane-x")
    db._write_sql("UPDATE sessions SET ended_at = ?, end_reason = 'normal' WHERE id = 'lane'", (_time.time() - 8 * 3600,))
    db.close()
    monkeypatch.setattr(wss, "_last_auto_archive_check", {})
    opened.clear()
    wss._maybe_auto_archive_for_profile(None)
    assert opened == [True, False]
    db = SessionDB(db_path=home / "state.db")
    try:
        assert (db.get_session("lane") or {}).get("archived")
    finally:
        db.close()
