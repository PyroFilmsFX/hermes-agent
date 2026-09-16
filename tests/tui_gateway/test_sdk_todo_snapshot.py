"""Read-only Claude task-store snapshots projected to the TUI todo feed."""

import json
import os
import threading
from types import SimpleNamespace

import pytest

import tui_gateway.server as server


@pytest.fixture(autouse=True)
def _isolate_claude_child_env(monkeypatch):
    """Tests must not inherit the task vars of the Claude session running them."""
    for key in (
        "CLAUDE_CODE_ENABLE_TODO_TOOLS", "CLAUDE_CODE_TASK_LIST_ID",
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _task_root(tmp_path, list_id="hermes-session"):
    root = tmp_path / "claude" / "tasks" / list_id
    root.mkdir(parents=True)
    return root


def _sdk_session(agent=None):
    return {
        "agent": agent or SimpleNamespace(_claude_sdk_session=object()),
        "edit_snapshots": {},
        "tool_started_at": {},
        "tool_progress_mode": "off",
    }


def test_snapshot_normalizes_tasks_and_skips_partial_files(tmp_path):
    root = _task_root(tmp_path)
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Plan", "status": "pending"})
    )
    (root / "2.json").write_text(
        json.dumps({"id": "2", "content": "Build", "status": "in_progress"})
    )
    (root / "3.json").write_text(
        json.dumps({"id": "3", "description": "Done", "status": "completed"})
    )
    (root / "4.json").write_text(
        json.dumps({"id": "4", "subject": "Removed", "status": "deleted"})
    )
    (root / "partial.json").write_text('{"id": "partial"')

    state = server._sdk_task_snapshot(
        task_list_id="hermes-session", config_dir=str(tmp_path / "claude")
    )

    assert state == {
        "todos": [
            {"id": "1", "content": "Plan", "status": "pending"},
            {"id": "2", "content": "Build", "status": "in_progress"},
            {"id": "3", "content": "Done", "status": "completed"},
            {"id": "4", "content": "Removed", "status": "cancelled"},
        ],
        "revision": 1,
    }


@pytest.mark.parametrize("task_list_id", ["../outside", "absolute-placeholder"])
def test_snapshot_rejects_task_list_paths_outside_canonical_store(
    tmp_path, monkeypatch, caplog, task_list_id
):
    store = tmp_path / "claude"
    root = _task_root(tmp_path, "normal")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Outside", "status": "pending"})
    )
    if task_list_id == "absolute-placeholder":
        task_list_id = str(outside)
    events = []
    sid = "sdk-path-fence"
    monkeypatch.setitem(server._sessions, sid, _sdk_session())
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    assert server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id=task_list_id, config_dir=str(store)
    ) is None
    assert events == []
    assert sum("invalid Claude task list" in record.message for record in caplog.records) == 1
    assert root.is_dir()


@pytest.mark.parametrize("kind", ["list", "file"])
def test_snapshot_rejects_symlinked_task_reads(tmp_path, monkeypatch, kind):
    store = tmp_path / "claude"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Outside", "status": "pending"})
    )
    if kind == "list":
        (store / "tasks").mkdir(parents=True)
        (store / "tasks" / "linked").symlink_to(outside, target_is_directory=True)
        task_list_id = "linked"
    else:
        root = _task_root(tmp_path, "linked-file")
        (root / "1.json").symlink_to(outside / "1.json")
        task_list_id = "linked-file"
    sid = f"sdk-symlink-{kind}"
    events = []
    monkeypatch.setitem(server._sessions, sid, _sdk_session())
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    assert server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id=task_list_id, config_dir=str(store)
    ) is None
    assert events == []


def test_snapshot_rejects_list_replacement_after_validation_and_reads_regular_list(
    tmp_path, monkeypatch
):
    store = tmp_path / "claude"
    root = _task_root(tmp_path, "raced-list")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Inside", "status": "pending"})
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "1.json").write_text(
        json.dumps({"id": "outside", "subject": "Outside", "status": "pending"})
    )
    original_open = os.open
    replaced = False

    def replace_list_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and dir_fd is None and os.fspath(path) == os.fspath(root):
            root.rename(tmp_path / "raced-list-real")
            root.symlink_to(outside, target_is_directory=True)
            replaced = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    sid = "sdk-raced-list"
    events = []
    monkeypatch.setitem(server._sessions, sid, _sdk_session())
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    monkeypatch.setattr(os, "open", replace_list_before_open)

    assert server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id="raced-list", config_dir=str(store)
    ) is None, "a list-directory replacement after validation must not be read"
    assert events == []

    root.unlink()
    (tmp_path / "raced-list-real").rename(root)
    state = server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id="raced-list", config_dir=str(store)
    )
    assert state["todos"] == [
        {"id": "1", "content": "Inside", "status": "pending"}
    ]


def test_eager_resume_uses_the_attachment_hook_for_task_bootstrap(tmp_path, monkeypatch):
    import agent.transports.claude_agent_sdk_session_config as config

    monkeypatch.setattr(
        config, "_provider_flag", lambda name, default=False: name == "task_tools"
    )
    root = _task_root(tmp_path, "persisted-list")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Eager", "status": "pending"})
    )
    binding = (
        'hermes-sdk-resume-v1:{"cwd":"/workspace","id":"sdk-resume",'
        '"task_list":"persisted-list"}'
    )
    db = SimpleNamespace(
        get_session=lambda _key: {
            "claude_sdk_session_id": binding,
            "cwd": "/workspace",
        }
    )
    agent = SimpleNamespace(
        provider="claude-agent-sdk",
        session_id="stored-session",
        _session_db=db,
        _claude_sdk_session=None,
        _claude_sdk_task_store_root=str(tmp_path / "claude"),
    )
    events = []
    monkeypatch.setattr(server, "_hydrate_session_cwd", lambda *args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *args: None)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *args: False)
    monkeypatch.setattr(server, "_start_session_services", lambda *args: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *args: None)
    monkeypatch.setattr(server, "_session_info", lambda *args: {})
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    server._init_session(
        "eager-runtime", "stored-session", agent, [], cwd="/workspace",
        session_db=db, source="tui", resume_session_id="stored-session",
    )

    todo_events = [event for event in events if event[0] == "todo.updated"]
    assert todo_events == [
        ("todo.updated", "eager-runtime", {
            "todos": [{"id": "1", "content": "Eager", "status": "pending"}],
            "revision": 1,
        })
    ]
    assert agent._tui_gateway_runtime_sid == "eager-runtime"
    assert callable(agent._claude_sdk_todo_snapshot_bootstrap)


@pytest.mark.parametrize(
    ("source", "task_list_id"),
    [("configured", "yaml-list"), ("inherited", "env-list"), ("configured", "")],
)
def test_resume_bootstrap_uses_effective_child_task_list_override(
    tmp_path, monkeypatch, source, task_list_id
):
    import agent.transports.claude_agent_sdk_session_config as config

    monkeypatch.setattr(
        config, "_provider_flag", lambda name, default=False: name == "task_tools"
    )
    monkeypatch.setattr(
        config,
        "_configured_sdk_env",
        lambda: {"CLAUDE_CODE_TASK_LIST_ID": task_list_id}
        if source == "configured"
        else {},
    )
    if source == "inherited":
        monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", task_list_id)

    if task_list_id:
        root = _task_root(tmp_path, task_list_id)
        (root / "1.json").write_text(
            json.dumps({"id": "1", "subject": "Override", "status": "pending"})
        )
    binding = (
        'hermes-sdk-resume-v1:{"cwd":"/workspace","id":"sdk-resume",'
        '"task_list":"derived-list"}'
    )
    db = SimpleNamespace(
        get_session=lambda _key: {
            "claude_sdk_session_id": binding,
            "cwd": "/workspace",
        }
    )
    agent = SimpleNamespace(
        provider="claude-agent-sdk",
        session_id="stored-session",
        _session_db=db,
        _claude_sdk_session=None,
        _claude_sdk_task_store_root=str(tmp_path / "claude"),
    )
    current = {"agent": None, "resume_session_id": "stored-session", "cwd": "/workspace"}
    sid = f"runtime-override-{source}"
    events = []
    monkeypatch.setitem(server._sessions, sid, current)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    server._attach_built_agent(sid, current, agent)

    assert agent._claude_sdk_task_list_id == (task_list_id or None)
    if task_list_id:
        assert events[-1][2]["todos"] == [
            {"id": "1", "content": "Override", "status": "pending"}
        ]
    else:
        assert events == []


def test_successful_fallback_marks_bootstrap_complete_before_rotation(tmp_path, monkeypatch):
    import agent.claude_sdk_runtime_session as runtime_session
    import agent.transports.claude_agent_sdk_session as sdk_session_mod
    import agent.transports.claude_agent_sdk_session_config as config
    from agent.claude_sdk_runtime_continuity import (
        _persisted_sdk_session_id, rotate_claude_sdk_session,
    )

    monkeypatch.setattr(
        config, "_provider_flag", lambda name, default=False: name == "task_tools"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    agent = SimpleNamespace(
        provider="claude-agent-sdk", session_id="stored-session",
        _claude_sdk_session=None, _claude_sdk_task_list_id="persisted-list",
        _claude_sdk_task_store_root=str(tmp_path / "claude"),
        platform="tui", model="claude-opus-4-8", skip_context_files=True,
        _session_db=None,
    )
    current = {"agent": None, "resume_session_id": "stored-session", "cwd": "/workspace"}
    sid = "runtime-fallback"
    events = []
    monkeypatch.setitem(server._sessions, sid, current)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    server._attach_built_agent(sid, current, agent)
    assert events == []

    root = _task_root(tmp_path, "persisted-list")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Fallback", "status": "pending"})
    )

    monkeypatch.setattr(runtime_session, "_build_approval_callback", lambda _agent: None)
    monkeypatch.setattr(runtime_session, "_background_result_sink", lambda _agent: None)
    monkeypatch.setattr(runtime_session, "build_system_prompt_append", lambda **kwargs: "")
    monkeypatch.setattr(runtime_session, "_hybrid_bridge_enabled", lambda: False)
    monkeypatch.setattr(runtime_session, "_configured_max_budget_usd", lambda: None)

    class FakeSdkSession:
        def __init__(self, **kwargs):
            self._resume_session_id = kwargs.get("resume_session_id")
            self._session_id = "sdk-live"
            self._cwd = kwargs["cwd"]
            self._turn_inbox = None
            self._turn_claim_requested = False
            self._turn_callback_lock = threading.RLock()
            self._retiring = False
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(sdk_session_mod, "ClaudeAgentSdkSession", FakeSdkSession)
    runtime_session._create_session(
        agent, resume_id="sdk-live", on_interim_assistant=None, on_tool_iteration=None
    )
    rotate_claude_sdk_session(agent, "test rotation")
    runtime_session._create_session(
        agent, resume_id=_persisted_sdk_session_id(agent),
        on_interim_assistant=None, on_tool_iteration=None,
    )

    todo_events = [event for event in events if event[0] == "todo.updated"]
    assert len(todo_events) == 1
    assert agent._claude_sdk_todo_snapshot_bootstrapped is True


@pytest.mark.parametrize(
    "name",
    [
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
        "TodoWrite",
        "todo_list",
        "mcp__hermes-tools__TaskCreate",
        "mcp__hermes-hybrid__TaskUpdate",
    ],
)
def test_task_tool_identity_accepts_bare_and_namespaced_names(name):
    assert server._is_sdk_task_tool(name)


def test_resume_bootstrap_emits_exactly_one_snapshot(tmp_path, monkeypatch):
    root = _task_root(tmp_path, "hermes-resume")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Resume", "status": "pending"})
    )
    sid = "sdk-bootstrap"
    session = _sdk_session(SimpleNamespace(_claude_sdk_session=object()))
    events = []
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    result = server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id="hermes-resume", config_dir=str(tmp_path / "claude")
    )

    assert result["todos"] == [{"id": "1", "content": "Resume", "status": "pending"}]
    assert [event[0] for event in events] == ["todo.updated"]
    assert events[0][2] == result


def test_actual_resume_attach_bootstraps_snapshot_without_turn_or_helper(tmp_path, monkeypatch):
    root = _task_root(tmp_path, "persisted-list")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Hydrated", "status": "pending"})
    )
    sid = "runtime-resume"
    binding = (
        'hermes-sdk-resume-v1:{"cwd":"/workspace","id":"sdk-resume",'
        '"task_list":"persisted-list"}'
    )
    db = SimpleNamespace(
        get_session=lambda _key: {"claude_sdk_session_id": binding},
    )
    agent = SimpleNamespace(
        provider="claude-agent-sdk",
        session_id="stored-session",
        _session_db=db,
        _claude_sdk_session=None,
        _claude_sdk_task_store_root=str(tmp_path / "claude"),
    )
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "agent_build_lock": threading.Lock(),
        "agent_build_started": False,
        "history": [],
        "history_lock": threading.Lock(),
        "session_key": "stored-session",
        "resume_session_id": "stored-session",
        "profile_home": None,
        "source": "tui",
        "cwd": "/workspace",
    }
    events = []
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_await_resume_history", lambda *_: True)
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: agent)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *_: False)
    monkeypatch.setattr(server, "_announce_built_agent", lambda *_: None)
    monkeypatch.setattr(server, "_finish_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    server._start_agent_build(sid, session)
    thread = session["_agent_build_thread"]
    thread.join(timeout=2)

    todo_events = [event for event in events if event[0] == "todo.updated"]
    assert todo_events == [
        ("todo.updated", sid, {
            "todos": [{"id": "1", "content": "Hydrated", "status": "pending"}],
            "revision": 1,
        })
    ]


def test_missing_task_directory_emits_nothing(tmp_path, monkeypatch):
    sid = "sdk-missing"
    session = _sdk_session()
    events = []
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    assert server.bootstrap_sdk_todo_snapshot(
        sid, task_list_id="hermes-missing", config_dir=str(tmp_path / "claude")
    ) is None
    assert events == []


def test_sdk_task_completion_refreshes_snapshot_after_tool_complete(tmp_path, monkeypatch):
    root = _task_root(tmp_path, "hermes-activity")
    (root / "1.json").write_text(
        json.dumps({"id": "1", "subject": "Activity", "status": "completed"})
    )
    sid = "sdk-activity"
    agent = SimpleNamespace(
        _claude_sdk_session=object(),
        _claude_sdk_task_list_id="hermes-activity",
        _claude_sdk_task_config_dir=str(tmp_path / "claude"),
    )
    session = _sdk_session(agent)
    events = []
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(server, "_emit", lambda event, event_sid, payload=None: events.append((event, event_sid, payload)))

    server._on_tool_complete(sid, "call-1", "mcp__hermes-tools__TaskUpdate", {}, "ok")

    assert [event[0] for event in events] == ["tool.complete", "todo.updated"]
    assert events[-1][2]["todos"] == [
        {"id": "1", "content": "Activity", "status": "completed"}
    ]


def test_task_store_root_uses_child_env_and_cwd(tmp_path):
    from agent import claude_sdk_runtime_session as runtime_session

    child_cwd = tmp_path / "child-workspace"
    child_home = tmp_path / "child-home"
    child_cwd.mkdir()
    child_home.mkdir()
    resolve_root = getattr(
        runtime_session, "_resolve_sdk_task_store_root", lambda *_args, **_kwargs: None
    )

    assert resolve_root(str(child_cwd), {"HOME": str(child_home)}) == (child_home / ".claude").resolve()
    assert resolve_root(
        str(child_cwd), {"HOME": str(child_home), "CLAUDE_CONFIG_DIR": "relative-config"}
    ) == (child_cwd / "relative-config").resolve()


def test_missing_task_list_is_quiet_not_an_invalid_path(tmp_path, caplog):
    """A list the CLI has not created yet is normal: no event and no warning."""
    import logging

    from tui_gateway import tool_progress

    (tmp_path / "tasks").mkdir()
    with caplog.at_level(logging.WARNING, logger=tool_progress.logger.name):
        result = tool_progress._sdk_task_snapshot(
            task_list_id="hermes-not-created-yet", task_store_root=str(tmp_path)
        )
    assert result is None
    assert not [r for r in caplog.records if "invalid Claude task list" in r.getMessage()]
