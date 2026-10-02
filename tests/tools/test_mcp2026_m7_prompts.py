"""Behavior tests for MCP 2026 M7 prompts as slash commands with typed args."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

import pytest
from tools.mcp_tool_handlers import (
    async_get_mcp_prompt,
    async_list_mcp_prompts,
    get_mcp_prompt,
    list_mcp_prompts,
    parse_prompt_args,
)


@pytest.fixture
def stdio_fixture_params():
    from mcp import StdioServerParameters
    fixture = Path(__file__).parents[1] / "fixtures" / "mcp2026_server.py"
    return StdioServerParameters(command=sys.executable, args=[str(fixture), "--stdio"])


def test_parse_prompt_args():
    declared = [{"name": "name", "required": True}, {"name": "greeting", "required": False}]

    # Positional mapping
    parsed = parse_prompt_args("Justin", declared)
    assert parsed == {"name": "Justin"}

    # Key=value
    parsed = parse_prompt_args("name=Justin greeting=Hi", declared)
    assert parsed == {"name": "Justin", "greeting": "Hi"}

    # Quoted values
    parsed = parse_prompt_args('name="Justin Case" --greeting="Good morning"', declared)
    assert parsed == {"name": "Justin Case", "greeting": "Good morning"}

    # Flag style --name Justin
    parsed = parse_prompt_args("--name Justin --greeting Hello", declared)
    assert parsed == {"name": "Justin", "greeting": "Hello"}

    # Empty
    assert parse_prompt_args("") == {}
    assert parse_prompt_args(None) == {}


def test_mcp_prompts_list_over_stdio(stdio_fixture_params: Any) -> None:
    prompts = list_mcp_prompts(stdio_fixture_params)
    assert len(prompts) >= 1
    greeting_prompt = next((p for p in prompts if p["name"] == "greeting"), None)
    assert greeting_prompt is not None
    assert "greeting" in greeting_prompt["name"]
    assert greeting_prompt["description"] == "A greeting prompt with a name argument."
    assert len(greeting_prompt["arguments"]) == 1
    arg = greeting_prompt["arguments"][0]
    assert arg["name"] == "name"
    assert arg["required"] is True


def test_mcp_prompts_get_with_args_renders_expected_text(stdio_fixture_params: Any) -> None:
    rendered = get_mcp_prompt(stdio_fixture_params, "greeting", {"name": "Hermes"})
    assert "messages" in rendered
    assert len(rendered["messages"]) == 1
    assert rendered["messages"][0]["role"] == "user"
    assert rendered["messages"][0]["content"] == "Hello, Hermes!"
    assert rendered["text"] == "Hello, Hermes!"


def test_mcp_prompts_get_missing_required_arg_is_error(stdio_fixture_params: Any) -> None:
    with pytest.raises(ValueError, match="Missing required argument"):
        get_mcp_prompt(stdio_fixture_params, "greeting", {})


@pytest.mark.asyncio
async def test_async_mcp_prompts_roundtrip(stdio_fixture_params: Any) -> None:
    prompts = await async_list_mcp_prompts(stdio_fixture_params)
    assert any(p["name"] == "greeting" for p in prompts)

    rendered = await async_get_mcp_prompt(stdio_fixture_params, "greeting", {"name": "AsyncUser"})
    assert rendered["text"] == "Hello, AsyncUser!"

    with pytest.raises(ValueError, match="Missing required argument"):
        await async_get_mcp_prompt(stdio_fixture_params, "greeting", {"wrong_arg": "value"})
