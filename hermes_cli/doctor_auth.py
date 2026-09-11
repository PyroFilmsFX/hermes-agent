"""Claude Agent SDK (subscription) row for ``hermes doctor``. Split out of ``hermes_cli/doctor.py``."""

from __future__ import annotations

from hermes_cli.doctor_report import Finding, check_info, check_ok, check_warn, doctor_check, warn_on_error


def _report_sdk_permission_mode(check_info) -> None:
    """One honest line about what the SDK lane's permission mode costs. (cntrl carry)"""
    try:
        from agent.transports.claude_agent_sdk_session import _configured_permission_mode
        mode = _configured_permission_mode() or "default (env mapping)"
    except Exception:
        return
    if mode.startswith("auto"):
        check_info("permission_mode=auto: the CLI's classifier screens tool calls; Hermes' guardian "
                   "runs only on prompt fall-through")
    else:
        check_info(f"permission_mode={mode}: every Bash call reaching the approval callback spawns a "
                   "guardian one-shot (a Claude CLI process) — consider "
                   "agent.claude_agent_sdk.permission_mode: auto")


@doctor_check()
def _check_claude_agent_sdk_auth(should_fix: bool, f: Finding) -> None:
    """Structural probe (env var / ~/.claude credential files): a macOS Keychain-only login reports as
    not-detected and the hint says so. Its own best-effort block so an import failure cannot disrupt
    the OAuth rows printed before it."""
    with warn_on_error(""):
        from hermes_cli.auth import get_claude_agent_sdk_auth_status
        sdk_status = get_claude_agent_sdk_auth_status() or {}
        if sdk_status.get("logged_in"):
            check_ok("Claude Agent SDK (subscription)", f"({sdk_status.get('source', 'credentials found')})")
            _report_sdk_permission_mode(check_info)
        else:
            check_warn("Claude Agent SDK (subscription)", "(no credential detected)")
            if sdk_status.get("hint"):
                check_info(sdk_status["hint"])
        # The SDK python package is an opt-in extra that lazy-installs at first use — mirror the
        # codex-CLI availability hint.
        with warn_on_error(""):
            from tools.lazy_deps import is_available
            if not is_available("provider.claude_agent_sdk"):
                check_info("claude-agent-sdk package not installed (optional — installs at first use, or: "
                           "pip install 'hermes-agent[claude-agent-sdk]')")
