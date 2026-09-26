"""Read-only conductor tb-build status scoped to the calling session workspace."""

from __future__ import annotations

import math
from collections import OrderedDict

from .method_ctx import HandlerRegistry, bind_module
from .methods_relay_jobs import _list_relay_jobs as _relay_job_reader

_registry = HandlerRegistry()
method = _registry.method

_MARKER_MAX_BYTES = 256 * 1024
_MARKER_MAX_AGE_SECONDS = 12 * 60 * 60
_MARKER_CACHE_LIMIT = 128
_MARKER_CACHE: OrderedDict[tuple[str, int, int], dict | None] = OrderedDict()


def _read_marker(workspace: Path) -> tuple[dict | None, bool]:
    """Read the active marker without following the state directory or marker symlinks."""
    import json
    import os
    import stat
    from datetime import datetime, timezone

    state = workspace / ".claude" / "state"
    marker_name = "tb-build-active.json"
    try:
        if state.is_symlink() or state.resolve() != state:
            return None, False
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(state, directory_flags)
    except FileNotFoundError:
        return None, False
    except (OSError, RuntimeError):
        return None, False

    try:
        try:
            before = os.stat(marker_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, False
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MARKER_MAX_BYTES:
            return None, False

        path = state / marker_name
        cache_key = (str(path), before.st_mtime_ns, before.st_size)
        if cache_key in _MARKER_CACHE:
            record = _MARKER_CACHE[cache_key]
            _MARKER_CACHE.move_to_end(cache_key)
        else:
            try:
                file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                marker_fd = os.open(marker_name, file_flags, dir_fd=directory_fd)
                try:
                    after = os.fstat(marker_fd)
                    if (not stat.S_ISREG(after.st_mode) or after.st_mtime_ns != before.st_mtime_ns
                            or after.st_size != before.st_size or after.st_size > _MARKER_MAX_BYTES):
                        return None, False
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
                        return None, False
                finally:
                    os.close(marker_fd)
                record = json.loads(content.decode("utf-8"))
                if not isinstance(record, dict):
                    return None, True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None, True
            _MARKER_CACHE[cache_key] = record
            _MARKER_CACHE.move_to_end(cache_key)
            while len(_MARKER_CACHE) > _MARKER_CACHE_LIMIT:
                _MARKER_CACHE.popitem(last=False)

        if not isinstance(record, dict):
            return None, True
        age = datetime.now(timezone.utc).timestamp() - before.st_mtime
        if age > _MARKER_MAX_AGE_SECONDS or record.get("done") is True:
            return None, False
        return record, False
    finally:
        os.close(directory_fd)


def _clean_waiting_on(value) -> str:
    import unicodedata

    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if unicodedata.category(char) != "Cc")[:80]


def _build_snapshot(session_cwd: str) -> tuple[dict | None, bool]:
    from datetime import datetime, timezone
    from pathlib import Path

    workspace = Path(session_cwd).expanduser().resolve()
    marker, unreadable = _read_marker(workspace)
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
    lease_expired = False
    if isinstance(lease_expires_at, (int, float)):
        lease_expired = lease_expires_at <= datetime.now(timezone.utc).timestamp()
    elif isinstance(lease_expires_at, str):
        try:
            lease_time = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
            if lease_time.tzinfo is None:
                lease_time = lease_time.replace(tzinfo=timezone.utc)
            lease_expired = lease_time.timestamp() <= datetime.now(timezone.utc).timestamp()
        except ValueError:
            pass
    state = "blocked" if marker.get("blocked") is True else (
        "lease_expired" if lease_expired else "waiting" if waiting_on else "active"
    )

    jobs = _relay_job_reader(str(workspace))
    lanes_running = sum(job.get("status") == "running" for job in jobs)
    lanes_stale = sum(job.get("status") == "stale" for job in jobs)
    run_id = marker.get("run_id") if isinstance(marker.get("run_id"), str) else ""
    # The shared #41 reader intentionally returns its public relay projection, which
    # currently omits build_run_id. Preserve its single-reader contract; zero means
    # no match metadata was available in this projection.
    lanes_build_matched = sum(
        isinstance(job.get("build_run_id"), str)
        and (job["build_run_id"] == run_id or job["build_run_id"].startswith(run_id + "-"))
        for job in jobs
    ) if run_id else 0

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
    build, unreadable = _build_snapshot(session_cwd)
    return _ok(rid, {"build": build, "unreadable": unreadable})


def register(server):
    bind_module(globals(), server)
