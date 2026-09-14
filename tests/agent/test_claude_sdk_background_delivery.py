"""Background result delivery wiring — claude-agent-sdk runtime tests (#25267).

Split from ``tests/agent/test_claude_sdk_runtime.py``; the SDK message
stand-ins, fake clients and shared builders live in
``tests.agent.claude_sdk_fakes``.
"""


import pytest

from agent.claude_sdk_runtime import run_claude_agent_sdk_turn
from tests.agent.claude_sdk_fakes import (
    _make_turn,
    _make_agent,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    """Provider config, gateway contextvars and the CLI approval callback are
    reset around every test in this module — carried explicitly, never hoisted
    to a conftest (see ``isolate_provider_config``)."""
    yield from isolate_provider_config(monkeypatch)


class TestBackgroundDeliveryWiring:
    """Runtime glue: the session's delivery callback enqueues an
    sdk_background_result event for the gateway watcher's direct outbound
    send, config-gated."""

    def _spy_kwargs(self, monkeypatch):

        captured = {}

        class SpySession:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run_turn(self, user_input, **kw):
                return _make_turn()

            def close(self):
                pass

        monkeypatch.setattr(
            "agent.transports.claude_agent_sdk_session.ClaudeAgentSdkSession",
            SpySession,
        )
        return captured

    def test_flag_on_wires_callback_and_queue_event(self, monkeypatch):
        import hermes_cli.config as cfg
        from tools.process_registry import process_registry

        captured = self._spy_kwargs(monkeypatch)
        events = []

        class _FakeQueue:
            def put(self, evt):
                events.append(evt)

        monkeypatch.setattr(process_registry, "completion_queue", _FakeQueue())
        monkeypatch.setattr(
            "tools.approval_context.get_current_session_key", lambda: "gw-key-7"
        )
        monkeypatch.delenv("HERMES_CLAUDE_SDK_DELIVER_BACKGROUND", raising=False)
        # Opt-in flag (upstream-conservative default is OFF).
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {
                "agent": {"claude_agent_sdk": {"deliver_background_results": True}}
            },
            raising=False,
        )

        agent = _make_agent()
        agent._claude_sdk_session = None
        agent.session_id = "sess-bg-1"
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        callback = captured.get("on_unsolicited_result")
        assert callback is not None, "flag defaults ON — callback must be wired"
        callback(
            ["Research landed — writing up.", "background answer text"],
            [{"kind": "peer_in", "text": "incoming", "uuid": "peer-1"}],
        )
        assert len(events) == 1
        evt = events[0]
        # Direct-outbound event: the payload burst rides UNJOINED (each text
        # becomes its own outbound message) and no model-facing directive is
        # prepended — on a direct send it would leak to the user.
        assert evt["type"] == "sdk_background_result"
        assert evt["payloads"] == [
            "Research landed — writing up.", "background answer text",
        ]
        assert evt["items"] == [{"kind": "peer_in", "text": "incoming", "uuid": "peer-1"}]
        assert not any("[USER IS WAITING" in p for p in evt["payloads"])
        assert evt["session_key"] == "gw-key-7"
        assert evt["parent_session_id"] == "sess-bg-1"
        assert "delegation_id" not in evt

    def test_bg_parent_resolved_at_delivery_time_after_rotation(
        self, monkeypatch,
    ):
        # P0.g: the SDK session outlives hermes session rotations. The old
        # code snapshotted parent_session_id/session_key at SDK-session
        # CREATION, so a completion firing after rotation carried the dead
        # parent — the gateway classified it permanently gone and dropped
        # it. The callback must resolve the parent AT DELIVERY TIME, with
        # the creation-time snapshot only as a fallback for the SDK-loop
        # thread where the session-key contextvar is unset.
        import hermes_cli.config as cfg
        from tools.process_registry import process_registry

        captured = self._spy_kwargs(monkeypatch)
        events = []

        class _FakeQueue:
            def put(self, evt):
                events.append(evt)

        monkeypatch.setattr(process_registry, "completion_queue", _FakeQueue())
        monkeypatch.setattr(
            "tools.approval_context.get_current_session_key", lambda: "gw-key-7"
        )
        monkeypatch.delenv("HERMES_CLAUDE_SDK_DELIVER_BACKGROUND", raising=False)
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {
                "agent": {"claude_agent_sdk": {"deliver_background_results": True}}
            },
            raising=False,
        )

        agent = _make_agent()
        agent._claude_sdk_session = None
        agent.session_id = "sess-before"
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        callback = captured.get("on_unsolicited_result")
        assert callback is not None

        # Hermes rotates the session between turns; the completion fires on
        # the SDK loop thread where the contextvar reads empty.
        agent.session_id = "sess-after-rotation"
        monkeypatch.setattr(
            "tools.approval_context.get_current_session_key", lambda: ""
        )
        callback(["late background report"])
        assert len(events) == 1
        assert events[0]["parent_session_id"] == "sess-after-rotation"
        # Empty live key -> creation-time snapshot fallback keeps the route.
        assert events[0]["session_key"] == "gw-key-7"

        # A live, non-empty contextvar read wins over the snapshot.
        monkeypatch.setattr(
            "tools.approval_context.get_current_session_key", lambda: "gw-key-LIVE"
        )
        callback(["second late report"])
        assert len(events) == 2
        assert events[1]["session_key"] == "gw-key-LIVE"
        assert events[1]["parent_session_id"] == "sess-after-rotation"

    def test_flag_off_leaves_callback_unwired(self, monkeypatch):
        import hermes_cli.config as cfg

        captured = self._spy_kwargs(monkeypatch)
        monkeypatch.delenv("HERMES_CLAUDE_SDK_DELIVER_BACKGROUND", raising=False)
        monkeypatch.setattr(
            cfg,
            "load_config_readonly",
            lambda *a, **k: {
                "agent": {"claude_agent_sdk": {"deliver_background_results": False}}
            },
            raising=False,
        )
        agent = _make_agent()
        agent._claude_sdk_session = None
        run_claude_agent_sdk_turn(
            agent, user_message="hi", original_user_message="hi",
            messages=[{"role": "user", "content": "hi"}], effective_task_id="t",
        )
        assert captured.get("on_unsolicited_result") is None


def test_unsolicited_peer_turn_projects_ordered_items_once():
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        def __init__(self, tool_id, name, input):
            self.id, self.name, self.input = tool_id, name, input

    class ToolResultBlock:
        def __init__(self, tool_id, content, is_error=False):
            self.tool_use_id, self.content, self.is_error = tool_id, content, is_error

    class UserMessage:
        def __init__(self, content, *, uuid=None, origin=None):
            self.content, self.uuid, self.origin = content, uuid, origin
            self.parent_tool_use_id = None

    class AssistantMessage:
        def __init__(self, content, *, uuid=None):
            self.content, self.uuid = content, uuid
            self.parent_tool_use_id = None

    class ResultMessage:
        def __init__(self, result, uuid):
            self.result, self.uuid = result, uuid

    delivered = []
    session = ClaudeAgentSdkSession(
        cwd="/tmp", on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items))
    )
    session._handle_unsolicited(UserMessage(
        "ignored envelope text", uuid="peer-in-1",
        origin={
            "kind": "peer", "from": "peer-id", "name": "Peer Name",
            "fromSession": "peer-session", "body": "incoming body",
        },
    ))
    session._handle_unsolicited(AssistantMessage([
        ToolUseBlock("tool-1", "SendMessage", {"to": "peer-id", "message": "outgoing body"}),
    ]))
    session._handle_unsolicited(UserMessage([
        ToolResultBlock("tool-1", [{"type": "text", "text": "sent"}]),
    ], uuid="tool-result-1"))
    session._handle_unsolicited(AssistantMessage([TextBlock("final body")]))
    session._handle_unsolicited(ResultMessage("final body", "result-1"))
    session._handle_unsolicited(ResultMessage("final body", "result-1"))

    assert len(delivered) == 1
    texts, items = delivered[0]
    assert texts == ["final body"]
    assert items == [
        {
            "kind": "lifecycle", "event": "woken",
            "source": "peer", "by": "Peer Name", "uuid": "peer-in-1",
        },
        {
            "kind": "peer_in", "text": "incoming body", "from": "peer-id",
            "name": "Peer Name", "from_session": "peer-session", "uuid": "peer-in-1",
        },
        {
            "kind": "tool", "tool_use_id": "tool-1", "name": "SendMessage",
            "args": {"to": "peer-id", "message": "outgoing body"},
            "result": "sent", "is_error": False,
        },
        {"kind": "peer_out", "text": "outgoing body", "to": "peer-id", "tool_use_id": "tool-1"},
        {"kind": "text", "text": "final body"},
    ]


def test_terminal_result_text_is_trailing_item_when_it_differs_from_buffered_text():
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content, self.uuid, self.parent_tool_use_id = content, "assistant-2", None

    class ResultMessage:
        def __init__(self, result):
            self.result, self.uuid = result, "result-2"

    delivered = []
    session = ClaudeAgentSdkSession(
        cwd="/tmp", on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items))
    )
    session._handle_unsolicited(AssistantMessage([TextBlock("buffered answer")]))
    session._handle_unsolicited(ResultMessage("authoritative terminal answer"))

    assert delivered[0][0] == ["buffered answer", "authoritative terminal answer"]
    assert delivered[0][1][-1] == {"kind": "text", "text": "authoritative terminal answer"}


@pytest.mark.parametrize("terminal", ["a\nb", " a  \n b "])
def test_terminal_result_does_not_repeat_joined_text_blocks(terminal):
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession
    from tests.agent.claude_sdk_fakes import AssistantMessage, TextBlock, ResultMessage

    delivered = []
    session = ClaudeAgentSdkSession(
        cwd="/tmp", on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items))
    )
    session._handle_unsolicited(AssistantMessage(content=[TextBlock("a"), TextBlock("b")]))
    session._handle_unsolicited(ResultMessage(result=terminal))
    assert delivered[0][1] == [{"kind": "text", "text": "a"}, {"kind": "text", "text": "b"}]
    assert delivered[0][0] == ["a\nb"]


def test_scheduled_task_wake_is_the_first_unsolicited_item():
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class UserMessage:
        def __init__(self):
            self.content = "scheduled task details"
            self.uuid = "wake-1"
            self.origin = {
                "kind": "task-notification",
                "subkind": "scheduled-trigger",
                "description": "morning report",
            }
            self.parent_tool_use_id = None

    delivered = []
    session = ClaudeAgentSdkSession(
        cwd="/tmp", on_unsolicited_result=lambda texts, items=None: delivered.append(items)
    )
    session._handle_unsolicited(UserMessage())

    assert delivered == []
    assert session._unsolicited_items == [{
        "kind": "lifecycle", "event": "woken",
        "source": "task-notification/scheduled-trigger",
        "by": "morning report", "uuid": "wake-1",
    }]


def test_task_notification_without_subkind_wakes_the_burst():
    from agent.transports.claude_agent_sdk_session import ClaudeAgentSdkSession

    class UserMessage:
        content = "generic task notification"
        uuid = "wake-generic"
        origin = {"kind": "task-notification", "description": "a completed task"}
        parent_tool_use_id = None

    session = ClaudeAgentSdkSession(cwd="/tmp", on_unsolicited_result=lambda *_: None)
    session._handle_unsolicited(UserMessage())

    assert session._unsolicited_items[0]["source"] == "task-notification"


def test_suppressed_own_answer_clears_all_projection_buffers():
    from agent.transports.claude_agent_sdk_session_turn import _clear_unsolicited_projection

    session = type("Session", (), {
        "_unsolicited_text": ["answer"],
        "_unsolicited_items": [{"kind": "tool"}],
        "_unsolicited_tool_items": {"tool-1": {"kind": "tool"}},
        "_unsolicited_seen": {"stale-1"},
    })()
    _clear_unsolicited_projection(session)
    assert session._unsolicited_text == []
    assert session._unsolicited_items == []
    assert session._unsolicited_tool_items == {}
    assert session._unsolicited_seen == set()


def test_stream_end_does_not_duplicate_runtime_child_exit_emission():
    from agent.transports.claude_agent_sdk_session_turn import ClaudeSdkTurnMixin, _StreamEnd, _claim_child_exit_emission
    from types import SimpleNamespace

    events = []
    session = SimpleNamespace(
        _on_unsolicited_result=lambda texts, items: events.append((texts, items)),
        _unsolicited_stream_end_emitted=False,
    )
    ClaudeSdkTurnMixin._handle_unsolicited(session, _StreamEnd("eof"))
    assert events == []
    # The runtime's retirement path remains the sole claimant/emitter.
    assert _claim_child_exit_emission(session) is True


def test_stale_projection_discard_clears_seen_ids():
    from agent.transports.claude_agent_sdk_session_turn import _clear_unsolicited_projection

    session = type("Session", (), {
        "_unsolicited_text": [],
        "_unsolicited_items": [{"kind": "lifecycle", "event": "woken"}],
        "_unsolicited_tool_items": {"tool-1": {"kind": "tool"}},
        "_unsolicited_seen": {"wake-1", "tool-1"},
    })()
    _clear_unsolicited_projection(session)
    assert session._unsolicited_items == []
    assert session._unsolicited_tool_items == {}
    assert session._unsolicited_seen == set()
