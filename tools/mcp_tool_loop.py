"""Background-loop plumbing for tools.mcp_tool: cross-process discovery file lock, scheduling
coroutines onto the MCP loop from caller threads (with profile HOME override and dashboard OAuth
flow propagation) and the loop's exception handler. Origin state (``_lock``, ``_mcp_loop``) is
read through ``_core`` so ``mock.patch("tools.mcp_tool.X")`` keeps working."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import os
import threading
import time
from typing import Any, Coroutine, Optional
from tools.mcp_tool_common import _core
from tools import mcp_tool_lifecycle as _lifecycle

logger = logging.getLogger("tools.mcp_tool")
MCP_TASK_POLL_INTERVAL_S = 5.0
# A pending row whose server stays disconnected this long is parked with ``expired_at`` (kept, never
# deleted) so the poller can exit; ``mcp.task_server_gone_expiry_s`` overrides it.
MCP_TASK_SERVER_GONE_EXPIRY_S = 3600.0
# Consecutive failed poll passes before the poller gives up (it restarts on the next task / loop start).
_MCP_TASK_POLL_MAX_FAILURES = 5
# Undelivered, unexpired: the only rows the poller works on.
_PENDING_TASK_WHERE = "delivered_at IS NULL AND expired_at IS NULL"
_mcp_task_backoff: dict[tuple[str, str], tuple[float, float]] = {}
# Only touched on the MCP loop thread (start, poll, drain all run there).
_mcp_task_pollers: dict[str, asyncio.Task] = {}
# (server, task_id) -> monotonic time the poller first found that row's server disconnected. In
# memory on purpose: after a process restart a reconnecting server gets a fresh window rather than
# having its rows expired before it has finished connecting.
_mcp_task_server_missing_since: dict[tuple[str, str], float] = {}
# A row whose CONNECTED server keeps failing tasks/get or tasks/result (e.g. it lost its task state)
# is expired after this many consecutive failed RPCs, or once this long has passed since the first
# of them, whichever comes first (``mcp.task_rpc_failure_max`` / ``mcp.task_rpc_failure_expiry_s``).
MCP_TASK_RPC_FAILURE_MAX = 10
MCP_TASK_RPC_FAILURE_EXPIRY_S = 600.0
# (server, task_id) -> (consecutive failed RPCs, monotonic time of the first). In memory, like
# ``_mcp_task_server_missing_since``: a success clears it; a process restart starts it fresh.
_mcp_task_rpc_failures: dict[tuple[str, str], tuple[int, float]] = {}


class _LockCookie:
    """Holds a cross-process file lock; ``release()`` drops it. The file object MUST stay open while
    held: both fcntl and portalocker locks are tied to the descriptor."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        # Best effort: an unlock/close failure must never propagate out of discovery.
        with contextlib.suppress(Exception):
            if os.name == "posix":
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            else:
                import portalocker
                portalocker.unlock(self._fh)
        with contextlib.suppress(Exception):
            self._fh.close()
        self._fh = None


def _acquire_lock_on_fh(fh: Any) -> bool:
    """Non-blocking exclusive lock (fcntl on POSIX, portalocker elsewhere). False when another process
    holds it; unexpected errors propagate so the caller can treat locking as unavailable."""
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        return True
    import portalocker
    try:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.LockException:
        return False
    return True


def _try_acquire_mcp_discovery_lock() -> Any:
    """``_LockCookie`` (acquired), ``None`` (held by another process) or ``_LOCK_UNAVAILABLE``
    (locking broken: run discovery unguarded)."""
    # The cached path lives on the ORIGIN module (tests reset ``tools.mcp_tool._MCP_DISCOVERY_LOCK_PATH``).
    # A routed profile (multiplexed gateway) locks under ITS home: the launch profile's lock file
    # would serialize discovery across profiles and never coordinate with B's own single-profile processes.
    from tools import mcp_tool as _origin
    try:
        from hermes_constants import get_hermes_home, get_hermes_home_override
        if get_hermes_home_override() is not None:
            lock_path = str(get_hermes_home() / ".mcp-discovery.lock")
        else:
            if _origin._MCP_DISCOVERY_LOCK_PATH is None:
                _origin._MCP_DISCOVERY_LOCK_PATH = str(get_hermes_home() / ".mcp-discovery.lock")
            lock_path = _origin._MCP_DISCOVERY_LOCK_PATH
        fh = open(lock_path, "w", encoding="utf-8")
    except Exception:
        return _core._LOCK_UNAVAILABLE
    try:
        acquired = _acquire_lock_on_fh(fh)
    except Exception:
        fh.close()
        return _core._LOCK_UNAVAILABLE
    if acquired:
        return _LockCookie(fh)
    fh.close()
    return None


def _mcp_loop_exception_handler(loop, context):
    """Suppress the benign 'Event loop is closed' RuntimeError httpx finalizers raise against the
    dead loop during shutdown; forward the rest."""
    exc = context.get("exception")
    if not (isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)):
        loop.default_exception_handler(context)


def _wrap_with_home_override(coro: "Coroutine") -> "Coroutine":
    """Carry the caller's context-local HERMES_HOME override into ``coro`` (task-local on the MCP
    loop, so concurrent scopes don't interfere)."""
    try:
        from hermes_constants import get_hermes_home_override, reset_hermes_home_override, set_hermes_home_override
        home_override = get_hermes_home_override()
    except Exception:
        home_override = None
    if not home_override:
        return coro

    async def _scoped():
        token = set_hermes_home_override(home_override)
        try:
            return await coro
        finally:
            reset_hermes_home_override(token)

    return _scoped()


def _wrap_with_dashboard_oauth_flow(coro):
    """Propagate a dashboard OAuth flow onto the dedicated MCP loop task."""
    try:
        from tools.mcp_dashboard_oauth import dashboard_oauth_flow, get_dashboard_oauth_flow
        flow = get_dashboard_oauth_flow()
    except Exception:
        flow = None
    if flow is None:
        return coro

    async def _scoped():
        with dashboard_oauth_flow(flow):
            return await coro

    return _scoped()


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The MCP loop when it is up, else None (read under ``_lock``)."""
    with _core._lock:
        loop = _core._mcp_loop
    return loop if loop is not None and loop.is_running() else None


def _run_on_mcp_loop(coro_or_factory, timeout: float = 30):
    """Schedule a coroutine (or zero-arg factory — avoids leaking a never-awaited coroutine when the
    loop is down) on the MCP loop and block until done, polling so user interrupts are honored."""
    from tools.interrupt import is_interrupted
    from agent.async_utils import safe_schedule_threadsafe

    loop = _running_loop()
    if loop is None:
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")
    # run_coroutine_threadsafe copies the LOOP thread's context, so a per-request profile scope
    # would vanish here; re-establish it inside the task's own context.
    coro = _wrap_with_dashboard_oauth_flow(_wrap_with_home_override(
        coro_or_factory() if callable(coro_or_factory) else coro_or_factory))
    future = safe_schedule_threadsafe(coro, loop, logger=logger, log_message="MCP scheduling failed")
    if future is None:
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    start_time = time.monotonic()
    deadline = None if timeout is None else start_time + timeout
    while True:
        if is_interrupted():
            future.cancel()
            raise InterruptedError("User sent a new message")
        remaining = 0.1 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            future.cancel()
            raise TimeoutError(f"MCP call timed out after {time.monotonic() - start_time:.1f}s "
                               f"(configured timeout: {float(timeout):.1f}s)")
        try:
            return future.result(timeout=min(0.1, remaining))
        except concurrent.futures.TimeoutError:
            # Aliases builtin TimeoutError, so it also fires for the coroutine's own timeout: a done
            # future must yield its outcome.
            if future.done():
                return future.result()


def _signal_reconnect(server: Any) -> bool:
    """Ask a server task to rebuild its transport, thread-safely: the event lives on the MCP loop,
    so set via ``call_soon_threadsafe`` when it runs (direct ``.set()`` otherwise). False when the
    server has no reconnect machinery."""
    event = getattr(server, "_reconnect_event", None)
    if event is None:
        return False
    loop = _core._mcp_loop
    if isinstance(event, asyncio.Event) and loop is not None and loop.is_running():
        loop.call_soon_threadsafe(event.set)
    else:
        event.set()
    return True


def reconnect_mcp_server(server_name: str) -> bool:
    """Ask a currently-live MCP server to rebuild after external re-auth."""
    from tools.mcp_tool_scope import _resolve_server_key
    with _core._lock:
        server = _core._servers.get(_resolve_server_key(server_name))
    return server is not None and _signal_reconnect(server)


def _wait_for_server_session_ready(srv: Any, *, old_session: Any = None, timeout: float = 15.0) -> bool:
    """Poll until the server exposes a usable, ready session (during a reconnect ``srv.session`` is
    briefly None or stale; retrying blindly burns breaker strikes). With ``old_session`` the observed
    session must differ. Iteration-bounded, not deadline-bounded: tests freeze ``time.monotonic``."""
    iterations = max(1, int(max(float(timeout), 0.0) / 0.25))
    for i in range(iterations):
        session = getattr(srv, "session", None)
        ready = getattr(srv, "_ready", None)
        try:
            is_ready = bool(ready.is_set()) if hasattr(ready, "is_set") else True
        except Exception:
            is_ready = True
        if session is not None and session is not old_session and is_ready:
            return True
        if i < iterations - 1:
            time.sleep(0.25)
    return False


def _signal_reconnect_and_wait(server_name: str, srv: Any, *, op_description: str, timeout: float = 15.0) -> bool:
    """Request a transport rebuild and wait for the fresh session. ``_ready`` is cleared on the loop
    BEFORE ``_reconnect_event`` is set, else the readiness poll returns at once on the dead session."""
    loop = _core._mcp_loop
    if loop is None or not loop.is_running():
        return False

    def _request_reconnect() -> None:
        ready, reconnect_event = getattr(srv, "_ready", None), getattr(srv, "_reconnect_event", None)
        if hasattr(ready, "clear"):
            ready.clear()
        if hasattr(reconnect_event, "set"):
            reconnect_event.set()

    old_session = getattr(srv, "session", None)
    logger.info("MCP server '%s': %s requesting transport reconnect", server_name, op_description)
    loop.call_soon_threadsafe(_request_reconnect)
    return _wait_for_server_session_ready(srv, old_session=old_session, timeout=timeout)


def _ensure_mcp_loop():
    """Start the background loop thread if not running. The loop/thread handles live on the ORIGIN
    module (tests read and reset ``tools.mcp_tool._mcp_loop``), so they are written there."""
    from tools import mcp_tool as _origin
    with _core._lock:
        if _origin._mcp_loop is None or not _origin._mcp_loop.is_running():
            loop = _origin._mcp_loop = asyncio.new_event_loop()
            loop.set_exception_handler(_mcp_loop_exception_handler)
            _origin._mcp_thread = threading.Thread(target=loop.run_forever, name="mcp-event-loop", daemon=True)
            _origin._mcp_thread.start()
    # Existing undelivered rows need to resume after a process restart, but the polling task is
    # profile-scoped and should not exist for profiles with no outstanding tasks.
    _ensure_mcp_task_poller_if_pending()


def _task_poll_interval() -> float:
    try:
        from hermes_cli.config import load_config_readonly
        value = float((load_config_readonly().get("mcp") or {}).get("task_poll_interval_s", MCP_TASK_POLL_INTERVAL_S))
        return max(0.1, value)
    except (TypeError, ValueError, AttributeError):
        return MCP_TASK_POLL_INTERVAL_S


def _task_server_gone_expiry() -> float:
    try:
        from hermes_cli.config import load_config_readonly
        value = float((load_config_readonly().get("mcp") or {}).get(
            "task_server_gone_expiry_s", MCP_TASK_SERVER_GONE_EXPIRY_S))
        return max(0.0, value)
    except (TypeError, ValueError, AttributeError):
        return MCP_TASK_SERVER_GONE_EXPIRY_S


def _task_rpc_failure_limits() -> tuple[int, float]:
    """(max consecutive failed RPCs, max seconds since the first) before a row is expired."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("mcp") or {}
        count = int(cfg.get("task_rpc_failure_max", MCP_TASK_RPC_FAILURE_MAX))
        window = float(cfg.get("task_rpc_failure_expiry_s", MCP_TASK_RPC_FAILURE_EXPIRY_S))
        return max(1, count), max(0.0, window)
    except (TypeError, ValueError, AttributeError):
        return MCP_TASK_RPC_FAILURE_MAX, MCP_TASK_RPC_FAILURE_EXPIRY_S


def _task_db():
    from hermes_state import SessionDB
    db = SessionDB()
    db.ensure_peer_mailbox()
    return db


def _profile_task_key() -> str:
    from hermes_constants import hermes_home_key
    return hermes_home_key()


def _has_pending_mcp_tasks() -> bool:
    db = _task_db()
    try:
        return db._read_one(f"SELECT 1 FROM mcp_pending_tasks WHERE {_PENDING_TASK_WHERE} LIMIT 1") is not None
    finally:
        db.close()


def _ensure_mcp_task_poller_if_pending() -> None:
    """Start one poller for the active profile only when its DB has pending task rows. The poller
    polls MCP-loop-owned sessions under their ``_rpc_lock``, so it is created ON ``_mcp_loop`` and
    nowhere else: a caller on another running loop (``@resource`` expansion runs on the agent /
    gateway loop) hands the start over with ``call_soon_threadsafe``. With the MCP loop down this is
    a no-op; ``_ensure_mcp_loop`` calls it again once the loop is up."""
    try:
        if not _has_pending_mcp_tasks():
            return
        key = _profile_task_key()
    except Exception:
        logger.debug("Unable to check for pending MCP tasks", exc_info=True)
        return
    from tools import mcp_tool as _origin
    loop = _origin._mcp_loop
    if loop is None or not loop.is_running():
        return
    try:  # the start runs on the loop thread, where the caller's profile override is not set
        from hermes_constants import get_hermes_home_override
        home_override = get_hermes_home_override()
    except Exception:
        home_override = None

    def start() -> None:
        task = _mcp_task_pollers.get(key)
        if task is None or task.done():
            _mcp_task_pollers[key] = loop.create_task(_poll_mcp_tasks_scoped(home_override), name="_poll_mcp_tasks")

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        start()
        return
    with contextlib.suppress(RuntimeError):  # the loop closed between the check and the hand-off
        loop.call_soon_threadsafe(start)


async def _poll_mcp_tasks_scoped(home_override) -> None:
    """``_poll_mcp_tasks`` under the starting caller's profile override (task-local on the MCP loop)."""
    if not home_override:
        return await _poll_mcp_tasks()
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home_override)
    try:
        return await _poll_mcp_tasks()
    finally:
        reset_hermes_home_override(token)


def _expire_mcp_task(db, row: dict, gone_for: float, *, reason: Optional[str] = None) -> None:
    """Park a row that can no longer be polled: ``expired_at`` is set, the row stays."""
    now = time.time()
    db._execute_write(lambda conn: conn.execute(
        "UPDATE mcp_pending_tasks SET expired_at = COALESCE(expired_at, ?) "
        "WHERE server = ? AND task_id = ? AND delivered_at IS NULL", (now, row["server"], row["task_id"])))
    key = (row["server"], row["task_id"])
    _mcp_task_server_missing_since.pop(key, None)
    _mcp_task_backoff.pop(key, None)
    _mcp_task_rpc_failures.pop(key, None)
    if reason is None:
        reason = "server '%s' has been disconnected for %.0fs" % (row["server"], gone_for)
    logger.warning("MCP task %s/%s expired: %s; the row is kept with expired_at set and is no longer polled",
                   row["server"], row["task_id"], reason)


def _enqueue_task_wake(db, row: dict, result: Any) -> None:
    import json
    from agent.secret_hygiene import mask_ingress_text
    from tui_gateway.session_mailbox import schedule_drain

    serialized = json.dumps(result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result,
                            ensure_ascii=False, default=str)
    body, _ = mask_ingress_text(f'MCP task result (untrusted data):\n"""\n{serialized}\n"""')
    now = time.time()
    dedupe = f"mcp-task:{row['server']}:{row['task_id']}"

    def write(conn):
        conn.execute("UPDATE mcp_pending_tasks SET completed_at = COALESCE(completed_at, ?) "
                     "WHERE server = ? AND task_id = ?", (now, row["server"], row["task_id"]))
        conn.execute(
            "INSERT OR IGNORE INTO peer_mailbox "
            "(target_session_id, from_label, body, dedupe_key, status, created_at) "
            "VALUES (?, 'MCP task', ?, ?, 'queued', ?)",
            (row["session_id"], body, dedupe, now))
        conn.execute("UPDATE mcp_pending_tasks SET delivered_at = COALESCE(delivered_at, ?) "
                     "WHERE server = ? AND task_id = ?", (now, row["server"], row["task_id"]))
    db._execute_write(write)
    schedule_drain(str(row["session_id"]), None)


class _PollPass:
    """What one poll pass did: ``failed`` row RPCs that raised, ``progressed`` rows that had a
    successful RPC or reached a terminal state (delivered or expired)."""
    __slots__ = ("failed", "progressed")

    def __init__(self) -> None:
        self.failed = 0
        self.progressed = 0

    @property
    def stalled(self) -> bool:
        return self.failed > 0 and self.progressed == 0


async def _poll_mcp_tasks_once() -> _PollPass:
    import mcp.types as types
    from pydantic import TypeAdapter
    from tools import mcp_tool as core

    outcome = _PollPass()
    db = _task_db()
    try:
        rows = [dict(row) for row in db._read_all(
            f"SELECT * FROM mcp_pending_tasks WHERE {_PENDING_TASK_WHERE} ORDER BY created_at")]
        with core._lock:
            servers = {str(server.name): server for server in core._servers.values() if server.session is not None}
        expiry = _task_server_gone_expiry()
        for row in rows:
            key = (row["server"], row["task_id"])
            server = servers.get(row["server"])
            if server is None:
                # Parked until the server reconnects, bounded: past the window the row expires so a
                # server that never comes back cannot keep the poller alive forever.
                now = time.monotonic()
                gone_for = now - _mcp_task_server_missing_since.setdefault(key, now)
                if gone_for >= expiry:
                    _expire_mcp_task(db, row, gone_for)
                    outcome.progressed += 1
                continue
            _mcp_task_server_missing_since.pop(key, None)  # reconnected inside the window: resume
            retry_at, delay = _mcp_task_backoff.get(key, (0.0, MCP_TASK_POLL_INTERVAL_S))
            if time.monotonic() < retry_at:
                continue
            try:
                async with server._rpc_lock:
                    status = await server.session.send_request(
                        types.GetTaskRequest(params=types.GetTaskRequestParams(taskId=row["task_id"])),
                        types.GetTaskResult)
                    if status.status != "completed":
                        _mcp_task_backoff.pop(key, None)
                        _mcp_task_rpc_failures.pop(key, None)
                        outcome.progressed += 1
                        continue
                    result = await server.session.send_request(
                        types.GetTaskPayloadRequest(params=types.GetTaskPayloadRequestParams(taskId=row["task_id"])),
                        TypeAdapter(dict[str, Any]))
                _enqueue_task_wake(db, row, result)
                _mcp_task_backoff.pop(key, None)
                _mcp_task_rpc_failures.pop(key, None)
                outcome.progressed += 1
            except Exception:
                outcome.failed += 1
                logger.debug("MCP task poll failed for %s/%s", *key, exc_info=True)
                # A connected server that keeps rejecting the row (lost task state, unknown id) would
                # otherwise keep it pending, and the poller alive, forever: bound it per row.
                now = time.monotonic()
                count, first = _mcp_task_rpc_failures.get(key, (0, now))
                count += 1
                max_failures, window = _task_rpc_failure_limits()
                if count >= max_failures or now - first >= window:
                    _expire_mcp_task(db, row, 0.0, reason=(
                        "server '%s' is connected but failed %d consecutive task polls over %.0fs"
                        % (row["server"], count, now - first)))
                    outcome.progressed += 1
                    continue
                _mcp_task_rpc_failures[key] = (count, first)
                delay = min(max(delay * 2, MCP_TASK_POLL_INTERVAL_S), 300.0)
                _mcp_task_backoff[key] = (now + delay, delay)
    finally:
        db.close()
    return outcome


async def _poll_mcp_tasks() -> None:
    """Poll until nothing is pending. Every exit is bounded: rows of a vanished server expire, rows a
    connected server keeps failing expire, a failing pending-check stops at once and repeated failed
    passes (raised, or every row RPC failed) stop after a few tries (the next persisted task or MCP
    loop start brings the poller back)."""
    key = _profile_task_key()
    current = asyncio.current_task()
    failures = 0
    try:
        while True:
            try:
                outcome = await _poll_mcp_tasks_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                if failures >= _MCP_TASK_POLL_MAX_FAILURES:
                    logger.warning("MCP task poller stopping after %d consecutive failed polls", failures,
                                   exc_info=True)
                    return
                logger.debug("MCP task poller failed", exc_info=True)
            else:
                # A pass whose row RPCs all failed is a failed poll too, even though the pass itself
                # returned; a pass that only skipped backed-off rows neither counts nor resets.
                if outcome is not None and outcome.stalled:
                    failures += 1
                    if failures >= _MCP_TASK_POLL_MAX_FAILURES:
                        logger.warning("MCP task poller stopping after %d consecutive failed polls (every "
                                       "task RPC failed); pending rows resume on the next task or MCP "
                                       "loop start", failures)
                        return
                elif outcome is None or outcome.progressed:
                    failures = 0
            try:
                if not _has_pending_mcp_tasks():
                    return
            except Exception:
                logger.warning("MCP task poller stopping: the pending-task check failed", exc_info=True)
                return
            await asyncio.sleep(_task_poll_interval())
    finally:
        if current is not None and _mcp_task_pollers.get(key) is current:
            _mcp_task_pollers.pop(key, None)


def _stop_mcp_loop(*, only_if_idle: bool = False) -> bool:
    """Stop the background event loop and join its thread."""
    from tools import mcp_tool as _origin
    with _core._lock:
        if only_if_idle and (_core._servers or _core._server_connecting):
            logger.debug("Leaving MCP event loop running; active servers are registered or connecting")
            return False
        loop, thread = _origin._mcp_loop, _origin._mcp_thread
        _origin._mcp_loop = _origin._mcp_thread = None
    if loop is None:
        return True
    # Drain before stopping: tasks still suspended when the loop closes get resumed by the GC
    # against a closed loop. shutdown_mcp_servers only reaps _servers; everything else ends here.
    future = None
    # Drain before stopping: closing the loop with tasks still suspended leaves their coroutines for the GC,
    # whose finalizer then resumes them to run cleanup against a loop that is already closed -> "Event loop
    # is closed" (#60197).
    if loop.is_running():
        from agent.async_utils import safe_schedule_threadsafe

        future = safe_schedule_threadsafe(
            _lifecycle._drain_and_stop_mcp_loop(), loop, logger=logger,
            log_message="MCP loop drain: failed to schedule", log_level=logging.WARNING)
        if future is not None:
            try:
                future.result(timeout=_core._MCP_LOOP_DRAIN_TIMEOUT + 1)
            except TimeoutError:
                logger.warning("Timed out waiting for MCP loop drain after %.1fs", _core._MCP_LOOP_DRAIN_TIMEOUT + 1)
            except BaseException as exc:
                logger.warning("Error draining MCP loop tasks: %s", exc)
    elif not loop.is_closed():
        try:
            loop.run_until_complete(_lifecycle._drain_mcp_loop_tasks(timeout=_core._MCP_LOOP_DRAIN_TIMEOUT))
        except BaseException as exc:
            logger.warning("Error draining stopped MCP loop tasks: %s", exc)
    if future is None and loop.is_running():  # drain-and-stop wasn't scheduled: stop it ourselves
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=5)
        if thread.is_alive():
            logger.warning("MCP event loop thread did not stop within 5.0s")
    try:
        loop.close()
    except Exception as exc:
        logger.warning("Unable to close MCP event loop cleanly: %s", exc)
    # The loop is gone, so no session can be in flight: reap active too.
    _lifecycle._kill_orphaned_mcp_children(include_active=True)
    return True
