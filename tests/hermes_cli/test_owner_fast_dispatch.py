"""The owner verifier CLI runs before the Hermes CLI import graph."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hermes_cli.subcommands.owner import build_owner_parser


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_owner_verify_dispatch_does_not_import_heavy_cli_modules():
    code = """
import runpy, sys
sys.argv = ['hermes', 'owner', 'verify', 'anchor-status']
try:
    runpy.run_module('hermes_cli.main', run_name='__main__')
except SystemExit:
    pass
print('HEAVY:' + ','.join(sorted(name for name in sys.modules if name in {
    'hermes_cli.config', 'hermes_cli.main_agent_cmds', 'agent', 'run_agent',
    'model_tools', 'openai', 'yaml'
})))
"""
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    env.pop("HERMES_SESSION_ID", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    heavy_modules = [line for line in result.stdout.splitlines() if line.startswith("HEAVY:")]
    assert heavy_modules == ["HEAVY:"], result.stdout


def test_owner_is_registered_in_the_regular_cli_parser():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_owner_parser(subparsers)

    args = parser.parse_args(["owner", "verify", "--session", "session-id"])

    assert args.command == "owner"
    assert args.owner_action == "verify"
    assert args.args == ["--session", "session-id"]
    assert callable(args.func)


def test_owner_help_is_available_on_the_fast_path():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "owner", "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "verify" in result.stdout
    assert "doctor" in result.stdout
