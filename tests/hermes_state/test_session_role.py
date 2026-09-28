"""Explicit per-session role tag (``sessions.session_role``, decision D28).

The desktop derives a role badge from the title (``-manager`` / ``-worker`` ...);
an explicit role set by the owner overrides it, and clearing it (``None``) falls
back to the name. The column is display metadata only: it never reaches the
system prompt or the transcript."""

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _row(db, sid):
    rows = db.list_sessions_rich(include_archived=True)
    return next(s for s in rows if s["id"] == sid)


def test_role_defaults_to_null(db):
    db.create_session(session_id="s1", source="cli")

    assert db.get_session_role("s1") is None
    assert _row(db, "s1")["session_role"] is None


@pytest.mark.parametrize("role", ["manager", "orchestrator", "worker", "stream"])
def test_set_read_back_and_clear(db, role):
    db.create_session(session_id="s1", source="cli")

    assert db.set_session_role("s1", role) is True
    assert db.get_session_role("s1") == role
    assert _row(db, "s1")["session_role"] == role
    assert db.get_session("s1")["session_role"] == role

    # None clears the explicit tag -> "Auto (from name)".
    assert db.set_session_role("s1", None) is True
    assert db.get_session_role("s1") is None
    assert _row(db, "s1")["session_role"] is None


def test_role_is_normalized_and_blank_clears(db):
    db.create_session(session_id="s1", source="cli")

    db.set_session_role("s1", "  Worker ")
    assert db.get_session_role("s1") == "worker"

    db.set_session_role("s1", "")
    assert db.get_session_role("s1") is None


def test_unknown_role_is_rejected(db):
    db.create_session(session_id="s1", source="cli")

    with pytest.raises(ValueError):
        db.set_session_role("s1", "admin")
    assert db.get_session_role("s1") is None


def test_missing_session_reports_false(db):
    assert db.set_session_role("nope", "worker") is False
    assert db.get_session_role("nope") is None


def test_role_persists_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(path)
    try:
        first.create_session(session_id="s1", source="cli")
        first.set_session_role("s1", "orchestrator")
    finally:
        first.close()

    second = SessionDB(path)
    try:
        assert second.get_session_role("s1") == "orchestrator"
    finally:
        second.close()


def test_reconciler_adds_the_column_to_a_legacy_store(tmp_path):
    """A pre-D28 state.db (no session_role column) gains it on open; existing rows read NULL."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    try:
        first.create_session(session_id="old", source="cli")
    finally:
        first.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE sessions DROP COLUMN session_role")
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert "session_role" not in cols
    finally:
        conn.close()

    reopened = SessionDB(path)
    try:
        assert reopened.get_session_role("old") is None
        assert reopened.set_session_role("old", "stream") is True
        assert reopened.get_session_role("old") == "stream"
    finally:
        reopened.close()


def test_role_is_lineage_stamped_across_compression(db):
    """Desktop projects a compression root forward to its tip; the role must agree on both."""
    db.create_session(session_id="root", source="cli")
    db.end_session("root", "compression")
    db.create_session(session_id="tip", source="cli", parent_session_id="root")

    db.set_session_role("tip", "manager")

    assert db.get_session_role("root") == "manager"
    assert db.get_session_role("tip") == "manager"
