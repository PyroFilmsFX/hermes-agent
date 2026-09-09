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


def _path_with_home(home):
    return subprocess.run(
        [sys.executable, ROUTER, "path"], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(home)},
    ).stdout.strip()


def test_registry_is_install_wide_not_per_profile(tmp_path):
    """Each Hermes profile is its own HERMES_HOME. Keying the registry off it
    gave every profile a private EMPTY registry while routes added at the root
    stayed invisible — the host then reports 'no routes' and silently routes
    nothing. Found live 2026-09-08 by a Hermes session reading its own
    registry from profiles/thinkbot/."""
    root = tmp_path / "hermes"
    profile = root / "profiles" / "thinkbot"
    profile.mkdir(parents=True)

    assert _path_with_home(root) == str(root / "cntrl-routes.json")
    assert _path_with_home(profile) == str(root / "cntrl-routes.json")


def test_route_added_at_root_is_visible_from_a_profile(tmp_path):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)

    def run_home(home, *args):
        return subprocess.run(
            [sys.executable, ROUTER, *args], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(home)},
        )

    assert run_home(root, "add", "fork", "--session", "hermes:fork").returncode == 0
    assert "hermes:fork" in run_home(profile, "list").stdout
    # And the reverse: a profile write lands in the shared registry.
    run_home(profile, "add", "kanban", "--session", "hermes:kanban")
    assert "hermes:kanban" in run_home(root, "list").stdout


def test_explicit_override_still_wins_over_the_root_walk(tmp_path):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    override = tmp_path / "elsewhere.json"
    out = subprocess.run(
        [sys.executable, ROUTER, "path"], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(profile),
             "CNTRL_ROUTER_ROUTES": str(override)},
    ).stdout.strip()
    assert out == str(override)


def test_a_home_not_inside_profiles_is_used_as_is(tmp_path):
    plain = tmp_path / "dot-hermes"
    plain.mkdir()
    assert _path_with_home(plain) == str(plain / "cntrl-routes.json")
