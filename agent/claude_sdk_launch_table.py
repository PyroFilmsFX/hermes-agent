"""In-memory launch table + runtime lineage for Claude SDK sessions (#49 / b10 H2)."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Origin of the planned Claude session id carried by a launch entry.
SID_ORIGIN_FRESH = "fresh"  # Hermes minted the uuid4 for this spawn (--session-id)
SID_ORIGIN_RESUMED = "resumed"  # --resume of an id read from agent-writable session state
SID_ORIGIN_UNKNOWN = "unknown"  # caller did not say; treated as unverified
_SID_ORIGINS = frozenset((SID_ORIGIN_FRESH, SID_ORIGIN_RESUMED))

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
    sid_origin: str = SID_ORIGIN_UNKNOWN,
    resume_authenticated: bool = False,
) -> dict:
    """Record a planned CLI launch in the in-memory launch table.

    ``sid_origin`` says where the planned Claude id came from. A ``resumed`` id was read from
    agent-writable state, so the entry is ``resumed_unverified`` unless the caller proved (see
    :func:`authenticated_resume`) that main already signed this id for this Hermes session.
    Main must not attest an entry whose ``resumed_unverified`` is not exactly ``False``.
    An unspecified origin fails closed (unverified).
    """
    global _seq
    cid = str(claude_session_id or "")
    capped = [str(x) for x in (lineage or []) if x][-16:]
    rec_at = time.time() if recorded_at is None else float(recorded_at)
    origin = sid_origin if sid_origin in _SID_ORIGINS else SID_ORIGIN_UNKNOWN
    verified = origin == SID_ORIGIN_FRESH or (
        origin == SID_ORIGIN_RESUMED and resume_authenticated is True
    )
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
            "sid_origin": origin,
            "resumed_unverified": not verified,
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


def authenticated_resume(
    claude_sid: str,
    hermes_sid: str,
    lineage: Optional[Iterable[str]] = None,
    *,
    profile: Optional[str] = None,
    anchor=None,
    uid: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> bool:
    """True when main already signed a launch attestation mapping ``claude_sid`` to
    ``hermes_sid`` or an ancestor in the backend's IN-MEMORY runtime lineage.

    The resumed id comes from state.db, which any agent can write; without this proof an agent
    could plant another session's Claude sid and have main attest it to this session's project.
    Verification is ``hermes_owner_grant.attest.verify_launch_provenance``: active kid now,
    valid signature, expiry ignored. Any error means False (fail closed).
    """
    try:
        from hermes_owner_grant import attest as _attest

        if not _attest.is_safe_session_component(claude_sid):
            return False
        accepted = [str(x) for x in [hermes_sid, *(lineage or [])] if x]
        if not accepted:
            return False
        if anchor is None:
            from hermes_owner_grant.anchor import load_trusted_anchor

            anchor = load_trusted_anchor()
        result = _attest.verify_launch_provenance(
            claude_session=str(claude_sid),
            hermes_sessions=accepted,
            uid=os.getuid() if uid is None else int(uid),
            now=int(time.time() * 1000) if now_ms is None else int(now_ms),
            profile=profile,
            anchor=anchor,
        )
        return bool(result.ok)
    except Exception:
        logger.debug("resume provenance check failed", exc_info=True)
        return False


def attestation_files_present(grants_dir: str, claude_sid: str) -> bool:
    """True when ``<grants_dir>/session-attest/<claude_sid>/`` holds a ``*.json`` entry.

    Presence only (the barrier is a sync aid; conductor verifies). ``session-attest`` and the
    sid dir are opened with ``O_NOFOLLOW``, so a symlink at either level counts as absent.
    May block on a hostile mount: call it from a daemon thread.
    """
    from hermes_owner_grant.attest import _open_dir_nofollow, is_safe_session_component

    if not is_safe_session_component(claude_sid):
        return False
    try:
        parent_fd = _open_dir_nofollow(os.path.join(str(grants_dir), "session-attest"))
    except OSError:
        return False
    try:
        try:
            sid_fd = _open_dir_nofollow(claude_sid, dir_fd=parent_fd)
        except OSError:
            return False
    finally:
        os.close(parent_fd)
    try:
        return any(
            n.endswith(".json") and not n.startswith(".") for n in os.listdir(sid_fd)
        )
    finally:
        os.close(sid_fd)


def run_bounded(fn, timeout: float, *, name: str = "launch-probe"):
    """Run ``fn()`` on a daemon thread; return ``(finished, value)`` within ``timeout`` seconds.

    A probe of an agent-writable path (FUSE, NFS, a symlink to a fifo) can block in the kernel;
    the daemon thread is abandoned rather than holding the caller past its deadline.
    """
    done = threading.Event()
    box: list = []

    def _run() -> None:
        try:
            box.append(fn())
        except Exception:
            logger.debug("%s raised", name, exc_info=True)
        finally:
            done.set()

    threading.Thread(target=_run, name=name, daemon=True).start()
    if not done.wait(max(0.0, float(timeout))) or not box:
        return False, None
    return True, box[0]


def _reset_table_for_tests() -> None:
    """Test seam: clear all entries and reset the monotonic seq counter."""
    global _seq, _last_consumer_seen
    with _condition:
        _launches.clear()
        _seq = 0
        _last_consumer_seen = 0.0
        _condition.notify_all()
