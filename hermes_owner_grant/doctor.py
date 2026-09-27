"""Read-only diagnostics for the installed owner-grant verifier (U8)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from . import anchor as anchor_mod
from .builder import build

VERIFIER_PATH = "/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py"


def build_reproducible() -> tuple[bool, str]:
    """Build the standalone verifier twice and compare the resulting bytes."""
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-owner-doctor-") as directory:
            first = Path(directory) / "first.py"
            second = Path(directory) / "second.py"
            build(first)
            build(second)
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()
        if first_bytes != second_bytes:
            return False, "two builds produced different bytes"
        return True, hashlib.sha256(first_bytes).hexdigest()
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def _anchor_check() -> tuple[Any | None, str]:
    try:
        return anchor_mod.load_trusted_anchor(), "trusted"
    except anchor_mod.AnchorError as exc:
        return None, exc.reason


def run_doctor() -> dict[str, Any]:
    """Return JSON-compatible anchor, key, verifier, grants and build checks."""
    trusted, anchor_status = _anchor_check()
    checks: dict[str, Any] = {
        "anchor": anchor_status,
        "keys": (
            [{"kid": key.kid, "status": key.status} for key in trusted.keys]
            if trusted is not None
            else "unavailable"
        ),
        "verifier_sha256": "not_set",
        "grants_dir": "unavailable",
        "bundle": "unavailable",
    }
    ok = trusted is not None
    if trusted is not None:
        if trusted.verifier_sha256 is not None:
            try:
                digest = hashlib.sha256(Path(VERIFIER_PATH).read_bytes()).hexdigest()
                matches = digest == trusted.verifier_sha256
            except OSError:
                matches = False
            checks["verifier_sha256"] = "match" if matches else "mismatch"
            ok = ok and matches
        try:
            os.listdir(trusted.grants_dir)
            checks["grants_dir"] = "readable"
        except OSError:
            checks["grants_dir"] = "unreadable"
            ok = False

    reproducible, build_detail = build_reproducible()
    checks["bundle"] = "reproducible" if reproducible else "not_reproducible"
    ok = ok and reproducible
    return {
        "schema": "hermes-owner-doctor/v1",
        "ok": bool(ok),
        "checks": checks,
        "detail": None if reproducible else build_detail,
    }


def main(argv: list[str] | None = None) -> int:
    """Print one JSON report and return zero only when every check passes."""
    if argv not in (None, [], ["--json"]):
        print(json.dumps({"schema": "hermes-owner-doctor/v1", "ok": False,
                          "error": "doctor accepts no arguments"}, sort_keys=True))
        return 2
    result = run_doctor()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1
