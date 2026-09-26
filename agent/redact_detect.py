"""Span-returning secret detector for ingest masking and the at-rest sweep.

``agent/redact.py`` stays the one secret-pattern list: this module reuses its
compiled regexes (vendor prefixes, DB URLs, assignments, JWT, auth headers,
private keys) and adds the ingest-only rules from HE-SECRET-HYGIENE §1/§4:
libpq ``password=`` DSNs, JDBC/URL ``password=`` params, and the generic
high-entropy ``key=value`` rule with its allow-list. It returns spans, never a
rewritten string, so callers choose their own placeholder:
``redact_sensitive_text`` keeps its log format, the ingest masker writes
``[REDACTED:<kind>:<tag>]``.

Findings hold offsets and a kind, never the secret value.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from agent import redact as _r

# Bump when detection changes, so the sweep's resume ledger restarts the scan.
DETECTOR_VERSION = 1

# ``[REDACTED:<kind>:<tag>]``: detectors never match inside one (idempotency).
PLACEHOLDER_PATTERN = r"\[REDACTED:(?P<kind>[a-z0-9-]{2,24}):(?P<tag>[0-9a-f]{4,64})\]"
PLACEHOLDER_RE = re.compile(PLACEHOLDER_PATTERN)
# Older masks from the log redactor: ``«redacted:ghp_…»`` / ``«redacted-secret»``.
_LEGACY_MASK_RE = re.compile(r"«redacted[^»\n]{0,64}»")


@dataclass(frozen=True)
class SecretFinding:
    """One detected secret: ``text[start:end]`` is the secret span to replace."""

    start: int
    end: int
    kind: str
    rule: str  # "known" | "assignment" | "entropy"


@dataclass(frozen=True)
class DetectOptions:
    """Detector knobs; mirrors ``security.secret_hygiene`` in config.yaml."""

    db_url_mask: str = "password"          # password | whole_url
    code_fence_mode: str = "known_only"    # known_only | full | off
    entropy_enabled: bool = True
    entropy_min_length: int = 24
    entropy_min_bits: float = 4.0
    entropy_min_classes: int = 3
    entropy_bare_strings: bool = False
    allow_patterns: Tuple[str, ...] = ()
    allow_value_sha256: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Kind tables
# ---------------------------------------------------------------------------

# First matching prefix wins; order longest/most specific first.
_PREFIX_KINDS: Tuple[Tuple[str, str], ...] = (
    ("sk-ant-", "anthropic-key"),
    ("sk-lf-", "generic-secret"),
    ("sk-", "openai-key"),
    ("github_pat_", "github-token"),
    ("ghp_", "github-token"), ("gho_", "github-token"), ("ghu_", "github-token"),
    ("ghs_", "github-token"), ("ghr_", "github-token"),
    ("FlyV1 ", "fly-token"), ("fm2_", "fly-token"), ("fo1_", "fly-token"),
    ("AKIA", "aws-access-key"), ("ASIA", "aws-access-key"),
    ("xox", "slack-token"), ("xapp-", "slack-token"),
    ("npg_", "db-password"),
)

_DB_SCHEME_KINDS = {
    "postgres": "postgres-url", "postgresql": "postgres-url",
    "mysql": "mysql-url", "mariadb": "mysql-url",
    "mongodb": "mongodb-url",
    "redis": "redis-url", "rediss": "redis-url",
    "amqp": "amqp-url", "amqps": "amqp-url",
}

_DB_KEY_HINT_RE = re.compile(r"PG|DB|SQL|POSTGRES|MYSQL|MARIA|REDIS|MONGO|DATABASE", re.IGNORECASE)
_AWS_SECRET_KEY_RE = re.compile(r"aws.{0,20}secret", re.IGNORECASE)


def _kind_for_prefix(token: str) -> str:
    return next((kind for prefix, kind in _PREFIX_KINDS if token.startswith(prefix)), "generic-secret")


def _kind_for_key(key: str) -> str:
    bare = key.strip("\"'")
    if _AWS_SECRET_KEY_RE.search(bare):
        return "aws-secret-key"
    if _r._has_word_bounded_keyword(bare, _r._PASSWORD_KEY_RE) and _DB_KEY_HINT_RE.search(bare):
        return "db-password"
    return "generic-secret"


# ---------------------------------------------------------------------------
# New ingest-only patterns (every source is checked by the ReDoS structural gate in tests)
# ---------------------------------------------------------------------------

_HOST_AFTER_AT_RE = re.compile(r"[^/\s:?#\"'<>]+")
_URL_REST_RE = re.compile(r"[^\s\"'<>]*")
_ANY_URL_SRC = r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{1,30}://[^\s\"'<>]+"
_JDBC_SRC = r"(?<![A-Za-z0-9])jdbc:[A-Za-z0-9]{1,30}:[^\s\"'<>]+"
_URL_PW_PARAM_SRC = r"[?&;](?:password|passwd|pwd)=([^&;#\s\"'<>]+)"
_LIBPQ_CONTEXT_SRC = r"(?<![\w.])(?:host|hostaddr|dbname|user|port|sslmode|connect_timeout)[ \t]*="
_LIBPQ_PW_SRC = r"(?<![\w.])password[ \t]*=[ \t]*(?:'((?:[^'\\\n]|\\.)*)'|([^\s'\"&;]+))"
_ENTROPY_ASSIGN_SRC = (
    r"(?<![\w.\-])[\"']?([A-Za-z_][\w.\-]{0,63})[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"([A-Za-z0-9_\-]{8,}+={0,2})(?![A-Za-z0-9_\-+/=.:@])"
)
_FENCE_LINE_SRC = r"(?m)^[ \t]{0,3}(?:```|~~~)"

NEW_PATTERN_SOURCES: Tuple[str, ...] = (
    _ANY_URL_SRC, _JDBC_SRC, _URL_PW_PARAM_SRC, _LIBPQ_CONTEXT_SRC, _LIBPQ_PW_SRC,
    _ENTROPY_ASSIGN_SRC, _FENCE_LINE_SRC,
)

_ANY_URL_RE = re.compile(_ANY_URL_SRC)
_JDBC_RE = re.compile(_JDBC_SRC, re.IGNORECASE)
_URL_PW_PARAM_RE = re.compile(_URL_PW_PARAM_SRC, re.IGNORECASE)
_LIBPQ_CONTEXT_RE = re.compile(_LIBPQ_CONTEXT_SRC, re.IGNORECASE)
_LIBPQ_PW_RE = re.compile(_LIBPQ_PW_SRC, re.IGNORECASE)
_ENTROPY_ASSIGN_RE = re.compile(_ENTROPY_ASSIGN_SRC)
_FENCE_LINE_RE = re.compile(_FENCE_LINE_SRC)

# ---------------------------------------------------------------------------
# §4 allow-list
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")
_HEX_DIGEST_LENGTHS = frozenset({40, 64}) | frozenset(range(7, 13))
_DIGEST_PREFIX_RE = re.compile(r"(?:sha1|sha224|sha256|sha384|sha512|md5)[:\-]", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"data:[\w.+\-]{1,64}/[\w.+\-]{1,64};base64,", re.IGNORECASE)
_B64_BLOCK_RE = re.compile(r"[A-Za-z0-9+/=\s]+")
_PLACEHOLDER_VALUE_RE = re.compile(
    r"your[-_ ].{0,40}[-_ ]here|changeme|change[-_]me|example|dummy|placeholder|"
    r"^x{3,}|^<[^>]*>$|^\$\{[^}]*\}$|^\$[A-Za-z_]\w*$|^\{[^{}]*\}$|os\.environ|os\.getenv|process\.env\.",
    re.IGNORECASE,
)
_REPEATED_CHAR_RE = re.compile(r"(.)\1*", re.DOTALL)


def _strong_key(key: str) -> bool:
    return bool(key) and _r._has_word_bounded_keyword(key.strip("\"'"), _r._STRONG_KEY_KEYWORD_RE)


def _is_allowlisted_value(value: str, key: str = "") -> bool:
    """§4 rule 1: values that look like a secret's shape but are not one."""
    if not value:
        return True
    if value == "***" or "«redacted" in value or PLACEHOLDER_RE.search(value):
        return True
    if _REPEATED_CHAR_RE.fullmatch(value) or _PLACEHOLDER_VALUE_RE.search(value):
        return True
    if _DIGEST_PREFIX_RE.match(value) or _DATA_URI_RE.match(value):
        return True
    if len(value) > 1024 and _B64_BLOCK_RE.fullmatch(value) and "PRIVATE" not in value:
        return True
    if not _strong_key(key):
        if _UUID_RE.fullmatch(value):
            return True
        if len(value) in _HEX_DIGEST_LENGTHS and _HEX_RE.fullmatch(value):
            return True
    return False


def shannon_bits_per_char(value: str) -> float:
    """Shannon entropy of ``value`` in bits per character."""
    if not value:
        return 0.0
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in Counter(value).values())


def _char_classes(value: str) -> int:
    return (any(c.islower() for c in value) + any(c.isupper() for c in value)
            + any(c.isdigit() for c in value) + any(not c.isalnum() for c in value))


def passes_entropy_gate(value: str, opts: DetectOptions) -> bool:
    """§4 rule 4: long, base64url/base62, >= N character classes, >= X bits/char, not hex-only."""
    if not (opts.entropy_min_length <= len(value) <= 4096):
        return False
    if _HEX_RE.fullmatch(value):
        return False
    return (_char_classes(value) >= opts.entropy_min_classes
            and shannon_bits_per_char(value) >= opts.entropy_min_bits)


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

# (start, end, kind, priority, rule, key, value_for_allowlist)
_Candidate = Tuple[int, int, str, int, str, str]

_PRI_PRIVATE_KEY, _PRI_DB, _PRI_PREFIX, _PRI_HEADER, _PRI_ASSIGN, _PRI_ENTROPY = range(6)


def _known_candidates(text: str, opts: DetectOptions) -> Iterator[_Candidate]:
    if "BEGIN" in text and "-----" in text:
        for m in _r._PRIVATE_KEY_RE.finditer(text):
            yield m.start(), m.end(), "private-key", _PRI_PRIVATE_KEY, "known", ""
    if "://" in text:
        yield from _db_url_candidates(text, opts)
        yield from _url_password_param_candidates(text)
        for m in _r._URL_BARE_TOKEN_RE.finditer(text):
            yield m.start(2), m.end(2), "generic-secret", _PRI_HEADER, "known", ""
    if "=" in text and "password" in text.lower():
        yield from _libpq_candidates(text)
    if _r._has_known_prefix_substring(text):
        for m in _r._PREFIX_RE.finditer(text):
            yield m.start(1), m.end(1), _kind_for_prefix(m.group(1)), _PRI_PREFIX, "known", ""
        for start, end, token in _r._control_split_token_spans(text):
            yield start, end, _kind_for_prefix(token), _PRI_PREFIX, "known", ""
    if "." in text:
        for m in _r._ZHIPU_API_KEY_RE.finditer(text):
            yield m.start(1), m.end(1), "generic-secret", _PRI_PREFIX, "known", ""
    if "eyJ" in text:
        for m in _r._JWT_RE.finditer(text):
            yield m.start(), m.end(), "jwt", _PRI_PREFIX, "known", ""
    if "uthorization" in text or "UTHORIZATION" in text:
        for m in _r._AUTH_HEADER_RE.finditer(text):
            yield m.start(3), m.end(3), "bearer", _PRI_HEADER, "known", ""
    if ":" in text:
        for m in _r._SECRET_HEADER_RE.finditer(text):
            yield m.start(2), m.end(2), "generic-secret", _PRI_HEADER, "known", ""
        for m in _r._TELEGRAM_RE.finditer(text):
            yield m.start(3), m.end(3), "generic-secret", _PRI_HEADER, "known", ""


def _db_url_candidates(text: str, opts: DetectOptions) -> Iterator[_Candidate]:
    for m in _r._DB_CONNSTR_RE.finditer(text):
        pw = m.group(2)
        if _is_allowlisted_value(pw, "password"):
            continue
        scheme = m.group(1).split("://", 1)[0].split("+", 1)[0].lower()
        host_m = _HOST_AFTER_AT_RE.match(text, m.end(3))
        host = host_m.group(0).lower() if host_m else ""
        kind = _DB_SCHEME_KINDS.get(scheme, "postgres-url")
        if kind == "postgres-url" and host.endswith(".neon.tech"):
            kind = "neon-url"
        if opts.db_url_mask == "whole_url":
            rest = _URL_REST_RE.match(text, m.end(3))
            yield m.start(1), rest.end() if rest else m.end(3), kind, _PRI_DB, "known", ""
        else:
            yield m.start(2), m.end(2), kind, _PRI_DB, "known", ""


def _url_password_param_candidates(text: str) -> Iterator[_Candidate]:
    seen = set()
    for url_re, kind in ((_JDBC_RE, "db-password"), (_ANY_URL_RE, None)):
        for url in url_re.finditer(text):
            scheme = url.group(0).split("://", 1)[0].split("+", 1)[0].lower()
            url_kind = kind or ("db-password" if scheme in _DB_SCHEME_KINDS else "generic-secret")
            for p in _URL_PW_PARAM_RE.finditer(url.group(0)):
                start, end = url.start() + p.start(1), url.start() + p.end(1)
                if (start, end) in seen or _is_allowlisted_value(p.group(1), "password"):
                    continue
                seen.add((start, end))
                yield start, end, url_kind, _PRI_DB, "known", ""


def _line_spans(text: str) -> Iterator[Tuple[int, int]]:
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, pos + len(line)
        pos += len(line)


def _libpq_candidates(text: str) -> Iterator[_Candidate]:
    for start, end in _line_spans(text):
        if not _LIBPQ_CONTEXT_RE.search(text, start, end):
            continue
        for m in _LIBPQ_PW_RE.finditer(text, start, end):
            group = 1 if m.group(1) is not None else 2
            if _is_allowlisted_value(m.group(group), "password"):
                continue
            yield m.start(group), m.end(group), "db-password", _PRI_DB, "known", ""


# (compiled regex getter, key group, value group, check_keyword, skip inside URLs)
_ASSIGNMENT_PASSES = (
    (lambda: _r._ENV_ASSIGN_RE, 1, 3, True, False),
    (lambda: _r._ENV_ASSIGN_LOWER_RE, 1, 3, True, True),
    (lambda: _r._CFG_DOTTED_RE, 1, 3, True, True),
    (lambda: _r._CFG_ANCHORED_RE, 1, 3, True, True),
    (lambda: _r._JSON_FIELD_RE, 1, 2, False, False),
    (lambda: _r._YAML_ASSIGN_RE, 1, 3, True, True),
)


def _assignment_candidates(text: str) -> Iterator[_Candidate]:
    """§4 rule 3: secret-NAMED keys (ENV/config/JSON/YAML/repr), via redact.py's own gates."""
    if "=" not in text and ":" not in text:
        return
    url_spans = [m.span() for m in _ANY_URL_RE.finditer(text)] if "://" in text else []

    def _in_url(pos: int) -> bool:
        return any(a <= pos < b for a, b in url_spans)

    has_secret_word = bool(_r._CFG_SECRET_WORD_RE.search(text))
    for get_re, key_g, val_g, check_keyword, url_guard in _ASSIGNMENT_PASSES:
        regex = get_re()
        if regex in (_r._CFG_DOTTED_RE, _r._CFG_ANCHORED_RE) and not has_secret_word:
            continue
        for m in regex.finditer(text):
            key, value = m.group(key_g), m.group(val_g)
            if url_guard and _in_url(m.start()):
                continue
            if not _r._should_redact_assignment(key, value, check_keyword=check_keyword):
                continue
            yield m.start(val_g), m.end(val_g), _kind_for_key(key), _PRI_ASSIGN, "assignment", key
    if ":" in text and "'" in text:
        for m in _r._PYTHON_REPR_FIELD_RE.finditer(text):
            key = m.group("key")
            group = "single_value" if m.group("single_value") is not None else "double_value"
            value = m.group(group)
            if not _r._is_python_repr_secret_key(key) or _r._ENV_LOOKUP_VALUE_RE.match(value):
                continue
            yield m.start(group), m.end(group), _kind_for_key(key), _PRI_ASSIGN, "assignment", key


def _entropy_candidates(text: str, opts: DetectOptions) -> Iterator[_Candidate]:
    """§4 rule 4: any key, value must pass the entropy gate. Rule 5 (bare strings) is off by default."""
    if "=" not in text and ":" not in text:
        return
    for m in _ENTROPY_ASSIGN_RE.finditer(text):
        value = m.group(2)
        if passes_entropy_gate(value, opts):
            yield m.start(2), m.end(2), "high-entropy", _PRI_ENTROPY, "entropy", m.group(1)


def _fence_spans(text: str) -> List[Tuple[int, int]]:
    """``[start, end)`` of fenced code blocks (``` or ~~~); an unclosed fence runs to the end."""
    spans, open_at = [], None
    for m in _FENCE_LINE_RE.finditer(text):
        if open_at is None:
            open_at = m.start()
        else:
            eol = text.find("\n", m.start())
            spans.append((open_at, len(text) if eol < 0 else eol))
            open_at = None
    if open_at is not None:
        spans.append((open_at, len(text)))
    return spans


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    spans = [m.span() for m in PLACEHOLDER_RE.finditer(text)] if "[REDACTED:" in text else []
    if "«redacted" in text:
        spans += [m.span() for m in _LEGACY_MASK_RE.finditer(text)]
    return spans


def _overlaps(start: int, end: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start < b and a < end for a, b in spans)


def _user_allowed(value: str, opts: DetectOptions, compiled: Sequence["re.Pattern[str]"]) -> bool:
    if opts.allow_value_sha256 and (
            hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest() in opts.allow_value_sha256):
        return True
    return any(p.search(value) for p in compiled)


def compile_allow_patterns(patterns: Iterable[str]) -> List["re.Pattern[str]"]:
    """User ``allow_patterns``: invalid or ReDoS-shaped entries are dropped (same gate as plugins)."""
    compiled = []
    for pattern in patterns or ():
        if not isinstance(pattern, str) or not pattern or _r._has_nested_unbounded_repeat(pattern):
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue
    return compiled


def find_secrets(text: str, opts: Optional[DetectOptions] = None) -> List[SecretFinding]:
    """All secret spans in ``text``, non-overlapping and sorted by offset.

    Rule order (§4): known shapes are always masked, even inside code fences; secret-named
    assignments are masked outside fences and inside only when the value passes the entropy
    gate; the generic entropy rule never runs inside fences. Existing placeholders and legacy
    masks are never re-matched, which makes masking idempotent.
    """
    if not isinstance(text, str) or not text:
        return []
    opts = opts or DetectOptions()
    fences = _fence_spans(text) if opts.code_fence_mode != "full" and ("```" in text or "~~~" in text) else []
    protected = _protected_spans(text)
    allow = compile_allow_patterns(opts.allow_patterns)

    candidates: List[_Candidate] = list(_known_candidates(text, opts))
    for cand in _assignment_candidates(text):
        start, end, _kind, _pri, _rule, key = cand
        value = text[start:end]
        if _is_allowlisted_value(value, key):
            continue
        if fences and _overlaps(start, end, fences):
            if opts.code_fence_mode == "off" or not passes_entropy_gate(value, opts):
                continue
        candidates.append(cand)
    if opts.entropy_enabled:
        for cand in _entropy_candidates(text, opts):
            start, end, _kind, _pri, _rule, key = cand
            if fences and _overlaps(start, end, fences):
                continue
            if not _is_allowlisted_value(text[start:end], key):
                candidates.append(cand)

    accepted: List[Tuple[int, int, str, str]] = []
    taken: List[Tuple[int, int]] = list(protected)
    for start, end, kind, _pri, rule, _key in sorted(candidates, key=lambda c: (c[3], c[0], -(c[1] - c[0]))):
        if end <= start or _overlaps(start, end, taken):
            continue
        if allow or opts.allow_value_sha256:
            if _user_allowed(text[start:end], opts, allow):
                continue
        taken.append((start, end))
        accepted.append((start, end, kind, rule))
    accepted.sort()
    return [SecretFinding(start=s, end=e, kind=k, rule=r) for s, e, k, r in accepted]
