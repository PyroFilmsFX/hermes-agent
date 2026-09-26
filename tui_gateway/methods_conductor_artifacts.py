"""Bounded local worker log reads for the conductor artifact viewer."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_JOB_ID_PATTERN = re.compile(r"^w_\d{8}T\d{6}Z_[0-9a-f]{4}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_JOB_MAX_BYTES = 256 * 1024
_WINDOW_MAX_BYTES = 1024 * 1024
_WINDOW_DEFAULT_BYTES = 256 * 1024
_HASH_MAX_BYTES = 64 * 1024 * 1024
_DURABLE_UNAVAILABLE = {"state": "unavailable", "reason": "not_configured"}


def _open_child_dir(parent_fd: int, name: str) -> int:
    import os

    return os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)


def _workspace_fd(session_cwd: str) -> int:
    import os
    from pathlib import Path

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(str(Path(session_cwd).expanduser().resolve()), flags)


def _state_fds(session_cwd: str):
    """Open the worker-spawn directory without following any state-directory symlink."""
    import os

    fd = _workspace_fd(session_cwd)
    try:
        for part in (".claude", "state", "worker-spawn"):
            next_fd = _open_child_dir(fd, part)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_record(spawn_fd: int, job_id: str) -> dict | None:
    import json
    import os
    import stat

    try:
        jobs_fd = _open_child_dir(spawn_fd, "jobs")
    except OSError:
        return None
    try:
        name = f"{job_id}.json"
        try:
            before = os.stat(name, dir_fd=jobs_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode) or before.st_size > _JOB_MAX_BYTES:
            raise OSError("unreadable job record")
        file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=jobs_fd)
        try:
            after = os.fstat(file_fd)
            if not stat.S_ISREG(after.st_mode) or after.st_size > _JOB_MAX_BYTES:
                raise OSError("unreadable job record")
            chunks = []
            remaining = _JOB_MAX_BYTES + 1
            while remaining:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _JOB_MAX_BYTES or len(raw) != after.st_size:
                raise OSError("unreadable job record")
        finally:
            os.close(file_fd)
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict) or record.get("job_id") != job_id:
            raise OSError("unreadable job record")
        return record
    finally:
        os.close(jobs_fd)


def _open_log(spawn_fd: int, job_id: str, variant: str):
    import os
    import stat

    logs_fd = _open_child_dir(spawn_fd, "logs")
    try:
        filename = f"{job_id}.log" if variant == "log" else f"{job_id}.agy.log"
        before = os.stat(filename, dir_fd=logs_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular log file")
        file_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=logs_fd)
        after = os.fstat(file_fd)
        if not stat.S_ISREG(after.st_mode):
            os.close(file_fd)
            raise OSError("not a regular log file")
        return file_fd, after
    finally:
        os.close(logs_fd)


def _file_metadata(file_fd: int, size: int) -> tuple[int | None, str | None]:
    import hashlib
    import os

    if size > _HASH_MAX_BYTES:
        return None, None
    digest = hashlib.sha256()
    lines = 0
    offset = 0
    while offset < size:
        chunk = os.pread(file_fd, min(1024 * 1024, size - offset), offset)
        if not chunk:
            return None, None
        digest.update(chunk)
        lines += chunk.count(b"\n")
        offset += len(chunk)
    if size and os.pread(file_fd, 1, size - 1) != b"\n":
        lines += 1
    return lines, digest.hexdigest()


def _class_value(record: dict) -> str:
    value = record.get("data_class")
    return value if value in {"A", "B", "C"} else "unknown"


def _local_sources(session_cwd: str, job_id: str) -> tuple[list[dict] | None, str]:
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    try:
        spawn_fd = _state_fds(session_cwd)
    except OSError:
        return None, "unreadable"
    try:
        try:
            record = _read_record(spawn_fd, job_id)
        except (OSError, UnicodeDecodeError, ValueError):
            return None, "unreadable"
        if record is None:
            return None, "no_record"
        expected = Path(session_cwd).expanduser().resolve() / ".claude" / "state" / "worker-spawn" / "logs" / f"{job_id}.log"
        if record.get("log_path") != str(expected):
            return None, "path_mismatch"
        data_class = _class_value(record)
        result = []
        for variant in ("log", "agy_log"):
            try:
                file_fd, info = _open_log(spawn_fd, job_id, variant)
            except FileNotFoundError:
                continue
            except OSError:
                return None, "unreadable"
            try:
                lines, sha256 = _file_metadata(file_fd, info.st_size)
            finally:
                os.close(file_fd)
            result.append({
                "job_id": job_id,
                "variant": variant,
                "bytes": info.st_size,
                "lines": lines,
                "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                "data_class": data_class,
                "viewable": "text" if data_class in {"B", "C"} else "metadata",
                "sha256": sha256,
                "status": str(record.get("status") or "unknown"),
                "worker": str(record.get("worker") or "unknown"),
            })
        if not result:
            return None, "unreadable"
        return result, "none"
    finally:
        os.close(spawn_fd)


@method("conductor_artifacts.list")
@_profile_scoped
def _conductor_artifacts_list(rid, params):
    session_id = _str_param(params, "session_id")
    _, session = _current_session_steer_authority(session_id)
    if session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    job_id = params.get("job_id")
    run_id = params.get("run_id")
    if (bool(job_id) == bool(run_id)
            or (job_id is not None and (not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id)))
            or (run_id is not None and (not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id)))):
        return _err(rid, 4092, "invalid artifact filter")
    local, reason = (None, "no_record")
    cwd = session.get("cwd")
    if isinstance(job_id, str) and isinstance(cwd, str) and cwd:
        local, reason = _local_sources(cwd, job_id)
    return _ok(rid, {"durable": dict(_DURABLE_UNAVAILABLE), "local": local, "local_reason": reason})


def _read_local_window(session_cwd: str, job_id: str, variant: str, mode: str, offset, length, from_end: bool):
    import base64
    import os
    from pathlib import Path

    try:
        spawn_fd = _state_fds(session_cwd)
    except OSError:
        return None, "missing"
    try:
        try:
            record = _read_record(spawn_fd, job_id)
        except (OSError, UnicodeDecodeError, ValueError):
            return None, "missing"
        if record is None:
            return None, "missing"
        expected = Path(session_cwd).expanduser().resolve() / ".claude" / "state" / "worker-spawn" / "logs" / f"{job_id}.log"
        if record.get("log_path") != str(expected):
            return None, "missing"
        if _class_value(record) not in {"B", "C"}:
            return None, "class_a"
        try:
            file_fd, info = _open_log(spawn_fd, job_id, variant)
        except OSError:
            return None, "missing"
        try:
            total = info.st_size
            requested = _WINDOW_DEFAULT_BYTES if length is None else length
            if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0 or requested > _WINDOW_MAX_BYTES:
                return None, "bad_window"
            if isinstance(offset, bool) or (offset is not None and (not isinstance(offset, int) or offset < 0)):
                return None, "bad_window"
            start = max(0, total - requested) if from_end or offset is None else offset
            start = min(start, total)
            end = min(total, start + requested)
            if mode == "bytes":
                raw = os.pread(file_fd, end - start, start)
                common = {"offset": start, "length": len(raw), "total_bytes": total, "eof": start + len(raw) >= total}
                return {"mode": "bytes", "base64": base64.b64encode(raw).decode("ascii"), **common}, None
            if start > 0 and os.pread(file_fd, 1, start - 1) != b"\n":
                prefix = os.pread(file_fd, end - start, start)
                newline = prefix.find(b"\n")
                if newline >= 0:
                    start += newline + 1
            end = min(total, max(start, end))
            margin = 8 * 1024
            context_start = max(0, start - margin)
            context_end = min(total, end + margin)
            context_raw = os.pread(file_fd, context_end - context_start, context_start)
            text = context_raw.decode("utf-8", errors="replace")
            original_text = text
            from agent.redact import redact_sensitive_text
            import re
            begin = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
            finish = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
            begins = list(begin.finditer(text))
            finishes = list(finish.finditer(text))
            if begins and (not finishes or begins[-1].start() > finishes[-1].start()):
                edge = begins[-1].start()
                text = text[:edge] + "*" * (len(text) - edge)
            elif finishes and (not begins or finishes[0].start() < begins[0].start()):
                edge = finishes[0].end()
                text = "*" * edge + text[edge:]
            scrubbed = redact_sensitive_text(text, force=True)

            from difflib import SequenceMatcher

            matcher = SequenceMatcher(None, text, scrubbed)

            def mapped_index(index, *, right):
                for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
                    if left_start <= index <= left_end:
                        if tag == "equal":
                            return right_start + index - left_start
                        return right_end if right else right_start
                return len(scrubbed)

            cut_start = start - context_start
            cut_end = end - context_start
            elided = False
            if context_start > 0 and b"\n" not in context_raw[:cut_start]:
                next_newline = context_raw.find(b"\n", cut_start, len(context_raw))
                if next_newline >= 0:
                    cut_start = min(cut_end, next_newline + 1)
                else:
                    cut_start = cut_end
                elided = True
            if context_end < total and b"\n" not in context_raw[cut_end:]:
                previous_newline = context_raw.rfind(b"\n", 0, cut_end)
                cut_end = max(cut_start, previous_newline + 1)
                elided = True
            visible_start = context_start + cut_start
            visible_end = context_start + cut_end
            char_start = len(context_raw[:cut_start].decode("utf-8", errors="replace"))
            char_end = len(context_raw[:cut_end].decode("utf-8", errors="replace"))
            visible = scrubbed[mapped_index(char_start, right=False):mapped_index(char_end, right=True)]
            common = {"offset": visible_start, "length": visible_end - visible_start, "total_bytes": total, "eof": end >= total}
            return {"mode": "text", "text": visible, **common, "bof": common["offset"] == 0, "redacted": scrubbed != original_text, "elided": elided}, None
        finally:
            os.close(file_fd)
    finally:
        os.close(spawn_fd)


@method("conductor_artifacts.read")
@_profile_scoped
def _conductor_artifacts_read(rid, params):
    session_id = _str_param(params, "session_id")
    _, session = _current_session_steer_authority(session_id)
    if session is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    source = params.get("source")
    if source == "durable":
        return _err(rid, 4095, "artifact not found")
    job_id = params.get("job_id")
    variant = params.get("variant", "log")
    mode = params.get("mode")
    if (source != "local" or not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id)
            or variant not in {"log", "agy_log"} or mode not in {"text", "bytes"}
            or params.get("artifact_id") is not None):
        return _err(rid, 4092, "invalid local artifact request")
    cwd = session.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return _err(rid, 4095, "artifact not found")
    result, error = _read_local_window(cwd, job_id, variant, mode, params.get("offset"), params.get("length"), params.get("from_end") is True)
    if error == "class_a":
        return _err(rid, 4093, "artifact is metadata only")
    if error == "bad_window":
        return _err(rid, 4092, "invalid byte window")
    if error:
        return _err(rid, 4095, "artifact not found")
    return _ok(rid, result)


def register(server):
    bind_module(globals(), server)
