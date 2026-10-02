"""Resource reads go to exactly one named MCP server; never a fan-out (W2 fix P1-4).

Before the fix a ``{uri}``-only read tried every connected server and the first success won, so an
untrusted server that accepts any URI could supply ``@resource`` text or MCP-App HTML.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.types")

from tools import mcp_tool as core  # noqa: E402
from tools import mcp_tool_resources as res  # noqa: E402


class _Session:
    def __init__(self, name: str, listed: list[str], log: list):
        self._name, self._listed, self._log = name, listed, log

    async def list_resources(self, *args, **kwargs):
        self._log.append((self._name, "list"))
        return SimpleNamespace(resources=[SimpleNamespace(uri=u, name=u) for u in self._listed],
                               nextCursor=None, next_cursor=None)

    async def read_resource(self, uri):
        # Accepts ANY uri, like an untrusted server would.
        self._log.append((self._name, "read", str(uri)))
        return SimpleNamespace(contents=[SimpleNamespace(uri=str(uri), mime_type="text/plain",
                                                         text=f"from {self._name}", blob=None)])


class _Server:
    def __init__(self, name: str, listed: list[str], log: list):
        self.name = name
        self.session = _Session(name, listed, log)
        self._rpc_lock = asyncio.Lock()

    def _is_recycled_stdio(self) -> bool:
        return False


@pytest.fixture
def servers():
    """Replace the connected-server registry with fakes for one test; restore it afterwards."""
    log: list = []
    with core._lock:
        saved = dict(core._servers)
        core._servers.clear()

    def add(name: str, listed: list[str] | None = None) -> _Server:
        srv = _Server(name, listed or [], log)
        with core._lock:
            core._servers[name] = srv
        return srv

    yield SimpleNamespace(add=add, log=log)
    with core._lock:
        core._servers.clear()
        core._servers.update(saved)


def _reads(log: list) -> list:
    return [entry for entry in log if entry[1] == "read"]


# ── tools layer ───────────────────────────────────────────────────────────────


def test_read_without_a_server_is_refused_and_contacts_nobody(servers) -> None:
    servers.add("evil")
    servers.add("good", ["fixture://state"])
    with pytest.raises(ValueError, match="server"):
        res.read_mcp_resource("fixture://state")
    assert _reads(servers.log) == []


def test_named_server_is_the_only_one_read(servers) -> None:
    servers.add("evil")
    servers.add("good", ["fixture://state"])
    out = res.read_mcp_resource("fixture://state", server_name="good")
    assert out["server"] == "good"
    assert out["contents"][0]["text"] == "from good"
    assert _reads(servers.log) == [("good", "read", "fixture://state")]


def test_unknown_named_server_is_refused(servers) -> None:
    servers.add("evil")
    with pytest.raises(ValueError, match="not connected"):
        res.read_mcp_resource("fixture://state", server_name="nobody")
    assert _reads(servers.log) == []


def test_sanitized_form_of_a_hyphenated_key_resolves(servers) -> None:
    """The MCP App card only knows ``mcp__<sanitized>__<tool>``, so it sends the sanitized server."""
    servers.add("hermes-mcp-2026-fixture", ["ui://app"])
    servers.add("other")
    out = res.read_mcp_resource("ui://app", server_name="hermes_mcp_2026_fixture")
    assert out["server"] == "hermes-mcp-2026-fixture"
    assert _reads(servers.log) == [("hermes-mcp-2026-fixture", "read", "ui://app")]


def test_exact_key_resolves(servers) -> None:
    servers.add("hermes-mcp-2026-fixture", ["ui://app"])
    out = res.read_mcp_resource("ui://app", server_name="hermes-mcp-2026-fixture")
    assert out["server"] == "hermes-mcp-2026-fixture"


def test_ambiguous_sanitized_collision_is_refused(servers) -> None:
    servers.add("a-b", ["ui://app"])
    servers.add("a.b", ["ui://app"])
    with pytest.raises(ValueError, match="ambiguous"):
        res.read_mcp_resource("ui://app", server_name="a_b")
    assert _reads(servers.log) == []


def test_exact_key_that_another_key_sanitizes_to_is_refused(servers) -> None:
    """``a_b`` is a real key AND the sanitized form of ``a-b``: the caller may mean either."""
    servers.add("a_b", ["ui://app"])
    servers.add("a-b", ["ui://app"])
    with pytest.raises(ValueError, match="ambiguous"):
        res.read_mcp_resource("ui://app", server_name="a_b")
    assert _reads(servers.log) == []


def test_listing_resolution_reads_only_the_listing_server(servers) -> None:
    servers.add("evil")
    servers.add("good", ["fixture://state"])
    out = res.read_mcp_resource("fixture://state", resolve_listed=True)
    assert out["server"] == "good"
    assert _reads(servers.log) == [("good", "read", "fixture://state")]


def test_listing_resolution_refuses_an_unlisted_uri(servers) -> None:
    servers.add("evil")
    servers.add("good", ["fixture://other"])
    with pytest.raises(ValueError, match="lists"):
        res.read_mcp_resource("fixture://state", resolve_listed=True)
    assert _reads(servers.log) == []


def test_listing_resolution_refuses_an_ambiguous_uri(servers) -> None:
    servers.add("one", ["fixture://state"])
    servers.add("two", ["fixture://state"])
    with pytest.raises(ValueError, match="ambiguous"):
        res.read_mcp_resource("fixture://state", resolve_listed=True)
    assert _reads(servers.log) == []


# ── RPC wire: mcp.resources.read {server, uri} ────────────────────────────────


def test_rpc_read_requires_server(servers) -> None:
    from tui_gateway import server as gateway_server

    servers.add("evil")
    servers.add("good", ["fixture://state"])
    resp = gateway_server.handle_request({"id": 1, "method": "mcp.resources.read",
                                          "params": {"uri": "fixture://state"}})
    assert "error" in resp, resp
    assert _reads(servers.log) == []


def test_rpc_read_with_sanitized_server_reads_that_server(servers) -> None:
    from tui_gateway import server as gateway_server

    servers.add("evil")
    servers.add("hermes-mcp-2026-fixture", ["ui://app"])
    resp = gateway_server.handle_request({"id": 2, "method": "mcp.resources.read",
                                          "params": {"server": "hermes_mcp_2026_fixture", "uri": "ui://app"}})
    assert "result" in resp, resp
    assert resp["result"]["server"] == "hermes-mcp-2026-fixture"
    assert _reads(servers.log) == [("hermes-mcp-2026-fixture", "read", "ui://app")]


# ── @resource references ──────────────────────────────────────────────────────


def test_at_resource_without_server_reads_the_listing_server_not_the_first(servers) -> None:
    from agent.context_references import preprocess_context_references

    servers.add("evil")  # first in the registry, accepts any uri
    servers.add("good", ["fixture://state"])
    result = preprocess_context_references("check @resource:fixture://state", cwd=Path.cwd(),
                                           context_length=8000)
    assert "from good" in result.message
    assert "from evil" not in result.message
    assert _reads(servers.log) == [("good", "read", "fixture://state")]


def test_at_resource_with_server_prefix_reads_that_server(servers) -> None:
    from agent.context_references import preprocess_context_references

    servers.add("evil")
    servers.add("good")
    result = preprocess_context_references("check @resource:good:fixture://state", cwd=Path.cwd(),
                                           context_length=8000)
    assert "from good" in result.message
    assert _reads(servers.log) == [("good", "read", "fixture://state")]


def test_at_resource_unlisted_uri_is_refused(servers) -> None:
    from agent.context_references import preprocess_context_references

    servers.add("evil")
    result = preprocess_context_references("check @resource:fixture://state", cwd=Path.cwd(),
                                           context_length=8000)
    assert "from evil" not in result.message
    assert result.warnings and "fixture://state" in result.warnings[0]
    assert _reads(servers.log) == []
