"""Read-only conductor worker-job snapshots scoped to the calling session's workspace."""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_RELAY_TERMINAL_WINDOW = timedelta(minutes=10)
_RELAY_JOB_LIMIT = 20
_RELAY_JOB_MAX_BYTES = 256 * 1024
_RELAY_JOB_STALE_SECONDS = 5 * 60
_RELAY_JOB_CACHE_LIMIT = 128
_RELAY_JOB_CACHE: OrderedDict[tuple[str, int, int], dict | None] = OrderedDict()


def _parse_job_time(value) -> datetime | None:
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _list_relay_jobs(session_cwd: str, *, now: datetime | None = None) -> list[dict]:
    """Read only ``w_*.json`` records from the session workspace's job store."""
    import os
    import stat
    from datetime import datetime, timedelta, timezone

    workspace = Path(session_cwd).expanduser().resolve()
    jobs_dir = workspace / ".claude" / "state" / "worker-spawn" / "jobs"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    rows: list[tuple[datetime, dict]] = []

    try:
        if jobs_dir.is_symlink() or jobs_dir.resolve() != jobs_dir:
            return []
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(jobs_dir, directory_flags)
    except (OSError, RuntimeError):
        return []

    try:
        candidates = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if not (entry.name.startswith("w_") and entry.name.endswith(".json")):
                    continue
                try:
                    file_stat = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _RELAY_JOB_MAX_BYTES:
                    continue
                candidates.append((file_stat.st_mtime_ns, file_stat.st_size, entry.name))
        candidates.sort(reverse=True)

        for mtime_ns, size, name in candidates:
            path = jobs_dir / name
            cache_key = (str(path), mtime_ns, size)
            if cache_key in _RELAY_JOB_CACHE:
                record = _RELAY_JOB_CACHE[cache_key]
                _RELAY_JOB_CACHE.move_to_end(cache_key)
            else:
                try:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                    file_fd = os.open(name, flags, dir_fd=directory_fd)
                    try:
                        file_stat = os.fstat(file_fd)
                        if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mtime_ns != mtime_ns
                                or file_stat.st_size != size or size > _RELAY_JOB_MAX_BYTES):
                            continue
                        content = os.read(file_fd, _RELAY_JOB_MAX_BYTES)
                    finally:
                        os.close(file_fd)
                    record = json.loads(content.decode("utf-8"))
                    if not isinstance(record, dict):
                        record = None
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    record = None
                _RELAY_JOB_CACHE[cache_key] = record
                _RELAY_JOB_CACHE.move_to_end(cache_key)
                while len(_RELAY_JOB_CACHE) > _RELAY_JOB_CACHE_LIMIT:
                    _RELAY_JOB_CACHE.popitem(last=False)
            if not isinstance(record, dict):
                continue
            job_id = record.get("job_id")
            status = record.get("status")
            spawned_at = _parse_job_time(record.get("spawned_at"))
            if (not isinstance(job_id, str) or not job_id or not isinstance(record.get("worker"), str)
                    or not record["worker"] or not isinstance(status, str) or spawned_at is None):
                continue
            if status == "running":
                now_epoch = current.timestamp()
                try:
                    lease_expiry = float(record.get("lease_expires_epoch"))
                except (TypeError, ValueError):
                    lease_expiry = None
                try:
                    heartbeat_epoch = float(record.get("heartbeat_epoch"))
                except (TypeError, ValueError):
                    heartbeat_epoch = None
                if ((lease_expiry is not None and lease_expiry <= now_epoch)
                        or (heartbeat_epoch is not None and now_epoch - heartbeat_epoch > _RELAY_JOB_STALE_SECONDS)):
                    status = "stale"
            if status != "running":
                completed_at = _parse_job_time(record.get("heartbeat_at")) or spawned_at
                if completed_at < current - _RELAY_TERMINAL_WINDOW or completed_at > current:
                    continue
            try:
                duration = float(record["duration_sec"]) if record.get("duration_sec") is not None else None
            except (TypeError, ValueError):
                duration = None
            rows.append((spawned_at, {
                "job_id": job_id,
                "worker": str(record.get("worker") or ""),
                "model": str(record.get("model") or ""),
                "model_resolved": str(record.get("model_resolved") or ""),
                "lane": str(record.get("lane") or ""),
                "role": str(record.get("role") or ""),
                "status": status,
                "spawned_at": record["spawned_at"],
                "heartbeat_at": str(record.get("heartbeat_at") or ""),
                "duration_sec": duration,
            }))
            if len(rows) >= _RELAY_JOB_LIMIT:
                break
    finally:
        os.close(directory_fd)

    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in rows[:_RELAY_JOB_LIMIT]]


@method("relay_jobs.list")
def _relay_jobs_list(rid, params):
    session_id = _str_param(params, "session_id")
    transport, session = _current_session_steer_authority(session_id)
    if transport is None or session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    session_cwd = session.get("cwd")
    if not isinstance(session_cwd, str) or not session_cwd:
        return _ok(rid, {"jobs": []})
    return _ok(rid, {"jobs": _list_relay_jobs(session_cwd)})


def register(server):
    bind_module(globals(), server)
