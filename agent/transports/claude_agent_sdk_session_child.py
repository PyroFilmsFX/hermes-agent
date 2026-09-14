"""Child-process reaping and the private loop thread of ``ClaudeAgentSdkSession``.

PID ownership checks, the forced-kill ladder for a wedged CLI, and the
loop-thread/coroutine plumbing mixin. Extracted from ``claude_agent_sdk_session.py``;
every method resolves through ``ClaudeAgentSdkSession``'s MRO unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
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
