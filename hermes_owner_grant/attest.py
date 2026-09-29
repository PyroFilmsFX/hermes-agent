"""Launch-attestation verifier (b10 H5, §0, §3, §4).

Wire form::

    {"format": "hermes-launch-attestation/v1", "kid": "ok_<16 hex>",
     "payload": "<b64url of the exact signed bytes>", "sig": "<b64url of 64 bytes>"}

The signature covers ``DOMAIN_PREFIX + payload_bytes`` with Ed25519.
Stdlib-only and Python 3.9 compatible: this module runs under ``/usr/bin/python3 -I -S``.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _c_ed
except ImportError:
    _c_ed = None

from . import anchor as _anchor
from . import ed25519_pure as _pure
from . import envelope as _envelope

FORMAT = "hermes-launch-attestation/v1"
DOMAIN_PREFIX = FORMAT.encode("ascii") + b"\x00"
AUDIENCE = "conductor:session-binding"
DISALLOWED_AUDIENCE = "hermes-owner-verify"
PAYLOAD_VERSION = 1
ATTEST_MAX_TTL_MS = 30 * 60 * 1000  # 30 minutes in ms
SKEW_MS = 5000  # 5 seconds
MAX_LINEAGE_ENTRIES = 16
MAX_ATTESTATION_BYTES = 64 * 1024  # 64 KB
MAX_SIGNATURE_CHECKS = 32

BINDING_FORMAT = "hermes-session-binding/v1"
BINDING_DOMAIN_PREFIX = BINDING_FORMAT.encode("ascii") + b"\x00"
BINDING_AUDIENCE = "hermes-main"
MAX_BINDING_BYTES = 64 * 1024  # 64 KB

SCHEMA = "hermes-owner-verify/v1"
VERSION = "1.0.0"
IMPL = "pure"

EXIT_OK = 0
EXIT_DENY = 1
EXIT_USAGE = 2
EXIT_ANCHOR = 3
EXIT_NOT_FOUND = 4
EXIT_INTERNAL = 5

REASON_BAD_SIGNATURE = "bad_signature"
REASON_WRONG_AUDIENCE = "wrong_audience"
REASON_UID_MISMATCH = "uid_mismatch"
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_PROFILE_MISMATCH = "profile_mismatch"
REASON_CLAUDE_SESSION_MISMATCH = "claude_session_mismatch"
REASON_EXPIRED = "expired"
REASON_TTL_EXCEEDED = "ttl_exceeded"
REASON_NOT_FOUND = "not_found"
REASON_UNBOUND = "unbound"
REASON_MALFORMED = _envelope.REASON_MALFORMED
REASON_INTERNAL = "internal_error"

REASON_ANCHOR_MISSING = _anchor.REASON_ANCHOR_MISSING
REASON_ANCHOR_UNTRUSTED = _anchor.REASON_ANCHOR_UNTRUSTED
REASON_UNKNOWN_KID = _anchor.REASON_UNKNOWN_KID
REASON_KEY_REVOKED = _anchor.REASON_KEY_REVOKED
REASON_KEY_RETIRED = _anchor.REASON_KEY_RETIRED
REASON_KEY_NOT_YET_VALID = _anchor.REASON_KEY_NOT_YET_VALID

_ANCHOR_REASONS = frozenset((REASON_ANCHOR_MISSING, REASON_ANCHOR_UNTRUSTED))
_REQUIRED_PAYLOAD_FIELDS = (
    "v",
    "aud",
    "owner_uid",
    "profile",
    "backend",
    "hermes_session_id",
    "claude_session_id",
    "launch_seq",
    "project_root",
    "binding_nonce",
    "issued_at",
    "expires_at",
)
_REQUIRED_BINDING_PAYLOAD_FIELDS = (
    "v",
    "aud",
    "owner_uid",
    "profile",
    "hermes_session_id",
    "state",
    "seq",
    "binding_nonce",
    "bound_at",
    "project_root",
)


class AttestUsageError(ValueError):
    """The caller passed an invalid request (CLI exit 2)."""


class _Deny(Exception):
    def __init__(
        self,
        reason: str,
        detail: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail
        self.payload = payload


def _malformed(detail: str, payload: Optional[Dict[str, Any]] = None) -> _Deny:
    return _Deny(REASON_MALFORMED, detail, payload=payload)


def _signature_ok(pub: bytes, message: bytes, sig: bytes) -> bool:
    if _c_ed is not None:
        try:
            _c_ed.Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
            return True
        except Exception:
            return False
    return _pure.verify(pub, message, sig)


def sign_bytes(payload: bytes) -> bytes:
    """The exact message an Ed25519 signature covers for launch attestations."""
    return DOMAIN_PREFIX + bytes(payload)


def _check_parts(kid: Any, payload: Any, sig: Any) -> None:
    if not _envelope.is_kid(kid):
        raise _envelope.EnvelopeError("kid must be ok_ + 16 lowercase hex")
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise _envelope.EnvelopeError("payload is empty")
    if len(payload) > _envelope.MAX_PAYLOAD_BYTES:
        raise _envelope.EnvelopeError("payload exceeds %d bytes" % _envelope.MAX_PAYLOAD_BYTES)
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != _envelope.SIG_LEN:
        raise _envelope.EnvelopeError("sig must be %d bytes" % _envelope.SIG_LEN)


@dataclass(frozen=True)
class AttestationEnvelope:
    kid: str
    payload: bytes
    sig: bytes

    @property
    def format(self) -> str:
        return FORMAT

    def sign_bytes(self) -> bytes:
        return sign_bytes(self.payload)

    def to_dict(self) -> Dict[str, str]:
        return {
            "format": FORMAT,
            "kid": self.kid,
            "payload": _envelope.b64url_encode(self.payload),
            "sig": _envelope.b64url_encode(self.sig),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def parse_envelope(source: Union[Mapping[str, Any], bytes, bytearray, str, Any]) -> AttestationEnvelope:
    """Validate the envelope shape. This does NOT verify the signature."""
    if hasattr(source, "to_dict"):
        source = source.to_dict()
    if isinstance(source, (bytes, bytearray, str)):
        if len(source) > MAX_ATTESTATION_BYTES:
            raise _envelope.EnvelopeError("envelope exceeds %d bytes" % MAX_ATTESTATION_BYTES)
        obj = _envelope.strict_json_object(source, "envelope")
    elif isinstance(source, Mapping):
        obj = dict(source)
    else:
        raise _envelope.EnvelopeError("envelope must be a JSON object")
    if set(obj) != frozenset(("format", "kid", "payload", "sig")):
        raise _envelope.EnvelopeError("envelope keys must be exactly ('format', 'kid', 'payload', 'sig')")
    if obj["format"] != FORMAT:
        raise _envelope.EnvelopeError("format is not %s" % FORMAT)
    if not isinstance(obj["payload"], str) or not isinstance(obj["sig"], str):
        raise _envelope.EnvelopeError("payload and sig must be base64url strings")
    payload = _envelope.b64url_decode(obj["payload"])
    sig = _envelope.b64url_decode(obj["sig"])
    _check_parts(obj["kid"], payload, sig)
    return AttestationEnvelope(kid=obj["kid"], payload=payload, sig=sig)


def seal(payload: bytes, kid: str, sign: Callable[[bytes], bytes]) -> AttestationEnvelope:
    """Build an envelope by signing ``sign_bytes(payload)`` with ``sign``."""
    payload = bytes(payload)
    _check_parts(kid, payload, b"\x00" * _envelope.SIG_LEN)
    sig = sign(sign_bytes(payload))
    _check_parts(kid, payload, sig)
    return AttestationEnvelope(kid=kid, payload=payload, sig=bytes(sig))


def read_envelope_file(
    path: str, *, max_bytes: int = MAX_ATTESTATION_BYTES, dir_fd: Optional[int] = None
) -> AttestationEnvelope:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags) if dir_fd is None else os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise _envelope.EnvelopeError("cannot open attestation file: %s" % exc.strerror) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _envelope.EnvelopeError("attestation file is not a regular file")
        if st.st_size > max_bytes:
            raise _envelope.EnvelopeError("attestation file exceeds %d bytes" % max_bytes)
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
        raise _envelope.EnvelopeError("attestation file exceeds %d bytes" % max_bytes)
    return parse_envelope(data)


def sign_binding_bytes(payload: bytes) -> bytes:
    """The exact message an Ed25519 signature covers for session bindings."""
    return BINDING_DOMAIN_PREFIX + bytes(payload)


@dataclass(frozen=True)
class BindingEnvelope:
    kid: str
    payload: bytes
    sig: bytes

    @property
    def format(self) -> str:
        return BINDING_FORMAT

    def sign_bytes(self) -> bytes:
        return sign_binding_bytes(self.payload)

    def to_dict(self) -> Dict[str, str]:
        return {
            "format": BINDING_FORMAT,
            "kid": self.kid,
            "payload": _envelope.b64url_encode(self.payload),
            "sig": _envelope.b64url_encode(self.sig),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def parse_binding_envelope(source: Union[Mapping[str, Any], bytes, bytearray, str, Any]) -> BindingEnvelope:
    """Validate the binding envelope shape. This does NOT verify the signature."""
    if hasattr(source, "to_dict"):
        source = source.to_dict()
    if isinstance(source, (bytes, bytearray, str)):
        if len(source) > MAX_BINDING_BYTES:
            raise _envelope.EnvelopeError("envelope exceeds %d bytes" % MAX_BINDING_BYTES)
        obj = _envelope.strict_json_object(source, "envelope")
    elif isinstance(source, Mapping):
        obj = dict(source)
    else:
        raise _envelope.EnvelopeError("envelope must be a JSON object")
    if set(obj) != frozenset(("format", "kid", "payload", "sig")):
        raise _envelope.EnvelopeError("envelope keys must be exactly ('format', 'kid', 'payload', 'sig')")
    if obj["format"] != BINDING_FORMAT:
        raise _envelope.EnvelopeError("format is not %s" % BINDING_FORMAT)
    if not isinstance(obj["payload"], str) or not isinstance(obj["sig"], str):
        raise _envelope.EnvelopeError("payload and sig must be base64url strings")
    payload = _envelope.b64url_decode(obj["payload"])
    sig = _envelope.b64url_decode(obj["sig"])
    _check_parts(obj["kid"], payload, sig)
    return BindingEnvelope(kid=obj["kid"], payload=payload, sig=sig)


def seal_binding(payload: bytes, kid: str, sign: Callable[[bytes], bytes]) -> BindingEnvelope:
    """Build a binding envelope by signing ``sign_binding_bytes(payload)`` with ``sign``."""
    payload = bytes(payload)
    _check_parts(kid, payload, b"\x00" * _envelope.SIG_LEN)
    sig = sign(sign_binding_bytes(payload))
    _check_parts(kid, payload, sig)
    return BindingEnvelope(kid=kid, payload=payload, sig=bytes(sig))


def read_binding_file(path: str, *, max_bytes: int = MAX_BINDING_BYTES) -> BindingEnvelope:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _envelope.EnvelopeError("cannot open binding file: %s" % exc.strerror) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _envelope.EnvelopeError("binding file is not a regular file")
        if st.st_size > max_bytes:
            raise _envelope.EnvelopeError("binding file exceeds %d bytes" % max_bytes)
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
        raise _envelope.EnvelopeError("binding file exceeds %d bytes" % max_bytes)
    return parse_binding_envelope(data)


@dataclass(frozen=True)
class AttestationResult:
    ok: bool
    reason: Optional[str] = None
    detail: Optional[str] = None
    hermes_session_id: Optional[str] = None
    claude_session_id: Optional[str] = None
    project_root: Optional[str] = None
    repo_common_root: Optional[str] = None
    binding_nonce: Optional[str] = None
    hermes_lineage: List[str] = field(default_factory=list)
    issued_at: Optional[int] = None
    expires_at: Optional[int] = None
    launch_seq: Optional[int] = None
    binding_seq: Optional[int] = None
    candidates: int = 0
    checked_at: int = 0
    attestation: Optional[Dict[str, Any]] = None
    anchor_sha256: Optional[str] = None

    @property
    def exit_code(self) -> int:
        if self.ok:
            return EXIT_OK
        if self.reason in _ANCHOR_REASONS:
            return EXIT_ANCHOR
        if self.reason == REASON_NOT_FOUND:
            return EXIT_NOT_FOUND
        if self.reason == REASON_INTERNAL:
            return EXIT_INTERNAL
        return EXIT_DENY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "hermes_session_id": self.hermes_session_id,
            "claude_session_id": self.claude_session_id,
            "project_root": self.project_root,
            "repo_common_root": self.repo_common_root,
            "binding_nonce": self.binding_nonce,
            "hermes_lineage": list(self.hermes_lineage),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "candidates": self.candidates,
            "checked_at": self.checked_at,
            "verifier": {
                "version": VERSION,
                "impl": IMPL,
                "anchor_sha256": self.anchor_sha256,
            },
            "attestation": self.attestation,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _check_anchor(
    anchor: Optional[_anchor.Anchor], fs: Any, uid: int
) -> _anchor.Anchor:
    if anchor is None:
        try:
            anchor = _anchor.load_trusted_anchor(fs=fs)
        except _anchor.AnchorError as exc:
            raise _Deny(exc.reason, exc.detail) from None
    elif not isinstance(anchor, _anchor.Anchor):
        raise AttestUsageError("anchor must be a hermes_owner_grant.anchor.Anchor")
    if anchor.owner_uid != uid:
        raise _Deny(
            REASON_ANCHOR_UNTRUSTED,
            "anchor pins owner_uid %d, verifying uid is %d" % (anchor.owner_uid, uid),
        )
    return anchor


def _validate_payload(p: Dict[str, Any]) -> None:
    for f in _REQUIRED_PAYLOAD_FIELDS:
        if f not in p:
            raise _malformed("payload lacks %s" % f, payload=p)
    if not isinstance(p["v"], int) or isinstance(p["v"], bool) or p["v"] != PAYLOAD_VERSION:
        raise _malformed("payload v must be %d" % PAYLOAD_VERSION, payload=p)
    if not isinstance(p["aud"], list) or not all(isinstance(a, str) for a in p["aud"]):
        raise _malformed("aud must be a list of strings", payload=p)
    for name in ("owner_uid", "launch_seq", "issued_at", "expires_at"):
        val = p[name]
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise _malformed("%s must be a non-negative int" % name, payload=p)
    if p["expires_at"] <= p["issued_at"]:
        raise _malformed("expires_at must be after issued_at", payload=p)
    for name in ("profile", "backend", "hermes_session_id", "claude_session_id"):
        val = p[name]
        if not isinstance(val, str) or not val:
            raise _malformed("%s must be a non-empty string" % name, payload=p)

    proj = p["project_root"]
    if proj is not None:
        if not isinstance(proj, str) or not proj:
            raise _malformed("project_root must be null or a non-empty string", payload=p)
        if (
            not posixpath.isabs(proj)
            or posixpath.normpath(proj) != proj
            or (proj != "/" and proj.endswith("/"))
        ):
            raise _malformed("project_root must be an absolute and normalized path", payload=p)

    repo_common = p.get("repo_common_root")
    if repo_common is not None:
        if not isinstance(repo_common, str) or not repo_common:
            raise _malformed("repo_common_root must be null or a non-empty string", payload=p)
        if (
            not posixpath.isabs(repo_common)
            or posixpath.normpath(repo_common) != repo_common
            or (repo_common != "/" and repo_common.endswith("/"))
        ):
            raise _malformed("repo_common_root must be an absolute and normalized path", payload=p)

    nonce = p["binding_nonce"]
    if nonce is not None and (not isinstance(nonce, str) or not nonce):
        raise _malformed("binding_nonce must be null or a non-empty string", payload=p)

    bseq = p.get("binding_seq")
    if bseq is not None and (not isinstance(bseq, int) or isinstance(bseq, bool) or bseq < 0):
        raise _malformed("binding_seq must be null or a non-negative int", payload=p)

    remote = p.get("repo_remote")
    if remote is not None and not isinstance(remote, str):
        raise _malformed("repo_remote must be null or a string", payload=p)

    lineage = p.get("hermes_lineage")
    if lineage is not None:
        if not isinstance(lineage, list) or not all(isinstance(x, str) and x for x in lineage):
            raise _malformed("hermes_lineage must be null or a list of non-empty strings", payload=p)
        if len(lineage) > MAX_LINEAGE_ENTRIES:
            raise _malformed("hermes_lineage exceeds %d entries" % MAX_LINEAGE_ENTRIES, payload=p)


def _require_active_key(
    anchor: _anchor.Anchor, kid: str, *, issued_at: int, now: int
) -> _anchor.AnchorKey:
    """The anchor key for ``kid`` when it is ACTIVE and valid for ``issued_at`` and ``now``.

    Unknown, retired and revoked kids are refused whatever the artifact claims about time.
    """
    key = anchor.key(kid)
    if key is None:
        raise _Deny(_anchor.REASON_UNKNOWN_KID, "kid %s is not in the anchor" % kid)
    if key.status == _anchor.STATUS_REVOKED:
        raise _Deny(_anchor.REASON_KEY_REVOKED, "key %s is revoked" % kid)
    if key.status == _anchor.STATUS_RETIRED:
        raise _Deny(
            _anchor.REASON_KEY_RETIRED,
            "key %s is retired (attestations require the active kid)" % kid,
        )
    if key.status != _anchor.STATUS_ACTIVE:
        raise _Deny(_anchor.REASON_KEY_NOT_YET_VALID, "key %s is not active (%s)" % (kid, key.status))
    if now < key.not_before or issued_at < key.not_before:
        raise _Deny(_anchor.REASON_KEY_NOT_YET_VALID, "key %s is not yet valid" % kid)
    return key


def _evaluate_attestation(
    env: AttestationEnvelope,
    payload: Dict[str, Any],
    anchor: _anchor.Anchor,
    *,
    uid: int,
    now: int,
    claude_session: str,
    session: Optional[str] = None,
    budget: List[int],
) -> None:
    # 1. Anchor kid lookup
    key = anchor.key(env.kid)
    if key is None:
        raise _Deny(_anchor.REASON_UNKNOWN_KID, "kid %s is not in the anchor" % env.kid)

    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at < 0:
        raise _malformed("issued_at must be a non-negative int")

    # Attestations need the ACTIVE kid, the same gate as bindings. The grant rule
    # (``anchor.check_key``) still honours a retired kid for artifacts issued before
    # retirement; main re-signs every live attestation at rotation, so that grace would only
    # help a leaked retired key backdate an attestation.
    key = _require_active_key(anchor, env.kid, issued_at=issued_at, now=now)

    # 2. Signature verification
    if budget[0] <= 0:
        raise _Deny(REASON_BAD_SIGNATURE, "signature-check budget exhausted")
    budget[0] -= 1
    if not _signature_ok(key.pub, env.sign_bytes(), env.sig):
        raise _Deny(REASON_BAD_SIGNATURE, "signature does not verify under %s" % env.kid)

    # 3. Shape validation
    _validate_payload(payload)

    # 4. Audience and UID
    if AUDIENCE not in payload["aud"]:
        raise _Deny(REASON_WRONG_AUDIENCE, "aud does not include %s" % AUDIENCE, payload=payload)
    if DISALLOWED_AUDIENCE in payload["aud"]:
        raise _Deny(REASON_WRONG_AUDIENCE, "aud must not include %s" % DISALLOWED_AUDIENCE, payload=payload)
    if payload["owner_uid"] != uid:
        raise _Deny(
            REASON_UID_MISMATCH,
            "attestation owner_uid %d, verifying uid %d" % (payload["owner_uid"], uid),
            payload=payload,
        )

    # 5. Session matches
    if payload["claude_session_id"] != claude_session:
        raise _Deny(
            REASON_CLAUDE_SESSION_MISMATCH,
            "claude_session_id %s does not match %s" % (payload["claude_session_id"], claude_session),
            payload=payload,
        )
    if session is not None and payload["hermes_session_id"] != session:
        raise _Deny(
            REASON_SESSION_MISMATCH,
            "hermes_session_id %s does not match expected %s" % (payload["hermes_session_id"], session),
            payload=payload,
        )

    # 6. TTL and Expiry
    ttl = payload["expires_at"] - payload["issued_at"]
    if ttl > ATTEST_MAX_TTL_MS:
        raise _Deny(
            REASON_TTL_EXCEEDED,
            "attestation lifetime %d ms exceeds the %d ms cap" % (ttl, ATTEST_MAX_TTL_MS),
            payload=payload,
        )
    if now < payload["issued_at"] - SKEW_MS:
        raise _Deny(REASON_EXPIRED, "attestation is issued in the future", payload=payload)
    if now > payload["expires_at"] + SKEW_MS:
        raise _Deny(REASON_EXPIRED, "attestation expired", payload=payload)

    # 7. Unbound check
    if payload.get("binding_nonce") is None or payload.get("project_root") is None:
        raise _Deny(REASON_UNBOUND, "attestation is explicitly unbound", payload=payload)


def _ok(
    payload: Dict[str, Any],
    anchor: Any,
    now: int,
    candidates: int,
) -> AttestationResult:
    lineage = payload.get("hermes_lineage")
    return AttestationResult(
        ok=True,
        reason=None,
        detail=None,
        hermes_session_id=payload.get("hermes_session_id"),
        claude_session_id=payload.get("claude_session_id"),
        project_root=payload.get("project_root"),
        repo_common_root=payload.get("repo_common_root"),
        binding_nonce=payload.get("binding_nonce"),
        hermes_lineage=list(lineage) if lineage else [],
        issued_at=payload.get("issued_at"),
        expires_at=payload.get("expires_at"),
        launch_seq=payload.get("launch_seq"),
        binding_seq=payload.get("binding_seq"),
        candidates=candidates,
        checked_at=now,
        attestation=payload,
        anchor_sha256=getattr(anchor, "sha256", None),
    )


def _denied(
    req_now: int,
    deny: _Deny,
    anchor: Any = None,
    candidates: int = 0,
) -> AttestationResult:
    payload = deny.payload
    lineage = payload.get("hermes_lineage") if payload else None
    return AttestationResult(
        ok=False,
        reason=deny.reason,
        detail=deny.detail,
        hermes_session_id=payload.get("hermes_session_id") if payload else None,
        claude_session_id=payload.get("claude_session_id") if payload else None,
        project_root=payload.get("project_root") if payload else None,
        repo_common_root=payload.get("repo_common_root") if payload else None,
        binding_nonce=payload.get("binding_nonce") if payload else None,
        hermes_lineage=list(lineage) if lineage else [],
        issued_at=payload.get("issued_at") if payload else None,
        expires_at=payload.get("expires_at") if payload else None,
        launch_seq=payload.get("launch_seq") if payload else None,
        binding_seq=payload.get("binding_seq") if payload else None,
        candidates=candidates,
        checked_at=req_now,
        attestation=payload,
        anchor_sha256=getattr(anchor, "sha256", None),
    )


def verify_attestation(
    *,
    claude_session: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    uid: int,
    now: int,
    session: Optional[str] = None,
    hermes_session: Optional[str] = None,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> AttestationResult:
    """Find the newest valid attestation in ``<grants_dir>/session-attest/<claude_session>/``."""
    csid = claude_session if claude_session is not None else claude_session_id
    if not isinstance(csid, str) or not csid.strip():
        raise AttestUsageError("claude_session must be a non-empty string")
    csid = csid.strip()
    if "/" in csid or "\\" in csid or csid in (".", ".."):
        raise AttestUsageError("invalid claude_session format")
    expected_session = session if session is not None else hermes_session

    trusted = None
    try:
        trusted = _check_anchor(anchor, fs, uid)
        attest_dir = posixpath.join(trusted.grants_dir, "session-attest", csid)
        try:
            names = os.listdir(attest_dir)
        except OSError:
            return _denied(
                now,
                _Deny(REASON_NOT_FOUND, "no attestations found for claude_session %s" % csid),
                trusted,
                candidates=0,
            )

        candidates = []
        for name in names:
            if not name.endswith(".json"):
                continue
            file_path = posixpath.join(attest_dir, name)
            try:
                env = read_envelope_file(file_path)
                payload = _envelope.decode_payload(env.payload)
            except (_envelope.EnvelopeError, OSError):
                continue
            issued = payload.get("issued_at")
            issued_int = issued if isinstance(issued, int) and not isinstance(issued, bool) else -1
            candidates.append((issued_int, name, env, payload))

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not candidates:
            return _denied(
                now,
                _Deny(REASON_NOT_FOUND, "no valid candidate files found for claude_session %s" % csid),
                trusted,
                candidates=0,
            )

        first_deny = None
        budget = [MAX_SIGNATURE_CHECKS]
        for _, _, env, payload in candidates:
            try:
                _evaluate_attestation(
                    env,
                    payload,
                    trusted,
                    uid=uid,
                    now=now,
                    claude_session=csid,
                    session=expected_session,
                    budget=budget,
                )
            except _Deny as deny:
                if deny.reason == REASON_UNBOUND:
                    return _denied(now, deny, trusted, candidates=len(candidates))
                if first_deny is None:
                    first_deny = deny
                if budget[0] <= 0:
                    break
                continue
            return _ok(payload, trusted, now, candidates=len(candidates))

        return _denied(
            now,
            first_deny or _Deny(REASON_NOT_FOUND, "no matching attestation"),
            trusted,
            candidates=len(candidates),
        )
    except _Deny as deny:
        return _denied(now, deny, trusted)
    except AttestUsageError:
        raise
    except Exception as exc:
        return _denied(
            now,
            _Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)),
            trusted,
        )


def verify_attestation_envelope(
    envelope: Union[AttestationEnvelope, Mapping[str, Any], bytes, str],
    *,
    claude_session: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    uid: int,
    now: int,
    session: Optional[str] = None,
    hermes_session: Optional[str] = None,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> AttestationResult:
    """Verify a single attestation envelope directly."""
    csid = claude_session if claude_session is not None else claude_session_id
    if not isinstance(csid, str) or not csid.strip():
        raise AttestUsageError("claude_session must be a non-empty string")
    csid = csid.strip()
    expected_session = session if session is not None else hermes_session

    trusted = None
    try:
        trusted = _check_anchor(anchor, fs, uid)
        if isinstance(envelope, AttestationEnvelope):
            envelope = envelope.to_dict()
        try:
            env = parse_envelope(envelope)
        except _envelope.EnvelopeError as exc:
            raise _malformed(exc.detail) from None
        try:
            payload = _envelope.decode_payload(env.payload)
        except _envelope.EnvelopeError as exc:
            raise _malformed(exc.detail) from None
        budget = [1]
        _evaluate_attestation(
            env,
            payload,
            trusted,
            uid=uid,
            now=now,
            claude_session=csid,
            session=expected_session,
            budget=budget,
        )
        return _ok(payload, trusted, now, 1)
    except _Deny as deny:
        return _denied(now, deny, trusted, 1 if trusted is not None else 0)
    except AttestUsageError:
        raise
    except Exception as exc:
        return _denied(
            now,
            _Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)),
            trusted,
            1 if trusted is not None else 0,
        )


def is_safe_session_component(value: Any) -> bool:
    """True when ``value`` can name one directory under ``session-attest/``.

    Refuses empty values, separators, NUL, ``.``/``..``, a leading dot and anything outside
    ``[A-Za-z0-9_-]`` (a Claude sid is a UUID). Capped at 128 characters.
    """
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    if value in (".", "..") or value.startswith("."):
        return False
    for ch in value:
        if not (ch.isascii() and (ch.isalnum() or ch in "-_")):
            return False
    return True


def _open_dir_nofollow(name: str, dir_fd: Optional[int] = None) -> int:
    """Open a directory without following a symlink at the last component."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(name, flags) if dir_fd is None else os.open(name, flags, dir_fd=dir_fd)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def verify_launch_provenance(
    *,
    claude_session: str,
    hermes_sessions: Sequence[str],
    uid: int,
    now: int,
    profile: Optional[str] = None,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> AttestationResult:
    """Did main ever sign ``claude_session -> one of hermes_sessions``? (b10 review, resume provenance)

    A resumed Claude sid is read from agent-writable state, so the backend may treat it as this
    session's own only when an attestation for that sid, naming this Hermes session (or an
    ancestor in the backend's in-memory lineage), carries main's signature.

    Rules, and how they differ from :func:`verify_attestation`:

    * **Key**: the kid must be ACTIVE in the anchor now. Retired, revoked and unknown kids fail.
      At rotation main re-signs every live attestation under the new kid, so a sid still worth
      resuming has an active-kid attestation; accepting a retired kid would let a leaked retired
      key forge history. A sid whose only attestations predate a rotation resumes unverified.
    * **Expiry does not matter**: this is history ("main mapped these ids"), not live authority.
      ``issued_at`` must still be at or after the key's ``not_before`` and the TTL cap holds.
    * **Unbound attestations count**: a revocation attestation is still main's statement of the
      mapping.
    * Directories are opened with ``O_NOFOLLOW``; a symlinked ``session-attest`` or sid dir
      finds nothing.
    """
    if not is_safe_session_component(claude_session):
        raise AttestUsageError("invalid claude_session format")
    accepted = frozenset(s for s in (hermes_sessions or ()) if isinstance(s, str) and s)
    if not accepted:
        raise AttestUsageError("hermes_sessions must name at least one session")

    trusted = None
    try:
        trusted = _check_anchor(anchor, fs, uid)
        try:
            parent_fd = _open_dir_nofollow(posixpath.join(trusted.grants_dir, "session-attest"))
        except OSError:
            return _denied(now, _Deny(REASON_NOT_FOUND, "no session-attest directory"), trusted)
        try:
            try:
                sid_fd = _open_dir_nofollow(claude_session, dir_fd=parent_fd)
            except OSError:
                return _denied(
                    now,
                    _Deny(REASON_NOT_FOUND, "no attestations for claude_session %s" % claude_session),
                    trusted,
                )
        finally:
            os.close(parent_fd)
        candidates = []
        try:
            for name in os.listdir(sid_fd):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                try:
                    env = read_envelope_file(name, dir_fd=sid_fd)
                    payload = _envelope.decode_payload(env.payload)
                except (_envelope.EnvelopeError, OSError):
                    continue
                issued = payload.get("issued_at")
                issued_int = issued if isinstance(issued, int) and not isinstance(issued, bool) else -1
                candidates.append((issued_int, name, env, payload))
        finally:
            os.close(sid_fd)
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not candidates:
            return _denied(now, _Deny(REASON_NOT_FOUND, "no candidate attestation files"), trusted)

        first_deny = None
        budget = MAX_SIGNATURE_CHECKS
        for _, _, env, payload in candidates:
            try:
                issued_at = payload.get("issued_at")
                if not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at < 0:
                    raise _malformed("issued_at must be a non-negative int")
                key = _require_active_key(trusted, env.kid, issued_at=issued_at, now=now)
                if budget <= 0:
                    break
                budget -= 1
                if not _signature_ok(key.pub, env.sign_bytes(), env.sig):
                    raise _Deny(REASON_BAD_SIGNATURE, "signature does not verify under %s" % env.kid)
                _validate_payload(payload)
                if AUDIENCE not in payload["aud"] or DISALLOWED_AUDIENCE in payload["aud"]:
                    raise _Deny(REASON_WRONG_AUDIENCE, "wrong audience", payload=payload)
                if payload["owner_uid"] != uid:
                    raise _Deny(REASON_UID_MISMATCH, "owner_uid mismatch", payload=payload)
                if payload["claude_session_id"] != claude_session:
                    raise _Deny(REASON_CLAUDE_SESSION_MISMATCH, "claude_session_id mismatch", payload=payload)
                if payload["hermes_session_id"] not in accepted:
                    raise _Deny(REASON_SESSION_MISMATCH, "hermes_session_id not in lineage", payload=payload)
                if profile is not None and payload["profile"] != profile:
                    raise _Deny(REASON_PROFILE_MISMATCH, "profile mismatch", payload=payload)
                if payload["expires_at"] - payload["issued_at"] > ATTEST_MAX_TTL_MS:
                    raise _Deny(REASON_TTL_EXCEEDED, "lifetime exceeds the cap", payload=payload)
            except _Deny as deny:
                if first_deny is None:
                    first_deny = deny
                continue
            return _ok(payload, trusted, now, candidates=len(candidates))
        return _denied(
            now,
            first_deny or _Deny(REASON_NOT_FOUND, "no matching attestation"),
            trusted,
            candidates=len(candidates),
        )
    except _Deny as deny:
        return _denied(now, deny, trusted)
    except AttestUsageError:
        raise
    except Exception as exc:
        return _denied(now, _Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)), trusted)


@dataclass(frozen=True)
class BindingResult:
    ok: bool
    reason: Optional[str] = None
    detail: Optional[str] = None
    profile: Optional[str] = None
    hermes_session_id: Optional[str] = None
    project_root: Optional[str] = None
    repo_common_root: Optional[str] = None
    repo_remote: Optional[str] = None
    binding_nonce: Optional[str] = None
    seq: Optional[int] = None
    state: Optional[str] = None
    bound_at: Optional[int] = None
    carried_from: Optional[str] = None
    project_id: Optional[str] = None
    binding: Optional[Dict[str, Any]] = None
    anchor_sha256: Optional[str] = None

    @property
    def exit_code(self) -> int:
        if self.ok:
            return EXIT_OK
        if self.reason in _ANCHOR_REASONS:
            return EXIT_ANCHOR
        if self.reason == REASON_NOT_FOUND:
            return EXIT_NOT_FOUND
        if self.reason == REASON_INTERNAL:
            return EXIT_INTERNAL
        return EXIT_DENY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "profile": self.profile,
            "hermes_session_id": self.hermes_session_id,
            "project_root": self.project_root,
            "repo_common_root": self.repo_common_root,
            "repo_remote": self.repo_remote,
            "binding_nonce": self.binding_nonce,
            "seq": self.seq,
            "state": self.state,
            "bound_at": self.bound_at,
            "carried_from": self.carried_from,
            "project_id": self.project_id,
            "verifier": {
                "version": VERSION,
                "impl": IMPL,
                "anchor_sha256": self.anchor_sha256,
            },
            "binding": self.binding,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _check_anchor_binding(
    anchor: Optional[_anchor.Anchor], fs: Any, uid: Optional[int]
) -> _anchor.Anchor:
    if anchor is None:
        try:
            anchor = _anchor.load_trusted_anchor(fs=fs)
        except _anchor.AnchorError as exc:
            raise _Deny(exc.reason, exc.detail) from None
    elif not isinstance(anchor, _anchor.Anchor):
        raise AttestUsageError("anchor must be a hermes_owner_grant.anchor.Anchor")
    if uid is not None and anchor.owner_uid != uid:
        raise _Deny(
            REASON_ANCHOR_UNTRUSTED,
            "anchor pins owner_uid %d, verifying uid is %d" % (anchor.owner_uid, uid),
        )
    return anchor


def _validate_binding_payload(p: Dict[str, Any]) -> None:
    for f in _REQUIRED_BINDING_PAYLOAD_FIELDS:
        if f not in p:
            raise _malformed("binding payload lacks %s" % f, payload=p)
    if not isinstance(p["v"], int) or isinstance(p["v"], bool) or p["v"] != PAYLOAD_VERSION:
        raise _malformed("binding payload v must be %d" % PAYLOAD_VERSION, payload=p)
    if not isinstance(p["aud"], list) or not all(isinstance(a, str) for a in p["aud"]):
        raise _malformed("binding aud must be a list of strings", payload=p)
    for name in ("owner_uid", "seq", "bound_at"):
        val = p[name]
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise _malformed("%s must be a non-negative int" % name, payload=p)
    for name in ("profile", "hermes_session_id", "binding_nonce"):
        val = p[name]
        if not isinstance(val, str) or not val:
            raise _malformed("%s must be a non-empty string" % name, payload=p)
    if p["state"] not in ("bound", "unbound"):
        raise _malformed("state must be 'bound' or 'unbound'", payload=p)

    proj = p["project_root"]
    if proj is not None:
        if not isinstance(proj, str) or not proj:
            raise _malformed("project_root must be null or a non-empty string", payload=p)
        if (
            not posixpath.isabs(proj)
            or posixpath.normpath(proj) != proj
            or (proj != "/" and proj.endswith("/"))
        ):
            raise _malformed("project_root must be an absolute and normalized path", payload=p)

    repo_common = p.get("repo_common_root")
    if repo_common is not None:
        if not isinstance(repo_common, str) or not repo_common:
            raise _malformed("repo_common_root must be null or a non-empty string", payload=p)
        if (
            not posixpath.isabs(repo_common)
            or posixpath.normpath(repo_common) != repo_common
            or (repo_common != "/" and repo_common.endswith("/"))
        ):
            raise _malformed("repo_common_root must be an absolute and normalized path", payload=p)

    for opt_field in ("repo_remote", "project_id", "carried_from"):
        val = p.get(opt_field)
        if val is not None and not isinstance(val, str):
            raise _malformed("%s must be null or a string" % opt_field, payload=p)


def _evaluate_binding(
    env: BindingEnvelope,
    payload: Dict[str, Any],
    anchor: _anchor.Anchor,
    *,
    uid: int,
    now: Optional[int] = None,
    expected_profile: Optional[str] = None,
    expected_session: Optional[str] = None,
) -> None:
    # 1. Anchor kid lookup and active kid check
    key = anchor.key(env.kid)
    if key is None:
        raise _Deny(_anchor.REASON_UNKNOWN_KID, "kid %s is not in the anchor" % env.kid)

    if key.status != _anchor.STATUS_ACTIVE:
        if key.status == _anchor.STATUS_RETIRED:
            raise _Deny(_anchor.REASON_KEY_RETIRED, "key %s is retired (bindings require active kid)" % env.kid)
        if key.status == _anchor.STATUS_REVOKED:
            raise _Deny(_anchor.REASON_KEY_REVOKED, "key %s is revoked" % env.kid)
        raise _Deny(_anchor.REASON_KEY_NOT_YET_VALID, "key %s is not active (%s)" % (env.kid, key.status))

    # 2. Shape validation
    _validate_binding_payload(payload)

    bound_at = payload["bound_at"]
    if bound_at < key.not_before:
        raise _Deny(_anchor.REASON_KEY_NOT_YET_VALID, "binding bound_at predates key not_before")
    if now is not None and now < key.not_before:
        raise _Deny(_anchor.REASON_KEY_NOT_YET_VALID, "verifying time predates key not_before")

    # 3. Signature verification
    if not _signature_ok(key.pub, env.sign_bytes(), env.sig):
        raise _Deny(REASON_BAD_SIGNATURE, "signature does not verify under %s" % env.kid)

    # 4. Audience and UID
    if BINDING_AUDIENCE not in payload["aud"]:
        raise _Deny(REASON_WRONG_AUDIENCE, "aud does not include %s" % BINDING_AUDIENCE, payload=payload)
    if DISALLOWED_AUDIENCE in payload["aud"]:
        raise _Deny(REASON_WRONG_AUDIENCE, "aud must not include %s" % DISALLOWED_AUDIENCE, payload=payload)
    if payload["owner_uid"] != uid:
        raise _Deny(
            REASON_UID_MISMATCH,
            "binding owner_uid %d, verifying uid %d" % (payload["owner_uid"], uid),
            payload=payload,
        )

    # 5. Profile and Session matches
    if expected_profile is not None and payload["profile"] != expected_profile:
        raise _Deny(
            REASON_PROFILE_MISMATCH,
            "binding profile %s does not match expected %s" % (payload["profile"], expected_profile),
            payload=payload,
        )
    if expected_session is not None and payload["hermes_session_id"] != expected_session:
        raise _Deny(
            REASON_SESSION_MISMATCH,
            "binding hermes_session_id %s does not match expected %s" % (payload["hermes_session_id"], expected_session),
            payload=payload,
        )

    # 6. Bound state and project_root
    if payload["state"] != "bound":
        raise _Deny(REASON_UNBOUND, "binding state is %s (expected bound)" % payload["state"], payload=payload)
    if not payload.get("project_root"):
        raise _Deny(REASON_UNBOUND, "bound binding has null or empty project_root", payload=payload)


def _binding_ok(
    payload: Dict[str, Any],
    anchor: Any,
) -> BindingResult:
    return BindingResult(
        ok=True,
        reason=None,
        detail=None,
        profile=payload.get("profile"),
        hermes_session_id=payload.get("hermes_session_id"),
        project_root=payload.get("project_root"),
        repo_common_root=payload.get("repo_common_root"),
        repo_remote=payload.get("repo_remote"),
        binding_nonce=payload.get("binding_nonce"),
        seq=payload.get("seq"),
        state=payload.get("state"),
        bound_at=payload.get("bound_at"),
        carried_from=payload.get("carried_from"),
        project_id=payload.get("project_id"),
        binding=payload,
        anchor_sha256=getattr(anchor, "sha256", None),
    )


def _binding_denied(
    deny: _Deny,
    anchor: Any = None,
) -> BindingResult:
    payload = deny.payload
    return BindingResult(
        ok=False,
        reason=deny.reason,
        detail=deny.detail,
        profile=payload.get("profile") if payload else None,
        hermes_session_id=payload.get("hermes_session_id") if payload else None,
        project_root=payload.get("project_root") if payload else None,
        repo_common_root=payload.get("repo_common_root") if payload else None,
        repo_remote=payload.get("repo_remote") if payload else None,
        binding_nonce=payload.get("binding_nonce") if payload else None,
        seq=payload.get("seq") if payload else None,
        state=payload.get("state") if payload else None,
        bound_at=payload.get("bound_at") if payload else None,
        carried_from=payload.get("carried_from") if payload else None,
        project_id=payload.get("project_id") if payload else None,
        binding=payload,
        anchor_sha256=getattr(anchor, "sha256", None),
    )


def verify_binding_envelope(
    envelope: Union[BindingEnvelope, Mapping[str, Any], bytes, str],
    *,
    profile: Optional[str] = None,
    expected_profile: Optional[str] = None,
    session: Optional[str] = None,
    hermes_session: Optional[str] = None,
    hermes_session_id: Optional[str] = None,
    expected_session: Optional[str] = None,
    uid: Optional[int] = None,
    owner_uid: Optional[int] = None,
    now: Optional[int] = None,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> BindingResult:
    """Verify a single binding envelope directly."""
    prof = expected_profile if expected_profile is not None else profile
    sess = expected_session
    if sess is None:
        sess = hermes_session_id if hermes_session_id is not None else (session or hermes_session)
    target_uid = uid if uid is not None else owner_uid

    trusted = None
    try:
        trusted = _check_anchor_binding(anchor, fs, target_uid)
        eff_uid = target_uid if target_uid is not None else trusted.owner_uid
        if isinstance(envelope, (BindingEnvelope, AttestationEnvelope)):
            envelope = envelope.to_dict()
        try:
            env = parse_binding_envelope(envelope)
        except _envelope.EnvelopeError as exc:
            raise _malformed(exc.detail) from None
        try:
            payload = _envelope.decode_payload(env.payload)
        except _envelope.EnvelopeError as exc:
            raise _malformed(exc.detail) from None

        _evaluate_binding(
            env,
            payload,
            trusted,
            uid=eff_uid,
            now=now,
            expected_profile=prof,
            expected_session=sess,
        )
        return _binding_ok(payload, trusted)
    except _Deny as deny:
        return _binding_denied(deny, trusted)
    except AttestUsageError:
        raise
    except Exception as exc:
        return _binding_denied(
            _Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)),
            trusted,
        )


def verify_binding_file(
    path: str,
    *,
    profile: Optional[str] = None,
    expected_profile: Optional[str] = None,
    session: Optional[str] = None,
    hermes_session: Optional[str] = None,
    hermes_session_id: Optional[str] = None,
    expected_session: Optional[str] = None,
    uid: Optional[int] = None,
    owner_uid: Optional[int] = None,
    now: Optional[int] = None,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
    max_bytes: int = MAX_BINDING_BYTES,
) -> BindingResult:
    """Read and verify a binding envelope file."""
    try:
        env = read_binding_file(path, max_bytes=max_bytes)
    except _envelope.EnvelopeError as exc:
        reason = REASON_NOT_FOUND if "cannot open binding file" in exc.detail else REASON_MALFORMED
        return _binding_denied(_Deny(reason, exc.detail))
    except Exception as exc:
        return _binding_denied(_Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)))
    return verify_binding_envelope(
        env,
        profile=profile,
        expected_profile=expected_profile,
        session=session,
        hermes_session=hermes_session,
        hermes_session_id=hermes_session_id,
        expected_session=expected_session,
        uid=uid,
        owner_uid=owner_uid,
        now=now,
        anchor=anchor,
        fs=fs,
    )
