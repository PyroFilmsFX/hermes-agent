"""The owner-input hold (b9): owner input preempts queued peer/background turns, and Stop holds the
auto-started chain. This module makes that hold bounded and self-releasing.

Two things hold auto-started work (peer mailbox rows, notifications, auto-continue) back:

* **owner pending**: an owner RPC is still claiming its turn (``_owner_submit_waiting``), or an owner
  message sits in the server queue waiting for its boundary;
* **Stop hold** (``_owner_stop_hold``): the owner pressed Stop; nothing auto-started runs until the owner
  speaks again.

Both used to be unbounded. Live 2026-09-30 (session 20260924_200208_c68a80): an owner forward queued at
00:14:31 behind a host turn that never saw its own result kept peer rows 1108 and 1114 refused with
"owner input pending or Stop hold active" for 44 and 41 minutes. The hold now releases:

* on delivery (the owner entry is drained, or the owner's own turn claims the session);
* on failure (the RPC returns and its ``_owner_submit_waiting`` count drops; a Stop that discards a queued
  owner message reports it to the client as failed, never silently);
* when the turn ends (the post-turn drain delivers the owner entry first);
* after ``TIMEOUT_S`` at the latest, with a WARNING log line. A timed-out owner entry stops holding peers
  but is NOT dropped: it stays first in line, and the backstop timer delivers it at the next boundary even
  when the CLI never reports one (a woken turn that never settles).

Peer rows stay durably queued in ``peer_mailbox`` while the hold is up and are drained when it releases.
Nothing here writes a user turn or injects into a running loop: an owner message only ever runs as its own
turn at a boundary, so prompt caching and role alternation are untouched.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# The longest any owner hold may keep auto-started work back. Long enough for a normal owner turn to
# claim the session; short enough that a stuck hold can't park the session's mail for half an hour.
TIMEOUT_S = 300.0
_BACKSTOP_SLACK_S = 0.05


def _session_label(session: dict) -> str:
    return str(session.get("session_key") or "?")


def _is_owner_entry(entry: Any) -> bool:
    return (isinstance(entry, dict) and entry.get("display_kind") in (None, "owner_forward")
            and not str(entry.get("rid") or "").startswith("peer-mailbox:"))


def _queued_entries(session: dict) -> list:
    head = session.get("queued_prompt")
    return ([head] if head else []) + list(session.get("queued_prompts") or [])


def _owner_entries(session: dict) -> list[dict]:
    return [entry for entry in _queued_entries(session) if _is_owner_entry(entry)]


def _age(since: Any, now: float) -> float:
    try:
        return now - float(since)
    except (TypeError, ValueError):
        return 0.0


# ── Stop hold ──────────────────────────────────────────────────────────────────────────────────────


def set_stop_hold(session: dict) -> None:
    """Stop holds the auto-started chain. Caller holds ``history_lock``."""
    session["_owner_stop_hold"] = True
    session["_owner_stop_hold_at"] = time.time()
    arm_backstop(session)


def stop_hold_active(session: dict) -> bool:
    """The Stop hold, released (with a log line) once it has been up for ``TIMEOUT_S``."""
    if not session.get("_owner_stop_hold"):
        return False
    now = time.time()
    since = session.get("_owner_stop_hold_at")
    if since is None:
        # Set by a path that predates the stamp: start its clock now so it still expires.
        session["_owner_stop_hold_at"] = now
        arm_backstop(session)
        return True
    age = _age(since, now)
    if age < TIMEOUT_S:
        return True
    session["_owner_stop_hold"] = False
    session.pop("_owner_stop_hold_at", None)
    logger.warning("owner hold: Stop hold on session %s released after %.0fs with no owner message "
                   "(bound %.0fs); queued peer mail and background work may run again",
                   _session_label(session), age, TIMEOUT_S)
    _schedule_release_drain(session)
    return False


# ── owner pending ──────────────────────────────────────────────────────────────────────────────────


def owner_submit_started(session: dict) -> None:
    """An owner RPC began claiming its turn. Caller holds ``history_lock``."""
    count = int(session.get("_owner_submit_waiting", 0)) + 1
    session["_owner_submit_waiting"] = count
    if count == 1:
        session["_owner_submit_waiting_since"] = time.time()


def owner_submit_finished(session: dict) -> None:
    """The owner RPC returned (queued, claimed, refused or raised). Caller holds ``history_lock``."""
    remaining = int(session.get("_owner_submit_waiting", 0)) - 1
    if remaining > 0:
        session["_owner_submit_waiting"] = remaining
        return
    session.pop("_owner_submit_waiting", None)
    session.pop("_owner_submit_waiting_since", None)
    session.pop("_owner_submit_waiting_expired", None)


def note_owner_queued(session: dict) -> None:
    """Start the owner-queue hold clock (if not already running). Caller holds ``history_lock``.

    The clock lives on the session, not on the queue entries: entries are compared and re-built by the
    queue code, and a delivered owner entry restarts the clock for whatever owner input is still queued."""
    session.setdefault("_owner_queued_since", time.time())
    arm_backstop(session)


def owner_entry_delivered(session: dict) -> None:
    """An owner entry claimed its turn: any owner input still queued gets a fresh bound."""
    session.pop("_owner_queued_since", None)
    session.pop("_owner_queued_released", None)


def owner_pending(session: dict) -> bool:
    """True while fresh owner input must win admission over auto-started work.

    An RPC still claiming its turn or an owner entry queued less than ``TIMEOUT_S`` ago holds. Past the bound
    the hold is released with a log line; the owner entry itself stays queued, first in line."""
    now = time.time()
    if session.get("_owner_submit_waiting"):
        since = session.setdefault("_owner_submit_waiting_since", now)
        if _age(since, now) < TIMEOUT_S:
            return True
        if not session.get("_owner_submit_waiting_expired"):
            session["_owner_submit_waiting_expired"] = True
            logger.warning("owner hold: owner submit on session %s has not claimed its turn after %.0fs "
                           "(bound %.0fs); releasing the hold on queued peer mail",
                           _session_label(session), _age(since, now), TIMEOUT_S)
            _schedule_release_drain(session)
    if not _owner_entries(session):
        owner_entry_delivered(session)
        return False
    since = session.setdefault("_owner_queued_since", now)
    if _age(since, now) < TIMEOUT_S:
        return True
    if not session.get("_owner_queued_released"):
        session["_owner_queued_released"] = True
        logger.warning("owner hold: owner message queued on session %s for %.0fs is still waiting for a turn "
                       "boundary (bound %.0fs); releasing the hold on queued peer mail, the owner message stays "
                       "first in line", _session_label(session), _age(since, now), TIMEOUT_S)
        _schedule_release_drain(session)
    return False


def overdue_owner_entry(session: dict) -> bool:
    """An owner message has waited past the bound: it must not keep waiting for a CLI boundary."""
    since = session.get("_owner_queued_since")
    return bool(since is not None and _owner_entries(session) and _age(since, time.time()) >= TIMEOUT_S)


# ── release + backstop ─────────────────────────────────────────────────────────────────────────────


def _schedule_release_drain(session: dict) -> None:
    """Drain the session's durable peer mail once the hold is gone (timer thread; never under a lock)."""
    key = str(session.get("session_key") or "")
    if not key:
        return
    with contextlib.suppress(Exception):
        from tui_gateway.session_mailbox import schedule_drain
        schedule_drain(key, session.get("profile_home"))


def arm_backstop(session: dict) -> None:
    """One timer per session: at the bound, re-evaluate the hold, deliver a parked owner message at the
    boundary it was waiting for, then drain peer mail. Re-arms while a fresher hold is still up."""
    if session.get("_owner_hold_backstop"):
        return
    try:
        timer = threading.Timer(TIMEOUT_S + _BACKSTOP_SLACK_S, _fire_backstop, args=(session,))
        timer.daemon = True
        timer.start()
    except Exception:  # noqa: BLE001 - no timer thread: the lazy bound and the mailbox retry loop still release
        logger.debug("owner hold: backstop timer unavailable for %s", _session_label(session), exc_info=True)
        return
    session["_owner_hold_backstop"] = timer


def cancel_backstop(session: dict) -> None:
    timer = session.pop("_owner_hold_backstop", None)
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.cancel()


def _live_sid(session: dict) -> str | None:
    from tui_gateway import server

    with server._sessions_lock:
        return next((sid for sid, live in server._sessions.items() if live is session), None)


def _fire_backstop(session: dict) -> None:
    session.pop("_owner_hold_backstop", None)
    try:
        run_backstop(session)
    except Exception:  # noqa: BLE001 - a timer thread must never die silently
        logger.warning("owner hold: backstop failed for session %s", _session_label(session), exc_info=True)


def run_backstop(session: dict) -> None:
    """Evaluate (and so expire) the hold, then run the boundary the owner message was waiting for."""
    if session.get("_closing") or session.get("_finalized"):
        return
    sid = _live_sid(session)
    if sid is None:
        return
    lock = session.get("history_lock") or contextlib.nullcontext()
    with lock:
        stop_held = stop_hold_active(session)
        pending = owner_pending(session)
        parked_owner = bool(_owner_entries(session))
    if parked_owner and not session.get("running"):
        logger.warning("owner hold: session %s idle with an owner message still queued; delivering it now",
                       _session_label(session))
    from tui_gateway.session_mailbox import _sdk_turn_boundary

    # Delivers a queued owner message first (owner priority), then drains peer mail unless a hold remains.
    _sdk_turn_boundary(sid, session)
    if stop_held or pending or (parked_owner and session.get("running")):
        arm_backstop(session)
