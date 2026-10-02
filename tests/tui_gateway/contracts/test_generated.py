"""The committed TypeScript + OpenRPC contract files are exactly what ``tui_gateway/contracts``
renders, and the contract catalog covers the whole wire.

Regenerate with ``.venv/bin/python scripts/gen_gateway_contracts.py`` when a model changes. The
two files are listed in ``scripts/ci/classify_changes.py::_PY_RELEVANT_CONTRACT_FILES`` so a
TS-only PR that edits them still runs this test.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GEN = REPO / "scripts" / "gen_gateway_contracts.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_gateway_contracts", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_files_are_current(gen):
    """Both committed artefacts equal an in-memory regeneration (byte-for-byte)."""
    stale = [path.relative_to(REPO) for path, text in gen.render_all().items()
             if (path.read_text(encoding="utf-8") if path.exists() else None) != text]
    assert not stale, f"stale generated contract files {stale}: run scripts/gen_gateway_contracts.py"


# The emitter inventory the old gateway-events.json scan used, kept as the completeness oracle:
# names must come from CODE the gateway runs, never from the contract tables themselves.
_EMIT_HELPERS = ("_emit", "_broadcast_global_event", "_voice_emit", "_pet_emit", "_emit_tool_lifecycle")
_LITERAL_EMIT = re.compile(r"\b(?:%s)\(\s*\"([a-z_][a-z0-9_.]*)\"" % "|".join(_EMIT_HELPERS))
_REQUEST_HELPERS = ("server_requests\\.send", "server_requests\\.send_async", "_ask", "_read_block")
_LITERAL_REQUEST = re.compile(r"\b(?:%s)\(\s*\"([a-z_][a-z0-9_.]*)\"" % "|".join(_REQUEST_HELPERS))
_LITERAL_FRAME = re.compile(r"\"method\":\s*\"event\".{0,120}?\"type\":\s*\"([a-z_][a-z0-9_.]*)\"", re.S)
_SIDE_AGENT = re.compile(r"_spawn_side_agent\((?:[^()]|\([^()]*\))*?\"([a-z_][a-z0-9_.]*\.complete)\"", re.S)
_SUBAGENT_RELAY = re.compile(r"\"(subagent\.[a-z_]+)\"")
_DESKTOP_UI_EMIT = re.compile(r"desktop_ui\.(?:emit|emit_or_error)\(\s*\"([a-z_][a-z0-9_.]*)\"")
_BROKER_FRAME = re.compile(r"^FRAME_[A-Z_]+ = \"(browser\.controller\.[a-z_]+)\"", re.M)
_SETUP_READY = re.compile(r"^SETUP_READY_EVENT = \"([a-z_.]+)\"", re.M)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def emitted_event_names() -> set[str]:
    names: set[str] = set()
    for src in (REPO / "tui_gateway").glob("*.py"):
        text = _read(src)
        names.update(_LITERAL_EMIT.findall(text))
        names.update(_LITERAL_FRAME.findall(text))
        names.update(_SIDE_AGENT.findall(text))
    from tui_gateway.agent_callbacks import _CHILD_DELTA_EVENTS
    from tui_gateway.change_watcher import _CHANGE_WATCHES

    names.update(_CHANGE_WATCHES)
    names.update(_CHILD_DELTA_EVENTS.values())
    for src in (REPO / "tools").glob("delegate_tool*.py"):
        names.update(_SUBAGENT_RELAY.findall(_read(src)))
    names.discard("subagent.text")  # mirrored into the watch window as message.delta, never emitted
    from tools.registry import _tool_module_candidates

    for src in _tool_module_candidates(REPO / "tools"):
        names.update(_DESKTOP_UI_EMIT.findall(_read(src)))
    names.update(_BROKER_FRAME.findall(_read(REPO / "gateway" / "browser_control_broker.py")))
    names.update(_SETUP_READY.findall(_read(REPO / "hermes_cli" / "free_tier_bootstrap.py")))
    names.update(_LITERAL_EMIT.findall(_read(REPO / "hermes_cli" / "plugins.py")))
    return names


def sent_server_requests() -> set[str]:
    names: set[str] = set()
    for src in (REPO / "tui_gateway").glob("*.py"):
        names.update(_LITERAL_REQUEST.findall(_read(src)))
    return names


def test_catalog_covers_the_whole_wire():
    """Every registered method, every emitted event and every sent server request has a contract,
    and no contract is orphaned (a deleted handler must take its contract with it)."""
    from tui_gateway import server
    from tui_gateway.contracts import registry

    registry.assert_complete(server._methods, emitted_event_names(), sent_server_requests())


class _CapturePeer:
    """Mock transport to capture frames emitted to connected clients."""

    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


def test_plugin_emit_captured_by_gateway():
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tui_gateway import server

    peer = _CapturePeer()
    server.register_live_transport(peer)
    try:
        manager = PluginManager()
        manager._discovered = True
        manifest = PluginManifest(name="alpha", key="alpha_key")
        ctx = PluginContext(manifest, manager)

        count = ctx.emit("custom.event", {"hello": "world", "num": 42})
        assert count == 0

        plugin_frames = [f for f in peer.frames if f.get("params", {}).get("type") == "plugin.event"]
        assert len(plugin_frames) == 1
        params = plugin_frames[0]["params"]
        assert params["payload"] == {
            "plugin": "alpha_key",
            "name": "custom.event",
            "payload": {"hello": "world", "num": 42},
        }
    finally:
        server.unregister_live_transport(peer)


def test_plugin_emit_oversize_dropped(caplog):
    import logging
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tui_gateway import server

    peer = _CapturePeer()
    server.register_live_transport(peer)
    try:
        manager = PluginManager()
        manager._discovered = True
        manifest = PluginManifest(name="beta", key="beta_key")
        ctx = PluginContext(manifest, manager)

        large_str = "x" * (65 * 1024)
        with caplog.at_level(logging.WARNING):
            ctx.emit("oversize.event", {"large": large_str})

        plugin_frames = [f for f in peer.frames if f.get("params", {}).get("type") == "plugin.event"]
        assert len(plugin_frames) == 0
        assert any("exceeds 64 KiB" in r.message for r in caplog.records)
    finally:
        server.unregister_live_transport(peer)


def test_plugin_emit_non_serialisable_dropped(caplog):
    import logging
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tui_gateway import server

    peer = _CapturePeer()
    server.register_live_transport(peer)
    try:
        manager = PluginManager()
        manager._discovered = True
        manifest = PluginManifest(name="gamma", key="gamma_key")
        ctx = PluginContext(manifest, manager)

        with caplog.at_level(logging.WARNING):
            ctx.emit("unserialisable.event", {"bad": object()})

        plugin_frames = [f for f in peer.frames if f.get("params", {}).get("type") == "plugin.event"]
        assert len(plugin_frames) == 0
        assert any("not JSON-serialisable" in r.message for r in caplog.records)
    finally:
        server.unregister_live_transport(peer)
