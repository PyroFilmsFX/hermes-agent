"""Tests for MCP resource context reference expansion (@resource:<uri>)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent.context_references import (
    parse_context_references,
    preprocess_context_references,
    preprocess_context_references_async,
)


def test_parse_mcp_resource_reference() -> None:
    refs = parse_context_references("Inspect @resource:postgres://schema/users for me")
    assert len(refs) == 1
    assert refs[0].kind == "resource"
    assert refs[0].target == "postgres://schema/users"
    assert refs[0].raw == "@resource:postgres://schema/users"

    quoted_refs = parse_context_references('Inspect @resource:"fixture://state with spaces" now')
    assert len(quoted_refs) == 1
    assert quoted_refs[0].kind == "resource"
    assert quoted_refs[0].target == "fixture://state with spaces"


@pytest.mark.asyncio
async def test_expand_mcp_resource_reference_success() -> None:
    fake_result = {
        "server": "fixture",
        "uri": "fixture://state",
        "contents": [
            {
                "uri": "fixture://state",
                "mimeType": "application/json",
                "text": '{"status": "ready", "count": 42}',
            }
        ],
    }

    with patch("tools.mcp_tool_resources.read_mcp_resource_async", new=AsyncMock(return_value=fake_result)):
        result = await preprocess_context_references_async(
            "Please check @resource:fixture://state",
            cwd=Path.cwd(),
            context_length=8000,
        )

    assert result.expanded is True
    assert not result.warnings
    assert "Please check @resource:fixture://state" in result.message
    assert "--- Attached Context ---" in result.message
    assert "📦 @resource:fixture://state" in result.message
    assert '```\n{"status": "ready", "count": 42}\n```' in result.message


@pytest.mark.asyncio
async def test_expand_mcp_resource_reference_binary() -> None:
    fake_result = {
        "server": "fixture",
        "uri": "fixture://image.png",
        "contents": [
            {
                "uri": "fixture://image.png",
                "mimeType": "image/png",
                "blob": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            }
        ],
    }

    with patch("tools.mcp_tool_resources.read_mcp_resource_async", new=AsyncMock(return_value=fake_result)):
        result = await preprocess_context_references_async(
            "Here is @resource:fixture://image.png",
            cwd=Path.cwd(),
            context_length=8000,
        )

    assert result.expanded is True
    assert not result.warnings
    assert f"[binary data, {len(fake_result['contents'][0]['blob'])} bytes]" in result.message


def test_expand_mcp_resource_reference_unknown_server_inline_error() -> None:
    with patch(
        "tools.mcp_tool_resources.read_mcp_resource_async",
        new=AsyncMock(side_effect=ValueError("MCP server 'unknown_server' is not connected")),
    ):
        result = preprocess_context_references(
            "Check @resource:unknown_server/fixture://state please",
            cwd=Path.cwd(),
            context_length=8000,
        )

    # Must NOT crash, must produce a clear inline warning/error
    assert result.expanded is True
    assert len(result.warnings) == 1
    assert "MCP server 'unknown_server' is not connected" in result.warnings[0]
    assert "--- Context Warnings ---" in result.message
    assert "MCP server 'unknown_server' is not connected" in result.message


@pytest.mark.asyncio
async def test_expand_mcp_resource_reference_empty() -> None:
    fake_result = {
        "server": "fixture",
        "uri": "fixture://empty",
        "contents": [],
    }

    with patch("tools.mcp_tool_resources.read_mcp_resource_async", new=AsyncMock(return_value=fake_result)):
        result = await preprocess_context_references_async(
            "Empty @resource:fixture://empty",
            cwd=Path.cwd(),
            context_length=8000,
        )

    assert result.expanded is True
    assert any("empty resource" in w for w in result.warnings)
