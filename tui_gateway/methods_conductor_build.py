"""Read-only conductor tb-build status scoped to the calling session workspace."""

from __future__ import annotations

from collections import OrderedDict

from .method_ctx import HandlerRegistry, bind_module
from .methods_relay_jobs import _list_relay_jobs as _relay_job_reader

_registry = HandlerRegistry()
method = _registry.method

_MARKER_NAME = "tb-build-active.json"
_MARKER_MAX_BYTES = 256 * 1024
_MARKER_CACHE_LIMIT = 128
_MARKER_CACHE: OrderedDict[tuple, dict | None] = OrderedDict()
# Nested build worktrees: ``<cwd>/.claude/worktrees/<name>/.claude/state/tb-build-active.json``.
# At most this many worktree dirs are opened (newest dir mtime first), out of at most
# ``_NESTED_ENTRY_SCAN_LIMIT`` directory entries listed.
_NESTED_WORKTREE_LIMIT = 32
_NESTED_ENTRY_SCAN_LIMIT = 256
# File age is "the last successful marker write", not activity (a marker verb can be refused all
# day while the conductor works). Age therefore never hides or idles a build; it only earns the
# quiet "Marker not updated since" hint past this many seconds.
_MARKER_QUIET_SECONDS = 30 * 60
# Guard against ancient markers: hide only when the owner is not a live Hermes session AND the
# lease (or, with no lease, the last write) is older than this.
_MARKER_ABANDONED_SECONDS = 7 * 24 * 60 * 60


def _build_open_dir(parent_fd: int, name: str) -> int:
    """Open one directory component under ``parent_fd``, refusing a symlink at that component."""
    import os

    return os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                   dir_fd=parent_fd)


def _build_walk(parent_fd: int, parts: tuple[str, ...]) -> int:
    """Walk ``parts`` below ``parent_fd`` one ``O_NOFOLLOW`` component at a time. The caller owns
    the returned fd; ``parent_fd`` is left open. Raises ``OSError`` on any missing/symlinked part."""
    import os

    fd = parent_fd
    try:
        for part in parts:
            next_fd = _build_open_dir(fd, part)
            if fd != parent_fd:
                os.close(fd)
            fd = next_fd
    except OSError:
        if fd != parent_fd:
            os.close(fd)
        raise
    return fd


def _build_marker_at(state_fd: int, cache_path: str) -> tuple[dict | None, bool, float | None]:
    """Read ``tb-build-active.json`` from an already-opened state dir fd.

    Returns ``(record, unreadable, mtime)``; the record is unfiltered (age and ``done`` are the
    caller's call). ``unreadable`` flags a torn or non-object marker so the client keeps its last
    value. The marker itself is opened with ``openat`` + ``O_NOFOLLOW`` and re-checked by fstat."""
    import json
    import os
    import stat

    try:
        before = os.stat(_MARKER_NAME, dir_fd=state_fd, follow_symlinks=False)
    except OSError:
        return None, False, None
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MARKER_MAX_BYTES:
        return None, False, None

    cache_key = (cache_path, before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
    if cache_key in _MARKER_CACHE:
        record = _MARKER_CACHE[cache_key]
        _MARKER_CACHE.move_to_end(cache_key)
    else:
        try:
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            marker_fd = os.open(_MARKER_NAME, file_flags, dir_fd=state_fd)
            try:
                after = os.fstat(marker_fd)
                if (not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                        or after.st_mtime_ns != before.st_mtime_ns
                        or after.st_size != before.st_size or after.st_size > _MARKER_MAX_BYTES):
                    return None, False, None
                chunks = []
                remaining = _MARKER_MAX_BYTES + 1
                while remaining:
                    chunk = os.read(marker_fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                final = os.fstat(marker_fd)
                if (len(content) > _MARKER_MAX_BYTES or final.st_mtime_ns != before.st_mtime_ns
                        or final.st_size != before.st_size or len(content) != before.st_size):
                    return None, False, None
            finally:
                os.close(marker_fd)
            record = json.loads(content.decode("utf-8"))
            if not isinstance(record, dict):
                return None, True, None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, True, None
        _MARKER_CACHE[cache_key] = record
        _MARKER_CACHE.move_to_end(cache_key)
        while len(_MARKER_CACHE) > _MARKER_CACHE_LIMIT:
            _MARKER_CACHE.popitem(last=False)

    if not isinstance(record, dict):
        return None, True, None
    return record, False, before.st_mtime


def _build_worktree_marker(worktrees_fd: int, name: str, cache_path: str) -> tuple[dict | None, bool, float | None]:
    """``<worktrees>/<name>/.claude/state`` walked with ``O_NOFOLLOW`` at every component, so a
    symlinked worktree dir (or a symlinked ``.claude``/``state`` inside it) is refused."""
    import os

    try:
        state_fd = _build_walk(worktrees_fd, (name, ".claude", "state"))
    except OSError:
        return None, False, None
    try:
        return _build_marker_at(state_fd, cache_path)
    finally:
        os.close(state_fd)


def _build_worktree_names(worktrees_fd: int) -> list[str]:
    """The newest ``_NESTED_WORKTREE_LIMIT`` real (non-symlink) directories under ``worktrees``,
    listed from the fd with at most ``_NESTED_ENTRY_SCAN_LIMIT`` entries examined."""
    import os
    import stat

    found: list[tuple[int, str]] = []
    try:
        with os.scandir(worktrees_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= _NESTED_ENTRY_SCAN_LIMIT:
                    break
                try:
                    info = os.stat(entry.name, dir_fd=worktrees_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    found.append((info.st_mtime_ns, entry.name))
    except (OSError, TypeError, NotImplementedError):
        return []
    found.sort(key=lambda item: (-item[0], item[1]))
    return [name for _mtime, name in found[:_NESTED_WORKTREE_LIMIT]]


def _build_session_owner_ids(session) -> frozenset[str]:
    """Ids a tb-build marker's ``session_id`` may carry for this gateway session: the Hermes
    session id (``session_key`` / ``agent.session_id``) or the live Claude SDK session id
    (``agent._claude_sdk_session._session_id``, or the id it is resuming before the first result)."""
    if not isinstance(session, dict):
        return frozenset()
    agent = session.get("agent")
    live = getattr(agent, "_claude_sdk_session", None) if agent is not None else None
    candidates = (
        session.get("session_key"),
        getattr(agent, "session_id", None) if agent is not None else None,
        getattr(live, "_session_id", None) if live is not None else None,
        getattr(live, "_resume_session_id", None) if live is not None else None,
    )
    return frozenset(value for value in candidates if isinstance(value, str) and value)


def _read_marker(workspace: Path, owner_ids: frozenset[str] = frozenset()) -> tuple[dict | None, bool, float | None, bool]:
    """Pick the build marker for this session: ``(record, unreadable, mtime, owned_by_this_session)``.

    Order: the root marker when this session owns it; else the newest-mtime nested-worktree marker
    this session owns; else the root marker whoever owns it (the workspace's own marker, as before
    discovery existed; its owner's liveness is judged by the caller). A nested marker owned by
    another session is never shown. Done markers never show."""
    import os

    try:
        workspace_fd = os.open(str(workspace), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, False, None, False
    unreadable_any = False
    root: tuple[dict, float] | None = None
    nested: tuple[dict, float] | None = None
    try:
        try:
            claude_fd = _build_open_dir(workspace_fd, ".claude")
        except OSError:
            return None, False, None, False
        try:
            try:
                state_fd = _build_open_dir(claude_fd, "state")
            except OSError:
                state_fd = None
            if state_fd is not None:
                try:
                    record, unreadable, mtime = _build_marker_at(state_fd, f"{workspace}/.claude/state/{_MARKER_NAME}")
                finally:
                    os.close(state_fd)
                unreadable_any |= unreadable
                if record is not None and mtime is not None and record.get("done") is not True:
                    root = (record, mtime)
                    if record.get("session_id") in owner_ids:
                        return record, False, mtime, True

            if owner_ids:
                try:
                    worktrees_fd = _build_open_dir(claude_fd, "worktrees")
                except OSError:
                    worktrees_fd = None
                if worktrees_fd is not None:
                    try:
                        for name in _build_worktree_names(worktrees_fd):
                            record, unreadable, mtime = _build_worktree_marker(
                                worktrees_fd, name, f"{workspace}/.claude/worktrees/{name}/.claude/state/{_MARKER_NAME}")
                            unreadable_any |= unreadable
                            if (record is None or mtime is None or record.get("done") is True
                                    or record.get("session_id") not in owner_ids):
                                continue
                            if nested is None or mtime > nested[1]:
                                nested = (record, mtime)
                    finally:
                        os.close(worktrees_fd)
        finally:
            os.close(claude_fd)
    finally:
        os.close(workspace_fd)

    if nested is not None:
        return nested[0], False, nested[1], True
    if root is not None:
        return root[0], False, root[1], False
    return None, unreadable_any, None, False


def _build_owner_live(owner_id) -> bool:
    """Whether a marker's ``session_id`` names a Hermes session that is live (a client attached) or
    busy (a turn running). Unknown ids (another CLI, a closed session) are not live: the lease is
    their only liveness signal."""
    if not isinstance(owner_id, str) or not owner_id:
        return False
    with _sessions_lock:
        records = list(_sessions.values())
    for record in records:
        if owner_id in _build_session_owner_ids(record):
            if record.get("running") or _session_has_live_transport(record):
                return True
    return False


def _build_epoch(value) -> float | None:
    """A marker time (epoch seconds or ISO string) as epoch seconds; ``None`` when absent/invalid."""
    import math
    from datetime import datetime, timezone

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def _clean_waiting_on(value) -> str:
    import unicodedata

    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if unicodedata.category(char) != "Cc")[:80]


def _build_resolve(workspace: Path, owner_ids: frozenset[str] = frozenset()) -> tuple[dict | None, bool, float | None, float | None]:
    """The one visibility decision for "this session's build", shared by the strip and the relay list:
    ``(marker, unreadable, idle_since, marker_stale_since)``; ``marker`` is ``None`` when nothing shows.

    A live owner (this session, or a Hermes session attached or mid-turn) always shows the build
    normally, whatever the file age or lease. Otherwise the lease is the only liveness signal: a lease
    that has run out makes the build ``idle`` (``idle_since`` = the lease expiry), and one that ran out
    over ``_MARKER_ABANDONED_SECONDS`` ago (or, with no lease, a write that old) hides it."""
    from datetime import datetime, timezone

    marker, unreadable, marker_mtime, mine = _read_marker(workspace, owner_ids)
    if marker is None or marker_mtime is None:
        return None, unreadable, None, None
    lease_time = _build_epoch(marker.get("lease_expires_at"))
    now = datetime.now(timezone.utc).timestamp()
    idle_since = None
    if not (mine or _build_owner_live(marker.get("session_id"))):
        if lease_time is not None and lease_time <= now:
            if now - lease_time > _MARKER_ABANDONED_SECONDS:
                return None, False, None, None
            idle_since = lease_time
        elif lease_time is None and now - marker_mtime > _MARKER_ABANDONED_SECONDS:
            return None, False, None, None
    marker_stale_since = (
        marker_mtime if idle_since is None and now - marker_mtime > _MARKER_QUIET_SECONDS else None
    )
    return marker, False, idle_since, marker_stale_since


def _build_snapshot(session_cwd: str, owner_ids: frozenset[str] = frozenset()) -> tuple[dict | None, bool]:
    # Local import: bind_module rebinds this function onto server globals, which lack ``math``.
    import math
    from pathlib import Path

    workspace = Path(session_cwd).expanduser().resolve()
    marker, unreadable, idle_since, marker_stale_since = _build_resolve(workspace, owner_ids)
    if marker is None:
        return None, unreadable

    plan_value = marker.get("plan")
    if not isinstance(plan_value, str) or not plan_value:
        return None, False
    total_value = marker.get("waves_total")
    try:
        waves_done = max(0, int(marker.get("waves_done", 0)))
    except (TypeError, ValueError):
        waves_done = 0
    valid_total = isinstance(total_value, (int, float)) and not isinstance(total_value, bool) and total_value > 0
    if isinstance(total_value, float):
        valid_total = valid_total and math.isfinite(total_value) and total_value.is_integer()
    if not valid_total:
        wave_current = waves_total = None
    else:
        waves_total = int(total_value)
        wave_current = min(waves_done + 1, waves_total)
    waiting_on = _clean_waiting_on(marker.get("waiting_on"))
    lease_expires_at = marker.get("lease_expires_at")
    state = "idle" if idle_since is not None else "blocked" if marker.get("blocked") is True else (
        "waiting" if waiting_on else "active"
    )

    run_id = marker.get("run_id") if isinstance(marker.get("run_id"), str) else ""
    # One reader: the shared relay projection derives ``build_match`` from the marker's run id.
    # Running/stale counts stay workspace-wide; the match count is over the same recent window.
    jobs = _relay_job_reader(str(workspace), marker_run_id=run_id)
    lanes_running = sum(job.get("status") == "running" for job in jobs)
    lanes_stale = sum(job.get("status") == "stale" for job in jobs)
    lanes_build_matched = sum(job.get("build_match") is True for job in jobs) if run_id else 0

    def wire_time(value):
        return value if isinstance(value, (str, int, float)) else None

    return {
        "state": state,
        "plan": Path(plan_value).stem,
        "run_id": run_id,
        "session_id": marker.get("session_id") if isinstance(marker.get("session_id"), str) else "",
        "wave_current": wave_current,
        "waves_total": waves_total,
        "waves_done": waves_done,
        "waiting_on": waiting_on,
        "wait_since": wire_time(marker.get("wait_since")),
        "armed_at": wire_time(marker.get("armed_at")),
        "lease_expires_at": wire_time(lease_expires_at),
        "lanes_running": lanes_running,
        "lanes_stale": lanes_stale,
        "lanes_build_matched": lanes_build_matched,
        "usd": None,
        "usd_source": "missing",
        "stage": None,
        "unit_id": None,
        "idle_since": idle_since,
        "marker_stale_since": marker_stale_since,
    }, False


@method("conductor_build.get")
def _conductor_build_get(rid, params):
    session_id = _str_param(params, "session_id")
    transport, session = _current_session_steer_authority(session_id)
    if transport is None or session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    session_cwd = session.get("cwd")
    if not isinstance(session_cwd, str) or not session_cwd:
        return _ok(rid, {"build": None, "unreadable": False})
    build, unreadable = _build_snapshot(session_cwd, _build_session_owner_ids(session))
    return _ok(rid, {"build": build, "unreadable": unreadable})


def register(server):
    bind_module(globals(), server)
