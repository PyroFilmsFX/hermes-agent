"""MCP resources management: list, read, subscribe, unsubscribe, and push notifications
on the plugin event bus (MCP 2026 unit M6)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools.mcp_tool_common import _core, mcp_field

logger = logging.getLogger("tools.mcp_tool")

_last_emitted: Dict[Tuple[str, str], float] = {}
_emit_lock = threading.Lock()


def _emit_resource_update(server_name: str, uri: str) -> None:
    """Publish resource update notification to both the backend plugin event bus and
    the renderer plugin event stream."""
    now = time.monotonic()
    with _emit_lock:
        last = _last_emitted.get((server_name, uri), 0.0)
        if now - last < 0.05:
            return
        _last_emitted[(server_name, uri)] = now

    payload = {"server": server_name, "uri": uri}
    logger.info("MCP server '%s': resource updated: %s", server_name, uri)

    # 1. Backend plugin event bus (PluginManager._dispatch_event / ctx.subscribe)
    try:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        manager._dispatch_event("mcp:resources.updated", dict(payload))
    except Exception as exc:
        logger.debug("Failed to dispatch mcp:resources.updated to backend plugin bus: %s", exc)

    # 2. Renderer / desktop global event stream (host.onEvent via broadcast_plugin_event)
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("mcp", "resources.updated", dict(payload))
    except Exception as exc:
        logger.debug("Failed to broadcast plugin event 'plugin.mcp.resources.updated': %s", exc)


class _ModernSubscription:
    def __init__(self, task: asyncio.Task, cancel_event: asyncio.Event):
        self.task = task
        self.cancel_event = cancel_event

    async def cancel(self) -> None:
        self.cancel_event.set()
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


class _LegacySubscription:
    def __init__(self, server_name: str, session: Any, uri: str):
        self.server_name = server_name
        self.session = session
        self.uri = uri

    async def cancel(self) -> None:
        try:
            if self.session is not None and hasattr(self.session, "unsubscribe_resource"):
                await self.session.unsubscribe_resource(self.uri)
        except Exception as exc:
            logger.debug("MCP server '%s' unsubscribe error for %s: %s", self.server_name, self.uri, exc)


_active_subscriptions: Dict[Tuple[str, str], Any] = {}
_subscriptions_lock = threading.Lock()


def get_active_subscriptions() -> set[Tuple[str, str]]:
    with _subscriptions_lock:
        return set(_active_subscriptions.keys())


def _get_server(server_name: str) -> Optional[Any]:
    from tools import mcp_tool_discovery as _discovery
    return _discovery._get_connected_server_for_call(server_name)


def _get_connected_servers() -> List[Any]:
    with _core._lock:
        servers = list(_core._servers.values())
    return [s for s in servers if s is not None and s.session is not None]


def _render_resource_item(server_name: str, r: Any) -> dict:
    if isinstance(r, dict):
        uri = str(r.get("uri", ""))
        name = r.get("name") or uri
        desc = r.get("description")
        mime = r.get("mimeType") or r.get("mime_type")
    else:
        uri = str(getattr(r, "uri", ""))
        name = getattr(r, "name", "") or uri
        desc = getattr(r, "description", None)
        mime = mcp_field(r, "mime_type", "mimeType")
    return {
        "server": server_name,
        "uri": uri,
        "name": str(name),
        "description": str(desc) if desc is not None else None,
        "mimeType": str(mime) if mime else None,
    }


def _render_read_result(server_name: str, uri: str, result: Any) -> dict:
    raw_contents = result.get("contents", []) if isinstance(result, dict) else getattr(result, "contents", [])
    contents = []
    for block in raw_contents:
        if isinstance(block, dict):
            block_uri = str(block.get("uri") or uri)
            mime = block.get("mimeType") or block.get("mime_type")
            text = block.get("text")
            blob = block.get("blob")
        else:
            block_uri = str(getattr(block, "uri", uri))
            mime = getattr(block, "mime_type", getattr(block, "mimeType", None))
            text = getattr(block, "text", None)
            blob = getattr(block, "blob", None)
        item: dict[str, Any] = {"uri": block_uri}
        if mime:
            item["mimeType"] = str(mime)
        if text is not None:
            item["text"] = str(text)
        if blob is not None:
            item["blob"] = str(blob) if isinstance(blob, str) else blob.decode("latin1", errors="replace")
        contents.append(item)
    return {
        "server": server_name,
        "uri": uri,
        "contents": contents,
    }


async def _list_mcp_resources_inner(server_target: Any = None) -> List[dict]:
    from tools import mcp_tool_discovery as _discovery

    # 1. Direct server parameters passed (e.g. StdioServerParameters in test fixture)
    if server_target is not None and not isinstance(server_target, str):
        if hasattr(server_target, "command") and hasattr(server_target, "args"):
            from mcp import Client
            try:
                async with Client(server_target) as client:
                    return await _list_mcp_resources_inner(client.session)
            except BaseException as exc:
                cur = exc
                while getattr(cur, "exceptions", None):
                    cur = cur.exceptions[0]
                raise cur

        session = getattr(server_target, "session", server_target)
        lock = getattr(server_target, "_rpc_lock", None)
        server_name = getattr(server_target, "name", "mcp")
        if lock:
            async with lock:
                all_resources = await _core._paginate_full_list(session.list_resources, "resources", server_name)
        else:
            all_resources = await _core._paginate_full_list(session.list_resources, "resources", server_name)
        return [_render_resource_item(server_name, r) for r in all_resources]

    # 2. Specific connected server name
    if isinstance(server_target, str):
        server = _discovery._get_connected_server_for_call(server_target)
        if server is None or server.session is None:
            raise ValueError(f"MCP server '{server_target}' is not connected")
        async with server._rpc_lock:
            all_resources = await _core._paginate_full_list(server.session.list_resources, "resources", server_target)
        return [_render_resource_item(server_target, r) for r in all_resources]

    # 3. All connected servers
    results: List[dict] = []
    servers = _get_connected_servers()
    for srv in servers:
        try:
            async with srv._rpc_lock:
                all_resources = await _core._paginate_full_list(srv.session.list_resources, "resources", srv.name)
            results.extend(_render_resource_item(srv.name, r) for r in all_resources)
        except Exception as exc:
            logger.debug("Failed to list resources for '%s': %s", srv.name, exc)
    return results


async def list_mcp_resources_async(server_target: Any = None, *, server_name: Optional[str] = None) -> List[dict]:
    """List resources from connected MCP servers (async, schedules on _mcp_loop if needed)."""
    target = server_name if server_name is not None else server_target
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    loop = _loop._running_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and current_loop is not loop:
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(_list_mcp_resources_inner(target), loop)
        return await asyncio.wrap_future(future)
    return await _list_mcp_resources_inner(target)


def _run_mcp_coroutine(coro_fn, timeout: float = 30):
    """Run an async coroutine on the MCP loop, or current/new loop if MCP loop is down."""
    from tools import mcp_tool_loop as _loop
    loop = _loop._running_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and current_loop is not loop:
        return _loop._run_on_mcp_loop(coro_fn, timeout=timeout)
    if current_loop is not None and current_loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro_fn())).result(timeout=timeout)
    return asyncio.run(coro_fn())


def list_mcp_resources(server_target: Any = None, *, server_name: Optional[str] = None) -> List[dict]:
    """Sync wrapper to list resources from connected MCP servers."""
    target = server_name if server_name is not None else server_target
    return _run_mcp_coroutine(lambda: _list_mcp_resources_inner(target))


async def _read_mcp_resource_inner(uri: str, server_target: Any = None) -> dict:
    from tools import mcp_tool_discovery as _discovery

    # 1. Direct server parameters passed (e.g. StdioServerParameters in test fixture)
    if server_target is not None and not isinstance(server_target, str):
        if hasattr(server_target, "command") and hasattr(server_target, "args"):
            from mcp import Client
            try:
                async with Client(server_target) as client:
                    return await _read_mcp_resource_inner(uri, client.session)
            except BaseException as exc:
                cur = exc
                while getattr(cur, "exceptions", None):
                    cur = cur.exceptions[0]
                raise cur

        session = getattr(server_target, "session", server_target)
        lock = getattr(server_target, "_rpc_lock", None)
        server_name = getattr(server_target, "name", "mcp")
        if lock:
            async with lock:
                result = await session.read_resource(uri)
        else:
            result = await session.read_resource(uri)
        return _render_read_result(server_name, uri, result)

    # 2. Specific connected server name
    if isinstance(server_target, str):
        server = _discovery._get_connected_server_for_call(server_target)
        if server is None or server.session is None:
            raise ValueError(f"MCP server '{server_target}' is not connected")
        async with server._rpc_lock:
            result = await server.session.read_resource(uri)
        return _render_read_result(server_target, uri, result)

    # 3. All connected servers
    servers = _get_connected_servers()
    if not servers:
        raise ValueError("No connected MCP servers")

    last_exc = None
    for srv in servers:
        try:
            async with srv._rpc_lock:
                result = await srv.session.read_resource(uri)
            return _render_read_result(srv.name, uri, result)
        except Exception as exc:
            last_exc = exc
            continue
    raise ValueError(f"Failed to read resource '{uri}': {last_exc}")


async def read_mcp_resource_async(uri: str, server_target: Any = None, *, server_name: Optional[str] = None) -> dict:
    """Read resource content by URI (async, schedules on _mcp_loop if needed)."""
    target = server_name if server_name is not None else server_target
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    loop = _loop._running_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and current_loop is not loop:
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(_read_mcp_resource_inner(uri, target), loop)
        return await asyncio.wrap_future(future)
    return await _read_mcp_resource_inner(uri, target)


def read_mcp_resource(uri: str, server_target: Any = None, *, server_name: Optional[str] = None) -> dict:
    """Sync wrapper to read resource content by URI."""
    target = server_name if server_name is not None else server_target
    return _run_mcp_coroutine(lambda: _read_mcp_resource_inner(uri, target))


async def _subscribe_mcp_resource_inner(server_name: str, uri: str) -> dict:
    key = (server_name, uri)
    with _subscriptions_lock:
        if key in _active_subscriptions:
            return {"ok": True, "server": server_name, "uri": uri}

    server = _get_server(server_name)
    if server is None or server.session is None:
        raise ValueError(f"MCP server '{server_name}' is not connected")

    session = server.session
    protocol_version = str(getattr(session, "protocol_version", "") or "")
    from mcp.client.subscriptions import MODERN_PROTOCOL_VERSIONS
    is_modern = protocol_version in MODERN_PROTOCOL_VERSIONS or "2026" in protocol_version

    if is_modern:
        from mcp.client.subscriptions import listen
        cancel_event = asyncio.Event()
        started_event = asyncio.Event()

        async def _driver():
            try:
                async with listen(session, resource_subscriptions=[uri]) as sub:
                    started_event.set()
                    async for event in sub:
                        if cancel_event.is_set():
                            break
                        updated_uri = getattr(event, "uri", uri)
                        _emit_resource_update(server_name, str(updated_uri))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Listen stream for '%s' (%s) ended: %s", server_name, uri, exc)
            finally:
                started_event.set()

        task = asyncio.create_task(_driver())
        await started_event.wait()
        handle = _ModernSubscription(task, cancel_event)
    else:
        await session.subscribe_resource(uri)
        handle = _LegacySubscription(server_name, session, uri)

    with _subscriptions_lock:
        _active_subscriptions[key] = handle

    return {"ok": True, "server": server_name, "uri": uri}


async def subscribe_mcp_resource_async(server_name: str, uri: str) -> dict:
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    loop = _loop._running_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and current_loop is not loop:
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(_subscribe_mcp_resource_inner(server_name, uri), loop)
        return await asyncio.wrap_future(future)
    return await _subscribe_mcp_resource_inner(server_name, uri)


def subscribe_mcp_resource(server_name: str, uri: str) -> dict:
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    return _loop._run_on_mcp_loop(lambda: _subscribe_mcp_resource_inner(server_name, uri))


async def _unsubscribe_mcp_resource_inner(server_name: str, uri: str) -> dict:
    key = (server_name, uri)
    with _subscriptions_lock:
        handle = _active_subscriptions.pop(key, None)

    if handle is not None:
        await handle.cancel()

    return {"ok": True, "server": server_name, "uri": uri}


async def unsubscribe_mcp_resource_async(server_name: str, uri: str) -> dict:
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    loop = _loop._running_loop()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and current_loop is not loop:
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(_unsubscribe_mcp_resource_inner(server_name, uri), loop)
        return await asyncio.wrap_future(future)
    return await _unsubscribe_mcp_resource_inner(server_name, uri)


def unsubscribe_mcp_resource(server_name: str, uri: str) -> dict:
    from tools import mcp_tool_loop as _loop
    _loop._ensure_mcp_loop()
    return _loop._run_on_mcp_loop(lambda: _unsubscribe_mcp_resource_inner(server_name, uri))


async def cleanup_server_subscriptions_async(server_name: str) -> None:
    to_cancel = []
    with _subscriptions_lock:
        keys = [k for k in _active_subscriptions if k[0] == server_name]
        for k in keys:
            to_cancel.append(_active_subscriptions.pop(k))
    for handle in to_cancel:
        try:
            await handle.cancel()
        except Exception as exc:
            logger.debug("Error canceling subscription for '%s': %s", server_name, exc)


def cleanup_server_subscriptions(server_name: str) -> None:
    from tools import mcp_tool_loop as _loop
    loop = _loop._running_loop()
    if loop is not None and loop.is_running():
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            asyncio.create_task(cleanup_server_subscriptions_async(server_name))
        else:
            from agent.async_utils import safe_schedule_threadsafe
            safe_schedule_threadsafe(cleanup_server_subscriptions_async(server_name), loop)
