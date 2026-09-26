"""Child-process reaping and the private loop thread of ``ClaudeAgentSdkSession``.

PID ownership checks, the forced-kill ladder for a wedged CLI, and the
loop-thread/coroutine plumbing mixin. Extracted from ``claude_agent_sdk_session.py``;
every method resolves through ``ClaudeAgentSdkSession``'s MRO unchanged.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import threading
import weakref
from typing import Any, Optional

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.transports.claude_agent_sdk_session")


# Ceiling on the SDK transport's close ladder (5s stdin lock + 5s graceful +
# 5s SIGTERM + 5s SIGKILL), plus slack.
_SDK_DISCONNECT_TIMEOUT_S = 25.0


def _sdk_child_pid(client: Any) -> Optional[int]:
    """OS pid of the CLI subprocess behind an SDK client, if reachable."""
    try:
        proc = getattr(getattr(client, "_transport", None), "_process", None)
        pid = getattr(proc, "pid", None)
        return int(pid) if pid else None
    except Exception:
        return None


def _own_sdk_child_process(pid: int) -> Any:
    """Return the live psutil Process when ``pid`` is our direct child.

    psutil is Hermes' canonical cross-platform PID layer.  Keeping the
    ``Process`` object also protects the TERM→KILL ladder against PID reuse:
    psutil checks the process identity before destructive operations.
    """
    import psutil

    try:
        process = psutil.Process(int(pid))
        if process.ppid() != os.getpid():
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


def _is_own_sdk_child(pid: int) -> bool:
    """Guard against PID reuse: only reap a live child of this process."""
    return _own_sdk_child_process(pid) is not None


def _force_kill_sdk_child(pid: Optional[int], *, process: Any = None) -> None:
    """Last-resort reap when disconnect() times out and strands the CLI child."""
    if not pid:
        return
    if process is None:
        process = _own_sdk_child_process(pid)
    if process is None:
        return
    import psutil

    try:
        if (
            int(process.pid) != int(pid)
            or process.ppid() != os.getpid()
            or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return
        process.terminate()
    except (psutil.Error, OSError, TypeError, ValueError):
        return
    try:
        process.wait(timeout=5.0)
        logger.info("claude-agent-sdk stranded child %s reaped (terminate)", pid)
        return
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        pass
    except (psutil.Error, OSError):
        return
    try:
        # is_running() performs psutil's identity check, so a reused PID is
        # never killed as though it were the original CLI child.
        if process.is_running():
            process.kill()
            logger.warning(
                "claude-agent-sdk stranded child %s required forced kill", pid
            )
    except (psutil.NoSuchProcess, psutil.Error, OSError):
        pass


# ---------- process-wide shutdown fence + live-child registry ----------
#
# Incident 2026-09-25: a backend that was already shutting down spawned a
# fresh CLI child (auto-continue/mailbox resume racing teardown) and exited
# without terminating it; the orphan (ppid=1) replayed a turn and sent
# duplicate peer messages. Every session that owns a CLI registers here at
# the point it assigns its client; shutdown raises the fence (no new
# registrations → no new spawns) under the same lock it snapshots with, so
# a session is either reaped or refused, never both missed.

# SIGTERM→SIGKILL grace for the shutdown reap. The CLI exits on SIGTERM in
# well under a second; this only bounds a wedged child.
_SHUTDOWN_REAP_GRACE_S = 3.0
_SHUTDOWN_KILL_WAIT_S = 1.0

_live_lock = threading.Lock()
_live_sessions: "weakref.WeakSet[Any]" = weakref.WeakSet()
_shutdown_begun = False


class SdkShuttingDownError(RuntimeError):
    """A CLI spawn was refused because the Hermes backend is shutting down."""

    def __init__(self) -> None:
        super().__init__(
            "claude-agent-sdk: Hermes backend is shutting down; "
            "refusing to start a new Claude CLI session"
        )


def sdk_shutdown_begun() -> bool:
    return _shutdown_begun


def _admit_sdk_session(session: Any) -> bool:
    """Register a session that is about to own a CLI; False once shutdown began."""
    with _live_lock:
        if _shutdown_begun:
            return False
        _live_sessions.add(session)
        return True


def _forget_sdk_session(session: Any) -> None:
    with _live_lock:
        _live_sessions.discard(session)


def _live_sdk_children() -> list[tuple[int, Any]]:
    with _live_lock:
        sessions = list(_live_sessions)
    children: list[tuple[int, Any]] = []
    for session in sessions:
        pid = _sdk_child_pid(getattr(session, "_client", None))
        process = _own_sdk_child_process(pid) if pid else None
        if process is not None:
            children.append((pid, process))
    return children


def _process_gone(process: Any) -> bool:
    try:
        return not process.is_running()
    except Exception:
        return True


def begin_sdk_shutdown() -> None:
    """Raise the spawn fence and SIGTERM every live CLI child. Non-blocking.

    Idempotent. Call as early as possible in backend teardown; pair with
    :func:`reap_sdk_children` to wait out the grace and SIGKILL survivors.
    """
    global _shutdown_begun
    with _live_lock:
        _shutdown_begun = True
    for pid, process in _live_sdk_children():
        try:
            process.terminate()
        except Exception:
            logger.debug("claude-agent-sdk shutdown: SIGTERM %s failed", pid, exc_info=True)


def reap_sdk_children(grace: Optional[float] = None) -> int:
    """Fence spawns, then terminate and await every live CLI child.

    SIGTERM, wait up to ``grace`` seconds (shared deadline), SIGKILL the
    survivors and wait briefly on them. Returns the number of children
    signalled. Never raises.
    """
    import time

    import psutil

    budget = _SHUTDOWN_REAP_GRACE_S if grace is None else max(0.0, float(grace))
    try:
        begin_sdk_shutdown()
        children = _live_sdk_children()
        for _pid, process in children:
            with contextlib.suppress(Exception):
                process.terminate()
        deadline = time.monotonic() + budget
        survivors: list[tuple[int, Any]] = []
        for pid, process in children:
            if _process_gone(process):
                with contextlib.suppress(Exception):
                    process.wait(timeout=0)
                continue
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except psutil.TimeoutExpired:
                survivors.append((pid, process))
            except Exception:
                pass
        for pid, process in survivors:
            try:
                if not process.is_running():
                    continue
                process.kill()
                logger.warning(
                    "claude-agent-sdk shutdown: CLI child %s ignored SIGTERM; killed", pid
                )
                process.wait(timeout=_SHUTDOWN_KILL_WAIT_S)
            except Exception:
                pass
        if children:
            logger.info("claude-agent-sdk shutdown: reaped %d CLI child(ren)", len(children))
        return len(children)
    except Exception:
        logger.debug("claude-agent-sdk shutdown reap failed", exc_info=True)
        return 0


def _reset_sdk_shutdown_state_for_tests() -> None:
    global _shutdown_begun
    with _live_lock:
        _shutdown_begun = False
        _live_sessions.clear()


# Safety net for any process that ran an SDK session (serve, gateway, CLI):
# never leave a CLI child behind at interpreter exit. Explicit shutdown
# paths call reap_sdk_children() earlier; a second call is a cheap no-op.
atexit.register(reap_sdk_children)


class ClaudeSdkChildProcessMixin:
    """Private event-loop thread and coroutine bridge (see module docstring)."""

    # ---------- loop-thread plumbing ----------

    def _start_loop_thread(self) -> Any:
        lifecycle_lock = self._turn_callback_lock
        with lifecycle_lock:
            if self._loop_thread is not None:
                return self._loop, self._loop_thread
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run, name="claude-sdk-loop", daemon=True
        )
        thread.start()
        ready.wait(timeout=10)
        # Publish and snapshot both halves while holding the lifecycle lock.
        # If close fenced the session while the thread was being created, the
        # starter owns the local resources and must reap them itself.
        with lifecycle_lock:
            if self._retiring or self._closed or self._loop_thread is not None:
                publish = False
            else:
                self._loop = loop
                self._loop_thread = thread
                return loop, thread
        if not publish:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            thread.join(timeout=5.0)
        return None

    def _stop_loop_thread(self) -> None:
        # Snapshot and detach under the same lock used by startup publication;
        # the blocking stop/join work must remain outside it.
        lifecycle_lock = getattr(self, "_turn_callback_lock", None)
        with contextlib.nullcontext() if lifecycle_lock is None else lifecycle_lock:
            loop = self._loop
            thread = self._loop_thread
            self._loop = None
            self._loop_thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # pragma: no cover
                pass
        if thread is not None:
            thread.join(timeout=5)

    def _run_coro(self, coro: Any, *, timeout: float) -> Any:
        import concurrent.futures

        assert self._loop is not None, "loop thread not started"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            raise asyncio.TimeoutError(f"coroutine exceeded {timeout}s")
