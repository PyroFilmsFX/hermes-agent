"""P0 (2026-09-26): desktop/dashboard control-plane variables never reach an agent-reachable child.

Every Claude SDK agent Hermes spawned inherited HERMES_DASHBOARD_SESSION_TOKEN (the desktop's
gateway credential: prompt.submit / approval.respond on ANY session, i.e. forged owner turns
and approvals) and HERMES_DESKTOP_CDP_PORT (the renderer's remote-debugging port). The contract
pinned here, per spawn path: with those variables set in the parent, the child env built by
that path does not carry them. One central deny list
(``tools.environments.local_env_policy.DESKTOP_CONTROL_PLANE_ENV_KEYS`` /
``is_desktop_control_plane_env`` / ``scrub_desktop_control_plane_env``) is the only policy;
``test_every_os_environ_copy_is_scrubbed`` fails when a new env-copy site bypasses it.

Fake values only — never the host's real token.
"""

from __future__ import annotations

import ast
import os
import warnings
from pathlib import Path

import pytest

TOKEN = "HERMES_DASHBOARD_SESSION_TOKEN"
CDP = "HERMES_DESKTOP_CDP_PORT"
REMOTE = "HERMES_DESKTOP_REMOTE_TOKEN"
# A future control-plane name nobody enumerated yet: caught by shape, not by list.
SHAPED = "HERMES_DESKTOP_FUTURE_DEBUG_TOKEN"
CONTROL_KEYS = (TOKEN, CDP, REMOTE, SHAPED)
# What the backend pops from its own os.environ (auth-provider secrets stay: plugins read them).
SEALED_KEYS = (TOKEN, CDP, REMOTE)
FAKE_TOKEN = "fake-desktop-session-token-for-tests"

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def parent_env(monkeypatch):
    """The desktop backend's environment as observed on the host."""
    monkeypatch.setenv(TOKEN, FAKE_TOKEN)
    monkeypatch.setenv(CDP, "9223")
    monkeypatch.setenv(REMOTE, "fake-remote-token")
    monkeypatch.setenv(SHAPED, "fake-shaped")
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    return monkeypatch


def _assert_clean(env, *, allow_blank: bool = False, keys=CONTROL_KEYS):
    folded = {str(k).upper(): v for k, v in env.items()}
    for key in keys:
        if allow_blank:
            assert not folded.get(key), f"{key} reached the child with a value"
        else:
            assert key not in folded, f"{key} reached the child env"
    assert FAKE_TOKEN not in {str(v) for v in env.values()}


# --- the central policy -------------------------------------------------------------------


def test_central_deny_list_names_the_control_plane_and_feeds_tier1():
    from tools.environments.local_env_policy import (
        _ALWAYS_STRIP_KEYS, DESKTOP_CONTROL_PLANE_ENV_KEYS, is_desktop_control_plane_env)

    assert {TOKEN, CDP, REMOTE} <= DESKTOP_CONTROL_PLANE_ENV_KEYS
    # Tier 1 is stripped even under inherit_credentials=True — the control plane rides it.
    assert DESKTOP_CONTROL_PLANE_ENV_KEYS <= _ALWAYS_STRIP_KEYS
    for name in (*CONTROL_KEYS, TOKEN.lower(), "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
                 "HERMES_DASHBOARD_OIDC_CLIENT_SECRET", "HERMES_DASHBOARD_DRAIN_SECRET"):
        assert is_desktop_control_plane_env(name), name
    # Non-credential desktop markers children legitimately read stay visible.
    for name in ("HERMES_DESKTOP", "HERMES_DESKTOP_TERMINAL", "HERMES_DESKTOP_DEV_SERVER",
                 "HERMES_HOME", "HERMES_SESSION_ID", "GH_TOKEN"):
        assert not is_desktop_control_plane_env(name), name


def test_scrub_helper_is_in_place_and_case_folded():
    from tools.environments.local_env_policy import scrub_desktop_control_plane_env

    env = {TOKEN: "a", CDP.lower(): "9223", "PATH": "/bin", "HERMES_DESKTOP": "1"}
    out = scrub_desktop_control_plane_env(env)
    assert out is env
    assert env == {"PATH": "/bin", "HERMES_DESKTOP": "1"}


def test_passthrough_cannot_register_control_plane_names():
    from tools import env_passthrough

    assert env_passthrough._is_hermes_provider_credential(CDP)
    assert env_passthrough._is_hermes_provider_credential(TOKEN)
    env_passthrough.clear_env_passthrough()
    try:
        env_passthrough.register_env_passthrough([CDP, TOKEN])
        assert not env_passthrough.is_env_passthrough(CDP)
        assert not env_passthrough.is_env_passthrough(TOKEN)
    finally:
        env_passthrough.clear_env_passthrough()


# --- shared builders (terminal tool, background/PTY, cron scripts, codex/copilot, ...) -------


@pytest.mark.parametrize("inherit", [False, True])
def test_hermes_subprocess_env(parent_env, inherit):
    from tools.environments.local import hermes_subprocess_env

    _assert_clean(hermes_subprocess_env(inherit_credentials=inherit))


def test_sanitize_subprocess_env_even_with_force_prefix(parent_env):
    from tools.environments.local import _sanitize_subprocess_env

    env = _sanitize_subprocess_env(
        dict(os.environ), {f"_HERMES_FORCE_{TOKEN}": FAKE_TOKEN, f"_HERMES_FORCE_{CDP}": "9223"})
    _assert_clean(env)


def test_terminal_tool_run_env(parent_env):
    """The terminal tool's shell env, including a terminal.env config entry naming the key."""
    from tools.environments.local import _make_run_env

    _assert_clean(_make_run_env({CDP: "9223", f"_HERMES_FORCE_{TOKEN}": FAKE_TOKEN}))


def test_build_subprocess_env_unscrubbed_mode_still_drops_control_plane(parent_env):
    from tools.environments.local import build_subprocess_env

    _assert_clean(build_subprocess_env(scrub_secrets=False))
    _assert_clean(build_subprocess_env(scrub_secrets=True))


def test_served_profile_child_env(parent_env):
    from tools.environments.local import served_profile_child_env

    _assert_clean(served_profile_child_env())


# --- code execution / sandbox ---------------------------------------------------------------


def test_execute_code_sandbox_env_even_if_passthrough_admits_everything(parent_env):
    from tools.code_execution_env import _scrub_child_env

    _assert_clean(_scrub_child_env(dict(os.environ), is_passthrough=lambda _k: True))


# --- MCP servers Hermes launches ------------------------------------------------------------


def test_mcp_stdio_server_env(parent_env):
    from tools.mcp_tool_config import _build_safe_env

    _assert_clean(_build_safe_env({CDP: "9223", TOKEN: FAKE_TOKEN}))


def test_sdk_hermes_tools_mcp_server_env(parent_env):
    from agent.transports.claude_agent_sdk_session_config import _build_hermes_tools_mcp_config

    _assert_clean(_build_hermes_tools_mcp_config(hermes_session_id=None)["env"])


# --- Claude SDK CLI children ----------------------------------------------------------------


def test_sdk_env_overrides_blank_control_plane_for_cli_child(parent_env):
    """The SDK spawns ``{**os.environ, **options.env}``: a key can only be neutralised by an
    override, so every present control-plane key must be overridden to ""."""
    from agent.transports.claude_agent_sdk_session_config import _sdk_env_overrides

    overrides = _sdk_env_overrides(metered_allowed=True, hermes_session_id="s1")
    for key in CONTROL_KEYS:
        assert overrides.get(key) == "", key
    _assert_clean({**os.environ, **overrides}, allow_blank=True)


def test_sdk_operator_env_cannot_reintroduce_control_plane(parent_env):
    import agent.transports.claude_agent_sdk_session_config as cfg

    parent_env.setattr(cfg, "_configured_sdk_env", lambda: {CDP: "9223", TOKEN: FAKE_TOKEN, "X": "1"})
    overrides = cfg._sdk_env_overrides(metered_allowed=False, hermes_session_id="s1")
    assert overrides["X"] == "1"
    _assert_clean({**os.environ, **overrides}, allow_blank=True)


# --- codex app-server transport -------------------------------------------------------------


def test_codex_app_server_spawn_env(parent_env):
    import subprocess

    from agent.transports import codex_app_server as cas

    captured = {}

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})
            self.stdin = self.stdout = self.stderr = None
            self.pid, self.returncode = 1, None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    parent_env.setattr(subprocess, "Popen", FakePopen)
    client = cas.CodexAppServerClient(codex_bin="codex", env={CDP: "9223"})
    client._closed = True
    _assert_clean(captured["env"])


# --- compute host (tui_gateway) and LSP servers ---------------------------------------------


def test_compute_host_child_env(parent_env):
    from tui_gateway.host_supervisor import compute_host_child_env

    _assert_clean(compute_host_child_env({CDP: "9223"}))


def test_lsp_server_child_env(parent_env):
    from agent.lsp.client import lsp_child_env

    _assert_clean(lsp_child_env({CDP: "9223"}))


# --- process-level seal: implicit inheritance (subprocess without env=) --------------------


def test_backend_seal_removes_control_plane_from_os_environ_but_keeps_auth(parent_env):
    from hermes_cli import process_identity

    process_identity.seal_desktop_control_plane_env()
    _assert_clean(dict(os.environ), keys=SEALED_KEYS)
    # The backend's own auth decisions still see the per-spawn credential.
    assert process_identity.desktop_session_token() == FAKE_TOKEN
    assert process_identity.is_desktop_owned_backend(argv=[]) is True


def test_start_server_seals_before_serving(parent_env):
    import hermes_cli.web_server as web_server
    from tests.hermes_cli.test_dashboard_auth_gate import _stub_uvicorn_run

    _stub_uvicorn_run(parent_env)
    parent_env.setattr(web_server, "_SESSION_TOKEN", FAKE_TOKEN)
    web_server.start_server(host="127.0.0.1", port=0, open_browser=False, headless=True)
    _assert_clean(dict(os.environ), keys=SEALED_KEYS)
    assert web_server._desktop_loopback_auth_exempt("127.0.0.1") is True


# --- guard: no os.environ copy bypasses the central scrub -----------------------------------

_SCAN_ROOTS = ("agent", "tools", "tui_gateway", "hermes_cli", "cron", "gateway")
# Builders whose output is final and already applies scrub_desktop_control_plane_env last.
# Each one is exercised behaviourally above; adding a name here without that is a review flag.
_SCRUBBING_BUILDERS = frozenset({
    "scrub_desktop_control_plane_env", "hermes_subprocess_env", "_sanitize_subprocess_env",
    "build_subprocess_env", "served_profile_child_env", "_make_run_env", "_finalize_child_env",
    "_scrub_child_env", "_build_safe_env", "_scrubbed_env", "compute_host_child_env", "lsp_child_env",
})
_PRAGMA = "control-plane-env:"


def _is_os_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id in {"os", "_os"})


def _mentions_os_environ(node) -> bool:
    return any(_is_os_environ(n) for n in ast.walk(node))


def _bare_environ(node) -> bool:
    """``os.environ`` itself handed over (``env=os.environ``, ``env=x or os.environ``, ...)."""
    if _is_os_environ(node):
        return True
    if isinstance(node, ast.IfExp):
        return _bare_environ(node.body) or _bare_environ(node.orelse)
    return isinstance(node, ast.BoolOp) and any(_bare_environ(v) for v in node.values)


def _env_copy_nodes(tree):
    """Expressions that materialise a copy of ``os.environ`` for a child (or hand it over)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "dict" and any(
                    _mentions_os_environ(a) for a in node.args):
                yield node
            elif isinstance(func, ast.Attribute) and func.attr == "copy" and _is_os_environ(func.value):
                yield node
            for kw in node.keywords:
                if kw.arg == "env" and _bare_environ(kw.value):
                    yield kw.value
        elif isinstance(node, ast.Dict):
            if any(k is None and _is_os_environ(v) for k, v in zip(node.keys, node.values)):
                yield node
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and (
                _is_os_environ(node.left) or _is_os_environ(node.right)):
            yield node


def _call_name(call: ast.Call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _violations():
    out = []
    for root in _SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "environ" not in src:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(src)
            lines = src.splitlines()
            parents = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            seen_lines = set()
            for node in _env_copy_nodes(tree):
                line = node.lineno
                if line in seen_lines:
                    continue
                window = " ".join(lines[max(0, line - 2):line])
                if _PRAGMA in window:
                    continue
                # Wrapped by a scrubbing call, or scrubbed later in the same function.
                ok = False
                cur, func = node, None
                while cur in parents:
                    cur = parents[cur]
                    if isinstance(cur, ast.Call) and _call_name(cur) in _SCRUBBING_BUILDERS:
                        ok = True
                        break
                    if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        func = cur
                        break
                if not ok and func is not None:
                    ok = any(isinstance(n, ast.Call) and _call_name(n) in _SCRUBBING_BUILDERS
                             and n.lineno > line for n in ast.walk(func))
                if not ok:
                    seen_lines.add(line)
                    out.append(f"{path.relative_to(REPO)}:{line}")
    return out


def test_every_os_environ_copy_is_scrubbed():
    """A new spawn path that copies ``os.environ`` must route it through the central scrub (or a
    builder that ends with it), or carry ``# control-plane-env: <reason>`` naming why the copy
    can never reach an agent-reachable child. Implicit inheritance (no ``env=``) is covered by
    the backend seal, pinned in ``test_start_server_seals_before_serving``."""
    violations = _violations()
    assert not violations, (
        "os.environ copies that bypass scrub_desktop_control_plane_env:\n  " + "\n  ".join(violations))


def test_guard_scanner_detects_a_bypass(tmp_path):
    """The guard itself must be able to fail."""
    bad = ast.parse(
        "import os, subprocess\n"
        "def spawn():\n"
        "    env = {**os.environ, 'X': '1'}\n"
        "    subprocess.Popen(['x'], env=env)\n"
        "def ok():\n"
        "    env = dict(os.environ)\n"
        "    return scrub_desktop_control_plane_env(env)\n"
        "def handover(env=None):\n"
        "    subprocess.run(['x'], env=env if env is not None else os.environ)\n")
    nodes = list(_env_copy_nodes(bad))
    assert {n.lineno for n in nodes} == {3, 6, 9}
