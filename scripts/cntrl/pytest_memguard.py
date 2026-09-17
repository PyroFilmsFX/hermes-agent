"""Stop a pytest process before it takes the machine down.

2026-09-16: two bare ``pytest tests/agent tests/tools ...`` runs (one process
each, thousands of tests) grew to 52 GB and 47 GB and forced a reboot. macOS
does not enforce RLIMIT_AS / RLIMIT_RSS, so the process has to guard itself:
a daemon thread samples its own physical footprint (the number Activity
Monitor shows; ``ps`` RSS misses compressed pages) and exits hard past the
limit, naming the test that was running.

Loaded for every pytest run in this repo by the root ``conftest.py``.

  HERMES_TEST_MEM_LIMIT_GB  footprint limit in GB (default 8; 0 disables)
  HERMES_TEST_MEM_TRACE     optional path: one JSON line per test with the
                            footprint and open-fd count after it ran
"""

from __future__ import annotations

import ctypes
import heapq
import json
import os
import sys
import tempfile
import threading
import time

EXIT_CODE = 86
_DEFAULT_LIMIT_GB = 8.0
_POLL_SECONDS = 0.5
_GB = 1024**3

_current = "<collection>"
_before = 0
_read = None
_trace = None
_growth: list[tuple[int, str]] = []  # min-heap of the 10 largest per-test increases


def _footprint_darwin():
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(256)  # rusage_info_v2 is 152 bytes
    pid = os.getpid()

    def read() -> int:
        if libc.proc_pid_rusage(pid, 2, buf) != 0:  # RUSAGE_INFO_V2
            return 0
        # 16-byte uuid, then uint64 fields; ri_phys_footprint is the 8th.
        return int.from_bytes(buf.raw[72:80], "little")

    return read


def _footprint_linux():
    def read() -> int:
        with open("/proc/self/statm", encoding="utf-8") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")

    return read


def _reader():
    if sys.platform == "darwin":
        return _footprint_darwin()
    if sys.platform.startswith("linux"):
        return _footprint_linux()
    return None


def _open_fds() -> int:
    for d in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(d))
        except OSError:
            pass
    return -1


def _trip(config, used: int, limit: int) -> None:
    lines = [
        f"pytest_memguard: footprint {used / _GB:.1f} GB passed the "
        f"{limit / _GB:.1f} GB limit (HERMES_TEST_MEM_LIMIT_GB).",
        f"  running: {_current}",
        f"  open fds: {_open_fds()}",
        "  largest per-test growth so far:",
        *(f"    +{d / 1024**2:,.0f} MB  {n}" for d, n in sorted(_growth, reverse=True)),
        "  Run the suite with scripts/run_tests.sh (one process per file), not bare pytest on directories.",
    ]
    text = "\n".join(lines) + "\n"
    report = os.path.join(tempfile.gettempdir(), f"hermes-pytest-memguard-{os.getpid()}.txt")
    try:
        with open(report, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    try:  # fd 2 is usually pytest's capture file; restore the terminal first
        capman = config.pluginmanager.getplugin("capturemanager")
        if capman is not None:
            capman.suspend_global_capture(in_=True)
    except Exception:
        pass
    try:
        sys.stderr.write(text + f"  report: {report}\n")
        sys.stderr.flush()
    finally:
        os._exit(EXIT_CODE)


def _watch(config, read, limit: int) -> None:
    while True:
        used = read()
        if used > limit:
            _trip(config, used, limit)
        time.sleep(_POLL_SECONDS)


def pytest_configure(config) -> None:
    global _read, _trace
    _read = _reader()
    try:
        limit_gb = float(os.environ.get("HERMES_TEST_MEM_LIMIT_GB", _DEFAULT_LIMIT_GB))
    except ValueError:
        limit_gb = _DEFAULT_LIMIT_GB
    trace_path = os.environ.get("HERMES_TEST_MEM_TRACE")
    _trace = open(trace_path, "a", buffering=1, encoding="utf-8") if trace_path and _read else None
    if _read is None or limit_gb <= 0:
        return
    threading.Thread(
        target=_watch,
        args=(config, _read, int(limit_gb * _GB)),
        name="pytest-memguard",
        daemon=True,
    ).start()


def pytest_runtest_logstart(nodeid, location):
    global _current, _before
    _current = nodeid
    _before = _read() if _read else 0


def pytest_runtest_logfinish(nodeid, location):
    if _read is None:
        return
    after = _read()
    delta = after - _before
    if delta > 0:
        entry = (delta, nodeid)
        if len(_growth) < 10:
            heapq.heappush(_growth, entry)
        elif entry > _growth[0]:
            heapq.heapreplace(_growth, entry)
    if _trace is not None:
        _trace.write(json.dumps({"t": nodeid, "mb": after // 1024**2, "fds": _open_fds()}) + "\n")
