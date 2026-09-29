"""Bounded incremental reader of conductor marker index and per-build status records."""

from __future__ import annotations

import json
import math
import os
import pwd
import re
import stat
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.secret_hygiene import mask_stored_text
from .methods_conductor_build import _BUILD_ID_RE


_MAX_TAIL_BYTES = 2 * 1024 * 1024  # 2 MiB tail cap
_MAX_STATE_ROOTS = 64


@dataclass(frozen=True)
class IndexScan:
    """Result of an incremental index scan."""

    entries: tuple[dict[str, Any], ...] = ()
    closed: int = 0
    skipped: int = 0
    bytes_read: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))


@dataclass(frozen=True)
class StatusRead:
    """Result of reading a conductor per-build status record."""

    present: bool = False
    valid: bool = False
    reason: str = "missing"
    fresh: bool = False
    stale: bool = False
    status_at: float | None = None
    seq: int | None = None
    record: dict[str, Any] | None = None
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.record is not None and self.data is None:
            object.__setattr__(self, "data", self.record)
        elif self.data is not None and self.record is None:
            object.__setattr__(self, "record", self.data)


@dataclass
class _FoldedEntry:
    order: int
    is_closed: bool
    row: dict[str, Any]


@dataclass
class _FileCacheState:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    last_offset: int
    seq_order: int
    skipped: int
    folded: dict[str, _FoldedEntry]
    last_scan: IndexScan


_CACHE: dict[tuple[str, str], _FileCacheState] = {}
_STATUS_HIGH_WATER: dict[tuple[str, str], tuple[int, StatusRead]] = {}


def _clear_cache() -> None:
    """Clear in-memory file cache and status high-water cache (testing hook)."""
    _CACHE.clear()
    _STATUS_HIGH_WATER.clear()


def _clear_status_cache() -> None:
    """Clear in-memory status high-water cache (testing hook)."""
    _STATUS_HIGH_WATER.clear()



def _get_passwd_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _validate_marker_path(marker_path: Any, home: Path) -> bool:
    """Path rule (§6): absolute, == realpath, valid suffix, under passwd home, not under temp."""
    if not isinstance(marker_path, str) or not marker_path:
        return False
    if not os.path.isabs(marker_path):
        return False
    try:
        real_marker = os.path.realpath(marker_path)
    except OSError:
        return False
    if real_marker != marker_path:
        return False

    # Suffix check: /.claude/state/tb-build-active.json or /.claude/state/builds/<id>/tb-build-active.json
    p = Path(marker_path)
    parts = p.parts
    if len(parts) >= 4 and parts[-3:] == (".claude", "state", "tb-build-active.json"):
        pass
    elif len(parts) >= 6 and parts[-5:-2] == (".claude", "state", "builds") and parts[-1] == "tb-build-active.json":
        build_id = parts[-2]
        if not _BUILD_ID_RE.fullmatch(build_id) or build_id in (".", ".."):
            return False
    else:
        return False

    # Must lie under home
    try:
        home_real = Path(os.path.realpath(home))
        home_raw = Path(home)
    except OSError:
        return False

    if not (p.is_relative_to(home_real) or p.is_relative_to(home_raw)):
        return False
    if p == home_real or p == home_raw:
        return False

    # Must NOT lie under tempdir realpath nor /private/tmp / /tmp
    temp_roots: list[Path] = []
    try:
        td = tempfile.gettempdir()
        temp_roots.append(Path(td))
        temp_roots.append(Path(os.path.realpath(td)))
    except OSError:
        pass
    for path_str in ("/private/tmp", "/tmp"):
        temp_roots.append(Path(path_str))
        try:
            temp_roots.append(Path(os.path.realpath(path_str)))
        except OSError:
            pass

    for temp_root in temp_roots:
        try:
            if p.is_relative_to(temp_root):
                return False
        except ValueError:
            pass

    return True


def _read_all(fd: int, to_read: int) -> bytes:
    chunks: list[bytes] = []
    remaining = to_read
    while remaining > 0:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_marker_index(
    path: Path | None = None,
    *,
    home: Path | None = None,
) -> IndexScan:
    """Bounded, incremental reader of the conductor marker index (~/.claude/state/tb-marker-index.jsonl)."""
    effective_home = Path(home) if home is not None else _get_passwd_home()
    target_path = Path(path) if path is not None else effective_home / ".claude" / "state" / "tb-marker-index.jsonl"

    try:
        st = os.lstat(target_path)
    except (FileNotFoundError, OSError):
        return IndexScan()

    # Symlinks for the index file itself are strictly refused (O_NOFOLLOW)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return IndexScan()

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target_path, flags)
    except OSError:
        return IndexScan()

    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return IndexScan()

        cache_key = (os.path.realpath(str(target_path)), os.path.realpath(str(effective_home)))
        cached = _CACHE.get(cache_key)

        # Unchanged file: 0 bytes read, reuse last scan
        if (
            cached is not None
            and cached.dev == fst.st_dev
            and cached.ino == fst.st_ino
            and fst.st_size == cached.size
            and fst.st_mtime_ns == cached.mtime_ns
        ):
            return IndexScan(
                entries=cached.last_scan.entries,
                closed=cached.last_scan.closed,
                skipped=cached.last_scan.skipped,
                bytes_read=0,
            )

        # Check if cache is valid for incremental read
        incremental = (
            cached is not None
            and cached.dev == fst.st_dev
            and cached.ino == fst.st_ino
            and fst.st_size >= cached.last_offset
            and (fst.st_size - cached.last_offset) <= _MAX_TAIL_BYTES
        )

        if incremental and cached is not None:
            seek_offset = cached.last_offset
            is_tail_cut = False
            skipped = cached.skipped
            seq_order = cached.seq_order
            folded = dict(cached.folded)
        else:
            # Full read (first read, inode changed, file shrank, or appended > 2 MiB)
            if fst.st_size > _MAX_TAIL_BYTES:
                seek_offset = fst.st_size - _MAX_TAIL_BYTES
                is_tail_cut = True
            else:
                seek_offset = 0
                is_tail_cut = False
            skipped = 0
            seq_order = 0
            folded = {}

        if fst.st_size == 0:
            scan = IndexScan(entries=(), closed=0, skipped=0, bytes_read=0)
            _CACHE[cache_key] = _FileCacheState(
                dev=fst.st_dev,
                ino=fst.st_ino,
                size=0,
                mtime_ns=fst.st_mtime_ns,
                last_offset=0,
                seq_order=0,
                skipped=0,
                folded={},
                last_scan=scan,
            )
            return scan

        if seek_offset > 0:
            os.lseek(fd, seek_offset, os.SEEK_SET)

        to_read = fst.st_size - seek_offset
        if to_read == 0:
            return IndexScan(
                entries=cached.last_scan.entries if cached else (),
                closed=cached.last_scan.closed if cached else 0,
                skipped=cached.last_scan.skipped if cached else 0,
                bytes_read=0,
            )

        data = _read_all(fd, to_read)
        bytes_read = len(data)

        if is_tail_cut:
            first_nl = data.find(b"\n")
            if first_nl == -1:
                # No newline in the tail: no complete lines
                scan = IndexScan(entries=(), closed=0, skipped=0, bytes_read=bytes_read)
                _CACHE[cache_key] = _FileCacheState(
                    dev=fst.st_dev,
                    ino=fst.st_ino,
                    size=fst.st_size,
                    mtime_ns=fst.st_mtime_ns,
                    last_offset=seek_offset,
                    seq_order=0,
                    skipped=0,
                    folded={},
                    last_scan=scan,
                )
                return scan
            data = data[first_nl + 1:]
            base_offset = seek_offset + first_nl + 1
        else:
            base_offset = seek_offset

        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            # Trailing partial line without newline: ignore until complete
            entries = cached.last_scan.entries if (incremental and cached) else ()
            closed_count = cached.last_scan.closed if (incremental and cached) else 0
            scan = IndexScan(
                entries=entries,
                closed=closed_count,
                skipped=skipped,
                bytes_read=bytes_read,
            )
            _CACHE[cache_key] = _FileCacheState(
                dev=fst.st_dev,
                ino=fst.st_ino,
                size=fst.st_size,
                mtime_ns=fst.st_mtime_ns,
                last_offset=base_offset,
                seq_order=seq_order,
                skipped=skipped,
                folded=folded,
                last_scan=scan,
            )
            return scan

        complete_data = data[:last_nl]
        new_last_offset = base_offset + last_nl + 1

        for line_bytes in complete_data.split(b"\n"):
            line = line_bytes.strip()
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except Exception:
                skipped += 1
                continue

            if not isinstance(row, dict):
                skipped += 1
                continue

            marker_path = row.get("marker_path")
            if not _validate_marker_path(marker_path, effective_home):
                skipped += 1
                continue

            seq_order += 1
            if row.get("closed") is True:
                folded[marker_path] = _FoldedEntry(order=seq_order, is_closed=True, row=row)
            else:
                folded[marker_path] = _FoldedEntry(order=seq_order, is_closed=False, row=row)

        closed_count = sum(1 for e in folded.values() if e.is_closed)
        open_entries = [e for e in folded.values() if not e.is_closed]
        open_entries.sort(key=lambda e: e.order, reverse=True)

        seen_roots: set[str] = set()
        entries_list: list[dict[str, Any]] = []
        for e in open_entries:
            sr = e.row.get("state_root")
            if sr not in seen_roots:
                if len(seen_roots) >= _MAX_STATE_ROOTS:
                    continue
                seen_roots.add(sr)
            entries_list.append(e.row)

        scan = IndexScan(
            entries=tuple(entries_list),
            closed=closed_count,
            skipped=skipped,
            bytes_read=bytes_read,
        )

        _CACHE[cache_key] = _FileCacheState(
            dev=fst.st_dev,
            ino=fst.st_ino,
            size=fst.st_size,
            mtime_ns=fst.st_mtime_ns,
            last_offset=new_last_offset,
            seq_order=seq_order,
            skipped=skipped,
            folded=folded,
            last_scan=scan,
        )
        return scan
    finally:
        os.close(fd)


_STATUS_MAX_BYTES = 64 * 1024  # 64 KiB
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ABS_PATH_RE = re.compile(
    r"(?:^|(?<=\s))(?:/(?:[^\s/]+/)+[^\s/]*|/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]*)+|~/(?:[^\s/]+/)*[^\s/]*|[A-Za-z]:[/\\][^\s]*|\\\\[^\s]+)(?=\s|$)"
)


def _clean_str(val: Any, max_len: int = 120) -> str | None:
    if not isinstance(val, str):
        return None
    s = _ANSI_RE.sub("", val)
    s = "".join(c for c in s if ord(c) >= 32 and ord(c) != 127 and unicodedata.category(c) != "Cc")
    s = s.strip()
    return s[:max_len] if s else None


def _clean_free_text(val: Any, max_len: int) -> str | None:
    if not isinstance(val, str):
        return None
    masked, _ = mask_stored_text(val)
    if not isinstance(masked, str):
        return None
    s = _ANSI_RE.sub("", masked)
    s = "".join(c for c in s if ord(c) >= 32 and ord(c) != 127 and unicodedata.category(c) != "Cc")
    stripped = s.strip()
    if stripped.startswith(("/", "~", "\\\\")) or re.match(r"^[A-Za-z]:[/\\]", stripped):
        return None
    s = _ABS_PATH_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return s[:max_len]


def _clean_int(val: Any, min_val: int = 0) -> int | None:
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    return val if val >= min_val else None


def _clean_float(val: Any, min_val: float = 0.0) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    if not math.isfinite(f) or f < min_val:
        return None
    return f


def _coerce_record(raw: dict[str, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {
        "schema": "tb-build-status/v1",
        "seq": _clean_int(raw.get("seq")),
        "written_at": _clean_str(raw.get("written_at"), 64),
        "conductor_version": _clean_str(raw.get("conductor_version"), 64),
    }

    # build
    raw_build = raw.get("build") if isinstance(raw.get("build"), dict) else {}
    plan_title = _clean_free_text(raw_build.get("plan_title"), 120)
    raw_branch = raw_build.get("branch")
    branch = None
    if isinstance(raw_branch, str):
        stripped_branch = raw_branch.strip()
        if not (stripped_branch.startswith(("/", "~", "\\\\")) or re.match(r"^[A-Za-z]:[/\\]", stripped_branch)):
            branch = _clean_str(stripped_branch, 120)

    coerced["build"] = {
        "run_id": str(raw_build.get("run_id") or ""),
        "session_id": str(raw_build.get("session_id") or ""),
        "build_id": _clean_str(raw_build.get("build_id"), 64),
        "hermes_session_id": _clean_str(raw_build.get("hermes_session_id"), 64),
        "binding_nonce": _clean_str(raw_build.get("binding_nonce"), 64),
        "plan_title": plan_title,
        "branch": branch,
        "armed_at": _clean_str(raw_build.get("armed_at"), 64),
    }

    # phase
    valid_phases = {"plan", "build", "review", "land", "waiting", "blocked", "closing", "done"}
    raw_phase = raw.get("phase")
    if isinstance(raw_phase, str):
        coerced["phase"] = raw_phase if raw_phase in valid_phases else "other"
    else:
        coerced["phase"] = None

    # progress
    raw_progress = raw.get("progress")
    if isinstance(raw_progress, dict):
        prog: dict[str, Any] = {}
        # waves
        raw_waves = raw_progress.get("waves")
        if isinstance(raw_waves, dict):
            done = _clean_int(raw_waves.get("done"), 0)
            total = _clean_int(raw_waves.get("total"), 1)
            prog["waves"] = {"done": done, "total": total} if (done is not None or total is not None) else None
        else:
            prog["waves"] = None

        # units
        raw_units = raw_progress.get("units")
        if isinstance(raw_units, dict):
            prog["units"] = {
                "done": _clean_int(raw_units.get("done"), 0),
                "running": _clean_int(raw_units.get("running"), 0),
                "failed": _clean_int(raw_units.get("failed"), 0),
                "remaining": _clean_int(raw_units.get("remaining"), 0),
                "total": _clean_int(raw_units.get("total"), 0),
            }
        else:
            prog["units"] = None

        # current_wave
        raw_cw = raw_progress.get("current_wave")
        if isinstance(raw_cw, dict):
            prog["current_wave"] = {
                "index": _clean_int(raw_cw.get("index"), 1),
                "id": _clean_str(raw_cw.get("id"), 32),
                "title": _clean_free_text(raw_cw.get("title"), 80),
            }
        else:
            prog["current_wave"] = None

        # current_units (cap <= 8)
        raw_cu = raw_progress.get("current_units")
        if isinstance(raw_cu, list):
            units_list = []
            for item in raw_cu:
                if isinstance(item, dict):
                    units_list.append({
                        "id": _clean_str(item.get("id"), 32),
                        "title": _clean_free_text(item.get("title"), 80),
                        "seat": _clean_str(item.get("seat"), 32),
                        "since": _clean_str(item.get("since"), 64),
                    })
            prog["current_units"] = units_list[:8]
        else:
            prog["current_units"] = []

        # remaining_waves (cap <= 12)
        raw_rw = raw_progress.get("remaining_waves")
        if isinstance(raw_rw, list):
            rw_list = []
            for item in raw_rw:
                if isinstance(item, dict):
                    rw_list.append({
                        "index": _clean_int(item.get("index"), 1),
                        "id": _clean_str(item.get("id"), 32),
                        "title": _clean_free_text(item.get("title"), 80),
                        "units": _clean_int(item.get("units"), 0),
                    })
            prog["remaining_waves"] = rw_list[:12]
        else:
            prog["remaining_waves"] = []

        coerced["progress"] = prog
    else:
        coerced["progress"] = None

    # estimate
    raw_est = raw.get("estimate")
    if isinstance(raw_est, dict):
        p50 = _clean_float(raw_est.get("p50"), 0.0)
        p90 = _clean_float(raw_est.get("p90"), 0.0)
        if p50 is not None and p90 is not None and p50 <= p90:
            basis_str = raw_est.get("basis")
            basis = basis_str if basis_str in {"predict", "reforecast", "manual"} else "other"
            coerced["estimate"] = {
                "unit": "work_hours",
                "p50": p50,
                "p90": p90,
                "basis": basis,
                "as_of": _clean_str(raw_est.get("as_of"), 64),
                "prediction_id": _clean_str(raw_est.get("prediction_id"), 64),
            }
        else:
            coerced["estimate"] = None
    else:
        coerced["estimate"] = None

    # gates (cap <= 16)
    valid_gate_kinds = {"ci", "owner", "relaunch", "review", "external", "date"}
    valid_gate_states = {"waiting", "passed", "failed", "cancelled"}
    raw_gates = raw.get("gates")
    if isinstance(raw_gates, list):
        gates_list = []
        for g in raw_gates:
            if isinstance(g, dict):
                k = g.get("kind")
                st_val = g.get("state")
                waiter = g.get("waiter") if isinstance(g.get("waiter"), bool) else None
                gates_list.append({
                    "id": _clean_str(g.get("id"), 64),
                    "kind": k if k in valid_gate_kinds else "other",
                    "label": _clean_free_text(g.get("label"), 120),
                    "since": _clean_str(g.get("since"), 64),
                    "due": _clean_str(g.get("due"), 64),
                    "ref": _clean_str(g.get("ref"), 64),
                    "state": st_val if st_val in valid_gate_states else "other",
                    "waiter": waiter,
                })
        coerced["gates"] = gates_list[:16]
    else:
        coerced["gates"] = []

    # seats + refused_min (§15)
    raw_seats = raw.get("seats")
    if isinstance(raw_seats, dict):
        seats_map = {}
        for s_name, s_obj in raw_seats.items():
            if isinstance(s_obj, dict):
                refused = _clean_int(s_obj.get("refused"), 0)
                seats_map[str(s_name)] = {
                    "spawned": _clean_int(s_obj.get("spawned"), 0),
                    "running": _clean_int(s_obj.get("running"), 0),
                    "succeeded": _clean_int(s_obj.get("succeeded"), 0),
                    "failed": _clean_int(s_obj.get("failed"), 0),
                    "refused": refused,
                    "refused_min": refused,
                }
        coerced["seats"] = seats_map
    else:
        coerced["seats"] = {}

    # other_names (cap <= 8)
    raw_other_names = raw.get("other_names")
    if isinstance(raw_other_names, list):
        on_list = [_clean_str(x, 64) for x in raw_other_names if _clean_str(x, 64)]
        coerced["other_names"] = on_list[:8]
    else:
        coerced["other_names"] = []

    # refusals (cap <= 10)
    raw_refusals = raw.get("refusals")
    if isinstance(raw_refusals, list):
        ref_list = []
        for r in raw_refusals:
            if isinstance(r, dict):
                ref_list.append({
                    "seat": _clean_str(r.get("seat"), 32),
                    "code": _clean_str(r.get("code"), 64),
                    "count": _clean_int(r.get("count"), 0),
                    "last_at": _clean_str(r.get("last_at"), 64),
                    "note": _clean_free_text(r.get("note"), 120),
                })
        coerced["refusals"] = ref_list[:10]
    else:
        coerced["refusals"] = []

    # lanes (job_ids <= 12)
    raw_lanes = raw.get("lanes")
    if isinstance(raw_lanes, dict):
        raw_jids = raw_lanes.get("job_ids")
        if isinstance(raw_jids, list):
            jids = [_clean_str(j, 64) for j in raw_jids if _clean_str(j, 64)][:12]
        else:
            jids = []
        coerced["lanes"] = {
            "running": _clean_int(raw_lanes.get("running"), 0),
            "stale": _clean_int(raw_lanes.get("stale"), 0),
            "cap": _clean_int(raw_lanes.get("cap"), 0),
            "job_ids": jids,
        }
    else:
        coerced["lanes"] = None

    # ci (cap <= 6)
    valid_ci_states = {"pending", "success", "failure", "cancelled", "unknown"}
    valid_ci_kinds = {"run", "pr"}
    raw_ci = raw.get("ci")
    if isinstance(raw_ci, list):
        ci_list = []
        for c in raw_ci:
            if isinstance(c, dict):
                raw_url = c.get("url")
                url = None
                if isinstance(raw_url, str) and raw_url.startswith("https://github.com/"):
                    url = _clean_str(raw_url, 500)
                raw_cbranch = c.get("branch")
                cbranch = None
                if isinstance(raw_cbranch, str):
                    stripped_cb = raw_cbranch.strip()
                    if not (stripped_cb.startswith(("/", "~", "\\\\")) or re.match(r"^[A-Za-z]:[/\\]", stripped_cb)):
                        cbranch = _clean_str(stripped_cb, 120)
                c_kind = c.get("kind")
                c_state = c.get("state")
                ci_list.append({
                    "kind": c_kind if c_kind in valid_ci_kinds else "other",
                    "ref": _clean_str(c.get("ref"), 64),
                    "branch": cbranch,
                    "pr": c.get("pr") if isinstance(c.get("pr"), (int, str)) else None,
                    "state": c_state if c_state in valid_ci_states else "other",
                    "url": url,
                    "checked_at": _clean_str(c.get("checked_at"), 64),
                })
        coerced["ci"] = ci_list[:6]
    else:
        coerced["ci"] = []

    # owner_blockers (cap <= 8)
    valid_ob_actions = {"answer", "approve", "decide", "relaunch", "do"}
    raw_ob = raw.get("owner_blockers")
    if isinstance(raw_ob, list):
        ob_list = []
        for b in raw_ob:
            if isinstance(b, dict):
                act = b.get("action")
                ob_list.append({
                    "id": _clean_str(b.get("id"), 64),
                    "action": act if act in valid_ob_actions else "other",
                    "label": _clean_free_text(b.get("label"), 120),
                    "since": _clean_str(b.get("since"), 64),
                    "ref": _clean_str(b.get("ref"), 64),
                })
        coerced["owner_blockers"] = ob_list[:8]
    else:
        coerced["owner_blockers"] = []

    # last_activity
    raw_la = raw.get("last_activity")
    if isinstance(raw_la, dict):
        coerced["last_activity"] = {
            "at": _clean_str(raw_la.get("at"), 64),
            "what": _clean_free_text(raw_la.get("what"), 80),
            "verb": _clean_str(raw_la.get("verb"), 64),
        }
    else:
        coerced["last_activity"] = None

    return coerced


def read_build_status(
    marker: dict[str, Any],
    marker_path: Path | str,
    *,
    now: float | None = None,
) -> StatusRead:
    """Read conductor per-build status record (tb-build-status/v1) beside the marker."""
    p = Path(marker_path)
    status_path = p.parent / "tb-build-status.json"

    try:
        st = os.lstat(status_path)
    except FileNotFoundError:
        return StatusRead(present=False, valid=False, reason="missing")
    except OSError:
        return StatusRead(present=True, valid=False, reason="unreadable")

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > _STATUS_MAX_BYTES:
        return StatusRead(present=True, valid=False, reason="unreadable")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(status_path, flags)
    except OSError:
        return StatusRead(present=True, valid=False, reason="unreadable")

    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_size > _STATUS_MAX_BYTES:
            return StatusRead(present=True, valid=False, reason="unreadable")

        chunks: list[bytes] = []
        remaining = _STATUS_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _STATUS_MAX_BYTES:
            return StatusRead(present=True, valid=False, reason="unreadable")

        file_mtime = fst.st_mtime
    except OSError:
        return StatusRead(present=True, valid=False, reason="unreadable")
    finally:
        os.close(fd)

    try:
        raw_data = json.loads(content.decode("utf-8"))
    except Exception:
        return StatusRead(present=True, valid=False, reason="unreadable")

    if not isinstance(raw_data, dict):
        return StatusRead(present=True, valid=False, reason="unreadable")

    # Schema check
    if raw_data.get("schema") != "tb-build-status/v1":
        return StatusRead(present=True, valid=False, reason="unknown_schema")

    # Join rule (§15)
    expected_run_id = marker.get("build_run_id") or marker.get("run_id")
    expected_session_id = marker.get("session_id")
    raw_build = raw_data.get("build")
    if not isinstance(raw_build, dict):
        return StatusRead(present=True, valid=False, reason="mismatched")

    record_run_id = raw_build.get("run_id")
    record_session_id = raw_build.get("session_id")

    if (
        not expected_run_id
        or not expected_session_id
        or str(record_run_id) != str(expected_run_id)
        or str(record_session_id) != str(expected_session_id)
    ):
        return StatusRead(present=True, valid=False, reason="mismatched")

    # seq monotonic high-water check
    seq_val = raw_data.get("seq")
    seq = seq_val if isinstance(seq_val, int) and not isinstance(seq_val, bool) and seq_val >= 0 else None
    hw_key = (str(p), str(expected_run_id))
    now_ts = time.time() if now is None else float(now)

    if hw_key in _STATUS_HIGH_WATER:
        prev_seq, last_good_read = _STATUS_HIGH_WATER[hw_key]
        if seq is not None and prev_seq is not None and seq < prev_seq:
            # Lower seq ignored; keep last good read (update freshness)
            is_fresh = (now_ts - last_good_read.status_at) <= 1800.0 if last_good_read.status_at is not None else False
            return StatusRead(
                present=last_good_read.present,
                valid=last_good_read.valid,
                reason=last_good_read.reason,
                fresh=is_fresh,
                stale=not is_fresh,
                status_at=last_good_read.status_at,
                seq=last_good_read.seq,
                record=last_good_read.record,
                data=last_good_read.data,
            )

    # Freshness and clock
    written_at_str = raw_data.get("written_at")
    written_at_ts: float | None = None
    if isinstance(written_at_str, str):
        try:
            written_at_ts = datetime.fromisoformat(written_at_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, OSError):
            written_at_ts = None

    mtime_cap = file_mtime + 300.0  # file mtime + 5 min
    if written_at_ts is not None:
        status_at = min(written_at_ts, mtime_cap)
    else:
        status_at = min(file_mtime, mtime_cap)

    # Clamp future timestamps to now
    if status_at > now_ts:
        status_at = now_ts

    fresh = (now_ts - status_at) <= 1800.0  # 30 min
    stale = not fresh

    coerced = _coerce_record(raw_data)
    result_read = StatusRead(
        present=True,
        valid=True,
        reason="ok",
        fresh=fresh,
        stale=stale,
        status_at=status_at,
        seq=seq,
        record=coerced,
        data=coerced,
    )

    _STATUS_HIGH_WATER[hw_key] = (seq if seq is not None else 0, result_read)
    return result_read


@dataclass
class DerivedStatus:
    """Status fields filled from status record when present/fresh, else derived fallbacks."""

    phase: str | None = None
    progress: dict[str, Any] | None = None
    estimate: dict[str, Any] | None = None
    gates: list[dict[str, Any]] = field(default_factory=list)
    seats: dict[str, Any] = field(default_factory=dict)
    other_names: list[str] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    lanes: dict[str, Any] | None = None
    ci: list[dict[str, Any]] = field(default_factory=list)
    owner_blockers: list[dict[str, Any]] = field(default_factory=list)
    last_activity: dict[str, Any] | None = None
    last_activity_at: float | None = None
    provenance: dict[str, str] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    stale: bool = False
    status: StatusRead | None = None
    record: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.provenance and not self.field_sources:
            object.__setattr__(self, "field_sources", dict(self.provenance))
        elif self.field_sources and not self.provenance:
            object.__setattr__(self, "provenance", dict(self.field_sources))
        if self.record is not None and self.status is None:
            object.__setattr__(self, "status", StatusRead(present=True, valid=True, record=self.record))

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    @property
    def waves(self) -> dict[str, Any] | None:
        if isinstance(self.progress, dict):
            return self.progress.get("waves")
        return None

    @property
    def blocked(self) -> bool:
        return bool(self.owner_blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "progress": self.progress,
            "estimate": self.estimate,
            "gates": self.gates,
            "seats": self.seats,
            "other_names": self.other_names,
            "refusals": self.refusals,
            "lanes": self.lanes,
            "ci": self.ci,
            "owner_blockers": self.owner_blockers,
            "last_activity": self.last_activity,
            "last_activity_at": self.last_activity_at,
            "provenance": dict(self.provenance),
            "field_sources": dict(self.field_sources),
            "stale": self.stale,
        }


def derive_row_status(
    marker: dict[str, Any],
    status: StatusRead | None,
    jobs: list[dict[str, Any]],
    now: float,
) -> DerivedStatus:
    """Fill each status field from status record when present and fresh, else fallback."""
    now_ts = float(now)

    status_record: dict[str, Any] | None = None
    is_stale = False
    has_status = False

    if isinstance(status, StatusRead):
        if status.valid and isinstance(status.record, dict):
            status_record = status.record
            is_stale = status.stale or not status.fresh
            has_status = True
    elif isinstance(status, dict):
        if "record" in status and isinstance(status.get("record"), dict):
            if status.get("valid", True):
                status_record = status["record"]
                is_stale = bool(status.get("stale", False) or not status.get("fresh", True))
                has_status = True
        elif status.get("schema") == "tb-build-status/v1":
            status_record = status
            is_stale = False
            has_status = True

    # Filter jobs by build_run_id match (K3: job.build_run_id == marker.build_run_id or marker.run_id)
    target_run_id = marker.get("build_run_id") or marker.get("run_id")
    target_run_id_str = str(target_run_id) if target_run_id is not None and str(target_run_id) else None

    matched_jobs: list[dict[str, Any]] = []
    if target_run_id_str is not None:
        for job in jobs:
            if isinstance(job, dict):
                job_brid = job.get("build_run_id")
                if job_brid is not None and str(job_brid) == target_run_id_str:
                    matched_jobs.append(job)

    # 1. Gates fallback from marker waits[]
    fallback_gates: list[dict[str, Any]] = []
    marker_waits = marker.get("waits")
    if isinstance(marker_waits, list):
        for idx, w in enumerate(marker_waits):
            if isinstance(w, dict):
                k = w.get("kind")
                kind_val = k if k in {"ci", "owner", "relaunch", "review", "external", "date"} else "other"
                lbl = _clean_free_text(w.get("label") or w.get("what"), 120)
                st_val = w.get("state")
                state_val = st_val if st_val in {"waiting", "passed", "failed", "cancelled"} else "waiting"
                waiter_val = w.get("waiter") if isinstance(w.get("waiter"), bool) else None
                fallback_gates.append({
                    "id": _clean_str(w.get("id"), 64) or f"g-{idx + 1}",
                    "kind": kind_val,
                    "label": lbl,
                    "since": _clean_str(w.get("since") or w.get("at"), 64),
                    "due": _clean_str(w.get("due"), 64),
                    "ref": _clean_str(w.get("ref") or w.get("ci_ref"), 64),
                    "state": state_val,
                    "waiter": waiter_val,
                })
        fallback_gates = fallback_gates[:16]
    elif marker.get("waiting_on") and marker.get("blocked") is not True:
        lbl = _clean_free_text(marker.get("waiting_on"), 120)
        fallback_gates.append({
            "id": "g-1",
            "kind": "other",
            "label": lbl,
            "since": _clean_str(marker.get("wait_since"), 64),
            "due": None,
            "ref": None,
            "state": "waiting",
            "waiter": None,
        })

    # 2. Seats and lanes fallback from matched relay jobs
    standard_seats = ("agy", "muse", "codex", "sonnet", "opus", "other")
    fallback_seats: dict[str, dict[str, int]] = {
        s: {
            "spawned": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "refused": 0,
            "refused_min": 0,
        }
        for s in standard_seats
    }
    fallback_other_names: list[str] = []
    seen_other_names: set[str] = set()

    for job in matched_jobs:
        raw_seat = job.get("served_seat") or job.get("worker")
        if isinstance(raw_seat, str) and raw_seat:
            clean_s = raw_seat.lower().strip()
            if clean_s == "gemini":
                seat_key = "agy"
            elif clean_s in standard_seats:
                seat_key = clean_s
            else:
                seat_key = "other"
                if clean_s not in seen_other_names and len(fallback_other_names) < 8:
                    fallback_other_names.append(clean_s)
                    seen_other_names.add(clean_s)
        else:
            seat_key = "other"

        fallback_seats[seat_key]["spawned"] += 1
        st = job.get("status")
        if st == "running":
            fallback_seats[seat_key]["running"] += 1
        elif st == "succeeded":
            fallback_seats[seat_key]["succeeded"] += 1
        elif st == "failed":
            fallback_seats[seat_key]["failed"] += 1

    running_lanes = sum(1 for j in matched_jobs if j.get("status") == "running")
    stale_lanes = sum(1 for j in matched_jobs if j.get("status") == "stale")
    job_ids = [_clean_str(j.get("job_id"), 64) for j in matched_jobs if j.get("job_id")][:12]
    fallback_lanes = {
        "running": running_lanes,
        "stale": stale_lanes,
        "cap": None,
        "job_ids": [jid for jid in job_ids if jid is not None],
    }

    # 3. Owner blockers fallback from marker blocked / blocked_reason
    fallback_owner_blockers: list[dict[str, Any]] = []
    is_blocked = marker.get("blocked") is True or bool(marker.get("blocked_reason"))
    if is_blocked:
        raw_label = marker.get("blocked_reason") or marker.get("waiting_on") or "Blocked"
        lbl = _clean_free_text(raw_label, 120) or "Blocked"
        fallback_owner_blockers.append({
            "id": _clean_str(marker.get("blocked_id"), 64) or "ob-1",
            "action": "do",
            "label": lbl,
            "since": _clean_str(marker.get("blocked_at") or marker.get("wait_since") or marker.get("armed_at"), 64),
            "ref": None,
        })

    # 4. Progress fallback from marker waves_total / waves_done
    total_val = marker.get("waves_total")
    try:
        waves_done_val = max(0, int(marker.get("waves_done", 0)))
    except (TypeError, ValueError):
        waves_done_val = 0

    valid_tot = isinstance(total_val, (int, float)) and not isinstance(total_val, bool) and total_val > 0
    if isinstance(total_val, float):
        valid_tot = valid_tot and math.isfinite(total_val) and total_val.is_integer()

    if valid_tot:
        waves_tot_int = int(total_val)
        current_w_idx = min(waves_done_val + 1, waves_tot_int)
    else:
        waves_tot_int = None
        current_w_idx = None

    stage_title = _clean_free_text(marker.get("wave_stage"), 80)
    current_u_list = [{"id": None, "title": stage_title, "seat": None, "since": None}] if stage_title else []

    fallback_progress = {
        "waves": {"done": waves_done_val, "total": waves_tot_int} if valid_tot else None,
        "units": None,
        "current_wave": {
            "index": current_w_idx,
            "id": f"W{current_w_idx}" if current_w_idx is not None else None,
            "title": None,
        } if current_w_idx is not None else None,
        "current_units": current_u_list,
        "remaining_waves": [],
    }

    # 5. Last activity fallback: max of marker mtime, newest job update, status written_at
    timestamps: list[float] = []

    m_mtime = marker.get("mtime") if isinstance(marker.get("mtime"), (int, float)) else marker.get("_mtime")
    if isinstance(m_mtime, (int, float)) and math.isfinite(m_mtime):
        timestamps.append(float(m_mtime))
    else:
        armed_str = marker.get("armed_at")
        if isinstance(armed_str, str):
            try:
                timestamps.append(datetime.fromisoformat(armed_str.replace("Z", "+00:00")).timestamp())
            except (ValueError, OSError):
                pass

    for j in matched_jobs:
        hb_ep = j.get("heartbeat_epoch")
        if isinstance(hb_ep, (int, float)) and math.isfinite(hb_ep):
            timestamps.append(float(hb_ep))
        else:
            time_str = j.get("heartbeat_at") or j.get("spawned_at")
            if isinstance(time_str, str):
                try:
                    timestamps.append(datetime.fromisoformat(time_str.replace("Z", "+00:00")).timestamp())
                except (ValueError, OSError):
                    pass

    if status_record is not None:
        w_at_str = status_record.get("written_at")
        if isinstance(w_at_str, str):
            try:
                timestamps.append(datetime.fromisoformat(w_at_str.replace("Z", "+00:00")).timestamp())
            except (ValueError, OSError):
                pass
        la_dict = status_record.get("last_activity")
        if isinstance(la_dict, dict) and isinstance(la_dict.get("at"), str):
            try:
                timestamps.append(datetime.fromisoformat(la_dict["at"].replace("Z", "+00:00")).timestamp())
            except (ValueError, OSError):
                pass
    if isinstance(status, StatusRead) and status.status_at is not None and math.isfinite(status.status_at):
        timestamps.append(float(status.status_at))

    fallback_last_activity: dict[str, Any] | None = None
    fallback_last_activity_at: float | None = None
    if timestamps:
        newest_ts = max(timestamps)
        if newest_ts > now_ts:
            newest_ts = now_ts
        fallback_last_activity_at = newest_ts
        iso_str = datetime.fromtimestamp(newest_ts, timezone.utc).isoformat().replace("+00:00", "Z")
        fallback_last_activity = {"at": iso_str, "what": None, "verb": None}

    # 6. Phase fallback
    if is_blocked:
        fallback_phase = "blocked"
    elif marker.get("waiting_on"):
        fallback_phase = "waiting"
    elif marker.get("wave_stage"):
        fallback_phase = str(marker["wave_stage"])
    else:
        fallback_phase = "build"

    # Field resolution
    provenance: dict[str, str] = {}

    def _resolve(field_name: str, fallback_val: Any) -> Any:
        if has_status and status_record is not None:
            val = status_record.get(field_name)
            if val is not None:
                provenance[field_name] = "status"
                return val
            if is_stale and val is not None:
                provenance[field_name] = "status"
                return val
        provenance[field_name] = "derived"
        return fallback_val

    phase = _resolve("phase", fallback_phase)
    progress = _resolve("progress", fallback_progress)
    estimate = _resolve("estimate", None)
    gates = _resolve("gates", fallback_gates)
    seats = _resolve("seats", fallback_seats)
    other_names = _resolve("other_names", fallback_other_names)
    refusals = _resolve("refusals", [])
    lanes = _resolve("lanes", fallback_lanes)
    ci = _resolve("ci", [])
    owner_blockers = _resolve("owner_blockers", fallback_owner_blockers)
    last_activity = _resolve("last_activity", fallback_last_activity)

    provenance["waves"] = provenance.get("progress", "derived")

    last_act_at: float | None = None
    if last_activity and isinstance(last_activity, dict) and isinstance(last_activity.get("at"), str):
        try:
            last_act_at = datetime.fromisoformat(last_activity["at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, OSError):
            last_act_at = fallback_last_activity_at
    else:
        last_act_at = fallback_last_activity_at

    return DerivedStatus(
        phase=phase,
        progress=progress,
        estimate=estimate,
        gates=gates,
        seats=seats,
        other_names=other_names,
        refusals=refusals,
        lanes=lanes,
        ci=ci,
        owner_blockers=owner_blockers,
        last_activity=last_activity,
        last_activity_at=last_act_at,
        provenance=provenance,
        field_sources=dict(provenance),
        stale=bool(is_stale and has_status),
        status=status if isinstance(status, StatusRead) else None,
        record=status_record,
    )

