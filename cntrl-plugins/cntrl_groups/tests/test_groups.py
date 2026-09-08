"""Session-group behaviour. The failures that matter: a tag on a session that
does not exist (invisible forever), and a listing that silently returns
nothing when a group is real but empty."""

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

GROUPS = str(Path(__file__).resolve().parents[1] / "groups.py")


@pytest.fixture
def db(tmp_path):
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
            ("s-old", "older work", now - 900, now - 900, 4, "main", "/repo/a"),
            ("s-new", "newer work", now - 100, now - 100, 9, "cntrl-hermes", "/repo/a"),
            ("s-other", "elsewhere", now - 50, now - 50, 2, None, "/repo/b"),
        ],
    )
    conn.commit()
    conn.close()
    return p


def run(db, *args):
    return subprocess.run(
        [sys.executable, GROUPS, *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CNTRL_GROUPS_DB": str(db)},
    )


def test_tag_ls_resume_untag(db):
    assert run(db, "tag", "fork", "s-old", "s-new").returncode == 0

    out = run(db, "ls", "fork").stdout
    # Newest first, so `resume` and the eye agree.
    assert out.index("s-new") < out.index("s-old")
    assert "cntrl-hermes" in out and "newer work" in out

    assert run(db, "resume", "fork").stdout.strip() == "s-new"

    groups = run(db, "ls").stdout
    assert "fork" in groups and "2" in groups

    assert run(db, "untag", "s-old").returncode == 0
    assert "s-old" not in run(db, "ls", "fork").stdout


def test_tagging_unknown_session_is_refused(db):
    r = run(db, "tag", "fork", "s-new", "does-not-exist")
    assert r.returncode == 1 and "does-not-exist" in r.stderr
    # Nothing was written: a partial tag is worse than none.
    assert run(db, "ls", "fork").returncode == 1
    # --force is the documented escape hatch.
    assert run(db, "tag", "fork", "does-not-exist", "--force").returncode == 0


def test_retag_moves_between_groups(db):
    run(db, "tag", "a", "s-new")
    run(db, "tag", "b", "s-new")
    assert run(db, "ls", "a").returncode == 1
    assert "s-new" in run(db, "ls", "b").stdout


def test_empty_group_exits_nonzero(db):
    r = run(db, "ls", "ghost")
    assert r.returncode == 1 and "ghost" in r.stderr
    assert run(db, "resume", "ghost").returncode == 1


def test_auto_suggests_only_untagged_repos(db):
    out = run(db, "auto").stdout
    assert "/repo/a" in out          # two untagged sessions
    assert "/repo/b" not in out      # only one, below --min 2
    run(db, "tag", "fork", "s-old", "s-new")
    assert "/repo/a" not in run(db, "auto").stdout
    # auto never writes.
    assert run(db, "ls").stdout.count("fork") == 1


def test_missing_db_fails_loudly(tmp_path):
    r = run(tmp_path / "nope.db", "ls")
    assert r.returncode != 0 and "no Hermes state db" in r.stderr


def test_hermes_schema_is_untouched(db):
    run(db, "tag", "fork", "s-new")
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "group_name" not in cols and "cntrl_group" not in cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cntrl_session_groups" in tables
