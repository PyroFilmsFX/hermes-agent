"""Owner-grant verifier: the check pipeline and candidate-first lookup (addendum §2.4, §3.2-3.4; U5).

Two entry points with the same semantics:

* ``verify(...)`` looks grants up in the anchor's ``grants_dir`` by session plus a grant id,
  a text hash, a quote, or only the requested scopes. It returns the newest grant that passes
  every check.
* ``verify_envelope(envelope, ...)`` runs the same pipeline on one envelope a caller already
  holds (for example ``display_metadata.owner_grant.envelope``).

Checks, in order. Each one fails closed and names its reason code:

1. anchor trusted (``anchor_missing`` / ``anchor_untrusted``). This includes the anchor
   pinning *this* process's uid, because an anchor for another account is not ours to trust.
2. envelope well formed (``malformed``) and kid listed in the anchor (``unknown_kid``);
3. key status (``key_revoked`` / ``key_retired`` / ``key_not_yet_valid``);
4. Ed25519 signature over the domain prefix plus the exact payload bytes (``bad_signature``),
   then the signed payload's schema (``malformed``);
5. audience (``wrong_audience``) and ``owner_uid`` equal to the verifying uid (``uid_mismatch``);
6. session binding (``session_mismatch`` / ``claude_session_mismatch``);
7. time: ``issued_at <= t <= expires_at`` with 5 s skew, plus optional max age (``expired``).
   Also ``expires_at - issued_at`` must be within the tightest max TTL among the grant's
   scope classes (``ttl_exceeded``). Without that cap, a leaked retired key could mint a
   backdated grant that never expires;
8. every requested scope present by exact string match (``scope_missing``);
9. subject for requested scopes that carry one (``subject_required`` / ``subject_mismatch``);
10. text hash or quote tier (``quote_mismatch`` / ``quote_fragment`` / ``quote_too_short``).

Steps 11 (revocation list) and 12 (``--consume``) belong to U6 and are not implemented here.

Lookup (G-17): list ``grants_dir`` and keep the names whose ``issued_at`` prefix falls
inside the longest TTL window. Then decode those payloads *unverified*, only to filter on
target, scope and hash or quote. The full pipeline, including the signature check, runs only
on the surviving candidates, newest first, and at most ``MAX_SIGNATURE_CHECKS`` of them. An
unverified decode can exclude a file but never admit one. A lookup finding no grant returns
``not_found`` (CLI exit 4).

The verifier never reads state.db, a transcript, the environment, the clock or the process
uid. The caller passes ``session``, ``uid`` and ``now``. The CLI (U7) derives them from its
own trusted context.

Stdlib-only and Python 3.9 compatible: this module runs under ``/usr/bin/python3 -I -S``.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import anchor as _anchor
from . import ed25519_pure
from . import envelope as _envelope
from . import quote as _quote
from . import scopes as _scopes

SCHEMA = "hermes-owner-verify/v1"
VERSION = "1.0.0"
IMPL = "pure"
AUDIENCE = "hermes-owner-verify"
PAYLOAD_VERSION = 1
SKEW_MS = 5000
MAX_TARGETS = 5
# Longest lookup window by file name (the quote-only class max, 7 d). A name is only a hint:
# every time check runs again on the signed payload.
LOOKUP_WINDOW_MS = max(_scopes.MAX_TTL_MS_BY_CLASS.values())
# Grants live in an agent-writable directory. Bounding the signature checks turns a flood of
# forged candidates into a denial (fail closed), not an unbounded hook stall.
MAX_SIGNATURE_CHECKS = 32

REASON_MALFORMED = _envelope.REASON_MALFORMED
REASON_ANCHOR_MISSING = _anchor.REASON_ANCHOR_MISSING
REASON_ANCHOR_UNTRUSTED = _anchor.REASON_ANCHOR_UNTRUSTED
REASON_BAD_SIGNATURE = "bad_signature"
REASON_WRONG_AUDIENCE = "wrong_audience"
REASON_UID_MISMATCH = "uid_mismatch"
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_CLAUDE_SESSION_MISMATCH = "claude_session_mismatch"
REASON_EXPIRED = "expired"
REASON_TTL_EXCEEDED = "ttl_exceeded"
REASON_SCOPE_MISSING = "scope_missing"
REASON_SUBJECT_REQUIRED = "subject_required"
REASON_SUBJECT_MISMATCH = "subject_mismatch"
REASON_QUOTE_MISMATCH = "quote_mismatch"
REASON_QUOTE_FRAGMENT = "quote_fragment"
REASON_QUOTE_TOO_SHORT = "quote_too_short"
REASON_NOT_FOUND = "not_found"
REASON_REVOKED = "revoked"
REASON_ALREADY_CONSUMED = "already_consumed"
REASON_INTERNAL = "internal_error"

EXIT_OK = 0
EXIT_DENY = 1
EXIT_USAGE = 2
EXIT_ANCHOR = 3
EXIT_NOT_FOUND = 4
EXIT_INTERNAL = 5
# A passing result that is evidence only, never authorization: an audit re-evaluation
# (audit_at) or a fragment quote accepted with allow_fragment. ``ok`` stays true.
EXIT_EVIDENCE = 6

_MIN_QUOTE = 12  # addendum §4.2; quote.match_quote enforces the same default
_ANCHOR_REASONS = frozenset((REASON_ANCHOR_MISSING, REASON_ANCHOR_UNTRUSTED))
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_PAYLOAD_FIELDS = (
    "v",
    "aud",
    "decision_id",
    "issued_at",
    "deliver_by",
    "expires_at",
    "owner_uid",
    "backend",
    "nonce",
    "gesture",
    "confirm",
    "source_session",
    "targets",
    "scope",
    "single_use",
    "subject",
    "text",
    "text_sha256",
    "text_len",
)
_TARGET_FIELDS = frozenset(("session_id", "claude_session_id"))
_SOURCE_FIELDS = frozenset(("session_id", "message_id", "role"))


class VerifyUsageError(ValueError):
    """The caller passed an invalid request (CLI exit 2). Never a statement about a grant."""


class _Deny(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail
        self.grant = None  # the _Grant, set only once its signature has verified
        self.match = None  # the step-10 match, set only when step 10 ran


# -- result --------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: Optional[str]
    detail: Optional[str]
    grant: Optional[Dict[str, Any]]  # set only once the signature has verified
    match: Optional[Dict[str, Any]]
    candidates: int
    audit: bool
    checked_at: int
    anchor_sha256: Optional[str]
    consumed: Optional[List[Dict[str, str]]] = None
    text: Optional[str] = None  # owner text; reported only with include_text
    tier: str = "signed"

    @property
    def exit_code(self) -> int:
        if self.ok:
            if self.audit or (
                self.match is not None and self.match.get("kind") == "fragment"
            ):
                return EXIT_EVIDENCE
            return EXIT_OK
        if self.reason in _ANCHOR_REASONS:
            return EXIT_ANCHOR
        if self.reason == REASON_NOT_FOUND:
            return EXIT_NOT_FOUND
        if self.reason == REASON_INTERNAL:
            return EXIT_INTERNAL
        return EXIT_DENY

    def to_dict(self) -> Dict[str, Any]:
        grant = None
        if self.grant is not None:
            grant = dict(self.grant)
            if self.text is not None:
                grant["text"] = self.text
        return {
            "schema": SCHEMA,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "tier": self.tier,
            "grant": grant,
            "match": dict(self.match) if self.match is not None else None,
            "consumed": self.consumed,
            "candidates": self.candidates,
            "audit": self.audit,
            "checked_at": self.checked_at,
            "verifier": {
                "version": VERSION,
                "impl": IMPL,
                "anchor_sha256": self.anchor_sha256,
            },
        }


# -- request validation --------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != "" and "\x00" not in value


@dataclass(frozen=True)
class _Request:
    session: str
    claude_session: Optional[str]
    uid: int
    now: int
    at: int  # the instant step 7 is evaluated at (now, or audit_at)
    audit: bool
    grant_id: Optional[str]
    text_sha: Optional[str]
    quote: Optional[str]
    scopes: Tuple[str, ...]
    subject: Optional[str]
    allow_fragment: bool
    max_age_ms: Optional[int]
    include_text: bool
    consume: bool


def _request(
    *,
    session: Any,
    claude_session: Any,
    uid: Any,
    now: Any,
    grant_id: Any,
    text_sha: Any,
    quote: Any,
    scopes: Any,
    subject: Any,
    allow_fragment: Any,
    max_age_s: Any,
    audit_at: Any,
    include_text: Any,
    consume: Any,
) -> _Request:
    if not _nonempty_str(session):
        raise VerifyUsageError("session must be a non-empty string")
    if claude_session is not None and not _nonempty_str(claude_session):
        raise VerifyUsageError("claude_session must be null or a non-empty string")
    if not _is_int(uid) or uid < 0:
        raise VerifyUsageError("uid must be a non-negative int")
    if not _is_int(now) or now < 0:
        raise VerifyUsageError("now must be a non-negative int (epoch ms)")
    if audit_at is not None and (not _is_int(audit_at) or audit_at < 0):
        raise VerifyUsageError("audit_at must be a non-negative int (epoch ms)")
    if consume and audit_at is not None:
        raise VerifyUsageError("consume cannot be combined with audit_at")
    if max_age_s is not None and (not _is_int(max_age_s) or max_age_s < 0):
        raise VerifyUsageError("max_age_s must be a non-negative int (seconds)")
    selectors = [s for s in (grant_id, text_sha, quote) if s is not None]
    if len(selectors) > 1:
        raise VerifyUsageError("pass at most one of grant_id, text_sha, quote")
    if grant_id is not None and not _envelope.is_grant_id(grant_id):
        raise VerifyUsageError("grant_id is not an og_ id")
    if text_sha is not None:
        if not isinstance(text_sha, str) or not _HEX64_RE.match(text_sha.lower()):
            raise VerifyUsageError("text_sha must be 64 hex characters")
        text_sha = text_sha.lower()
    if quote is not None and not isinstance(quote, str):
        raise VerifyUsageError("quote must be a string")
    if isinstance(scopes, (str, bytes)) or scopes is None:
        raise VerifyUsageError("scopes must be a sequence of scope strings")
    wanted = []
    for value in scopes:
        try:
            _scopes.parse_scope(value)  # grammar only: no wildcards, exact strings
        except _scopes.ScopeError as exc:
            raise VerifyUsageError("scope %r: %s" % (value, exc)) from None
        if value not in wanted:
            wanted.append(value)
    if subject is not None and not _nonempty_str(subject):
        raise VerifyUsageError("subject must be null or a non-empty string")
    return _Request(
        session=session,
        claude_session=claude_session,
        uid=uid,
        now=now,
        at=now if audit_at is None else audit_at,
        audit=audit_at is not None,
        grant_id=grant_id,
        text_sha=text_sha,
        quote=quote,
        scopes=tuple(wanted),
        subject=subject,
        allow_fragment=bool(allow_fragment),
        max_age_ms=None if max_age_s is None else max_age_s * 1000,
        include_text=bool(include_text),
        consume=bool(consume),
    )


# -- signed payload schema -----------------------------------------------------------------


@dataclass(frozen=True)
class _Grant:
    grant_id: str
    kid: str
    payload: Dict[str, Any]

    def summary(self) -> Dict[str, Any]:
        p = self.payload
        return {
            "id": self.grant_id,
            "kid": self.kid,
            "decision_id": p["decision_id"],
            "issued_at": p["issued_at"],
            "expires_at": p["expires_at"],
            "gesture": p["gesture"],
            "confirm": p["confirm"],
            "source_session": dict(p["source_session"]),
            "targets": [dict(t) for t in p["targets"]],
            "scope": list(p["scope"]),
            "single_use": list(p["single_use"]),
            "text_sha256": p["text_sha256"],
            "text_len": p["text_len"],
        }


def _malformed(detail: str) -> _Deny:
    return _Deny(REASON_MALFORMED, detail)


def _str_list(value: Any, what: str) -> List[str]:
    if not isinstance(value, list) or not all(_nonempty_str(v) for v in value):
        raise _malformed("%s must be a list of non-empty strings" % what)
    if len(set(value)) != len(value):
        raise _malformed("%s has duplicates" % what)
    return value


def _validate_payload(p: Dict[str, Any]) -> None:
    """Shape-check a payload whose signature has verified. Extra signed fields are allowed
    (forward compatible); every field the checks rely on must be present and typed."""
    missing = [f for f in _PAYLOAD_FIELDS if f not in p]
    if missing:
        raise _malformed("payload lacks %s" % ", ".join(missing))
    if not _is_int(p["v"]) or p["v"] != PAYLOAD_VERSION:
        raise _malformed("payload v must be %d" % PAYLOAD_VERSION)
    _str_list(p["aud"], "aud")
    for name in ("issued_at", "deliver_by", "expires_at", "owner_uid", "text_len"):
        if not _is_int(p[name]) or p[name] < 0:
            raise _malformed("%s must be a non-negative int" % name)
    if p["expires_at"] <= p["issued_at"]:
        raise _malformed("expires_at must be after issued_at")
    for name in ("decision_id", "backend", "nonce", "gesture", "confirm"):
        if not _nonempty_str(p[name]):
            raise _malformed("%s must be a non-empty string" % name)

    source = p["source_session"]
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise _malformed("source_session must have exactly %s" % sorted(_SOURCE_FIELDS))
    if not _nonempty_str(source["session_id"]):
        raise _malformed("source_session.session_id must be a non-empty string")
    for name in ("message_id", "role"):
        if source[name] is not None and not isinstance(source[name], str):
            raise _malformed("source_session.%s must be null or a string" % name)

    targets = p["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise _malformed("targets must list 1..%d sessions" % MAX_TARGETS)
    seen = set()
    for i, target in enumerate(targets):
        if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
            raise _malformed(
                "targets[%d] must have exactly %s" % (i, sorted(_TARGET_FIELDS))
            )
        if not _nonempty_str(target["session_id"]):
            raise _malformed("targets[%d].session_id must be a non-empty string" % i)
        claude = target["claude_session_id"]
        if claude is not None and not _nonempty_str(claude):
            raise _malformed(
                "targets[%d].claude_session_id must be null or non-empty" % i
            )
        if target["session_id"] in seen:
            raise _malformed("targets repeat session %r" % target["session_id"])
        seen.add(target["session_id"])

    scope = _str_list(p["scope"], "scope")
    parsed = {}
    for value in scope:
        try:
            parsed[value] = _scopes.parse_scope(value)
        except _scopes.ScopeError:
            raise _malformed("scope %r is outside the grammar" % value) from None
    single_use = _str_list(p["single_use"], "single_use")
    if not set(single_use) <= set(scope):
        raise _malformed("single_use lists a scope the grant doesn't carry")
    needs_single_use = {v for v, s in parsed.items() if s.single_use}
    if not needs_single_use <= set(single_use):
        raise _malformed("single_use omits a single-use class scope")
    subject = p["subject"]
    if not isinstance(subject, dict) or not all(
        _nonempty_str(v) for v in subject.values()
    ):
        raise _malformed("subject must map scopes to non-empty strings")
    if not set(subject) <= set(scope):
        raise _malformed("subject names a scope the grant doesn't carry")
    for value, parsed_scope in parsed.items():
        if parsed_scope.subject_required and value not in subject:
            raise _malformed("%s requires a signed subject" % value)

    text = p["text"]
    if not isinstance(text, str):
        raise _malformed("text must be a string")
    if not isinstance(p["text_sha256"], str) or not _HEX64_RE.match(p["text_sha256"]):
        raise _malformed("text_sha256 must be 64 lowercase hex")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != p["text_sha256"]:
        raise _malformed("text_sha256 does not match text")
    if p["text_len"] != len(text.encode("utf-8")):
        raise _malformed("text_len does not match UTF-8 byte length")


def validate_payload(payload: Any) -> Dict[str, Any]:
    """Public schema check for a payload whose envelope signature was verified by a caller.

    This deliberately validates shape only; audience, time, session, uid and scope checks stay
    with the caller's authorization flow.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    try:
        _validate_payload(payload)
    except _Deny as exc:
        raise ValueError(exc.detail) from None
    return payload


# -- the pipeline --------------------------------------------------------------------------


def _signature_ok(pub: bytes, message: bytes, sig: bytes) -> bool:
    """Step 4. Pure Ed25519 only: the hook path has no cryptography package, and one
    implementation means one acceptance set (S < L and canonical points, strict)."""
    return ed25519_pure.verify(pub, message, sig)


def _check_anchor(anchor: Any, fs: Any, uid: int) -> Any:
    """Step 1. Returns the trusted anchor or raises _Deny."""
    if anchor is None:
        try:
            anchor = _anchor.load_trusted_anchor(fs=fs)
        except _anchor.AnchorError as exc:
            raise _Deny(exc.reason, exc.detail) from None
    elif not isinstance(anchor, _anchor.Anchor):
        raise VerifyUsageError("anchor must be a hermes_owner_grant.anchor.Anchor")
    if anchor.owner_uid != uid:
        raise _Deny(
            REASON_ANCHOR_UNTRUSTED,
            "anchor pins owner_uid %d, verifying uid is %d" % (anchor.owner_uid, uid),
        )
    return anchor


def _match_text(req: _Request, grant: Mapping[str, Any]) -> Dict[str, Any]:
    """Step 10 against (possibly unverified) payload fields. Returns the match object with
    ``ok`` and ``reason`` alongside the reported ``kind``/``segment_index``."""
    if req.grant_id is not None:
        return {"ok": True, "reason": None, "kind": "grant_id", "segment_index": None}
    if req.text_sha is not None:
        ok = grant.get("text_sha256") == req.text_sha
        return {
            "ok": ok,
            "reason": None if ok else REASON_QUOTE_MISMATCH,
            "kind": "sha" if ok else None,
            "segment_index": None,
        }
    if req.quote is not None:
        text = grant.get("text")
        if not isinstance(text, str):
            return {
                "ok": False,
                "reason": REASON_QUOTE_MISMATCH,
                "kind": None,
                "segment_index": None,
            }
        m = _quote.match_quote(req.quote, text, allow_fragment=req.allow_fragment)
        kind = m.kind if m.kind in ("exact", "segment", "fragment") else None
        return {
            "ok": m.ok,
            "reason": m.reason,
            "kind": kind,
            "segment_index": m.segment_index,
        }
    return {"ok": True, "reason": None, "kind": None, "segment_index": None}


def _public_match(match: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if match is None:
        return None
    return {"kind": match["kind"], "segment_index": match["segment_index"]}


def _evaluate(
    env: _envelope.Envelope, req: _Request, anchor: Any, budget: List[int]
) -> Tuple[Optional[_Grant], Optional[Dict[str, Any]], Optional[List[Dict[str, str]]]]:
    """Steps 2-10 on one envelope. Returns (grant, match) or raises _Deny. ``grant`` is
    attached to the deny when the signature had already verified."""
    # Step 2: kid listed (shape was checked by parse_envelope).
    key = anchor.key(env.kid)
    if key is None:
        raise _Deny(_anchor.REASON_UNKNOWN_KID, "kid %s is not in the anchor" % env.kid)
    # Step 3: key status needs issued_at, read here before the signature. A forged value can
    # only produce a denial; once the signature verifies, the value is the owner's.
    try:
        unverified = _envelope.decode_payload(env.payload)
    except _envelope.EnvelopeError as exc:
        raise _malformed(exc.detail) from None
    issued_at = unverified.get("issued_at")
    if not _is_int(issued_at) or issued_at < 0:
        raise _malformed("issued_at must be a non-negative int")
    key_reason = _anchor.check_key(anchor, env.kid, issued_at=issued_at, now=req.now)
    if key_reason is not None:
        raise _Deny(key_reason, "key %s: %s" % (env.kid, key_reason))
    # Step 4: signature over the exact bytes, then the signed schema.
    if budget[0] <= 0:
        raise _Deny(REASON_BAD_SIGNATURE, "signature-check budget exhausted")
    budget[0] -= 1
    if not _signature_ok(key.pub, env.sign_bytes(), env.sig):
        raise _Deny(
            REASON_BAD_SIGNATURE, "signature does not verify under %s" % env.kid
        )
    p = unverified
    grant = _Grant(grant_id=env.grant_id, kid=env.kid, payload=p)
    try:
        _validate_payload(p)
        match = _evaluate_signed(grant, req)
        if _is_revoked(anchor.grants_dir, grant.grant_id):
            raise _Deny(REASON_REVOKED, "grant is listed in revoked.jsonl")
        consumed = (
            _consume_grant(anchor.grants_dir, grant, req) if req.consume else None
        )
        return grant, match, consumed
    except _Deny as deny:
        if deny.reason != REASON_MALFORMED:
            deny.grant = grant
        raise


def _evaluate_signed(grant: _Grant, req: _Request) -> Dict[str, Any]:
    p = grant.payload
    # Step 5: audience, owner uid.
    if AUDIENCE not in p["aud"]:
        raise _Deny(REASON_WRONG_AUDIENCE, "aud does not include %s" % AUDIENCE)
    if p["owner_uid"] != req.uid:
        raise _Deny(
            REASON_UID_MISMATCH,
            "grant owner_uid %d, verifying uid %d" % (p["owner_uid"], req.uid),
        )
    # Step 6: session binding. A grant that names a Claude session id requires the caller's
    # (hook stdin) id to match it; env alone may be overridable (addendum §4.1, T-6).
    target = next((t for t in p["targets"] if t["session_id"] == req.session), None)
    if target is None:
        raise _Deny(REASON_SESSION_MISMATCH, "session is not a target of this grant")
    if target["claude_session_id"] is not None and (
        req.claude_session != target["claude_session_id"]
    ):
        raise _Deny(
            REASON_CLAUDE_SESSION_MISMATCH,
            "the grant binds a Claude session id the caller did not match",
        )
    # Step 7: time, with the scope-class TTL cap.
    ttl = p["expires_at"] - p["issued_at"]
    cap = _scopes.max_ttl_ms_for_scopes(p["scope"])
    if ttl > cap:
        raise _Deny(
            REASON_TTL_EXCEEDED,
            "grant lifetime %d ms exceeds the %d ms cap for its scope classes"
            % (ttl, cap),
        )
    if req.at < p["issued_at"] - SKEW_MS:
        raise _Deny(REASON_EXPIRED, "grant is issued in the future")
    if req.at > p["expires_at"] + SKEW_MS:
        raise _Deny(REASON_EXPIRED, "grant expired")
    if req.max_age_ms is not None and req.at - p["issued_at"] > req.max_age_ms:
        raise _Deny(REASON_EXPIRED, "grant is older than max_age")
    # Step 8: exact scope strings.
    missing = [s for s in req.scopes if s not in p["scope"]]
    if missing:
        raise _Deny(REASON_SCOPE_MISSING, "grant lacks %s" % ", ".join(missing))
    # Step 9: subject for each requested scope that carries one.
    for value in req.scopes:
        if value in p["subject"]:
            if req.subject is None:
                raise _Deny(REASON_SUBJECT_REQUIRED, "%s needs --subject" % value)
            if req.subject != p["subject"][value]:
                raise _Deny(REASON_SUBJECT_MISMATCH, "subject differs for %s" % value)
    # Step 10: hash or quote tier, against the signed text.
    match = _match_text(req, p)
    if not match["ok"]:
        deny = _Deny(
            match["reason"] or REASON_QUOTE_MISMATCH, "quote or hash does not match"
        )
        deny.match = match
        raise deny
    return match


def _is_revoked(grants_dir: str, grant_id: str) -> bool:
    """Read the advisory revocation ledger; malformed or unreadable data fails closed."""
    path = posixpath.join(grants_dir, "revoked.jsonl")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return True
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            if os.fstat(source.fileno()).st_size > 1024 * 1024:
                return True
            for line in source:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    return True
                if (
                    not isinstance(row, dict)
                    or not _envelope.is_grant_id(row.get("grant_id"))
                    or not _is_int(row.get("revoked_at"))
                    or not _nonempty_str(row.get("by"))
                ):
                    return True
                if row["grant_id"] == grant_id:
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _consume_grant(
    grants_dir: str, grant: _Grant, req: _Request
) -> Optional[List[Dict[str, str]]]:
    """Atomically mark requested signed single-use scopes and append their audit rows."""
    signed = grant.payload["single_use"]
    scopes = [scope for scope in (req.scopes or signed) if scope in signed]
    if not scopes:
        return None
    directory = posixpath.join(grants_dir, "consumed")
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    dir_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(directory, dir_flags)
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        os.close(directory_fd)
        raise OSError("consumed path is not a directory")

    records = []
    try:
        for scope in scopes:
            digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
            record = {"scope": scope, "scope_hash": digest}
            payload = {
                "grant_id": grant.grant_id,
                "scope": scope,
                "scope_hash": digest,
                "consumed_at": req.now,
                "session": req.session,
            }
            try:
                fd = os.open(
                    grant.grant_id + "." + digest,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                raise _Deny(
                    REASON_ALREADY_CONSUMED, "single-use scope was already consumed"
                ) from None
            try:
                marker_data = json.dumps(payload, sort_keys=True).encode("utf-8")
                if os.write(fd, marker_data) != len(marker_data):
                    raise OSError("short consumption marker write")
            finally:
                os.close(fd)
            records.append(record)
            line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            audit_fd = os.open(
                posixpath.join(grants_dir, "consumed.jsonl"),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(audit_fd).st_mode):
                    raise OSError("consumption audit path is not a regular file")
                if os.write(audit_fd, line) != len(line):
                    raise OSError("short consumption audit write")
            finally:
                os.close(audit_fd)
    finally:
        os.close(directory_fd)
    return records


# -- results -------------------------------------------------------------------------------


def _ok(
    req: _Request,
    anchor: Any,
    grant: _Grant,
    match: Mapping[str, Any],
    n: int,
    consumed: Optional[List[Dict[str, str]]] = None,
):
    return VerifyResult(
        ok=True,
        reason=None,
        detail=None,
        grant=grant.summary(),
        match=_public_match(match),
        candidates=n,
        audit=req.audit,
        checked_at=req.now,
        anchor_sha256=anchor.sha256,
        consumed=consumed,
        text=grant.payload["text"] if req.include_text else None,
    )


def _denied(
    req: Optional[_Request],
    now: int,
    deny: _Deny,
    anchor: Any = None,
    n: int = 0,
    match: Optional[Mapping[str, Any]] = None,
) -> VerifyResult:
    grant = deny.grant
    if deny.match is not None:
        match = deny.match
    return VerifyResult(
        ok=False,
        reason=deny.reason,
        detail=deny.detail,
        grant=grant.summary() if grant is not None else None,
        match=_public_match(match),
        candidates=n,
        audit=req.audit if req is not None else False,
        checked_at=now,
        anchor_sha256=getattr(anchor, "sha256", None),
    )


def _internal(req: _Request, exc: BaseException, anchor: Any = None) -> VerifyResult:
    return _denied(
        req,
        req.now,
        _Deny(REASON_INTERNAL, "%s: %s" % (type(exc).__name__, exc)),
        anchor,
    )


# -- entry points --------------------------------------------------------------------------


def verify_envelope(
    envelope: Union[_envelope.Envelope, Mapping[str, Any], bytes, str],
    *,
    session: str,
    uid: int,
    now: int,
    claude_session: Optional[str] = None,
    text_sha: Optional[str] = None,
    quote: Optional[str] = None,
    scopes: Sequence[str] = (),
    subject: Optional[str] = None,
    allow_fragment: bool = False,
    max_age_s: Optional[int] = None,
    audit_at: Optional[int] = None,
    include_text: bool = False,
    consume: bool = False,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> VerifyResult:
    """Run the pipeline on one envelope. ``anchor=`` is for tests and trusted in-process
    callers only; by default the root-owned anchor at the hard-coded path is loaded."""
    req = _request(
        session=session,
        claude_session=claude_session,
        uid=uid,
        now=now,
        grant_id=None,
        text_sha=text_sha,
        quote=quote,
        scopes=scopes,
        subject=subject,
        allow_fragment=allow_fragment,
        max_age_s=max_age_s,
        audit_at=audit_at,
        include_text=include_text,
        consume=consume,
    )
    trusted = None
    try:
        trusted = _check_anchor(anchor, fs, req.uid)
        if isinstance(envelope, _envelope.Envelope):
            envelope = envelope.to_dict()  # re-check the shape of a caller-built object
        try:
            env = _envelope.parse_envelope(envelope)
        except _envelope.EnvelopeError as exc:
            raise _malformed(exc.detail) from None
        grant, match, consumed = _evaluate(env, req, trusted, [1])
        return _ok(req, trusted, grant, match, 1, consumed)
    except _Deny as deny:
        return _denied(req, req.now, deny, trusted, 1 if trusted is not None else 0)
    except VerifyUsageError:
        raise
    except Exception as exc:  # fail closed: CLI exit 5
        return _internal(req, exc, trusted)


def verify(
    *,
    session: str,
    uid: int,
    now: int,
    claude_session: Optional[str] = None,
    grant_id: Optional[str] = None,
    text_sha: Optional[str] = None,
    quote: Optional[str] = None,
    scopes: Sequence[str] = (),
    subject: Optional[str] = None,
    allow_fragment: bool = False,
    max_age_s: Optional[int] = None,
    audit_at: Optional[int] = None,
    include_text: bool = False,
    consume: bool = False,
    anchor: Optional[_anchor.Anchor] = None,
    fs: Any = None,
) -> VerifyResult:
    """Find the newest grant in the anchor's ``grants_dir`` that passes every check for
    this session. Pass at most one of ``grant_id``/``text_sha``/``quote``; with none,
    ``scopes`` must be non-empty (``verify --session S --scope X``)."""
    req = _request(
        session=session,
        claude_session=claude_session,
        uid=uid,
        now=now,
        grant_id=grant_id,
        text_sha=text_sha,
        quote=quote,
        scopes=scopes,
        subject=subject,
        allow_fragment=allow_fragment,
        max_age_s=max_age_s,
        audit_at=audit_at,
        include_text=include_text,
        consume=consume,
    )
    if (
        req.grant_id is None
        and req.text_sha is None
        and req.quote is None
        and not req.scopes
    ):
        raise VerifyUsageError(
            "pass grant_id, text_sha or quote, or at least one scope"
        )
    trusted = None
    try:
        trusted = _check_anchor(anchor, fs, req.uid)
        if req.quote is not None and len(_quote.normalize_text(req.quote)) < _MIN_QUOTE:
            raise _Deny(
                REASON_QUOTE_TOO_SHORT, "quotes need %d characters" % _MIN_QUOTE
            )
        candidates = _candidates(req, trusted.grants_dir)
        if not candidates:
            return _denied(
                req,
                req.now,
                _Deny(
                    REASON_NOT_FOUND, "no grant for this session matches the request"
                ),
                trusted,
            )
        budget = [MAX_SIGNATURE_CHECKS]
        first_deny = None
        for env in candidates:
            try:
                grant, match, consumed = _evaluate(env, req, trusted, budget)
            except _Deny as deny:
                if first_deny is None:
                    first_deny = deny
                if budget[0] <= 0:
                    break
                continue
            return _ok(req, trusted, grant, match, len(candidates), consumed)
        return _denied(req, req.now, first_deny, trusted, len(candidates))
    except _Deny as deny:
        return _denied(req, req.now, deny, trusted)
    except VerifyUsageError:
        raise
    except Exception as exc:  # fail closed: CLI exit 5
        return _internal(req, exc, trusted)


# -- candidate-first lookup (G-17) ---------------------------------------------------------


def _read_grant_file(path: str) -> _envelope.Envelope:
    return _envelope.read_envelope_file(path)


def _prefilter(req: _Request, payload: Mapping[str, Any]) -> bool:
    """Cheap, UNVERIFIED filter. It may only exclude; the full pipeline re-checks all."""
    targets = payload.get("targets")
    if not isinstance(targets, list) or not any(
        isinstance(t, dict) and t.get("session_id") == req.session for t in targets
    ):
        return False
    scope = payload.get("scope")
    if req.scopes and (
        not isinstance(scope, list) or not all(s in scope for s in req.scopes)
    ):
        return False
    match = _match_text(req, payload)
    # A refused fragment stays a candidate so the caller learns ``quote_fragment`` rather
    # than ``not_found``; step 10 still refuses it.
    return match["ok"] or match["kind"] == "fragment"


def _candidates(req: _Request, grants_dir: str) -> List[_envelope.Envelope]:
    try:
        names = os.listdir(grants_dir)
    except OSError:
        return []
    hinted = []
    for name in names:
        parsed = _envelope.parse_grant_filename(name)
        if parsed is None:
            continue
        issued_at, name_id = parsed
        if req.grant_id is not None:
            if name_id != req.grant_id:
                continue
        elif not req.at - LOOKUP_WINDOW_MS - SKEW_MS <= issued_at <= req.at + SKEW_MS:
            continue
        hinted.append((issued_at, name))
    hinted.sort(reverse=True)  # newest first by the name hint
    out = []
    seen = set()
    for _, name in hinted:
        try:
            env = _read_grant_file(posixpath.join(grants_dir, name))
            payload = _envelope.decode_payload(env.payload)
        except _envelope.EnvelopeError:
            continue
        if env.grant_id in seen:
            continue
        if req.grant_id is not None and env.grant_id != req.grant_id:
            continue  # the id is derived from the bytes, never taken from the name
        if req.grant_id is None and not _prefilter(req, payload):
            continue
        seen.add(env.grant_id)
        issued = payload.get("issued_at")
        out.append((issued if _is_int(issued) else -1, env))
    out.sort(key=lambda item: item[0], reverse=True)  # newest first by the payload
    return [env for _, env in out]
