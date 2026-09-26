"""A Claude peer name must have exactly one live registrant (unit #24b).

Incident 2026-09-25: an orphaned CLI spawned by a shutting-down backend
registered the SAME ``--name`` as the live session's CLI, so peers' native
ListAgents/SendMessage saw two processes under one name and the orphan sent a
duplicate report. The Claude CLI's own peer registry (``~/.claude/sessions/
<pid>.json``) is keyed by pid and does not enforce name uniqueness, so Hermes
holds a per-profile name lease and fences stale holders before a new spawn.

Fakes only: liveness, the fence, and processes are injected; no real CLI.
"""

from __future__ import annotations

import json
import time

import pytest

from agent.transports import claude_sdk_peer_name_lease as L
from tests.agent.claude_sdk_fakes import _FakeClient, isolate_provider_config


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


class _World:
    """Fake process table + recording fence."""

    def __init__(self) -> None:
        self.dead: set[int] = set()
        self.fenced: list[tuple[str, dict | None]] = []

    def liveness(self, pid, start) -> bool:
        return int(pid) not in self.dead

    def fence(self, name, record) -> None:
        self.fenced.append((name, dict(record) if record else None))


def _owner(pid=100, session="sess-a", generation="gen-1", start=1.0):
    return L.LeaseOwner(pid=pid, start=start, session_id=session, generation=generation)


@pytest.fixture
def world(tmp_path):
    w = _World()
    w.home = tmp_path
    return w


def _claim(world, name, owner):
    return L.claim(name, owner, home=world.home, liveness=world.liveness, fence=world.fence)


# ---------- two spawns, one holder ----------


def test_two_live_owners_only_one_holds_the_name(world):
    a = _owner(pid=100, session="sess-a")
    b = _owner(pid=200, session="sess-b", generation="gen-9")

    assert _claim(world, "hermes:worker", a) is True
    assert _claim(world, "hermes:worker", b) is False

    holder = L.holder("hermes:worker", home=world.home)
    assert holder["session_id"] == "sess-a" and holder["pid"] == 100
    assert [rec for _name, rec in world.fenced if rec] == [], "a live holder must never be fenced"
    assert len(world.fenced) == 1, "a refused claim must not sweep anything"


def test_same_session_in_another_live_backend_is_refused(world):
    """Two live backends hosting one Hermes session must not both register it."""
    assert _claim(world, "hermes:worker", _owner(pid=100, session="s")) is True
    assert _claim(world, "hermes:worker", _owner(pid=300, session="s", generation="g2")) is False


def test_same_process_rotation_supersedes_its_own_older_generation(world):
    assert _claim(world, "hermes:worker", _owner(generation="gen-1")) is True
    assert _claim(world, "hermes:worker", _owner(generation="gen-2")) is True
    assert L.holder("hermes:worker", home=world.home)["generation"] == "gen-2"
    # The superseded generation's late release must not free the new holder.
    assert L.release("hermes:worker", _owner(generation="gen-1"), home=world.home) is False
    assert L.holder("hermes:worker", home=world.home)["generation"] == "gen-2"


# ---------- stale holders are taken over and fenced ----------


def test_dead_backend_holder_is_taken_over_and_its_orphan_cli_fenced(world):
    old = _owner(pid=100, session="s", generation="old")
    assert _claim(world, "hermes:worker", old) is True
    L.record_cli("hermes:worker", old, cli_pid=4242, cli_start=5.0, home=world.home)

    world.dead.add(100)  # the backend exited; its CLI child (4242) lives on as an orphan
    new = _owner(pid=200, session="s", generation="new")
    assert _claim(world, "hermes:worker", new) is True

    assert L.holder("hermes:worker", home=world.home)["generation"] == "new"
    fenced = [rec for name, rec in world.fenced if rec]
    assert fenced and fenced[0]["cli_pid"] == 4242, world.fenced


def test_free_name_claim_still_sweeps_unleased_orphans(world):
    """An orphan spawned before the lease recorded it is swept by the fence."""
    assert _claim(world, "hermes:worker", _owner()) is True
    assert ("hermes:worker", None) in world.fenced


def test_corrupt_lease_is_treated_as_free(world):
    path = L.lease_path("hermes:worker", home=world.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert _claim(world, "hermes:worker", _owner()) is True


# ---------- release ----------


def test_release_on_close_frees_the_name(world):
    a = _owner(pid=100, session="a")
    b = _owner(pid=200, session="b", generation="gb")
    assert _claim(world, "hermes:worker", a) is True
    assert L.release("hermes:worker", b, home=world.home) is False  # not the holder
    assert _claim(world, "hermes:worker", b) is False
    assert L.release("hermes:worker", a, home=world.home) is True
    assert L.holder("hermes:worker", home=world.home) is None
    assert _claim(world, "hermes:worker", b) is True


# ---------- rename moves the lease atomically ----------


def test_rename_moves_the_lease_atomically(world):
    a = _owner()
    assert _claim(world, "hermes:old", a) is True
    assert L.move("hermes:old", "hermes:new", a, home=world.home,
                  liveness=world.liveness, fence=world.fence) is True
    assert L.holder("hermes:old", home=world.home) is None
    assert L.holder("hermes:new", home=world.home)["generation"] == a.generation


def test_rename_onto_a_live_foreign_name_keeps_the_old_lease(world):
    a = _owner(pid=100, session="a")
    other = _owner(pid=200, session="b", generation="gb")
    assert _claim(world, "hermes:old", a) is True
    assert _claim(world, "hermes:taken", other) is True
    assert L.move("hermes:old", "hermes:taken", a, home=world.home,
                  liveness=world.liveness, fence=world.fence) is False
    assert L.holder("hermes:old", home=world.home)["session_id"] == "a"
    assert L.holder("hermes:taken", home=world.home)["session_id"] == "b"


# ---------- default fence: CLI-registry orphan sweep ----------


class _Proc:
    def __init__(self, pid, *, ppid, create_time, name="claude", alive=True):
        self.pid = pid
        self._ppid = ppid
        self._create = create_time
        self._name = name
        self._alive = alive
        self.terminated = False
        self.killed = False

    def ppid(self):
        return self._ppid

    def create_time(self):
        return self._create

    def name(self):
        return self._name

    def cmdline(self):
        return [f"/Users/x/.local/bin/{self._name}", "--output-format", "stream-json"]

    def is_running(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _registry_entry(dir_, pid, name, started_s, entrypoint="sdk-py"):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "name": name, "entrypoint": entrypoint,
        "startedAt": int(started_s * 1000),
    }))


def test_default_fence_kills_only_orphaned_sdk_registrants_of_that_name(tmp_path, monkeypatch):
    now = time.time()
    sessions = tmp_path / "claude" / "sessions"
    procs = {
        11: _Proc(11, ppid=1, create_time=now),        # orphan, same name -> fence
        12: _Proc(12, ppid=999, create_time=now),      # live-parented -> keep
        13: _Proc(13, ppid=1, create_time=now),        # orphan, other name -> keep
        14: _Proc(14, ppid=1, create_time=now),        # interactive cli -> keep
        15: _Proc(15, ppid=1, create_time=now + 500),  # pid reused -> keep
        16: _Proc(16, ppid=1, create_time=now, name="Finder"),  # not claude -> keep
    }
    _registry_entry(sessions, 11, "hermes:w", now)
    _registry_entry(sessions, 12, "hermes:w", now)
    _registry_entry(sessions, 13, "hermes:x", now)
    _registry_entry(sessions, 14, "hermes:w", now, entrypoint="cli")
    _registry_entry(sessions, 15, "hermes:w", now)
    _registry_entry(sessions, 16, "hermes:w", now)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(L, "_process", lambda pid: procs.get(int(pid)))

    L.default_fence("hermes:w", None)

    assert procs[11].terminated
    assert not any(procs[p].terminated or procs[p].killed for p in (12, 13, 14, 15, 16))


def test_default_fence_kills_the_recorded_cli_of_a_dead_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    proc = _Proc(77, ppid=1, create_time=123.0)
    reused = _Proc(78, ppid=1, create_time=999.0)
    table = {77: proc, 78: reused}
    monkeypatch.setattr(L, "_process", lambda pid: table.get(int(pid)))

    L.default_fence("hermes:w", {"cli_pid": 77, "cli_start": 123.0})
    L.default_fence("hermes:w", {"cli_pid": 78, "cli_start": 123.0})

    assert proc.terminated
    assert not reused.terminated, "a reused pid must never be fenced"


# ---------- session integration ----------


def _session(name, sid, holder):
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    def factory(options=None):
        client = _FakeClient(options=options)
        holder.setdefault("clients", []).append(client)
        return client

    return ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory,
        session_name=name, hermes_session_id=sid,
    )


def _spawned_name(client):
    opts = client.options
    extra = (opts.get("extra_args") if isinstance(opts, dict) else opts.extra_args) or {}
    return extra.get("name")


def test_second_session_with_a_held_name_spawns_unnamed_until_release(monkeypatch):
    monkeypatch.setattr(L, "default_fence", lambda name, record: None)
    held: dict = {}
    first = _session("hermes:dup", "sess-1", held)
    second = _session("hermes:dup", "sess-2", held)
    third = None
    try:
        first.ensure_started()
        second.ensure_started()
        assert _spawned_name(held["clients"][0]) == "hermes:dup"
        assert _spawned_name(held["clients"][1]) is None, (
            "two live CLIs registered the same peer name"
        )
        assert L.holder("hermes:dup")["session_id"] == "sess-1"

        first.close()
        assert L.holder("hermes:dup") is None, "close() must release the name"
        third = _session("hermes:dup", "sess-3", held)
        third.ensure_started()
        assert _spawned_name(held["clients"][2]) == "hermes:dup"
    finally:
        for s in (first, second, third):
            if s is not None:
                s.close()


def test_live_rename_holds_both_names_until_the_cli_acknowledges(monkeypatch):
    from tests.agent.claude_sdk_fakes import AssistantMessage, ResultMessage, TextBlock
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    monkeypatch.setattr(L, "default_fence", lambda name, record: None)
    ack = "Session renamed to: hermes:after"
    clients = []

    def factory(options=None):
        client = _FakeClient(options=options, script=[
            AssistantMessage(content=[TextBlock(ack)]), ResultMessage(result=ack, uuid="u-ack"),
        ])
        clients.append(client)
        return client

    session = ClaudeAgentSdkSession(
        cwd="/tmp", model="claude-opus-4-8", client_factory=factory,
        session_name="hermes:before", hermes_session_id="sess-r",
    )
    import os
    # A live foreign backend (the test runner's parent), not a dead pid.
    squatter = L.LeaseOwner(pid=os.getppid(), start=None, session_id="other", generation="sq")
    try:
        session.ensure_started()
        assert L.holder("hermes:before")["session_id"] == "sess-r"
        assert session.rename("hermes:after") is True
        deadline = time.monotonic() + 3
        while L.holder("hermes:before") is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert L.holder("hermes:after")["session_id"] == "sess-r"
        assert L.holder("hermes:before") is None, "the old name must be freed once the CLI renamed"
        # A foreign live holder of the target name refuses the rename outright:
        # the CLI keeps its (still leased) name instead of duplicating another's.
        assert L.claim("hermes:taken", squatter, liveness=lambda p, s: True,
                       fence=lambda n, r: None) is True
        before = list(clients[0].queried)
        # True = nothing left for the caller to do: a rotation would only
        # rebuild the CLI unnamed, so the unique old name is kept instead.
        assert session.rename("hermes:taken") is True
        time.sleep(0.1)
        assert clients[0].queried == before, "a refused rename must not reach the CLI"
        assert session._session_name == "hermes:after"
        assert L.holder("hermes:after")["session_id"] == "sess-r"
    finally:
        session.close()
    assert L.holder("hermes:after") is None


def test_unnamed_spawn_may_still_rename_onto_a_free_name(monkeypatch):
    monkeypatch.setattr(L, "default_fence", lambda name, record: None)
    held: dict = {}
    first = _session("hermes:dup", "sess-1", held)
    second = _session("hermes:dup", "sess-2", held)
    try:
        first.ensure_started()
        second.ensure_started()
        assert second._peer_name_refused is True
        assert second._peer_lease_move("hermes:free") is True
        assert second._peer_name_refused is False
        assert L.holder("hermes:free")["session_id"] == "sess-2"
        assert L.holder("hermes:dup")["session_id"] == "sess-1"
    finally:
        second.close()
        first.close()
    assert L.holder("hermes:free") is None and L.holder("hermes:dup") is None
