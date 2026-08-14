"""Session adapter for the claude-agent-sdk runtime.

Owns one Claude Agent SDK client per Hermes session — the structural twin of
``codex_app_server_session.py``, with the Codex JSON-RPC subprocess replaced
by Anthropic's official ``claude-agent-sdk`` (which manages the Claude Code
CLI subprocess, its agent loop, and — critically — **subscription OAuth**:
``CLAUDE_CODE_OAUTH_TOKEN`` / the ``~/.claude`` credential store, never a
metered ``ANTHROPIC_API_KEY``). See GitHub issue #25267.

Lifecycle:
    session = ClaudeAgentSdkSession(cwd="/home/x/proj", model="claude-opus-4-8")
    session.ensure_started()                       # loop thread + SDK connect
    result = session.run_turn(user_input="hello")  # blocks until ResultMessage
    session.close()                                # disconnect + stop loop

Threading model: the SDK is async-first, but AIAgent.run_conversation() is
synchronous (the same constraint that made CodexAppServerClient thread-based).
The adapter owns a dedicated background thread running one asyncio event loop
for the whole session lifetime; every SDK coroutine is marshaled onto it with
``asyncio.run_coroutine_threadsafe`` and awaited with a timeout, so the SDK
client keeps stable loop affinity and ``run_turn`` stays blocking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

# TurnResult is the shared contract with the runtime glue — reused verbatim
# from the codex session module (same fields, same semantics) so
# ``run_claude_agent_sdk_turn`` mirrors ``run_codex_app_server_turn`` 1:1.
from agent.transports.codex_app_server_session import TurnResult
from agent.transports.claude_sdk_event_projector import ClaudeSdkEventProjector

logger = logging.getLogger(__name__)


# HERMES_TERMINAL_SECURITY_MODE → SDK permission_mode, read at session
# construction (default "auto"). Precedence: explicit constructor arg, then
# the agent.claude_agent_sdk.permission_mode config key (an SDK mode literal
# — see _configured_permission_mode), then this env mapping.
#
# Posture, stated honestly: "auto" → acceptEdits is NOT codex parity, it is
# merely the closest available mode. Codex's default workspace-write profile
# still surfaces escalations for approval; acceptEdits auto-approves file
# edits under cwd with NO Hermes approval callback in the loop — the
# can_use_tool bridge is wired ONLY in "default" mode (see
# build_option_fields), and bypassPermissions disables SDK permission
# prompts entirely. Operators who want the gateway's approval flow set
# HERMES_TERMINAL_SECURITY_MODE=approval-required or
# agent.claude_agent_sdk.permission_mode: default in config.yaml.
_HERMES_TO_SDK_PERMISSION_MODE = {
    "auto": "acceptEdits",
    "approval-required": "default",
    "unrestricted": "bypassPermissions",
    "yolo": "bypassPermissions",
}

# The SDK's own permission_mode literals (verified against the installed
# claude-agent-sdk 0.2.120 ClaudeAgentOptions type).
_SDK_PERMISSION_MODES = (
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
    "auto",
)


def _configured_permission_mode() -> Optional[str]:
    """agent.claude_agent_sdk.permission_mode from config.yaml, validated.

    Takes an SDK permission_mode literal VERBATIM (one of
    _SDK_PERMISSION_MODES — note "auto" here is the SDK's own mode, NOT the
    HERMES_TERMINAL_SECURITY_MODE value of the same name). Empty/absent —
    the default — keeps current behavior: the HERMES_TERMINAL_SECURITY_MODE
    mapping stands, so existing deployments harden without env archaeology
    only when they opt in. Unknown values are ignored with a warning:
    permissions must never silently loosen (or tighten into an unusable
    mode) on a typo."""
    raw = str(_provider_config().get("permission_mode") or "").strip()
    if not raw:
        return None
    if raw not in _SDK_PERMISSION_MODES:
        logger.warning(
            "agent.claude_agent_sdk.permission_mode=%r is not a valid SDK "
            "permission mode (one of %s) — ignoring it; the "
            "HERMES_TERMINAL_SECURITY_MODE mapping stands.",
            raw,
            ", ".join(_SDK_PERMISSION_MODES),
        )
        return None
    return raw


# The SDK's own setting-source literals (verified against the installed
# claude-agent-sdk 0.2.120 SettingSource type).
_SDK_SETTING_SOURCES = ("user", "project", "local")


def _configured_setting_sources() -> list:
    """agent.claude_agent_sdk.setting_sources from config.yaml, validated.

    Default (absent/empty) is FULL ISOLATION — the SDK loads no filesystem
    settings, so ambient ``~/.claude`` or project files cannot re-permission
    tools or install hooks underneath the configured posture. Deployments
    whose operating model deliberately stores tool grants in the operator's
    own ``~/.claude/settings.json`` — an unattended box whose cron turns
    must pre-approve WebSearch/MCP tools with no human to answer a prompt —
    opt back in explicitly (``setting_sources: ["user"]``). Unknown entries
    are dropped with a warning: a typo must neither silently widen isolation
    nor quietly load an unintended source."""
    raw = _provider_config().get("setting_sources")
    if not isinstance(raw, (list, tuple)):
        return []
    sources: list = []
    for entry in raw:
        name = str(entry or "").strip()
        if name in _SDK_SETTING_SOURCES:
            if name not in sources:
                sources.append(name)
        elif name:
            logger.warning(
                "agent.claude_agent_sdk.setting_sources entry %r is not a "
                "valid SDK setting source (one of %s) — dropping it.",
                name,
                ", ".join(_SDK_SETTING_SOURCES),
            )
    return sources


# ---------- turn-lifetime defaults ----------
# The soft turn budget. It was a hard wall-clock over the whole turn since the
# provider's birth (transplanted verbatim from the codex twin, where a
# post-tool quiet watchdog compensates); production forensics on six 600s
# kills showed four were actively-working turns (tool loops, human approval
# taps) — so the budget is now evaluated together with activity evidence
# (see _TurnWatch) instead of alone.
_DEFAULT_TURN_TIMEOUT = 600.0
# Post-tool quiet watchdog default WHEN streaming is on (codex parity: its
# post_tool_quiet_timeout=90 mirrors openclaw's #81697 watchdog). With
# streaming OFF there is no liveness signal between a tool result and the
# next complete AssistantMessage — thinking is indistinguishable from wedged
# — so the watchdog defaults to DISABLED there (operator opt-in).
_DEFAULT_POST_TOOL_QUIET_STREAMING = 90.0
# The budget rule only fires when the turn has ALSO been quiet this long
# (capped at the budget itself so tiny test budgets keep tripping at the
# budget): an actively-producing turn is never killed mid-sentence.
_BUDGET_GRACE_IDLE = 30.0
# After a watchdog trip we interrupt the CLI and give _consume_turn this long
# to unwind on the interrupt-ack ResultMessage — a clean unwind preserves the
# partial transcript and the resumable session id; only expiry hard-cancels.
_TURN_ABORT_GRACE = 15.0
# An inter-poll gap this many times the poll interval means the PROCESS was
# stalled (swap/OOM descheduling on a memory-constrained host), not the turn
# — re-baseline instead of tripping on time nobody was actually waiting.
_POLL_STALL_FACTOR = 5.0


def _configured_timeout_seconds(key: str, *, allow_zero: bool) -> Optional[float]:
    """Numeric seconds from `agent.claude_agent_sdk.<key>`, validated.

    Same warn-and-fall-back contract as _configured_max_budget_usd: bools are
    rejected before float() (YAML `true` must not become 1.0), non-numeric and
    negative values warn and yield None (= use the built-in default). `0` is
    key-specific: allowed only where "disabled" is a documented meaning."""
    raw = _provider_config().get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        logger.warning(
            "agent.claude_agent_sdk.%s=%r is a boolean, not seconds — "
            "ignoring it (using the built-in default).", key, raw,
        )
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "agent.claude_agent_sdk.%s=%r is not a number of seconds — "
            "ignoring it (using the built-in default).", key, raw,
        )
        return None
    if value < 0 or (value == 0 and not allow_zero):
        logger.warning(
            "agent.claude_agent_sdk.%s=%r is out of range — ignoring it "
            "(using the built-in default).", key, raw,
        )
        return None
    return value


def _configured_turn_timeout() -> Optional[float]:
    """agent.claude_agent_sdk.turn_timeout (seconds). Positive only — there
    is deliberately no `0 = unlimited`: the gateway's inactivity monitor
    (`agent.gateway_timeout`, 1800s) is the outer ceiling for SDK turns and
    an unlimited soft budget under it would leave the quiet watchdog as the
    only transport-level bound."""
    return _configured_timeout_seconds("turn_timeout", allow_zero=False)


def _configured_post_tool_quiet_timeout() -> Optional[float]:
    """agent.claude_agent_sdk.post_tool_quiet_timeout (seconds).
    `0` = explicitly disabled. Absent/None = streaming-dependent default
    (90s with streaming on, disabled with streaming off)."""
    return _configured_timeout_seconds("post_tool_quiet_timeout", allow_zero=True)


class _TurnWatch:
    """Activity evidence for one in-flight turn.

    Threading contract: mutating calls happen on the session's loop thread
    (message drain, projections, approval bridge) with ONE sanctioned
    exception — rebaseline(), called from the run_turn poll thread, also
    writes last_activity. That dual-writer race is benign by construction:
    float stores are GIL-atomic (never torn), and a lost update leaves
    last_activity merely STALE, which the stall detector re-baselines and
    the two-poll debounce absorbs before any verdict — trips can only be
    DELAYED by it, never wrongly fired. Everything else is single-writer;
    the poll thread otherwise only READS. No lock, deliberately: a lock
    shared with the loop thread would risk stalling the SDK stream drain.

    Evidence gate: a turn with tool calls outstanding (issued ToolUseBlocks
    minus resolved ToolResultBlocks — server tools never enter the count,
    they resolve inside their own assistant message) or an approval prompt
    awaiting a human tap is PROVABLY working/waiting and is never tripped.
    If the CLI never resolves an issued tool id (interrupted mid-tool), the
    suspension persists and the gateway's 1800s inactivity ceiling remains
    the backstop — documented, deliberate."""

    def __init__(self) -> None:
        now = time.monotonic()
        self.started = now
        self.last_activity = now
        self.post_tool_armed = False
        self.outstanding_tools = 0
        self.approvals_pending = 0

    # -- loop-thread writers --

    def tick(self) -> None:
        self.last_activity = time.monotonic()

    def note_tools_issued(self, count: int) -> None:
        if count > 0:
            self.outstanding_tools += count

    def note_tools_resolved(self, count: int) -> None:
        if count > 0:
            self.outstanding_tools = max(0, self.outstanding_tools - count)

    def arm_post_tool(self) -> None:
        self.post_tool_armed = True

    def disarm_post_tool(self) -> None:
        self.post_tool_armed = False

    def approval_begin(self) -> None:
        self.approvals_pending += 1
        self.tick()

    def approval_end(self) -> None:
        self.approvals_pending = max(0, self.approvals_pending - 1)
        self.tick()

    # -- caller-thread readers --

    def rebaseline(self) -> None:
        """After a detected process stall: the elapsed gap was spent
        descheduled, not waiting — restamp so neither rule fires on it."""
        self.last_activity = time.monotonic()

    def check(self, *, budget: float, quiet: float) -> Optional[str]:
        """Returns None (keep waiting), "post_tool_quiet", or "budget"."""
        if self.outstanding_tools > 0 or self.approvals_pending > 0:
            return None
        now = time.monotonic()
        idle = now - self.last_activity
        if quiet > 0 and self.post_tool_armed and idle >= quiet:
            return "post_tool_quiet"
        elapsed = now - self.started
        if elapsed >= budget and idle >= min(_BUDGET_GRACE_IDLE, budget):
            return "budget"
        return None


def _swallow_interrupt_result(future: Any) -> None:
    """Done-callback for the fire-and-forget client.interrupt() future: the
    SDK's control request times out after 60s on a wedged CLI and an
    unretrieved exception would log 'Future exception was never retrieved'
    at teardown — retrieve and demote it."""
    try:
        future.result()
    except Exception:
        logger.debug("SDK interrupt control request failed", exc_info=True)


# Substrings in SDK/CLI errors that signal broken subscription credentials.
# Conservative on purpose — mirrors codex's _OAUTH_REFRESH_FAILURE_HINTS
# contract: every needle is a phrase, never a bare token. Bare "401" matched
# tool ids and byte offsets; bare "credentials" matched an MCP server
# complaining about its OWN files — and a hit retires the session, so a
# false positive is a wrong shutdown.
_AUTH_FAILURE_HINTS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "401 unauthorized",
    "unauthorized",
    "oauth token",
    "token has expired",
    "expired token",
    "invalid bearer token",
    "setup-token",
)


def classify_auth_failure(*parts: str) -> Optional[str]:
    """Return a user-friendly re-auth hint if the strings look like a Claude
    subscription auth failure; otherwise None. The hint keeps the underlying
    error text: a hit retires the session, so the evidence must survive the
    redirect (codex surfaces the original error the same way)."""
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return None
    for needle in _AUTH_FAILURE_HINTS:
        if needle in haystack:
            original = next((p.strip() for p in parts if p and p.strip()), "")
            if len(original) > 400:
                original = original[:400] + "…"
            return (
                "Claude authentication failed — the subscription OAuth token "
                "looks expired or invalid. Refresh it with `claude setup-token` "
                "(or `claude login` on this machine) and update "
                "CLAUDE_CODE_OAUTH_TOKEN, then retry. "
                f"(underlying error: {original})"
            )
    return None


def check_claude_sdk_available() -> tuple[bool, str]:
    """Preflight: the optional SDK extra must be importable, and it bundles /
    locates the Claude Code CLI itself. Mirrors check_codex_binary()."""
    # Lazy-install lane, mirroring agent/anthropic_adapter._get_anthropic_sdk:
    # the extra is opt-in (excluded from [all]), so first use on a lean
    # install goes through tools.lazy_deps.ensure. FeatureUnavailable falls
    # through to the ImportError message below — same fail shape either way.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("provider.claude_agent_sdk", prompt=False)
    except ImportError:
        pass
    except Exception:
        # FeatureUnavailable — fall through to ImportError handling below
        pass
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return (
            False,
            "claude-agent-sdk is not installed. "
            "Install with: pip install 'hermes-agent[claude-agent-sdk]'",
        )
    return True, "ok"


def _hermes_repo_root() -> str:
    """Repo root for the hermes-tools MCP subprocess (PYTHONPATH)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


# The SDK spawns the Claude Code CLI with the FULL inherited parent env and
# merges ``ClaudeAgentOptions.env`` ON TOP of it (subprocess_cli.py builds
# ``{**os.environ, ..., **options.env, ...}``), so a key can never be REMOVED
# from the options side — the only lever is an explicit override. Every
# metered billing vector below is overridden to "" (the CLI and the AWS/GCP
# SDKs treat an empty value as unset), so a credentialed gateway environment
# cannot silently re-route this provider's billing off the Claude
# subscription. Why each class of key is stripped:
#
#   ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN — the CLI prefers these over
#     subscription OAuth: the exact silent-rebilling the fail-closed startup
#     guard exists to stop. The guard covers Hermes' own process at startup;
#     this scrub covers the spawned CLI, which otherwise inherits them.
#   CLAUDE_CODE_USE_BEDROCK + AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
#     AWS_SESSION_TOKEN — flips the CLI onto AWS Bedrock, billing the AWS
#     account (metered) instead of the subscription; the AWS static
#     credentials are the vector that makes the flip actually authenticate.
#   CLAUDE_CODE_USE_VERTEX + GOOGLE_APPLICATION_CREDENTIALS — the same
#     takeover via Google Vertex, billing the GCP project.
#
# Deliberately NOT stripped: HOME/PATH (the CLI needs them to run and to find
# its credential store) and the subscription token flow itself —
# CLAUDE_CODE_OAUTH_TOKEN / ~/.claude — which is what this provider runs on.
_METERED_ENV_DENYLIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def _is_subscription_oauth_token(value: str) -> bool:
    """True when an ANTHROPIC_TOKEN value is OAuth/setup-token shaped — the
    subscription lane itself, not a metered vector. Unknown shapes count as
    metered (fail closed), including when the classifier cannot be imported."""
    try:
        from agent.anthropic_adapter import _is_oauth_token
    except Exception:
        return False
    try:
        return bool(_is_oauth_token(value))
    except Exception:
        return False


def _scrubbed_sdk_env() -> dict[str, str]:
    """Empty-string overrides for every metered billing vector currently set
    in the parent environment. Only PRESENT keys are overridden — writing
    ``""`` for absent ones would introduce empty vars the child never had
    (an empty AWS_ACCESS_KEY_ID can itself confuse AWS credential chains)."""
    return {key: "" for key in _METERED_ENV_DENYLIST if os.environ.get(key)}


# The SDK serializes the stdio MCP config — env INCLUDED — into the claude
# CLI's --mcp-config argument, i.e. onto the subprocess argv, which any local
# user can read via ps. Nothing secret may ever ride this dict: the env is a
# minimal ALLOWLIST, never a copy of the credentialed environment. Keyed
# Hermes tools inside the server degrade via their own check_fns — the
# subscription lane's fail-closed posture.
_MCP_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "PYTHONUTF8",
    "HERMES_HOME",
    "HERMES_KANBAN_TASK",
    "HERMES_MCP_STATE_DB",  # the shims' documented state-DB override — a path, not a secret
    "HERMES_QUIET",
    "HERMES_REDACT_SECRETS",
)


def _provider_config() -> dict:
    """The `agent.claude_agent_sdk` config block ({} when absent/unreadable)."""
    try:
        from hermes_cli.config import load_config_readonly

        block = ((load_config_readonly() or {}).get("agent", {}) or {}).get(
            "claude_agent_sdk", {}
        )
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _provider_flag(config_key: str, default: bool = False) -> bool:
    """Behavioural flag read from `agent.claude_agent_sdk.<key>` in config.yaml.

    config.yaml is the ONLY interface. AGENTS.md keeps non-secret behavioural
    settings out of `HERMES_*` environment variables, so there is deliberately
    no env override here — a deployment sets the key in config.yaml.
    Canonical defaults live in `hermes_cli/config.py::DEFAULT_CONFIG`.
    """
    value = _provider_config().get(config_key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _build_hermes_tools_mcp_config(
    hermes_session_id: Optional[str] = None,
) -> dict[str, Any]:
    """The stdio MCP server exposing Hermes tools into the SDK agent loop —
    the exact server the codex runtime uses (backend-agnostic), launched with
    this venv's interpreter. McpStdioServerConfig has no cwd field, so the
    repo root rides PYTHONPATH."""
    env = {
        key: os.environ[key]
        for key in _MCP_ENV_ALLOWLIST
        if os.environ.get(key)
    }
    env["PYTHONPATH"] = _hermes_repo_root() + os.pathsep + os.environ.get("PYTHONPATH", "")
    if hermes_session_id:
        # Lets the stateless session_search shim exclude the calling
        # session's own lineage from recall results (#26567). The shim reads
        # the canonical HERMES_SESSION_ID — set explicitly with THIS
        # session's id rather than allowlisting the ambient variable, so a
        # multi-session host can never leak a sibling session's id into the
        # subprocess.
        env["HERMES_SESSION_ID"] = str(hermes_session_id)
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
        "env": env,
    }


class _StreamEnd:
    """Reader-loop sentinel: the SDK message stream ended (CLI exited or the
    transport tore down). Routed to the in-flight turn so it fails fast and
    retires cleanly instead of waiting out its full turn_timeout on a dead
    stream."""

    def __init__(self, error: Optional[str] = None) -> None:
        self.error = error


class ClaudeAgentSdkSession:
    """One SDK client per Hermes session, lifetime owned by AIAgent.

    Not thread-safe from the caller's side — one caller drives it at a time,
    matching AIAgent.run_conversation(). Internally owns a loop thread."""

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        system_prompt_append: Optional[str] = None,
        approval_callback: Optional[Callable[..., str]] = None,
        on_tool_started: Optional[Callable[[str, str, dict], None]] = None,
        max_budget_usd: Optional[float] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        include_hermes_tools: bool = True,
        hermes_session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        on_stream_delta: Optional[Callable[[str], None]] = None,
        on_unsolicited_result: Optional[Callable[[list[str]], None]] = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._model = model
        self._permission_mode = (
            permission_mode
            or _configured_permission_mode()
            or _HERMES_TO_SDK_PERMISSION_MODE.get(
                os.environ.get("HERMES_TERMINAL_SECURITY_MODE", "auto"),
                "acceptEdits",
            )
        )
        self._system_prompt_append = system_prompt_append
        self._approval_callback = approval_callback
        self._on_tool_started = on_tool_started
        self._max_budget_usd = max_budget_usd
        self._client_factory = client_factory  # test seam
        self._include_hermes_tools = include_hermes_tools
        # Hermes-side session id, exported to the hermes-tools MCP subprocess
        # so the stateless session_search shim can exclude its own lineage.
        self._hermes_session_id = hermes_session_id
        # SDK-side session id to resume (#25267 continuity). Verified live:
        # resume restores the model context and keeps the SAME session id; a
        # stale id fails the session start (the caller retires + retries
        # fresh).
        self._resume_session_id = resume_session_id
        # Display-only partial-text consumer (W4 streaming). Deltas never
        # enter the projected transcript; the gateway's stream consumer
        # handles rate limiting and the already_sent final-send dedup.
        self._on_stream_delta = on_stream_delta

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._session_id: Optional[str] = None
        self._interrupt_event = threading.Event()
        self._closed = False
        # Activity evidence for the in-flight turn (None between turns).
        self._turn_watch: Optional[_TurnWatch] = None
        # Snapshot the streaming posture once: the quiet-watchdog default is
        # derived from it, and a mid-session config edit must not flip the
        # watchdog's semantics while the live client still has the old
        # include_partial_messages option.
        self._streaming = _provider_flag("streaming")
        # Stream ownership (see _reader_loop). The reader task is the ONLY
        # consumer of the SDK stream; `_turn_inbox` is non-None exactly while a
        # turn is in flight, which is what makes "unsolicited" decidable.
        self._reader_task: Any = None
        self._turn_inbox: Any = None
        self._unsolicited_results = 0
        self._stream_ended: Optional[_StreamEnd] = None
        # Delivery half of the stream-ownership fix (dasbrow-hermes-coder#2):
        # a finished background Agent task's answer is captured here and
        # handed to the callback — it must never enter a turn's result, but
        # dropping it entirely left completed work silently undelivered
        # (observed live 2026-07-29: answers sat in the CLI session until the
        # operator poked). No callback wired = the historical drop semantics.
        self._on_unsolicited_result = on_unsolicited_result
        self._unsolicited_text: list[str] = []
        self._unsolicited_delivered: set[str] = set()

    # ---------- lifecycle ----------

    def ensure_started(self) -> str:
        """Start the loop thread, build the SDK client, connect. Idempotent —
        returns the session marker (SDK session ids arrive on first result)."""
        if self._client is not None:
            return self._session_id or "pending"
        # Hard rule, enforced fail-closed: this provider exists to bill the
        # Claude SUBSCRIPTION. If a metered ANTHROPIC_API_KEY is present the
        # underlying CLI would silently prefer it — refuse to start instead.
        # ANTHROPIC_TOKEN is in the set because it alone authenticates
        # Hermes' NATIVE Anthropic lane (x-api-key, metered) — but Hermes
        # also persists subscription setup tokens there, so only an
        # API-key-shaped value counts as a metered vector.
        allow_metered = _provider_flag("allow_metered_key")
        for metered_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"):
            value = os.environ.get(metered_var)
            if not value or allow_metered:
                continue
            if metered_var == "ANTHROPIC_TOKEN" and _is_subscription_oauth_token(value):
                continue
            raise RuntimeError(
                f"claude-agent-sdk runtime refuses to start: {metered_var} "
                "is set, which would silently switch billing from the "
                "Claude subscription to metered API usage. Unset it, or "
                "set agent.claude_agent_sdk.allow_metered_key: true in "
                "config.yaml to explicitly allow it."
            )
        if self._client_factory is None:
            ok, msg = check_claude_sdk_available()
            if not ok:
                raise RuntimeError(msg)

        self._start_loop_thread()
        client = self._build_client()
        # Assign BEFORE connect: a connect timeout/cancel leaves a
        # half-connected client whose CLI subprocess close() must still reap
        # — a None _client would skip disconnect and orphan it.
        self._client = client
        self._run_coro(client.connect(), timeout=60.0)
        # From here on exactly ONE consumer owns the SDK stream (_reader_loop).
        # Started after connect so the client is live, before any turn so no
        # message can arrive unowned.
        self._start_reader()
        logger.info(
            "claude-agent-sdk session started: model=%s mode=%s cwd=%s",
            self._model or "cli-default",
            self._permission_mode,
            self._cwd,
        )
        return self._session_id or "pending"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Cancel the reader BEFORE disconnect so it unwinds on a live stream
        # instead of raising against a torn-down one.
        self._stop_reader()
        if self._client is not None and self._loop is not None:
            try:
                self._run_coro(self._client.disconnect(), timeout=10.0)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._client = None
        self._stop_loop_thread()

    def __enter__(self) -> "ClaudeAgentSdkSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- interrupt ----------

    def consume_interrupt(self) -> None:
        """Clear a pending interrupt signal — the caller honored it through
        another path (e.g. the runtime's cold-agent short-circuit)."""
        self._interrupt_event.clear()

    def request_interrupt(self) -> None:
        """Idempotent: signal the active turn loop to interrupt and unwind."""
        self._interrupt_event.set()
        if self._client is not None and self._loop is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._client.interrupt(), self._loop
                )
                future.add_done_callback(_swallow_interrupt_result)
            except Exception:  # pragma: no cover
                logger.debug("SDK interrupt scheduling failed", exc_info=True)

    # ---------- per-turn ----------

    def run_turn(
        self,
        user_input: Any,
        *,
        turn_timeout: Optional[float] = None,
        post_tool_quiet_timeout: Optional[float] = None,
        watch_poll_interval: float = 1.0,
        abort_grace: float = _TURN_ABORT_GRACE,
    ) -> TurnResult:
        """Send a user message and block until the SDK's ResultMessage,
        projecting the typed stream into Hermes' messages shape.

        Turn lifetime is activity-aware, not a bare wall clock (production
        forensics: four of six 600s kills were actively-working turns).
        `turn_timeout` (explicit arg > agent.claude_agent_sdk.turn_timeout >
        600s) is a SOFT budget: it only fires when nothing is outstanding —
        no tool running, no approval awaiting a human — and the stream has
        ALSO been quiet ≥ min(30s, budget). A post-tool quiet watchdog
        (`post_tool_quiet_timeout`; default 90s with streaming on, disabled
        with streaming off) catches wedges early: armed when a tool result
        arrives, cleared by any later activity. On a trip the CLI is
        interrupted and given `abort_grace` to unwind — a clean unwind keeps
        the partial transcript and the resumable session id (no retire);
        only a grace expiry hard-cancels and retires."""
        result = TurnResult()
        try:
            self.ensure_started()
        except Exception as exc:
            hint = classify_auth_failure(str(exc))
            result.error = hint or f"claude-agent-sdk startup failed: {exc}"
            result.should_retire = True
            # A refusal to start is fatal to the run, not turn-scoped: the
            # metered-key guard and an uninstallable SDK are config errors
            # ("startup" — NOT "billing": kanban maps failure_reason
            # "billing" to the transient EX_TEMPFAIL requeue sentinel, and
            # retrying cannot fix a present metered key); an auth-classified
            # failure is "auth".
            result.fatal_reason = "auth" if hint else "startup"
            return result

        # Text buffered before this turn whose terminal ResultMessage never
        # arrived is partial mid-burst content — a later unrelated result
        # must never pick it up (misattribution is the proven worse failure;
        # texts parked DURING the turn re-buffer via the residue drain and
        # are unaffected). Never a silent drop: WARN what is discarded.
        stale = list(self._unsolicited_text)
        if stale:
            self._unsolicited_text.clear()
            logger.warning(
                "claude-agent-sdk: discarding %d stale unsolicited text(s) "
                "(%d chars) buffered before this turn — their terminal "
                "ResultMessage never arrived; attaching them to a later "
                "unrelated result is the proven worse failure",
                len(stale), sum(len(t) for t in stale),
            )

        # An interrupt that arrived between turns or during connect (up to
        # 60s) targets THIS turn — honor it instead of erasing it. (The old
        # unconditional clear() silently swallowed that window.)
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            result.interrupted = True
            return result
        text = _coerce_turn_input_text(user_input)

        import concurrent.futures

        budget = (
            float(turn_timeout)
            if turn_timeout is not None
            else (_configured_turn_timeout() or _DEFAULT_TURN_TIMEOUT)
        )
        if post_tool_quiet_timeout is not None:
            quiet = float(post_tool_quiet_timeout)
        else:
            configured_quiet = _configured_post_tool_quiet_timeout()
            if configured_quiet is not None:
                quiet = configured_quiet  # 0 = explicitly disabled
            else:
                quiet = (
                    _DEFAULT_POST_TOOL_QUIET_STREAMING if self._streaming else 0.0
                )
        poll = max(0.01, float(watch_poll_interval))

        assert self._loop is not None, "loop thread not started"
        watch = _TurnWatch()
        self._turn_watch = watch
        trip: Optional[str] = None
        trip_elapsed = trip_idle = 0.0
        hard_trip = False
        turn_data: Optional[dict[str, Any]] = None
        future = asyncio.run_coroutine_threadsafe(
            self._consume_turn(text), self._loop
        )
        try:
            pending_verdict: Optional[str] = None
            prev_poll = time.monotonic()
            while turn_data is None and trip is None:
                try:
                    turn_data = future.result(timeout=poll)
                except (TimeoutError, concurrent.futures.TimeoutError):
                    # py3.11 unifies TimeoutError/asyncio.TimeoutError/
                    # concurrent.futures.TimeoutError — a DONE future here
                    # means the COROUTINE settled in the race window: with an
                    # exception (typically its own TimeoutError — a socket/
                    # pipe timeout under the CLI), re-raise into the
                    # classification path instead of misreading it as a poll
                    # expiry (which would busy-spin until the budget); with a
                    # RESULT, harvest it — the turn completed.
                    if future.done():
                        if future.exception() is not None:
                            raise
                        turn_data = future.result()
                        continue
                    now = time.monotonic()
                    if now - prev_poll > _POLL_STALL_FACTOR * poll:
                        # The PROCESS was descheduled (swap/OOM), not the
                        # turn — that gap proves nothing about the stream.
                        watch.rebaseline()
                        prev_poll = now
                        pending_verdict = None
                        continue
                    prev_poll = now
                    verdict = watch.check(budget=budget, quiet=quiet)
                    if verdict is None:
                        pending_verdict = None
                        continue
                    if pending_verdict != verdict:
                        # Debounce: two consecutive polls must agree before
                        # a trip (closes the tick-vs-check race window).
                        pending_verdict = verdict
                        continue
                    now = time.monotonic()
                    trip = verdict
                    trip_elapsed = now - watch.started
                    trip_idle = now - watch.last_activity
            if trip is not None and turn_data is None:
                if future.done():
                    # Completed between check() and the trip decision —
                    # completion wins, the trip is void.
                    turn_data = future.result()
                    trip = None
                else:
                    # Interrupt through request_interrupt(): setting
                    # _interrupt_event is REQUIRED — _consume_turn's W22 EDE
                    # mask keys on the turn-local flag, so the CLI's
                    # interrupt-ack is masked instead of delivered as a
                    # fresh error.
                    self.request_interrupt()
                    try:
                        turn_data = future.result(timeout=abort_grace)
                    except (TimeoutError, concurrent.futures.TimeoutError):
                        if future.done():
                            if future.exception() is not None:
                                raise
                            turn_data = future.result()
                        elif not future.cancel():
                            # Settled in the cancel race window — a real
                            # result beats a fabricated hard trip.
                            if future.exception() is not None:
                                raise
                            turn_data = future.result()
                        else:
                            hard_trip = True
            if trip is not None and turn_data is not None and not hard_trip:
                if (
                    not turn_data["error"]
                    and turn_data.get("result_uuid")
                    and turn_data["final_text"]
                ):
                    # The turn FINISHED inside the grace window with a real
                    # answer — deliver it in full; the trip never happened.
                    # final_text is REQUIRED: the W22-masked interrupt ack
                    # also has error=None + a result uuid, but its final_text
                    # is empty (projection stopped at the interrupt) — that
                    # shape must stay a trip, or it degrades into a silent
                    # dropped turn. Consume our own interrupt signal so the
                    # mapping below cannot misread it. (A real user /stop
                    # racing this exact window loses its signal too — the
                    # completed answer it targeted is delivered, same as any
                    # near-boundary stop today.)
                    self._interrupt_event.clear()
                    logger.info(
                        "claude-agent-sdk: turn completed during watchdog "
                        "grace (%.0fs elapsed) — delivered in full",
                        time.monotonic() - watch.started,
                    )
                    trip = None
        except Exception as exc:
            self._interrupt_event.clear()
            hint = classify_auth_failure(str(exc))
            result.error = hint or f"claude-agent-sdk turn failed: {exc}"
            result.should_retire = True
            if hint is not None:
                # Auth failures are fatal; other mid-turn exceptions stay
                # transient (retire + retry semantics unchanged).
                result.fatal_reason = "auth"
            return result
        finally:
            self._turn_watch = None

        if turn_data is None:
            # Hard trip: the CLI ignored the interrupt for the whole grace —
            # nothing was harvested. Retire (today's shape, now the rare
            # fallback for a genuinely unresponsive CLI).
            self._interrupt_event.clear()
            result.interrupted = True
            result.error = self._format_trip_error(
                trip, budget, quiet, trip_elapsed, trip_idle
            )
            result.should_retire = True
            return result

        result.final_text = turn_data["final_text"]
        result.projected_messages = turn_data["messages"]
        result.tool_iterations = turn_data["tool_iterations"]
        result.token_usage_last = turn_data["usage"]
        result.token_usage_total = turn_data["usage"]
        result.model_last = turn_data.get("model")
        result.thread_id = self._session_id
        result.turn_id = turn_data.get("result_uuid")
        result.interrupted = self._interrupt_event.is_set()
        if result.interrupted:
            # Consume the honored interrupt so it cannot bleed into the
            # next turn on this session object.
            self._interrupt_event.clear()
        if turn_data["error"]:
            hint = classify_auth_failure(turn_data["error"])
            result.error = hint or turn_data["error"]
            if hint is not None:
                result.should_retire = True
                result.fatal_reason = "auth"
        if turn_data.get("stream_ended"):
            # A dead stream is permanent on this session object: the reader
            # exited, _stream_ended stays set, and ensure_started() keeps
            # returning early while _client is non-None — so WITHOUT retire,
            # every later turn short-circuits to this same error with zero
            # model calls, forever (the CLI died once and poisoned the
            # session; observed as the unrecoverable half of the 2026-08-09
            # desync incident). Retire lets the runtime rebuild a fresh CLI
            # on the next attempt/turn. Model-level result subtypes
            # (error_max_turns, error_max_budget_usd) stay non-retiring.
            result.should_retire = True
        if trip is not None:
            # Clean-ack trip: the CLI honored our interrupt inside the grace,
            # the partial transcript above is preserved, and the session id
            # is resumable — mirror the user-interrupt lane (close-but-keep-
            # resume-id) instead of retiring. The trip text WINS over
            # whatever error the drain surfaced (typically the W22-masked
            # interrupt ack) — EXCEPT auth and stream-death, which keep
            # their retire verdicts from the mapping above.
            if result.fatal_reason != "auth":
                result.error = self._format_trip_error(
                    trip, budget, quiet, trip_elapsed, trip_idle
                )
            result.interrupted = True
        return result

    def _format_trip_error(
        self,
        kind: Optional[str],
        budget: float,
        quiet: float,
        elapsed: float,
        idle: float,
    ) -> str:
        """≤200 chars (the gateway truncates at str(err)[:200]); keeps the
        literal "turn timed out" needle; names the cause."""
        if kind == "post_tool_quiet":
            return (
                f"turn timed out: no SDK activity for {idle:.0f}s after a "
                f"tool result (turn ran {elapsed:.0f}s, quiet limit "
                f"{quiet:.0f}s)"
            )
        return (
            f"turn timed out after {elapsed:.0f}s "
            f"(budget {budget:.0f}s, idle {idle:.0f}s)"
        )

    # ---------- internals ----------

    async def _consume_turn(self, text: str) -> dict[str, Any]:
        """The async side of one turn: query, then read THIS turn's messages
        off the reader loop's inbox until the ResultMessage.

        Deliberately NOT `receive_response()` — that helper serves the oldest
        buffered ResultMessage, which is not necessarily ours. See
        `_reader_loop` for why that is a permanent-corruption bug."""
        projector = ClaudeSdkEventProjector()
        out: dict[str, Any] = {
            "final_text": "",
            "messages": [],
            "tool_iterations": 0,
            "usage": None,
            "error": None,
            "result_uuid": None,
            "model": None,
            "stream_ended": False,
        }
        ended = self._stream_ended
        if ended is not None:
            out["error"] = "SDK message stream ended before this turn" + (
                f": {ended.error}" if ended.error else ""
            )
            out["stream_ended"] = True
            return out
        inbox: Any = asyncio.Queue()
        # Claim the stream BEFORE query() — a fast first message must not land
        # while no turn is claimed and get routed away as unsolicited.
        self._turn_inbox = inbox
        interrupted = False
        try:
            await self._client.query(text)
            while True:
                message = await inbox.get()
                watch = self._turn_watch
                if watch is not None:
                    # Any stream message is liveness — stamp before anything
                    # else so the caller-thread watchdog sees it.
                    watch.tick()
                if isinstance(message, _StreamEnd):
                    out["error"] = (
                        "SDK message stream ended before this turn's result"
                        + (f": {message.error}" if message.error else "")
                    )
                    out["stream_ended"] = True
                    break
                # Capture the SDK session id from ANY message that carries it —
                # the init SystemMessage announces it first, so even a turn
                # interrupted before its ResultMessage keeps a resumable id.
                early_sid = getattr(message, "session_id", None)
                if early_sid:
                    self._session_id = early_sid
                if self._interrupt_event.is_set():
                    # Previously `break`. Bailing out before this turn's
                    # ResultMessage orphaned it in the shared stream, and the
                    # NEXT turn was then served that stale result — the same
                    # off-by-one corruption `_reader_loop` exists to prevent,
                    # reachable through the interrupt path with no Agent tool
                    # involved. Keep draining to the result; stop projecting.
                    interrupted = True
                if type(message).__name__ == "StreamEvent":
                    if watch is not None:
                        # A partial delta proves the post-tool model call is
                        # alive — the quiet watchdog stands down.
                        watch.disarm_post_tool()
                    if not interrupted:
                        self._forward_stream_delta(message)
                    continue
                if not interrupted:
                    self._notify_tool_started(message)
                projection = projector.project(message)
                if watch is not None:
                    # Outstanding-tool evidence: ToolUseBlocks issue, tool
                    # results resolve (server tools never enter — the
                    # projector resolves them inside their own assistant
                    # message). A turn with a tool in flight is suspended
                    # from BOTH watchdog rules.
                    issued = sum(
                        len(m.get("tool_calls") or [])
                        for m in projection.messages
                        if m.get("role") == "assistant"
                    )
                    if issued:
                        watch.note_tools_issued(issued)
                    if projection.is_tool_iteration:
                        watch.note_tools_resolved(
                            sum(
                                1
                                for m in projection.messages
                                if m.get("role") == "tool"
                            )
                        )
                        # Codex-parity arm point: a tool result just landed;
                        # silence from here on is the wedge signature.
                        watch.arm_post_tool()
                    elif projection.messages or projection.final_text is not None:
                        watch.disarm_post_tool()
                if projection.model:
                    # Last reported id wins; captured even on interrupted
                    # turns — the tokens were still spent on that model.
                    out["model"] = projection.model
                if not interrupted:
                    if projection.messages:
                        out["messages"].extend(projection.messages)
                    if projection.is_tool_iteration:
                        out["tool_iterations"] += 1
                    if projection.final_text is not None:
                        out["final_text"] = projection.final_text
                if projection.is_result:
                    usage = getattr(message, "usage", None)
                    if isinstance(usage, dict):
                        out["usage"] = dict(usage)
                    sid = getattr(message, "session_id", None)
                    if sid:
                        self._session_id = sid
                    out["result_uuid"] = getattr(message, "uuid", None)
                    subtype = getattr(message, "subtype", "") or ""
                    if getattr(message, "is_error", False):
                        errors = getattr(message, "errors", None) or []
                        if subtype == "success" and not errors:
                            # Contradictory envelope: is_error=True yet
                            # subtype="success" with nothing in errors. The
                            # CLI emits this shape rarely (2026-08-11: it
                            # killed a cron run as "RuntimeError: SDK result
                            # error (subtype=success): success" — a dead job
                            # over a turn that had actually produced its
                            # answer). There is no error to report, so the
                            # error flag loses to the subtype; kept loud for
                            # diagnosis. A genuine failure carries a non-empty
                            # errors list or a non-success subtype and still
                            # takes the honest path below.
                            logger.warning(
                                "claude-agent-sdk: contradictory result "
                                "envelope (is_error=True, subtype=success, "
                                "no errors) — treated as success: %r",
                                message,
                            )
                            break
                        err_text = (
                            f"SDK result error (subtype={subtype}): "
                            + ("; ".join(str(e) for e in errors) or subtype)
                        )
                        # A turn WE interrupted before any assistant content
                        # ends as is_error/error_during_execution in the CLI
                        # ("[ede_diagnostic] result_type=user…") — that is the
                        # interrupt being honored, not a failure; surfacing it
                        # paged the operator with a false "Processing stopped"
                        # (2026-08-09 barge-in incident). Mask ONLY that exact
                        # shape, only when THIS turn's interrupt flag is set
                        # (never a fresh event read — a late /stop must not
                        # reclassify a genuine error), and only when no auth
                        # needle is present (an auth failure must keep its
                        # error + retire path regardless of interrupts).
                        # A real transient failure colliding with an interrupt
                        # re-fires next turn with no interrupt pending and
                        # takes the honest path; the masked text is INFO-kept.
                        if (
                            interrupted
                            and subtype == "error_during_execution"
                            and classify_auth_failure(err_text) is None
                        ):
                            logger.info(
                                "claude-agent-sdk: masked %s on requested "
                                "interrupt (interrupt honored, not a "
                                "failure): %s", subtype, err_text,
                            )
                        else:
                            out["error"] = err_text
                    elif subtype not in ("", "success"):
                        # e.g. error_max_turns / error_max_budget_usd — surface
                        # honestly; the partial transcript is still projected.
                        out["error"] = f"SDK turn ended: {subtype}"
                    break
        finally:
            self._turn_inbox = None
            # Anything the reader parked after our ResultMessage belongs to a
            # CLI-initiated turn that overlapped ours. Route it now — left in
            # a discarded queue it would be lost, and left in the stream it
            # would become the next turn's answer. EXCEPT a residue result
            # that repeats THIS turn's own answer: delivering it through the
            # background lane re-presents the agent's own text as a fake
            # completion (the 2026-08-06 echo class) — suppress it, dedup-mark
            # it, and consume its burst buffer. Genuinely different residue is
            # a real background completion and still delivers.
            final_norm = " ".join(str(out["final_text"]).split())
            while not inbox.empty():
                residue = inbox.get_nowait()
                if final_norm and type(residue).__name__ == "ResultMessage":
                    res_text = getattr(residue, "result", None)
                    if (
                        isinstance(res_text, str)
                        and " ".join(res_text.split()) == final_norm
                    ):
                        uuid = getattr(residue, "uuid", None)
                        if uuid:
                            self._unsolicited_delivered.add(uuid)
                        self._unsolicited_results += 1
                        buffered = len(self._unsolicited_text)
                        self._unsolicited_text.clear()
                        logger.warning(
                            "claude-agent-sdk: residue ResultMessage %s "
                            "matches this turn's own answer — suppressed, "
                            "never delivered as a background result "
                            "(%d buffered text(s) consumed)",
                            uuid, buffered,
                        )
                        continue
                self._handle_unsolicited(residue)
        return out

    # ---------- stream ownership ----------

    async def _reader_loop(self) -> None:
        """Sole owner of the SDK message stream for the client's lifetime.

        Hermes used to call `receive_response()` once per turn. That helper
        returns the OLDEST buffered ResultMessage — not necessarily this
        turn's — and the SDK offers no per-turn correlator (`query()` takes
        only a `session_id`, which is constant across turns; `ResultMessage`
        has a `uuid` with no request-side counterpart). Ordering is therefore
        the ONLY thing keeping answers matched to questions.

        The Claude Code CLI breaks that ordering by itself: when a background
        Agent task completes it injects a `<task-notification>` and runs a
        FULL assistant turn nobody asked for, leaving an unconsumed
        ResultMessage in the shared FIFO. Every later turn then pops a stale
        result. Because a desynced turn still looks like success — no error,
        no timeout, no interrupt — no retire path fires, so the skew is
        permanent and silent. Observed live 2026-07-25: four such turns left a
        constant off-by-4 for hours (dasbrow-hermes-coder#2).

        One reader for the whole client lifetime makes the rule enforceable:
        a message arriving while no turn is in flight is unsolicited BY
        DEFINITION, and gets routed away instead of poisoning the next turn."""
        end: _StreamEnd
        try:
            async for message in self._client.receive_messages():
                inbox = self._turn_inbox
                if inbox is not None:
                    inbox.put_nowait(message)
                else:
                    self._handle_unsolicited(message)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # pragma: no cover - stream torn down
            logger.debug("claude-agent-sdk reader loop ended", exc_info=True)
            end = _StreamEnd(error=repr(exc))
        else:
            end = _StreamEnd(error=None)
        # The stream is gone (CLI exited or transport died). Mark it for any
        # future turn and wake the in-flight one, so nobody waits out a full
        # turn_timeout against a dead stream.
        self._stream_ended = end
        inbox = self._turn_inbox
        if inbox is not None:
            inbox.put_nowait(end)

    def _handle_unsolicited(self, message: Any) -> None:
        """Route a message that arrived with no turn in flight.

        These are real CLI output (typically a finished background Agent task
        reporting in). They answer nothing Hermes asked, so they must never
        enter a turn's result — but their CONTENT is completed work the user
        is waiting on: with a delivery callback wired, capture each top-level
        assistant message's text and hand the FULL burst over as an ordered
        list on the terminal ResultMessage (uuid-deduped). Without a
        callback, the historical WARN-drop stands."""
        if isinstance(message, _StreamEnd):
            return
        sid = getattr(message, "session_id", None)
        if sid:
            self._session_id = sid
        name = type(message).__name__
        if name == "ResultMessage":
            self._unsolicited_results += 1
            if self._on_unsolicited_result is None:
                self._unsolicited_text.clear()
                logger.warning(
                    "claude-agent-sdk: dropped unsolicited ResultMessage (no "
                    "turn in flight, total=%d) — CLI-initiated turn; see "
                    "dasbrow-hermes-coder#2",
                    self._unsolicited_results,
                )
                return
            uuid = getattr(message, "uuid", None)
            if uuid and uuid in self._unsolicited_delivered:
                self._unsolicited_text.clear()
                logger.debug(
                    "claude-agent-sdk: duplicate unsolicited ResultMessage "
                    "%s ignored", uuid,
                )
                return
            result_text = getattr(message, "result", None)
            texts = list(self._unsolicited_text)
            self._unsolicited_text.clear()
            if isinstance(result_text, str) and result_text.strip():
                # The CLI's result text repeats the turn's final assistant
                # message — never hand the same text over twice.
                if not texts or texts[-1] != result_text:
                    texts.append(result_text)
            if uuid:
                self._unsolicited_delivered.add(uuid)
            if not texts:
                logger.warning(
                    "claude-agent-sdk: unsolicited ResultMessage carried no "
                    "text (total=%d) — nothing to deliver",
                    self._unsolicited_results,
                )
                return
            logger.info(
                "claude-agent-sdk: delivering unsolicited result burst "
                "(background task finished, total=%d, %d message(s), "
                "%d chars)",
                self._unsolicited_results, len(texts),
                sum(len(t) for t in texts),
            )
            try:
                self._on_unsolicited_result(texts)
            except Exception:
                logger.warning(
                    "claude-agent-sdk: unsolicited-result delivery callback "
                    "raised — answer may be lost", exc_info=True,
                )
        elif name == "AssistantMessage":
            # Buffer top-level text, one entry per message so the burst
            # delivers in message granularity; subagent streams
            # (parent_tool_use_id set) are noise — the same gate
            # _forward_stream_delta uses.
            if (
                self._on_unsolicited_result is not None
                and not getattr(message, "parent_tool_use_id", None)
            ):
                parts = [
                    getattr(block, "text", "") or ""
                    for block in getattr(message, "content", None) or []
                    if type(block).__name__ == "TextBlock"
                ]
                message_text = "\n".join(p for p in parts if p)
                if message_text:
                    self._unsolicited_text.append(message_text)
            logger.info(
                "claude-agent-sdk: unsolicited %s outside a turn", name,
            )
        else:
            logger.debug(
                "claude-agent-sdk: unsolicited %s outside a turn", name,
            )

    def _start_reader(self) -> None:
        """Spawn the reader on the session loop. Idempotent."""
        if self._reader_task is not None:
            return

        async def _spawn() -> Any:
            return asyncio.ensure_future(self._reader_loop())

        self._reader_task = self._run_coro(_spawn(), timeout=10.0)

    def _stop_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(task.cancel)
        except Exception:  # pragma: no cover - loop already gone
            pass

    def _forward_stream_delta(self, message: Any) -> None:
        """Relay a top-level text delta to the display callback (never the
        transcript). Subagent streams (parent_tool_use_id set) stay quiet."""
        if self._on_stream_delta is None:
            return
        if getattr(message, "parent_tool_use_id", None):
            return
        event = getattr(message, "event", None) or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text")
        if not text:
            return
        try:
            self._on_stream_delta(text)
        except Exception:  # pragma: no cover - display callback
            logger.debug("stream delta callback raised", exc_info=True)

    def _notify_tool_started(self, message: Any) -> None:
        """Bridge ToolUseBlocks to Hermes tool-progress (gateway breadcrumbs),
        mirroring codex_runtime._codex_note_to_tool_progress (#38835)."""
        if self._on_tool_started is None:
            return
        if type(message).__name__ != "AssistantMessage":
            return
        for block in getattr(message, "content", None) or []:
            if type(block).__name__ != "ToolUseBlock":
                continue
            name = getattr(block, "name", "") or "unknown"
            args = getattr(block, "input", None) or {}
            if not isinstance(args, dict):
                args = {"input": args}
            preview = _tool_preview(name, args)
            try:
                self._on_tool_started(name, preview, args)
            except Exception:  # pragma: no cover - display callback
                logger.debug("tool-progress callback raised", exc_info=True)

    def build_option_fields(self) -> dict[str, Any]:
        """The ClaudeAgentOptions field dict — plain data so tests can assert
        on it without importing the SDK."""
        mcp_servers: dict[str, Any] = {}
        if self._include_hermes_tools:
            mcp_servers["hermes-tools"] = _build_hermes_tools_mcp_config(
                hermes_session_id=self._hermes_session_id
            )

        system_prompt: Any = {"type": "preset", "preset": "claude_code"}
        if self._system_prompt_append:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_prompt_append,
            }

        can_use_tool = None
        if (
            self._approval_callback is not None
            and self._permission_mode == "default"
        ):
            can_use_tool = self._make_can_use_tool()

        # Metered-vector scrub for the spawned CLI (see _METERED_ENV_DENYLIST).
        # agent.claude_agent_sdk.allow_metered_key: true is the operator's
        # explicit "bill me metered" opt-in (the same flag the startup guard
        # honors), so it disables the scrub too — otherwise the documented
        # escape hatch would hand the CLI an environment with the key blanked.
        env_overrides: dict[str, str] = {}
        if not _provider_flag("allow_metered_key"):
            env_overrides = _scrubbed_sdk_env()

        fields = {
            "model": self._model,
            "cwd": self._cwd,
            "permission_mode": self._permission_mode,
            "system_prompt": system_prompt,
            "mcp_servers": mcp_servers,
            "max_budget_usd": self._max_budget_usd,
            "can_use_tool": can_use_tool,
            "env": env_overrides,
            # Explicit SDK isolation. None (the SDK default, verified against
            # claude-agent-sdk 0.2.120) means the CLI loads ALL filesystem
            # settings — ~/.claude/settings.json, .claude/settings.json,
            # .claude/settings.local.json — so ambient operator/project
            # settings could re-permission tools or install hooks UNDERNEATH
            # the permission_mode/can_use_tool posture configured above. The
            # empty list is the SDK's documented isolation mode: settings
            # come from Hermes config only. Accepted side effect: "project"
            # is also what loads CLAUDE.md, which this runtime doesn't want —
            # it composes its own system-prompt append. Operators whose
            # deployment stores tool grants in ~/.claude/settings.json
            # (unattended cron turns with nobody to answer a prompt) opt
            # back in via agent.claude_agent_sdk.setting_sources — see
            # _configured_setting_sources.
            "setting_sources": _configured_setting_sources(),
            # Hermes has no AskUserQuestion answer channel: a tap approves the
            # tool but the chosen option never reaches the CLI, so the tool
            # dead-ends with "The user did not answer the questions." The model
            # must ask in plain text (Telegram-compatible); full option-button
            # mapping is a later feature.
            "disallowed_tools": ["AskUserQuestion"],
        }
        if self._resume_session_id:
            fields["resume"] = self._resume_session_id
        # Default OFF (upstream-conservative): partial messages only when the
        # operator opts in via agent.claude_agent_sdk.streaming in config.yaml.
        # Reads the __init__ snapshot so option and quiet-watchdog semantics
        # can never diverge across a mid-session config edit.
        if self._streaming:
            fields["include_partial_messages"] = True
        return fields

    def _build_client(self) -> Any:
        fields = self.build_option_fields()
        if self._client_factory is not None:
            return self._client_factory(options=fields)
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        return ClaudeSDKClient(options=ClaudeAgentOptions(**fields))

    def _make_can_use_tool(self) -> Any:
        """Bridge SDK permission requests onto Hermes' approval callback.
        Fail-closed: any callback failure denies.

        Silent-deny observability (P2.d): every deny that was NOT an
        operator's choice is logged at INFO here — this is the choke point
        every SDK-lane deny transits with tool name and honest reason in
        hand (the 2026-08-06 incident's silent denies had no log line at
        all). "denied by user" (with or without ": <text>") appears IFF a
        human chose deny — W8/W11 reserved that wording — so the prefix is
        the discriminator; operator denies are not silent and not logged
        here. Honesty boundary: settings deny-rule hits on the SDK side
        (the CLI consulting ~/.claude/settings.json deny rules, e.g. an
        installer-only skills-dir rule) never invoke can_use_tool and never
        transit hermes code — they are UNLOGGABLE here by construction;
        operator-facing relief for that class is a separate deny-notice
        feature decision."""
        approval_callback = self._approval_callback
        hermes_session_id = self._hermes_session_id

        async def _can_use_tool(tool_name: str, tool_input: dict, context: Any):
            from claude_agent_sdk import (
                PermissionResultAllow,
                PermissionResultDeny,
            )

            # Capture the watch OBJECT at entry: an approval that outlives
            # its turn (orphaned wait, self-expiring at approvals.timeout)
            # must decrement the watch it suspended, never a later turn's.
            # None = no Hermes turn in flight (unsolicited CLI-side turn) —
            # nothing to suspend.
            watch = self._turn_watch
            if watch is not None:
                # A human is being asked — their think time is not the
                # turn's silence. Both watchdog rules stand down until the
                # tap (or the approval machinery's own timeout) resolves.
                watch.approval_begin()
            try:
                return await self._resolve_can_use_tool(
                    tool_name, tool_input, context,
                    approval_callback, hermes_session_id,
                    PermissionResultAllow, PermissionResultDeny,
                )
            finally:
                if watch is not None:
                    watch.approval_end()

        return _can_use_tool

    async def _resolve_can_use_tool(
        self,
        tool_name: str,
        tool_input: dict,
        context: Any,
        approval_callback: Any,
        hermes_session_id: Any,
        PermissionResultAllow: Any,
        PermissionResultDeny: Any,
    ) -> Any:
        try:
            kwargs: dict = {"allow_permanent": False}
            # tool_use_id correlation (P2.a): the SDK guarantees a
            # non-empty context.tool_use_id — thread it through so a
            # button tap resolves THIS prompt, not queue[0]. Opt-in via
            # marker attribute: the CLI thread-local callback keeps its
            # exact signature (same additive philosophy as the widened
            # return channel).
            if getattr(approval_callback, "_accepts_tool_use_id", False):
                kwargs["tool_use_id"] = (
                    getattr(context, "tool_use_id", "") or ""
                )
            result = await asyncio.to_thread(
                approval_callback,
                f"{tool_name}({_tool_preview(tool_name, tool_input)})",
                f"Claude requests tool {tool_name}",
                **kwargs,
            )
        except Exception:
            logger.exception("approval_callback raised on SDK permission")
            logger.info(
                "claude-agent-sdk: silent deny (no operator choice): "
                "tool=%s reason=%s session=%s",
                tool_name, "approval callback failed", hermes_session_id,
            )
            return PermissionResultDeny(message="approval callback failed")
        # Widened callback contract: a plain choice string, or a dict
        # {"choice": str, "reason": str} carrying an honest deny reason
        # (no-approver / timeout / notify-failure / teardown-expiry).
        # "denied by user" is reserved for a real human deny — a
        # reason-bearing deny must never be attributed to the user.
        reason = None
        choice = result
        if isinstance(result, dict):
            reason = result.get("reason")
            choice = result.get("choice")
        if choice in ("once", "session", "always"):
            return PermissionResultAllow()
        if choice == "timeout" and not reason:
            # The CLI thread-local callback surfaces its prompt timeout
            # as a bare string; mapping it to the user-deny default
            # would fabricate attribution for a prompt nobody answered.
            reason = "approval timed out — no operator response"
        message = reason or "denied by user"
        if not message.startswith("denied by user"):
            logger.info(
                "claude-agent-sdk: silent deny (no operator choice): "
                "tool=%s reason=%s session=%s",
                tool_name, message, hermes_session_id,
            )
        return PermissionResultDeny(message=message)

    # ---------- loop-thread plumbing ----------

    def _start_loop_thread(self) -> None:
        if self._loop_thread is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run, name="claude-sdk-loop", daemon=True
        )
        thread.start()
        ready.wait(timeout=10)
        self._loop = loop
        self._loop_thread = thread

    def _stop_loop_thread(self) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # pragma: no cover
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None

    def _run_coro(self, coro: Any, *, timeout: float) -> Any:
        import concurrent.futures

        assert self._loop is not None, "loop thread not started"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            raise asyncio.TimeoutError(f"coroutine exceeded {timeout}s")


def _tool_preview(name: str, args: dict) -> str:
    """Short human preview of a tool call for progress breadcrumbs."""
    for key in ("command", "file_path", "path", "url", "query", "prompt"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return name


def _coerce_turn_input_text(user_input: Any) -> str:
    """Collapse Hermes/OpenAI rich content into plain text input (same
    contract as the codex session's _coerce_turn_input_text)."""
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        parts: list[str] = []
        for item in user_input:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item_type in {"image", "image_url", "input_image"}:
                parts.append("[image attached]")
        text = "\n\n".join(p for p in parts if p).strip()
        return text or "What do you see in this image?"
    return "" if user_input is None else str(user_input)
