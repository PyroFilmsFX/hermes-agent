#!/usr/bin/env python3
"""Verify the repo venv still has what Hermes needs to run and be tested.

Why this exists: a build lane that runs ``uv run`` / ``uv sync`` in the MAIN
checkout rebuilds .venv from whatever it resolves — wrong Python, no extras —
and silently strips claude-agent-sdk, psycopg, psutil and pytest. It has
happened twice (2026-09-13 twice in one day). The damage is invisible until a
test run dies with "No module named pytest" or a live SDK session fails to
close with "No module named psutil", so this check is the tripwire.

Usage:
    .venv/bin/python scripts/cntrl/venv_guard.py          # report + exit 1 if broken
    .venv/bin/python scripts/cntrl/venv_guard.py --quiet  # exit code only
"""
from __future__ import annotations

import argparse
import configparser
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
VENV = REPO / ".venv"

# Import name -> why it must be there. Keep this list short and load-bearing:
# every entry is something whose absence has actually broken a run.
REQUIRED = {
    "pytest": "the test suite cannot run at all",
    "claude_agent_sdk": "the SDK lane (our default runtime) cannot start",
    "psycopg": "memory/pgvector paths fail at import",
    "psutil": "live SDK sessions raise on close (child-process reaping)",
}

RESTORE = (
    "uv sync --extra claude-agent-sdk --extra dev --inexact\n"
    '  uv pip install "psycopg[binary]" psycopg-pool'
)


def expected_python() -> str | None:
    """The Python the project pins, from .python-version (authoritative here)."""
    pin = REPO / ".python-version"
    if pin.is_file():
        return pin.read_text().strip() or None
    return None


def venv_python() -> str | None:
    cfg = VENV / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    parser = configparser.ConfigParser()
    # pyvenv.cfg has no section header; synthesize one.
    parser.read_string("[v]\n" + cfg.read_text())
    return parser["v"].get("version_info") or parser["v"].get("version")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args()

    problems: list[str] = []

    if not VENV.is_dir():
        problems.append(".venv is missing entirely")
    else:
        want, have = expected_python(), venv_python()
        # Compare on major.minor only: the pin is "3.11", pyvenv.cfg may say
        # "3.11.9". A rebuild on the wrong minor is the failure we care about.
        if want and have and not have.startswith(want):
            problems.append(
                f"venv Python is {have}, project pins {want} "
                "(a lane rebuilt .venv on the wrong interpreter)"
            )

    for module, why in REQUIRED.items():
        if importlib.util.find_spec(module) is None:
            problems.append(f"missing {module} — {why}")

    if not problems:
        if not args.quiet:
            print(f"venv ok: Python {venv_python()}, all {len(REQUIRED)} required packages present")
        return 0

    if not args.quiet:
        print("venv is BROKEN:")
        for p in problems:
            print(f"  - {p}")
        print("\nRestore with:\n  " + RESTORE)
        print("\nThen re-run this check. Never run `uv` in the main checkout from a")
        print("build lane — give the lane its own worktree (see CLAUDE.md).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
