from types import SimpleNamespace

from agent.transports.claude_agent_sdk_session_notify import ClaudeSdkNotifyMixin
from agent.transports.claude_agent_sdk_session_turn import ClaudeSdkTurnMixin
from tui_gateway import tool_progress


class AssistantMessage:
    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class TextBlock:
    def __init__(self, text):
        self.text = text


class StreamEvent:
    def __init__(self, text, parent_tool_use_id):
        self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
        self.parent_tool_use_id = parent_tool_use_id


class TaskStartedMessage:
    def __init__(self, task_id, description, tool_use_id="parent-tool", session_id="child-session"):
        self.task_id = task_id
        self.description = description
        self.tool_use_id = tool_use_id
        self.session_id = session_id


class TaskProgressMessage:
    def __init__(self, task_id, description, usage):
        self.task_id = task_id
        self.description = description
        self.usage = usage


class TaskNotificationMessage:
    def __init__(self, task_id, status):
        self.task_id = task_id
        self.status = status


class TaskUpdatedMessage:
    def __init__(self, task_id, status):
        self.task_id = task_id
        self.patch = SimpleNamespace(status=status)


def _session(events):
    session = object.__new__(ClaudeSdkNotifyMixin)
    session._on_subagent_event = lambda event, name, preview, args, **kw: events.append(
        (event, name, preview, args, kw)
    )
    session._on_tool_started = lambda *args: None
    session._on_tool_use = None
    session._on_tool_result = None
    session._open_tool_cards = {}
    return session


def _turn_session(events):
    class Session(ClaudeSdkTurnMixin, ClaudeSdkNotifyMixin):
        pass

    session = object.__new__(Session)
    session._on_subagent_event = lambda event, name, preview, args, **kw: events.append(
        (event, name, preview, args, kw)
    )
    session._on_tool_started = lambda *args: None
    session._on_tool_use = None
    session._on_tool_result = None
    session._open_tool_cards = {}
    session._session_id = None
    session._unsolicited_text = []
    session._unsolicited_results = 0
    session._unsolicited_delivered = set()
    session._on_unsolicited_result = None
    session._pending_rename_ack = None
    return session


def test_task_lifecycle_emits_one_complete_for_notification_then_update():
    events = []
    session = _session(events)
    session._notify_tool_use(
        AssistantMessage([
            ToolUseBlock("parent-tool", "Agent", {"description": "inspect files"})
        ])
    )
    session._notify_task_message(TaskStartedMessage("task-1", "inspect files"))
    session._notify_task_message(TaskProgressMessage("task-1", "halfway", {"input_tokens": 4}))
    session._notify_task_message(TaskNotificationMessage("task-1", "complete"))
    session._notify_task_message(TaskUpdatedMessage("task-1", "completed"))

    assert [event[0] for event in events] == [
        "subagent.start", "subagent.progress", "subagent.complete"
    ]
    assert events[0][4]["subagent_id"] == "task-1"
    assert events[0][4]["goal"] == "inspect files"
    assert events[1][4]["usage"] == {"input_tokens": 4}
    assert events[2][4]["status"] == "completed"


def test_child_tool_and_text_are_scoped_and_not_top_level_cards():
    events = []
    session = _session(events)
    session._notify_tool_use(
        AssistantMessage([
            ToolUseBlock("parent-tool", "Agent", {"subagent_type": "researcher"})
        ])
    )
    session._notify_task_message(TaskStartedMessage("task-1", "researcher"))
    session._notify_tool_started(
        AssistantMessage([
            TextBlock("child says hello"),
            ToolUseBlock("child-tool", "Bash", {"command": "pwd"}),
        ], parent_tool_use_id="parent-tool")
    )

    assert [event[0] for event in events] == [
        "subagent.start", "subagent.text", "subagent.tool"
    ]
    assert events[1][4]["subagent_id"] == "task-1"
    assert events[2][1] == "Bash"
    assert events[2][4]["parent_tool_id"] == "parent-tool"
    assert set(session._open_tool_cards) == {"parent-tool"}


def test_child_stream_text_uses_subagent_text_without_top_level_delta():
    events = []
    session = _session(events)
    session._notify_tool_use(
        AssistantMessage([
            ToolUseBlock("parent-tool", "Agent", {"description": "stream work"})
        ])
    )
    session._notify_task_message(TaskStartedMessage("task-1", "stream work"))
    session._forward_stream_delta(StreamEvent("partial child", "parent-tool"))
    assert [event[0] for event in events] == ["subagent.start", "subagent.text"]
    assert events[-1][2] == "partial child"


def test_child_completed_text_reconciles_streamed_deltas():
    events = []
    session = _session(events)
    session._notify_tool_use(
        AssistantMessage([
            ToolUseBlock("parent-tool", "Agent", {"description": "stream work"})
        ])
    )
    session._notify_task_message(TaskStartedMessage("task-1", "stream work"))
    session._forward_stream_delta(StreamEvent("hel", "parent-tool"))
    session._forward_stream_delta(StreamEvent("lo", "parent-tool"))
    session._notify_child_text(
        AssistantMessage([TextBlock("hello")], parent_tool_use_id="parent-tool")
    )

    assert [event[2] for event in events if event[0] == "subagent.text"] == ["hel", "lo"]


def test_foreground_task_update_after_interrupt_still_finalizes_once():
    from tests.agent.claude_sdk_fakes import ResultMessage, _make_session

    class TaskStartedMessage:
        task_id = "task-interrupted"
        description = "background work"
        tool_use_id = "parent-tool"
        task_type = "local_agent"

    class TaskUpdatedMessage:
        task_id = "task-interrupted"
        patch = type("Patch", (), {"status": "killed"})()

    events = []
    session, _holder = _make_session(
        script=[TaskStartedMessage(), TaskUpdatedMessage(), ResultMessage(result="stopped")],
        on_unsolicited_result=None,
    )
    session._on_subagent_event = lambda event, name, preview, args, **kw: (
        events.append((event, kw)),
        session._interrupt_event.set() if event == "subagent.start" else None,
    )
    try:
        turn = session.run_turn("start")
    finally:
        session.close()

    assert [event for event, _kw in events].count("subagent.complete") == 1
    assert events[-1][1]["status"] == "interrupted"
    assert session._sdk_task_records == {}


def test_unsolicited_task_messages_use_the_same_lifecycle_path():
    events = []
    session = _turn_session(events)
    session._notify_tool_use(
        AssistantMessage([
            ToolUseBlock("parent-tool", "Agent", {"description": "background work"})
        ])
    )
    session._handle_unsolicited(TaskStartedMessage("task-bg", "background work"))
    session._handle_unsolicited(TaskNotificationMessage("task-bg", "stop"))
    assert [event[0] for event in events] == ["subagent.start", "subagent.complete"]
    assert events[-1][4]["status"] == "interrupted"


def test_scoped_tool_started_breadcrumb_is_not_dropped(monkeypatch):
    emitted = []
    monkeypatch.setattr(tool_progress, "_progress_subagent", lambda sid, name, preview, kw, event: emitted.append((event, sid, kw)))

    tool_progress._on_tool_progress(
        "parent", "tool.started", "Bash", "pwd", None,
        subagent_id="task-1", goal="inspect files", parent_tool_id="parent-tool",
    )
    assert emitted == [("subagent.tool", "parent", {"subagent_id": "task-1", "goal": "inspect files", "parent_tool_id": "parent-tool"})]


def test_a_started_subagent_carries_its_real_identity_not_the_parent_session():
    """The panel showed a raw Agent("…") and claimed the PARENT session uuid was the child's."""
    events = []
    session = _session(events)
    session._sdk_agent_tool_uses = {"parent-tool": {
        "goal": "review the diff",
        "args": {"subagent_type": "conductor:codex-worker", "description": "review the diff",
                 "model": "sonnet", "prompt": "..."},
    }}
    session._notify_task_message(TaskStartedMessage(
        "task-9", "review the diff", tool_use_id="parent-tool", session_id="parent-session-uuid"))

    (event, _name, _preview, _args, kwargs), = events
    assert event == "subagent.start"
    meta = kwargs["subagent_meta"]
    assert meta["subagent_type"] == "conductor:codex-worker"
    assert meta["display_name"] == "conductor:codex-worker"
    # The Task call ASKED for sonnet; a relay wrapper may drive something else entirely, so this is
    # recorded as the request, never as the model that ran.
    assert meta["requested_model"] == "sonnet"
    assert "model" not in meta
    # The ids that join this row to the on-disk child transcript.
    assert meta["sdk_agent_id"] == "task-9"
    assert meta["sdk_parent_session_id"] == "parent-session-uuid"
    # ...and that parent uuid is NOT passed off as an openable child session.
    assert kwargs.get("child_session_id") is None


def test_a_subagent_without_declared_type_still_reports_its_join_ids():
    events = []
    session = _session(events)
    session._sdk_agent_tool_uses = {"parent-tool": {"goal": "do the thing", "args": {}}}
    session._notify_task_message(TaskStartedMessage(
        "task-3", "do the thing", tool_use_id="parent-tool", session_id="parent-uuid"))

    (_event, _name, _preview, _args, kwargs), = events
    meta = kwargs["subagent_meta"]
    assert meta["sdk_agent_id"] == "task-3"
    assert meta["description"] == "do the thing"
    assert "subagent_type" not in meta
