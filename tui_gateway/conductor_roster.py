"""Bounded incremental reader of the conductor marker index (~/.claude/state/tb-marker-index.jsonl)."""

from __future__ import annotations

import json
import os
import pwd
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _clear_cache() -> None:
    """Clear in-memory file cache (testing hook)."""
    _CACHE.clear()


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
