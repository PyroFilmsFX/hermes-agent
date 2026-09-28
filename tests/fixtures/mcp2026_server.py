"""Small MCP 2026 conformance server used by the Hermes host tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension
from mcp.server.mcpserver.context import Context
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequiredResult,
    Result,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel


TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
APP_EXTENSION = "io.modelcontextprotocol/ui"
RESOURCE_URI = "fixture://state"
APP_URI = "ui://fixture/app"
_tasks: dict[str, dict[str, Any]] = {}


class EchoOutput(BaseModel):
    value: str


class TaskHandle(Result):
    """The fixture's claimed task result; tasks are an opt-in extension."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: Literal["working"] = "working"
    created_at: str
    duration_ms: int


class FixtureExtensions(Extension):
    identifier = TASKS_EXTENSION

    def __init__(self, task_duration: float):
        self.task_duration = task_duration

    def settings(self) -> dict[str, Any]:
        return {"tasks": {"call": {"resultTypes": ["task"]}}}

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: Any,
    ) -> Any:
        if params.name == "schema_mismatch":
            # Keep the declared output schema, but send intentionally invalid structure.
            return CallToolResult(
                content=[TextContent(type="text", text="deliberately invalid output")],
                structured_content={"value": 42},
            )

        if params.name == "task_start" and _tasks_extension_requested(ctx):
            task_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            _tasks[task_id] = {"status": "working", "created_at": now}
            asyncio.create_task(_complete_task(task_id, self.task_duration))
            return TaskHandle(
                taskId=task_id,
                createdAt=now,
                duration_ms=round(self.task_duration * 1000),
            )

        return await call_next(ctx)


class AppsExtension(Extension):
    identifier = APP_EXTENSION

    def settings(self) -> dict[str, Any]:
        return {"mimeTypes": ["text/html;profile=mcp-app"]}


def _tasks_extension_requested(ctx: ServerRequestContext[Any, Any]) -> bool:
    """The amendment opts tasks into each tools/call request via its _meta."""
    request_meta = ctx.meta or {}
    declared = request_meta.get(TASKS_EXTENSION)
    if declared is not None:
        return True
    capabilities = request_meta.get(CLIENT_CAPABILITIES_META_KEY) or {}
    return TASKS_EXTENSION in (capabilities.get("extensions") or {})


async def _complete_task(task_id: str, duration: float) -> None:
    await asyncio.sleep(duration)
    task = _tasks.get(task_id)
    if task is not None:
        task["status"] = "completed"


def build_server(task_duration: float = 0.05) -> MCPServer:
    server = MCPServer(
        "hermes-mcp-2026-fixture",
        version="1.0.0",
        extensions=[FixtureExtensions(task_duration), AppsExtension()],
    )

    @server.tool(description="Report N progress steps.")
    async def progress_steps(steps: int, ctx: Context) -> str:
        for step in range(1, steps + 1):
            await ctx.report_progress(step, steps, f"step {step}/{steps}")
        return f"reported {steps} steps"

    @server.tool(description="Return the request _meta as JSON.")
    async def echo_meta(ctx: Context) -> str:
        return json.dumps(ctx.request_context.meta or {}, sort_keys=True)

    @server.tool(
        title="Valid structured output",
        annotations=ToolAnnotations(title="Fixture output", read_only_hint=True, destructive_hint=False),
        structured_output=True,
    )
    def structured_valid() -> EchoOutput:
        return EchoOutput(value="valid")

    @server.tool(
        title="Deliberately mismatching structured output",
        annotations=ToolAnnotations(title="Fixture output", read_only_hint=True, destructive_hint=False),
        structured_output=True,
    )
    def schema_mismatch() -> EchoOutput:
        return EchoOutput(value="intercepted")

    @server.tool(description="Ask the client to fill a form through input_required.", structured_output=False)
    async def elicit_form(ctx: Context) -> str | InputRequiredResult:
        answer = (ctx.input_responses or {}).get("form")
        if isinstance(answer, ElicitResult):
            return json.dumps({"action": answer.action, "answer": (answer.content or {}).get("answer")})
        return InputRequiredResult(
            input_requests={
                "form": ElicitRequest(
                    params=ElicitRequestFormParams(
                        message="Provide a short answer",
                        requested_schema={
                            "type": "object",
                            "properties": {"answer": {"type": "string", "title": "Answer"}},
                            "required": ["answer"],
                        },
                    )
                )
            },
            request_state="form-state",
        )

    @server.tool(description="Ask the client to open a URL through input_required.", structured_output=False)
    async def elicit_url(ctx: Context) -> str | InputRequiredResult:
        answer = (ctx.input_responses or {}).get("url")
        if isinstance(answer, ElicitResult):
            return answer.action
        return InputRequiredResult(
            input_requests={
                "url": ElicitRequest(
                    params=ElicitRequestURLParams(
                        message="Continue on the fixture site",
                        url="https://example.com/fixture",
                    )
                )
            },
            request_state="url-state",
        )

    @server.tool(description="Return a 2026 input_required result.")
    async def needs_input() -> InputRequiredResult:
        return InputRequiredResult(request_state="fixture-state")

    @server.tool(description="Return a task handle when the request opts into tasks.")
    async def task_start() -> str:
        return "tasks extension was not requested"

    @server.tool(description="Publish a resource change notification.")
    async def change_resource(ctx: Context) -> str:
        # MCP 2.2.0 supports subscriptions/listen; resources/subscribe is removed in 2026.
        await ctx.notify_resource_updated(RESOURCE_URI)
        return "resource update published"

    @server.resource(RESOURCE_URI, name="fixture-state", mime_type="application/json")
    def state_resource() -> str:
        return json.dumps({"status": "ready"})

    @server.resource(
        APP_URI,
        name="fixture-app",
        description="Static MCP App HTML fixture.",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"csp": {"connect-src": [], "resource_domains": []}}},
    )
    def app_resource() -> str:
        return "<!doctype html><html><body><main>MCP fixture app</main></body></html>"

    @server.prompt(name="greeting", description="A greeting prompt with a name argument.")
    def greeting(name: str) -> str:
        return f"Hello, {name}!"

    return server


async def _run(args: argparse.Namespace) -> None:
    server = build_server(args.task_duration)
    if args.stdio:
        await server.run_stdio_async()
    else:
        await server.run_streamable_http_async(host="127.0.0.1", port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--stdio", action="store_true")
    transport.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--task-duration", type=float, default=0.05)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
