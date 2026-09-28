"""M2 annotations, approval policy (D56) and outputSchema validation against the stdio M0 fixture.

The consent function is always mocked; nothing here can prompt for real.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

_CONSENT = "tools.approval_prompt.request_elicitation_consent"


async def _discover(server_parameters):
    async with stdio_client(server_parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            valid = await session.call_tool("structured_valid")
            try:
                mismatch = await session.call_tool("schema_mismatch")
            except RuntimeError as exc:
                mismatch = exc
            return tools, valid, mismatch


@pytest.fixture
def fixture_server(mcp2026_stdio_server, monkeypatch):
    """Discover the fixture's tools over stdio; ``configure(trust, confirm_destructive)`` records per-server policy."""
    from tools import mcp_tool
    from tools.mcp_tool_registration import _record_tool_trust_metadata, _track_mcp_tool_server
    from tools.mcp_tool_scope import _server_key

    tools, valid, mismatch = asyncio.run(_discover(mcp2026_stdio_server))
    key = _server_key("fixture")
    for table in ("_server_trust_levels", "_server_confirm_destructive", "_tool_read_only_hints",
                  "_tool_annotations", "_tool_raw_names"):
        monkeypatch.setattr(mcp_tool, table, dict(getattr(mcp_tool, table)))

    def configure(trust: str = "full", confirm_destructive=None) -> None:
        config = {"trust": trust}
        if confirm_destructive is not None:
            config["confirm_destructive"] = confirm_destructive
        _record_tool_trust_metadata("fixture", config, tools, key=key)
        _track_mcp_tool_server("mcp__fixture__progress_steps", "fixture")

    return SimpleNamespaceLike(configure=configure, valid=valid, mismatch=mismatch)


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_read_only_auto_allows_on_an_untrusted_server(fixture_server) -> None:
    fixture_server.configure("untrusted")
    with patch(_CONSENT, return_value="deny") as ask:
        from tools.mcp_tool_handlers import _trust_gate_check
        assert _trust_gate_check("fixture", "structured_valid") is None
    ask.assert_not_called()


@pytest.mark.parametrize("tool", ["no_hints", "destructive_action"])
def test_write_capable_prompts_on_an_untrusted_server(fixture_server, tool) -> None:
    from tools.mcp_tool_handlers import _trust_gate_check
    fixture_server.configure("untrusted")
    with patch(_CONSENT, return_value="accept") as ask:
        assert _trust_gate_check("fixture", tool) is None
    ask.assert_called_once()
    with patch(_CONSENT, return_value="deny"):
        assert "was NOT run" in json.loads(_trust_gate_check("fixture", tool))["error"]


def test_destructive_on_a_trusted_server_does_not_prompt_by_default(fixture_server) -> None:
    from tools.mcp_tool_handlers import _trust_gate_check
    for confirm in (None, False, "true", 1):  # only a literal true opts in
        fixture_server.configure("full", confirm)
        with patch(_CONSENT, return_value="deny") as ask:
            assert _trust_gate_check("fixture", "destructive_action") is None
            assert _trust_gate_check("fixture", "no_hints") is None
        ask.assert_not_called()


def test_destructive_on_a_trusted_server_prompts_with_confirm_destructive(fixture_server) -> None:
    from tools.mcp_tool_handlers import _trust_gate_check
    fixture_server.configure("full", True)
    with patch(_CONSENT, return_value="accept") as ask:
        assert _trust_gate_check("fixture", "destructive_action") is None
        ask.assert_called_once()
        assert "Destructive fixture action" in ask.call_args.args[0]  # the title labels the prompt
        ask.reset_mock()
        assert _trust_gate_check("fixture", "no_hints") is None  # not destructive: still no prompt
        ask.assert_not_called()
    with patch(_CONSENT, return_value="deny"):
        assert "was NOT run" in json.loads(_trust_gate_check("fixture", "destructive_action"))["error"]
    with patch(_CONSENT, side_effect=RuntimeError("no surface")):  # fail closed
        assert "fail-closed" in json.loads(_trust_gate_check("fixture", "destructive_action"))["error"]


def test_read_only_hint_never_waives_an_always_ask_rule(fixture_server) -> None:
    """confirm_destructive is an always-ask rule: a tool claiming readOnlyHint + destructiveHint still asks,
    on trusted and untrusted servers alike. (The MCP path has no other deny/ask rules: the gate is the only
    one and runs first in the handler, before the circuit breaker and any transport work.)"""
    from tools import mcp_tool
    from tools.mcp_tool_handlers import _trust_gate_check
    from tools.mcp_tool_scope import _server_key
    for trust in ("full", "untrusted"):
        fixture_server.configure(trust, True)
        mcp_tool._tool_read_only_hints[_server_key("fixture")]["destructive_action"] = True
        with patch(_CONSENT, return_value="deny") as ask:
            assert "was NOT run" in json.loads(_trust_gate_check("fixture", "destructive_action"))["error"]
        ask.assert_called_once()
    fixture_server.configure("full")  # without the rule a read-only claim on a trusted server is just allowed
    mcp_tool._tool_read_only_hints[_server_key("fixture")]["destructive_action"] = True
    with patch(_CONSENT) as ask:
        assert _trust_gate_check("fixture", "destructive_action") is None
    ask.assert_not_called()


def test_handler_consults_the_gate_before_any_transport_work(fixture_server) -> None:
    from tools.mcp_tool_handlers import _make_tool_handler
    fixture_server.configure("full", True)
    with patch(_CONSENT, return_value="deny") as ask, \
            patch("tools.mcp_tool_handlers._acquire_call_server", side_effect=AssertionError("transport touched")):
        out = _make_tool_handler("fixture", "destructive_action", 5.0)({})
    ask.assert_called_once()
    assert "was NOT run" in json.loads(out)["error"]


def test_confirm_destructive_is_stored_per_server_and_forgotten(fixture_server) -> None:
    from tools import mcp_tool
    from tools.mcp_tool_registration import _record_scope_trust
    from tools.mcp_tool_scope import _server_key
    fixture_server.configure("full", True)
    assert mcp_tool._server_confirm_destructive[_server_key("fixture")] is True
    _record_scope_trust("other", {"trust": "full"}, "scope-x")
    assert mcp_tool._server_confirm_destructive[_server_key("other", "scope-x", current=False)] is False


def test_title_is_the_card_label(fixture_server) -> None:
    from tools.mcp_tool_handlers import _tool_display_title
    fixture_server.configure()
    assert _tool_display_title("mcp__fixture__progress_steps") == "Progress steps"
    assert _tool_display_title("mcp__fixture__unknown") is None


def test_valid_structured_content_passes(fixture_server) -> None:
    from tools.mcp_tool_handlers import _render_validated_call_tool_result
    fixture_server.configure()
    out = json.loads(_render_validated_call_tool_result(fixture_server.valid, "fixture", "structured_valid"))
    assert "error" not in out
    assert "valid" in json.dumps(out)


def test_structured_content_schema_mismatch_becomes_a_tool_error(fixture_server) -> None:
    from mcp.types import CallToolResult, TextContent
    from tools.mcp_tool_handlers import _render_validated_call_tool_result, _structured_output_exception_result
    fixture_server.configure()
    # Path 1: the SDK client rejects it while parsing the response.
    if isinstance(fixture_server.mismatch, BaseException):
        error = json.loads(_structured_output_exception_result(fixture_server.mismatch, "schema_mismatch"))
        assert error["error_type"] == "mcp_output_schema" and "Invalid structured content" in error["error"]
    # Path 2: the SDK let it through; our own jsonschema check against the discovered outputSchema catches it.
    bad = CallToolResult(content=[TextContent(type="text", text="x")], structured_content={"value": 42})
    error = json.loads(_render_validated_call_tool_result(bad, "fixture", "schema_mismatch"))
    assert error["error_type"] == "mcp_output_schema"
    assert "Invalid structured content" in error["error"]
