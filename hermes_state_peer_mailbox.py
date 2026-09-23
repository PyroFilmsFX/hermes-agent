"""Durable cross-session mailbox (``peer_mailbox``) — fork-local (cntrl carry).

A message addressed to a stored Hermes session is written here FIRST, then delivered as a turn by the
gateway (``tui_gateway/session_mailbox.py``): straight into the live session, by resuming it, or later
when the session next starts / the gateway boots. Nothing here talks to the gateway.

Delivery is at-most-once. A row moves ``queued -> claimed -> delivered``; only the claimant may settle
its claim, and a claim whose owner process died is settled as ``failed`` (the turn may or may not have
been accepted — redelivering could duplicate it). ``dedupe_key`` makes sender retries idempotent.

Like the Telegram topic tables, the table is created on first write, not by startup reconciliation, so
the upstream schema version is untouched; reads tolerate its absence.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

PEER_MAILBOX_DDL = """
CREATE TABLE IF NOT EXISTS peer_mailbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_session_id TEXT NOT NULL,
    target_hint TEXT,
    from_session_id TEXT,
    from_label TEXT,
    body TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    claimed_at REAL,
    claim_owner TEXT,
    delivered_at REAL,
    delivered_via TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_peer_mailbox_status_target ON peer_mailbox(status, target_session_id, id);
"""

PEER_MAILBOX_STATUSES = ("queued", "claimed", "delivered", "failed")


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


class SessionPeerMailboxMixin:
    """``peer_mailbox`` persistence for :class:`hermes_state.SessionDB`."""

    _peer_mailbox_ready = False

    def ensure_peer_mailbox(self) -> None:
        if self._peer_mailbox_ready:
            return

        def _do(conn):
            # One statement per execute: executescript() would COMMIT the caller's BEGIN IMMEDIATE.
            for statement in PEER_MAILBOX_DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)
        self._execute_write(_do)
        self._peer_mailbox_ready = True

    def _mailbox_read_all(self, sql: str, params=()) -> List[Dict[str, Any]]:
        try:
            return [dict(row) for row in self._read_all(sql, params)]
        except sqlite3.OperationalError:
            return []  # table not created yet: nothing was ever queued

    def _mailbox_read_one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        try:
            return _row(self._read_one(sql, params))
        except sqlite3.OperationalError:
            return None

    def peer_mailbox_enqueue(
        self, *, target_session_id: str, body: str, from_session_id: str = "", from_label: str = "",
        dedupe_key: Optional[str] = None, target_hint: str = "",
    ) -> Tuple[Dict[str, Any], bool]:
        """Queue one message → ``(row, created)``. A repeated ``dedupe_key`` returns the original row
        with ``created=False`` and never queues a second copy."""
        self.ensure_peer_mailbox()
        now = time.time()

        def _do(conn):
            if dedupe_key:
                existing = conn.execute("SELECT * FROM peer_mailbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                if existing is not None:
                    return dict(existing), False
            cursor = conn.execute(
                "INSERT INTO peer_mailbox (target_session_id, target_hint, from_session_id, from_label, body, "
                "dedupe_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (target_session_id, target_hint or None, from_session_id or None, from_label or None, body,
                 dedupe_key or None, now))
            row = conn.execute("SELECT * FROM peer_mailbox WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row), True
        return self._execute_write(_do)

    def peer_mailbox_get(self, message_id: int) -> Optional[Dict[str, Any]]:
        return self._mailbox_read_one("SELECT * FROM peer_mailbox WHERE id = ?", (int(message_id),))

    def peer_mailbox_pending(self, target_session_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if target_session_id:
            return self._mailbox_read_all(
                "SELECT * FROM peer_mailbox WHERE status = 'queued' AND target_session_id = ? ORDER BY id LIMIT ?",
                (target_session_id, int(limit)))
        return self._mailbox_read_all(
            "SELECT * FROM peer_mailbox WHERE status = 'queued' ORDER BY id LIMIT ?", (int(limit),))

    def peer_mailbox_pending_targets(self, limit: int = 50) -> List[str]:
        rows = self._mailbox_read_all(
            "SELECT target_session_id, MIN(id) AS first_id FROM peer_mailbox WHERE status = 'queued' "
            "GROUP BY target_session_id ORDER BY first_id LIMIT ?", (int(limit),))
        return [str(row["target_session_id"]) for row in rows]

    def peer_mailbox_claimed(self) -> List[Dict[str, Any]]:
        return self._mailbox_read_all("SELECT * FROM peer_mailbox WHERE status = 'claimed' ORDER BY id")

    def peer_mailbox_claim(self, message_id: int, owner: str) -> bool:
        """``queued -> claimed`` for exactly one claimant."""
        self.ensure_peer_mailbox()
        return self._write_rowcount(
            "UPDATE peer_mailbox SET status = 'claimed', claimed_at = ?, claim_owner = ? "
            "WHERE id = ? AND status = 'queued'", (time.time(), owner, int(message_id))) == 1

    def peer_mailbox_mark_delivered(self, message_id: int, owner: str, via: str) -> bool:
        self.ensure_peer_mailbox()
        return self._write_rowcount(
            "UPDATE peer_mailbox SET status = 'delivered', delivered_at = ?, delivered_via = ?, last_error = NULL "
            "WHERE id = ? AND status = 'claimed' AND claim_owner = ?",
            (time.time(), via, int(message_id), owner)) == 1

    def peer_mailbox_release(self, message_id: int, owner: str, error: str, *, max_attempts: int) -> str:
        """Settle a claim whose delivery was REFUSED (never accepted): back to ``queued`` for a retry, or
        ``failed`` once ``max_attempts`` refusals accumulated. Returns the resulting status ('' if the
        claim was not ours)."""
        self.ensure_peer_mailbox()

        def _do(conn):
            row = conn.execute("SELECT attempts FROM peer_mailbox WHERE id = ? AND status = 'claimed' AND claim_owner = ?",
                               (int(message_id), owner)).fetchone()
            if row is None:
                return ""
            attempts = int(row[0] or 0) + 1
            status = "failed" if max_attempts > 0 and attempts >= max_attempts else "queued"
            conn.execute(
                "UPDATE peer_mailbox SET status = ?, attempts = ?, last_error = ?, claimed_at = NULL, claim_owner = NULL "
                "WHERE id = ?", (status, attempts, str(error)[:2000], int(message_id)))
            return status
        return self._execute_write(_do)

    def peer_mailbox_fail(self, message_id: int, error: str, *, owner: Optional[str] = None) -> bool:
        """Terminal failure. With ``owner`` only that claimant's claim is settled; without, any non-delivered
        row (used for unknown-outcome claims of dead processes and permanently unresolvable targets)."""
        self.ensure_peer_mailbox()
        if owner is not None:
            return self._write_rowcount(
                "UPDATE peer_mailbox SET status = 'failed', last_error = ? WHERE id = ? AND status = 'claimed' "
                "AND claim_owner = ?", (str(error)[:2000], int(message_id), owner)) == 1
        return self._write_rowcount(
            "UPDATE peer_mailbox SET status = 'failed', last_error = ? WHERE id = ? AND status IN ('queued', 'claimed')",
            (str(error)[:2000], int(message_id))) == 1

    def list_resident_session_candidates(self, limit: int = 3) -> List[str]:
        """Pinned, unarchived, top-level interactive sessions, most recently active first (B-lite residency)."""
        rows = self._mailbox_read_all(
            "SELECT id FROM sessions WHERE COALESCE(pinned, 0) = 1 AND COALESCE(archived, 0) = 0 "
            "AND source IN ('tui', 'desktop') "
            "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?", (int(limit),))
        return [str(row["id"]) for row in rows]


__all__ = ["PEER_MAILBOX_DDL", "PEER_MAILBOX_STATUSES", "SessionPeerMailboxMixin"]
