"""Owner-grant envelope: sign bytes, strict base64url, derived grant ids (addendum §2.1, U1).

Wire form::

    {"format": "hermes-owner-grant/v1", "kid": "ok_<16 hex>",
     "payload": "<b64url of the exact signed bytes>", "sig": "<b64url of 64 bytes>"}

The signature covers ``DOMAIN_PREFIX + payload_bytes``. Verifiers never re-canonicalize: they
verify the exact bytes first and only then parse them. ``grant_id`` is derived from the payload
hash and is never carried in the envelope, so neither a file name nor an extra field can
relabel a grant.

Stdlib-only and Python 3.9 compatible: this module runs under ``/usr/bin/python3 -I -S``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

FORMAT = "hermes-owner-grant/v1"
DOMAIN_PREFIX = FORMAT.encode("ascii") + b"\x00"
GRANT_ID_PREFIX = "og_"
KID_PREFIX = "ok_"
SIG_LEN = 64
PUB_LEN = 32
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
REASON_MALFORMED = "malformed"

_ENVELOPE_KEYS = frozenset(("format", "kid", "payload", "sig"))
_KID_RE = re.compile(r"ok_[0-9a-f]{16}\Z")
_GRANT_ID_RE = re.compile(r"og_[a-z2-7]{26}\Z")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*\Z")
# <issued_at ms, no leading zero>-<grant_id>.json
_FILENAME_RE = re.compile(r"(0|[1-9][0-9]{0,15})-(og_[a-z2-7]{26})\.json\Z")


class EnvelopeError(ValueError):
    """A malformed envelope, payload or grant file. ``reason`` is the verifier reason code."""

    def __init__(self, detail: str, reason: str = REASON_MALFORMED) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# -- base64url (RFC 4648 §5, unpadded, strict) ---------------------------------------------


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    """Decode unpadded base64url, refusing padding, other alphabets, whitespace and
    non-canonical encodings (so each byte string has exactly one accepted spelling)."""
    if not isinstance(text, str) or not _B64URL_RE.match(text) or len(text) % 4 == 1:
        raise EnvelopeError("invalid base64url")
    try:
        data = base64.b64decode(
            text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        raise EnvelopeError("invalid base64url") from None
    if b64url_encode(data) != text:
        raise EnvelopeError("non-canonical base64url")
    return data


# -- ids and sign bytes --------------------------------------------------------------------


def sign_bytes(payload: bytes) -> bytes:
    """The exact message an Ed25519 signature covers."""
    return DOMAIN_PREFIX + bytes(payload)


def derive_grant_id(payload: bytes) -> str:
    digest = hashlib.sha256(bytes(payload)).digest()
    return GRANT_ID_PREFIX + base64.b32encode(digest).decode("ascii")[:26].lower()


def kid_for_pub(pub: bytes) -> str:
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != PUB_LEN:
        raise EnvelopeError("an Ed25519 public key is 32 bytes")
    return KID_PREFIX + hashlib.sha256(bytes(pub)).hexdigest()[:16]


def is_kid(value: Any) -> bool:
    return isinstance(value, str) and bool(_KID_RE.match(value))


def is_grant_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_GRANT_ID_RE.match(value))


# -- payload JSON --------------------------------------------------------------------------


def encode_payload(obj: Mapping[str, Any]) -> bytes:
    """Signer-side convention: compact, sorted keys, UTF-8. Verifiers never call this."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise EnvelopeError("duplicate JSON key %r" % key)
        out[key] = value
    return out


def _no_constants(name):
    raise EnvelopeError("non-finite JSON number %s" % name)


def strict_json_object(raw: Union[bytes, bytearray, str], what: str) -> Dict[str, Any]:
    """Parse one JSON object: strict UTF-8, no duplicate keys, no NaN/Infinity."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise EnvelopeError("%s is not UTF-8" % what) from None
    if not isinstance(raw, str):
        raise EnvelopeError("%s is not text" % what)
    try:
        obj = json.loads(
            raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants
        )
    except EnvelopeError:
        raise
    except (ValueError, RecursionError):
        raise EnvelopeError("%s is not valid JSON" % what) from None
    if not isinstance(obj, dict):
        raise EnvelopeError("%s is not a JSON object" % what)
    return obj


def decode_payload(payload: bytes) -> Dict[str, Any]:
    """Parse payload bytes. Callers must not trust the result before the signature checks
    out; unverified decoding is only for candidate filtering (addendum §3.4)."""
    return strict_json_object(payload, "payload")


# -- the envelope --------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    kid: str
    payload: bytes
    sig: bytes

    @property
    def format(self) -> str:
        return FORMAT

    @property
    def grant_id(self) -> str:
        return derive_grant_id(self.payload)

    def sign_bytes(self) -> bytes:
        return sign_bytes(self.payload)

    def to_dict(self) -> Dict[str, str]:
        return {
            "format": FORMAT,
            "kid": self.kid,
            "payload": b64url_encode(self.payload),
            "sig": b64url_encode(self.sig),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _check_parts(kid: Any, payload: Any, sig: Any) -> None:
    if not is_kid(kid):
        raise EnvelopeError("kid must be ok_ + 16 lowercase hex")
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise EnvelopeError("payload is empty")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise EnvelopeError("payload exceeds %d bytes" % MAX_PAYLOAD_BYTES)
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != SIG_LEN:
        raise EnvelopeError("sig must be %d bytes" % SIG_LEN)


def seal(payload: bytes, kid: str, sign: Callable[[bytes], bytes]) -> Envelope:
    """Build an envelope by signing ``sign_bytes(payload)`` with ``sign`` (Ed25519 in
    production, where Electron main is the only signer; tests pass a stand-in)."""
    payload = bytes(payload)
    _check_parts(kid, payload, b"\x00" * SIG_LEN)
    sig = sign(sign_bytes(payload))
    _check_parts(kid, payload, sig)
    return Envelope(kid=kid, payload=payload, sig=bytes(sig))


def parse_envelope(source: Union[Mapping[str, Any], bytes, bytearray, str]) -> Envelope:
    """Validate the envelope shape. This does NOT verify the signature."""
    if isinstance(source, (bytes, bytearray, str)):
        if len(source) > MAX_ENVELOPE_BYTES:
            raise EnvelopeError("envelope exceeds %d bytes" % MAX_ENVELOPE_BYTES)
        obj = strict_json_object(source, "envelope")
    elif isinstance(source, Mapping):
        obj = dict(source)
    else:
        raise EnvelopeError("envelope must be a JSON object")
    if set(obj) != _ENVELOPE_KEYS:
        # Extra keys are refused, not ignored: an unsigned "grant_id" or "scope" beside the
        # payload could mislead a reader that doesn't know it is unsigned.
        raise EnvelopeError("envelope keys must be exactly %s" % sorted(_ENVELOPE_KEYS))
    if obj["format"] != FORMAT:
        raise EnvelopeError("format is not %s" % FORMAT)
    if not isinstance(obj["payload"], str) or not isinstance(obj["sig"], str):
        raise EnvelopeError("payload and sig must be base64url strings")
    payload = b64url_decode(obj["payload"])
    sig = b64url_decode(obj["sig"])
    _check_parts(obj["kid"], payload, sig)
    return Envelope(kid=obj["kid"], payload=payload, sig=sig)


# -- grant files ---------------------------------------------------------------------------


def grant_filename(issued_at: int, grant_id: str) -> str:
    if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at < 0:
        raise EnvelopeError("issued_at must be a non-negative int (epoch ms)")
    if not is_grant_id(grant_id):
        raise EnvelopeError("not a grant id")
    return "%d-%s.json" % (issued_at, grant_id)


def parse_grant_filename(name: str) -> Optional[Tuple[int, str]]:
    """``(issued_at, grant_id)`` from a grant file name, or None. A lookup hint only: the
    real id is always re-derived from the payload bytes."""
    match = _FILENAME_RE.match(name) if isinstance(name, str) else None
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def read_envelope_file(path: str, *, max_bytes: int = MAX_ENVELOPE_BYTES) -> Envelope:
    """Read and shape-check one grant file. Refuses non-regular files (a FIFO would block the
    hook), a symlink as the final component, and oversize files. Grants live in an
    agent-writable directory, so only the signature makes the result trustworthy."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EnvelopeError("cannot open grant file: %s" % exc.strerror) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EnvelopeError("grant file is not a regular file")
        if st.st_size > max_bytes:
            raise EnvelopeError("grant file exceeds %d bytes" % max_bytes)
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise EnvelopeError("grant file exceeds %d bytes" % max_bytes)
    return parse_envelope(data)
