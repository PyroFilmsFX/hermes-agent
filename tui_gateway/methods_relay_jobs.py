"""Read-only conductor worker-job snapshots scoped to the calling session's workspace."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_RELAY_TERMINAL_WINDOW = timedelta(minutes=10)
# A derived-stale worker (record still says running) is the one row the owner most needs to see,
# so it outlives the terminal window until an hour after its last heartbeat.
_RELAY_STALE_WINDOW = timedelta(minutes=60)
_RELAY_JOB_LIMIT = 20
_RELAY_BUILD_JOB_LIMIT = 40
_RELAY_SCOPES = ("recent", "build")
_RELAY_BRIEF_MAX_BYTES = 4 * 1024
_RELAY_BRIEF_CACHE: OrderedDict[tuple[str, int, int, int, int], str] = OrderedDict()
_RELAY_JOB_ID_RE = re.compile(r"^w_\d{8}T\d{6}Z_[0-9a-f]{4}$")
_RELAY_HEX_RUN_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
# A 32-hex id anywhere inside a label (``impl-<run id>``), with the separators around it.
_RELAY_EMBEDDED_HEX_RE = re.compile(r"[-_.]*[0-9a-fA-F]{32,}[-_.]*")
_RELAY_LABEL_MAX = 40
_RELAY_PURPOSE_MAX = 100
_RELAY_BRIEF_SKIP_PREFIXES = (
    "CROSS_MODEL_CONTRACT", "SINGLE-SEAT DIRECTIVE", "ANALYSIS ONLY", "AGY WRITE-LANE", "TB_", "<",
    # Seat metadata some briefs open with; the model is already on the row's second line.
    "MODEL:", "EFFORT:",
)
_RELAY_BRIEF_BARE_SCOPE_RE = re.compile(r"^scope:\s*\|?\s*$", re.IGNORECASE)
_RELAY_BRIEF_LABEL_RE = re.compile(r"^(?:scope|goal|task|bug)\s*:\s*", re.IGNORECASE)
# An absolute POSIX path token. The look-behind keeps URLs (``https://host/a``) and relative
# paths (``a/b``) out; the caller swaps the whole token for its basename.
_RELAY_ABS_PATH_RE = re.compile(r"(?<![\w.:/~-])/[^\s'\"`()<>\[\]{},;]+")
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


def _relay_clean_text(value) -> str:
    import unicodedata

    if not isinstance(value, str):
        return ""
    return "".join(" " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char for char in value)


def _relay_run_label(record: dict, marker_run_id: str) -> str:
    """The worker's human name, from the caller-chosen ``build_run_id`` (never an id)."""
    import re
    raw = record.get("build_run_id") or record.get("run_id")
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if marker_run_id and (value == marker_run_id or value.startswith(marker_run_id + "-")):
        value = value[len(marker_run_id):]
    elif re.match(r"^[0-9a-fA-F]{32}-", value):
        # Another build's run id prefix is still an id, never a name on first read.
        value = value[33:]
    value = value.lstrip("-")
    if not value or _RELAY_HEX_RUN_ID_RE.match(value) or _RELAY_JOB_ID_RE.match(value):
        return ""
    value = re.sub(r"[^A-Za-z0-9._-]", "", value)
    # An id embedded in the name (``impl-<32 hex>``) is still an id: drop it and its separators.
    value, embedded = _RELAY_EMBEDDED_HEX_RE.subn("-", value)
    if embedded:
        value = re.sub(r"-{2,}", "-", value).strip("-._")
    if not value or _RELAY_JOB_ID_RE.match(value):
        return ""
    return value[:_RELAY_LABEL_MAX]


def _relay_build_match(record: dict, marker_run_id: str) -> bool:
    if not marker_run_id:
        return False
    run_id = record.get("build_run_id") or record.get("run_id")
    return isinstance(run_id, str) and (run_id == marker_run_id or run_id.startswith(marker_run_id + "-"))


def _relay_place(cwd, workspace: Path, session_cwd: str) -> str:
    """Where the worker ran, as one basename (the lane directory), never a path."""
    import os

    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    normalized = os.path.normpath(cwd.strip())
    if normalized in {str(workspace), os.path.normpath(session_cwd)}:
        return "main checkout"
    marker = "/.claude/worktrees/"
    if marker in normalized + "/":
        normalized = normalized[: (normalized + "/").index(marker)] or "/"
    name = os.path.basename(normalized.rstrip("/"))
    return _relay_clean_text(name).strip()[:60]


def _relay_basename_paths(text: str) -> str:
    import os
    import re

    def basename(match):
        token = match.group(0)
        trail = re.search(r"[.:]+$", token)
        suffix = trail.group(0) if trail else ""
        core = token[: len(token) - len(suffix)] if suffix else token
        return os.path.basename(core.rstrip("/")) + suffix

    return _RELAY_ABS_PATH_RE.sub(basename, text)


def _relay_purpose_from_brief(text: str) -> str:
    """First sentence of the brief's first meaningful line. ``text`` is already redacted."""
    import re
    for line in text.splitlines():
        stripped = _relay_clean_text(line).strip()
        if not stripped or stripped.startswith(_RELAY_BRIEF_SKIP_PREFIXES):
            continue
        if _RELAY_BRIEF_BARE_SCOPE_RE.match(stripped):
            continue  # the purpose is the next non-blank line; strip() dedents it
        stripped = _RELAY_BRIEF_LABEL_RE.sub("", stripped, count=1).strip()
        if not stripped:
            continue
        sentence_end = re.search(r"\.\s", stripped)
        if sentence_end:
            stripped = stripped[: sentence_end.start()]
        stripped = stripped.rstrip()
        if stripped.endswith(".") and not stripped.endswith(".."):
            stripped = stripped[:-1]
        stripped = re.sub(r"\s+", " ", _relay_basename_paths(stripped)).strip()
        if not stripped:
            continue
        stripped = stripped[0].upper() + stripped[1:]
        if len(stripped) > _RELAY_PURPOSE_MAX:
            cut = stripped[: _RELAY_PURPOSE_MAX - 1]
            space = cut.rfind(" ")
            if space > _RELAY_PURPOSE_MAX // 2:
                cut = cut[:space]
            stripped = cut.rstrip(" ,;:-") + "…"
        return stripped
    return ""


def _relay_open_child_dir(parent_fd: int, name: str) -> int:
    """Open one directory component under ``parent_fd``, refusing a symlink at that component."""
    import os

    return os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                   dir_fd=parent_fd)


def _relay_open_spawn_dir(workspace: Path) -> int | None:
    """Walk ``<workspace>/.claude/state/worker-spawn`` one component at a time with
    ``O_NOFOLLOW|O_DIRECTORY``. The jobs and briefs directories are both opened from this one fd,
    so a symlink swapped into any parent after the walk cannot redirect either read."""
    import os

    try:
        fd = os.open(str(workspace), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        for part in (".claude", "state", "worker-spawn"):
            next_fd = _relay_open_child_dir(fd, part)
            os.close(fd)
            fd = next_fd
    except OSError:
        os.close(fd)
        return None
    return fd


def _relay_brief_purpose(briefs_fd: int | None, briefs_key: str, job_id: str) -> str:
    """Read ``briefs/<job_id>.txt`` through the briefs dir fd (opened from the same worker-spawn
    fd as the jobs dir). The record's own ``brief_path`` is never followed: only the canonical job
    id picks the file, and it is opened with ``openat`` + ``O_NOFOLLOW``, never by path string."""
    import os
    import stat

    if briefs_fd is None or not _RELAY_JOB_ID_RE.match(job_id):
        return ""
    name = f"{job_id}.txt"
    try:
        before = os.stat(name, dir_fd=briefs_fd, follow_symlinks=False)
    except OSError:
        return ""
    if not stat.S_ISREG(before.st_mode):
        return ""
    cache_key = (f"{briefs_key}/{name}", before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
    if cache_key in _RELAY_BRIEF_CACHE:
        _RELAY_BRIEF_CACHE.move_to_end(cache_key)
        return _RELAY_BRIEF_CACHE[cache_key]
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        brief_fd = os.open(name, flags, dir_fd=briefs_fd)
        try:
            after = os.fstat(brief_fd)
            if (not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                    or after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size):
                return ""
            content = os.read(brief_fd, _RELAY_BRIEF_MAX_BYTES)
        finally:
            os.close(brief_fd)
    except OSError:
        return ""
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(content.decode("utf-8", errors="replace"), force=True)
    purpose = _relay_purpose_from_brief(text)
    _RELAY_BRIEF_CACHE[cache_key] = purpose
    while len(_RELAY_BRIEF_CACHE) > _RELAY_JOB_CACHE_LIMIT:
        _RELAY_BRIEF_CACHE.popitem(last=False)
    return purpose


def _list_relay_jobs(session_cwd: str, *, now: datetime | None = None, marker_run_id: str = "",
                     scope: str = "recent") -> list[dict]:
    """Read only ``w_*.json`` records (and their ``briefs/<job_id>.txt``) from the session
    workspace's job store. ``recent`` keeps running rows plus the terminal window; ``build`` keeps
    every row whose run id belongs to the armed marker's ``marker_run_id``, with no age window."""
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
    build_scope = scope == "build"
    limit = _RELAY_BUILD_JOB_LIMIT if build_scope else _RELAY_JOB_LIMIT
    if build_scope and not marker_run_id:
        return []

    spawn_fd = _relay_open_spawn_dir(workspace)
    if spawn_fd is None:
        return []
    try:
        directory_fd = _relay_open_child_dir(spawn_fd, "jobs")
    except OSError:
        os.close(spawn_fd)
        return []
    try:
        briefs_fd = _relay_open_child_dir(spawn_fd, "briefs")
    except OSError:
        briefs_fd = None
    finally:
        os.close(spawn_fd)
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
            build_match = _relay_build_match(record, marker_run_id)
            if build_scope:
                if not build_match:
                    continue
            elif status != "running":
                completed_at = _parse_job_time(record.get("heartbeat_at")) or spawned_at
                if status == "stale":
                    try:
                        completed_at = datetime.fromtimestamp(float(record.get("heartbeat_epoch")), timezone.utc)
                    except (TypeError, ValueError, OverflowError, OSError):
                        pass
                window = _RELAY_STALE_WINDOW if status == "stale" else _RELAY_TERMINAL_WINDOW
                if completed_at < current - window or completed_at > current:
                    continue
            try:
                duration = float(record["duration_sec"]) if record.get("duration_sec") is not None else None
            except (TypeError, ValueError):
                duration = None
            raw_exit = record.get("exit_code")
            exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
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
                "label": _relay_run_label(record, marker_run_id),
                "purpose": _relay_brief_purpose(briefs_fd, str(jobs_dir.parent / "briefs"), job_id),
                "place": _relay_place(record.get("cwd"), workspace, session_cwd),
                "effort": _relay_clean_text(record.get("effort")).strip()[:24],
                "exit_code": exit_code,
                "build_match": build_match,
            }))
            if len(rows) >= limit:
                break
    finally:
        os.close(directory_fd)
        if briefs_fd is not None:
            os.close(briefs_fd)

    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in rows[:limit]]


@method("relay_jobs.list")
def _relay_jobs_list(rid, params):
    session_id = _str_param(params, "session_id")
    transport, session = _current_session_steer_authority(session_id)
    if transport is None or session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    scope = params.get("scope", "recent") if isinstance(params, dict) else "recent"
    if scope not in _RELAY_SCOPES:
        return _err(rid, 4092, "invalid relay job scope")
    session_cwd = session.get("cwd")
    if not isinstance(session_cwd, str) or not session_cwd:
        return _ok(rid, {"jobs": []})
    # Same visibility decision as ``conductor_build.get``, so "this build" means one marker everywhere.
    marker = _build_resolve(Path(session_cwd).expanduser().resolve(), _build_session_owner_ids(session))[0]
    marker_run_id = marker.get("run_id") if isinstance(marker, dict) and isinstance(marker.get("run_id"), str) else ""
    return _ok(rid, {"jobs": _list_relay_jobs(session_cwd, marker_run_id=marker_run_id, scope=scope)})


def register(server):
    bind_module(globals(), server)
