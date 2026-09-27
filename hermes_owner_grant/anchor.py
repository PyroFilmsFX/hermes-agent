"""Root-owned owner-grant trust anchor (addendum §1.3, §1.5, §2.4 steps 1 and 3; U3).

The owner's Ed25519 public keys are pinned in one root-owned file at a hard-coded path. A
same-uid agent can't write it, so an agent can't swap in its own key. Before trusting the
file the loader applies ssh ``StrictModes``-style checks to the file and to every directory
from ``/`` down to it:

* ``lstat`` shows no symlink, and the kind is right (directory, then regular file);
* ``st_uid == 0``;
* no group- or other-write bit.

The file is then opened with ``O_NOFOLLOW`` and its ``fstat`` must be the same inode and pass
the same checks, which closes a swap between ``lstat`` and ``open``.

**No environment variable, argument or flag can change the anchor path.** ``ANCHOR_PATH`` is
a literal, the loader has no path parameter, and this module never reads the environment.
Tests (and trusted in-process callers) inject a filesystem object with ``fs=`` to simulate
root-owned versus user-owned chains without root; the path it is asked about is still the
constant. The verify layer (U5) additionally accepts an already-built ``Anchor`` object for
in-process callers; the CLI never exposes either.

Known limit: macOS ACLs are not inspected (the stdlib has no ACL API). Only root can add an
ACL to a root-owned directory, so an agent can't use one to get past these checks.

Stdlib-only and Python 3.9 compatible: this module runs under ``/usr/bin/python3 -I -S``.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .envelope import (
    EnvelopeError,
    b64url_decode,
    is_kid,
    kid_for_pub,
    strict_json_object,
)

ANCHOR_FORMAT = "hermes-owner-anchor/v1"
ANCHOR_DIR = "/Library/Application Support/Hermes/owner-grant"
ANCHOR_PATH = ANCHOR_DIR + "/anchor.json"
MAX_ANCHOR_BYTES = 64 * 1024
MAX_KEYS = 32
# Longest grant TTL class (quote-only, 7 days; addendum §6.3). A retired key can't verify
# anything once this much time has passed since retirement, even for a backdated grant.
MAX_GRANT_TTL_MS = 7 * 24 * 3600 * 1000

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_REVOKED = "revoked"
KEY_STATUSES = (STATUS_ACTIVE, STATUS_RETIRED, STATUS_REVOKED)

REASON_ANCHOR_MISSING = "anchor_missing"
REASON_ANCHOR_UNTRUSTED = "anchor_untrusted"
REASON_UNKNOWN_KID = "unknown_kid"
REASON_KEY_REVOKED = "key_revoked"
REASON_KEY_RETIRED = "key_retired"
REASON_KEY_NOT_YET_VALID = "key_not_yet_valid"

_ANCHOR_KEYS = frozenset((
    "format",
    "owner_uid",
    "grants_dir",
    "keys",
    "verifier_sha256",
))
_ANCHOR_REQUIRED = frozenset(("format", "owner_uid", "grants_dir", "keys"))
_KEY_FIELDS = frozenset(("kid", "alg", "pub", "status", "not_before", "retired_at"))
_HEX = frozenset("0123456789abcdef")
_WRITABLE_BY_OTHERS = stat.S_IWGRP | stat.S_IWOTH


class AnchorError(Exception):
    """The anchor is missing or can't be trusted. ``reason`` is ``anchor_missing`` or
    ``anchor_untrusted`` (CLI exit 3); ``detail`` says which check failed."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AnchorKey:
    kid: str
    alg: str
    pub: bytes
    status: str
    not_before: int
    retired_at: Optional[int]


@dataclass(frozen=True)
class Anchor:
    owner_uid: int
    grants_dir: str
    keys: Tuple[AnchorKey, ...]
    verifier_sha256: Optional[str]
    sha256: str  # hex digest of the exact anchor bytes, reported by the verifier

    def key(self, kid: str) -> Optional[AnchorKey]:
        for entry in self.keys:
            if entry.kid == kid:
                return entry
        return None

    @property
    def active_key(self) -> Optional[AnchorKey]:
        for entry in self.keys:
            if entry.status == STATUS_ACTIVE:
                return entry
        return None


# -- filesystem access (injectable) --------------------------------------------------------


class OsFileSystem:
    """The real filesystem. Tests substitute an object with the same methods (``listdir`` is
    needed only by the installed-verifier check)."""

    def lstat(self, path: str) -> Any:
        return os.lstat(path)

    def listdir(self, path: str) -> list:
        return os.listdir(path)

    def read_nofollow(self, path: str, limit: int) -> Tuple[Any, bytes]:
        """Open without following a final symlink or blocking on a FIFO; return the
        descriptor's ``fstat`` and at most ``limit`` bytes (none unless it's a regular file)."""
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return st, b""
            chunks = []
            remaining = limit
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return st, b"".join(chunks)
        finally:
            os.close(fd)


def _untrusted(detail: str) -> AnchorError:
    return AnchorError(REASON_ANCHOR_UNTRUSTED, detail)


def _require_root_owned(st: Any, path: str) -> None:
    if st.st_uid != 0:
        raise _untrusted("%s is owned by uid %d, not root" % (path, st.st_uid))
    if stat.S_IMODE(st.st_mode) & _WRITABLE_BY_OTHERS:
        raise _untrusted(
            "%s is group- or other-writable (mode %o)"
            % (path, stat.S_IMODE(st.st_mode))
        )


def _lstat(fs: Any, path: str) -> Any:
    try:
        return fs.lstat(path)
    except FileNotFoundError:
        raise AnchorError(REASON_ANCHOR_MISSING, "%s does not exist" % path) from None
    except OSError as exc:
        raise _untrusted("cannot lstat %s: %s" % (path, exc.strerror or exc)) from None


def _dir_chain(directory: str) -> Tuple[str, ...]:
    if not directory.startswith("/") or posixpath.normpath(directory) != directory:
        raise _untrusted(
            "anchor directory %r is not a normalized absolute path" % directory
        )
    chain = ["/"]
    for part in directory.strip("/").split("/"):
        if part:
            chain.append(posixpath.join(chain[-1], part))
    return tuple(chain)


def _check_dir_chain(directory: str, fs: Any) -> None:
    """Every directory from ``/`` to ``directory``: not a symlink, a directory, root-owned,
    not group- or other-writable."""
    for path in _dir_chain(directory):
        st = _lstat(fs, path)
        if stat.S_ISLNK(st.st_mode):
            raise _untrusted("%s is a symlink" % path)
        if not stat.S_ISDIR(st.st_mode):
            raise _untrusted("%s is not a directory" % path)
        _require_root_owned(st, path)


def read_root_owned_file(path: str, fs: Any, limit: int) -> bytes:
    """Return the bytes of ``path`` after the StrictModes checks on it and on every directory
    from ``/`` down to it (see the module docstring). Raises ``AnchorError``: missing, or
    untrusted (including a file larger than ``limit`` bytes)."""
    _check_dir_chain(posixpath.dirname(path), fs)
    st = _lstat(fs, path)
    if stat.S_ISLNK(st.st_mode):
        raise _untrusted("%s is a symlink" % path)
    if not stat.S_ISREG(st.st_mode):
        raise _untrusted("%s is not a regular file" % path)
    _require_root_owned(st, path)
    try:
        opened, data = fs.read_nofollow(path, limit + 1)
    except FileNotFoundError:
        raise AnchorError(
            REASON_ANCHOR_MISSING, "%s disappeared before open" % path
        ) from None
    except OSError as exc:
        raise _untrusted("cannot open %s: %s" % (path, exc.strerror or exc)) from None
    if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
        raise _untrusted("%s changed between lstat and open" % path)
    if not stat.S_ISREG(opened.st_mode):
        raise _untrusted("%s is not a regular file" % path)
    _require_root_owned(opened, path)
    if len(data) > limit:
        raise _untrusted("%s exceeds %d bytes" % (path, limit))
    return data


def _load_anchor_at(path: str, fs: Any) -> Anchor:
    """Load and trust-check the anchor at ``path``. Private: production code only ever calls
    it with ``ANCHOR_PATH`` (through ``load_trusted_anchor``)."""
    return parse_anchor(read_root_owned_file(path, fs, MAX_ANCHOR_BYTES))


def load_trusted_anchor(*, fs: Any = None) -> Anchor:
    """Load the anchor from the hard-coded ``ANCHOR_PATH``. Raises ``AnchorError``."""
    return _load_anchor_at(ANCHOR_PATH, OsFileSystem() if fs is None else fs)


# -- contents ------------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_key(entry: Any, index: int) -> AnchorKey:
    where = "keys[%d]" % index
    if not isinstance(entry, dict) or set(entry) != _KEY_FIELDS:
        raise _untrusted(
            "%s must have exactly the fields %s" % (where, sorted(_KEY_FIELDS))
        )
    if entry["alg"] != "Ed25519":
        raise _untrusted("%s.alg is not Ed25519" % where)
    try:
        pub = b64url_decode(entry["pub"])
        derived = kid_for_pub(pub)
    except EnvelopeError as exc:
        raise _untrusted("%s.pub: %s" % (where, exc.detail)) from None
    if not is_kid(entry["kid"]) or entry["kid"] != derived:
        raise _untrusted("%s.kid does not match its pub" % where)
    status = entry["status"]
    if status not in KEY_STATUSES:
        raise _untrusted(
            "%s.status %r is not one of %s" % (where, status, KEY_STATUSES)
        )
    not_before, retired_at = entry["not_before"], entry["retired_at"]
    if not _is_int(not_before) or not_before < 0:
        raise _untrusted("%s.not_before must be a non-negative int (epoch ms)" % where)
    if retired_at is not None and (not _is_int(retired_at) or retired_at < 0):
        raise _untrusted(
            "%s.retired_at must be null or a non-negative int (epoch ms)" % where
        )
    if status == STATUS_ACTIVE and retired_at is not None:
        raise _untrusted("%s is active but has retired_at" % where)
    if status == STATUS_RETIRED and retired_at is None:
        raise _untrusted("%s is retired without retired_at" % where)
    return AnchorKey(
        kid=derived,
        alg="Ed25519",
        pub=pub,
        status=status,
        not_before=not_before,
        retired_at=retired_at,
    )


def parse_anchor(data: bytes) -> Anchor:
    """Parse anchor bytes (``hermes-owner-anchor/v1``). Raises ``AnchorError``
    (``anchor_untrusted``). Does no filesystem checks: only feed it bytes that came from
    ``load_trusted_anchor`` or from a trusted in-process caller."""
    try:
        obj: Dict[str, Any] = strict_json_object(data, "anchor")
    except EnvelopeError as exc:
        raise _untrusted(exc.detail) from None
    if not _ANCHOR_REQUIRED <= set(obj) <= _ANCHOR_KEYS:
        raise _untrusted(
            "anchor fields must be %s (+ optional verifier_sha256)"
            % sorted(_ANCHOR_REQUIRED)
        )
    if obj["format"] != ANCHOR_FORMAT:
        raise _untrusted("format is not %s" % ANCHOR_FORMAT)
    owner_uid = obj["owner_uid"]
    if not _is_int(owner_uid) or owner_uid < 0:
        raise _untrusted("owner_uid must be a non-negative int")
    grants_dir = obj["grants_dir"]
    if (
        not isinstance(grants_dir, str)
        or not grants_dir.startswith("/")
        or grants_dir == "/"
        or "\x00" in grants_dir
        or posixpath.normpath(grants_dir) != grants_dir
    ):
        raise _untrusted("grants_dir must be a normalized absolute path")
    verifier_sha256 = obj.get("verifier_sha256")
    if verifier_sha256 is not None and (
        not isinstance(verifier_sha256, str)
        or len(verifier_sha256) != 64
        or not set(verifier_sha256) <= _HEX
    ):
        raise _untrusted("verifier_sha256 must be 64 lowercase hex")
    raw_keys = obj["keys"]
    if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= MAX_KEYS:
        raise _untrusted("keys must be a list of 1..%d entries" % MAX_KEYS)
    keys = tuple(_parse_key(entry, i) for i, entry in enumerate(raw_keys))
    if len({k.kid for k in keys}) != len(keys):
        raise _untrusted("duplicate kid")
    if sum(1 for k in keys if k.status == STATUS_ACTIVE) > 1:
        raise _untrusted("more than one active key")
    return Anchor(
        owner_uid=owner_uid,
        grants_dir=grants_dir,
        keys=keys,
        verifier_sha256=verifier_sha256,
        sha256=hashlib.sha256(bytes(data)).hexdigest(),
    )


# -- key status (addendum §2.4 step 3, §1.5) ----------------------------------------------


def check_key(anchor: Anchor, kid: str, *, issued_at: int, now: int) -> Optional[str]:
    """Return None when ``kid`` may verify a grant issued at ``issued_at`` (signed payload
    field, epoch ms) at time ``now`` (epoch ms), else the reason code:

    * ``unknown_kid``: the anchor doesn't list the kid;
    * ``key_revoked``: every grant under the kid fails, whatever it claims about time;
    * ``key_retired``: retired key and ``issued_at >= retired_at``, or more than
      ``MAX_GRANT_TTL_MS`` has passed since retirement (bounds backdating with a leaked
      retired key; revoke after a compromise);
    * ``key_not_yet_valid``: ``now < not_before``, or the grant claims to predate its key.

    The signature itself is checked afterwards (step 4), by the caller.
    """
    if not _is_int(issued_at) or not _is_int(now):
        raise TypeError("issued_at and now must be int epoch milliseconds")
    key = anchor.key(kid)
    if key is None:
        return REASON_UNKNOWN_KID
    if key.status == STATUS_REVOKED:
        return REASON_KEY_REVOKED
    if key.status == STATUS_RETIRED:
        retired_at = key.retired_at if key.retired_at is not None else 0
        if issued_at >= retired_at or now > retired_at + MAX_GRANT_TTL_MS:
            return REASON_KEY_RETIRED
    if now < key.not_before or issued_at < key.not_before:
        return REASON_KEY_NOT_YET_VALID
    return None
