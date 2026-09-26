"""Classify Claude Agent SDK API failures without scheduling retries."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping

from agent.error_classifier import (
    _AUTH_PATTERNS,
    _BILLING_PATTERNS,
    _CONNECTION_MESSAGE_PATTERNS,
    _CONTEXT_OVERFLOW_PATTERNS,
    _OVERLOADED_PATTERNS,
    _SSL_CERT_VERIFY_PATTERNS,
    _TIMEOUT_MESSAGE_PATTERNS,
    _UNVERIFIED_BILLING_PATTERNS,
)

_SDK_CONNECTION_NEEDLES = (
    "can't reach the api server", "enotfound", "eai_again", "econnrefused",
    "econnreset", "etimedout", "ehostunreach", "enetunreach",
    "connection error", "unable to connect", "socket hang up",
    "network is unreachable",
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _reset_timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def classify_sdk_api_failure(
    signals: Mapping[str, Any],
) -> tuple[str, str, float | None]:
    """Return ``(verdict, class, wait_hint_s)`` using the D0 first-match table."""
    turn = signals.get("turn")
    agent = signals.get("agent")
    if (
        bool(getattr(turn, "interrupted", False))
        or (isinstance(turn, Mapping) and bool(turn.get("interrupted")))
        or bool(signals.get("interrupt_observed"))
        or bool(signals.get("agent_interrupt_requested"))
        or bool(getattr(agent, "_interrupt_requested", False))
    ):
        return "none", "interrupt", None

    kind = str(signals.get("api_error_kind") or "").lower()
    status = signals.get("api_error_status")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError, OverflowError):
        status = None
    retries = signals.get("api_retries")
    retries = retries if isinstance(retries, Mapping) else {}
    rate_limit = signals.get("rate_limit_rejected")
    rate_limit = rate_limit if isinstance(rate_limit, Mapping) else None
    text = str(signals.get("result_text") or "").lower()
    retry_error = str(retries.get("error") or "").lower()

    # 1. Auth
    if (
        signals.get("fatal_reason") == "auth"
        or kind == "authentication_failed"
        or status in {401, 403}
        or _matches(text, _AUTH_PATTERNS)
    ):
        return "permanent", "auth", None

    # 2. Billing
    if (
        bool(signals.get("billing_guard_violation"))
        or kind == "billing_error"
        or status == 402
        or _matches(text, _BILLING_PATTERNS)
        or _matches(text, _UNVERIFIED_BILLING_PATTERNS)
        or (rate_limit is not None and str(rate_limit.get("rate_limit_type") or "").lower() == "overage")
    ):
        return "permanent", "billing", None

    # 3. Validation
    if (
        kind == "invalid_request"
        or (status is not None and 400 <= status <= 499 and status not in {408, 429})
        or _matches(text, _CONTEXT_OVERFLOW_PATTERNS)
    ):
        return "permanent", "validation", None

    # 4. Rate limit
    if status == 429 or kind == "rate_limit" or rate_limit is not None:
        wait_hint = None
        if rate_limit is not None:
            reset = _reset_timestamp(rate_limit.get("resets_at"))
            if reset is not None:
                wait_hint = max(0.0, reset - float(signals.get("now", time.time())))
        return "transient", "rate_limit", wait_hint

    # 5. Overload
    if status == 529 or kind == "overloaded" or _matches(text, _OVERLOADED_PATTERNS):
        return "transient", "overloaded", None

    # 6. Timeout
    if (
        status in {408, 504, 524}
        or _matches(text, _TIMEOUT_MESSAGE_PATTERNS)
        or "no-response" in retry_error
        or "no response" in retry_error
    ):
        return "transient", "timeout", None

    # 7. Other server failures
    if status is not None and 500 <= status <= 599:
        return "transient", "server_error", None

    # 8. Connection failures (plain lower-cased substring matching).
    if (
        status is None
        and (kind == "server_error" or retries.get("error_status") is None)
        and (
            _matches(text, _SDK_CONNECTION_NEEDLES)
            or _matches(text, _CONNECTION_MESSAGE_PATTERNS)
        )
    ):
        return "transient", "connection", None

    # 9. Deterministic certificate failures
    if _matches(text, _SSL_CERT_VERIFY_PATTERNS):
        return "permanent", "ssl", None

    # 10. Fail closed, including stream death and max-turn/budget errors.
    return "permanent", "other", None
