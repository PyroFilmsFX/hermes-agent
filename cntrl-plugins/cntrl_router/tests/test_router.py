"""Router registry behaviour. The failure that matters here is a SILENT one:
a route that resolves to nothing, or a half-written registry, loses the work
without anybody seeing an error."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROUTER = str(Path(__file__).resolve().parents[1] / "router.py")


def run(env_path, *args):
    return subprocess.run(
        [sys.executable, ROUTER, *args],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CNTRL_ROUTER_ROUTES": str(env_path)},
    )


def test_path_honours_override(tmp_path):
    p = tmp_path / "r.json"
    assert run(p, "path").stdout.strip() == str(p)


def test_add_list_find_remove_roundtrip(tmp_path):
    p = tmp_path / "r.json"
    assert run(p, "add", "kanban", "--session", "hermes:kanban sync",
               "--owns", "board sync").returncode == 0
    out = run(p, "list").stdout
    assert "kanban" in out and "hermes:kanban sync" in out

    rows = json.loads(run(p, "list", "--json").stdout)
    assert rows == [{"key": "kanban", "session": "hermes:kanban sync", "owns": "board sync"}]

    # find matches on the key and on words from `owns`.
    assert json.loads(run(p, "find", "the kanban board is stuck").stdout)[0]["key"] == "kanban"
    assert run(p, "find", "board").returncode == 0

    # No match must EXIT 1, never print a wrong destination.
    r = run(p, "find", "totally unrelated")
    assert r.returncode == 1 and r.stdout.strip() == ""

    assert run(p, "remove", "kanban").returncode == 0
    assert json.loads(run(p, "list", "--json").stdout) == []
    assert run(p, "remove", "kanban").returncode == 1


def test_add_replaces_same_key_instead_of_duplicating(tmp_path):
    p = tmp_path / "r.json"
    run(p, "add", "k", "--session", "old")
    run(p, "add", "k", "--session", "new")
    rows = json.loads(run(p, "list", "--json").stdout)
    assert len(rows) == 1 and rows[0]["session"] == "new"


def test_missing_registry_is_empty_not_an_error(tmp_path):
    r = run(tmp_path / "nope.json", "list")
    assert r.returncode == 0 and "no routes yet" in r.stdout


def test_corrupt_registry_fails_loudly(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{not json", encoding="utf-8")
    r = run(p, "list")
    assert r.returncode != 0 and "cannot read" in r.stderr


def test_registry_shaped_wrong_fails_loudly(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('{"routes": "nope"}', encoding="utf-8")
    r = run(p, "list")
    assert r.returncode != 0 and "not a route registry" in r.stderr


def test_write_is_atomic_no_tmp_left_behind(tmp_path):
    p = tmp_path / "r.json"
    run(p, "add", "k", "--session", "s")
    assert p.exists()
    assert list(tmp_path.glob("*.tmp")) == []
