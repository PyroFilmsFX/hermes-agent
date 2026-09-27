"""Owner-forward: the owner's confirmed text delivered into other sessions as a real user turn.

Design: ``_ops/plans/HE-OWNER-FORWARD-DESIGN-2026-09-26.md`` §2, §3 and §5. The renderer composes; Electron
main shows a native confirm and signs a single-use Owner Grant. ``owner.forward`` verifies the canonical v1
envelope against the public key set and backend spawn id passed at process start, then :func:`deliver` hands
each target the text through its normal ``prompt.submit``, stamped with an
:class:`OwnerForwardStamp`. The stamp is the only thing that makes a turn ``display_kind="owner_forward"``:
JSON cannot produce one (``prompt.submit`` answers 4125 to anything else in ``_owner_forward``), and its one
constructor call is in :func:`deliver`, after verification (static test P-12).

Grant wire format is the canonical ``hermes-owner-grant/v1`` envelope. The exact signed payload bytes
are verified before parsing; the payload's ``forward_targets`` binds profile-qualified delivery while
``targets[].session_id`` remains the bare Hermes session id used by hooks.

Trust root (review P0/P1, 2026-09-27): the ONLY source of trusted keys is the root-owned anchor
(``hermes_owner_grant.anchor.load_trusted_anchor``: hard-coded path, StrictModes checks), re-read on
every call. An envelope verifies only under the anchor's ACTIVE key for this uid; no anchor means
owner.forward is disabled. Nothing an agent can write (``os.environ``, ``$HERMES_HOME/.env``, files
under ``~/.hermes``) can add a key. ``HERMES_OWNER_GRANT_KEYS`` is a public hint Electron still passes;
this module never reads it. The backend binding ``HERMES_OWNER_GRANT_BACKEND`` is the value the parent
passed at spawn, as captured before any dotenv load (``hermes_cli.env_loader.spawn_env_at_start``).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional, Protocol

logger = logging.getLogger(__name__)

KEYS_ENV = "HERMES_OWNER_GRANT_KEYS"
BACKEND_ENV = "HERMES_OWNER_GRANT_BACKEND"
AUDIENCE = "hermes-owner-forward"
GRANT_VERSION = 1
MAX_GRANT_LIFETIME_MS = 60_000
CLOCK_SKEW_MS = 5_000
HARD_MAX_TARGETS = 5
HARD_MAX_CHARS = 32_000
GESTURES = frozenset({"menu", "selection", "slash_to", "proposal", "composer_signed"})
CONFIRMS = frozenset({"native_dialog", "touch_id"})

# Error codes (the design's 4403 for callers that may never forward; 4125 lives in prompt.submit).
ERR_CALLER = 4403
ERR_DISABLED = 4126
ERR_GRANT = 4127
ERR_CONTENT = 4128
ERR_TARGET = 4129
ERR_RATE = 4131

_DEFAULTS: dict[str, Any] = {"enabled": True, "max_chars": 8000, "max_targets": 5, "per_minute": 20}

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
    """The seam ``owner.forward`` depends on. ``load_anchor`` returns the trusted root-owned anchor or raises
    :class:`AnchorUnavailable`; ``verify`` checks the anchor key status, signature, audience, version,
    backend binding, time window and the text binding; the caller owns nonce, content, target and rate."""

    backend_id: str

    def load_anchor(self) -> Any: ...

    def verify(self, envelope: Any, text: Any, anchor: Any = None) -> VerifyResult: ...


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
    if not isinstance(claims.get("aud"), list) or AUDIENCE not in claims["aud"]:
        return fail("audience")
    if not backend_id or claims.get("backend") != backend_id:
        return fail("backend")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
        return fail("nonce")
    issued_at, deliver_by = claims.get("issued_at"), claims.get("deliver_by")
    if (not _is_int(issued_at) or not _is_int(deliver_by) or deliver_by <= issued_at
            or deliver_by - issued_at > MAX_GRANT_LIFETIME_MS):
        return fail("lifetime")
    if now_ms < issued_at - CLOCK_SKEW_MS or now_ms > deliver_by + CLOCK_SKEW_MS:
        return fail("expired")
    if claims.get("gesture") not in GESTURES:
        return fail("gesture")
    if claims.get("confirm") not in CONFIRMS:
        return fail("confirm")
    source = claims.get("source_session")
    if (not isinstance(source, dict) or not isinstance(source.get("session_id"), str)
            or not source["session_id"]):
        return fail("origin")
    targets = claims.get("targets")
    if (not isinstance(targets, list) or not targets or len(targets) > HARD_MAX_TARGETS
            or not all(isinstance(t, dict) and isinstance(t.get("session_id"), str) and t["session_id"]
                       for t in targets)
            or len({t["session_id"] for t in targets}) != len(targets)):
        return fail("targets")
    forward_targets = claims.get("forward_targets")
    if (not isinstance(forward_targets, list) or not forward_targets
            or not all(isinstance(t, str) and t for t in forward_targets)
            or len(set(forward_targets)) != len(forward_targets)
            or not {t.split(":", 1)[-1] for t in forward_targets} <= {t["session_id"] for t in targets}):
        return fail("targets")
    if not _is_int(claims.get("owner_uid")) or claims["owner_uid"] != os.getuid():
        return fail("owner_uid")
    if not isinstance(text, str):
        return fail("text")
    try:
        encoded = text.encode("utf-8")  # strict, like the hook verifier: a lone surrogate has no bytes
    except UnicodeEncodeError:
        return fail("text")
    if claims.get("text_sha256") != hashlib.sha256(encoded).hexdigest():
        return fail("text_hash")
    if not _is_int(claims.get("text_len")) or claims["text_len"] != len(encoded):
        return fail("text_len")
    return VerifyResult(True, claims=claims)


ANCHOR_REQUIRED_MESSAGE = "owner-forward needs the anchor: run the one-time enable"


def _load_anchor():
    """The root-owned anchor, freshly loaded and trust-checked (raises ``AnchorError``). Module-level
    so tests can route it through an injected fake filesystem; production never passes a path."""
    from hermes_owner_grant import anchor as owner_anchor

    return owner_anchor.load_trusted_anchor()


class AnchorUnavailable(Exception):
    """No trusted anchor for this uid: owner.forward is disabled until the one-time enable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EnvelopeGrantVerifier:
    """Verify canonical v1 envelopes against the root-owned anchor's ACTIVE key (strict pure Ed25519).

    The anchor is loaded per call (it is one small file): a later one-time enable works without a
    restart, and a revoked or retired key stops verifying at once."""

    def __init__(self, *, backend_id: str, anchor_loader=None, clock=time.time) -> None:
        self.backend_id = backend_id
        self._anchor_loader = anchor_loader
        self._clock = clock

    def load_anchor(self):
        """The trusted anchor for this uid, or raise :class:`AnchorUnavailable`."""
        from hermes_owner_grant.anchor import AnchorError

        try:
            trusted = (self._anchor_loader or _load_anchor)()
        except AnchorError as exc:
            raise AnchorUnavailable(exc.reason) from None
        except Exception:  # noqa: BLE001 - an anchor we can't read is no anchor
            logger.warning("owner-grant anchor could not be loaded", exc_info=True)
            raise AnchorUnavailable("anchor_untrusted") from None
        if not _is_int(getattr(trusted, "owner_uid", None)) or trusted.owner_uid != os.getuid():
            raise AnchorUnavailable("anchor_owner_uid")
        return trusted

    def verify(self, envelope: Any, text: Any, anchor: Any = None) -> VerifyResult:
        from hermes_owner_grant import ed25519_pure, envelope as owner_envelope, verify as owner_verify
        from hermes_owner_grant.anchor import STATUS_ACTIVE, check_key

        if anchor is None:
            try:
                anchor = self.load_anchor()
            except AnchorUnavailable as exc:
                return VerifyResult(False, exc.reason)
        try:
            env = owner_envelope.parse_envelope(envelope)
        except (owner_envelope.EnvelopeError, TypeError, ValueError):
            return VerifyResult(False, "malformed")
        key = anchor.key(env.kid)
        if key is None:
            return VerifyResult(False, "unknown_kid")
        if key.status != STATUS_ACTIVE:
            # A forward is delivered live (60 s window), never backdated: only the active key forwards.
            return VerifyResult(False, "key_" + key.status)
        if not ed25519_pure.verify(key.pub, env.sign_bytes(), env.sig):
            return VerifyResult(False, "signature")
        try:
            claims = owner_envelope.decode_payload(env.payload)
            owner_verify.validate_payload(claims)
        except (owner_envelope.EnvelopeError, ValueError, TypeError):
            return VerifyResult(False, "malformed")
        now_ms = int(self._clock() * 1000)
        issued_at = claims.get("issued_at")
        if not _is_int(issued_at):
            return VerifyResult(False, "lifetime")
        if (key_refusal := check_key(anchor, env.kid, issued_at=issued_at, now=now_ms)) is not None:
            return VerifyResult(False, key_refusal)
        result = check_claims(claims, backend_id=self.backend_id, text=text, now_ms=now_ms)
        return VerifyResult(result.ok, result.reason, {**claims, "_grant_envelope": env.to_dict(),
                                                       "_grant_id": env.grant_id}) if result.ok else result


def verifier_at_startup(start_env: Mapping[str, Optional[str]] | None = None) -> Optional[GrantVerifier]:
    """The backend's verifier, or None (owner.forward disabled, fail closed) when the parent passed no
    backend binding at spawn.

    The binding comes from ``start_env`` (default: the values captured before any dotenv load), never
    from the live ``os.environ``: ``$HERMES_HOME/.env`` is agent-writable. Keys are not read here at
    all; they come from the anchor on every call."""
    if start_env is None:
        from hermes_cli.env_loader import spawn_env_at_start

        backend_id = str(spawn_env_at_start(BACKEND_ENV) or "").strip()
    else:
        backend_id = str(start_env.get(BACKEND_ENV) or "").strip()
    if not backend_id or len(backend_id) > 128 or not backend_id.isprintable():
        return None
    return EnvelopeGrantVerifier(backend_id=backend_id)


_verifier: Optional[GrantVerifier] = None


# ── single-use and rate state (in memory: every grant names this backend's per-spawn binding, so a
#    restart, which mints a new binding, voids every outstanding grant) ─────────────────────────


class _NonceCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}

    def claim(self, nonce: str, deliver_by_ms: int, now_ms: int) -> bool:
        with self._lock:
            for old in [n for n, until in self._seen.items() if until < now_ms]:
                del self._seen[old]
            if nonce in self._seen:
                return False
            # Kept until the grant could no longer pass the time check anyway.
            self._seen[nonce] = deliver_by_ms + CLOCK_SKEW_MS
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
                 "target_profile", "text_sha256", "one_time_id", "grant_id", "grant_envelope", "decision_id",
                 "_consumed", "_consume_lock")
    __mint = object()

    def __init__(self, mint: object, /, *, origin_session_id: str, origin_title: str,
                 origin_message_id: Optional[str], gesture: str, confirm: str, nonce: str, forwarded_at: float,
                 fanout_index: int, fanout_total: int, target_session_id: str, target_session_key: str,
                 target_profile: str,
                 text_sha256: str, one_time_id: str, grant_id: str, grant_envelope: Mapping[str, str],
                 decision_id: str) -> None:
        if mint is not OwnerForwardStamp.__mint:
            raise TypeError("an OwnerForwardStamp is minted by tui_gateway.owner_forward.deliver() only")
        for name, value in (
                ("origin_session_id", origin_session_id), ("origin_title", origin_title),
                ("origin_message_id", origin_message_id), ("gesture", gesture), ("confirm", confirm),
                ("nonce", nonce), ("forwarded_at", forwarded_at), ("fanout_index", fanout_index),
                ("fanout_total", fanout_total), ("target_session_id", target_session_id),
                ("target_session_key", target_session_key),
                ("target_profile", target_profile), ("text_sha256", text_sha256), ("one_time_id", one_time_id),
                ("grant_id", grant_id), ("grant_envelope", dict(grant_envelope)), ("decision_id", decision_id),
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
            "owner_grant": {"id": self.grant_id, "envelope": dict(self.grant_envelope)},
            "decision_id": self.decision_id,
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
    origin = claims["source_session"]
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
            one_time_id=base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii"),
            grant_id=str(claims["_grant_id"]), grant_envelope=claims["_grant_envelope"],
            decision_id=str(claims["decision_id"]))
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
    if set(requested) != set(claims["forward_targets"]) or len(requested) != len(claims["forward_targets"]):
        return [], "", "the targets differ from the ones the owner confirmed"
    target_session_ids = {t["session_id"] for t in claims["targets"]}
    origin_id = str(claims["source_session"]["session_id"])
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
            if target_id not in target_session_ids:
                return [], "", "the targets differ from the sessions the owner confirmed"
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
                if claims.get("gesture") != "composer_signed" or len(requested) != 1:
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
    # 3. The grant: the root-owned anchor (re-read now), its active key, signature, claims, time; then
    #    the nonce, burned before delivery starts.
    pol = policy()
    verifier = _verifier
    if verifier is None or not pol["enabled"]:
        return server._err(rid, ERR_DISABLED, "owner forward is disabled: this backend has no owner-grant binding")
    try:
        trusted_anchor = verifier.load_anchor()
    except AnchorUnavailable as exc:
        return server._err(rid, ERR_DISABLED, f"{ANCHOR_REQUIRED_MESSAGE} ({exc.reason})")
    except Exception:  # noqa: BLE001 - fail closed
        logger.warning("owner.forward anchor check raised", exc_info=True)
        return server._err(rid, ERR_DISABLED, f"{ANCHOR_REQUIRED_MESSAGE} (anchor_error)")
    text = params.get("text")
    try:
        result = verifier.verify(params.get("envelope"), text, anchor=trusted_anchor)
    except Exception:  # noqa: BLE001 - a verifier crash is a refusal
        logger.warning("owner.forward verifier raised", exc_info=True)
        result = VerifyResult(False, "verifier_error")
    if not result.ok or result.claims is None:
        return server._err(rid, ERR_GRANT, f"owner grant refused ({result.reason or 'invalid'}); send again")
    claims = result.claims
    if not _NONCES.claim(str(claims["nonce"]), int(claims["deliver_by"]), int(time.time() * 1000)):
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
    """Install ``owner.forward`` (a pool handler: a cold target resumes) and bind the verifier to the
    backend id the parent passed at spawn. Keys are never read here: every call re-reads the anchor.
    Not ``bind_module``: this module's state (verifier, nonces, the stamp class) stays here, not on server."""
    global _verifier
    server.register_method("owner.forward", forward_rpc)
    server._LONG_HANDLERS = server._LONG_HANDLERS | {"owner.forward"}
    _verifier = verifier_at_startup()
