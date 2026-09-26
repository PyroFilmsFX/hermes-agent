"""Lock, backup and restore-verification for ``hermes security scrub``.

The owner rules this module enforces:

* a timestamped backup of every file and ``state.db`` about to change is written
  (0700 dir, 0600 files) BEFORE any write;
* the backup is RESTORED into a scratch dir inside itself and verified
  (file checksums; ``PRAGMA integrity_check``, row counts and a content hash of every
  row about to be rewritten for each DB copy) before the caller may write anything. A failed verification aborts the run
  with zero writes;
* nothing here deletes, prunes or truncates a scrub target. The only removals
  are the scrub's own lock file and its own scratch verification copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

LOCK_NAME = ".secret-scrub.lock"
BACKUP_SUBDIR = Path("backups") / "secret-scrub"
_COUNT_TABLES = ("messages", "sessions")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Lock (O_EXCL + pid, stale-pid reclaim)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil is a hard dependency
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class ScrubLock:
    """``<root>/.secret-scrub.lock``; a lock whose pid is dead is reclaimed."""

    def __init__(self, root: Path):
        self.path = Path(root) / LOCK_NAME
        self.held = False

    def acquire(self) -> Optional[str]:
        """``None`` when acquired, else a refusal message."""
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                try:
                    pid = int((self.path.read_text(encoding="utf-8").strip() or "0").split()[0])
                except (OSError, ValueError):
                    pid = 0
                if pid and _pid_alive(pid):
                    return f"another scrub holds the lock {self.path} (pid {pid})"
                try:
                    self.path.unlink()  # stale lock from a dead scrub: our own file, never a target
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()}\n")
            self.held = True
            return None
        return f"could not take the scrub lock {self.path}"

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

@dataclass
class FileBackup:
    locator: str
    source: Path
    backup_rel: str
    sha256: str
    size: int
    # Re-reads the live original the same symlink-safe way the scrub does; ``None`` = by path.
    reader: Optional[Callable[[], bytes]] = field(default=None, repr=False)


# ``{table: (columns, ids)}``: the rows the scrub is about to rewrite, keyed by ``id``.
RowSpec = Dict[str, Tuple[Tuple[str, ...], Sequence[object]]]


@dataclass
class DbBackup:
    locator: str
    source: Path
    backup_rel: str
    counts: Dict[str, int] = field(default_factory=dict)
    rows: Optional[RowSpec] = field(default=None, repr=False)
    rows_sha256: Optional[str] = None


@dataclass
class Backup:
    path: Path
    files: List[FileBackup] = field(default_factory=list)
    dbs: List[DbBackup] = field(default_factory=list)

    def manifest(self) -> dict:
        return {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "note": "Original bytes before `hermes security scrub --apply`. Holds the raw secrets.",
            "files": [{"locator": f.locator, "backup": f.backup_rel, "sha256": f.sha256, "size": f.size}
                      for f in self.files],
            "dbs": [{"locator": d.locator, "backup": d.backup_rel, "counts": d.counts,
                     "rows_sha256": d.rows_sha256} for d in self.dbs],
        }


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _copy_private(src: Path, dst: Path) -> None:
    _mkdir_private(dst.parent)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())


def _write_private(dst: Path, data: bytes) -> None:
    _mkdir_private(dst.parent)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def rows_digest(db_path: Path, spec: Optional[RowSpec]) -> Optional[str]:
    """SHA-256 over the ``spec`` rows (``id`` + columns, ordered by id); ``None`` without a spec."""
    if not spec:
        return None
    h = hashlib.sha256()
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        for table in sorted(spec):
            cols, ids = spec[table]
            wanted = sorted(set(ids), key=lambda v: (str(type(v)), v))
            h.update(f"\x00{table}\x00{len(wanted)}\x00".encode())
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table} "
                                        f"WHERE id IN ({marks}) ORDER BY id", chunk):
                    h.update(repr(tuple(row)).encode("utf-8", "surrogatepass"))
                    h.update(b"\x1e")
    finally:
        conn.close()
    return h.hexdigest()


def table_counts(db_path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in _COUNT_TABLES}
    finally:
        conn.close()


def new_backup_dir(root: Path) -> Path:
    base = Path(root) / BACKUP_SUBDIR
    _mkdir_private(base)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate, n = base / stamp, 1
    while candidate.exists():
        n += 1
        candidate = base / f"{stamp}-{n}"
    _mkdir_private(candidate)
    return candidate


def write_backup(root: Path, files: Sequence[tuple], dbs: Sequence[tuple]) -> Backup:
    """Copy originals into a fresh backup dir.

    ``files``: ``(locator, source, backup_rel[, data[, reader]])``; with ``data`` the backup is
    those exact bytes (the ones the scrub planned against) and ``reader`` re-reads the live
    original at verification. ``dbs``: ``(locator, source, backup_rel[, row_spec])``; the
    ``row_spec`` rows are content-hashed. Raises ``RuntimeError`` if a DB snapshot cannot be
    made consistently.
    """
    from hermes_cli.backup_sqlite import _safe_copy_db

    backup = Backup(path=new_backup_dir(root))
    for item in files:
        locator, source, rel = item[:3]
        data = item[3] if len(item) > 3 else None
        reader = item[4] if len(item) > 4 else None
        dst = backup.path / rel
        if data is None:
            _copy_private(source, dst)
        else:
            _write_private(dst, data)
        backup.files.append(FileBackup(locator, source, rel, sha256_file(dst), dst.stat().st_size, reader))
    for item in dbs:
        locator, source, rel = item[:3]
        spec = item[3] if len(item) > 3 else None
        dst = backup.path / rel
        _mkdir_private(dst.parent)
        if not _safe_copy_db(source, dst):
            raise RuntimeError(f"could not snapshot {locator} consistently")
        os.chmod(dst, 0o600)
        backup.dbs.append(DbBackup(locator, source, rel, table_counts(dst), spec, rows_digest(dst, spec)))
    manifest_path = backup.path / "manifest.json"
    fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(backup.manifest(), fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    return backup


def verify_backup_restores(backup: Backup) -> List[str]:
    """Restore every backed-up item into a scratch dir and check it; returns problems (empty = ok).

    Files: restored sha256 == manifest sha256 == the live original's current sha256.
    DBs: restored copy passes ``PRAGMA integrity_check``; its row counts equal both the
    manifest and the live DB's current counts; and the content hash of every row about to
    be rewritten equals the manifest's and the live DB's (a writer that changed the DB
    after the snapshot fails verification rather than leaving an out-of-date backup).
    """
    problems: List[str] = []
    try:
        manifest = json.loads((backup.path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"backup manifest unreadable: {exc}"]
    recorded = {f["backup"]: f for f in manifest.get("files", [])}
    recorded_dbs = {d["backup"]: d for d in manifest.get("dbs", [])}
    with tempfile.TemporaryDirectory(dir=backup.path, prefix=".verify-") as scratch:
        scratch_path = Path(scratch)
        for f in backup.files:
            restored = scratch_path / f.backup_rel
            try:
                _copy_private(backup.path / f.backup_rel, restored)
                got = sha256_file(restored)
                live = sha256_bytes(f.reader()) if f.reader is not None else sha256_file(f.source)
            except OSError as exc:
                problems.append(f"{f.locator}: restore failed ({exc.__class__.__name__})")
                continue
            want = (recorded.get(f.backup_rel) or {}).get("sha256")
            if not (got == want == live == f.sha256):
                problems.append(f"{f.locator}: restored checksum does not match the original")
        for d in backup.dbs:
            restored = scratch_path / d.backup_rel
            try:
                _copy_private(backup.path / d.backup_rel, restored)
                conn = sqlite3.connect(str(restored), timeout=5.0)
                try:
                    rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
                    counts = {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
                              for t in _COUNT_TABLES}
                finally:
                    conn.close()
                live_counts = table_counts(d.source)
                restored_digest = rows_digest(restored, d.rows)
                live_digest = rows_digest(d.source, d.rows)
            except (OSError, sqlite3.DatabaseError) as exc:
                problems.append(f"{d.locator}: restored copy unreadable ({exc.__class__.__name__})")
                continue
            want_digest = (recorded_dbs.get(d.backup_rel) or {}).get("rows_sha256")
            if rows != ["ok"]:
                problems.append(f"{d.locator}: restored copy fails integrity_check")
            elif not (counts == d.counts == live_counts):
                problems.append(f"{d.locator}: restored row counts differ from the live database")
            elif not (restored_digest == want_digest == live_digest == d.rows_sha256):
                problems.append(f"{d.locator}: restored rows to be rewritten differ from the live database "
                                "(content hash mismatch)")
    return problems


def icloud_warning(root: Path) -> Optional[str]:
    try:
        resolved = Path(root).resolve()
    except OSError:
        return None
    home = Path.home()
    for synced in (home / "Documents", home / "Desktop"):
        try:
            resolved.relative_to(synced.resolve())
        except (ValueError, OSError):
            continue
        return (f"HERMES_HOME is under {synced}; if iCloud 'Desktop & Documents' sync is on, "
                "the backup (raw secrets) syncs too.")
    return None


def utc_now() -> float:
    return time.time()
