"""Secret-hygiene JSON-RPC surface (HE-SECRET-HYGIENE S2): ``secrets.mask`` plus the submit helpers.

- ``secrets.mask`` masks composer text as ``[REDACTED:<kind>:<tag>]`` (the paste edge, E1) and
  returns per-kind counts and tags, never a value. With ``optout_tags`` (sent only after the
  client's explicit confirm modal) it mints a single-use nonce bound to this session, the exact
  text (sha256) and the tag set.
- ``_consume_secret_optout`` is how ``prompt.submit`` spends that nonce: a missing, unknown,
  expired, reused or mismatched nonce yields no opt-out, so the message stays fully masked.
- ``_mask_submit_text`` is the E2 edge shared by ``prompt.submit`` / ``prompt.btw`` /
  ``prompt.background``.

Handlers run against server.py's globals, so every stdlib import is function-local. The nonce
table lives in process memory only (never persisted), and opt-out can be disabled
entirely with ``security.secret_hygiene.optout.allowed: false``.

Handlers are rebound onto server.py's globals at install time (see method_ctx.py).
"""

from __future__ import annotations

import threading

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_SECRET_OPTOUT_TTL_S = 300.0
_SECRET_OPTOUT_MAX = 256
_secret_optout_lock = threading.Lock()
# nonce -> (session_id, sha256(text), frozenset(tags), expires_at_monotonic)
_secret_optout_nonces: dict = {}


def _secret_text_digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _mask_submit_text(text, optout_tags) -> tuple:
    """``(masked_text, display_metadata_fragment)``; the fragment carries kind counts only."""
    from agent.secret_hygiene import ingress_kind_counts, mask_ingress_text

    if not isinstance(text, str) or not text:
        return text, {}
    masked, findings = mask_ingress_text(text, optout_tags=optout_tags)
    if not findings:
        return masked, {}
    return masked, {"secret_mask": {"v": 1, "kinds": ingress_kind_counts(findings)}}


def _optout_kinds(full_meta: dict, kept_meta: dict) -> dict:
    """Per-kind count of secrets the opt-out left raw (full mask minus what stayed masked)."""
    full = ((full_meta or {}).get("secret_mask") or {}).get("kinds") or {}
    kept = ((kept_meta or {}).get("secret_mask") or {}).get("kinds") or {}
    return {k: n - kept.get(k, 0) for k, n in full.items() if n - kept.get(k, 0) > 0}


def _mint_secret_optout_nonce(sid: str, text: str, tags: frozenset) -> str:
    import secrets as _secrets
    import time
    nonce = _secrets.token_urlsafe(24)
    now = time.monotonic()
    with _secret_optout_lock:
        for key in [k for k, v in _secret_optout_nonces.items() if v[3] <= now]:
            _secret_optout_nonces.pop(key, None)
        while len(_secret_optout_nonces) >= _SECRET_OPTOUT_MAX:
            _secret_optout_nonces.pop(next(iter(_secret_optout_nonces)))
        _secret_optout_nonces[nonce] = (sid, _secret_text_digest(text), tags, now + _SECRET_OPTOUT_TTL_S)
    return nonce


def _consume_secret_optout(sid, text, optout) -> frozenset:
    """Spend a confirmed opt-out; returns the tags to leave raw (empty = mask everything)."""
    import time
    if not isinstance(optout, dict) or not isinstance(text, str) or not text:
        return frozenset()
    nonce = optout.get("confirm_nonce")
    tags = optout.get("tags")
    if not isinstance(nonce, str) or not nonce or not isinstance(tags, list) or not tags:
        return frozenset()
    with _secret_optout_lock:
        entry = _secret_optout_nonces.pop(nonce, None)  # single use, even on a mismatch
    if entry is None:
        return frozenset()
    bound_sid, digest, bound_tags, expires_at = entry
    requested = frozenset(t for t in tags if isinstance(t, str))
    from agent.secret_hygiene import load_secret_hygiene_config
    if (time.monotonic() > expires_at or bound_sid != str(sid or "")
            or digest != _secret_text_digest(text) or not requested or requested != bound_tags
            or not load_secret_hygiene_config().optout_allowed):
        return frozenset()
    return requested


@method("secrets.mask")
def _(rid, params: dict) -> dict:
    from agent.secret_hygiene import (
        ingress_config, ingress_kind_counts, ingress_secret_tags, mask_ingress_text)
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    raw = params.get("text") or ""
    text = sanitize_user_prompt_text(raw) if isinstance(raw, str) else ""
    cfg = ingress_config()
    if cfg is None or not text:
        return _ok(rid, {"text": text, "kinds": {}, "tags": [], "confirm_nonce": None})
    masked, findings = mask_ingress_text(text, config=cfg)
    tags = ingress_secret_tags(text, config=cfg)
    nonce = None
    requested = params.get("optout_tags")
    sid = str(params.get("session_id") or "")
    if isinstance(requested, list) and requested and sid and cfg.optout_allowed:
        wanted = frozenset(t for t in requested if isinstance(t, str))
        if wanted and wanted <= {t["tag"] for t in tags}:
            nonce = _mint_secret_optout_nonce(sid, text, wanted)
    return _ok(rid, {"text": masked, "kinds": ingress_kind_counts(findings), "tags": tags,
                     "confirm_nonce": nonce})


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
