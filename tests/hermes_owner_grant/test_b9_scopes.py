"""b9 owner-grant scopes: the new classes, catalog labels and signed-subject grammar.

The subject grammar vectors are shared with the desktop mirrors through
tests/fixtures/owner_grant_subject_vectors.json, so the fixture drives these tests and a test
pins the fixture's patterns to ``scopes.SUBJECT_GRAMMAR``.
"""

import hashlib
import json
from pathlib import Path

import pytest

from hermes_owner_grant import scopes

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "hermes_owner_grant" / "scopes.json"
VECTORS_PATH = REPO_ROOT / "tests" / "fixtures" / "owner_grant_subject_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

MIN = 60 * 1000
HOUR = 60 * MIN

# brief §1: class -> (touch_id, default TTL, max TTL); all single-use and subject-required.
NEW_CLASSES = {
    "answer": ("never", HOUR, 4 * HOUR),
    "defer": ("never", HOUR, 4 * HOUR),
    "override": ("never", HOUR, 4 * HOUR),
    "gc": ("never", HOUR, HOUR),
    "policy": ("when_available", 15 * MIN, HOUR),
    "spend": ("when_available", 15 * MIN, HOUR),
    "continuity": ("never", 15 * MIN, 15 * MIN),
}

# brief §2: scope -> label.
NEW_SCOPES = {
    "conductor:answer:stage-variant": "Answer a conductor stage question",
    "conductor:defer:wave-or-unit": "Defer a planned wave or unit",
    "conductor:policy:standing-approval": "Enable a standing-approval rule",
    "conductor:policy:unsandboxed-write": "Allow unsandboxed writes for a CLI",
    "conductor:override:review-budget": "Override the review budget",
    "conductor:gc:prune-lanes": "Prune reviewed lanes",
    "conductor:spend:fly": "Start paid Fly lanes",
    "conductor:continuity:session-relaunch": "Rebind a session's marker after a relaunch",
}

# The pre-b9 classes, frozen: b9 must not change any of them.
EXISTING_CLASSES = {
    "allowlist": {
        "default_ttl_ms": 12 * HOUR,
        "max_ttl_ms": 72 * HOUR,
        "single_use": False,
        "subject_required": False,
        "touch_id": "never",
    },
    "gate": {
        "default_ttl_ms": 12 * HOUR,
        "max_ttl_ms": 72 * HOUR,
        "single_use": False,
        "subject_required": False,
        "touch_id": "never",
    },
    "marker": {
        "default_ttl_ms": HOUR,
        "max_ttl_ms": 4 * HOUR,
        "single_use": True,
        "subject_required": False,
        "touch_id": "never",
    },
    "prod": {
        "default_ttl_ms": 15 * MIN,
        "max_ttl_ms": HOUR,
        "single_use": True,
        "subject_required": True,
        "touch_id": "when_available",
    },
    "quote-only": {
        "default_ttl_ms": 7 * 24 * HOUR,
        "max_ttl_ms": 7 * 24 * HOUR,
        "single_use": False,
        "subject_required": False,
        "touch_id": "never",
    },
}
EXISTING_SCOPES = {
    "conductor:allowlist:member-profile": "Allowlisted member profile",
    "conductor:gate:job-store-write-block": "Block job store writes",
    "conductor:gate:lane-test-budget-enable": "Enable lane test budget",
    "conductor:gate:pr-discipline-enable": "Enable PR discipline",
    "conductor:gate:review-budget-enable": "Enable review budget",
    "conductor:marker:bypass": "Bypass marker",
    "conductor:marker:rebind-owner": "Rebind marker owner",
    "conductor:marker:repoint-ledger": "Repoint marker ledger",
    "conductor:marker:restore": "Restore marker",
    "conductor:prod:target": "Production target",
}


def normalize_question_body(raw):
    """The stage-question hash rule, written out independently of any production code."""
    text = raw.replace("\r\n", "\n")
    return text.strip(" \t\n")


def _grammar_cases(kind):
    return [
        pytest.param(scope, value, id="%s-%d" % (scope.rsplit(":", 1)[-1], i))
        for scope, entry in sorted(VECTORS["scopes"].items())
        for i, value in enumerate(entry[kind])
    ]


# -- classes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("scope_class", sorted(NEW_CLASSES))
def test_each_new_class_loads_with_the_brief_policy(scope_class):
    touch_id, default_ttl, max_ttl = NEW_CLASSES[scope_class]
    assert scopes.CLASS_POLICY[scope_class] == {
        "default_ttl_ms": default_ttl,
        "max_ttl_ms": max_ttl,
        "single_use": True,
        "subject_required": True,
        "touch_id": touch_id,
    }
    assert scopes.DEFAULT_TTL_MS_BY_CLASS[scope_class] == default_ttl
    assert scopes.MAX_TTL_MS_BY_CLASS[scope_class] == max_ttl
    assert scope_class in scopes.SUBJECT_BOUND_CLASSES
    parsed = scopes.parse_scope("conductor:%s:some-future-name" % scope_class)
    assert parsed.scope_class == scope_class
    assert parsed.single_use is True and parsed.subject_required is True
    assert parsed.is_catalogued is False


def test_subject_bound_classes_are_exactly_the_new_classes():
    assert scopes.SUBJECT_BOUND_CLASSES == frozenset(NEW_CLASSES)


def test_existing_classes_and_scopes_are_unchanged():
    for scope_class, policy in EXISTING_CLASSES.items():
        assert scopes.CLASS_POLICY[scope_class] == policy
    for value, label in EXISTING_SCOPES.items():
        assert scopes.CATALOG_BY_SCOPE[value]["label"] == label
    assert set(scopes.CLASS_POLICY) == set(EXISTING_CLASSES) | set(NEW_CLASSES)
    assert set(scopes.CATALOG_BY_SCOPE) == set(EXISTING_SCOPES) | set(NEW_SCOPES)
    # Existing classes carry no subject grammar: their subjects keep the old rules.
    for value in EXISTING_SCOPES:
        assert value not in scopes.SUBJECT_GRAMMAR
    assert scopes.subject_matches_grammar("conductor:prod:target", "sha256:" + "c" * 64)


@pytest.mark.parametrize(
    "value",
    [
        "conductor:answers:stage-variant",
        "conductor:Answer:stage-variant",
        "conductor:answer:",
        "conductor:answer:stage_variant",
        "conductor:spend:fly:extra",
        "conductor:continuity",
    ],
)
def test_scope_grammar_still_rejects_near_misses(value):
    with pytest.raises(scopes.ScopeError):
        scopes.parse_scope(value)


# -- catalog ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", sorted(NEW_SCOPES))
def test_catalog_carries_each_new_scope_with_its_exact_label(value):
    entry = scopes.CATALOG_BY_SCOPE[value]
    assert entry == {"scope": value, "label": NEW_SCOPES[value]}
    parsed = scopes.parse_scope(value)
    assert parsed.is_catalogued is True
    assert parsed.scope_class in NEW_CLASSES
    assert parsed.single_use is True and parsed.subject_required is True


def test_catalog_file_stays_sorted_unique_and_canonical():
    raw = CATALOG_PATH.read_text(encoding="utf-8")
    catalog = json.loads(raw)
    values = [entry["scope"] for entry in catalog["scopes"]]
    assert values == sorted(values)
    assert len(values) == len(set(values))
    assert raw == json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _load_with(monkeypatch, mutate):
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    mutate(catalog)
    monkeypatch.setattr(scopes, "_BUNDLED_CATALOG_JSON", json.dumps(catalog))
    return scopes._load_catalog()


def test_the_unmodified_catalog_loads_through_the_bundled_path(monkeypatch):
    classes, by_scope = _load_with(monkeypatch, lambda catalog: None)
    assert set(classes) == set(EXISTING_CLASSES) | set(NEW_CLASSES)
    assert set(by_scope) == set(EXISTING_SCOPES) | set(NEW_SCOPES)


@pytest.mark.parametrize("scope_class", sorted(NEW_CLASSES))
@pytest.mark.parametrize("flag", ["single_use", "subject_required"])
def test_loader_refuses_a_new_class_that_is_reusable_or_subject_free(
    monkeypatch, scope_class, flag
):
    def mutate(catalog):
        catalog["classes"][scope_class][flag] = False

    with pytest.raises(scopes.ScopeError, match="single-use and require a subject"):
        _load_with(monkeypatch, mutate)


@pytest.mark.parametrize("scope_class", sorted(NEW_CLASSES))
def test_loader_refuses_a_catalog_missing_a_new_class(monkeypatch, scope_class):
    def mutate(catalog):
        del catalog["classes"][scope_class]

    with pytest.raises(scopes.ScopeError, match="classes are invalid"):
        _load_with(monkeypatch, mutate)


def test_loader_refuses_an_unknown_extra_class(monkeypatch):
    def mutate(catalog):
        catalog["classes"]["refund"] = dict(catalog["classes"]["spend"])

    with pytest.raises(scopes.ScopeError, match="classes are invalid"):
        _load_with(monkeypatch, mutate)


def test_loader_refuses_unsorted_or_duplicate_new_entries(monkeypatch):
    def unsorted(catalog):
        catalog["scopes"].reverse()

    def duplicate(catalog):
        catalog["scopes"].append(dict(catalog["scopes"][-1]))

    for mutate in (unsorted, duplicate):
        with pytest.raises(scopes.ScopeError, match="unique and sorted"):
            _load_with(monkeypatch, mutate)


def test_loader_refuses_a_subject_grammar_for_an_uncatalogued_scope(monkeypatch):
    def mutate(catalog):
        catalog["scopes"] = [
            e for e in catalog["scopes"] if e["scope"] != "conductor:spend:fly"
        ]

    with pytest.raises(scopes.ScopeError, match="uncatalogued"):
        _load_with(monkeypatch, mutate)


# -- shared fixture --------------------------------------------------------------------------


def test_fixture_shape_and_scope_set():
    assert VECTORS["schema"] == "hermes-owner-grant-subject-vectors/v1"
    assert set(VECTORS) >= {"schema", "normalization", "scopes"}
    assert set(VECTORS["scopes"]) == set(NEW_SCOPES)
    for value, entry in VECTORS["scopes"].items():
        assert entry["label"] == NEW_SCOPES[value]
        assert entry["class"] == scopes.parse_scope(value).scope_class
        for key in ("example", "valid", "invalid", "match", "mismatch", "grammar"):
            assert entry[key], (value, key)


def test_fixture_patterns_are_the_verifier_grammar():
    assert set(scopes.SUBJECT_GRAMMAR) == set(NEW_SCOPES)
    for value, entry in VECTORS["scopes"].items():
        assert scopes.SUBJECT_GRAMMAR[value] == entry["grammar"], value


@pytest.mark.parametrize(("scope", "subject"), _grammar_cases("valid"))
def test_fixture_valid_subjects_fit_the_grammar(scope, subject):
    assert scopes.subject_matches_grammar(scope, subject)


@pytest.mark.parametrize(("scope", "subject"), _grammar_cases("invalid"))
def test_fixture_invalid_subjects_are_outside_the_grammar(scope, subject):
    assert not scopes.subject_matches_grammar(scope, subject)


def test_fixture_examples_and_pairs_fit_the_grammar():
    for value, entry in VECTORS["scopes"].items():
        assert scopes.subject_matches_grammar(value, entry["example"])
        for pair in entry["match"]:
            assert pair["signed"] == pair["request"]
            assert scopes.subject_matches_grammar(value, pair["signed"])
        for pair in entry["mismatch"]:
            assert pair["signed"] != pair["request"]
            assert scopes.subject_matches_grammar(value, pair["signed"])
            assert scopes.subject_matches_grammar(value, pair["request"])


@pytest.mark.parametrize(
    "vector", VECTORS["normalization"]["vectors"], ids=lambda v: v["name"][:40]
)
def test_stage_question_hash_rule_vectors(vector):
    normalized = normalize_question_body(vector["raw"])
    assert normalized == vector["normalized"]
    assert hashlib.sha256(normalized.encode("utf-8")).hexdigest() == vector["sha256"]


def test_stage_question_rule_trims_nothing_but_space_tab_and_lf():
    assert normalize_question_body(" q ") == " q "
    assert normalize_question_body("q\r") == "q\r"
    assert normalize_question_body("\rq") == "\rq"
    assert normalize_question_body(" \t\nq\r\n\t ") == "q"


def test_answer_fixture_question_hash_and_option_two_subject():
    question = VECTORS["scopes"]["conductor:answer:stage-variant"]["question"]
    body = question["text"] + "\n" + "\n".join(
        "%d. %s" % (i, option) for i, option in enumerate(question["options"], 1)
    )
    assert body == question["body"]
    digest = hashlib.sha256(normalize_question_body(body).encode("utf-8")).hexdigest()
    assert digest == question["question_sha256"]
    assert question["option_2_subject"] == digest + ":2"
    assert scopes.subject_matches_grammar(
        "conductor:answer:stage-variant", question["option_2_subject"]
    )
    lines = question["directive"].split("\n")
    assert lines[0] == (
        ':::stage-question{question_sha256="%s" scope="conductor:answer:stage-variant"}'
        % digest
    )
    assert lines[-1] == ":::"
    assert "\n".join(lines[1:-1]) == body
