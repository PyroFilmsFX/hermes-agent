"""b9 owner-grant scopes through the verifier: subject match, single use, grammar, TTL cap.

Each scope's vectors come from tests/fixtures/owner_grant_subject_vectors.json (shared with the
desktop mirrors). Grants are signed with a real Ed25519 key from the ``cryptography`` package
inside the test only, exactly as test_verify.py does, and verified by the stdlib verifier.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    reason="test keys are generated with the cryptography package",
)

from hermes_owner_grant import scopes  # noqa: E402

from .test_verify import (  # noqa: E402
    GATE,
    HOUR,
    MIN,
    NOW,
    anchor,  # noqa: F401  (pytest fixture)
    check,
    check_env,
    denied,
    grants,  # noqa: F401  (pytest fixture)
    make_payload,
    owner,  # noqa: F401  (pytest fixture)
    seal,
    write_grant,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "owner_grant_subject_vectors.json"
)
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["scopes"]
SCOPES = sorted(VECTORS)


def _payload(value, subject, **kw):
    """A grant for one b9 scope ``value`` signed with ``subject`` (None: no subject)."""
    kw.setdefault("scope", [value])
    kw.setdefault("single_use", [value])
    kw.setdefault("subject", {value: subject} if subject is not None else {})
    default_ttl = scopes.CLASS_DEFAULT_TTL_MS[scopes.class_for_scope(value)]
    kw.setdefault("expires_at", NOW - MIN + default_ttl)
    return make_payload(**kw)


def _short(scope):
    return scope.rsplit(":", 1)[-1]


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_signed_subject_match_passes(owner, anchor, scope):
    for pair in VECTORS[scope]["match"]:
        env = seal(owner, _payload(scope, pair["signed"]))
        result = check_env(anchor, env, scopes=[scope], subject=pair["request"])
        assert result.ok, result.to_dict()
        assert result.grant["single_use"] == [scope]


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_a_changed_subject_part_is_subject_mismatch(owner, anchor, scope):
    for pair in VECTORS[scope]["mismatch"]:
        env = seal(owner, _payload(scope, pair["signed"]))
        denied(
            check_env(anchor, env, scopes=[scope], subject=pair["request"]),
            "subject_mismatch",
        )


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_a_missing_request_subject_is_subject_required(owner, anchor, scope):
    env = seal(owner, _payload(scope, VECTORS[scope]["example"]))
    denied(check_env(anchor, env, scopes=[scope]), "subject_required")


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_a_grant_signed_without_a_subject_is_malformed(owner, anchor, scope):
    env = seal(owner, _payload(scope, None))
    denied(
        check_env(anchor, env, scopes=[scope], subject=VECTORS[scope]["example"]),
        "malformed",
    )


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_a_grant_that_omits_single_use_is_malformed(owner, anchor, scope):
    env = seal(owner, _payload(scope, VECTORS[scope]["example"], single_use=[]))
    denied(
        check_env(anchor, env, scopes=[scope], subject=VECTORS[scope]["example"]),
        "malformed",
    )


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_a_second_use_fails(owner, grants, anchor, scope):
    subject = VECTORS[scope]["example"]
    env = seal(owner, _payload(scope, subject))
    write_grant(grants, env)
    first = check(
        anchor, grant_id=env.grant_id, scopes=[scope], subject=subject, consume=True
    )
    assert first.ok, first.to_dict()
    assert [row["scope"] for row in first.to_dict()["consumed"]] == [scope]
    second = check(
        anchor, grant_id=env.grant_id, scopes=[scope], subject=subject, consume=True
    )
    denied(second, "already_consumed")


@pytest.mark.parametrize(
    ("scope", "bad"),
    [
        pytest.param(scope, bad, id="%s-%d" % (_short(scope), i))
        for scope in SCOPES
        for i, bad in enumerate(VECTORS[scope]["invalid"])
    ],
)
def test_a_signed_subject_outside_the_grammar_is_malformed(owner, anchor, scope, bad):
    env = seal(owner, _payload(scope, bad))
    # Even a request that repeats the out-of-grammar subject byte for byte is refused.
    result = denied(check_env(anchor, env, scopes=[scope], subject=bad), "malformed")
    assert result.grant is None  # a malformed grant is never reported as a grant
    denied(check_env(anchor, env), "malformed")  # quote-only citation too


def test_the_answer_option_two_subject_verifies_and_option_three_does_not(owner, anchor):
    scope = "conductor:answer:stage-variant"
    question = VECTORS[scope]["question"]
    env = seal(owner, _payload(scope, question["option_2_subject"]))
    ok = check_env(anchor, env, scopes=[scope], subject=question["option_2_subject"])
    assert ok.ok, ok.to_dict()
    denied(
        check_env(
            anchor, env, scopes=[scope], subject=question["question_sha256"] + ":3"
        ),
        "subject_mismatch",
    )


@pytest.mark.parametrize("scope", SCOPES, ids=_short)
def test_ttl_is_clamped_to_the_class_maximum(owner, anchor, scope):
    subject = VECTORS[scope]["example"]
    cap = scopes.MAX_TTL_MS_BY_CLASS[scopes.class_for_scope(scope)]
    at_cap = seal(owner, _payload(scope, subject, expires_at=NOW - MIN + cap))
    assert check_env(anchor, at_cap, scopes=[scope], subject=subject).ok
    over = seal(owner, _payload(scope, subject, expires_at=NOW - MIN + cap + 1))
    denied(check_env(anchor, over, scopes=[scope], subject=subject), "ttl_exceeded")


@pytest.mark.parametrize(
    ("scope", "cap"),
    [
        ("conductor:answer:stage-variant", 4 * HOUR),
        ("conductor:defer:wave-or-unit", 4 * HOUR),
        ("conductor:override:review-budget", 4 * HOUR),
        ("conductor:gc:prune-lanes", HOUR),
        ("conductor:policy:standing-approval", HOUR),
        ("conductor:policy:unsandboxed-write", HOUR),
        ("conductor:spend:fly", HOUR),
        ("conductor:continuity:session-relaunch", 15 * MIN),
    ],
)
def test_mixed_with_a_gate_scope_the_tighter_new_cap_wins(owner, anchor, scope, cap):
    subject = VECTORS[scope]["example"]
    payload = _payload(
        scope, subject, scope=[GATE, scope], expires_at=NOW - MIN + cap + 1
    )
    denied(
        check_env(anchor, seal(owner, payload), scopes=[GATE]), "ttl_exceeded"
    )
    payload = _payload(scope, subject, scope=[GATE, scope], expires_at=NOW - MIN + cap)
    assert check_env(anchor, seal(owner, payload), scopes=[GATE]).ok
