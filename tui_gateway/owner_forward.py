"""Owner-forward: the owner's confirmed text delivered into other sessions as a real user turn.

Design: ``_ops/plans/HE-OWNER-FORWARD-DESIGN-2026-09-26.md`` §2, §3 and §5. The renderer composes; Electron
main shows a native confirm and signs a single-use Owner Gesture Grant with a per-spawn Ed25519 key whose
public half reaches this backend as ``HERMES_OWNER_FORWARD_PUBKEY``; ``owner.forward`` verifies the grant
here and :func:`deliver` hands each target the text through its normal ``prompt.submit``, stamped with an
:class:`OwnerForwardStamp`. The stamp is the only thing that makes a turn ``display_kind="owner_forward"``:
JSON cannot produce one (``prompt.submit`` answers 4125 to anything else in ``_owner_forward``), and its one
constructor call is in :func:`deliver`, after verification (static test P-12).

Grant wire format (per-spawn key, this module): ``grant`` is base64url (no padding) of the exact payload
bytes Electron signed and ``signature`` is base64url of ``Ed25519(SIGN_DOMAIN + payload)``. The payload is
never re-canonicalized: verification runs over the received bytes and only then parses them (the VERIFY
addendum's D-5, which avoids byte-identical JS/Python serializers). Payload (JSON object)::

    {"v":1, "aud":"hermes-owner-forward", "backend":<backend_id>, "nonce":<str>, "iat":<ms>, "exp":<ms>,
     "gesture":"menu"|"selection"|"slash_to"|"proposal", "confirm":"native_dialog"|"touch_id",
     "origin":{"session_id":<str>, "message_id":<str>|null}, "targets":[<profile>:<session id>...],
     "text_sha256":<hex sha256 of the UTF-8 text>, "text_len":<UTF-8 byte length of the text>}

``backend`` is ``ofk_`` + the first 16 hex chars of sha256(raw public key): the key is minted per backend
spawn, so its fingerprint names the spawn and a grant for another (or a restarted) backend fails. ``text_len``
is a UTF-8 BYTE count so JS (``Buffer.byteLength``) and Python agree; ``String.length`` would not for emoji.

VERIFIER SEAM (for U13, VERIFY addendum §1.2/§2): ``owner.forward`` only talks to a :class:`GrantVerifier`
(``verify(grant, signature, text) -> VerifyResult``). :class:`PerSpawnKeyVerifier` is today's implementation.
The v1 envelope (``hermes-owner-grant/v1``, ``{format,kid,payload,sig}``, keys from
``HERMES_OWNER_GRANT_KEYS`` or the root-owned anchor via ``hermes_owner_grant.verify_envelope``) plugs in as
another ``GrantVerifier`` returned by :func:`verifier_from_env`; it must return claims carrying the same keys
this module reads (``nonce``, ``exp``, ``origin``, ``targets``, ``gesture``, ``confirm``) and enforce
``deliver_by`` plus the spawn binding itself. Nothing below the verifier changes.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional, Protocol

logger = logging.getLogger(__name__)

PUBKEY_ENV = "HERMES_OWNER_FORWARD_PUBKEY"
AUDIENCE = "hermes-owner-forward"
GRANT_VERSION = 1
# Domain separation: a signature over these bytes can never be replayed as anything else the key signs
# (and never as a VERIFY-addendum ``hermes-owner-grant/v1`` envelope, whose prefix differs).
SIGN_DOMAIN = b"hermes-owner-forward/v1\x00"
MAX_GRANT_LIFETIME_MS = 60_000
CLOCK_SKEW_MS = 5_000
MAX_GRANT_BYTES = 16 * 1024
HARD_MAX_TARGETS = 5
HARD_MAX_CHARS = 32_000
GESTURES = frozenset({"menu", "selection", "slash_to", "proposal"})
CONFIRMS = frozenset({"native_dialog", "touch_id"})

# Error codes (the design's 4403 for callers that may never forward; 4125 lives in prompt.submit).
ERR_CALLER = 4403
ERR_DISABLED = 4126
ERR_GRANT = 4127
ERR_CONTENT = 4128
ERR_TARGET = 4129
ERR_RATE = 4131

_DEFAULTS: dict[str, Any] = {"enabled": True, "max_chars": 8000, "max_targets": 5, "per_minute": 20}

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def policy() -> dict[str, Any]:
    """Effective ``owner_forward:`` config block. Values are clamped to the design's hard ceilings: config can
    tighten the limits, never lift them past 5 targets or ``HARD_MAX_CHARS``."""
    out = dict(_DEFAULTS)
    raw: Any = None
    with contextlib.suppress(Exception):
        from tui_gateway import server
        raw = (server._load_cfg() or {}).get("owner_forward")
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        value = raw["enabled"]
        out["enabled"] = value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)
    for key, ceiling in (("max_chars", HARD_MAX_CHARS), ("max_targets", HARD_MAX_TARGETS), ("per_minute", 600)):
        with contextlib.suppress(TypeError, ValueError):
            out[key] = min(ceiling, max(1, int(raw[key]))) if key in raw else out[key]
    return out


# ── verification ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""
    claims: Optional[Mapping[str, Any]] = None


class GrantVerifier(Protocol):
    """The seam ``owner.forward`` depends on. ``verify`` checks signature, audience, version, backend binding,
    time window and the text binding; the caller owns nonce, content, target and rate checks."""

    backend_id: str

    def verify(self, grant: Any, signature: Any, text: Any) -> VerifyResult: ...


def _b64u_decode(value: str, max_bytes: int) -> bytes | None:
    """Strict unpadded base64url: one encoding per byte string, so a grant has no malleable spellings."""
    if not isinstance(value, str) or len(value) > (max_bytes * 4) // 3 + 4 or not _B64U_RE.match(value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    return raw if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() == value else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_claims(claims: Any, *, backend_id: str, text: str, now_ms: int) -> VerifyResult:
    """Claim checks shared by every verifier (§3.2 check 3 minus the nonce, plus the text binding of check 4)."""
    def fail(reason: str) -> VerifyResult:
        return VerifyResult(False, reason)

    if not isinstance(claims, dict):
        return fail("malformed")
    if not _is_int(claims.get("v")) or claims["v"] != GRANT_VERSION:
        return fail("version")
    if claims.get("aud") != AUDIENCE:
        return fail("audience")
    if not backend_id or claims.get("backend") != backend_id:
        return fail("backend")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
        return fail("nonce")
    iat, exp = claims.get("iat"), claims.get("exp")
    if not (_is_int(iat) and _is_int(exp)) or exp <= iat or exp - iat > MAX_GRANT_LIFETIME_MS:
        return fail("lifetime")
    if now_ms < iat - CLOCK_SKEW_MS or now_ms > exp + CLOCK_SKEW_MS:
        return fail("expired")
    if claims.get("gesture") not in GESTURES:
        return fail("gesture")
    if claims.get("confirm") not in CONFIRMS:
        return fail("confirm")
    origin = claims.get("origin")
    if (not isinstance(origin, dict) or not isinstance(origin.get("session_id"), str) or not origin["session_id"]
            or not (origin.get("message_id") is None or isinstance(origin.get("message_id"), str))):
        return fail("origin")
    targets = claims.get("targets")
    if (not isinstance(targets, list) or not targets or len(targets) > HARD_MAX_TARGETS
            or not all(isinstance(t, str) and t for t in targets) or len(set(targets)) != len(targets)):
        return fail("targets")
    if not isinstance(text, str):
        return fail("text")
    encoded = text.encode("utf-8", "surrogatepass")
    if claims.get("text_sha256") != hashlib.sha256(encoded).hexdigest():
        return fail("text_hash")
    if not _is_int(claims.get("text_len")) or claims["text_len"] != len(encoded):
        return fail("text_len")
    return VerifyResult(True, claims=claims)


class PerSpawnKeyVerifier:
    """Ed25519 against the per-spawn public key Electron main passed in (base design §2 step 5)."""

    def __init__(self, public_key: bytes, *, clock=time.time) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
            raise ValueError("an Ed25519 public key is 32 raw bytes")
        self._key = Ed25519PublicKey.from_public_bytes(bytes(public_key))
        self.backend_id = "ofk_" + hashlib.sha256(bytes(public_key)).hexdigest()[:16]
        self._clock = clock

    def verify(self, grant: Any, signature: Any, text: Any) -> VerifyResult:
        from cryptography.exceptions import InvalidSignature

        payload = _b64u_decode(grant, MAX_GRANT_BYTES) if isinstance(grant, str) else None
        if not payload or len(payload) > MAX_GRANT_BYTES:
            return VerifyResult(False, "malformed")
        sig = _b64u_decode(signature, 64) if isinstance(signature, str) else None
        if sig is None or len(sig) != 64:
            return VerifyResult(False, "signature")
        try:
            self._key.verify(sig, SIGN_DOMAIN + payload)
        except InvalidSignature:
            return VerifyResult(False, "signature")
        try:  # parsed only after the signature holds
            claims = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return VerifyResult(False, "malformed")
        return check_claims(claims, backend_id=self.backend_id, text=text, now_ms=int(self._clock() * 1000))


def verifier_from_env(environ: Mapping[str, str] | None = None) -> Optional[GrantVerifier]:
    """The backend's verifier, or None (owner.forward disabled, fail closed) when the key is missing or bad.

    U13 plugs in here: prefer ``HERMES_OWNER_GRANT_KEYS`` / the anchored v1 verifier when present."""
    env = os.environ if environ is None else environ
    raw = str(env.get(PUBKEY_ENV) or "").strip()
    if not raw:
        return None
    normalized = raw.replace("+", "-").replace("/", "_").rstrip("=")
    key = _b64u_decode(normalized, 32)
    if key is None or len(key) != 32:
        logger.warning("%s is not a base64 Ed25519 public key; owner.forward stays disabled", PUBKEY_ENV)
        return None
    try:
        return PerSpawnKeyVerifier(key)
    except Exception:  # noqa: BLE001 - missing cryptography or a bad point: disabled, never open
        logger.warning("owner.forward verifier unavailable; the method stays disabled", exc_info=True)
        return None


_verifier: Optional[GrantVerifier] = None


# ── single-use and rate state (in memory: the key is per spawn, so a restart voids every grant) ─


class _NonceCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}

    def claim(self, nonce: str, exp_ms: int, now_ms: int) -> bool:
        with self._lock:
            for old in [n for n, until in self._seen.items() if until < now_ms]:
                del self._seen[old]
            if nonce in self._seen:
                return False
            # Kept until the grant could no longer pass the time check anyway.
            self._seen[nonce] = exp_ms + CLOCK_SKEW_MS
            return True


class _RateWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accepted: deque[float] = deque()

    def allow(self, per_minute: int, now: float) -> bool:
        with self._lock:
            while self._accepted and now - self._accepted[0] >= 60.0:
                self._accepted.popleft()
            if len(self._accepted) >= per_minute:
                return False
            self._accepted.append(now)
            return True


class OwnerResumeGate:
    """One in-flight owner-forward resume per target. Deliberately not the peer mailbox's ``_gate``: its
    interval and concurrency caps are cost guards against agents, and this resume is the owner's own ask."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, list] = {}

    @contextlib.contextmanager
    def hold(self, target: str) -> Iterator[None]:
        with self._lock:
            entry = self._held.setdefault(target, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()
        try:
            yield
        finally:
            entry[0].release()
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self._held.pop(target, None)


_NONCES = _NonceCache()
_RATE = _RateWindow()
_RESUME_GATE = OwnerResumeGate()


def _reset_for_tests() -> None:
    global _NONCES, _RATE, _RESUME_GATE
    _NONCES, _RATE, _RESUME_GATE = _NonceCache(), _RateWindow(), OwnerResumeGate()


# ── the stamp ───────────────────────────────────────────────────────────────────────────────

class OwnerForwardStamp:
    """In-process proof that ``owner.forward`` verified a grant for this turn. Immutable, uncopyable across
    processes, and minted only by :func:`deliver` (P-12). ``prompt.submit`` reads ``display_metadata()`` and
    ignores any caller-supplied metadata when a stamp is present."""

    __slots__ = ("origin_session_id", "origin_title", "origin_message_id", "gesture", "confirm", "nonce",
                 "forwarded_at", "fanout_index", "fanout_total", "target_session_id", "target_session_key",
                 "target_profile",
                 "text_sha256", "one_time_id", "_consumed", "_consume_lock")
    __mint = object()

    def __init__(self, mint: object, /, *, origin_session_id: str, origin_title: str,
                 origin_message_id: Optional[str], gesture: str, confirm: str, nonce: str, forwarded_at: float,
                 fanout_index: int, fanout_total: int, target_session_id: str, target_session_key: str,
                 target_profile: str,
                 text_sha256: str, one_time_id: str) -> None:
        if mint is not OwnerForwardStamp.__mint:
            raise TypeError("an OwnerForwardStamp is minted by tui_gateway.owner_forward.deliver() only")
        for name, value in (
                ("origin_session_id", origin_session_id), ("origin_title", origin_title),
                ("origin_message_id", origin_message_id), ("gesture", gesture), ("confirm", confirm),
                ("nonce", nonce), ("forwarded_at", forwarded_at), ("fanout_index", fanout_index),
                ("fanout_total", fanout_total), ("target_session_id", target_session_id),
                ("target_session_key", target_session_key),
                ("target_profile", target_profile), ("text_sha256", text_sha256), ("one_time_id", one_time_id),
                ("_consumed", False), ("_consume_lock", threading.Lock())):
            object.__setattr__(self, name, value)

    def consume(self, text: str, session: dict) -> bool:
        """Atomically validate the signed text and destination, then spend this stamp."""
        from hermes_constants import profile_name_for_home
        from tui_gateway import server

        profile = profile_name_for_home(session.get("profile_home")) or server._current_profile_name()
        if (hashlib.sha256(text.encode("utf-8")).hexdigest() != self.text_sha256
                or str(session.get("session_key") or "") != self.target_session_key
                or profile != self.target_profile):
            return False
        with self._consume_lock:
            if self._consumed:
                return False
            object.__setattr__(self, "_consumed", True)
            return True

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("OwnerForwardStamp is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("OwnerForwardStamp is immutable")

    def __copy__(self) -> "OwnerForwardStamp":
        return self

    def __deepcopy__(self, memo: dict) -> "OwnerForwardStamp":
        return self

    def __reduce__(self):
        raise TypeError("an OwnerForwardStamp never leaves the process")

    def display_metadata(self) -> dict[str, Any]:
        return {
            "kind": "owner_forward", "from_session_id": self.origin_session_id, "from_title": self.origin_title,
            "from_message_id": self.origin_message_id, "gesture": self.gesture, "confirm": self.confirm,
            "grant_nonce": self.nonce, "forwarded_at": self.forwarded_at,
            "fanout": [self.fanout_index, self.fanout_total],
        }

    def __repr__(self) -> str:
        return (f"OwnerForwardStamp(from={self.origin_session_id!r}, nonce={self.nonce!r}, "
                f"fanout={self.fanout_index}/{self.fanout_total})")


# ── delivery (§5.1) ─────────────────────────────────────────────────────────────────────────


def _submit_stamped(sid: str, text: str, stamp: OwnerForwardStamp) -> dict:
    """The target's normal ``prompt.submit`` as a queued user turn, with no bound transport (the target's own
    window keeps its stream) — never ``send_peer_message``, never the peer envelope."""
    from tui_gateway import server
    from tui_gateway.transport import bind_transport, reset_transport

    token = bind_transport(None)
    try:
        return server._methods["prompt.submit"](
            f"owner-forward:{sid}", {"session_id": sid, "text": text, "queued": True, "_owner_forward": stamp})
    finally:
        reset_transport(token)


def _status_from_submit(response: Any, woke: bool) -> tuple[str, Optional[str]]:
    if not isinstance(response, dict):
        return "failed:submit", "prompt.submit returned no response"
    if error := response.get("error"):
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message") if isinstance(error, dict) else error)
        if code == 4090:
            return "failed:slot_limit", message
        if code in (5070, 5071, 5072):
            return "failed:storage", message
        return "failed:submit", message
    status = str((response.get("result") or {}).get("status") or "")
    if status == "queued":
        return "queued", None
    return ("resumed-and-delivered" if woke else "delivered"), None


def _deliver_one(tip: str, text: str, stamp: OwnerForwardStamp, profile_home: Optional[str]) -> tuple[str, Optional[str]]:
    from tui_gateway import server
    from tui_gateway import session_mailbox as mb

    if (live := mb._find_live(tip, profile_home)) is not None:
        return _status_from_submit(_submit_stamped(live[0], text, stamp), woke=False)
    with _RESUME_GATE.hold(tip):
        if (live := mb._find_live(tip, profile_home)) is not None:  # a concurrent forward woke it
            return _status_from_submit(_submit_stamped(live[0], text, stamp), woke=False)
        sid, error = mb._resume(tip, profile_home)
        if not sid:
            return "failed:resume", error or "resume failed"
        with server._sessions_lock:
            if (session := server._sessions.get(sid)) is not None and not session.get("pinned_resident"):
                # Not ``_peer_mailbox_woken``: an owner wake must not consume the peer mailbox's slots.
                session["_owner_forward_woken"] = True
        return _status_from_submit(_submit_stamped(sid, text, stamp), woke=True)


def deliver(claims: Mapping[str, Any], text: str,
            targets: list[tuple[str, str, Optional[str], str, str]], *, origin_title: str = "",
            profile_home: Optional[str] = None) -> list[dict[str, Any]]:
    """Fan a verified grant's text out to ``targets`` (``(requested id, compression tip)``), sequentially and
    independently. The ONLY place an :class:`OwnerForwardStamp` is constructed (P-12)."""
    origin = claims["origin"]
    forwarded_at = time.time()
    results = []
    for index, (target_id, tip, target_home, target_profile, stored_target_id) in enumerate(targets):
        stamp = OwnerForwardStamp(
            OwnerForwardStamp._OwnerForwardStamp__mint,
            origin_session_id=str(origin["session_id"]), origin_title=origin_title,
            origin_message_id=origin.get("message_id"), gesture=str(claims["gesture"]),
            confirm=str(claims["confirm"]), nonce=str(claims["nonce"]), forwarded_at=forwarded_at,
            fanout_index=index, fanout_total=len(targets), target_session_id=stored_target_id,
            target_session_key=tip, target_profile=target_profile,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            one_time_id=base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii"))
        try:
            status, detail = _deliver_one(tip, text, stamp, target_home)
        except Exception as exc:  # noqa: BLE001 - one target's failure never aborts the others
            logger.warning("owner.forward delivery to %s failed", tip, exc_info=True)
            status, detail = "failed:submit", str(exc) or type(exc).__name__
        results.append({"target_session_id": target_id, "status": status, "detail": detail})
    return results


# ── owner.forward (§3.2: every check in order, every one fails closed) ──────────────────────


def _content_refusal(text: str, pol: Mapping[str, Any]) -> Optional[str]:
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    if not text.strip():
        return "the text is empty"
    if text.lstrip().startswith("/"):
        return "a forward cannot start with '/': open the target and type commands there"
    if len(text) > int(pol["max_chars"]):
        return f"the text exceeds {pol['max_chars']} characters"
    if sanitize_user_prompt_text(text) != text:
        # prompt.submit would deliver different bytes than the owner confirmed.
        return "the text contains characters the composer strips; forward it again from the sheet"
    return None


def _resolve_targets(requested: Any, claims: Mapping[str, Any], pol: Mapping[str, Any],
                     profile_home: Optional[str]) -> tuple[list[tuple[str, str, Optional[str], str, str]], str, Optional[str]]:
    """``([(display id, tip, profile home, profile name, stored id)], origin_title, refusal)``."""
    from tui_gateway import session_mailbox as mb

    if (not isinstance(requested, list) or not requested
            or not all(isinstance(t, str) and t for t in requested) or len(set(requested)) != len(requested)):
        return [], "", "targets must be a non-empty list of distinct session ids"
    if len(requested) > int(pol["max_targets"]):
        return [], "", f"at most {pol['max_targets']} targets per forward"
    if set(requested) != set(claims["targets"]) or len(requested) != len(claims["targets"]):
        return [], "", "the targets differ from the ones the owner confirmed"
    origin_id = str(claims["origin"]["session_id"])
    from tui_gateway import server

    resolved: list[tuple[str, str, Optional[str], str, str]] = []
    with mb._open_db(profile_home) as db:
        if db is None:
            return [], "", "session store is unavailable"
        origin_row = None
        with contextlib.suppress(Exception):
            origin_row = db.get_session(origin_id)
        origin_title = str((origin_row or {}).get("title") or "")
        origin_tip = mb._tip(db, origin_id)
        tips: set[str] = set()
        for target in requested:
            if ":" not in target:
                return [], "", "targets must use <profile>:<session_id>"
            target_profile, target_id = target.split(":", 1)
            if not target_profile or not target_id:
                return [], "", "targets must use <profile>:<session_id>"
            if target_profile == server._current_profile_name():
                target_home = None
            else:
                try:
                    target_home = server._profile_home(target_profile)
                except server.ProfileUnavailableError:
                    return [], "", f"profile {target_profile!r} is not served by this backend"
            target_profile_home = str(target_home) if target_home is not None else None
            with mb._open_db(target_profile_home) as target_db:
                row = None
                with contextlib.suppress(Exception):
                    row = target_db.get_session(target_id) if target_db is not None else None
                if not row:
                    return [], "", f"no stored session {target_id!r} in profile {target_profile!r}"
                if str(row.get("source") or "") not in mb._ADDRESSABLE_SOURCES:
                    return [], "", f"session {target_id!r} cannot receive forwards"
                tip = mb._tip(target_db, str(row["id"]))
            if ((target_profile == server._current_profile_name() and target_id == origin_id)
                    or (target_profile == server._current_profile_name() and tip in {origin_id, origin_tip})):
                return [], "", "a forward cannot target its own origin session"
            if tip in tips:
                return [], "", "two targets resolve to the same session"
            tips.add(tip)
            result_id = target_id if target_profile == server._current_profile_name() else target
            resolved.append((result_id, tip, target_profile_home, target_profile, target_id))
    return resolved, origin_title, None


def forward_rpc(rid: Any, params: dict) -> dict:
    from tui_gateway import server
    from tui_gateway.transport import current_transport

    # 1. Only a connected client. No transport = the mailbox, the resume path, a relay: in-process code.
    transport = current_transport()
    if transport is None:
        return server._err(rid, ERR_CALLER, "owner.forward is refused to in-process callers")
    # 2. Never a scoped session-spawn connection (ws.py already refuses the method there; defense in depth).
    if getattr(transport, "session_spawn_capability", None) is not None or "_session_spawn_capability" in params:
        return server._err(rid, ERR_CALLER, "owner.forward is refused on a session-spawn connection")
    # 3. The grant: configured key, signature, claims, time; then the nonce, burned before delivery starts.
    pol = policy()
    verifier = _verifier
    if verifier is None or not pol["enabled"]:
        return server._err(rid, ERR_DISABLED, "owner forward is disabled: this backend has no owner key")
    text = params.get("text")
    try:
        result = verifier.verify(params.get("grant"), params.get("signature"), text)
    except Exception:  # noqa: BLE001 - a verifier crash is a refusal
        logger.warning("owner.forward verifier raised", exc_info=True)
        result = VerifyResult(False, "verifier_error")
    if not result.ok or result.claims is None:
        return server._err(rid, ERR_GRANT, f"owner grant refused ({result.reason or 'invalid'}); send again")
    claims = result.claims
    if not _NONCES.claim(str(claims["nonce"]), int(claims["exp"]), int(time.time() * 1000)):
        return server._err(rid, ERR_GRANT, "owner grant refused (nonce already used); send again")
    # 4. Content.
    if (refusal := _content_refusal(text, pol)) is not None:
        return server._err(rid, ERR_CONTENT, refusal)
    # 5. Targets are bound to a served profile as well as a stored session id.
    targets, origin_title, refusal = _resolve_targets(params.get("targets"), claims, pol, None)
    if refusal is not None:
        return server._err(rid, ERR_TARGET, refusal)
    # 6. Rate.
    if not _RATE.allow(int(pol["per_minute"]), time.monotonic()):
        return server._err(rid, ERR_RATE, "too many forwards this minute; wait and send again")
    results = deliver(claims, text, targets, origin_title=origin_title)
    return server._ok(rid, {"nonce": str(claims["nonce"]), "results": results})


def register(server) -> None:
    """Install ``owner.forward`` (a pool handler: a cold target resumes) and read the key once, at startup.
    Not ``bind_module``: this module's state (verifier, nonces, the stamp class) stays here, not on server."""
    global _verifier
    server.register_method("owner.forward", forward_rpc)
    server._LONG_HANDLERS = server._LONG_HANDLERS | {"owner.forward"}
    _verifier = verifier_from_env()
