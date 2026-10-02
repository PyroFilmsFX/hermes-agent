"""Tests for SDK AskUserQuestion elicitation bridge (M5).

Verifies that AskUserQuestion is unbanned and routes through Hermes elicitation
(desktop form or consent path) instead of being disallowed.
"""

import asyncio
from unittest.mock import patch, MagicMock
import pytest

from tests.agent.claude_sdk_fakes import (
    _make_session,
    _plant_claude_agent_sdk_stand_in,
    isolate_provider_config,
    ResultMessage,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


class TestClaudeSdkAskUserQuestionElicitation:
    """AskUserQuestion routes through Hermes elicitation and carries answers."""

    @pytest.fixture(autouse=True)
    def _sdk_permission_results(self, monkeypatch):
        _plant_claude_agent_sdk_stand_in(monkeypatch)

    def test_ask_user_question_not_in_disallowed_tools(self):
        """AskUserQuestion is no longer in disallowed_tools; native Read remains disallowed."""
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        fields = session.build_option_fields()
        assert "AskUserQuestion" not in fields["disallowed_tools"]
        assert fields["disallowed_tools"] == ["Read"]

    def test_desktop_accepted_answers_return_allow_with_updated_input(self, monkeypatch):
        """When desktop form accepts, updated_input carries the submitted answers."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        questions = [
            {
                "question": "Which database would you prefer?",
                "header": "Database Choice",
                "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}],
            },
            {
                "question": "Enable caching?",
                "options": [{"label": "Yes"}, {"label": "No"}],
            },
        ]
        tool_input = {"questions": questions}

        submitted_answers = {
            "Which database would you prefer?": "PostgreSQL",
            "Enable caching?": "Yes",
        }

        from tools.mcp_tool_sampling import PendingElicitation, _pending_elicitations, _pending_elicitations_lock
        from mcp.types import ElicitResult

        # Mock connected desktop client
        monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: True)

        async def _mock_handler_call(self, context, params):
            # Simulate desktop client responding with accept and answers
            return ElicitResult(action="accept", content=submitted_answers)

        monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _mock_handler_call)

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))
        assert type(result).__name__ == "PermissionResultAllow"
        assert result.updated_input is not None
        assert result.updated_input["questions"] == questions
        assert result.updated_input["answers"] == submitted_answers

    def test_desktop_decline_returns_deny(self, monkeypatch):
        """When user declines in desktop form, PermissionResultDeny is returned."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        tool_input = {
            "questions": [
                {"question": "Proceed with deployment?", "options": [{"label": "Yes"}, {"label": "No"}]}
            ]
        }

        from mcp.types import ElicitResult

        monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: True)

        async def _mock_handler_call(self, context, params):
            return ElicitResult(action="decline")

        monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _mock_handler_call)

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))
        assert type(result).__name__ == "PermissionResultDeny"
        assert "decline" in result.message or "denied by user" in result.message

    def test_timeout_returns_deny(self, monkeypatch):
        """When elicitation times out, PermissionResultDeny is returned without hanging."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        tool_input = {
            "questions": [
                {"question": "Confirm action?", "options": [{"label": "OK"}, {"label": "Cancel"}]}
            ]
        }

        async def _mock_timeout(self, context, params):
            raise asyncio.TimeoutError()

        monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _mock_timeout)

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))
        assert type(result).__name__ == "PermissionResultDeny"
        assert "timed out" in result.message

    def test_cancel_returns_deny(self, monkeypatch):
        """When elicitation returns cancel (e.g. gateway wait expired), PermissionResultDeny is returned."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        tool_input = {
            "questions": [
                {"question": "Confirm action?", "options": [{"label": "OK"}, {"label": "Cancel"}]}
            ]
        }

        from mcp.types import ElicitResult

        async def _mock_cancel(self, context, params):
            return ElicitResult(action="cancel")

        monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _mock_cancel)

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))
        assert type(result).__name__ == "PermissionResultDeny"
        assert "timed out" in result.message

    def test_no_desktop_routes_to_consent_path(self, monkeypatch):
        """When no desktop client is connected, request_elicitation_consent is used."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        questions = [
            {
                "question": "Which protocol?",
                "header": "Protocol Selection",
                "options": [{"label": "HTTP"}, {"label": "gRPC"}],
            }
        ]
        tool_input = {"questions": questions}

        # Ensure desktop is disconnected
        monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: False)

        consent_calls = []

        def _mock_consent(message, description, **kwargs):
            consent_calls.append((message, description, kwargs))
            return "accept"

        monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", _mock_consent)

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))

        assert len(consent_calls) == 1
        assert "Which protocol?" in consent_calls[0][0] or "Claude" in consent_calls[0][0]
        assert "Protocol Selection" in consent_calls[0][1] or "Which protocol?" in consent_calls[0][1]

        # The consent path can only say yes/no; it cannot carry an answer, so the tool call is
        # denied with the question named rather than guessing an option on the owner's behalf.
        assert type(result).__name__ == "PermissionResultDeny"
        assert "Which protocol?" in result.message

    def test_no_desktop_consent_declined(self, monkeypatch):
        """When consent is declined in CLI/gateway, PermissionResultDeny is returned."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        tool_input = {
            "questions": [{"question": "Delete temp files?", "options": [{"label": "Yes"}, {"label": "No"}]}]
        }

        monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: False)
        monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "decline")

        result = asyncio.run(can_use_tool("AskUserQuestion", tool_input, None))
        assert type(result).__name__ == "PermissionResultDeny"
        assert "decline" in result.message or "denied by user" in result.message

    def test_malformed_input_denies_cleanly(self):
        """Malformed questions input denies cleanly instead of raising or hanging."""
        session, _ = _make_session()
        can_use_tool = session._make_can_use_tool()

        # Missing questions
        res1 = asyncio.run(can_use_tool("AskUserQuestion", {}, None))
        assert type(res1).__name__ == "PermissionResultDeny"
        assert "malformed" in res1.message

        # Empty questions list
        res2 = asyncio.run(can_use_tool("AskUserQuestion", {"questions": []}, None))
        assert type(res2).__name__ == "PermissionResultDeny"
        assert "malformed" in res2.message

        # Question not a dict
        res3 = asyncio.run(can_use_tool("AskUserQuestion", {"questions": ["invalid"]}, None))
        assert type(res3).__name__ == "PermissionResultDeny"
        assert "malformed" in res3.message

        # Question missing question text
        res4 = asyncio.run(can_use_tool("AskUserQuestion", {"questions": [{"options": []}]}, None))
        assert type(res4).__name__ == "PermissionResultDeny"
        assert "malformed" in res4.message

    def test_other_tools_permission_behavior_unchanged(self):
        """Existing permission behavior for other tools (Read, MCP tools, Bash) is unchanged."""
        session, _ = _make_session(
            approval_callback=lambda *a, **k: "once",
            permission_mode="default",
        )
        can_use_tool = session._make_can_use_tool()

        # Native Read is disallowed
        res_read = asyncio.run(can_use_tool("Read", {"file_path": "/tmp/test.txt"}, None))
        assert type(res_read).__name__ == "PermissionResultDeny"
        assert "native SDK Read is disallowed" in res_read.message

        # Bounded MCP reader is auto-allowed
        res_mcp = asyncio.run(can_use_tool("mcp__hermes-tools__read_file", {"path": "/tmp/test.txt"}, None))
        assert type(res_mcp).__name__ == "PermissionResultAllow"

        # Bash runs through approval callback
        res_bash = asyncio.run(can_use_tool("Bash", {"command": "ls -la"}, None))
        assert type(res_bash).__name__ == "PermissionResultAllow"


def test_unanswered_desktop_question_denies_and_never_invents_an_answer(monkeypatch):
    session, _ = _make_session()
    can_use_tool = session._make_can_use_tool()
    questions = [
        {"question": "Pick a region", "options": [{"label": "ord"}, {"label": "iad"}]},
        {"question": "Enable caching?", "options": [{"label": "Yes"}, {"label": "No"}]},
    ]
    monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: True)

    async def _answer_only_first(self, context, params):
        from mcp.types import ElicitResult
        return ElicitResult(action="accept", content={"Pick a region": "iad"})

    monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _answer_only_first)
    result = asyncio.run(can_use_tool("AskUserQuestion", {"questions": questions}, None))

    assert type(result).__name__ == "PermissionResultDeny"
    assert "Enable caching?" in result.message


def test_multiselect_question_takes_a_comma_joined_answer(monkeypatch):
    session, _ = _make_session()
    can_use_tool = session._make_can_use_tool()
    questions = [{"question": "Which sections?", "multiSelect": True,
                  "options": [{"label": "Intro"}, {"label": "Summary"}]}]
    monkeypatch.setattr("tools.mcp_tool_sampling._has_connected_desktop_clients", lambda: True)
    seen = {}

    async def _answer(self, context, params):
        from mcp.types import ElicitResult
        schema = getattr(params, "requestedSchema", None) or getattr(params, "requested_schema", None)
        seen["field"] = schema["properties"]["Which sections?"]
        return ElicitResult(action="accept", content={"Which sections?": "Intro, Summary"})

    monkeypatch.setattr("tools.mcp_tool_sampling.ElicitationHandler.__call__", _answer)
    result = asyncio.run(can_use_tool("AskUserQuestion", {"questions": questions}, None))

    assert "enum" not in seen["field"]
    assert type(result).__name__ == "PermissionResultAllow"
    assert result.updated_input["answers"] == {"Which sections?": "Intro, Summary"}
    assert result.updated_input["questions"] == questions
