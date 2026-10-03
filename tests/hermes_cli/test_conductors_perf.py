"""Performance acceptance tests for Conductors page (#49 Unit T2)."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server
import hermes_cli.web_server_sessions as _web_server_sessions
from hermes_cli.web_routers import conductors, profiles as profiles_mod
from tui_gateway import conductor_roster, git_probe


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    temp_dir = (tmp_path / "temp").resolve()
    temp_dir.mkdir()

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_dir))
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: type("Pw", (), {"pw_dir": str(home)})())
    monkeypatch.setenv("HOME", str(home))

    state_dir = home / ".claude" / "state"
    state_dir.mkdir(parents=True)
    index_file = state_dir / "tb-marker-index.jsonl"
    monkeypatch.setenv("TB_MARKER_INDEX_PATH", str(index_file))

    mock_db = SimpleNamespace(list_sessions_rich=lambda limit=500: [], list_sessions=lambda: [], close=lambda: None)
    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile",
                        lambda profile, *, read_only=True: mock_db)
    monkeypatch.setattr(profiles_mod, "_profile_targets",
                        lambda label: [("default", home)])

    import tui_gateway.server as server
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")

    conductor_roster._clear_cache()
    conductors._clear_cache()

    yield SimpleNamespace(
        home=home,
        temp_dir=temp_dir,
        index_file=index_file,
        server=server,
        sessions=sessions,
    )

    conductor_roster._clear_cache()
    conductors._clear_cache()


def _make_marker_with_git(home: Path, proj_name: str, run_id: str = "run-1", session_id: str = "sid-1", **kwargs) -> tuple[Path, Path]:
    proj_dir = home / proj_name
    git_dir = proj_dir / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    state_dir = proj_dir / ".claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker_path = state_dir / "tb-build-active.json"
    data = {
        "schema": "tb-build/v1",
        "run_id": run_id,
        "session_id": session_id,
        "plan": f"{proj_name}/plan.md",
        "waves_total": 4,
        "waves_done": 1,
        "armed_at": "2026-09-29T08:00:00Z",
        "context_path": str(proj_dir),
        **kwargs,
    }
    marker_path.write_text(json.dumps(data), encoding="utf-8")

    # Seed git_probe cache so steady state requires 0 subprocess calls
    git_probe._cache._roots[str(proj_dir)] = str(proj_dir)
    git_probe._cache._roots[f"common:{proj_dir}"] = str(proj_dir)

    return proj_dir, marker_path


def _add_index_entry(index_file: Path, marker_path: Path, proj_dir: Path, run_id: str = "run-1", session_id: str = "sid-1"):
    entry = {
        "session_id": session_id,
        "marker_path": str(marker_path),
        "context_path": str(proj_dir),
        "state_root": str(marker_path.parent),
        "run_id": run_id,
    }
    with open(index_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_steady_state_spawns_no_subprocess(client, env, monkeypatch):
    """Steady state spawns no subprocess across 20 route calls."""
    proj_dir, m_path = _make_marker_with_git(env.home, "repo-steady", run_id="run-steady", session_id="sid-steady")
    _add_index_entry(env.index_file, m_path, proj_dir, run_id="run-steady", session_id="sid-steady")

    # Prime the first call
    resp_init = client.get("/api/profiles/conductors")
    assert resp_init.status_code == 200

    def forbidden(*args, **kwargs):
        raise AssertionError(f"Subprocess spawned in steady state: {args} {kwargs}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    # 20 steady state route calls must never spawn any subprocess
    for _ in range(20):
        resp = client.get("/api/profiles/conductors")
        assert resp.status_code == 200
        assert resp.json()["rows"][0]["build"]["run_id"] == "run-steady"


def test_concurrent_requests_trigger_single_flight_read(env, monkeypatch):
    """50 concurrent requests trigger exactly one underlying read (single-flight coalesced)."""
    proj_dir, m_path = _make_marker_with_git(env.home, "repo-concurrent", run_id="run-c", session_id="sid-c")
    _add_index_entry(env.index_file, m_path, proj_dir, run_id="run-c", session_id="sid-c")

    read_count = [0]
    orig_read_marker_index = conductor_roster.read_marker_index

    def counted_read(*args, **kwargs):
        read_count[0] += 1
        time.sleep(0.05)  # Simulate I/O latency so 50 requests arrive concurrently in flight
        return orig_read_marker_index(*args, **kwargs)

    monkeypatch.setattr(conductor_roster, "read_marker_index", counted_read)
    monkeypatch.setattr(conductors, "read_marker_index", counted_read)

    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False

    try:
        async def run_concurrent():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web_server.app),
                base_url="http://test",
                headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
            ) as ac:
                tasks = [ac.get("/api/profiles/conductors") for _ in range(50)]
                responses = await asyncio.gather(*tasks)
                assert all(r.status_code == 200 for r in responses)
                assert read_count[0] == 1

        asyncio.run(run_concurrent())
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.mark.allow_real_home_io
def test_200_row_marker_index_read_and_projected_under_budget(env):
    """A 200-row marker index is read and projected in < 250 ms."""
    lines = []
    # 20 distinct projects with 10 appended events each (realistic append-only marker index)
    for p in range(20):
        proj_dir, m_path = _make_marker_with_git(env.home, f"proj_{p}", run_id=f"run-{p}", session_id=f"sid-{p}")
        for version in range(10):
            entry = {
                "session_id": f"sid-{p}",
                "marker_path": str(m_path),
                "context_path": str(proj_dir),
                "state_root": str(m_path.parent),
                "run_id": f"run-{p}",
            }
            lines.append(json.dumps(entry))

    env.index_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Clear cache to measure a cold read and projection from disk
    conductor_roster._clear_cache()
    conductors._clear_cache()

    t0 = time.perf_counter()
    payload = conductors._build_conductors_payload()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(payload["rows"]) == 20

    # Mark timing assertion with a generous bound (250 ms budget)
    # The read and projection typically completes in ~60-120 ms on this machine.
    print(f"\n200-row read & projection took {elapsed_ms:.2f} ms")
    assert elapsed_ms < 250.0, f"Read and projection exceeded 250 ms budget: {elapsed_ms:.2f} ms"
