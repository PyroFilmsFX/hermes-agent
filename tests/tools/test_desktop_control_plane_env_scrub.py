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

    from starlette.datastructures import State

    _stub_uvicorn_run(parent_env)
    parent_env.setattr(web_server, "_SESSION_TOKEN", FAKE_TOKEN)
    # start_server configures the process-global app.state (auth gate, host role, profiles...);
    # give it a throwaway copy so later tests in the same process see the original.
    parent_env.setattr(web_server.app, "state", State(dict(web_server.app.state._state)))
    web_server.start_server(host="127.0.0.1", port=0, open_browser=False, headless=True)
    _assert_clean(dict(os.environ), keys=SEALED_KEYS)
    assert web_server._desktop_loopback_auth_exempt("127.0.0.1") is True


# --- guard: no os.environ copy bypasses the central scrub -----------------------------------

# plugins/ and skills/ (incl. optional-skills/) ship Python the agent runs in-process or as
# a child of the backend: same contract as the core tree.
_SCAN_ROOTS = ("agent", "tools", "tui_gateway", "hermes_cli", "cron", "gateway",
               "plugins", "skills", "optional-skills")
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


def _target_key(node) -> str | None:
    """Stable identity of an assignment target / call argument (``env``, ``self._env``, ...)."""
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return ast.unparse(node)
    return None


def _scrubbed_keys_after(func, line: int) -> set[str]:
    """Targets a scrubbing builder is applied TO (as an argument) after *line* in *func*."""
    keys = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Call) and _call_name(n) in _SCRUBBING_BUILDERS and n.lineno > line:
            for arg in (*n.args, *(kw.value for kw in n.keywords)):
                key = _target_key(arg)
                if key:
                    keys.add(key)
    return keys


def _copy_is_scrubbed(node, parents) -> bool:
    """Dataflow proof that the scrub operates on THIS copy, not merely that one runs nearby.

    Accepted: (a) the copy is (inside) an argument of a scrubbing builder; (b) the copy is
    assigned to a target and a LATER scrubbing call in the same function takes that very
    target as an argument (``env = dict(os.environ); ...; scrub(env)``). A copy returned,
    handed to ``env=``, or bound to a name the scrub never receives is a violation."""
    cur, stmt = node, None
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Call) and _call_name(cur) in _SCRUBBING_BUILDERS:
            return True
        if isinstance(cur, ast.stmt):
            stmt = cur
            break
    if stmt is None:
        return False
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    keys = {k for k in (_target_key(t) for t in targets) if k}
    if not keys:
        return False
    func = stmt
    while func in parents and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        func = parents[func]
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return bool(keys & _scrubbed_keys_after(func, node.lineno))


def _scan_source(src: str) -> list[int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    lines = src.splitlines()
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    bad = []
    for node in _env_copy_nodes(tree):
        line = node.lineno
        if line in bad:
            continue
        window = " ".join(lines[max(0, line - 2):line])
        if _PRAGMA in window:
            continue
        if not _copy_is_scrubbed(node, parents):
            bad.append(line)
    return bad


def _violations():
    out = []
    for root in _SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO)
            if "tests" in rel.parts or path.name.startswith("test_"):
                continue  # a skill's own test suite never runs inside a Hermes process
            src = path.read_text(encoding="utf-8")
            if "environ" not in src:
                continue
            out.extend(f"{path.relative_to(REPO)}:{line}" for line in _scan_source(src))
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


def test_guard_requires_the_scrub_to_receive_the_spawned_env():
    """P2: a scrub of some OTHER object in the same function is not proof. Only a scrub that
    takes the copy (or the name it is bound to, after the copy) passes."""
    src = (
        "import os, subprocess\n"
        "def decoy():\n"
        "    env = dict(os.environ)\n"
        "    other = scrub_desktop_control_plane_env({})\n"
        "    subprocess.Popen(['x'], env=env)\n"
        "def wrong_name():\n"
        "    env = {**os.environ}\n"
        "    clean = dict(env)\n"
        "    scrub_desktop_control_plane_env(clean)\n"
        "    subprocess.Popen(['x'], env=env)\n"
        "def scrub_before_copy():\n"
        "    scrub_desktop_control_plane_env(env := {})\n"
        "    env = os.environ.copy()\n"
        "    subprocess.Popen(['x'], env=env)\n"
        "def good_in_place():\n"
        "    env = dict(os.environ)\n"
        "    env['X'] = '1'\n"
        "    scrub_desktop_control_plane_env(env)\n"
        "    subprocess.Popen(['x'], env=env)\n"
        "def good_wrapped():\n"
        "    return scrub_desktop_control_plane_env({**os.environ, 'X': '1'})\n"
        "def good_attr(self):\n"
        "    self.env = os.environ.copy()\n"
        "    self.env = hermes_subprocess_env(self.env)\n"
        "def returned_raw():\n"
        "    return dict(os.environ)\n")
    assert _scan_source(src) == [3, 7, 13, 26]


def test_guard_scans_plugins_and_skills():
    assert {"plugins", "skills", "optional-skills"} <= set(_SCAN_ROOTS)


# --- P1: the seal survives a late .env reload (tui_gateway.server import) -------------------


def _write_dotenv(home: Path, **values) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def test_dotenv_reload_after_seal_cannot_republish_control_plane(tmp_path, parent_env):
    """web_server seals at start_server, then imports tui_gateway.server, whose module-level
    load_hermes_dotenv re-read .env: a token persisted there came straight back."""
    from hermes_cli import process_identity
    from hermes_cli.env_loader import load_hermes_dotenv

    home = tmp_path / "home"
    _write_dotenv(home, **{TOKEN: "dotenv-token", CDP: "9333", SHAPED: "x",
                           "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "pw", "SOME_SETTING": "1"})
    process_identity.seal_desktop_control_plane_env()
    parent_env.delenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", raising=False)
    parent_env.delenv("SOME_SETTING", raising=False)
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    _assert_clean(dict(os.environ), keys=(TOKEN, CDP, REMOTE, SHAPED))
    assert "dotenv-token" not in os.environ.values()
    # Ordinary settings and the runtime-read auth-provider secret still load.
    assert os.environ.get("SOME_SETTING") == "1"
    assert os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD") == "pw"
    # The adopted/sealed token is not replaced by the .env value.
    assert process_identity.desktop_session_token() == FAKE_TOKEN


def test_dotenv_still_loads_control_plane_names_before_any_seal(tmp_path, monkeypatch):
    """Unsealed processes (a standalone CLI) keep the historical loader behaviour."""
    from hermes_cli.env_loader import load_hermes_dotenv

    home = tmp_path / "home"
    _write_dotenv(home, **{CDP: "9333"})
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    assert os.environ.get(CDP) == "9333"


def test_seal_does_not_let_a_dotenv_token_replace_the_adopted_one(parent_env):
    from hermes_cli import process_identity

    process_identity.adopt_desktop_session_token("file-token")
    assert process_identity.desktop_session_token() == "file-token"
    assert TOKEN not in os.environ


def test_skill_inline_shell_child_env_has_no_control_plane(tmp_path, parent_env):
    """agent/skill_preprocessing.run_inline_shell used env=delegated_child_subprocess_env(),
    which returned None (inherit) outside a delegated context."""
    import subprocess

    from agent import skill_preprocessing

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    parent_env.setattr(skill_preprocessing.subprocess, "run", fake_run)
    assert skill_preprocessing.run_inline_shell("echo ok", tmp_path, 5) == "ok"
    assert captured["env"] is not None, "inline shell inherits os.environ implicitly"
    _assert_clean(captured["env"])


def test_skill_inline_shell_real_child_sees_no_token(tmp_path, parent_env, real_bash):
    from agent import skill_preprocessing

    out = skill_preprocessing.run_inline_shell(
        f'printf "%s|%s" "${{{TOKEN}:-absent}}" "${{{CDP}:-absent}}"', tmp_path, 10)
    assert out == "absent|absent"


def test_delegated_child_subprocess_env_never_returns_none(parent_env):
    from agent.delegation_context import delegated_child_subprocess_env

    env = delegated_child_subprocess_env()
    assert isinstance(env, dict)
    _assert_clean(env)
    _assert_clean(delegated_child_subprocess_env({TOKEN: FAKE_TOKEN, "PATH": "/bin"}))


# --- Part 2: the local Desktop hands the token over a private one-shot FILE ------------------

FILE_TOKEN = "fake-file-handoff-token-0123456789abcdefABCDEF"


def _desktop_local_file(home: Path, *, token: str = FILE_TOKEN, root_mode=0o700, dir_mode=0o700,
                        file_mode=0o600) -> Path:
    root = home / ".hermes" / "desktop-local"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(root_mode)
    run = root / ("a" * 32)
    run.mkdir()
    run.chmod(dir_mode)
    path = run / ("b" * 16 + ".token")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    path.chmod(file_mode)
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "os-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX mode checks")


@posix_only
def test_session_token_file_is_read_once_and_deleted(fake_home):
    from hermes_cli.main_dashboard import _read_desktop_session_token_file

    path = _desktop_local_file(fake_home)
    assert _read_desktop_session_token_file(str(path)) == FILE_TOKEN
    assert not path.exists() and not path.parent.exists(), "one-shot file (and its dir) must be gone"
    with pytest.raises(SystemExit, match="not accessible"):
        _read_desktop_session_token_file(str(path))


@posix_only
@pytest.mark.parametrize("kwargs, message", [
    ({"file_mode": 0o644}, "unsafe permissions"),
    ({"dir_mode": 0o755}, "parent directory has unsafe permissions"),
    ({"root_mode": 0o755}, "runtime root has unsafe permissions"),
    ({"token": "short"}, "invalid token"),
    ({"token": "bad token with spaces " * 3}, "invalid token"),
])
def test_session_token_file_fails_closed_and_still_deletes(fake_home, kwargs, message):
    from hermes_cli.main_dashboard import _read_desktop_session_token_file

    path = _desktop_local_file(fake_home, **kwargs)
    with pytest.raises(SystemExit, match=message):
        _read_desktop_session_token_file(str(path))
    if "root_mode" not in kwargs:  # the root check fails before the file is opened
        assert not path.exists()


@posix_only
def test_session_token_file_rejects_paths_outside_the_private_root_and_symlinks(fake_home, tmp_path):
    from hermes_cli.main_dashboard import _read_desktop_session_token_file

    with pytest.raises(SystemExit, match="must be absolute"):
        _read_desktop_session_token_file("relative.token")
    with pytest.raises(SystemExit, match="under the desktop-local directory"):
        _read_desktop_session_token_file(str(tmp_path / "x.token"))
    with pytest.raises(SystemExit, match="invalid runtime path"):
        _read_desktop_session_token_file(str(fake_home / ".hermes" / "desktop-local" / "nothex" / "x.token"))
    real = _desktop_local_file(fake_home)
    link = real.parent / ("c" * 16 + ".token")
    link.symlink_to(real)
    with pytest.raises(SystemExit, match="symlink"):
        _read_desktop_session_token_file(str(link))
    assert real.exists(), "a symlink attempt must not consume the real file"


def test_serve_parser_accepts_session_token_file():
    from hermes_cli.subcommands.dashboard import build_serve_parser

    args = build_serve_parser(cmd_dashboard=lambda a: None).parse_args(
        ["--session-token-file", "/x/y.token", "--ssh-session-token-file", "/s"])
    assert args.desktop_session_token_file == "/x/y.token"
    assert args.ssh_session_token_file == "/s"


def test_desktop_owned_backend_accepts_the_token_file_flag(monkeypatch):
    from hermes_cli import process_identity

    monkeypatch.setenv("HERMES_DESKTOP", "1")
    assert process_identity.is_desktop_owned_backend(["serve", "--session-token-file", "/p"])
    assert process_identity.is_desktop_owned_backend(["serve", "--session-token-file=/p"])
    assert not process_identity.is_desktop_owned_backend(["serve"])
    monkeypatch.setenv("HERMES_DESKTOP", "0")
    assert not process_identity.is_desktop_owned_backend(["serve", "--session-token-file", "/p"])


@posix_only
def test_cmd_dashboard_adopts_and_deletes_the_file_before_anything_else(fake_home, monkeypatch):
    """The token is read, the file deleted, and the process sealed BEFORE the ownership checks
    and long before ``hermes_cli.web_server`` is imported; no token ever enters os.environ."""
    import hermes_cli.main as cli_main
    from hermes_cli import process_identity

    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.delenv(TOKEN, raising=False)
    path = _desktop_local_file(fake_home)
    seen = {}

    class _Stop(Exception):
        pass

    def _probe(_headless):
        seen["token"] = process_identity.desktop_session_token()
        seen["file_exists"] = path.exists()
        seen["env_has_token"] = TOKEN in os.environ
        seen["owned"] = process_identity.is_desktop_owned_backend([])
        raise _Stop

    monkeypatch.setattr(cli_main, "_dashboard_sanitize_desktop_env", _probe)
    from hermes_cli.subcommands.dashboard import build_serve_parser

    args = build_serve_parser(cmd_dashboard=cli_main.cmd_dashboard).parse_args(
        ["--host", "127.0.0.1", "--port", "0", "--session-token-file", str(path)])
    monkeypatch.setattr(cli_main, "_dashboard_validate_serve_args", lambda *a: None)
    with pytest.raises(_Stop):
        cli_main.cmd_dashboard(args)
    assert seen == {"token": FILE_TOKEN, "file_exists": False, "env_has_token": False, "owned": True}

    # The web server's module-level token resolution now yields the file token.
    import hermes_cli.web_server as ws
    assert ws._resolve_session_token() == FILE_TOKEN


@posix_only
def test_real_serve_adopts_the_file_token_and_never_serves_it(tmp_path):
    """End to end on a real ``hermes serve``: Electron's file handoff authenticates, the file is
    consumed, ``GET /`` carries no token, and nothing token-shaped is in the backend's env."""
    import re
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request

    home = tmp_path / "os-home"
    hermes_home = tmp_path / "hermes-home"
    home.mkdir()
    hermes_home.mkdir()
    path = _desktop_local_file(home)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_", "PYTEST_")) and k not in ("PYTHONPATH",)}
    env.update(HOME=str(home), HERMES_HOME=str(hermes_home), HERMES_DESKTOP="1",
               HERMES_GATEWAY_LOCK_DIR=str(tmp_path / "locks"), XDG_STATE_HOME=str(tmp_path / "state"),
               PYTHONPATH=str(REPO), HERMES_NONINTERACTIVE="1", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "serve", "--host", "127.0.0.1", "--port", "0",
         "--session-token-file", str(path)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        port, lines, deadline = None, [], time.monotonic() + 150
        while port is None and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            m = re.search(r"HERMES_BACKEND_READY port=(\d+)", line)
            if m:
                port = int(m.group(1))
        assert port, "backend never became ready:\n" + "".join(lines[-40:])
        assert not path.exists(), "the one-shot token file must be consumed before serving"

        base = f"http://127.0.0.1:{port}"
        root = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        assert FILE_TOKEN not in root and "__HERMES_SESSION_TOKEN__" not in root

        def status(headers):
            req = urllib.request.Request(base + "/api/sessions", headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=15).status
            except urllib.error.HTTPError as exc:
                return exc.code

        assert status({}) == 401
        assert status({"X-Hermes-Session-Token": "wrong-token"}) == 401
        assert status({"X-Hermes-Session-Token": FILE_TOKEN}) != 401, "the desktop must still authenticate"

        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            # The backend's own environment block carries neither the token nor its name.
            if sys.platform.startswith("linux"):
                block = Path(f"/proc/{proc.pid}/environ").read_bytes().decode(errors="replace")
            else:
                block = subprocess.run(["ps", "eww", "-p", str(proc.pid)], capture_output=True,
                                       text=True, check=False).stdout
            assert "HERMES_DESKTOP=1" in block, "env block not visible; the check would be vacuous"
            assert FILE_TOKEN not in block
            assert TOKEN not in block
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_backend_only_plugin_keys_stay_in_the_backend_but_never_reach_children():
    """Owner ruling 2026-09-26: HERMES_EMBED_API_KEY (the pgvector memory plugin's billable
    embedding key) is for the backend and its in-process plugins only; agent shells never get it."""
    from hermes_cli.control_plane_env import (
        is_process_sealed_control_plane_env, scrub_desktop_control_plane_env)

    env = {"HERMES_EMBED_API_KEY": "fake-embed-key", "PATH": "/bin"}
    scrub_desktop_control_plane_env(env)
    assert "HERMES_EMBED_API_KEY" not in env
    assert env["PATH"] == "/bin"
    # The in-process plugin reads it from the backend's os.environ at runtime: not sealed.
    assert not is_process_sealed_control_plane_env("HERMES_EMBED_API_KEY")
