"""peer_mailbox storage: durable queue, dedupe, single-claimant settlement (cntrl carry, inventory #11)."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    yield handle
    handle.close()


def test_reads_tolerate_missing_table(db):
    assert db.peer_mailbox_pending() == []
    assert db.peer_mailbox_pending_targets() == []
    assert db.peer_mailbox_claimed() == []
    assert db.peer_mailbox_get(1) is None


def test_enqueue_is_durable_and_dedupes_on_key(db, tmp_path):
    row, created = db.peer_mailbox_enqueue(target_session_id="t1", body="hello", from_session_id="s1",
                                           from_label="manager", dedupe_key="s1:r1")
    assert created and row["status"] == "queued" and row["attempts"] == 0
    again, created_again = db.peer_mailbox_enqueue(target_session_id="t1", body="hello", dedupe_key="s1:r1")
    assert not created_again and again["id"] == row["id"]
    db.close()
    reopened = SessionDB(tmp_path / "state.db")
    try:
        assert [r["id"] for r in reopened.peer_mailbox_pending("t1")] == [row["id"]]
    finally:
        reopened.close()


def test_claim_is_exclusive_and_only_the_claimant_settles(db):
    row, _ = db.peer_mailbox_enqueue(target_session_id="t1", body="x")
    assert db.peer_mailbox_claim(row["id"], "A")
    assert not db.peer_mailbox_claim(row["id"], "B")
    assert not db.peer_mailbox_mark_delivered(row["id"], "B", "live")
    assert db.peer_mailbox_mark_delivered(row["id"], "A", "live")
    assert db.peer_mailbox_get(row["id"])["status"] == "delivered"
    assert not db.peer_mailbox_claim(row["id"], "A"), "a delivered row is never claimed again"
    assert db.peer_mailbox_pending("t1") == []


def test_release_requeues_until_max_attempts_then_fails(db):
    row, _ = db.peer_mailbox_enqueue(target_session_id="t1", body="x")
    for expected in ("queued", "queued", "failed"):
        assert db.peer_mailbox_claim(row["id"], "A")
        assert db.peer_mailbox_release(row["id"], "A", "busy", max_attempts=3) == expected
    final = db.peer_mailbox_get(row["id"])
    assert final["status"] == "failed" and final["attempts"] == 3 and final["last_error"] == "busy"


def test_pending_targets_in_arrival_order(db):
    db.peer_mailbox_enqueue(target_session_id="b", body="1")
    db.peer_mailbox_enqueue(target_session_id="a", body="2")
    db.peer_mailbox_enqueue(target_session_id="b", body="3")
    assert db.peer_mailbox_pending_targets() == ["b", "a"]
    assert [r["body"] for r in db.peer_mailbox_pending("b")] == ["1", "3"]


def test_resident_candidates_are_pinned_interactive_rows(db):
    db.create_session("pinned-desktop", "desktop")
    db.create_session("pinned-telegram", "telegram")
    db.create_session("plain-desktop", "desktop")
    db.set_session_pinned("pinned-desktop", True)
    db.set_session_pinned("pinned-telegram", True)
    assert db.list_resident_session_candidates(limit=5) == ["pinned-desktop"]


def test_sender_auth_column_is_added_to_a_pre_existing_table(tmp_path):
    """b3-30 follow-up: the queue-only marker is an additive migration on an already-created table."""
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = SessionDB(path)
    legacy.close()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE peer_mailbox (id INTEGER PRIMARY KEY AUTOINCREMENT, target_session_id TEXT NOT NULL, "
                 "target_hint TEXT, from_session_id TEXT, from_label TEXT, body TEXT NOT NULL, dedupe_key TEXT UNIQUE, "
                 "status TEXT NOT NULL DEFAULT 'queued', created_at REAL NOT NULL, claimed_at REAL, claim_owner TEXT, "
                 "delivered_at REAL, delivered_via TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    conn.execute("INSERT INTO peer_mailbox (target_session_id, body, created_at) VALUES ('t1', 'old row', 1.0)")
    conn.commit()
    conn.close()
    db = SessionDB(path)
    try:
        row, created = db.peer_mailbox_enqueue(target_session_id="t1", body="new", from_session_id="s1",
                                               sender_auth='{"owner_session_id": "rt"}')
        assert created and row["sender_auth"] == '{"owner_session_id": "rt"}'
        [legacy_row] = [r for r in db.peer_mailbox_pending("t1") if r["body"] == "old row"]
        assert legacy_row["sender_auth"] is None, "pre-existing rows stay normal-path rows"
    finally:
        db.close()


def test_enqueue_per_sender_cap_raises_without_writing(db):
    from hermes_state_peer_mailbox import PeerMailboxQueueFull

    db.peer_mailbox_enqueue(target_session_id="t1", body="a", from_session_id="s1", max_queued_per_sender=1)
    with pytest.raises(PeerMailboxQueueFull):
        db.peer_mailbox_enqueue(target_session_id="t2", body="b", from_session_id="s1", max_queued_per_sender=1)
    assert len(db.peer_mailbox_pending()) == 1


def test_expire_queued_fails_only_old_queued_rows(db):
    import time as _time

    old, _ = db.peer_mailbox_enqueue(target_session_id="t1", body="old")
    new, _ = db.peer_mailbox_enqueue(target_session_id="t1", body="new")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE peer_mailbox SET created_at = ? WHERE id = ?", (_time.time() - 100, old["id"])))
    expired = db.peer_mailbox_expire_queued(older_than=_time.time() - 50)
    assert [r["id"] for r in expired] == [old["id"]]
    assert db.peer_mailbox_get(old["id"])["status"] == "failed"
    assert db.peer_mailbox_get(old["id"])["last_error"] == "expired"
    assert db.peer_mailbox_get(new["id"])["status"] == "queued"
