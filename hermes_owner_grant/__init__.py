"""Owner-grant verifier package (stdlib-only; see HE-OWNER-FORWARD verify addendum)."""

from typing import Mapping, Optional


def hosted_session(env: Mapping[str, Optional[str]]) -> bool:
    """True when a hook runs inside a Hermes-hosted (SDK) Claude session, where conductor's local
    owner-line capture must be OFF and only signed grants count (addendum §7 item 3, D-16, T-5).

    Hosted = ``HERMES_SESSION_ID`` is non-empty OR ``CLAUDE_CODE_ENTRYPOINT`` is not exactly
    ``"cli"``. Either signal alone turns capture off, so the check fails safe: a missing or unknown
    entrypoint counts as hosted, and a settings ``env`` block that rewrites one variable can't make a
    hosted session look like a plain terminal while the other still says hosted. Conductor may copy
    this function verbatim (stdlib only, no I/O)."""
    session_id = env.get("HERMES_SESSION_ID")
    if isinstance(session_id, str) and session_id.strip():
        return True
    return env.get("CLAUDE_CODE_ENTRYPOINT") != "cli"
