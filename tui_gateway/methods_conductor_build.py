"""Read-only conductor tb-build status scoped to the calling session workspace."""

from __future__ import annotations

import math
import re
from collections import OrderedDict

from .method_ctx import HandlerRegistry, bind_module
from .methods_relay_jobs import _list_relay_jobs as _relay_job_reader

_registry = HandlerRegistry()
method = _registry.method

_MARKER_MAX_BYTES = 256 * 1024
_MARKER_MAX_AGE_SECONDS = 12 * 60 * 60
_STALE_KEY = "\x00hermes_stale"
_MARKER_CACHE_LIMIT = 128
_MARKER_CACHE: OrderedDict[tuple[str, int, int], dict | None] = OrderedDict()


def _read_json_file(directory: Path, name: str, max_bytes: int = _MARKER_MAX_BYTES, *,
                    subdirs: tuple[str, ...] = ()) -> tuple[dict | None, float | None, bool]:
    """``(record, mtime, unreadable)`` for ``directory/<subdirs...>/name`` without following any symlink below
    ``directory``: each subdir is opened relative to its parent's descriptor with O_NOFOLLOW, so swapping a
    component for a symlink mid-read can't redirect the open. A missing file is ``(None, None, False)``; a file
    that is not a regular JSON object is unreadable."""
    import json
    import os
    import stat

    try:
        if directory.is_symlink() or directory.resolve() != directory:
            return None, None, False
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(directory, directory_flags)
    except FileNotFoundError:
        return None, None, False
    except (OSError, RuntimeError):
        return None, None, False
    for part in subdirs:
        try:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
        except OSError:  # missing, not a directory, or a symlink (ELOOP): never followed
            os.close(directory_fd)
            return None, None, False
        os.close(directory_fd)
        directory_fd = child_fd
    directory = directory.joinpath(*subdirs)

    try:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            return None, None, False
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None, None, False

        cache_key = (str(directory / name), before.st_mtime_ns, before.st_size)
        if cache_key in _MARKER_CACHE:
            record = _MARKER_CACHE[cache_key]
            _MARKER_CACHE.move_to_end(cache_key)
        else:
            try:
                file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                try:
                    after = os.fstat(file_fd)
                    if (not stat.S_ISREG(after.st_mode) or after.st_mtime_ns != before.st_mtime_ns
                            or after.st_size != before.st_size or after.st_size > max_bytes):
                        return None, None, False
                    chunks = []
                    remaining = max_bytes + 1
                    while remaining:
                        chunk = os.read(file_fd, min(64 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    content = b"".join(chunks)
                    final = os.fstat(file_fd)
                    if (len(content) > max_bytes or final.st_mtime_ns != before.st_mtime_ns
                            or final.st_size != before.st_size or len(content) != before.st_size):
                        return None, None, False
                finally:
                    os.close(file_fd)
                record = json.loads(content.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None, None, True
            _MARKER_CACHE[cache_key] = record
            _MARKER_CACHE.move_to_end(cache_key)
            while len(_MARKER_CACHE) > _MARKER_CACHE_LIMIT:
                _MARKER_CACHE.popitem(last=False)
        if not isinstance(record, dict):
            return None, None, True
        return record, before.st_mtime, False
    finally:
        os.close(directory_fd)


def owner_liveness(session: dict | None, gateway_server: Any = None) -> str:
    """Probe session liveness: 'busy', 'attached', 'cli', or 'none'.

    Matches the Conductors router owner-liveness probe (§4):
    - gateway session running -> busy
    - open non-detached transport -> attached
    - live_claude_cli_session(agent) == 'live' -> cli
    - otherwise -> none
    """
    if not isinstance(session, dict) or session.get("_finalized"):
        return "none"
    if session.get("running"):
        return "busy"
    transport = session.get("transport")
    if transport and not getattr(transport, "_closed", False):
        detached = getattr(gateway_server, "_detached_ws_transport", None) if gateway_server else None
        if detached is None and "_detached_ws_transport" in globals():
            detached = globals().get("_detached_ws_transport")
        if detached is None:
            try:
                from tui_gateway import server as _default_server
                detached = getattr(_default_server, "_detached_ws_transport", None)
            except Exception:
                detached = None
        if transport is not detached:
            return "attached"
    agent = session.get("agent")
    if agent:
        try:
            from agent.claude_sdk_runtime_continuity import live_claude_cli_session
            c_state, _ = live_claude_cli_session(agent)
            if c_state == "live":
                return "cli"
        except Exception:
            pass
    return "none"


def _has_fresh_running_lane(jobs: list[dict] | None, now: float) -> bool:
    """Return True if any build lane is running with a heartbeat of 5 min or less."""
    if not jobs:
        return False
    from datetime import datetime, timezone
    for job in jobs:
        if isinstance(job, dict) and job.get("status") == "running":
            hb = job.get("heartbeat_epoch")
            if isinstance(hb, (int, float)):
                if (now - hb) <= 300:
                    return True
            time_str = job.get("heartbeat_at") or job.get("spawned_at")
            if isinstance(time_str, str) and time_str:
                try:
                    dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now - dt.timestamp()) <= 300:
                        return True
                except (ValueError, OSError):
                    pass
    return False


def _is_lease_expired_or_absent(lease_expires_at: Any, now: float) -> bool:
    """Return True if the lease is in the past (expired) or absent."""
    if lease_expires_at is None or lease_expires_at == "":
        return True
    if isinstance(lease_expires_at, (int, float)) and not isinstance(lease_expires_at, bool):
        return float(lease_expires_at) <= now
    if isinstance(lease_expires_at, str):
        from datetime import datetime, timezone
        try:
            lease_time = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
            if lease_time.tzinfo is None:
                lease_time = lease_time.replace(tzinfo=timezone.utc)
            return lease_time.timestamp() <= now
        except (ValueError, OSError):
            return True
    return True


def _is_marker_stale(
    record: dict | None,
    mtime: float | None,
    session: dict | None,
    jobs: list[dict] | None,
    now: float,
) -> bool:
    if record is None or mtime is None:
        return False
    if (now - mtime) <= _MARKER_MAX_AGE_SECONDS:
        return False
    if owner_liveness(session) in ("busy", "attached"):
        return False
    if _has_fresh_running_lane(jobs, now):
        return False
    if not _is_lease_expired_or_absent(record.get("lease_expires_at"), now):
        return False
    return True


def _marker_view(
    record: dict | None,
    mtime: float | None,
    *,
    session: dict | None = None,
    jobs: list[dict] | None = None,
) -> dict | None:
    """A finished build shows nothing; an unfinished one past 12 h stays visible, muted (D32)
    when the owner session is idle. A private key, so a marker field can never collide with it."""
    from datetime import datetime, timezone

    if record is None or record.get("done") is True:
        return None
    now = datetime.now(timezone.utc).timestamp()
    if _is_marker_stale(record, mtime, session, jobs, now):
        record = dict(record)
        record[_STALE_KEY] = True
    return record


_BUILD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _indexed_marker(
    state: Path,
    claude_sid: str | None,
    *,
    session: dict | None = None,
    jobs: list[dict] | None = None,
) -> tuple[dict | None, bool, bool]:
    """Conductor 3.62's ``tb-builds-index.json`` (schema ``tb-builds-index/v1``): ``(record, unreadable,
    decided)``. ``decided`` is False when there's no index or it names no build for this session, so the
    caller falls back to the flat marker. A row is trusted only for a namespaced marker inside this state
    dir (``builds/<build_id>/tb-build-active.json``) or the flat file itself, and only after the marker is
    re-read (the index is a derived view that can lag a deleted marker)."""
    index, _mtime, unreadable = _read_json_file(state, "tb-builds-index.json", 1024 * 1024)
    if index is None or index.get("schema") != "tb-builds-index/v1" or not isinstance(index.get("builds"), list):
        return None, unreadable, False
    rows = [row for row in index["builds"]
            if isinstance(row, dict) and row.get("status") in ("open", "blocked")]
    if claude_sid:
        mine = [row for row in rows if row.get("session_id") == claude_sid]
    else:
        mine = []
    # Only this session's own build, or the checkout's single live build; several builds and none of them
    # ours is ambiguous (conductor refuses to guess too), so the flat marker's first-armed build shows.
    candidates = mine or (rows if len(rows) == 1 else [])
    for row in reversed(candidates):  # sorted by (armed_at, build_id): newest last
        build_id, legacy = row.get("build_id"), row.get("legacy_flat") is True
        if legacy or build_id is None:
            subdirs: tuple[str, ...] = ()
        elif isinstance(build_id, str) and _BUILD_ID_RE.fullmatch(build_id) and build_id not in (".", ".."):
            subdirs = ("builds", build_id)
        else:
            continue
        expected = state.joinpath(*subdirs, "tb-build-active.json")
        if str(row.get("marker_path") or "") not in ("", str(expected)):
            continue
        record, mtime, bad = _read_json_file(state, "tb-build-active.json", subdirs=subdirs)
        if bad:
            return None, True, True
        if record is not None and (not claude_sid or not mine or record.get("session_id") == claude_sid):
            return _marker_view(record, mtime, session=session, jobs=jobs), False, True
    return None, False, False


def _read_marker(
    workspace: Path,
    claude_sid: str | None = None,
    *,
    session: dict | None = None,
    jobs: list[dict] | None = None,
) -> tuple[dict | None, bool]:
    """The build to show for this workspace: the 3.62 builds index (this session's own row), else the
    flat ``tb-build-active.json`` (pre-3.62 installs, and 3.62's dual-written first build)."""
    if jobs is None:
        jobs = _relay_job_reader(str(workspace))
    state = workspace / ".claude" / "state"
    record, unreadable, decided = _indexed_marker(state, claude_sid, session=session, jobs=jobs)
    if decided:
        return record, unreadable
    flat, mtime, flat_unreadable = _read_json_file(state, "tb-build-active.json")
    return _marker_view(flat, mtime, session=session, jobs=jobs), flat_unreadable or (unreadable and flat is None)


def _session_claude_sid(session: dict) -> str | None:
    """The Claude Code session id conductor stamps on its markers, for an SDK-lane Hermes session."""
    import json

    agent = session.get("agent") if isinstance(session, dict) else None
    db, sid = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
    if not db or not sid:
        return None
    try:
        raw = (db.get_session(sid) or {}).get("claude_sdk_session_id")
    except Exception:  # noqa: BLE001 - a panel read never fails on the session row
        return None
    if not isinstance(raw, str) or not raw:
        return None
    from agent.claude_sdk_runtime_continuity import _SDK_RESUME_BINDING_PREFIX as prefix

    if raw.startswith(prefix):
        try:
            raw = json.loads(raw[len(prefix):]).get("id")
        except (ValueError, AttributeError):
            return None
    return raw if isinstance(raw, str) and raw else None

def _clean_waiting_on(value) -> str:
    import unicodedata

    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if unicodedata.category(char) != "Cc")[:80]


def _build_snapshot(
    session_cwd: str,
    claude_sid: str | None = None,
    *,
    session: dict | None = None,
) -> tuple[dict | None, bool]:
    from datetime import datetime, timezone
    from pathlib import Path

    from . import git_probe

    resolved_cwd = Path(session_cwd).expanduser().resolve()
    top = git_probe.repo_root(str(resolved_cwd))
    workspace = Path(top).resolve() if top else resolved_cwd
    jobs = _relay_job_reader(str(workspace))
    marker, unreadable = _read_marker(workspace, claude_sid, session=session, jobs=jobs)
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
    state = "stale" if marker.get(_STALE_KEY) is True else (
        "blocked" if marker.get("blocked") is True else (
            "lease_expired" if lease_expired else "waiting" if waiting_on else "active"
        )
    )

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
    build, unreadable = _build_snapshot(session_cwd, _session_claude_sid(session), session=session)
    return _ok(rid, {"build": build, "unreadable": unreadable})


def register(server):
    bind_module(globals(), server)
