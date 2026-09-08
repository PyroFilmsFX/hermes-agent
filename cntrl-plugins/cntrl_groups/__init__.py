"""cntrl_groups — Hermes plugin entry point.

Surfaces the group registry three ways without touching Hermes core:

  /groups                in-session slash command
  hermes groups ...      CLI subcommand
  on_session_start       optional auto-tag by git repo

All logic lives in ``groups.py`` so the CLI keeps working with no plugin
context (tests, a bare shell, another machine).
"""

from __future__ import annotations

import logging
import shlex
from typing import Any, Optional

log = logging.getLogger("plugins.cntrl_groups")

_cfg: dict = {"auto_group_by_repo": False, "auto_group_prefix": ""}


def _groups():
    """Import the CLI module by path so it works whether this plugin is
    installed as a package or symlinked into $HERMES_HOME/plugins."""
    try:
        from . import groups as mod  # packaged
        return mod
    except ImportError:
        import importlib.util
        import pathlib

        path = pathlib.Path(__file__).resolve().parent / "groups.py"
        spec = importlib.util.spec_from_file_location("cntrl_groups_cli", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod


def _run(argv: list[str]) -> str:
    """Run a groups subcommand and capture its output for a chat reply."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    mod = _groups()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(argv)
    except SystemExit as exc:  # argparse --help, or a loud registry failure
        code = int(exc.code or 0)
    text = (out.getvalue() + err.getvalue()).strip()
    if code and not text:
        text = f"groups: command failed ({code})"
    return text or "(no output)"


def _cmd_slash(raw_args: str) -> str:
    """``/groups [args]`` — bare call lists every group."""
    argv = shlex.split(raw_args or "") or ["ls"]
    return _run(argv)


def _auto_tag(session_id: str = "", **_kw: Any) -> None:
    """Tag a brand-new session into a group named for its git repo.

    Never overwrites an existing tag: a session the user placed by hand must
    stay where they put it. Fail-open — a grouping miss must never cost a
    session start."""
    if not _cfg.get("auto_group_by_repo") or not session_id:
        return
    try:
        mod = _groups()
        conn = mod.connect()
        row = conn.execute(
            "SELECT git_repo_root FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        root = (row["git_repo_root"] if row else "") or ""
        if not root:
            return
        existing = conn.execute(
            f"SELECT 1 FROM {mod.TABLE} WHERE session_id = ?", (session_id,)
        ).fetchone()
        if existing:
            return
        import os
        import time

        name = f"{_cfg.get('auto_group_prefix') or ''}{os.path.basename(root.rstrip('/')) or 'root'}"
        with conn:
            conn.execute(
                f"INSERT INTO {mod.TABLE}(session_id, group_name, added_at) VALUES(?,?,?)",
                (session_id, name, time.time()),
            )
        log.info("cntrl_groups: auto-tagged %s -> %s", session_id, name)
    except (Exception, SystemExit):
        # SystemExit is NOT an Exception: groups.connect() raises it on a
        # missing db, and an uncaught one here would abort the session start
        # this hook is only decorating. Caught explicitly so auto-tagging can
        # never cost a session.
        log.debug("cntrl_groups: auto-tag failed", exc_info=True)


def _setup_cli(parser) -> None:
    parser.add_argument("args", nargs="*", help="ls | tag | untag | resume | auto | path")


def _handle_cli(args) -> int:
    print(_run(list(getattr(args, "args", []) or ["ls"])))
    return 0


def register(ctx) -> None:
    global _cfg
    for key in list(_cfg):
        try:
            value = ctx.get_config(key, None)
        except Exception:
            value = None
        if value is not None:
            _cfg[key] = value

    ctx.register_command(
        "groups",
        _cmd_slash,
        description="Session groups: /groups [ls|tag|untag|resume|auto] ...",
        args_hint="[ls|tag <group> <id>|resume <group>]",
    )
    ctx.register_cli_command(
        "groups", "Session groups (list, tag, resume)", _setup_cli, _handle_cli,
        description="Tag sessions into named projects and resume by project.",
    )
    if _cfg.get("auto_group_by_repo"):
        ctx.register_hook("on_session_start", _auto_tag)
        log.info("cntrl_groups: registered (auto-group by repo ON)")
    else:
        log.info("cntrl_groups: registered (auto-group off)")
