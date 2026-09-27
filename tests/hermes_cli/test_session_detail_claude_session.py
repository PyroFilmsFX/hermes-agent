"""#60 T-6: ``GET /api/sessions/{id}`` exposes the LIVE Claude CLI session id as ``claude_session_id``.

Electron main signs ``targets[].claude_session_id`` from this route (``main.ts`` ``resolveSession``).
The hook path binds a grant to the CLI whose stdin carries that id, which a settings-env
``HERMES_SESSION_ID`` override can't forge. The id comes only from the in-process runtime's
connected CLI (``ClaudeAgentSdkSession.live_cli_session_id``), never from a state.db column (an agent
can write state.db, R1). ``claude_session_state`` says why the id is null so the dialog can say so.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_cli.web_server_sessions as _web_server_sessions
from hermes_cli.web_routers import sessions as sessions_router
from hermes_state import SessionDB


@pytest.fixture
def backend(monkeypatch, tmp_path):
    import tui_gateway.server as server

    path = tmp_path / "state.db"
    seed = SessionDB(path)
    for sid in ("worker-a", "worker-b"):
        seed.create_session(sid, "desktop")
    seed.close()
    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile",
                        lambda profile, *, read_only: SessionDB(path))
    monkeypatch.setattr(sessions_router, "_serving_profile", lambda profile: profile or "default")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    return SimpleNamespace(server=server, sessions=sessions, path=path)


def live_cli(backend, key: str, claude_id: str | None, *, connected: bool = True, closed: bool = False,
             profile_home: str | None = None, runtime_sid: str | None = None):
    """Register an in-process runtime for ``key`` whose SDK CLI announced ``claude_id``."""
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    cli = ClaudeAgentSdkSession(cwd="/tmp", hermes_session_id=key)
    cli._client = object() if connected else None
    cli._session_id = claude_id
    cli._closed = closed
    agent = SimpleNamespace(session_id=key, api_mode="claude_agent_sdk", _claude_sdk_session=cli)
    backend.sessions[runtime_sid or f"rt-{key}"] = {"session_key": key, "agent": agent, "profile_home": profile_home}
    return cli


async def detail(sid: str, profile: str | None = "default") -> dict:
    return await sessions_router.get_session_detail(sid, profile=profile)


@pytest.mark.asyncio
async def test_a_live_cli_exposes_its_claude_session_id(backend):
    live_cli(backend, "worker-a", "claude-a")
    row = await detail("worker-a")
    assert (row["claude_session_id"], row["claude_session_state"]) == ("claude-a", "live")


@pytest.mark.asyncio
async def test_each_session_gets_its_own_cli_id(backend):
    live_cli(backend, "worker-a", "claude-a")
    live_cli(backend, "worker-b", "claude-b")
    assert (await detail("worker-a"))["claude_session_id"] == "claude-a"
    assert (await detail("worker-b"))["claude_session_id"] == "claude-b"


@pytest.mark.asyncio
async def test_no_runtime_means_not_running_and_null(backend):
    row = await detail("worker-a")
    assert (row["claude_session_id"], row["claude_session_state"]) == (None, "not_running")


@pytest.mark.asyncio
async def test_a_cli_that_has_not_announced_its_id_is_starting_and_null(backend):
    live_cli(backend, "worker-a", None)
    row = await detail("worker-a")
    assert (row["claude_session_id"], row["claude_session_state"]) == (None, "starting")


@pytest.mark.asyncio
async def test_a_closed_or_disconnected_cli_never_exposes_a_stale_id(backend):
    live_cli(backend, "worker-a", "claude-old", closed=True)
    live_cli(backend, "worker-b", "claude-gone", connected=False)
    for sid in ("worker-a", "worker-b"):
        row = await detail(sid)
        assert row["claude_session_id"] is None, sid
        assert row["claude_session_state"] != "live", sid


@pytest.mark.asyncio
async def test_the_state_db_is_never_the_source(backend):
    """An agent can write state.db (R1): neither the persisted SDK resume binding nor a planted value
    becomes the signed id."""
    db = SessionDB(backend.path)
    db.update_claude_sdk_session_id("worker-a", 'hermes-sdk-resume-v1:{"cwd":"/tmp","id":"claude-planted"}')
    db.close()
    row = await detail("worker-a")
    assert row["claude_session_id"] is None and row["claude_session_state"] == "not_running"


@pytest.mark.asyncio
async def test_a_non_sdk_lane_has_no_claude_session(backend):
    backend.sessions["rt-x"] = {"session_key": "worker-a", "profile_home": None,
                                "agent": SimpleNamespace(session_id="worker-a", api_mode="codex_app_server")}
    row = await detail("worker-a")
    assert (row["claude_session_id"], row["claude_session_state"]) == (None, "not_running")


@pytest.mark.asyncio
async def test_a_same_id_runtime_in_another_profile_is_not_this_sessions_cli(backend, monkeypatch, tmp_path):
    """Timestamp ids can collide across profiles (#100029): only the runtime serving the requested
    profile answers."""
    other_home = tmp_path / "profiles" / "work"
    other_home.mkdir(parents=True)
    live_cli(backend, "worker-a", "claude-work", profile_home=str(other_home))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "profile_name_for_home",
                        lambda home: "work" if home and str(home) == str(other_home) else None)
    assert (await detail("worker-a", "default"))["claude_session_id"] is None
    assert (await detail("worker-a", "work"))["claude_session_id"] == "claude-work"


# ── #60 P2 (4d): main looks up source_session.role from the backend's stored row ──────────────


def _add(path, sid, role, content, **display):
    db = SessionDB(path)
    try:
        return db.append_message(sid, role, content, **display)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_message_role_is_answered_from_the_stored_row(backend):
    user_id = _add(backend.path, "worker-a", "user", "typed by the owner")
    asst_id = _add(backend.path, "worker-a", "assistant", "drafted by the agent")
    assert (await sessions_router.get_session_detail("worker-a", profile="default", message_id=str(asst_id)))[
        "message_role"] == "assistant"
    assert (await sessions_router.get_session_detail("worker-a", profile="default", message_id=str(user_id)))[
        "message_role"] == "user"


@pytest.mark.asyncio
async def test_a_peer_row_is_peer_and_foreign_or_bad_ids_are_null(backend):
    peer_id = _add(backend.path, "worker-a", "user", "from another session", display_kind="peer_message")
    other_id = _add(backend.path, "worker-b", "assistant", "someone else's row")
    tool_id = _add(backend.path, "worker-a", "tool", "tool output")
    row = await sessions_router.get_session_detail("worker-a", profile="default", message_id=str(peer_id))
    assert row["message_role"] == "peer"
    for bad in (str(other_id), str(tool_id), "999999", "1; drop table", "-1", ""):
        row = await sessions_router.get_session_detail("worker-a", profile="default", message_id=bad)
        assert row["message_role"] is None, bad


@pytest.mark.asyncio
async def test_no_message_id_means_no_message_role_key(backend):
    assert "message_role" not in await detail("worker-a")
