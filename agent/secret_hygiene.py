"""Ingest secret masking (HE-SECRET-HYGIENE S1).

``mask_secrets_for_ingest(text)`` replaces every secret span found by
``agent.redact_detect.find_secrets`` with ``[REDACTED:<kind>:<tag>]``, where
``tag`` is the first N hex chars of ``HMAC-SHA256(tag_key, kind || 0x00 || secret)``.
The same secret always gets the same tag under one install's key, and the
placeholder is never re-matched, so masking is deterministic and idempotent:
``mask(mask(x)) == mask(x)`` byte for byte. Host handoffs that compare staged
content for equality (turn_context ``_stage_turn_user_message``,
session_workdir ``_adopt_submit_user_row``) depend on that.

Findings carry kind, count and spans only; the secret value never leaves this
module. Configuration comes from ``security.secret_hygiene`` in config.yaml
(no env vars). The tag key lives in the macOS login Keychain, or a 0600 file in
the reveal dir when the Keychain is unavailable; tests inject a fixed key with
``StaticTagKeyProvider``.

The functions here are pure (no I/O besides the one-time key load), so the
sweep (``hermes security scrub``) and future egress paths reuse them.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from agent import redact as _redact
from agent.redact_detect import (
    DETECTOR_VERSION,
    PLACEHOLDER_RE,
    DetectOptions,
    SecretFinding,
    find_secrets,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DETECTOR_VERSION", "PLACEHOLDER_RE", "FileTagKeyProvider", "IngestFinding",
    "SecretHygieneConfig", "StaticTagKeyProvider", "TagKeyProvider", "default_reveal_dir",
    "load_secret_hygiene_config", "mask_json_value", "mask_secrets", "mask_secrets_for_ingest",
    "mask_stored_text", "scan_secrets", "set_tag_key_provider",
    "ingress_config", "ingress_kind_counts", "ingress_secret_tags", "is_text_payload",
    "mask_ingress_bytes", "mask_ingress_content", "mask_ingress_text", "mask_tool_result_text",
    "take_optout_tags",
    "write_private_bytes",
]

KEYCHAIN_SERVICE = "hermes-secret-hygiene-tag"
KEYCHAIN_ACCOUNT = "tag-key"
_TAG_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Config (config.yaml ``security.secret_hygiene``)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecretHygieneConfig:
    enabled: bool = True
    mask_ingress: bool = True
    db_url_mask: str = "password"
    tag_hex_chars: int = 8
    code_fence_mode: str = "known_only"
    entropy_enabled: bool = True
    entropy_min_length: int = 24
    entropy_min_bits: float = 4.0
    entropy_min_classes: int = 3
    entropy_bare_strings: bool = False
    allow_patterns: Tuple[str, ...] = ()
    allow_value_sha256: Tuple[str, ...] = ()
    sweep_backup_retention_days: int = 7
    sweep_live_window_hours: float = 24.0
    optout_allowed: bool = True

    def detect_options(self) -> DetectOptions:
        return DetectOptions(
            db_url_mask=self.db_url_mask,
            code_fence_mode=self.code_fence_mode,
            entropy_enabled=self.entropy_enabled,
            entropy_min_length=self.entropy_min_length,
            entropy_min_bits=self.entropy_min_bits,
            entropy_min_classes=self.entropy_min_classes,
            entropy_bare_strings=self.entropy_bare_strings,
            allow_patterns=self.allow_patterns,
            allow_value_sha256=self.allow_value_sha256,
        )


def _load_security_section() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    section = load_config_readonly().get("security") or {}
    return section if isinstance(section, dict) else {}


def _choice(value: Any, allowed: Tuple[str, ...], default: str) -> str:
    value = str(value).strip().lower() if value is not None else default
    return value if value in allowed else default


def _num(value: Any, default, cast, lo, hi):
    try:
        out = cast(value)
    except (TypeError, ValueError):
        return default
    return out if lo <= out <= hi else default


def _strs(value: Any) -> Tuple[str, ...]:
    return tuple(str(v) for v in value if isinstance(v, (str, int))) if isinstance(value, (list, tuple)) else ()


def load_secret_hygiene_config() -> SecretHygieneConfig:
    """Read ``security.secret_hygiene`` leniently; a bad value falls back to its default.

    ``security.redact_secrets: false`` does NOT disable ingest masking; only
    ``secret_hygiene.enabled: false`` does.
    """
    try:
        raw = _load_security_section().get("secret_hygiene") or {}
    except Exception:
        logger.warning("secret_hygiene: config unreadable, using secure defaults", exc_info=True)
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    d = SecretHygieneConfig()
    ent = raw.get("entropy") if isinstance(raw.get("entropy"), dict) else {}
    sweep = raw.get("sweep") if isinstance(raw.get("sweep"), dict) else {}
    optout = raw.get("optout") if isinstance(raw.get("optout"), dict) else {}
    return SecretHygieneConfig(
        enabled=raw.get("enabled", d.enabled) is not False,
        mask_ingress=raw.get("mask_ingress", d.mask_ingress) is not False,
        db_url_mask=_choice(raw.get("db_url_mask"), ("password", "whole_url"), d.db_url_mask),
        tag_hex_chars=_num(raw.get("tag_hex_chars", d.tag_hex_chars), d.tag_hex_chars, int, 8, 64),
        code_fence_mode=_choice(raw.get("code_fence_mode"), ("known_only", "full", "off"), d.code_fence_mode),
        entropy_enabled=ent.get("enabled", d.entropy_enabled) is not False,
        entropy_min_length=_num(ent.get("min_length", d.entropy_min_length), d.entropy_min_length, int, 8, 4096),
        entropy_min_bits=_num(ent.get("min_bits_per_char", d.entropy_min_bits), d.entropy_min_bits, float, 1.0, 8.0),
        entropy_min_classes=_num(ent.get("min_char_classes", d.entropy_min_classes), d.entropy_min_classes, int, 1, 4),
        entropy_bare_strings=ent.get("bare_strings", d.entropy_bare_strings) is True,
        allow_patterns=_strs(raw.get("allow_patterns")),
        allow_value_sha256=tuple(s.lower() for s in _strs(raw.get("allow_value_sha256"))),
        sweep_backup_retention_days=_num(sweep.get("backup_retention_days", 7), 7, int, 1, 3650),
        sweep_live_window_hours=_num(sweep.get("live_window_hours", 24), 24.0, float, 0.0, 24 * 365),
        optout_allowed=optout.get("allowed", d.optout_allowed) is not False,
    )


# ---------------------------------------------------------------------------
# Tag key (§3.1): Keychain, then a 0600 file in the reveal dir; injectable for tests
# ---------------------------------------------------------------------------

class TagKeyProvider(Protocol):
    def get_key(self) -> bytes: ...


class StaticTagKeyProvider:
    """A fixed key (tests, or a caller that already resolved one)."""

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ValueError("tag key must be at least 16 bytes")
        self._key = bytes(key)

    def get_key(self) -> bytes:
        return self._key


def default_reveal_dir() -> Path:
    """Per-user dir OUTSIDE HERMES_HOME (never synced, backed up or exported with a profile)."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Hermes" / "secret-reveal"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "Hermes" / "secret-reveal"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / "hermes" / "secret-reveal"


def _keychain_read() -> Optional[bytes]:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = result.stdout.strip() if result.returncode == 0 else ""
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == _TAG_KEY_BYTES else None


def _keychain_write(key: bytes) -> bool:
    """Store via ``security -i`` with the value hex-encoded on stdin (never on argv)."""
    payload = key.hex().encode("ascii").hex()
    line = f'add-generic-password -U -a "{KEYCHAIN_ACCOUNT}" -s "{KEYCHAIN_SERVICE}" -X {payload}\n'
    try:
        result = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class FileTagKeyProvider:
    """Keychain (macOS) first, else ``<reveal_dir>/tag.key`` (32 bytes, 0600, dir 0700)."""

    def __init__(self, reveal_dir: Optional[Path] = None, *, use_keychain: Optional[bool] = None):
        self._dir = Path(reveal_dir) if reveal_dir is not None else default_reveal_dir()
        self._use_keychain = (sys.platform == "darwin") if use_keychain is None else use_keychain
        self._key: Optional[bytes] = None
        self._lock = threading.Lock()

    def _file_key(self) -> bytes:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._dir, 0o700)
        path = self._dir / "tag.key"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read()
            if len(data) == _TAG_KEY_BYTES:
                return data
            raise ValueError("tag key file has the wrong length")
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(_TAG_KEY_BYTES)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        return key

    def get_key(self) -> bytes:
        with self._lock:
            if self._key is None:
                key = _keychain_read() if self._use_keychain else None
                if key is None and self._use_keychain:
                    key = secrets.token_bytes(_TAG_KEY_BYTES)
                    if not _keychain_write(key) or _keychain_read() != key:
                        key = None
                self._key = key if key is not None else self._file_key()
            return self._key


_provider_lock = threading.Lock()
_default_provider: Optional[TagKeyProvider] = None


def set_tag_key_provider(provider: Optional[TagKeyProvider]) -> None:
    """Override the process-wide key provider (``None`` restores the default)."""
    global _default_provider
    with _provider_lock:
        _default_provider = provider


def _get_provider() -> TagKeyProvider:
    global _default_provider
    with _provider_lock:
        if _default_provider is None:
            _default_provider = FileTagKeyProvider()
        return _default_provider


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestFinding:
    """Per-kind result: how many secrets were masked and where (offsets into the INPUT)."""

    kind: str
    count: int
    spans: Tuple[Tuple[int, int], ...] = field(default=())


def tag_for(key: bytes, kind: str, secret: str, hex_chars: int = 8) -> str:
    digest = hmac.new(key, kind.encode("ascii") + b"\x00" + secret.encode("utf-8", "surrogatepass"), sha256)
    return digest.hexdigest()[:hex_chars]


def _group(findings: List[SecretFinding]) -> Tuple[IngestFinding, ...]:
    by_kind: Dict[str, List[Tuple[int, int]]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append((f.start, f.end))
    return tuple(IngestFinding(kind=k, count=len(v), spans=tuple(v)) for k, v in sorted(by_kind.items()))


def scan_secrets(text: Any, *, config: Optional[SecretHygieneConfig] = None) -> Tuple[IngestFinding, ...]:
    """Detect without masking (no key needed): the dry-run path."""
    cfg = config or load_secret_hygiene_config()
    if not cfg.enabled or not isinstance(text, str) or not text:
        return ()
    return _group(find_secrets(text, cfg.detect_options()))


def mask_secrets_for_ingest(
    text: Any, *, config: Optional[SecretHygieneConfig] = None,
    key_provider: Optional[TagKeyProvider] = None,
    skip_tags: frozenset = frozenset(),
) -> Tuple[Any, Tuple[IngestFinding, ...]]:
    """Mask every secret in ``text``; returns ``(masked_text, findings)``.

    Non-strings and empty strings pass through. Deterministic and idempotent.
    ``skip_tags`` leaves the secrets with those tags raw (the per-message opt-out);
    they are not reported as findings.
    """
    cfg = config or load_secret_hygiene_config()
    if not cfg.enabled or not isinstance(text, str) or not text:
        return text, ()
    found = find_secrets(text, cfg.detect_options())
    if not found:
        return text, ()
    key = (key_provider or _get_provider()).get_key()
    out: List[str] = []
    masked: List[SecretFinding] = []
    pos = 0
    for f in found:
        tag = tag_for(key, f.kind, text[f.start:f.end], cfg.tag_hex_chars)
        if tag in skip_tags:
            continue
        out.append(text[pos:f.start])
        out.append(f"[REDACTED:{f.kind}:{tag}]")
        masked.append(f)
        pos = f.end
    out.append(text[pos:])
    return "".join(out), _group(masked)


mask_secrets = mask_secrets_for_ingest


# ---------------------------------------------------------------------------
# Structured values (JSON transcripts, state.db JSON columns)
# ---------------------------------------------------------------------------

def _merge(counts: Counter, findings: Tuple[IngestFinding, ...]) -> None:
    for f in findings:
        counts[f.kind] += f.count


# Identifier-valued JSON keys (``id``, ``tool_call_id``, ``call_id``, ``session_id``, ``uuid``,
# ``sessionId``, ``parentUuid``): their values are opaque ids, never judged by entropy.
_ID_KEY_RE = re.compile(r"(?:^|[_.\-])(?:id|uuid)$", re.IGNORECASE)
_CAMEL_ID_KEY_RE = re.compile(r"[a-z0-9](?:Id|ID|Uuid|UUID)$")


def _is_id_key(key: str) -> bool:
    key = key.strip()
    return bool(_ID_KEY_RE.search(key) or _CAMEL_ID_KEY_RE.search(key))


def _is_secret_key(key: str) -> bool:
    """The redact keyword list: ``password``, ``api_key``, ``client_secret``, ``token``..."""
    key = key.strip()
    return bool(key) and (_redact._key_has_secret_keyword(key)
                          or _redact._has_word_bounded_keyword(key, _redact._STRONG_KEY_KEYWORD_RE))


def mask_json_value(value: Any, *, config: Optional[SecretHygieneConfig] = None,
                    key_provider: Optional[TagKeyProvider] = None,
                    dry_run: bool = False) -> Tuple[Any, Counter]:
    """Recursively mask string leaves of a decoded JSON value; returns ``(new_value, kind_counts)``.

    Inside structured JSON only pattern-based kinds apply to a leaf (prefix tokens, DB URLs,
    private keys, keyword ``key=value``). The generic entropy rule applies only when the
    leaf's own JSON key is secret-NAMED (``"client_secret": "..."``), judged with that key
    as context since decoding loses it. Values under id keys (``id``, ``*_id``,
    ``tool_call_id``, ``session_id``, ``uuid``...) are masked only when a pattern matches.
    ``dry_run`` counts without needing the tag key.
    """
    cfg = config or load_secret_hygiene_config()
    leaf_cfg = replace(cfg, entropy_enabled=False)
    counts: Counter = Counter()

    def _mask_str(s: str, ctx_key: Optional[str]) -> str:
        if ctx_key is not None and s and not _is_id_key(ctx_key) and _is_secret_key(ctx_key):
            probe = json.dumps({ctx_key: s}, ensure_ascii=False)
            findings = scan_secrets(probe, config=cfg)
            keyed = [f for f in findings for (a, b) in f.spans if probe[a:b] == s]
            if keyed and not scan_secrets(s, config=leaf_cfg):
                kind = keyed[0].kind
                counts[kind] += 1
                if dry_run:
                    return s
                key = (key_provider or _get_provider()).get_key()
                return f"[REDACTED:{kind}:{tag_for(key, kind, s, cfg.tag_hex_chars)}]"
        if dry_run:
            _merge(counts, scan_secrets(s, config=leaf_cfg))
            return s
        masked, findings = mask_secrets_for_ingest(s, config=leaf_cfg, key_provider=key_provider)
        _merge(counts, findings)
        return masked

    def _walk(node: Any, ctx_key: Optional[str] = None) -> Any:
        if isinstance(node, str):
            return _mask_str(node, ctx_key)
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, dict):
            return {k: _walk(v, k if isinstance(k, str) else None) for k, v in node.items()}
        return node

    return _walk(value), counts


def mask_stored_text(text: Any, *, config: Optional[SecretHygieneConfig] = None,
                     key_provider: Optional[TagKeyProvider] = None,
                     dry_run: bool = False) -> Tuple[Any, Counter]:
    """Mask a stored text value, JSON-aware when it is a JSON object/array.

    Raw-text masking of serialized JSON could swallow an escape sequence and corrupt the
    document, so a parseable object/array is decoded, masked leaf by leaf, and re-encoded
    (only when something changed). Anything else is masked as plain text.
    """
    if not isinstance(text, str) or not text:
        return text, Counter()
    cfg = config or load_secret_hygiene_config()
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            new_value, counts = mask_json_value(decoded, config=cfg, key_provider=key_provider, dry_run=dry_run)
            if dry_run or not counts:
                return text, counts
            ascii_only = text.isascii()
            compact = ", " not in text and ": " not in text
            return json.dumps(new_value, ensure_ascii=ascii_only,
                              separators=(",", ":") if compact else None), counts
    counts: Counter = Counter()
    if dry_run:
        _merge(counts, scan_secrets(text, config=cfg))
        return text, counts
    masked, findings = mask_secrets_for_ingest(text, config=cfg, key_provider=key_provider)
    _merge(counts, findings)
    return masked, counts


# ---------------------------------------------------------------------------
# Ingest edges (S2): one entry point per content shape, shared by every host
# ---------------------------------------------------------------------------

class _EphemeralTagKeyProvider:
    """Process-local random key, used only when the install key cannot be loaded.

    Masking still happens (a broken Keychain must never let a secret through); tags are
    stable within the process, which is all the host handoffs compare.
    """

    _key = secrets.token_bytes(_TAG_KEY_BYTES)

    def get_key(self) -> bytes:
        return self._key


def ingress_config() -> Optional[SecretHygieneConfig]:
    """The live config when ingest masking is on, else ``None``."""
    cfg = load_secret_hygiene_config()
    return cfg if cfg.enabled and cfg.mask_ingress else None


def mask_ingress_text(
    text: Any, *, optout_tags: Optional[frozenset] = None,
    config: Optional[SecretHygieneConfig] = None,
) -> Tuple[Any, Tuple[IngestFinding, ...]]:
    """Mask user content at an ingest edge; ``(masked, findings)``.

    Honours ``enabled`` and ``mask_ingress``. Deterministic and idempotent, so an edge and
    the ``build_turn_context`` backstop compose (the backstop is a no-op on masked text).
    ``optout_tags`` leaves those tags raw (a confirmed per-message opt-out, one turn only).
    A tag-key failure falls back to a process-local key; a detector failure is logged
    (never the value) and the text passes through unchanged.
    """
    if not isinstance(text, str) or not text:
        return text, ()
    cfg = config or ingress_config()
    if cfg is None:
        return text, ()
    skip = frozenset(optout_tags or ()) if cfg.optout_allowed else frozenset()
    try:
        return mask_secrets_for_ingest(text, config=cfg, skip_tags=skip)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("secret_hygiene: tag key unavailable; masking with a process-local key")
        try:
            return mask_secrets_for_ingest(text, config=cfg, key_provider=_EphemeralTagKeyProvider(),
                                           skip_tags=skip)
        except Exception:
            logger.error("secret_hygiene: ingest masking failed (%d chars left unmasked)", len(text))
            return text, ()
    except Exception:
        logger.error("secret_hygiene: ingest masking failed (%d chars left unmasked)", len(text))
        return text, ()


def ingress_kind_counts(findings: Tuple[IngestFinding, ...]) -> Dict[str, int]:
    return {f.kind: f.count for f in findings}


def mask_ingress_content(
    content: Any, *, optout_tags: Optional[frozenset] = None,
    config: Optional[SecretHygieneConfig] = None,
) -> Tuple[Any, Dict[str, int]]:
    """Mask a user message's content: a string, or a list of content parts.

    Only text parts (``{"type": "text", "text": ...}`` / bare strings) are masked; image
    and file parts pass through. Returns the SAME object when nothing changed, so identity
    and equality checks downstream behave exactly as before for secret-free input.
    """
    cfg = config or ingress_config()
    if cfg is None:
        return content, {}
    counts: Counter = Counter()
    if isinstance(content, str):
        masked, findings = mask_ingress_text(content, optout_tags=optout_tags, config=cfg)
        return masked, ingress_kind_counts(findings)
    if not isinstance(content, list):
        return content, {}
    out: List[Any] = []
    changed = False
    for part in content:
        if isinstance(part, str):
            new, findings = mask_ingress_text(part, optout_tags=optout_tags, config=cfg)
        elif isinstance(part, dict) and part.get("type") in ("text", "input_text") \
                and isinstance(part.get("text"), str):
            masked, findings = mask_ingress_text(part["text"], optout_tags=optout_tags, config=cfg)
            new = {**part, "text": masked} if masked != part["text"] else part
        else:
            out.append(part)
            continue
        _merge(counts, findings)
        changed = changed or new is not part
        out.append(new)
    return (out if changed else content), dict(counts)


# Magic numbers of binary containers that can decode as UTF-8 by accident.
_BINARY_MAGIC = (b"%PDF", b"PK\x03\x04", b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"\x1f\x8b",
                 b"SQLite format 3", b"\xd0\xcf\x11\xe0")


def is_text_payload(data: bytes) -> bool:
    """Strict UTF-8 without NUL bytes and without a known binary signature."""
    if not isinstance(data, (bytes, bytearray)) or not data or b"\x00" in data:
        return False
    if bytes(data[:16]).startswith(_BINARY_MAGIC):
        return False
    try:
        bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def mask_ingress_bytes(data: bytes) -> Tuple[bytes, Dict[str, int]]:
    """Mask a text-like file payload before it is written; binary passes through unchanged."""
    if ingress_config() is None or not is_text_payload(data):
        return data, {}
    text = bytes(data).decode("utf-8")
    masked, findings = mask_ingress_text(text)
    if not findings:
        return data, {}
    return masked.encode("utf-8"), ingress_kind_counts(findings)


def write_private_bytes(path: Path, data: bytes) -> None:
    """Create ``path`` with mode 0600 (never following a symlink) and write ``data``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def ingress_secret_tags(text: Any, *, config: Optional[SecretHygieneConfig] = None) -> List[Dict[str, str]]:
    """``[{"kind", "tag"}]`` for each distinct secret in ``text`` (never the value)."""
    cfg = config or ingress_config()
    if cfg is None or not isinstance(text, str) or not text:
        return []
    found = find_secrets(text, cfg.detect_options())
    if not found:
        return []
    key = _get_provider().get_key()
    seen: Dict[str, str] = {}
    for f in found:
        seen.setdefault(tag_for(key, f.kind, text[f.start:f.end], cfg.tag_hex_chars), f.kind)
    return [{"kind": kind, "tag": tag} for tag, kind in seen.items()]


def take_optout_tags(holder: Any) -> frozenset:
    """Pop a one-turn opt-out tag set off an agent (or any object); empty when unset."""
    tags = getattr(holder, "_secret_optout_tags", None)
    try:
        holder._secret_optout_tags = frozenset()
    except Exception:
        pass
    return frozenset(tags) if tags else frozenset()


def mask_tool_result_text(text: Any) -> Any:
    """Mask a tool result before it is persisted (SDK-lane CLI-native results bypass Hermes
    tool-output redaction). JSON-aware like the sweep, so the scrub finds nothing new later."""
    if not isinstance(text, str) or not text:
        return text
    cfg = ingress_config()
    if cfg is None:
        return text
    try:
        return mask_stored_text(text, config=cfg)[0]
    except Exception:
        logger.warning("secret_hygiene: tool-result masking failed; falling back to text masking")
        return mask_ingress_text(text, config=cfg)[0]
