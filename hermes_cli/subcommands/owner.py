"""The fast ``hermes owner`` command family."""

from __future__ import annotations

import sys
from typing import Sequence


def dispatch(argv: Sequence[str]) -> int:
    """Dispatch owner commands without importing the normal Hermes CLI stack."""
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: hermes owner {verify,list,anchor-status,doctor} ...")
        return 0
    command, *arguments = argv
    if command == "doctor":
        from hermes_owner_grant.doctor import main as doctor_main

        return doctor_main(arguments)
    if command in ("verify", "list", "anchor-status"):
        from hermes_owner_grant.cli import main as verifier_main

        return verifier_main([command, *arguments])
    print("hermes owner: unknown command %r" % command, file=sys.stderr)
    return 2


def build_owner_parser(subparsers) -> None:
    """Register owner for the regular parser/help tree as well as fast dispatch."""
    import argparse

    parser = subparsers.add_parser("owner", help="Verify owner-granted decisions")
    parser.add_argument("owner_action", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parser.set_defaults(
        func=lambda args: dispatch(
            [args.owner_action, *args.args] if args.owner_action else ["--help"]
        )
    )
