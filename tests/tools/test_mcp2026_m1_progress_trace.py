"""M1 MCP progress and trace propagation against the real stdio conformance server (M0 fixture)."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

_VALID = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
_GENERATED = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01")


def _run_call(server_parameters, tool: str, args: dict, **kwargs):
    from tools.mcp_tool_handlers import _call_tool_racing_stdio_death

    async def go():
        async with stdio_client(server_parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await _call_tool_racing_stdio_death(
                    SimpleNamespace(session=session), "fixture", tool, args, **kwargs)

    return asyncio.run(go())


def _echoed_traceparent(result) -> str:
    return json.loads(result.content[0].text)["traceparent"]


def test_progress_updates_reach_the_tool_progress_callback(mcp2026_stdio_server) -> None:
    events: list[tuple[tuple, dict]] = []
    result = _run_call(
        mcp2026_stdio_server, "progress_steps", {"steps": 3},
        progress_callback=lambda *a, **kw: events.append((a, kw)), tool_call_id="call-progress")
    assert result.is_error is False
    assert [kw["progress"] for _, kw in events] == [1.0, 2.0, 3.0]
    assert [kw["total"] for _, kw in events] == [3.0, 3.0, 3.0]
    assert [kw["message"] for _, kw in events] == ["step 1/3", "step 2/3", "step 3/3"]
    # positional contract of the agent tool_progress_callback: (event_type, name, preview, args)
    assert all(a[0] == "tool.progress" and a[1] == "mcp__fixture__progress_steps" for a, _ in events)
    assert [a[2] for a, _ in events] == ["step 1/3", "step 2/3", "step 3/3"]
    assert all(kw["tool_call_id"] == "call-progress" for _, kw in events)


def test_a_failing_progress_callback_does_not_fail_the_call(mcp2026_stdio_server) -> None:
    def boom(*_a, **_kw):
        raise RuntimeError("ui gone")

    result = _run_call(mcp2026_stdio_server, "progress_steps", {"steps": 2}, progress_callback=boom)
    assert result.is_error is False


def test_supplied_valid_traceparent_is_passed_through(mcp2026_stdio_server) -> None:
    result = _run_call(mcp2026_stdio_server, "echo_meta", {}, traceparent=_VALID)
    assert _echoed_traceparent(result) == _VALID


def test_missing_traceparent_is_generated(mcp2026_stdio_server) -> None:
    assert _GENERATED.fullmatch(_echoed_traceparent(_run_call(mcp2026_stdio_server, "echo_meta", {})))


@pytest.mark.parametrize("bad", [
    "not-a-traceparent",
    "",
    "00-" + "0" * 32 + "-" + "b" * 16 + "-01",  # all-zero trace id
    "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # all-zero span id
    "00-" + "A" * 32 + "-" + "b" * 16 + "-01",  # uppercase hex
    "00-" + "a" * 31 + "-" + "b" * 16 + "-01",  # short trace id
])
def test_invalid_traceparent_is_replaced_with_a_generated_valid_one(mcp2026_stdio_server, bad) -> None:
    echoed = _echoed_traceparent(_run_call(mcp2026_stdio_server, "echo_meta", {}, traceparent=bad))
    assert echoed != bad
    assert _GENERATED.fullmatch(echoed)
