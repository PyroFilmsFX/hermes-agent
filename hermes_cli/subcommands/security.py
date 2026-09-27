"""``hermes security`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_json_flag


def build_security_parser(subparsers, *, cmd_security: Callable, enable_scrub: bool = False) -> None:
    """Attach the ``security`` subcommand to ``subparsers``."""
    security_parser = subparsers.add_parser(
        "security", help="Supply-chain audit (OSV.dev)",
        description="On-demand vulnerability scan against OSV.dev. Covers the Hermes "
            "venv (installed PyPI dists), Python deps declared by plugins under "
            "~/.hermes/plugins/, and pinned npx/uvx MCP servers in config.yaml. "
            "Does NOT scan globally-installed packages or editor/browser extensions.")
    security_subparsers = security_parser.add_subparsers(
        dest="security_command", metavar="<subcommand>")

    audit_parser = security_subparsers.add_parser(
        "audit", help="Run a one-shot supply-chain audit",
        description="Query OSV.dev for known vulnerabilities in installed components.")
    add_json_flag(audit_parser, "Emit machine-readable JSON instead of human-readable text")
    audit_parser.add_argument(
        "--fail-on", default="critical", choices=["low", "moderate", "high", "critical"],
        help="Exit non-zero when any finding meets this severity (default: critical)")
    audit_parser.add_argument(
        "--skip-venv", action="store_true", help="Skip scanning the Hermes Python venv")
    audit_parser.add_argument(
        "--skip-plugins", action="store_true", help="Skip scanning plugin requirements files")
    audit_parser.add_argument(
        "--skip-mcp", action="store_true", help="Skip scanning pinned MCP servers in config.yaml")
    audit_parser.set_defaults(func=cmd_security)

    # D14: stored-secret scrubbing remains parked until the owner decision is made.
    if enable_scrub:
        scrub_parser = security_subparsers.add_parser(
        "scrub", help="Find and mask secrets already stored at rest (dry run by default)",
        description="Scan composer pastes, attachments, transcripts, the gateway document "
            "cache, state.db and Hermes-created Claude SDK transcripts for stored secrets. "
            "Reports file/row, kind and count only, never values. --apply backs up and "
            "verifies the backup first, then masks in place; live sessions are deferred.")
        scope = scrub_parser.add_mutually_exclusive_group()
        scope.add_argument("--profile", metavar="NAME", help="Scrub this profile (default: the active one)")
        scope.add_argument("--all-profiles", action="store_true", help="Scrub the default profile and every named profile")
        scrub_parser.add_argument(
        "--targets", metavar="LIST",
        help="Comma list: pastes,attachments,transcripts,state-db,doc-cache,sdk-transcripts (default: all)")
        mode = scrub_parser.add_mutually_exclusive_group()
        mode.add_argument(
        "--apply", action="store_true",
        help="Mask in place after a verified backup; refused while Hermes is running "
             "(default is a dry run that writes nothing)")
        mode.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be masked and write nothing (the default; allowed while Hermes runs)")
        add_json_flag(scrub_parser, "Emit the report as JSON (no secret values)")
        scrub_parser.add_argument("--report", metavar="PATH", help="Also write the JSON report to a NEW file at PATH (0600; refused if it exists, is a symlink, or is inside HERMES_HOME or the Claude projects dir)")
        scrub_parser.add_argument(
        "--include-optouts", action="store_true",
        help="Also mask rows the user explicitly sent unmasked (secret_optout)")
        scrub_parser.add_argument(
        "--vacuum", action="store_true", help="VACUUM state.db afterwards when no other process holds it")
        scrub_parser.add_argument(
        "--status", action="store_true", help="Show the last run and the deferred-live ledger, then exit")
        scrub_parser.set_defaults(func=cmd_security)
    security_parser.set_defaults(func=cmd_security)
