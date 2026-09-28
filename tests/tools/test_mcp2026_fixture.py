"""Round-trip the MCP 2026 conformance fixture over both supported transports."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import pytest
from mcp import Client
from mcp.client.extension import ClientExtension, ResultClaim
from mcp_types import CallToolResult, ElicitResult, Result
from pydantic import BaseModel


class TaskHandle(Result):
    result_type: Literal["task"] = "task"
    task_id: str
    status: Literal["working"] = "working"
    created_at: str
    duration_ms: int


async def _resolve_task(_result: TaskHandle, _ctx: Any) -> CallToolResult:
    return CallToolResult(content=[])


class TasksClientExtension(ClientExtension):
    identifier = "io.modelcontextprotocol/tasks"

    def claims(self):
        return (ResultClaim(result_type="task", model=TaskHandle, resolve=_resolve_task),)


async def _exercise(server: Any) -> None:
    elicitation_requests: list[str] = []

    async def answer_elicitation(_context: Any, request: Any) -> ElicitResult:
        elicitation_requests.append(request.mode)
        if request.mode == "form":
            return ElicitResult(action="accept", content={"answer": "typed"})
        return ElicitResult(action="accept")

    async with Client(
        server,
        elicitation_callback=answer_elicitation,
        extensions=[TasksClientExtension()],
        read_timeout_seconds=3,
    ) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()
        tool_names = {tool.name for tool in tools.tools}
        assert {
            "progress_steps",
            "echo_meta",
            "structured_valid",
            "schema_mismatch",
            "elicit_form",
            "elicit_url",
            "needs_input",
            "task_start",
            "change_resource",
        } <= tool_names
        assert {resource.uri for resource in resources.resources} >= {
            "fixture://state",
            "ui://fixture/app",
        }
        assert {prompt.name for prompt in prompts.prompts} >= {"greeting"}

        progress: list[tuple[float, float | None, str | None]] = []

        async def collect_progress(current: float, total: float | None, message: str | None) -> None:
            progress.append((current, total, message))

        await client.call_tool(
            "progress_steps",
            {"steps": 2},
            progress_callback=collect_progress,
        )
        assert [item[0] for item in progress] == [1, 2]

        echoed = await client.call_tool("echo_meta", meta={"traceparent": "00-abc-def-01"})
        assert "traceparent" in echoed.content[0].text
        assert "00-abc-def-01" in echoed.content[0].text

        valid_tool = next(tool for tool in tools.tools if tool.name == "structured_valid")
        mismatch_tool = next(tool for tool in tools.tools if tool.name == "schema_mismatch")
        assert valid_tool.annotations.title == "Fixture output"
        assert valid_tool.annotations.read_only_hint is True
        assert valid_tool.annotations.destructive_hint is False
        assert valid_tool.output_schema
        assert mismatch_tool.output_schema == valid_tool.output_schema
        valid = await client.call_tool("structured_valid")
        assert valid.structured_content == {"value": "valid"}
        with pytest.raises(RuntimeError, match="Invalid structured content"):
            await client.call_tool("schema_mismatch")

        form = await client.call_tool("elicit_form")
        url = await client.call_tool("elicit_url")
        assert json.loads(form.content[0].text) == {"action": "accept", "answer": "typed"}
        assert url.content[0].text == "accept"
        assert elicitation_requests == ["form", "url"]

        input_result = await client.session.call_tool("needs_input", allow_input_required=True)
        assert input_result.result_type == "input_required"
        assert input_result.request_state

        task = await client.session.call_tool(
            "task_start",
            meta={"io.modelcontextprotocol/tasks": {}},
            allow_claimed=True,
        )
        assert task.result_type == "task"
        assert task.task_id

        async with client.listen(resource_subscriptions=["fixture://state"]) as subscription:
            await client.call_tool("change_resource")
            event = await asyncio.wait_for(anext(subscription), timeout=2)
            assert event.uri == "fixture://state"

        state = await client.read_resource("fixture://state")
        assert json.loads(state.contents[0].text) == {"status": "ready"}
        app = await client.read_resource("ui://fixture/app")
        assert "MCP fixture app" in app.contents[0].text
        assert app.contents[0].mime_type == "text/html;profile=mcp-app"
        app_meta = next(resource for resource in resources.resources if str(resource.uri) == "ui://fixture/app").meta
        assert app_meta["ui"]["csp"] is not None

        prompt = await client.get_prompt("greeting", {"name": "Hermes"})
        assert "Hello, Hermes!" in prompt.messages[0].content.text


def test_mcp2026_fixture_round_trips_over_both_transports(mcp2026_server: Any) -> None:
    asyncio.run(_exercise(mcp2026_server))
