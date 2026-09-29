"""Poll parked (usage-limited) Claude SDK sessions and continue them at their reset (D62).

The parks live in state.db (agent.claude_sdk_usage_park), so a backend restart
loses nothing: the next scheduler start re-reads them. A due park is handed to
``dispatch``; it is consumed only when a live session accepted the continue,
otherwise it stays parked until the desktop re-opens that session.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_POLL_SECONDS = 15.0
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def poll_once(dispatch: Callable[[dict], bool], *, now: float | None = None) -> list[str]:
    from agent import claude_sdk_usage_park as usage_park

    try:
        return usage_park.dispatch_due(dispatch, now=now)
    except Exception:
        logger.warning("usage-park poll failed", exc_info=True)
        return []


def ensure_started(dispatch: Callable[[dict], bool]) -> bool:
    """Start the single poller thread (idempotent). True when this call started it."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()

        def run() -> None:
            while not _stop.wait(_POLL_SECONDS):
                poll_once(dispatch)

        _thread = threading.Thread(target=run, name="usage-park-scheduler", daemon=True)
        _thread.start()
        return True


def stop() -> None:
    _stop.set()
