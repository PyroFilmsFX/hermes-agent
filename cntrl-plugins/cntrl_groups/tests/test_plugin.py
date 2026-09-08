"""Plugin surface: slash command, CLI command, and auto-tag on session start.

The failure that matters for auto-tag is OVERWRITING: a session the user
placed by hand must never be moved by the automation."""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
import cntrl_groups as plugin  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "state.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL,"
        " last_activity_at REAL, message_count INTEGER, git_branch TEXT, git_repo_root TEXT)"
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        [
            ("s1", "work", now, now, 1, "main", "/Users/j/Projects/hermes-cntrl"),
            ("s2", "other", now, now, 1, "main", ""),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CNTRL_GROUPS_DB", str(p))
    return p


class _Ctx:
    def __init__(self, **cfg):
        self.cfg, self.commands, self.cli, self.hooks = cfg, {}, {}, {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kw):
        self.cli[name] = handler_fn

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def test_register_exposes_slash_and_cli(db):
    ctx = _Ctx()
    plugin.register(ctx)
    assert "groups" in ctx.commands and "groups" in ctx.cli
    # Auto-tag is off by default, so no session hook is installed.
    assert "on_session_start" not in ctx.hooks


def test_slash_command_lists_and_tags(db):
    ctx = _Ctx()
    plugin.register(ctx)
    slash = ctx.commands["groups"]
    assert "no groups yet" in slash("")
    assert "-> fork" in slash("tag fork s1")
    assert "s1" in slash("ls fork")
    # Quoted group names survive shell-style splitting.
    assert "-> my project" in slash('tag "my project" s2')
    assert "my project" in slash("ls")


def test_slash_reports_failure_instead_of_silence(db):
    ctx = _Ctx()
    plugin.register(ctx)
    out = ctx.commands["groups"]("ls nope")
    assert "nope" in out and out != "(no output)"


def test_auto_tag_uses_repo_name_and_prefix(db):
    ctx = _Ctx(auto_group_by_repo=True, auto_group_prefix="repo:")
    plugin.register(ctx)
    assert "on_session_start" in ctx.hooks
    ctx.hooks["on_session_start"](session_id="s1")
    assert "repo:hermes-cntrl" in ctx.commands["groups"]("ls")


def test_auto_tag_never_overwrites_a_manual_tag(db):
    ctx = _Ctx(auto_group_by_repo=True)
    plugin.register(ctx)
    ctx.commands["groups"]("tag mine s1")
    ctx.hooks["on_session_start"](session_id="s1")
    assert "s1" in ctx.commands["groups"]("ls mine")
    assert "hermes-cntrl" not in ctx.commands["groups"]("ls")


def test_auto_tag_skips_sessions_without_a_repo(db):
    ctx = _Ctx(auto_group_by_repo=True)
    plugin.register(ctx)
    ctx.hooks["on_session_start"](session_id="s2")
    assert "no groups yet" in ctx.commands["groups"]("ls")


def test_auto_tag_is_fail_open(db, monkeypatch):
    ctx = _Ctx(auto_group_by_repo=True)
    plugin.register(ctx)
    monkeypatch.setenv("CNTRL_GROUPS_DB", "/nonexistent/state.db")
    # A grouping miss must never cost a session start.
    ctx.hooks["on_session_start"](session_id="s1")
