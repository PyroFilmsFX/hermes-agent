"""The standalone verifier's package and scope catalog survive distribution."""

from __future__ import annotations

from pathlib import Path
import re


def test_package_discovery_includes_verifier_and_scope_catalog():
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = project.read_text(encoding="utf-8")

    assert re.search(r'^\s*"hermes_owner_grant",$', text, re.M)
    assert re.search(r'^hermes_owner_grant = \["scopes.json"\]$', text, re.M)
