"""Park a Claude SDK session on a usage limit and continue it at the reset (D62).

A 5 h / 7 d subscription limit (a 429 / ``rate_limit_rejected`` whose reset is
beyond the in-turn retry window) used to end the turn. It now PARKS the session:
``{session, resets_at, reason}`` is written to ``state.db`` (so a backend
restart keeps it), and at ``resets_at`` + a per-session stagger the gateway
CONTINUES the same Claude session through the D62 continue path. Nothing here
ever re-sends the user's prompt.

Only a real rate-limit signal with a real reset time parks. A billing / overage
denial, or "out of extra usage" text with no reset (which can be the provider
screening the appended system prompt, not a balance), stays permanent.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

_TABLE = "sdk_usage_parks"
_DEFAULT_STAGGER_MAX_SECONDS = 90.0
_RETRY_BACKOFF_SECONDS = 60.0


def _db_path(home: Optional[Path] = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    return Path(home) / "state.db"


def _connect(home: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(home)), timeout=10.0)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        " session_key TEXT PRIMARY KEY,"
        " sdk_session_id TEXT,"
        " resets_at REAL NOT NULL,"
        " resume_at REAL NOT NULL,"
        " reason TEXT NOT NULL,"
        " created_at REAL NOT NULL)"
    )
    return conn


def configured_policy() -> tuple[bool, float]:
    """(usage_limit_auto_resume, usage_limit_resume_stagger_max_seconds) from config.yaml."""
    from agent.transports.claude_agent_sdk_session import _provider_config

    config = _provider_config()
    raw = config.get("usage_limit_auto_resume", True)
    enabled = raw.strip().lower() in ("1", "true", "yes") if isinstance(raw, str) else bool(raw)
    stagger = config.get("usage_limit_resume_stagger_max_seconds", _DEFAULT_STAGGER_MAX_SECONDS)
    if isinstance(stagger, bool):
        stagger = _DEFAULT_STAGGER_MAX_SECONDS
    try:
        stagger = max(0.0, min(float(stagger), 3600.0))
    except (TypeError, ValueError, OverflowError):
        stagger = _DEFAULT_STAGGER_MAX_SECONDS
    return enabled, stagger


def stagger_seconds(session_key: str, resets_at: float, stagger_max: float) -> float:
    """A per-session offset in [0, stagger_max]: limited sessions do not all resume at once.

    Seeded by (session, reset) so a restart recomputes the same moment."""
    if stagger_max <= 0:
        return 0.0
    return random.Random(f"{session_key}:{int(resets_at)}").uniform(0.0, stagger_max)


def _timestamp(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        # Millisecond epochs appear on some wire shapes.
        return value / 1000.0 if value > 1e12 else value
    if isinstance(value, str) and value.strip():
        try:
            return _timestamp(float(value))
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def real_rate_limit_signal(turn: Any) -> bool:
    """A 429 or a ``rate_limit_rejected`` that is not a pure billing/overage denial."""
    rate_limit = getattr(turn, "rate_limit_rejected", None)
    if isinstance(rate_limit, Mapping):
        kind = str(rate_limit.get("rate_limit_type") or "").lower()
        if kind in {"overage", "billing"}:
            return False
        return True
    status = getattr(turn, "api_error_status", None)
    try:
        return int(status) == 429
    except (TypeError, ValueError, OverflowError):
        return False


def _account_window_reset(now: float) -> Optional[float]:
    """The active Anthropic account's next reported 5 h / 7 d window reset, if any."""
    try:
        from agent import account_usage

        fetch = getattr(account_usage, "_fetch_anthropic_account_usage", None)
        snapshot = fetch() if callable(fetch) else None
    except Exception:
        logger.debug("usage park: account usage lookup failed", exc_info=True)
        return None
    resets = []
    for window in getattr(snapshot, "windows", None) or ():
        reset = getattr(window, "reset_at", None) or getattr(window, "resets_at", None)
        ts = _timestamp(reset)
        if ts is not None and ts > now:
            resets.append(ts)
    return min(resets) if resets else None


def reset_time(turn: Any, now: float, *, account_lookup: Optional[Callable[[float], Optional[float]]] = None) -> Optional[float]:
    """Reset source order: the error's resets_at / Retry-After, then the account's window reset."""
    rate_limit = getattr(turn, "rate_limit_rejected", None)
    if isinstance(rate_limit, Mapping):
        for key in ("resets_at", "resetsAt", "reset_at"):
            ts = _timestamp(rate_limit.get(key))
            if ts is not None and ts > now:
                return ts
        retry_after = rate_limit.get("retry_after")
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after > 0:
            return now + float(retry_after)
    retries = getattr(turn, "api_retries", None)
    if isinstance(retries, Mapping):
        retry_ms = retries.get("retry_delay_ms") or retries.get("retry_after_ms")
        if isinstance(retry_ms, (int, float)) and not isinstance(retry_ms, bool) and retry_ms > 0:
            return now + float(retry_ms) / 1000.0
    lookup = account_lookup if account_lookup is not None else _account_window_reset
    return lookup(now)


def park(session_key: str, sdk_session_id: Optional[str], resets_at: float, reason: str, *,
         stagger_max: float, home: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """Persist (replace) the park for ``session_key``; returns the stored record."""
    now = time.time() if now is None else now
    resume_at = float(resets_at) + stagger_seconds(session_key, resets_at, stagger_max)
    record = {
        "session_key": session_key,
        "sdk_session_id": sdk_session_id,
        "resets_at": float(resets_at),
        "resume_at": resume_at,
        "reason": reason,
        "created_at": now,
    }
    with _connect(home) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {_TABLE} (session_key, sdk_session_id, resets_at, resume_at, reason, created_at)"
            " VALUES (:session_key, :sdk_session_id, :resets_at, :resume_at, :reason, :created_at)",
            record,
        )
    return record


def cancel(session_key: str, *, home: Optional[Path] = None) -> bool:
    """Drop the park (user Stop / cancel, or consumed). True when one existed."""
    try:
        with _connect(home) as conn:
            return conn.execute(f"DELETE FROM {_TABLE} WHERE session_key = ?", (session_key,)).rowcount > 0
    except sqlite3.Error:
        logger.warning("usage park: cancel failed for %s", session_key, exc_info=True)
        return False


def retry_later(session_key: str, *, home: Optional[Path] = None,
                now: Optional[float] = None) -> None:
    """Back off a refused due park without discarding its durable retry state."""
    retry_at = (time.time() if now is None else now) + _RETRY_BACKOFF_SECONDS
    with _connect(home) as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET resume_at = MAX(resume_at, ?) WHERE session_key = ?",
            (retry_at, session_key),
        )


def get(session_key: str, *, home: Optional[Path] = None) -> Optional[dict]:
    with _connect(home) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {_TABLE} WHERE session_key = ?", (session_key,)).fetchone()
    return dict(row) if row else None


def due(*, now: Optional[float] = None, home: Optional[Path] = None) -> list[dict]:
    now = time.time() if now is None else now
    with _connect(home) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE resume_at <= ? ORDER BY resume_at", (now,)
        ).fetchall()
    return [dict(row) for row in rows]


def dispatch_due(dispatch: Callable[[dict], bool], *, now: Optional[float] = None,
                 home: Optional[Path] = None) -> list[str]:
    """Hand every due park to ``dispatch``; a park is consumed only when dispatch accepted it.

    A session that is not live in this process stays parked until it is (the
    desktop re-opens sessions after a backend restart)."""
    consumed = []
    dispatch_now = time.time() if now is None else now
    for record in due(now=now, home=home):
        try:
            accepted = bool(dispatch(record))
        except Exception:
            logger.warning("usage park: dispatch failed for %s", record["session_key"], exc_info=True)
            accepted = False
        if accepted:
            cancel(record["session_key"], home=home)
            consumed.append(record["session_key"])
        else:
            retry_later(record["session_key"], home=home, now=dispatch_now)
    return consumed


def paused_status_text(resume_at: float) -> str:
    """User-facing pause copy (local time)."""
    return f"Paused: usage limit, resumes at {datetime.fromtimestamp(resume_at).strftime('%H:%M')}"
