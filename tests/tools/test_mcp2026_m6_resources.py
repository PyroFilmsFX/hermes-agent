"""Tests for MCP 2026 unit M6a: MCP resources list, read, subscribe, and push notifications
on the plugin event bus."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from typing import Any

import pytest

from tools.mcp_tool_resources import (
    get_active_subscriptions,
    list_mcp_resources,
    list_mcp_resources_async,
    read_mcp_resource,
    read_mcp_resource_async,
)
from tui_gateway import server as gateway_server


@pytest.fixture
def stdio_fixture_params():
    from mcp import StdioServerParameters
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "mcp2026_server.py"
    return StdioServerParameters(command=sys.executable, args=[str(fixture), "--stdio"])


@pytest.fixture
def fixture_server_config():
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "mcp2026_server.py"
    return {
        "command": sys.executable,
        "args": [str(fixture_path), "--stdio"],
        "cwd": str(fixture_path.parent.parent.parent),
        "protocol": "2026-07-28",
    }


def test_mcp_resources_list_over_stdio(stdio_fixture_params: Any) -> None:
    resources = list_mcp_resources(stdio_fixture_params)
    assert len(resources) >= 2
    uris = {r["uri"]: r for r in resources}
    assert "fixture://state" in uris
    assert "ui://fixture/app" in uris

    state_item = uris["fixture://state"]
    assert state_item["name"] == "fixture-state"
    assert state_item["mimeType"] == "application/json"


def test_mcp_resources_read_over_stdio(stdio_fixture_params: Any) -> None:
    result = read_mcp_resource("fixture://state", stdio_fixture_params)
    assert result["uri"] == "fixture://state"
    assert len(result["contents"]) == 1
    content_block = result["contents"][0]
    assert content_block["uri"] == "fixture://state"
    assert content_block["mimeType"] == "application/json"
    parsed = json.loads(content_block["text"])
    assert parsed == {"status": "ready"}


@pytest.mark.asyncio
async def test_async_mcp_resources_roundtrip(stdio_fixture_params: Any) -> None:
    resources = await list_mcp_resources_async(stdio_fixture_params)
    assert any(r["uri"] == "fixture://state" for r in resources)

    result = await read_mcp_resource_async("fixture://state", stdio_fixture_params)
    assert len(result["contents"]) >= 1
    assert json.loads(result["contents"][0]["text"]) == {"status": "ready"}


def test_mcp_resources_rpc_unknown_server() -> None:
    # 1. mcp.resources.list on unknown server returns error
    res_list = gateway_server.handle_request({
        "id": 1,
        "method": "mcp.resources.list",
        "params": {"server": "nonexistent_server_xyz"},
    })
    assert "error" in res_list
    assert res_list["error"]["code"] in (4018, 5024)

    # 2. mcp.resources.read on unknown server returns error
    res_read = gateway_server.handle_request({
        "id": 2,
        "method": "mcp.resources.read",
        "params": {"server": "nonexistent_server_xyz", "uri": "fixture://state"},
    })
    assert "error" in res_read
    assert res_read["error"]["code"] in (4018, 5024)

    # 3. mcp.resources.subscribe on unknown server returns error
    res_sub = gateway_server.handle_request({
        "id": 3,
        "method": "mcp.resources.subscribe",
        "params": {"server": "nonexistent_server_xyz", "uri": "fixture://state"},
    })
    assert "error" in res_sub
    assert res_sub["error"]["code"] in (4018, 5024)


def test_mcp_resources_subscribe_push_and_disconnect_cleanup(fixture_server_config: dict) -> None:
    from hermes_cli.plugins import get_plugin_manager
    from tools import mcp_tool_discovery as _discovery
    from tools import mcp_tool_loop as _loop
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    # Start server through the real registry path so it lives on the MCP background loop thread
    register_mcp_servers({"fixture": fixture_server_config})

    manager = get_plugin_manager()
    received_event = threading.Event()
    received_data: dict[str, Any] = {}

    def _on_updated(**kwargs):
        received_data.update(kwargs)
        received_event.set()

    manager._subscribe_event("test_resource_listener", "mcp:resources.updated", _on_updated)

    try:
        # 1. mcp.resources.list RPC
        rpc_list = gateway_server.handle_request({
            "id": 10,
            "method": "mcp.resources.list",
            "params": {"server": "fixture"},
        })
        assert "result" in rpc_list, f"RPC failed: {rpc_list}"
        uris = [r["uri"] for r in rpc_list["result"]["resources"]]
        assert "fixture://state" in uris

        # 2. mcp.resources.read RPC
        rpc_read = gateway_server.handle_request({
            "id": 11,
            "method": "mcp.resources.read",
            "params": {"server": "fixture", "uri": "fixture://state"},
        })
        assert "result" in rpc_read, f"RPC failed: {rpc_read}"
        assert rpc_read["result"]["server"] == "fixture"
        assert json.loads(rpc_read["result"]["contents"][0]["text"]) == {"status": "ready"}

        # 3. mcp.resources.subscribe RPC
        sub_resp = gateway_server.handle_request({
            "id": 12,
            "method": "mcp.resources.subscribe",
            "params": {"server": "fixture", "uri": "fixture://state"},
        })
        assert "result" in sub_resp, f"RPC subscribe failed: {sub_resp}"
        assert sub_resp["result"]["ok"] is True
        assert ("fixture", "fixture://state") in get_active_subscriptions()

        # 4. Trigger resource update on fixture server via change_resource tool
        server = _discovery._get_connected_server_for_call("fixture")
        assert server is not None and server.session is not None
        tool_call_res = _loop._run_on_mcp_loop(lambda: server.session.call_tool("change_resource"), timeout=10)
        assert tool_call_res is not None

        # Acceptance: fixture resource update reaches the bus within 2 s (no polling)
        assert received_event.wait(timeout=2.0), "Resource update event not received on plugin bus within 2.0s"
        assert received_data["server"] == "fixture"
        assert received_data["uri"] == "fixture://state"

        # 5. mcp.resources.unsubscribe RPC
        unsub_resp = gateway_server.handle_request({
            "id": 13,
            "method": "mcp.resources.unsubscribe",
            "params": {"server": "fixture", "uri": "fixture://state"},
        })
        assert "result" in unsub_resp, f"RPC unsubscribe failed: {unsub_resp}"
        assert unsub_resp["result"]["ok"] is True
        assert ("fixture", "fixture://state") not in get_active_subscriptions()

        # 6. Re-subscribe and verify disconnect removes subscription
        sub_resp2 = gateway_server.handle_request({
            "id": 14,
            "method": "mcp.resources.subscribe",
            "params": {"server": "fixture", "uri": "fixture://state"},
        })
        assert sub_resp2["result"]["ok"] is True
        assert ("fixture", "fixture://state") in get_active_subscriptions()

        # Disconnect server
        shutdown_mcp_servers(names={"fixture"})
        assert ("fixture", "fixture://state") not in get_active_subscriptions()

    finally:
        manager._remove_plugin_subscriptions("test_resource_listener")
        shutdown_mcp_servers(names={"fixture"})
