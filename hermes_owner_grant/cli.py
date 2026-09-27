"""Command-line interface for the owner-grant verifier (addendum §3.2-3.3, U7)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional, Sequence, TextIO

from . import anchor as anchor_mod
from . import envelope as envelope_mod
from . import scopes as scopes_mod
from . import verify as verify_mod

JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema",
        "ok",
        "reason",
        "detail",
        "tier",
        "grant",
        "match",
        "consumed",
        "candidates",
        "audit",
        "checked_at",
        "verifier",
    ],
    "properties": {
        "schema": {"const": "hermes-owner-verify/v1"},
        "ok": {"type": "boolean"},
        "reason": {"type": ["string", "null"]},
        "detail": {"type": ["string", "null"]},
        "tier": {"type": "string"},
        "grant": {"type": ["object", "null"]},
        "match": {"type": ["object", "null"]},
        "consumed": {"type": ["array", "null"]},
        "candidates": {"type": "integer", "minimum": 0},
        "audit": {"type": "boolean"},
        "checked_at": {"type": "integer", "minimum": 0},
        "verifier": {"type": "object"},
    },
    "additionalProperties": False,
}


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="hermes_owner_verify.py")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify")
    _add_request_args(verify, session_required=True)
    selector = verify.add_mutually_exclusive_group()
    selector.add_argument("--grant")
    selector.add_argument("--text-sha")
    selector.add_argument("--quote-stdin", action="store_true")
    verify.add_argument("--consume", action="store_true")
    verify.add_argument("--allow-fragment", action="store_true")
    verify.add_argument("--max-age", type=int)
    verify.add_argument("--audit-at", type=int)
    verify.add_argument("--include-text", action="store_true")

    listing = commands.add_parser("list")
    _add_request_args(listing, session_required=True)

    commands.add_parser("anchor-status")
    return parser


def _add_request_args(
    parser: argparse.ArgumentParser, *, session_required: bool
) -> None:
    parser.add_argument("--session", required=session_required)
    parser.add_argument("--claude-session")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--subject")


def _write(output: TextIO, value: Any) -> None:
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def _list_grants(args: Any, trusted: Any, uid: int, now_ms: int) -> dict:
    for scope in args.scope:
        scopes_mod.parse_scope(scope)
    names = sorted(os.listdir(trusted.grants_dir), reverse=True)
    grants = []
    inspected = 0
    for name in names:
        if envelope_mod.parse_grant_filename(name) is None:
            continue
        try:
            envelope = envelope_mod.read_envelope_file(
                os.path.join(trusted.grants_dir, name)
            )
        except envelope_mod.EnvelopeError:
            continue
        inspected += 1
        result = verify_mod.verify_envelope(
            envelope,
            session=args.session,
            claude_session=args.claude_session,
            uid=uid,
            now=now_ms,
            scopes=args.scope,
            subject=args.subject,
            anchor=trusted,
        )
        if result.ok:
            grants.append(result.to_dict()["grant"])
    return {
        "schema": verify_mod.SCHEMA,
        "ok": bool(grants),
        "reason": None if grants else verify_mod.REASON_NOT_FOUND,
        "grants": grants,
        "candidates": inspected,
    }


def _usage_result(message: str, reason: str = "usage_error") -> dict:
    return {
        "schema": verify_mod.SCHEMA,
        "ok": False,
        "reason": reason,
        "detail": message,
        "tier": "signed",
        "grant": None,
        "match": None,
        "consumed": None,
        "candidates": 0,
        "audit": False,
        "checked_at": 0,
        "verifier": {
            "version": verify_mod.VERSION,
            "impl": verify_mod.IMPL,
            "anchor_sha256": None,
        },
    }


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    anchor: Any = None,
    now_ms: Optional[int] = None,
    uid: Optional[int] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Run the CLI. Optional injected values are for trusted in-process callers and tests."""
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        _write(output_stream, _usage_result(str(exc)))
        return verify_mod.EXIT_USAGE

    if args.command == "anchor-status":
        try:
            loaded = anchor if anchor is not None else anchor_mod.load_trusted_anchor()
        except anchor_mod.AnchorError as exc:
            _write(
                output_stream,
                {
                    "schema": verify_mod.SCHEMA,
                    "ok": False,
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
            )
            return verify_mod.EXIT_ANCHOR
        _write(
            output_stream,
            {
                "schema": verify_mod.SCHEMA,
                "ok": True,
                "reason": None,
                "owner_uid": loaded.owner_uid,
                "anchor_sha256": loaded.sha256,
            },
        )
        return verify_mod.EXIT_OK

    try:
        caller_uid = os.getuid() if uid is None else uid
        checked_at = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        kwargs = {
            "session": args.session,
            "claude_session": args.claude_session,
            "uid": caller_uid,
            "now": checked_at,
            "scopes": args.scope,
            "subject": args.subject,
            "anchor": anchor,
        }
        if args.command == "verify":
            if args.quote_stdin:
                kwargs["quote"] = input_stream.read()
            kwargs.update(
                grant_id=args.grant,
                text_sha=args.text_sha,
                consume=args.consume,
                allow_fragment=args.allow_fragment,
                max_age_s=args.max_age,
                audit_at=args.audit_at,
                include_text=args.include_text,
            )
        else:
            trusted = anchor if anchor is not None else anchor_mod.load_trusted_anchor()
            body = _list_grants(args, trusted, caller_uid, checked_at)
            _write(output_stream, body)
            return verify_mod.EXIT_OK if body["ok"] else verify_mod.EXIT_NOT_FOUND
        result = verify_mod.verify(**kwargs)
    except verify_mod.VerifyUsageError as exc:
        _write(output_stream, _usage_result(str(exc)))
        return verify_mod.EXIT_USAGE
    except Exception as exc:
        _write(
            output_stream,
            _usage_result("%s: %s" % (type(exc).__name__, exc), "internal_error"),
        )
        return verify_mod.EXIT_INTERNAL
    _write(output_stream, result.to_dict())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
