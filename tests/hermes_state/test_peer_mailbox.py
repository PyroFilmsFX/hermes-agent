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
