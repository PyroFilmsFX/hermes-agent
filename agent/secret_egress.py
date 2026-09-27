"""Egress secret masking (HE-SECRET-HYGIENE S4, design §5.5).

Every outbound copy of user content is masked with the S1 detector
(``agent.redact_detect.find_secrets``) before its bytes leave the process or are
written for sharing: cntrl_sync payloads, Skill Sync blobs, the Hugging Face trace
upload, ``hermes debug share``, ``/save``, the ``hermes backup`` archive, profile
export and kanban board export. Secrets become the same ``[REDACTED:<kind>:<tag>]``
placeholder the ingest edges write, so an egress copy of already-masked content is
byte-identical (the masker is idempotent).

Egress fails CLOSED: a masking error raises :class:`EgressMaskError` and the caller
must not send or publish. A tag-key failure is not an error: masking falls back to a
process-local key, because an unmasked upload is worse than an unstable tag.

Only staged or in-memory copies are ever rewritten; live files and databases are
read, never modified. File modes and times of rewritten staged files are preserved.
Findings are counted by kind; no secret value is ever logged, printed or raised.

Config: ``security.secret_hygiene.enabled`` (default true) in config.yaml;
disabling secret hygiene turns off egress masking with the rest of the feature.
"""

from __future__ import annotations

import logging
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from agent import secret_hygiene as _sh
from agent.redact_detect import PLACEHOLDER_RE

logger = logging.getLogger(__name__)

__all__ = [
    "EgressMaskError", "EgressMasker", "STATE_DB_COLUMNS", "egress_config", "legacy_redact",
    "mask_egress_text", "mask_egress_value",
]

# state.db cells that hold conversation text (design §5.1 ``state-db`` target).
STATE_DB_COLUMNS: Dict[str, tuple] = {
    "messages": ("content", "api_content", "tool_calls", "reasoning", "reasoning_content", "display_metadata"),
    "sessions": ("title", "last_activity_description"),
}
# FTS backfill markers: a full 'rebuild' leaves every index complete, so they are cleared.
_FTS_REBUILD_KEYS = ("fts_rebuild_progress", "fts_rebuild_high_water",
                     "fts_cjk_rebuild_progress", "fts_cjk_rebuild_high_water")
_BATCH = 500
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ENV_ASSIGNMENT_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_.-]*)(\s*=\s*)(.*?)(\r?\n)?$")


class EgressMaskError(RuntimeError):
    """Masking an outbound copy failed; the copy must not leave. Never carries a secret value."""


def egress_config() -> Optional[_sh.SecretHygieneConfig]:
    """The live config when egress masking is on, else ``None``."""
    cfg = _sh.load_secret_hygiene_config()
    return cfg if cfg.enabled else None


class _EgressKeyProvider:
    """The install tag key, or a process-local key when it cannot be loaded (masking must happen)."""

    def __init__(self) -> None:
        self._key: Optional[bytes] = None

    def get_key(self) -> bytes:
        if self._key is None:
            try:
                self._key = _sh._get_provider().get_key()
            except (OSError, ValueError, subprocess.SubprocessError):
                logger.warning("secret_hygiene: tag key unavailable for egress; masking with a process-local key")
                self._key = _sh._EphemeralTagKeyProvider().get_key()
        return self._key


def legacy_redact(text: Any) -> Any:
    """``redact_sensitive_text(force=True)`` on the text BETWEEN placeholders.

    The log redactor predates the placeholder and would re-mask one sitting under a
    keyword key (``PGPASSWORD=[REDAC...b48]``); splitting around placeholders keeps them
    whole while the redactor still covers its extra sources (vault exact values).
    """
    if not isinstance(text, str) or not text:
        return text
    from agent.redact import redact_sensitive_text

    out, pos = [], 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            out.append(redact_sensitive_text(text[pos:m.start()], force=True))
        out.append(m.group(0))
        pos = m.end()
    if pos < len(text) or not out:
        out.append(redact_sensitive_text(text[pos:], force=True))
    return "".join(out)


class EgressMasker:
    """One outbound operation: config and tag key resolved once, kind counts accumulated.

    ``active`` is False when egress masking is off in config; every method is then a
    pass-through. Any detector failure raises :class:`EgressMaskError`.
    """

    def __init__(self, surface: str, *, config: Optional[_sh.SecretHygieneConfig] = None):
        self.surface = surface
        try:
            self.config = config if config is not None else egress_config()
        except Exception as exc:  # load_secret_hygiene_config is lenient; this is belt and braces
            raise EgressMaskError(f"secret masking config unreadable for {surface} ({type(exc).__name__})") from None
        self.counts: Counter = Counter()
        self._kp = _EgressKeyProvider()

    @property
    def active(self) -> bool:
        return self.config is not None and self.config.enabled

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def _fail(self, exc: BaseException) -> EgressMaskError:
        logger.error("secret_hygiene: egress masking failed for %s (%s); nothing was sent",
                     self.surface, type(exc).__name__)
        return EgressMaskError(f"secret masking failed for {self.surface} ({type(exc).__name__}); "
                               "the outbound copy was blocked")

    # -- values ------------------------------------------------------------

    def text(self, value: Any, *, key: Optional[str] = None) -> Any:
        """Mask free text with every rule (prefix tokens, DB URLs, assignments, entropy)."""
        if not self.active or not isinstance(value, str) or not value:
            return value
        if key:
            return self._keyed_text(value, key)
        lines = value.splitlines(keepends=True)
        if len(lines) > 1:
            return "".join(self.text(line) for line in lines)
        if match := _ENV_ASSIGNMENT_RE.match(value):
            prefix, env_key, equals, raw, newline = match.groups()
            candidate = raw.strip().strip("\"'")
            if candidate:
                masked = self._keyed_text(candidate, env_key)
                if masked != candidate:
                    return f"{prefix}{env_key}{equals}{masked}{newline or ''}"
        try:
            masked, findings = _sh.mask_secrets_for_ingest(value, config=self.config, key_provider=self._kp)
        except Exception as exc:
            raise self._fail(exc) from None
        for f in findings:
            self.counts[f.kind] += f.count
        return masked

    def _keyed_text(self, value: str, key: str) -> str:
        """Apply the detector's structured key rules to one value."""
        try:
            probe = f"{key}={value}"
            masked, findings = _sh.mask_secrets_for_ingest(
                probe, config=self.config, key_provider=self._kp)
        except Exception as exc:
            raise self._fail(exc) from None
        for finding in findings:
            self.counts[finding.kind] += finding.count
        return masked[len(key) + 1:]

    def stored(self, value: Any) -> Any:
        """Mask a stored cell/document, JSON-aware (never corrupts serialized JSON)."""
        if not self.active or not isinstance(value, str) or not value:
            return value
        try:
            masked, counts = _sh.mask_stored_text(value, config=self.config, key_provider=self._kp)
        except Exception as exc:
            raise self._fail(exc) from None
        self.counts.update(counts)
        return masked

    def value(self, node: Any) -> Any:
        """Recursively mask the string leaves of a dict/list/tuple (keys are left alone)."""
        return self._value_with_key(node)

    def _value_with_key(self, node: Any, key: Optional[str] = None) -> Any:
        if isinstance(node, str):
            if key == "display_metadata":
                return self.stored(node)
            return self.text(node, key=key)
        if isinstance(node, list):
            return [self._value_with_key(v, key) for v in node]
        if isinstance(node, tuple):
            return tuple(self._value_with_key(v, key) for v in node)
        if isinstance(node, dict):
            res = {}
            for k, v in node.items():
                if key == "owner_grant" and k == "envelope" and _sh.is_clean_owner_grant_envelope(v):
                    res[k] = v
                else:
                    res[k] = self._value_with_key(v, k if isinstance(k, str) else None)
            return res
        return node

    def payload(self, data: bytes, name: str = "", path: Optional[Union[str, Path]] = None) -> bytes:
        """Mask a text-like file payload; binary (images, PDFs, archives, DBs) passes through."""
        if not self.active or not _sh.is_text_payload(data):
            return data
        text = bytes(data).decode("utf-8")
        if os.path.splitext(name or str(path or ""))[1].lower() == ".json":
            # A signed grant file stays byte-identical so it still verifies, but only when clean.
            try:
                if _sh.is_clean_owner_grant_envelope(json.loads(text)):
                    return data
            except ValueError:
                pass
        suffix = os.path.splitext(name)[1].lower()
        if suffix == ".jsonl":
            parts = []
            for line in text.splitlines(keepends=True):
                body = line.rstrip("\r\n")
                parts.append(self.stored(body) + line[len(body):])
            masked = "".join(parts)
        elif suffix == ".json":
            masked = self.stored(text)
        else:
            masked = self.text(text)
        return data if masked == text else masked.encode("utf-8")

    # -- staged files --------------------------------------------------------

    def file_in_place(self, path: Path) -> bool:
        """Mask a STAGED copy in place (temp + ``os.replace``, mode and times kept).

        A symlink whose target is a regular file is materialized as a masked regular file
        only when something changed, so masking never writes through a link back into the
        source tree. Returns True when the file was rewritten.
        """
        if not self.active:
            return False
        path = Path(path)
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return False
            data = path.read_bytes()
        except OSError as exc:
            raise self._fail(exc) from None
        new = self.payload(data, path.name, path=path)
        if new is data or new == data:
            return False
        tmp = path.with_name(f".{path.name}.egress-{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(new)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise self._fail(exc) from None
        return True

    def tree_in_place(self, root: Path, *, select=None) -> int:
        """Mask every selected regular file under a STAGED tree; returns files rewritten."""
        if not self.active or not Path(root).is_dir():
            return 0
        changed = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = Path(dirpath) / name
                if select is not None and not select(path):
                    continue
                if self.file_in_place(path):
                    changed += 1
        return changed

    # -- staged SQLite snapshots -------------------------------------------------

    def sqlite_file(self, db_path: Path, columns: Optional[Mapping[str, Sequence[str]]] = None, *,
                    extension_homes: Sequence[Path] = ()) -> int:
        """Mask text cells of a STAGED SQLite snapshot; returns cells rewritten.

        ``columns`` is retained for callers on older releases; masking inspects the staged
        schema and covers every stored text cell in every ordinary table. With
        ``secure_delete`` on, every FTS5 index is rebuilt from the masked content, the file
        is VACUUMed and the WAL truncated, so no freed page, index segment or WAL frame keeps
        the raw value. ``extension_homes`` are HERMES_HOMEs to load the cjk tokenizer from.
        """
        if not self.active:
            return 0
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            tables = {str(r[0]): str(r[1] or "") for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
            fts = [n for n, sql in tables.items() if "USING FTS5" in sql.upper().replace("  ", " ")]
            if any("cjk_unicode61" in tables[n] for n in fts) and not _load_cjk(conn, extension_homes):
                raise EgressMaskError(f"{self.surface}: the cjk_unicode61 FTS tokenizer is unavailable, "
                                      "so the search index cannot be rebuilt masked")
            for n in fts:
                if "content=''" in tables[n].replace(" ", "").replace('"', "'"):
                    raise EgressMaskError(f"{self.surface}: contentless FTS table cannot be rebuilt masked")
            # Inspect the staged database itself. State DB schemas evolve faster
            # than this masker; a curated list silently leaves newer text fields
            # (including JSON columns) untouched in an outbound snapshot.
            plan = _generic_columns(conn, tables, fts)
            conn.execute("PRAGMA secure_delete=ON")
            changed = 0
            conn.execute("BEGIN IMMEDIATE")
            for table, cols in plan.items():
                if table not in tables:
                    continue
                present = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
                for col in cols:
                    if col in present:
                        changed += self._mask_column(conn, table, col)
            if changed:
                for name in fts:
                    conn.execute(f'INSERT INTO "{name}"("{name}") VALUES (\'rebuild\')')
                if "state_meta" in tables:
                    conn.execute(f"DELETE FROM state_meta WHERE key IN ({','.join('?' * len(_FTS_REBUILD_KEYS))})",
                                 _FTS_REBUILD_KEYS)
            conn.execute("COMMIT")
            if changed:
                problems = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
                if problems != ["ok"]:
                    raise EgressMaskError(f"{self.surface}: masked snapshot failed its integrity check")
                conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return changed
        except EgressMaskError:
            _rollback(conn)
            raise
        except Exception as exc:
            _rollback(conn)
            raise self._fail(exc) from None
        finally:
            if conn is not None:
                conn.close()

    def _mask_column(self, conn: sqlite3.Connection, table: str, col: str) -> int:
        changed, last = 0, None
        select = f'SELECT rowid, "{col}" FROM "{table}" WHERE typeof("{col}") = \'text\''
        while True:
            if last is None:
                rows = conn.execute(f"{select} ORDER BY rowid LIMIT {_BATCH}").fetchall()
            else:
                rows = conn.execute(f"{select} AND rowid > ? ORDER BY rowid LIMIT {_BATCH}", (last,)).fetchall()
            if not rows:
                return changed
            for rowid, value in rows:
                new = self.stored(value)
                if new != value:
                    conn.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (new, rowid))
                    changed += 1
            last = rows[-1][0]

    def log_summary(self) -> None:
        if self.counts:
            logger.info("secret_hygiene: masked %d secret(s) in %s (%s)", self.total, self.surface,
                        ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))


def _rollback(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None and conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _generic_columns(conn: sqlite3.Connection, tables: Mapping[str, str], fts: Sequence[str]) -> Dict[str, tuple]:
    """Every column of every ordinary table, excluding only FTS internals."""
    plan: Dict[str, tuple] = {}
    for table, sql in tables.items():
        if table.startswith("sqlite_") or table in fts or "VIRTUAL TABLE" in sql.upper():
            continue
        if any(table.startswith(f"{name}_") for name in fts):
            continue
        cols = tuple(str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")'))
        if cols:
            plan[table] = cols
    return plan


def _load_cjk(conn: sqlite3.Connection, homes: Sequence[Path]) -> bool:
    for home in homes:
        so = Path(home) / "lib" / "libfts5_cjk.so"
        if so.is_file():
            try:
                conn.enable_load_extension(True)
                try:
                    conn.load_extension(str(so))
                finally:
                    conn.enable_load_extension(False)
                return True
            except Exception:
                continue
    try:
        from hermes_state_fts import load_fts5_cjk_extension

        return bool(load_fts5_cjk_extension(conn))
    except Exception:
        return False


def mask_egress_text(text: Any, *, surface: str, legacy: bool = False,
                     config: Optional[_sh.SecretHygieneConfig] = None) -> Any:
    """Mask one outbound string (fail closed); ``legacy`` then runs the log redactor around it."""
    masker = EgressMasker(surface, config=config)
    out = masker.text(text)
    masker.log_summary()
    return legacy_redact(out) if legacy else out


def mask_egress_value(value: Any, *, surface: str, config: Optional[_sh.SecretHygieneConfig] = None) -> Any:
    """Mask the string leaves of an outbound JSON-like value (fail closed)."""
    masker = EgressMasker(surface, config=config)
    out = masker.value(value)
    masker.log_summary()
    return out
