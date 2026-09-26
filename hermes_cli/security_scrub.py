"""``hermes security scrub``: mask secrets already stored at rest (HE-SECRET-HYGIENE §5).

Dry run by default: the report lists target, locator, kind, count and action, never a
value. ``--apply``:

1. takes ``<root>/.secret-scrub.lock``; refuses while an FTS rebuild is in flight or a
   ``PRAGMA quick_check`` fails;
2. writes a timestamped backup (0700/0600) of every file and ``state.db`` about to
   change, then RESTORES it into a scratch dir and verifies it (checksums; DB integrity,
   row counts and a content hash of the rows to be rewritten); any verification failure
   aborts with zero writes;
3. rewrites files atomically (temp file with the original's mode/owner + fsync +
   ``os.replace``). Immediately before the replace the original is re-opened
   ``O_NOFOLLOW`` and ``fstat``-ed: a changed size, mtime or inode, or another process
   holding it open for writing (``lsof``, when installed), leaves the file untouched
   (``changed-during-apply`` / ``deferred-live``). ``state.db`` is updated in
   compare-and-swap batches inside ``BEGIN IMMEDIATE`` transactions with
   ``secure_delete`` on, then FTS integrity-check + optimize, a PASSIVE WAL checkpoint
   (never TRUNCATE) and ``PRAGMA integrity_check``. An incomplete checkpoint fails the run
   and the next ``--apply`` finishes it.

Every target directory is walked with directory fds (``O_NOFOLLOW|O_DIRECTORY``): a
symlinked directory or file is reported ``skipped-symlink`` and never read or written.
Report locators, details, warnings and errors pass through the masker before output.

It is idempotent (placeholders never re-match) and resumable: every run rescans every
row, and ``state_meta['secret_scrub_progress']`` tracks the per-row items still pending
(including deferred-live rows), which a completed run clears only once they are masked.
Live sessions are deferred (§5.4), never rewritten.

Owner rule: masking in place after a verified backup is the ONLY mutation. The scrub
never deletes, prunes or truncates a transcript, log or any other target file.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shutil
import sqlite3
import stat as _stat
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from agent.redact_detect import DetectOptions, _is_allowlisted_value, find_secrets, passes_entropy_gate
from agent.secret_hygiene import (
    DETECTOR_VERSION,
    SecretHygieneConfig,
    TagKeyProvider,
    load_secret_hygiene_config,
    mask_secrets_for_ingest,
    mask_stored_text,
    scan_secrets,
)
from hermes_cli import security_scrub_backup as bk

TARGETS = ("pastes", "attachments", "transcripts", "state-db", "doc-cache", "sdk-transcripts")
BATCH_SIZE = 500
MAX_FILE_BYTES = 64 * 1024 * 1024
RECENT_WRITE_GRACE_S = 300.0
_MSG_COLUMNS = ("content", "api_content", "tool_calls", "reasoning", "reasoning_content", "display_metadata")
_SESSION_COLUMNS = ("title", "last_activity_description")
_FTS_TABLES = ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")
_FTS_REBUILD_KEYS = ("fts_rebuild_progress", "fts_rebuild_high_water",
                     "fts_cjk_rebuild_progress", "fts_cjk_rebuild_high_water")
_PROGRESS_KEY = "secret_scrub_progress"
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff", ".ico", ".pdf", ".docx",
    ".xlsx", ".pptx", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".mp3", ".mp4", ".mov", ".wav",
    ".ogg", ".m4a", ".webm", ".sqlite", ".db", ".bin", ".so", ".dylib",
})
_SDK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")
_WRITE_ACTIONS = ("would-mask", "masked")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
WAL_INCOMPLETE_MSG = ("state.db changes are committed but old WAL frames may still hold secrets. Close Hermes "
                      "and rerun `hermes security scrub --apply` to finish.")


# ---------------------------------------------------------------------------
# Report text masking: locators and details come from file names and stored fields
# ---------------------------------------------------------------------------

# Known prefixes glued to other name characters (``leak-ghp_...``), which the detector's
# word-boundary guard skips.
_REPORT_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-ant-|sk-|github_pat_|gh[pousr]_|fm2_|fo1_|npg_|xox[abposre]-|xapp-)"
    r"[A-Za-z0-9_\-]{12,}|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}|FlyV1 \S+")
_REPORT_TOKEN_RE = re.compile(r"[A-Za-z0-9_+=]{24,}")
_REPORT_OPTS = DetectOptions()


def redact_report_text(text: Any, trusted: Sequence[Optional[str]] = ()) -> Any:
    """Mask anything secret-shaped in report text (no tag key: ``[REDACTED:<kind>]``).

    Besides the detector, known prefixes glued into a file name and bare high-entropy
    name tokens are masked too. ``trusted`` paths (the Hermes root, the backup dir) are
    exempt from the bare-token rule only: a random temp-dir component is not a secret."""
    if not isinstance(text, str) or not text:
        return text
    spans: List[Tuple[int, int, str]] = [(f.start, f.end, f.kind) for f in find_secrets(text, _REPORT_OPTS)]
    spans += [(m.start(), m.end(), "secret") for m in _REPORT_PREFIX_RE.finditer(text)]
    safe: List[Tuple[int, int]] = []
    for t in trusted:
        if t:
            pos = text.find(t)
            while pos >= 0:
                safe.append((pos, pos + len(t)))
                pos = text.find(t, pos + 1)
    for m in _REPORT_TOKEN_RE.finditer(text):
        value = m.group(0)
        if any(a <= m.start() and m.end() <= b for a, b in safe):
            continue
        if passes_entropy_gate(value, _REPORT_OPTS) and not _is_allowlisted_value(value):
            spans.append((m.start(), m.end(), "high-entropy"))
    if not spans:
        return text
    merged: List[List[Any]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, kind])
    out, pos = [], 0
    for start, end, kind in merged:
        out.append(text[pos:start])
        out.append(f"[REDACTED:{kind}]")
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class ScrubItem:
    target: str
    locator: str
    kind: str
    count: int
    action: str  # would-mask | masked | deferred-live | skipped-optout | skipped-binary | report-only | ...
    detail: str = ""


@dataclass
class ScrubReport:
    mode: str
    root: str
    items: List[ScrubItem] = field(default_factory=list)
    backup_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    refused: Optional[str] = None
    exit_code: int = 0

    def add(self, target: str, locator: str, counts: Counter, action: str, detail: str = "") -> None:
        locator, detail = redact_report_text(locator), redact_report_text(detail)
        if not counts:
            self.items.append(ScrubItem(target, locator, "-", 0, action, detail))
            return
        for kind, count in sorted(counts.items()):
            self.items.append(ScrubItem(target, locator, kind, int(count), action, detail))

    def totals(self) -> Counter:
        out: Counter = Counter()
        for item in self.items:
            if item.action in _WRITE_ACTIONS:
                out[item.kind] += item.count
        return out

    def _trusted(self) -> Tuple[Optional[str], ...]:
        return (self.root, self.backup_path)

    def warn(self, message: str) -> None:
        message = redact_report_text(message, self._trusted())
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message: str) -> None:
        message = redact_report_text(message, self._trusted())
        if message not in self.errors:
            self.errors.append(message)

    def to_json(self) -> dict:
        def r(value):
            return redact_report_text(value, self._trusted())
        return {
            "mode": self.mode, "root": r(self.root), "exit_code": self.exit_code,
            "refused": r(self.refused), "backup_path": r(self.backup_path),
            "items": [{k: r(v) for k, v in vars(i).items()} for i in self.items],
            "totals_by_kind": dict(sorted(self.totals().items())),
            "warnings": [r(w) for w in self.warnings], "errors": [r(e) for e in self.errors],
        }

    def render_text(self) -> str:
        return "\n".join(redact_report_text(line, self._trusted()) for line in self._render_lines())

    def _render_lines(self) -> List[str]:
        head = "APPLY" if self.mode == "apply" else "DRY RUN (nothing written)"
        lines = [f"hermes security scrub: {head}", f"root: {self.root}"]
        if self.refused:
            lines.append(f"REFUSED: {self.refused}")
        if self.items:
            w_t = max(len("TARGET"), *(len(i.target) for i in self.items))
            w_a = max(len("ACTION"), *(len(i.action) for i in self.items))
            w_k = max(len("KIND"), *(len(i.kind) for i in self.items))
            lines.append(f"{'TARGET':<{w_t}}  {'ACTION':<{w_a}}  {'KIND':<{w_k}}  COUNT  LOCATOR")
            order = {t: n for n, t in enumerate(TARGETS)}
            for i in sorted(self.items, key=lambda i: (order.get(i.target, 99), i.action != "would-mask"
                                                         and i.action != "masked", i.locator)):
                extra = f"  ({i.detail})" if i.detail else ""
                lines.append(f"{i.target:<{w_t}}  {i.action:<{w_a}}  {i.kind:<{w_k}}  {i.count:>5}  {i.locator}{extra}")
        else:
            lines.append("No stored secrets found.")
        totals = self.totals()
        verb = "masked" if self.mode == "apply" else "would mask"
        lines.append(f"Totals ({verb}): " + (", ".join(f"{k}={v}" for k, v in sorted(totals.items())) or "none"))
        deferred = {i.locator for i in self.items if i.action == "deferred-live"}
        if deferred:
            lines.append(f"Deferred live: {len(deferred)} row(s)/file(s) of open sessions; retried on the next "
                         "--apply. Close the app for a full clean.")
        changed = {i.locator for i in self.items if i.action == "changed-during-apply"}
        if changed:
            lines.append(f"Changed during apply: {len(changed)} file(s) left untouched; retried on the next --apply.")
        symlinks = {i.locator for i in self.items if i.action == "skipped-symlink"}
        if symlinks:
            lines.append(f"Symlinks not followed: {len(symlinks)} (never read or written).")
        if self.mode == "apply" and totals:
            lines.append("Note: a rewritten closed session replays masked text if resumed (one prompt-cache write).")
        if self.backup_path:
            lines.append(f"Backup: {self.backup_path}  (holds the ORIGINAL secrets; keep it private)")
        lines += [f"warning: {w}" for w in self.warnings]
        lines += [f"error: {e}" for e in self.errors]
        return lines


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

@dataclass
class _FileUnit:
    target: str
    path: Path  # display/backup path; every read and write goes through ``anchor`` + ``parts``
    locator: str
    backup_rel: str
    fmt: str  # text | jsonl | json
    profile: str = ""
    session_id: Optional[str] = None
    report_only: bool = False
    anchor: Optional[Path] = None
    parts: Tuple[str, ...] = ()  # directory components from ``anchor`` to the file's parent
    live_keys: Tuple[Tuple[str, str], ...] = ()  # (profile, hermes session id) pairs owning the file


@dataclass(frozen=True)
class _Snap:
    """What the plan and the backup were taken against (``fstat`` of the opened file)."""

    size: int
    mtime_ns: int
    ino: int
    dev: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def of(cls, st: os.stat_result) -> "_Snap":
        return cls(st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_mode, st.st_uid, st.st_gid)

    def same_file_state(self, other: "_Snap") -> bool:
        return (self.size, self.mtime_ns, self.ino, self.dev) == (other.size, other.mtime_ns, other.ino, other.dev)


@dataclass
class _DbUnit:
    profile: str
    home: Path
    path: Path
    locator: str
    backup_rel: str
    live: Dict[str, str] = field(default_factory=dict)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _file_fmt(name: str) -> str:
    suffix = os.path.splitext(name)[1].lower()
    return "jsonl" if suffix == ".jsonl" else "json" if suffix == ".json" else "text"


# ---------------------------------------------------------------------------
# Symlink-safe filesystem access: directory fds, O_NOFOLLOW on every component
# ---------------------------------------------------------------------------

def _open_dir_at(name: str, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)


def _open_chain(anchor: Path, parts: Sequence[str]) -> int:
    """A directory fd for ``anchor/parts...``; raises ``OSError`` if any part is a symlink."""
    fd = os.open(str(anchor), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in parts:
            nfd = _open_dir_at(part, fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lstat_at(name: str, dir_fd: int) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


def _list_dir(anchor: Path, parts: Tuple[str, ...], on_symlink: Callable[[Tuple[str, ...]], None],
              recursive: bool = True, suffix: Optional[str] = None,
              dirs_only: bool = False) -> List[Tuple[Tuple[str, ...], str]]:
    """Regular files (or, with ``dirs_only``, directories) under ``anchor/parts``, never
    following a symlink. Symlinks met on the way -- including ``parts`` themselves -- go to
    ``on_symlink`` with their components and are not entered. Returns ``(dir_parts, name)``."""
    out: List[Tuple[Tuple[str, ...], str]] = []
    try:
        fd = os.open(str(anchor), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError:
        return out
    try:
        for i, part in enumerate(parts):
            st = _lstat_at(part, fd)
            if st is None:
                return out
            if _stat.S_ISLNK(st.st_mode):
                on_symlink(parts[:i + 1])
                return out
            if not _stat.S_ISDIR(st.st_mode):
                return out
            try:
                nfd = _open_dir_at(part, fd)
            except OSError:
                on_symlink(parts[:i + 1])  # swapped for a symlink since the lstat
                return out
            os.close(fd)
            fd = nfd
        _walk_fd(fd, tuple(parts), on_symlink, recursive, suffix, dirs_only, out)
    finally:
        os.close(fd)
    return out


def _walk_fd(fd: int, parts: Tuple[str, ...], on_symlink, recursive: bool, suffix: Optional[str],
             dirs_only: bool, out: List[Tuple[Tuple[str, ...], str]]) -> None:
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return
    for name in names:
        st = _lstat_at(name, fd)
        if st is None:
            continue
        if _stat.S_ISLNK(st.st_mode):
            on_symlink(parts + (name,))
        elif _stat.S_ISDIR(st.st_mode):
            if dirs_only:
                out.append((parts, name))
            elif recursive:
                try:
                    cfd = _open_dir_at(name, fd)
                except OSError:
                    on_symlink(parts + (name,))
                    continue
                try:
                    _walk_fd(cfd, parts + (name,), on_symlink, recursive, suffix, dirs_only, out)
                finally:
                    os.close(cfd)
        elif _stat.S_ISREG(st.st_mode) and not dirs_only and (suffix is None or name.endswith(suffix)):
            out.append((parts, name))


def _anchor_for(root: Path, home: Path) -> Tuple[Path, Tuple[str, ...]]:
    """Walk profile dirs from ``root`` so a symlinked ``profiles/<name>`` is caught too."""
    try:
        rel = Path(home).relative_to(root)
    except ValueError:
        return Path(home), ()
    return root, tuple(p for p in rel.parts if p not in ("", "."))


def _collect_root_files(root: Path, profiles: Sequence[Tuple[str, Path]], targets: Set[str],
                        report: ScrubReport) -> List[_FileUnit]:
    units: List[_FileUnit] = []
    seen: Set[str] = set()
    reported: Set[str] = set()

    def symlink(target: str, anchor: Path):
        def _report(parts: Tuple[str, ...]) -> None:
            loc = _rel(root, anchor.joinpath(*parts))
            if loc not in reported:
                reported.add(loc)
                report.add(target, loc, Counter(), "skipped-symlink", "symlink not followed")
        return _report

    def collect(target: str, anchor: Path, parts: Tuple[str, ...], profile: str = "", *,
                recursive: bool = True, suffix: Optional[str] = None, fmt: Optional[str] = None,
                session_from_stem: bool = False) -> None:
        for dir_parts, name in _list_dir(anchor, parts, symlink(target, anchor), recursive, suffix):
            path = anchor.joinpath(*dir_parts, name)
            rel = _rel(root, path)
            if rel in seen:
                continue
            seen.add(rel)
            session_id = os.path.splitext(name)[0] if session_from_stem else None
            units.append(_FileUnit(target, path, rel, f"files/{rel}", fmt or _file_fmt(name), profile, session_id,
                                   anchor=anchor, parts=dir_parts))

    if "pastes" in targets:
        collect("pastes", root, ("composer-pastes",), fmt="text")
    for name, home in profiles:
        anchor, base = _anchor_for(root, Path(home))
        if "attachments" in targets:
            collect("attachments", anchor, base + ("attachments",), name, fmt="text")
        if "transcripts" in targets:
            collect("transcripts", anchor, base + ("sessions",), name, recursive=False, suffix=".jsonl",
                    session_from_stem=True)
            collect("transcripts", anchor, base + ("pending_messages",), name)
        if "doc-cache" in targets:
            collect("doc-cache", anchor, base + ("cache", "documents"), name, fmt="text")
    if "attachments" in targets:
        collect("attachments", root, ("attachments",), fmt="text")
    return units


def _sdk_session_map(dbs: Sequence[_DbUnit]) -> Dict[str, Tuple[str, str]]:
    """``claude_sdk_session_id`` -> (profile, hermes session id), from each profile's state.db."""
    out: Dict[str, Tuple[str, str]] = {}
    for db in dbs:
        try:
            conn = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("SELECT id, claude_sdk_session_id FROM sessions "
                                    "WHERE claude_sdk_session_id IS NOT NULL AND claude_sdk_session_id <> ''").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for sid, sdk_id in rows:
            if isinstance(sdk_id, str) and _SDK_ID_RE.fullmatch(sdk_id):
                out.setdefault(sdk_id, (db.profile, str(sid)))
    return out


def _classify_sdk_text(text: str, sdk_map: Dict[str, Tuple[str, str]]
                       ) -> Tuple[bool, str, Tuple[Tuple[str, str], ...]]:
    """Hermes-created iff EVERY line's ``sessionId`` is a ``claude_sdk_session_id`` stored in
    state.db and EVERY line's ``entrypoint`` is ``sdk-*`` (at least one of each recorded).
    Anything else is ambiguous and left alone. Reasons never echo a field value."""
    owners: Set[Tuple[str, str]] = set()
    saw_entrypoint = saw_session = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return False, "non-JSON line", ()
        if not isinstance(obj, dict):
            return False, "non-object JSON line", ()
        if "entrypoint" in obj:
            entry = obj["entrypoint"]
            if not isinstance(entry, str) or not entry.startswith("sdk-"):
                return False, "entrypoint not sdk-*", ()
            saw_entrypoint = True
        if "sessionId" in obj:
            sdk_id = obj["sessionId"]
            if not isinstance(sdk_id, str) or sdk_id not in sdk_map:
                return False, "sessionId not a Claude SDK session stored in state.db", ()
            owners.add(sdk_map[sdk_id])
            saw_session = True
    if not saw_entrypoint:
        return False, "no entrypoint recorded", ()
    if not saw_session:
        return False, "no sessionId recorded", ()
    return True, "", tuple(sorted(owners))


def _collect_sdk_files(claude_dir: Optional[Path], sdk_map: Dict[str, Tuple[str, str]],
                       report: ScrubReport) -> List[_FileUnit]:
    """Candidates: ``projects/<proj>/<sdk_id>.jsonl`` and ``projects/<proj>/<sdk_id>/subagents/*.jsonl``
    for every stored SDK id. Classification (every line) happens on the bytes actually scanned."""
    units: List[_FileUnit] = []
    if not claude_dir or not sdk_map:
        return units
    claude_dir = Path(claude_dir)
    reported: Set[str] = set()

    def on_symlink(parts: Tuple[str, ...]) -> None:
        loc = "claude:" + "/".join(parts)
        if loc not in reported:
            reported.add(loc)
            report.add("sdk-transcripts", loc, Counter(), "skipped-symlink", "symlink not followed")

    def add(dir_parts: Tuple[str, ...], name: str, sdk_id: str) -> None:
        rel = "/".join(dir_parts + (name,))
        profile, sid = sdk_map[sdk_id]
        units.append(_FileUnit("sdk-transcripts", claude_dir.joinpath(*dir_parts, name), f"claude:{rel}",
                               f"claude/{rel}", "jsonl", profile, sid, anchor=claude_dir, parts=dir_parts))

    for _, proj in _list_dir(claude_dir, ("projects",), on_symlink, dirs_only=True):
        proj_parts = ("projects", proj)
        def related_symlink(parts: Tuple[str, ...]) -> None:
            name = parts[-1]
            if name in sdk_map or (name.endswith(".jsonl") and name[:-6] in sdk_map):
                on_symlink(parts)

        top = {name for _, name in _list_dir(claude_dir, proj_parts, related_symlink, recursive=False,
                                             suffix=".jsonl")}
        subdirs = {name for _, name in _list_dir(claude_dir, proj_parts, lambda _p: None, dirs_only=True)}
        for sdk_id in sorted(sdk_map):
            if f"{sdk_id}.jsonl" in top:
                add(proj_parts, f"{sdk_id}.jsonl", sdk_id)
            if sdk_id in subdirs:
                for dir_parts, name in _list_dir(claude_dir, proj_parts + (sdk_id, "subagents"), on_symlink,
                                                 recursive=False, suffix=".jsonl"):
                    add(dir_parts, name, sdk_id)
    return units


# ---------------------------------------------------------------------------
# Live sessions (§5.4)
# ---------------------------------------------------------------------------

def _default_holders(db_path: Path) -> list:
    from hermes_state_holders import foreign_state_db_holders

    return foreign_state_db_holders(db_path)


def _peer_lease_sessions(root: Path) -> Set[str]:
    """Hermes session ids whose Claude peer-name lease is held by a live backend."""
    lease_dir = root / "runtime" / "claude-peer-names"
    out: Set[str] = set()
    if not lease_dir.is_dir():
        return out
    try:
        from hermes_cli.active_sessions import _pid_liveness
    except Exception:  # pragma: no cover - defensive
        _pid_liveness = None
    for path in lease_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = str(record.get("session_id") or "") if isinstance(record, dict) else ""
        if not sid:
            continue
        try:
            alive = True if _pid_liveness is None else _pid_liveness(
                int(record.get("pid") or 0), record.get("start"), lenient=True) is not False
        except Exception:
            alive = True  # unknowable: treat as live
        if alive:
            out.add(sid)
    return out


def _live_sessions(conn: sqlite3.Connection, db: _DbUnit, root: Path, now: float, window_h: float,
                   holders_fn: Callable[[Path], list], peer_live: Set[str]) -> Dict[str, str]:
    live: Dict[str, str] = {}
    try:
        for (cid,) in conn.execute("SELECT conversation_id FROM session_turn_leases WHERE expires_at > ?", (now,)):
            live[str(cid)] = "turn lease"
    except sqlite3.Error:
        pass
    known = {str(r[0]): r[1] for r in conn.execute("SELECT id, parent_session_id FROM sessions")}
    for sid in peer_live:
        if sid in known:
            live.setdefault(sid, "live Claude SDK CLI")
    try:
        holders = holders_fn(db.path)
    except Exception:
        holders = [(0, "uninspectable")]
    if holders:
        cutoff = now - window_h * 3600.0
        for (sid,) in conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL AND COALESCE(last_activity_at, started_at) >= ?",
                (cutoff,)):
            live.setdefault(str(sid), "state.db held by another process")
    # A live conversation spans its compression lineage: defer ancestors and descendants too.
    children: Dict[str, List[str]] = {}
    for sid, parent in known.items():
        if parent:
            children.setdefault(str(parent), []).append(sid)
    for sid, reason in list(live.items()):
        stack, seen = [sid], {sid}
        parent = known.get(sid)
        while parent and parent not in seen:
            seen.add(parent)
            live.setdefault(parent, reason)
            parent = known.get(parent)
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in seen:
                    seen.add(child)
                    live.setdefault(child, reason)
                    stack.append(child)
    return live


# ---------------------------------------------------------------------------
# Masking helpers
# ---------------------------------------------------------------------------

def _mask_file_text(text: str, fmt: str, cfg: SecretHygieneConfig, key_provider: Optional[TagKeyProvider],
                    dry_run: bool) -> Tuple[str, Counter]:
    counts: Counter = Counter()
    if fmt == "jsonl":
        out = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            new, c = mask_stored_text(body, config=cfg, key_provider=key_provider, dry_run=dry_run)
            counts.update(c)
            out.append(new + ending)
        return "".join(out), counts
    if fmt == "json":
        return mask_stored_text(text, config=cfg, key_provider=key_provider, dry_run=dry_run)
    if dry_run:
        for f in scan_secrets(text, config=cfg):
            counts[f.kind] += f.count
        return text, counts
    masked, findings = mask_secrets_for_ingest(text, config=cfg, key_provider=key_provider)
    for f in findings:
        counts[f.kind] += f.count
    return masked, counts


def _read_unit(unit: _FileUnit) -> Tuple[Optional[str], Optional[bytes], str, Optional[_Snap]]:
    """(text, raw, skip_action, snap), reading through directory fds with ``O_NOFOLLOW``.
    skip_action is '' when the file is scannable text."""
    if os.path.splitext(unit.path.name)[1].lower() in _BINARY_SUFFIXES:
        return None, None, "skipped-binary", None
    try:
        pfd = _open_chain(unit.anchor or unit.path.parent, unit.parts if unit.anchor else ())
    except OSError:
        return None, None, "skipped-symlink", None
    try:
        try:
            fd = os.open(unit.path.name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=pfd)
        except OSError:
            return None, None, "skipped-symlink" if _lstat_is_link(unit.path.name, pfd) else "skipped-unreadable", None
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                return None, None, "skipped-unreadable", None
            if st.st_size > MAX_FILE_BYTES:
                return None, None, "skipped-large", None
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            snap = _Snap.of(os.fstat(fd))
        finally:
            os.close(fd)
    except OSError:
        return None, None, "skipped-unreadable", None
    finally:
        os.close(pfd)
    if snap.size != len(raw):
        return None, None, "changed-during-apply", None
    if b"\x00" in raw[:8192]:
        return None, raw, "skipped-binary", snap
    try:
        return raw.decode("utf-8"), raw, "", snap
    except UnicodeDecodeError:
        return None, raw, "skipped-binary", snap


def _lstat_is_link(name: str, dir_fd: int) -> bool:
    st = _lstat_at(name, dir_fd)
    return bool(st and _stat.S_ISLNK(st.st_mode))


def _reader_for(unit: _FileUnit) -> Callable[[], bytes]:
    def read() -> bytes:
        _, raw, skip, _ = _read_unit(unit)
        if raw is None:
            raise OSError(f"cannot re-read ({skip})")
        return raw
    return read


def _open_writer_pids(path: Path) -> List[int]:
    """Pids of OTHER processes holding ``path`` open for writing (``lsof``); ``[]`` when
    ``lsof`` is missing, slow or fails -- the live-session deferral and the quiet window
    remain the guard then."""
    exe = shutil.which("lsof")
    if not exe:
        return []
    try:
        result = subprocess.run([exe, "-w", "-F", "pa", "--", str(path)], capture_output=True, text=True,
                                timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: Set[int] = set()
    pid = 0
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("a") and line[1:2] in ("w", "u") and pid and pid != os.getpid():
            pids.add(pid)
    return sorted(pids)


def _atomic_rewrite_unit(unit: _FileUnit, data: bytes, planned: _Snap,
                         open_writers_fn: Callable[[Path], List[int]]) -> Tuple[str, str]:
    """Replace ``unit``'s content atomically; returns ``(outcome, detail)``.

    ``outcome``: ``"masked"``, ``"changed-during-apply"`` (the original's size, mtime or inode
    differ from ``planned``), or ``"deferred-live"`` (another process holds it open for
    writing). The temp file lives in the same directory (opened through the same
    ``O_NOFOLLOW`` directory fd), gets the original's mode (and owner, when we own it), is
    fsynced and ``os.replace``-d over the original. The original is never unlinked or
    truncated; only the scrub's own temp file is removed when the replace does not happen.
    """
    pfd = _open_chain(unit.anchor or unit.path.parent, unit.parts if unit.anchor else ())
    try:
        tmp = f".{unit.path.name}.{_secrets.token_hex(6)}.scrub-tmp"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=pfd)
        replaced = False
        try:
            with os.fdopen(tfd, "wb") as fh:
                fh.write(data)
                fh.flush()
                if hasattr(os, "fchown") and os.geteuid() in (0, planned.uid):
                    try:
                        os.fchown(fh.fileno(), planned.uid, planned.gid)
                    except OSError:
                        pass  # e.g. not a member of the original group: keep ours
                os.fchmod(fh.fileno(), _stat.S_IMODE(planned.mode))
                os.fsync(fh.fileno())
            _before_replace(unit.path)
            current = _fstat_at(unit.path.name, pfd)
            if current is None or not current.same_file_state(planned):
                return "changed-during-apply", "file changed between plan and replace; retried next run"
            pids = open_writers_fn(unit.path)
            if pids:
                return "deferred-live", "file open for writing by another process; retried next run"
            os.replace(tmp, unit.path.name, src_dir_fd=pfd, dst_dir_fd=pfd)
            replaced = True
        finally:
            if not replaced:
                try:
                    os.unlink(tmp, dir_fd=pfd)
                except FileNotFoundError:
                    pass
        try:
            os.fsync(pfd)
        except OSError:
            pass
    finally:
        os.close(pfd)
    return "masked", ""


def _fstat_at(name: str, dir_fd: int) -> Optional[_Snap]:
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        return _Snap.of(os.fstat(fd))
    finally:
        os.close(fd)


def _atomic_rewrite(path: Path, data: bytes) -> None:
    """The scrub's OWN ledger files: 0600 temp in the same dir, fsync, ``os.replace``."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".scrub-tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# Test seams (monkeypatched in tests; no-ops in production).
def _after_backup_written(backup_dir: Path) -> None:
    return None


def _after_batch_commit(batch_no: int) -> None:
    return None


def _before_replace(path: Path) -> None:
    return None


def _wal_checkpoint(conn: sqlite3.Connection) -> Tuple[int, int, int]:
    """``(busy, log, checkpointed)`` of a PASSIVE checkpoint. Never TRUNCATE: it corrupted
    B-trees on large databases under exclusive-lock I/O pressure (hermes_state
    ``_try_wal_checkpoint``, issue #45383)."""
    row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    return (int(row[0]), int(row[1]), int(row[2])) if row else (0, -1, -1)


def _integrity_problems(conn: sqlite3.Connection) -> List[str]:
    rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    return [] if rows == ["ok"] else rows


# ---------------------------------------------------------------------------
# state.db
# ---------------------------------------------------------------------------

def _load_cjk(conn: sqlite3.Connection, home: Path) -> bool:
    so = home / "lib" / "libfts5_cjk.so"
    if so.exists():
        try:
            conn.enable_load_extension(True)
            try:
                conn.load_extension(str(so))
            finally:
                conn.enable_load_extension(False)
            return True
        except Exception:
            pass
    try:
        from hermes_state_fts import load_fts5_cjk_extension

        return load_fts5_cjk_extension(conn)
    except Exception:
        return False


def _tables(conn: sqlite3.Connection) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def _db_preflight(db: _DbUnit) -> Optional[str]:
    try:
        conn = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return f"{db.locator}: cannot open ({exc})"
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
        if rows != ["ok"]:
            return f"{db.locator}: PRAGMA quick_check failed; run `hermes sessions repair` first"
        placeholders = ",".join("?" * len(_FTS_REBUILD_KEYS))
        busy = conn.execute(f"SELECT key FROM state_meta WHERE key IN ({placeholders})", _FTS_REBUILD_KEYS).fetchall()
        if busy:
            return f"{db.locator}: a search-index (FTS) rebuild is in progress; re-run when it finishes"
        if "messages_fts_cjk" in _tables(conn) and not _load_cjk(conn, db.home):
            return f"{db.locator}: messages_fts_cjk needs the cjk_unicode61 tokenizer, which is unavailable"
    except sqlite3.Error as exc:
        return f"{db.locator}: preflight failed ({exc})"
    finally:
        conn.close()
    return None


def _is_optout(display_metadata: Any) -> bool:
    if not isinstance(display_metadata, str) or "secret_optout" not in display_metadata:
        return False
    try:
        meta = json.loads(display_metadata)
    except ValueError:
        return False
    return isinstance(meta, dict) and bool(meta.get("secret_optout"))


def _read_progress(conn: sqlite3.Connection) -> Optional[dict]:
    """The stored per-item progress (``None`` if absent, unreadable or from another detector)."""
    try:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (_PROGRESS_KEY,)).fetchone()
        progress = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    if isinstance(progress, dict) and progress.get("detector_version") == DETECTOR_VERSION:
        return progress
    return None


def _write_progress(conn: sqlite3.Connection, pending: Iterable[str], complete: bool, wal_pending: bool) -> None:
    """Per-item tracking: ``pending`` holds every ``messages#<id>.<col>`` / ``sessions#<id>.<col>``
    not yet masked -- including deferred-live rows, retried on every later run."""
    value = json.dumps({"detector_version": DETECTOR_VERSION, "complete": bool(complete),
                        "pending": sorted(set(pending)), "wal_pending": bool(wal_pending)})
    conn.execute("INSERT INTO state_meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_PROGRESS_KEY, value))


def _msg_item(msg_id: int, col: str) -> str:
    return f"messages#{msg_id}.{col}"


def _session_item(sid: str, col: str) -> str:
    return f"sessions#{sid}.{col}"


@dataclass
class _DbPlan:
    rows: List[Tuple[int, str, str, Counter]] = field(default_factory=list)  # (id, session, col, counts)
    session_rows: List[Tuple[str, str, Counter]] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)  # progress items of deferred-live rows
    previous: Optional[dict] = None  # progress stored by an earlier run

    @property
    def has_rows(self) -> bool:
        return bool(self.rows or self.session_rows)

    def needs_housekeeping(self) -> bool:
        """An earlier run left items pending, an unfinished WAL checkpoint, or a stale record."""
        prev = self.previous
        if prev is None:
            return bool(self.deferred)
        return (prev.get("complete") is not True or bool(prev.get("wal_pending"))
                or set(prev.get("pending") or []) != set(self.deferred))

    def row_spec(self) -> dict:
        spec = {}
        if self.rows:
            spec["messages"] = (_MSG_COLUMNS, sorted({r[0] for r in self.rows}))
        if self.session_rows:
            spec["sessions"] = (_SESSION_COLUMNS, sorted({r[0] for r in self.session_rows}))
        return spec


def _scan_db(conn: sqlite3.Connection, db: _DbUnit, cfg: SecretHygieneConfig, include_optouts: bool,
             report: ScrubReport) -> _DbPlan:
    """Scan EVERY row (no watermark): masked rows never re-match, so a rescan is the resume."""
    plan = _DbPlan(previous=_read_progress(conn))
    cols = ", ".join(_MSG_COLUMNS)
    last = 0
    while True:
        rows = conn.execute(f"SELECT id, session_id, {cols} FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                            (last, BATCH_SIZE)).fetchall()
        if not rows:
            break
        for row in rows:
            msg_id, sid, values = int(row[0]), str(row[1]), row[2:]
            optout = _is_optout(values[_MSG_COLUMNS.index("display_metadata")])
            for col, value in zip(_MSG_COLUMNS, values):
                _, counts = mask_stored_text(value, config=cfg, dry_run=True)
                if not counts:
                    continue
                locator = f"{db.locator}:messages#{msg_id}.{col}"
                if sid in db.live:
                    report.add("state-db", locator, counts, "deferred-live", f"session {sid}: {db.live[sid]}")
                    plan.deferred.append(_msg_item(msg_id, col))
                elif optout and not include_optouts:
                    report.add("state-db", locator, counts, "skipped-optout", f"session {sid}")
                else:
                    plan.rows.append((msg_id, sid, col, counts))
        last = int(rows[-1][0])
    for row in conn.execute(f"SELECT id, {', '.join(_SESSION_COLUMNS)} FROM sessions ORDER BY id"):
        sid = str(row[0])
        for col, value in zip(_SESSION_COLUMNS, row[1:]):
            _, counts = mask_stored_text(value, config=cfg, dry_run=True)
            if not counts:
                continue
            locator = f"{db.locator}:sessions#{sid}.{col}"
            if sid in db.live:
                report.add("state-db", locator, counts, "deferred-live", f"session {sid}: {db.live[sid]}")
                plan.deferred.append(_session_item(sid, col))
            else:
                plan.session_rows.append((sid, col, counts))
    return plan


def _apply_db(db: _DbUnit, plan: _DbPlan, cfg: SecretHygieneConfig, key_provider: Optional[TagKeyProvider],
              report: ScrubReport, vacuum: bool, holders_fn: Callable[[Path], list]) -> None:
    from hermes_state_repair import _connect_repair_durable

    conn = _connect_repair_durable(db.path)
    try:
        _load_cjk(conn, db.home)
        conn.execute("PRAGMA secure_delete=ON")
        prev = _read_progress(conn)
        wal_pending = bool(prev and prev.get("wal_pending"))
        pending: Set[str] = set(plan.deferred)
        pending |= {_msg_item(msg_id, col) for msg_id, _, col, _ in plan.rows}
        pending |= {_session_item(sid, col) for sid, col, _ in plan.session_rows}
        if not plan.has_rows:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write_progress(conn, pending, complete=True, wal_pending=wal_pending)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if wal_pending:
                _checkpoint_wal(conn, db, report)
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_progress(conn, pending, complete=False, wal_pending=True)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        by_id: Dict[int, List[Tuple[str, str, Counter]]] = {}
        for msg_id, sid, col, counts in plan.rows:
            by_id.setdefault(msg_id, []).append((sid, col, counts))
        ids = sorted(by_id)
        batch_no = 0
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start:start + BATCH_SIZE]
            batch_no += 1
            done: Set[str] = set()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for msg_id in chunk:
                    for sid, col, _ in by_id[msg_id]:
                        item = _msg_item(msg_id, col)
                        old = conn.execute(f"SELECT {col} FROM messages WHERE id = ?", (msg_id,)).fetchone()
                        if old is None:
                            done.add(item)  # row deleted since the scan: nothing left to mask
                            continue
                        new, counts = mask_stored_text(old[0], config=cfg, key_provider=key_provider)
                        if not counts or new == old[0]:
                            done.add(item)
                            continue
                        cur = conn.execute(f"UPDATE messages SET {col} = ? WHERE id = ? AND {col} IS ?",
                                           (new, msg_id, old[0]))
                        locator = f"{db.locator}:messages#{msg_id}.{col}"
                        if cur.rowcount == 1:
                            report.add("state-db", locator, counts, "masked", f"session {sid}")
                            done.add(item)
                        else:
                            report.warn(f"{locator}: changed concurrently; retried next run")
                _write_progress(conn, pending - done, complete=False, wal_pending=True)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            pending -= done
            _after_batch_commit(batch_no)
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = set()
            for sid, col, _ in plan.session_rows:
                item = _session_item(sid, col)
                old = conn.execute(f"SELECT {col} FROM sessions WHERE id = ?", (sid,)).fetchone()
                if old is None:
                    done.add(item)
                    continue
                new, counts = mask_stored_text(old[0], config=cfg, key_provider=key_provider)
                if not counts or new == old[0]:
                    done.add(item)
                    continue
                cur = conn.execute(f"UPDATE sessions SET {col} = ? WHERE id = ? AND {col} IS ?",
                                   (new, sid, old[0]))
                if cur.rowcount == 1:
                    report.add("state-db", f"{db.locator}:sessions#{sid}.{col}", counts, "masked")
                    done.add(item)
                else:
                    report.warn(f"{db.locator}:sessions#{sid}.{col}: changed concurrently; retried next run")
            _write_progress(conn, pending - done, complete=True, wal_pending=True)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _finish_db(conn, db, report, vacuum, holders_fn)
    finally:
        conn.close()


def _checkpoint_wal(conn: sqlite3.Connection, db: _DbUnit, report: ScrubReport) -> bool:
    """PASSIVE checkpoint; on anything short of complete, fail the run with the finish-later
    message and leave ``wal_pending`` set so the next ``--apply`` retries it."""
    try:
        busy, log, checkpointed = _wal_checkpoint(conn)
    except sqlite3.Error as exc:
        report.warn(f"{db.locator}: wal_checkpoint failed ({exc.__class__.__name__})")
        busy, log, checkpointed = 1, 1, 0
    if busy or (log >= 0 and checkpointed != log):
        report.error(WAL_INCOMPLETE_MSG)
        return False
    progress = _read_progress(conn) or {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        _write_progress(conn, progress.get("pending") or [], complete=progress.get("complete") is not False,
                        wal_pending=False)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def _finish_db(conn: sqlite3.Connection, db: _DbUnit, report: ScrubReport, vacuum: bool,
               holders_fn: Callable[[Path], list]) -> None:
    tables = _tables(conn)
    for fts in _FTS_TABLES:
        if fts not in tables:
            continue
        for command in ("integrity-check", "optimize"):
            try:
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES (?)", (command,))
            except sqlite3.Error as exc:
                report.warn(f"{db.locator}: {fts} {command} failed ({exc.__class__.__name__})")
    checkpointed = _checkpoint_wal(conn, db, report)
    problems = _integrity_problems(conn)
    if problems:
        report.error(f"{db.locator}: PRAGMA integrity_check failed after the scrub: {problems[0][:200]}")
        return
    if vacuum:
        if not checkpointed or holders_fn(db.path):
            report.warn(f"{db.locator}: --vacuum skipped, another process holds the database")
        else:
            conn.execute("VACUUM")


# ---------------------------------------------------------------------------
# Ledgers (locators only, never values)
# ---------------------------------------------------------------------------

def _ledger_dir(root: Path) -> Path:
    path = root / "secret-scrub"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write_json_private(path: Path, payload: dict) -> None:
    _atomic_rewrite(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _write_ledgers(root: Path, report: ScrubReport, dbs: Sequence[_DbUnit], deferred_files: Dict[str, str],
                   pending_files: Iterable[str] = (), complete: bool = True) -> None:
    """``deferred.json`` + ``progress.json``; per-file ``pending`` = deferred or not yet masked."""
    def r(value):
        return redact_report_text(value, report._trusted())

    ledger = _ledger_dir(root)
    sessions = [{"db": r(db.locator), "session_id": r(sid), "reason": r(reason)}
                for db in dbs for sid, reason in sorted(db.live.items())]
    now = bk.utc_now()
    _write_json_private(ledger / "deferred.json", {
        "updated_at": now, "sessions": sessions,
        "files": [{"locator": r(loc), "reason": r(why)} for loc, why in sorted(deferred_files.items())]})
    masked = {i.locator for i in report.items if i.action == "masked"}
    pending = sorted({r(loc) for loc in pending_files} | {r(loc) for loc in deferred_files} - masked)
    _write_json_private(ledger / "progress.json", {
        "updated_at": now, "detector_version": DETECTOR_VERSION, "backup": r(report.backup_path),
        "exit_code": report.exit_code, "complete": bool(complete), "pending_files": pending,
        "masked": sorted(masked)})


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def run_scrub(*, root: Path, profiles: Sequence[Tuple[str, Path]], apply: bool = False,
              targets: Optional[Iterable[str]] = None, include_optouts: bool = False, vacuum: bool = False,
              config: Optional[SecretHygieneConfig] = None, key_provider: Optional[TagKeyProvider] = None,
              claude_config_dir: Optional[Path] = None, holders_fn: Optional[Callable[[Path], list]] = None,
              now: Optional[float] = None, recent_write_grace_s: float = RECENT_WRITE_GRACE_S,
              open_writers_fn: Optional[Callable[[Path], List[int]]] = None) -> ScrubReport:
    """Scan (and with ``apply``, mask) every target. Never raises for expected failures:
    ``report.exit_code`` is 0 ok, 1 error (nothing or partially written; see errors), 2 refused."""
    root = Path(root)
    cfg = config or load_secret_hygiene_config()
    targets_set = set(targets or TARGETS)
    holders_fn = holders_fn or _default_holders
    open_writers_fn = open_writers_fn or _open_writer_pids
    now = bk.utc_now() if now is None else now
    report = ScrubReport(mode="apply" if apply else "dry-run", root=str(root))
    warning = bk.icloud_warning(root)
    if warning:
        report.warn(warning)

    dbs = []
    for name, home in profiles:
        home = Path(home)
        db_path = home / "state.db"
        if db_path.is_file() and not db_path.is_symlink():
            rel = _rel(root, home)
            dbs.append(_DbUnit(name, home, db_path, _rel(root, db_path),
                               "db/state.db" if rel == "." else f"db/{rel}/state.db"))
        elif db_path.is_symlink():
            report.add("state-db", _rel(root, db_path), Counter(), "skipped-symlink", "symlink not followed")

    lock = bk.ScrubLock(root)
    if apply:
        refusal = lock.acquire()
        if refusal:
            report.refused, report.exit_code = refusal, 2
            return report
    try:
        return _run_locked(report, root, profiles, dbs, targets_set, apply, include_optouts, vacuum, cfg,
                           key_provider, claude_config_dir, holders_fn, now, recent_write_grace_s, open_writers_fn)
    finally:
        lock.release()


def _run_locked(report, root, profiles, dbs, targets_set, apply, include_optouts, vacuum, cfg,
                key_provider, claude_config_dir, holders_fn, now, grace_s, open_writers_fn) -> ScrubReport:
    if apply and "state-db" in targets_set:
        for db in dbs:
            refusal = _db_preflight(db)
            if refusal:
                report.refused, report.exit_code = refusal, 2
                return report

    peer_live = _peer_lease_sessions(root)
    live_by_profile: Dict[str, Dict[str, str]] = {}
    for db in dbs:
        try:
            conn = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
            try:
                db.live = _live_sessions(conn, db, root, now, cfg.sweep_live_window_hours, holders_fn, peer_live)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            report.error(f"{db.locator}: cannot read ({exc.__class__.__name__})")
        live_by_profile[db.profile] = db.live

    # ---- files: scan
    units = _collect_root_files(root, profiles, targets_set, report)
    sdk_map: Dict[str, Tuple[str, str]] = {}
    if "sdk-transcripts" in targets_set:
        sdk_map = _sdk_session_map(dbs)
        units += _collect_sdk_files(claude_config_dir, sdk_map, report)
    pending: List[Tuple[_FileUnit, bytes, Counter, _Snap]] = []
    deferred_files: Dict[str, str] = {}
    for unit in units:
        text, raw, skip, snap = _read_unit(unit)
        if skip:
            report.add(unit.target, unit.locator, Counter(), skip)
            continue
        if unit.target == "sdk-transcripts":
            ok, why, owners = _classify_sdk_text(text, sdk_map)
            if not ok:
                report.add(unit.target, unit.locator, Counter(), "ambiguous-skip", why)
                continue
            unit.live_keys = owners
        _, counts = _mask_file_text(text, unit.fmt, cfg, None, dry_run=True)
        if not counts:
            continue
        owners = unit.live_keys or ((unit.profile, unit.session_id or ""),)
        live_reason = next((live_by_profile.get(prof, {}).get(sid) for prof, sid in owners
                            if live_by_profile.get(prof, {}).get(sid)), None)
        if not live_reason and grace_s > 0 and now - snap.mtime_ns / 1e9 < grace_s:
            live_reason = "written in the last few minutes"
        if live_reason:
            deferred_files[unit.locator] = live_reason
            report.add(unit.target, unit.locator, counts, "deferred-live", live_reason)
            continue
        if not apply:
            report.add(unit.target, unit.locator, counts, "would-mask")
            continue
        pending.append((unit, raw, counts, snap))

    # ---- state.db: scan
    plans: Dict[str, _DbPlan] = {}
    if "state-db" in targets_set:
        for db in dbs:
            try:
                conn = sqlite3.connect(f"{db.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
                try:
                    plan = _scan_db(conn, db, cfg, include_optouts, report)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                report.error(f"{db.locator}: scan failed ({exc.__class__.__name__})")
                continue
            if not apply:
                for msg_id, sid, col, counts in plan.rows:
                    report.add("state-db", f"{db.locator}:messages#{msg_id}.{col}", counts, "would-mask",
                               f"session {sid}")
                for sid, col, counts in plan.session_rows:
                    report.add("state-db", f"{db.locator}:sessions#{sid}.{col}", counts, "would-mask")
            elif plan.has_rows or plan.needs_housekeeping():
                plans[db.locator] = plan

    if not apply:
        report.exit_code = 1 if report.errors else 0
        return report
    if not pending and not plans:
        _write_ledgers(root, report, dbs, deferred_files)
        report.exit_code = 1 if report.errors else 0
        return report
    return _apply(report, root, dbs, pending, plans, deferred_files, cfg, key_provider, vacuum, holders_fn,
                  open_writers_fn)


def _apply(report, root, dbs, pending, plans, deferred_files, cfg, key_provider, vacuum, holders_fn,
           open_writers_fn) -> ScrubReport:
    row_dbs = [db for db in dbs if db.locator in plans and plans[db.locator].has_rows]
    expected: Dict[str, str] = {}
    backup = None
    if pending or row_dbs:
        # ---- backup (of the exact bytes/rows planned against), then prove it restores, before ANY write
        try:
            backup = bk.write_backup(
                root,
                files=[(u.locator, u.path, u.backup_rel, raw, _reader_for(u)) for u, raw, _, _ in pending],
                dbs=[(db.locator, db.path, db.backup_rel, plans[db.locator].row_spec()) for db in row_dbs])
        except Exception as exc:
            report.error(f"backup failed, nothing was changed: {exc.__class__.__name__}: {exc}")
            report.exit_code = 1
            return report
        report.backup_path = str(backup.path)
        _after_backup_written(backup.path)
        problems = bk.verify_backup_restores(backup)
        if problems:
            report.error("backup verification failed, nothing was changed: " + "; ".join(problems[:5]))
            report.exit_code = 1
            return report
        expected = {f.locator: f.sha256 for f in backup.files}

    # ---- files
    pending_files = {u.locator for u, _, _, _ in pending}
    _write_ledgers(root, report, dbs, deferred_files, pending_files, complete=False)
    for unit, _, _, snap in pending:
        text, current, skip, now_snap = _read_unit(unit)
        if skip or current is None or now_snap is None:
            report.add(unit.target, unit.locator, Counter(), skip or "skipped-unreadable")
            continue
        if bk.sha256_bytes(current) != expected.get(unit.locator) or not now_snap.same_file_state(snap):
            report.add(unit.target, unit.locator, Counter(), "changed-during-apply",
                       "file changed since the backup; retried next run")
            continue
        masked, counts = _mask_file_text(text, unit.fmt, cfg, key_provider, dry_run=False)
        if not counts:
            pending_files.discard(unit.locator)
            continue
        try:
            outcome, detail = _atomic_rewrite_unit(unit, masked.encode("utf-8"), snap, open_writers_fn)
        except OSError as exc:
            report.error(f"{unit.locator}: rewrite failed ({exc.__class__.__name__})")
            continue
        if outcome != "masked":
            if outcome == "deferred-live":
                deferred_files[unit.locator] = detail
            report.add(unit.target, unit.locator, counts, outcome, detail)
            continue
        pending_files.discard(unit.locator)
        after, _, _, _ = _read_unit(unit)
        _, residue = _mask_file_text(after or "", unit.fmt, cfg, None, dry_run=True)
        if residue:
            report.error(f"{unit.locator}: re-scan still finds {sum(residue.values())} secret(s)")
        report.add(unit.target, unit.locator, counts, "masked")

    # ---- state.db
    for db in dbs:
        plan = plans.get(db.locator)
        if plan is None:
            continue
        try:
            _apply_db(db, plan, cfg, key_provider, report, vacuum, holders_fn)
        except Exception as exc:
            where = f" Backup: {backup.path}" if backup else ""
            report.error(f"{db.locator}: stopped mid-scrub ({exc.__class__.__name__}: {exc}); committed batches "
                         f"are kept, re-run to resume.{where}")
    report.exit_code = 1 if report.errors else 0
    _write_ledgers(root, report, dbs, deferred_files, pending_files)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_profiles(args, root: Path, home: Path) -> Tuple[List[Tuple[str, Path]], Optional[str]]:
    if getattr(args, "all_profiles", False):
        profiles = [("default", root)]
        pdir = root / "profiles"
        if pdir.is_dir():
            profiles += [(p.name, p) for p in sorted(pdir.iterdir()) if p.is_dir() and not p.is_symlink()]
        return profiles, None
    name = getattr(args, "profile", None)
    if name:
        if name == "default":
            return [("default", root)], None
        pdir = root / "profiles" / name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or not pdir.is_dir():
            return [], f"unknown profile: {name}"
        if pdir.is_symlink():
            return [], f"profile {name} is a symlink; refusing to follow it"
        return [(name, root / "profiles" / name)], None
    return [("default" if home.resolve() == root.resolve() else home.name, home)], None


def _parse_targets(raw: Optional[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    if not raw:
        return list(TARGETS), None
    chosen = [t.strip() for t in raw.split(",") if t.strip()]
    bad = [t for t in chosen if t not in TARGETS]
    if bad:
        return None, f"unknown target(s): {', '.join(bad)} (choose from {', '.join(TARGETS)})"
    return chosen, None


def _status(root: Path) -> int:
    ledger = root / "secret-scrub"
    for name in ("progress.json", "deferred.json"):
        path = ledger / name
        print(f"== {name}")
        print(path.read_text(encoding="utf-8") if path.is_file() else "(none)")
    return 0


def _report_path_refusal(path: Path, roots: Sequence[Path]) -> Optional[str]:
    """Why ``--report PATH`` is refused, or ``None``. The report is only ever written to a
    NEW file (never truncating one), never through a symlink, and never inside a scrub root."""
    if path.is_symlink():
        return f"--report {path} is a symlink; refusing to write through it"
    if os.path.lexists(path):
        return f"--report {path} already exists; refusing to overwrite it (choose a new file name)"
    parent = Path(os.path.realpath(path.parent))
    if not parent.is_dir():
        return f"--report directory {path.parent} does not exist"
    resolved = parent / path.name
    for base in roots:
        real = Path(os.path.realpath(base))
        if resolved == real or real in resolved.parents:
            return (f"--report {path} is inside a scrub target ({base}); write the report outside "
                    "HERMES_HOME and the Claude projects directory")
    return None


def _write_report_file(path: Path, payload: dict) -> Optional[str]:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
    except OSError as exc:
        return f"--report {path}: not written ({exc.__class__.__name__}); it must be a new file"
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return None


def cmd_security_scrub(args, *, config: Optional[SecretHygieneConfig] = None,
                       key_provider: Optional[TagKeyProvider] = None) -> int:
    """Entry point for ``hermes security scrub``; returns the process exit code."""
    import sys

    from hermes_constants import get_default_hermes_root, get_hermes_home

    home = Path(get_hermes_home())
    root = Path(get_default_hermes_root())
    if getattr(args, "status", False):
        return _status(root)
    profiles, err = _resolve_profiles(args, root, home)
    targets, terr = _parse_targets(getattr(args, "targets", None))
    if err or terr:
        print(err or terr, file=sys.stderr)
        return 2
    claude_env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    claude_dir = Path(claude_env).expanduser() if claude_env else Path.home() / ".claude"
    report_path = getattr(args, "report", None)
    if report_path:
        report_path = Path(report_path).expanduser().absolute()
        refusal = _report_path_refusal(report_path, [root, *(h for _, h in profiles), claude_dir / "projects"])
        if refusal:
            print(redact_report_text(refusal), file=sys.stderr)
            return 2
    report = run_scrub(root=root, profiles=profiles, apply=bool(getattr(args, "apply", False)),
                       targets=targets, include_optouts=bool(getattr(args, "include_optouts", False)),
                       vacuum=bool(getattr(args, "vacuum", False)), config=config, key_provider=key_provider,
                       claude_config_dir=claude_dir, holders_fn=_default_holders)
    if getattr(args, "json", False):
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.render_text())
    if report_path:
        failure = _write_report_file(report_path, report.to_json())
        if failure:
            print(redact_report_text(failure), file=sys.stderr)
            return report.exit_code or 1
    return report.exit_code
