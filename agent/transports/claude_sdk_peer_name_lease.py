"""Single-owner leases for Claude peer names (``--name`` of a spawned CLI).

Incident 2026-09-25: an orphaned Claude CLI (ppid=1), spawned by a backend
that was shutting down, kept the SAME peer name as the live session's CLI, so
peers' native ListAgents/SendMessage saw two processes under one name and the
orphan sent a duplicate report.

The Claude CLI's peer registry (``$CLAUDE_CONFIG_DIR/sessions/<pid>.json``,
one file per process, ``name`` is just a field) does not enforce uniqueness,
so Hermes does it before each spawn: a lease file per name under
``<hermes root>/runtime/claude-peer-names/`` (shared by every profile) records the owning backend
(pid + start time), Hermes session id, session-object generation, and the CLI
child it spawned. All reads/writes share one file lock, so a rename (claim the
new name, drop the old one) is a single atomic step.

Claim rules:

* free, corrupt, or held by the same generation -> take it;
* holder backend is dead (or its pid was reused) -> stale: fence the CLI it
  recorded, then take it;
* holder is this same process and the same Hermes session -> a rotation
  superseding its own older session object -> take it;
* any other live holder -> refuse (the caller spawns without ``--name``).

Every successful claim also sweeps the CLI registry for orphaned SDK CLIs
(ppid=1, entrypoint ``sdk-*``, identity-checked) still registered under the
name: those are exactly the incident's shape, including orphans that predate
their lease record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("agent.transports.claude_agent_sdk_session")

_LEASE_DIR = "claude-peer-names"
_FENCE_GRACE_S = 3.0
# CLI registry ``startedAt`` vs psutil create_time: same process within this.
_REGISTRY_START_TOLERANCE_S = 10.0

Liveness = Callable[[int, Optional[float]], bool]
Fence = Callable[[str, Optional[dict]], None]


@dataclass(frozen=True)
class LeaseOwner:
    pid: int
    start: Optional[float]
    session_id: str
    generation: str

    @classmethod
    def current(cls, session_id: Optional[str], generation: str) -> "LeaseOwner":
        pid = os.getpid()
        return cls(pid=pid, start=_process_start(pid), session_id=str(session_id or ""),
                   generation=str(generation))


def _process_start(pid: int) -> Optional[float]:
    from hermes_cli.active_sessions import _process_start_time

    return _process_start_time(pid)


def _root(home: Any = None) -> Path:
    # Profiles share one Claude peer registry (~/.claude unless CLAUDE_CONFIG_DIR
    # moves it) and the default ``hermes:{title}`` name carries no profile, so the
    # lease namespace is the Hermes root, not the profile home: a "manager" session
    # in two profiles must not both register ``hermes:manager``.
    if home is None:
        from hermes_constants import get_default_hermes_root

        home = get_default_hermes_root()
    return Path(home) / "runtime" / _LEASE_DIR


def lease_path(name: str, *, home: Any = None) -> Path:
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return _root(home) / f"{digest}.json"


def _lock(home: Any):
    from hermes_cli.active_sessions import _FileLock

    return _FileLock(_root(home).parent / f"{_LEASE_DIR}.lock")


def _read(name: str, home: Any) -> Optional[dict]:
    try:
        record = json.loads(lease_path(name, home=home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("name") != name:
        return None
    return record


def _write(name: str, record: dict, home: Any) -> None:
    from utils import atomic_json_write

    atomic_json_write(lease_path(name, home=home), record, indent=None, sort_keys=True)


def _drop(name: str, home: Any) -> None:
    try:
        lease_path(name, home=home).unlink()
    except FileNotFoundError:
        pass


def _default_liveness(pid: int, start: Optional[float]) -> bool:
    from hermes_cli.active_sessions import _pid_liveness

    return bool(_pid_liveness(pid, start, lenient=True))


def _holds(record: Optional[dict], owner: LeaseOwner) -> bool:
    return bool(record) and record.get("generation") == owner.generation and record.get("pid") == owner.pid


def _claim_locked(name: str, owner: LeaseOwner, home: Any, liveness: Liveness) -> tuple[bool, Optional[dict]]:
    """(claimed, stale_record_to_fence). Caller holds the lock."""
    record = _read(name, home)
    stale: Optional[dict] = None
    if record is not None and not _holds(record, owner):
        try:
            holder_live = liveness(int(record.get("pid") or 0), record.get("start"))
        except Exception:
            holder_live = True  # unknowable: never steal a possibly-live name
        if holder_live:
            same_session_rotation = (
                int(record.get("pid") or 0) == owner.pid
                and str(record.get("session_id") or "") == owner.session_id
            )
            if not same_session_rotation:
                return False, None
        else:
            stale = record
    fresh = {
        "name": name,
        "pid": owner.pid,
        "start": owner.start,
        "session_id": owner.session_id,
        "generation": owner.generation,
        "claimed_at": time.time(),
    }
    if _holds(record, owner):
        fresh["cli_pid"] = record.get("cli_pid")
        fresh["cli_start"] = record.get("cli_start")
    _write(name, fresh, home)
    return True, stale


def claim(name: str, owner: LeaseOwner, *, home: Any = None,
          liveness: Optional[Liveness] = None, fence: Optional[Fence] = None) -> bool:
    """Claim ``name`` for ``owner``; True when it now holds the lease."""
    liveness = liveness or _default_liveness
    with _lock(home):
        claimed, stale = _claim_locked(name, owner, home, liveness)
    if claimed:
        _run_fence(fence, name, stale)
    return claimed


def move(old: str, new: str, owner: LeaseOwner, *, home: Any = None,
         liveness: Optional[Liveness] = None, fence: Optional[Fence] = None,
         keep_old: bool = False) -> bool:
    """Atomically claim ``new`` and drop ``old`` (when ``owner`` holds it).

    ``keep_old`` holds both names — used while a live CLI still carries the
    old name until it acknowledges ``/rename``; :func:`release` drops it then.
    On refusal nothing changes and the old lease stays with ``owner``."""
    liveness = liveness or _default_liveness
    with _lock(home):
        claimed, stale = _claim_locked(new, owner, home, liveness)
        if claimed and old and old != new and not keep_old:
            prior = _read(old, home)
            if _holds(prior, owner):
                current = _read(new, home) or {}
                if prior.get("cli_pid") and not current.get("cli_pid"):
                    current["cli_pid"] = prior.get("cli_pid")
                    current["cli_start"] = prior.get("cli_start")
                    _write(new, current, home)
                _drop(old, home)
    if claimed:
        _run_fence(fence, new, stale)
    return claimed


def record_cli(name: str, owner: LeaseOwner, *, cli_pid: Optional[int],
               cli_start: Optional[float] = None, home: Any = None) -> None:
    """Attach the spawned CLI's identity so a successor can fence it if we die."""
    if not cli_pid:
        return
    if cli_start is None:
        cli_start = _process_start(int(cli_pid))
    with _lock(home):
        record = _read(name, home)
        if _holds(record, owner):
            record["cli_pid"] = int(cli_pid)
            record["cli_start"] = cli_start
            _write(name, record, home)


def release(name: str, owner: LeaseOwner, *, home: Any = None) -> bool:
    """Drop the lease if ``owner`` (this exact generation) holds it."""
    with _lock(home):
        if not _holds(_read(name, home), owner):
            return False
        _drop(name, home)
        return True


def holder(name: str, *, home: Any = None) -> Optional[dict]:
    with _lock(home):
        return _read(name, home)


# ---------- fencing stale holders ----------


def _run_fence(fence: Optional[Fence], name: str, stale: Optional[dict]) -> None:
    try:
        (fence or default_fence)(name, stale)
    except Exception:
        logger.debug("claude peer-name fence failed for %r", name, exc_info=True)


def _process(pid: int) -> Any:
    import psutil

    try:
        return psutil.Process(int(pid))
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


def _is_claude(proc: Any) -> bool:
    try:
        names = [str(proc.name() or "")] + [str(a) for a in (proc.cmdline() or [])[:1]]
    except Exception:
        return False
    return any("claude" in os.path.basename(n).lower() for n in names if n)


def _terminate(proc: Any, why: str) -> None:
    import psutil

    try:
        proc.terminate()
        try:
            proc.wait(timeout=_FENCE_GRACE_S)
        except psutil.TimeoutExpired:
            if proc.is_running():
                proc.kill()
        logger.warning("claude-agent-sdk: fenced %s (pid %s) holding a Hermes peer name", why, proc.pid)
    except (psutil.Error, OSError):
        pass


def _config_dir() -> Path:
    configured = str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _orphaned_registrants(name: str) -> list[Any]:
    """Live, orphaned, SDK-spawned Claude CLIs registered under ``name``."""
    found = []
    try:
        entries = list((_config_dir() / "sessions").glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("name") != name:
            continue
        if not str(data.get("entrypoint") or "").startswith("sdk"):
            continue
        pid, started_ms = data.get("pid"), data.get("startedAt")
        if not isinstance(pid, int) or not isinstance(started_ms, (int, float)):
            continue
        proc = _process(pid)
        if proc is None:
            continue
        try:
            if proc.ppid() != 1 or not proc.is_running():
                continue
            if abs(float(proc.create_time()) - started_ms / 1000.0) > _REGISTRY_START_TOLERANCE_S:
                continue  # stale registry file; the pid now belongs to something else
        except Exception:
            continue
        if _is_claude(proc):
            found.append(proc)
    return found


def default_fence(name: str, stale: Optional[dict]) -> None:
    """Terminate the stale holder's recorded CLI and any orphaned registrant.

    Only processes that are provably the recorded CLI (pid + start time) or
    provably an orphaned SDK Claude CLI under this name are signalled; a
    recycled pid or a live-parented CLI is never touched."""
    if stale and stale.get("cli_pid"):
        proc = _process(int(stale["cli_pid"]))
        expected = stale.get("cli_start")
        try:
            identity_ok = proc is not None and proc.is_running() and (
                expected is not None and abs(float(proc.create_time()) - float(expected)) < 1.0
            )
        except Exception:
            identity_ok = False
        if identity_ok and proc.ppid() != os.getpid():
            _terminate(proc, "stale lease holder's CLI")
    for proc in _orphaned_registrants(name):
        _terminate(proc, "orphaned CLI")
