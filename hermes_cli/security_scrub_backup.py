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
import secrets as _secrets
import shutil
import sqlite3
import stat as _stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

LOCK_NAME = ".secret-scrub.lock"
BACKUP_SUBDIR = Path("backups") / "secret-scrub"
BACKUP_PARTS = ("backups", "secret-scrub")
LEDGER_PARTS = ("secret-scrub",)
_COUNT_TABLES = ("messages", "sessions")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC


class SymlinkRefusal(OSError):
    """A path component the scrub would walk or create through is a symlink (or not a directory)."""


# ---------------------------------------------------------------------------
# Directory-fd walking: every component opened O_NOFOLLOW|O_DIRECTORY, created with mkdirat
# ---------------------------------------------------------------------------

def _lstat_at(name: str, dir_fd: int) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


def walk_from_fd(start_fd: int, parts: Sequence[str], *, create: bool = False, label: str = "",
                 missing_ok: bool = False) -> Optional[int]:
    """A NEW directory fd for ``start_fd/parts...`` (``start_fd`` stays open).

    Each component is opened ``O_NOFOLLOW|O_DIRECTORY``; with ``create`` a missing component
    is made with ``os.mkdir(name, 0o700, dir_fd=...)`` first. A component that is a symlink
    or not a directory raises :class:`SymlinkRefusal`. With ``missing_ok`` a missing
    component (and no ``create``) returns ``None``."""
    fd = os.open(".", _DIR_FLAGS, dir_fd=start_fd)  # a fresh fd for the same directory
    try:
        for i, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if missing_ok and not create:
                    os.close(fd)
                    return None
                raise
            except OSError as exc:
                st = _lstat_at(part, fd)
                where = "/".join(filter(None, (label, *parts[:i + 1])))
                if st is not None and _stat.S_ISLNK(st.st_mode):
                    raise SymlinkRefusal(exc.errno, f"{where} is a symlink; refusing to follow it") from None
                if st is not None and not _stat.S_ISDIR(st.st_mode):
                    raise SymlinkRefusal(exc.errno, f"{where} is not a directory") from None
                raise
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir_chain(anchor: Path, parts: Sequence[str], *, create: bool = False,
                   missing_ok: bool = False) -> Optional[int]:
    """``walk_from_fd`` from the directory ``anchor`` (the anchor itself is the trusted root)."""
    afd = os.open(str(anchor), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        return walk_from_fd(afd, parts, create=create, missing_ok=missing_ok)
    finally:
        os.close(afd)


def check_dir_chain(anchor: Path, parts: Sequence[str]) -> Optional[str]:
    """Why ``anchor/parts`` cannot be used (a symlinked or non-directory component), or ``None``.
    Missing components are fine: they are created later with ``mkdirat``."""
    try:
        fd = open_dir_chain(anchor, parts, missing_ok=True)
    except SymlinkRefusal as exc:
        return exc.strerror
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{'/'.join(parts)}: cannot open ({exc.__class__.__name__})"
    if fd is not None:
        os.close(fd)
    return None


def _write_new_at(dir_fd: int, rel: str, data: bytes) -> str:
    """Create ``dir_fd/rel`` (parents via mkdirat) with ``O_CREAT|O_EXCL|O_NOFOLLOW`` 0600,
    write and fsync; returns the sha256 of what was written."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"bad backup path {rel!r}")
    pfd = walk_from_fd(dir_fd, parts[:-1], create=True)
    try:
        fd = os.open(parts[-1], _NEW_FILE_FLAGS, 0o600, dir_fd=pfd)
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.fsync(pfd)
        except OSError:
            pass
    finally:
        os.close(pfd)
    return sha256_bytes(data)


def _read_at(dir_fd: int, rel: str) -> bytes:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    pfd = walk_from_fd(dir_fd, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=pfd)
    finally:
        os.close(pfd)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _path_is_entry_at(path: Path, dir_fd: int, name: str) -> bool:
    """``path`` (resolved by the kernel) is the regular file ``name`` inside ``dir_fd``."""
    at = _lstat_at(name, dir_fd)
    try:
        by_path = os.stat(path)
    except OSError:
        return False
    return bool(at and _stat.S_ISREG(at.st_mode) and (at.st_dev, at.st_ino) == (by_path.st_dev, by_path.st_ino))


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Remove the scrub's OWN scratch dir ``parent_fd/name`` without following symlinks."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return
    try:
        for child in os.listdir(fd):
            st = _lstat_at(child, fd)
            if st is not None and _stat.S_ISDIR(st.st_mode):
                _rmtree_at(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


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
    # Raw bytes another process appended to the ORIGINAL inode while it was being replaced:
    # ``{"backup", "offset", "size", "sha256"}`` each; restore = base + tails in offset order.
    tails: List[dict] = field(default_factory=list)


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
    sha256: Optional[str] = None  # of the backup copy == of the private snapshot it was streamed from
    size: int = 0


@dataclass
class Backup:
    path: Path
    files: List[FileBackup] = field(default_factory=list)
    dbs: List[DbBackup] = field(default_factory=list)
    # The backup dir, reached by an O_NOFOLLOW walk; every write goes through it.
    dir_fd: Optional[int] = field(default=None, repr=False)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def close(self) -> None:
        if self.dir_fd is not None:
            os.close(self.dir_fd)
            self.dir_fd = None

    def manifest(self) -> dict:
        return {
            "version": 1,
            "created_at": self.created_at,
            "note": "Original bytes before `hermes security scrub --apply`. Holds the raw secrets.",
            "files": [{"locator": f.locator, "backup": f.backup_rel, "sha256": f.sha256, "size": f.size,
                       "tails": list(f.tails)} for f in self.files],
            "dbs": [{"locator": d.locator, "backup": d.backup_rel, "counts": d.counts,
                     "rows_sha256": d.rows_sha256, "sha256": d.sha256, "size": d.size} for d in self.dbs],
        }


def cell_key(table: str, row_id: object, col: str) -> Tuple[str, str, str]:
    return table, str(row_id), col


def cell_digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8", "surrogatepass")).hexdigest()


def _iter_spec_rows(db_path: Path, spec: RowSpec):
    """Yields ``(table, cols, n_wanted, None)`` per table, then ``(table, cols, n, row)`` per row."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        for table in sorted(spec):
            cols, ids = spec[table]
            wanted = sorted(set(ids), key=lambda v: (str(type(v)), v))
            yield table, cols, len(wanted), None
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table} "
                                        f"WHERE id IN ({marks}) ORDER BY id", chunk):
                    yield table, cols, len(wanted), tuple(row)
    finally:
        conn.close()


def rows_digest(db_path: Path, spec: Optional[RowSpec]) -> Optional[str]:
    """SHA-256 over the ``spec`` rows (``id`` + columns, ordered by id); ``None`` without a spec."""
    if not spec:
        return None
    h = hashlib.sha256()
    for table, _cols, n, row in _iter_spec_rows(db_path, spec):
        if row is None:
            h.update(f"\x00{table}\x00{n}\x00".encode())
            continue
        h.update(repr(row).encode("utf-8", "surrogatepass"))
        h.update(b"\x1e")
    return h.hexdigest()


def cell_digests(db_path: Path, spec: Optional[RowSpec]) -> Dict[Tuple[str, str, str], str]:
    """``{(table, str(id), col): digest}`` of every planned cell, read from ``db_path`` (the
    VERIFIED backup copy): the apply step only rewrites a cell whose live value still hashes
    to what the backup holds."""
    out: Dict[Tuple[str, str, str], str] = {}
    if not spec:
        return out
    for table, cols, _n, row in _iter_spec_rows(db_path, spec):
        if row is None:
            continue
        for col, value in zip(cols, row[1:]):
            out[cell_key(table, row[0], col)] = cell_digest(value)
    return out


def table_counts(db_path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in _COUNT_TABLES}
    finally:
        conn.close()


def new_backup_dir(root: Path) -> Tuple[Path, int]:
    """``<root>/backups/secret-scrub/<UTC stamp>`` created by an ``O_NOFOLLOW`` fd walk (mkdirat,
    0700); a symlinked component raises :class:`SymlinkRefusal`. Returns ``(path, dir_fd)``."""
    base_fd = open_dir_chain(Path(root), BACKUP_PARTS, create=True)
    try:
        os.fchmod(base_fd, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name, n = stamp, 1
        while True:
            try:
                os.mkdir(name, 0o700, dir_fd=base_fd)
                break
            except FileExistsError:
                n += 1
                name = f"{stamp}-{n}"
        fd = walk_from_fd(base_fd, (name,))
        os.fchmod(fd, 0o700)
    finally:
        os.close(base_fd)
    return Path(root).joinpath(*BACKUP_PARTS, name), fd


# Test seam (monkeypatched in tests; a no-op in production): runs after the backup entry's
# parent directory was walked and before the database copy is streamed into it.
def _before_db_stream(dir_fd: int, rel: str) -> None:
    return None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _private_snapshot_dir(exclude: Sequence[Path]) -> str:
    """A fresh 0700 ``mkdtemp`` dir OUTSIDE every scrub target for SQLite's by-path backup API."""
    tmp = tempfile.mkdtemp(prefix="hermes-scrub-snap-")
    os.chmod(tmp, 0o700)
    real = os.path.realpath(tmp)
    for root in exclude:
        top = os.path.realpath(os.fspath(root)).rstrip(os.sep) + os.sep
        if (real + os.sep).startswith(top):
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError("the private database snapshot dir would sit inside a scrub target; "
                               "point TMPDIR elsewhere")
    return tmp


def _stream_db_into(dir_fd: int, rel: str, snapshot: str) -> Tuple[str, int]:
    """Copy the private ``snapshot`` into ``dir_fd/rel`` through directory fds only
    (``O_CREAT|O_EXCL|O_NOFOLLOW`` 0600, chunked, fsync file + dir), re-read the written file
    through the same dir fd and require its sha256 to equal the snapshot's. Finally the walked
    chain must still lead to that very inode: a parent swapped for a symlink since the walk
    raises :class:`SymlinkRefusal`. Returns ``(sha256, size)``."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"bad backup path {rel!r}")
    pfd = walk_from_fd(dir_fd, parts[:-1], create=True)
    try:
        _before_db_stream(dir_fd, rel)
        want = hashlib.sha256()
        src = os.open(snapshot, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            out = os.open(parts[-1], _NEW_FILE_FLAGS, 0o600, dir_fd=pfd)
            try:
                os.fchmod(out, 0o600)
                while True:
                    chunk = os.read(src, 1 << 20)
                    if not chunk:
                        break
                    want.update(chunk)
                    _write_all(out, chunk)
                os.fsync(out)
                written = os.fstat(out)
            finally:
                os.close(out)
        finally:
            os.close(src)
        try:
            os.fsync(pfd)
        except OSError:
            pass
        got = hashlib.sha256()
        rfd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=pfd)
        try:
            if (os.fstat(rfd).st_dev, os.fstat(rfd).st_ino) != (written.st_dev, written.st_ino):
                raise SymlinkRefusal(0, f"backup entry {rel} was replaced while it was written")
            while True:
                chunk = os.read(rfd, 1 << 20)
                if not chunk:
                    break
                got.update(chunk)
        finally:
            os.close(rfd)
        if got.hexdigest() != want.hexdigest():
            raise RuntimeError(f"backup copy {rel} does not match its snapshot")
    finally:
        os.close(pfd)
    cfd = walk_from_fd(dir_fd, parts[:-1])  # raises SymlinkRefusal on a swapped component
    try:
        at = _lstat_at(parts[-1], cfd)
    finally:
        os.close(cfd)
    if at is None or (at.st_dev, at.st_ino) != (written.st_dev, written.st_ino):
        raise SymlinkRefusal(0, f"backup entry {rel} is no longer inside the backup directory")
    return want.hexdigest(), written.st_size


def _snapshot_db_at(dir_fd: int, rel: str, source: Path, spec: Optional[RowSpec],
                    exclude: Sequence[Path] = ()) -> Tuple[Dict[str, int], Optional[str], str, int]:
    """SQLite's backup API writes by PATH, so it only ever writes into a private ``mkdtemp``
    dir (0700, outside every scrub target; the snapshot file 0600). The snapshot is switched
    to rollback-journal mode there (so no later read-only open of the backup copy creates a
    ``-wal``/``-shm`` beside it), counted and row-hashed, then streamed into the backup
    through the walked dir fd, and the private copy + dir are removed.
    Returns ``(counts, rows_sha256, sha256, size)``."""
    from hermes_cli import backup_sqlite

    tmp = _private_snapshot_dir(exclude)
    try:
        snapshot = os.path.join(tmp, "state.db")
        if not backup_sqlite._safe_copy_db(Path(source), Path(snapshot)):
            raise RuntimeError("could not snapshot the database consistently")
        conn = sqlite3.connect(snapshot, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            conn.close()
        os.chmod(snapshot, 0o600)
        counts = table_counts(Path(snapshot))
        digest = rows_digest(Path(snapshot), spec)
        sha, size = _stream_db_into(dir_fd, rel, snapshot)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # the scrub's own private snapshot, never a target
    return counts, digest, sha, size


def write_backup(root: Path, files: Sequence[tuple], dbs: Sequence[tuple],
                 private_exclude: Sequence[Path] = ()) -> Backup:
    """Copy originals into a fresh backup dir (every file created ``O_CREAT|O_EXCL|O_NOFOLLOW``
    through directory fds).

    ``files``: ``(locator, source, backup_rel[, data[, reader]])``; with ``data`` the backup is
    those exact bytes (the ones the scrub planned against) and ``reader`` re-reads the live
    original at verification. ``dbs``: ``(locator, source, backup_rel[, row_spec])``; the
    ``row_spec`` rows are content-hashed. A DB is snapshotted in a private temp dir outside
    ``root`` and every ``private_exclude`` path, then streamed in by dir fd: nothing is ever
    written into the backup tree by pathname. Raises ``RuntimeError`` if a DB snapshot cannot
    be made consistently and :class:`SymlinkRefusal` for a symlinked backup path.
    """
    path, dir_fd = new_backup_dir(root)
    backup = Backup(path=path, dir_fd=dir_fd)
    try:
        for item in files:
            locator, source, rel = item[:3]
            data = item[3] if len(item) > 3 else None
            reader = item[4] if len(item) > 4 else None
            if data is None:
                with open(source, "rb") as fh:
                    data = fh.read()
            digest = _write_new_at(dir_fd, rel, data)
            backup.files.append(FileBackup(locator, source, rel, digest, len(data), reader))
        for item in dbs:
            locator, source, rel = item[:3]
            spec = item[3] if len(item) > 3 else None
            try:
                counts, digest, sha, size = _snapshot_db_at(dir_fd, rel, source, spec,
                                                            (Path(root), *private_exclude))
            except RuntimeError as exc:
                raise RuntimeError(f"could not snapshot {locator} consistently ({exc})") from None
            backup.dbs.append(DbBackup(locator, source, rel, counts, spec, digest, sha, size))
        _write_new_at(dir_fd, "manifest.json",
                      json.dumps(backup.manifest(), indent=2, sort_keys=True).encode("utf-8"))
    except BaseException:
        backup.close()
        raise
    return backup


def verify_backup_restores(backup: Backup) -> List[str]:
    """Restore every backed-up item into a scratch dir and check it; returns problems (empty = ok).

    Files: restored sha256 == manifest sha256 == the live original's current sha256.
    DBs: restored copy passes ``PRAGMA integrity_check``; its row counts equal both the
    manifest and the live DB's current counts; and the content hash of every row about to
    be rewritten equals the manifest's and the live DB's (a writer that changed the DB
    after the snapshot fails verification rather than leaving an out-of-date backup).
    The scratch dir is made, written and removed through the backup's directory fd.
    """
    problems: List[str] = []
    if backup.dir_fd is None:
        return ["backup directory is not open"]
    dir_fd = backup.dir_fd
    try:
        manifest = json.loads(_read_at(dir_fd, "manifest.json").decode("utf-8"))
    except (OSError, ValueError) as exc:
        return [f"backup manifest unreadable: {exc.__class__.__name__}"]
    recorded = {f["backup"]: f for f in manifest.get("files", [])}
    recorded_dbs = {d["backup"]: d for d in manifest.get("dbs", [])}
    scratch = f".verify-{_secrets.token_hex(6)}"
    os.mkdir(scratch, 0o700, dir_fd=dir_fd)
    try:
        sfd = walk_from_fd(dir_fd, (scratch,))
        try:
            for f in backup.files:
                try:
                    got = _write_new_at(sfd, f.backup_rel, _read_at(dir_fd, f.backup_rel))
                    live = sha256_bytes(f.reader()) if f.reader is not None else sha256_file(f.source)
                except OSError as exc:
                    problems.append(f"{f.locator}: restore failed ({exc.__class__.__name__})")
                    continue
                want = (recorded.get(f.backup_rel) or {}).get("sha256")
                if not (got == want == live == f.sha256):
                    problems.append(f"{f.locator}: restored checksum does not match the original")
            for d in backup.dbs:
                restored = backup.path / scratch / d.backup_rel
                try:
                    got_db = _write_new_at(sfd, d.backup_rel, _read_at(dir_fd, d.backup_rel))
                    if got_db != (recorded_dbs.get(d.backup_rel) or {}).get("sha256") or got_db != d.sha256:
                        problems.append(f"{d.locator}: restored copy checksum does not match the snapshot")
                        continue
                    parts = d.backup_rel.split("/")
                    pfd = walk_from_fd(sfd, parts[:-1])
                    try:
                        if not _path_is_entry_at(restored, pfd, parts[-1]):
                            raise SymlinkRefusal(0, "restored copy is not inside the scratch directory")
                    finally:
                        os.close(pfd)
                    conn = sqlite3.connect(f"{restored.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
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
        finally:
            os.close(sfd)
    finally:
        _rmtree_at(dir_fd, scratch)
    return problems


def verified_cell_digests(backup: Backup) -> Dict[str, Dict[Tuple[str, str, str], str]]:
    """Per DB locator, the planned cells' digests read from the (verified) backup copy."""
    return {d.locator: cell_digests(backup.path / d.backup_rel, d.rows) for d in backup.dbs}


def _replace_manifest(backup: Backup) -> None:
    """Rewrite ``manifest.json`` atomically inside the walked backup dir fd (0600 ``O_EXCL``
    temp, fsync, ``os.replace`` within the dir fd, fsync the dir)."""
    assert backup.dir_fd is not None
    tmp = f".manifest.{_secrets.token_hex(6)}.tmp"
    _write_new_at(backup.dir_fd, tmp, json.dumps(backup.manifest(), indent=2, sort_keys=True).encode("utf-8"))
    try:
        os.replace(tmp, "manifest.json", src_dir_fd=backup.dir_fd, dst_dir_fd=backup.dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=backup.dir_fd)
        except FileNotFoundError:
            pass
        raise
    try:
        os.fsync(backup.dir_fd)
    except OSError:
        pass


def append_tail(backup: Backup, locator: str, offset: int, data: bytes) -> str:
    """Back up RAW bytes another process appended to ``locator``'s ORIGINAL inode at ``offset``
    while the scrub replaced it: a new ``<backup_rel>.tail-<n>`` sidecar (``O_CREAT|O_EXCL|
    O_NOFOLLOW`` 0600 through the walked backup dir fd, re-read and checksum-verified), recorded
    in the manifest so :func:`restore_file_bytes` replays it. Tails must be contiguous."""
    if backup.dir_fd is None:
        raise OSError("backup directory is not open")
    entry = next((f for f in backup.files if f.locator == locator), None)
    if entry is None:
        raise KeyError(locator)
    expected = entry.size + sum(int(t["size"]) for t in entry.tails)
    if offset != expected:
        raise ValueError(f"tail offset {offset} is not contiguous (expected {expected})")
    rel = f"{entry.backup_rel}.tail-{len(entry.tails) + 1}"
    digest = _write_new_at(backup.dir_fd, rel, data)
    if sha256_bytes(_read_at(backup.dir_fd, rel)) != digest:
        raise OSError(f"backup tail {rel} does not read back")
    entry.tails.append({"backup": rel, "offset": offset, "size": len(data), "sha256": digest})
    _replace_manifest(backup)
    return rel


def restore_file_bytes(backup_dir: Path, locator: str) -> bytes:
    """The ORIGINAL bytes of ``locator`` as backed up: the base copy followed by every
    appended-tail sidecar in offset order, each checked against the manifest. Reads only
    through directory fds (``O_NOFOLLOW`` on every component)."""
    fd = open_dir_chain(Path(backup_dir), ())
    try:
        manifest = json.loads(_read_at(fd, "manifest.json").decode("utf-8"))
        entry = next((f for f in manifest.get("files", []) if f.get("locator") == locator), None)
        if entry is None:
            raise KeyError(locator)
        out = bytearray(_read_at(fd, entry["backup"]))
        if sha256_bytes(bytes(out)) != entry["sha256"]:
            raise ValueError(f"{locator}: backup copy checksum mismatch")
        for tail in sorted(entry.get("tails") or [], key=lambda t: int(t["offset"])):
            if int(tail["offset"]) != len(out):
                raise ValueError(f"{locator}: backup tail {tail['backup']} is not contiguous")
            chunk = _read_at(fd, tail["backup"])
            if sha256_bytes(chunk) != tail["sha256"]:
                raise ValueError(f"{locator}: backup tail {tail['backup']} checksum mismatch")
            out += chunk
        return bytes(out)
    finally:
        os.close(fd)


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
