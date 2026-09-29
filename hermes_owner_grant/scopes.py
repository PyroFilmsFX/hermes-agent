"""Owner-grant scope grammar and class policy (addendum §6.3, U9).

The JSON catalog supplies dialog labels and shared policy data. Scope class is determined by
the grammar namespace, so a future grammar-valid name remains identifiable even before it is
added to the label catalog. Unknown namespaces and malformed names fail closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple

CATALOG_FORMAT = "hermes-owner-grant-scopes/v1"
_SCOPE_RE = re.compile(
    r"\Aconductor:"
    r"(allowlist|gate|marker|prod|answer|defer|override|gc|policy|spend|continuity)"
    r":([a-z0-9][a-z0-9-]{0,62})\Z"
)
_ORIGINAL_CLASSES = frozenset(("quote-only", "allowlist", "gate", "marker", "prod"))
# b9 owner-grant scopes: each of these classes authorizes one exact, owner-confirmed act, so the
# loader refuses a catalog that makes any of them reusable or subject-free.
SUBJECT_BOUND_CLASSES = frozenset((
    "answer",
    "defer",
    "override",
    "gc",
    "policy",
    "spend",
    "continuity",
))
_CLASS_NAMES = _ORIGINAL_CLASSES | SUBJECT_BOUND_CLASSES

# Per-scope signed-subject grammar. Each pattern must match the whole subject (re.fullmatch;
# the desktop mirrors use ``^(?:pattern)$`` with no flags, so every pattern here is written in
# the Python/JavaScript common subset). The shared vectors live in
# tests/fixtures/owner_grant_subject_vectors.json and a test pins them to this table.
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_HEX64 = r"[0-9a-f]{64}"
SUBJECT_GRAMMAR: Mapping[str, str] = {
    # <question_sha256>:<option_index>, 1-based, no leading zeros.
    "conductor:answer:stage-variant": _HEX64 + r":[1-9][0-9]*",
    # <old_claude_sid>:<new_claude_sid>; a rebind to the same id is not a rebind.
    "conductor:continuity:session-relaunch": "(" + _ID + r"):(?!\1$)" + _ID,
    # <build_id>:<wave_or_unit_id>
    "conductor:defer:wave-or-unit": _ID + ":" + _ID,
    # <dry_run_manifest_sha256>
    "conductor:gc:prune-lanes": _HEX64,
    # <build_id>:<wave>:<fan|recheck>
    "conductor:override:review-budget": _ID + ":" + _ID + ":(?:fan|recheck)",
    # <rule_id>:<rule_sha256>
    "conductor:policy:standing-approval": _ID + ":" + _HEX64,
    # <cli>
    "conductor:policy:unsandboxed-write": r"[a-z0-9][a-z0-9._-]{0,63}",
    # <fly_app>:<usd_cap>, a positive decimal: no leading zeros, at most 2 decimals, not 0.
    "conductor:spend:fly": (
        r"[a-z0-9][a-z0-9-]{0,62}:(?![0.]+$)(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?"
    ),
}
_CATALOG_KEYS = frozenset(("format", "classes", "scopes"))
_CLASS_POLICY_KEYS = frozenset((
    "default_ttl_ms",
    "max_ttl_ms",
    "single_use",
    "subject_required",
    "touch_id",
))
try:
    _BUNDLED_CATALOG_JSON
except NameError:
    _BUNDLED_CATALOG_JSON = ""


class ScopeError(ValueError):
    """A scope string or the bundled scope catalog is malformed."""


@dataclass(frozen=True)
class Scope:
    value: str
    scope_class: str
    name: str
    is_catalogued: bool
    single_use: bool
    subject_required: bool


def _load_catalog() -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Load and validate the bundled, environment-independent JSON catalog."""
    try:
        if _BUNDLED_CATALOG_JSON:
            catalog = json.loads(_BUNDLED_CATALOG_JSON)
        else:
            path = Path(__file__).with_name("scopes.json")
            with path.open("r", encoding="utf-8") as source:
                catalog = json.load(source)
    except (OSError, ValueError) as exc:
        raise ScopeError("cannot load scope catalog: %s" % exc) from None

    if not isinstance(catalog, dict) or set(catalog) != _CATALOG_KEYS:
        raise ScopeError("scope catalog has an invalid top-level shape")
    if catalog["format"] != CATALOG_FORMAT:
        raise ScopeError("scope catalog format is unsupported")
    classes = catalog["classes"]
    if not isinstance(classes, dict) or set(classes) != _CLASS_NAMES:
        raise ScopeError("scope catalog classes are invalid")
    for scope_class, policy in classes.items():
        if not isinstance(policy, dict) or set(policy) != _CLASS_POLICY_KEYS:
            raise ScopeError("scope class %r has an invalid policy" % scope_class)
        default_ttl = policy["default_ttl_ms"]
        max_ttl = policy["max_ttl_ms"]
        if (
            type(default_ttl) is not int
            or type(max_ttl) is not int
            or not 0 < default_ttl <= max_ttl
        ):
            raise ScopeError("scope class %r has invalid TTLs" % scope_class)
        if (
            type(policy["single_use"]) is not bool
            or type(policy["subject_required"]) is not bool
        ):
            raise ScopeError("scope class %r has invalid boolean policy" % scope_class)
        if policy["touch_id"] not in ("never", "when_available"):
            raise ScopeError("scope class %r has invalid Touch ID policy" % scope_class)
    if (
        classes["prod"]["single_use"] is not True
        or classes["prod"]["subject_required"] is not True
    ):
        raise ScopeError("prod scopes must be single-use and require a subject")
    for scope_class in sorted(SUBJECT_BOUND_CLASSES):
        if (
            classes[scope_class]["single_use"] is not True
            or classes[scope_class]["subject_required"] is not True
        ):
            raise ScopeError(
                "%s scopes must be single-use and require a subject" % scope_class
            )

    entries = catalog["scopes"]
    if not isinstance(entries, list):
        raise ScopeError("scope catalog entries must be a list")
    by_scope = {}
    previous = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"scope", "label"}:
            raise ScopeError("scope catalog entry has an invalid shape")
        value = entry["scope"]
        label = entry["label"]
        if not isinstance(value, str) or not isinstance(label, str) or not label:
            raise ScopeError("scope catalog entry has invalid values")
        if previous is not None and value <= previous:
            raise ScopeError("scope catalog entries must be unique and sorted")
        match = _SCOPE_RE.fullmatch(value)
        if match is None:
            raise ScopeError("catalog contains an invalid scope %r" % value)
        if match.group(1) not in classes:
            raise ScopeError("catalog scope has no class policy")
        previous = value
        by_scope[value] = entry
    for value in SUBJECT_GRAMMAR:
        if value not in by_scope:
            raise ScopeError("subject grammar names an uncatalogued scope %r" % value)
    return classes, by_scope


CLASS_POLICY, CATALOG_BY_SCOPE = _load_catalog()
DEFAULT_TTL_MS_BY_CLASS: Mapping[str, int] = {
    name: policy["default_ttl_ms"] for name, policy in CLASS_POLICY.items()
}
MAX_TTL_MS_BY_CLASS: Mapping[str, int] = {
    name: policy["max_ttl_ms"] for name, policy in CLASS_POLICY.items()
}
# Singular aliases make the intended class-level values easy to discover at call sites.
CLASS_DEFAULT_TTL_MS = DEFAULT_TTL_MS_BY_CLASS
CLASS_MAX_TTL_MS = MAX_TTL_MS_BY_CLASS


def parse_scope(value: str) -> Scope:
    """Validate one scope and return its class and use policy.

    Syntactically valid names can be used before their dialog label is added by PR; callers
    can use ``is_catalogued`` to distinguish those names. Unknown namespaces never pass.
    """
    if not isinstance(value, str):
        raise ScopeError("scope must be a string")
    match = _SCOPE_RE.fullmatch(value)
    if match is None:
        raise ScopeError("scope does not match the owner-grant grammar")
    scope_class, name = match.groups()
    policy = CLASS_POLICY.get(scope_class)
    if policy is None:
        raise ScopeError("scope namespace has no class policy")
    return Scope(
        value=value,
        scope_class=scope_class,
        name=name,
        is_catalogued=value in CATALOG_BY_SCOPE,
        single_use=policy["single_use"],
        subject_required=policy["subject_required"],
    )


def class_for_scope(value: str) -> str:
    """Return the policy class for a grammar-valid scope string."""
    return parse_scope(value).scope_class


def max_ttl_ms_for_scopes(values: Sequence[str]) -> int:
    """Return the tightest class maximum for a grant's scopes (quote-only if empty)."""
    if not values:
        return MAX_TTL_MS_BY_CLASS["quote-only"]
    return min(MAX_TTL_MS_BY_CLASS[class_for_scope(value)] for value in values)


def subject_matches_grammar(scope: str, subject: str) -> bool:
    """Whether ``subject`` fits ``scope``'s signed-subject grammar.

    Scopes without a registered grammar accept any subject here; the verifier's non-empty
    string check and exact-match rule still apply to them.
    """
    # Compiled on first use (re caches it), keeping the launcher's import cost flat.
    pattern = SUBJECT_GRAMMAR.get(scope)
    if pattern is None:
        return True
    return isinstance(subject, str) and re.fullmatch(pattern, subject) is not None
