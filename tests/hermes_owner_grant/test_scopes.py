"""U9: owner-grant scope grammar, classes, and catalog (addendum §6.3)."""

import json
import re
from pathlib import Path

import pytest

from hermes_owner_grant import scopes

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "hermes_owner_grant"
CATALOG_PATH = PACKAGE_DIR / "scopes.json"


@pytest.mark.parametrize(
    ("value", "scope_class", "name"),
    [
        ("conductor:allowlist:member-profile", "allowlist", "member-profile"),
        ("conductor:gate:review-budget-enable", "gate", "review-budget-enable"),
        ("conductor:marker:restore", "marker", "restore"),
        ("conductor:prod:fly-ord", "prod", "fly-ord"),
        # Grammar-valid future names remain recognizable but are not catalog entries.
        ("conductor:gate:future-name", "gate", "future-name"),
    ],
)
def test_parse_scope_validates_grammar_and_maps_namespace(value, scope_class, name):
    parsed = scopes.parse_scope(value)
    assert parsed.value == value
    assert parsed.scope_class == scope_class
    assert parsed.name == name
    assert parsed.is_catalogued == (value in scopes.CATALOG_BY_SCOPE)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "conductor:unknown:name",
        "conductor:gate:",
        "conductor:gate:-starts-with-hyphen",
        "conductor:gate:Uppercase",
        "conductor:gate:has_underscore",
        "conductor:gate:" + "a" * 64,
        "other:gate:name",
        "conductor:gate:name:extra",
        " conductor:gate:name",
    ],
)
def test_parse_scope_rejects_invalid_grammar_and_unknown_namespaces(value):
    with pytest.raises(scopes.ScopeError):
        scopes.parse_scope(value)


def test_scope_class_ttl_caps_and_defaults_match_decision_d8():
    assert scopes.MAX_TTL_MS_BY_CLASS == {
        "quote-only": 7 * 24 * 60 * 60 * 1000,
        "allowlist": 72 * 60 * 60 * 1000,
        "gate": 72 * 60 * 60 * 1000,
        "marker": 4 * 60 * 60 * 1000,
        "prod": 60 * 60 * 1000,
    }
    assert scopes.DEFAULT_TTL_MS_BY_CLASS == {
        "quote-only": 7 * 24 * 60 * 60 * 1000,
        "allowlist": 12 * 60 * 60 * 1000,
        "gate": 12 * 60 * 60 * 1000,
        "marker": 60 * 60 * 1000,
        "prod": 15 * 60 * 1000,
    }
    assert scopes.max_ttl_ms_for_scopes([]) == scopes.MAX_TTL_MS_BY_CLASS["quote-only"]
    assert (
        scopes.max_ttl_ms_for_scopes([
            "conductor:gate:review-budget-enable",
            "conductor:prod:fly-ord",
        ])
        == scopes.MAX_TTL_MS_BY_CLASS["prod"]
    )


@pytest.mark.parametrize(
    ("value", "single_use", "subject_required"),
    [
        ("conductor:allowlist:member-profile", False, False),
        ("conductor:gate:review-budget-enable", False, False),
        ("conductor:marker:restore", True, False),
        ("conductor:prod:fly-ord", True, True),
    ],
)
def test_scope_exposes_use_and_subject_policy(value, single_use, subject_required):
    parsed = scopes.parse_scope(value)
    assert parsed.single_use is single_use
    assert parsed.subject_required is subject_required


def test_catalog_is_schema_valid_sorted_and_uses_the_shared_grammar():
    raw = CATALOG_PATH.read_text(encoding="utf-8")
    catalog = json.loads(raw)
    assert set(catalog) == {"format", "classes", "scopes"}
    assert catalog["format"] == "hermes-owner-grant-scopes/v1"
    assert set(catalog["classes"]) == {
        "quote-only",
        "allowlist",
        "gate",
        "marker",
        "prod",
    }
    for scope_class, policy in catalog["classes"].items():
        assert set(policy) == {
            "default_ttl_ms",
            "max_ttl_ms",
            "single_use",
            "subject_required",
            "touch_id",
        }
        assert type(policy["default_ttl_ms"]) is int
        assert type(policy["max_ttl_ms"]) is int
        assert 0 < policy["default_ttl_ms"] <= policy["max_ttl_ms"]
        assert type(policy["single_use"]) is bool
        assert type(policy["subject_required"]) is bool
        assert policy["touch_id"] in ("never", "when_available")
        if scope_class == "prod":
            assert policy["single_use"] and policy["subject_required"]
            assert policy["touch_id"] == "when_available"
        else:
            assert not policy["subject_required"]

    entries = catalog["scopes"]
    assert entries == sorted(entries, key=lambda entry: entry["scope"])
    seen = set()
    for entry in entries:
        assert set(entry) == {"scope", "label"}
        assert re.fullmatch(
            r"conductor:(allowlist|gate|marker|prod):[a-z0-9][a-z0-9-]{0,62}",
            entry["scope"],
        )
        parsed = scopes.parse_scope(entry["scope"])
        assert parsed.scope_class in catalog["classes"]
        assert isinstance(entry["label"], str) and entry["label"]
        assert entry["scope"] not in seen
        seen.add(entry["scope"])

    assert raw.endswith("\n")
    assert (
        raw == json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    assert seen == set(scopes.CATALOG_BY_SCOPE)
