"""The secret-hygiene skill only names `hermes security scrub` invocations the CLI accepts."""

import argparse
import re
import shlex
from pathlib import Path

from hermes_cli.subcommands.security import build_security_parser

SKILL = Path(__file__).resolve().parents[2] / "skills" / "devops" / "secret-hygiene" / "SKILL.md"


def _parser(*, enable_scrub=False):
    parser = argparse.ArgumentParser()
    build_security_parser(parser.add_subparsers(dest="command"), cmd_security=lambda a: None,
                          enable_scrub=enable_scrub)
    return parser


def test_every_documented_scrub_command_parses():
    text = SKILL.read_text(encoding="utf-8")
    commands = set(re.findall(r"`?(hermes security scrub[^`|\n]*)`?", text))
    assert "hermes security scrub" in {c.strip() for c in commands}
    parser = _parser(enable_scrub=True)
    for command in commands:
        args = parser.parse_args(shlex.split(command.strip())[1:])
        assert args.security_command == "scrub"


def test_skill_defaults_to_dry_run_and_gates_apply_on_the_user():
    text = SKILL.read_text(encoding="utf-8")
    assert "parked and disabled by default" in text
    assert "Always start with the dry run" in text
    assert "only after the user explicitly asks" in text
    assert "never deletes" in text
