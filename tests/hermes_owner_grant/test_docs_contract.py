"""The conductor contract documents the verifier's public wire contract."""

from __future__ import annotations

import re
from pathlib import Path

from hermes_owner_grant import verify


REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "website" / "docs" / "reference" / "owner-grants.md"


def test_owner_grants_doc_exit_codes_match_cli():
    text = DOC.read_text(encoding="utf-8")
    rows = dict(
        (int(code), name)
        for code, name in re.findall(r"^\|\s*(\d+)\s*\|\s*`?([a-z_]+)`?\s*\|", text, re.M)
    )
    expected = {
        verify.EXIT_OK: "ok",
        verify.EXIT_DENY: "deny",
        verify.EXIT_USAGE: "usage",
        verify.EXIT_ANCHOR: "anchor",
        verify.EXIT_NOT_FOUND: "not_found",
        verify.EXIT_INTERNAL: "internal",
        verify.EXIT_EVIDENCE: "evidence",
    }
    assert rows == expected


def test_owner_grants_doc_says_evidence_only_results_do_not_authorize():
    text = DOC.read_text(encoding="utf-8")
    assert verify.EXIT_EVIDENCE == 6
    assert "--allow-fragment" in text and "--audit-at" in text
    assert "evidence only" in text.lower()


def test_owner_grants_doc_covers_hook_trust_boundaries_and_amendments():
    text = DOC.read_text(encoding="utf-8").lower()
    for phrase in (
        "root-owned",
        "agent-supplied",
        "reimplement",
        "revoked.jsonl",
        "local capture",
        "scope grammar",
        "§7",
    ):
        assert phrase in text


def test_owner_grants_doc_warns_against_dev_fd_invocation():
    # /usr/bin/python3 -I -S /dev/fd/N exits 0 on macOS without running the script.
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "website/docs/reference/owner-grants.md").read_text()
    assert "/dev/fd" in text and "ok: true" in text
