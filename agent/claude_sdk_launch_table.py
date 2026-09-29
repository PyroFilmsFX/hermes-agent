"""In-memory launch table + runtime lineage for Claude SDK sessions (#49 / b10 H2)."""

from __future__ import annotations

import threading
import time
from typing import Optional

_condition = threading.Condition()
_launches: dict[str, dict] = {}
_seq: int = 0
_last_consumer_seen: float = 0.0


def record_consumer_seen(ts: Optional[float] = None) -> float:
    """Record when a launch consumer polled the launch table."""
    global _last_consumer_seen
    val = time.monotonic() if ts is None else float(ts)
    with _condition:
        _last_consumer_seen = val
    return val


def get_last_consumer_seen() -> float:
    """Return monotonic timestamp of the last consumer poll, or 0.0."""
    with _condition:
        return _last_consumer_seen


def set_last_consumer_seen(ts: Optional[float] = None) -> float:
    """Setter alias for record_consumer_seen."""
    return record_consumer_seen(ts)


def last_consumer_seen() -> float:
    """Getter alias for get_last_consumer_seen."""
    return get_last_consumer_seen()


def has_recent_consumer(within_seconds: float = 60.0) -> bool:
    """Return True if a consumer polled within the given window."""
    with _condition:
        if _last_consumer_seen <= 0.0:
            return False
        return (time.monotonic() - _last_consumer_seen) <= float(within_seconds)


def record_launch(
    *,
    hermes_session_id: str,
    claude_session_id: str,
    profile: str = "default",
    lineage: Optional[list[str]] = None,
    recorded_at: Optional[float] = None,
) -> dict:
    """Record a planned CLI launch in the in-memory launch table."""
    global _seq
    cid = str(claude_session_id or "")
    capped = [str(x) for x in (lineage or []) if x][-16:]
    rec_at = time.time() if recorded_at is None else float(recorded_at)
    with _condition:
        _seq += 1
        entry = {
            "launch_seq": _seq,
            "hermes_session_id": str(hermes_session_id or ""),
            "claude_session_id": cid,
            "profile": str(profile or "default"),
            "lineage": capped,
            "hermes_lineage": capped,
            "recorded_at": rec_at,
        }
        if cid:
            _launches[cid] = entry
        _condition.notify_all()
        return dict(entry)


def update_lineage(
    claude_session_id: str,
    *,
    hermes_session_id: str,
    lineage: Optional[list[str]] = None,
    recorded_at: Optional[float] = None,
) -> Optional[dict]:
    """Update an active launch's hermes_session_id and lineage across /compress."""
    global _seq
    cid = str(claude_session_id or "")
    capped = [str(x) for x in (lineage or []) if x][-16:]
    with _condition:
        entry = _launches.get(cid)
        if entry is None:
            return None
        _seq += 1
        entry["launch_seq"] = _seq
        entry["hermes_session_id"] = str(hermes_session_id or "")
        entry["lineage"] = capped
        entry["hermes_lineage"] = capped
        if recorded_at is not None:
            entry["recorded_at"] = float(recorded_at)
        _condition.notify_all()
        return dict(entry)


def retire(claude_session_id: str) -> Optional[dict]:
    """Retire a launch entry when the CLI session closes."""
    global _seq
    cid = str(claude_session_id or "")
    with _condition:
        entry = _launches.pop(cid, None)
        if entry is not None:
            _seq += 1
            _condition.notify_all()
        return entry


def snapshot(since_seq: int = 0) -> tuple[int, list[dict]]:
    """Return current monotonic seq and active entries with launch_seq > since_seq."""
    since = int(since_seq or 0)
    with _condition:
        current_seq = _seq
        entries = [
            dict(e)
            for e in sorted(_launches.values(), key=lambda x: x["launch_seq"])
            if e["launch_seq"] > since
        ]
        return current_seq, entries


def wait_for_change(since_seq: int = 0, timeout: Optional[float] = None) -> int:
    """Wait for table seq to advance past since_seq, returning current seq."""
    since = int(since_seq or 0)
    to = float(timeout) if timeout is not None else None
    with _condition:
        if _seq > since:
            return _seq
        if to is not None and to <= 0:
            return _seq
        _condition.wait_for(lambda: _seq > since, timeout=to)
        return _seq


def _reset_table_for_tests() -> None:
    """Test seam: clear all entries and reset the monotonic seq counter."""
    global _seq, _last_consumer_seen
    with _condition:
        _launches.clear()
        _seq = 0
        _last_consumer_seen = 0.0
        _condition.notify_all()
