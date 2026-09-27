"""G-7 to G-10, G-12, G-17 plus the TTL cap and anchor-uid refusal (addendum §2.4, §3.4; U5).

Every grant here is signed with a real Ed25519 key that the ``cryptography`` package
generates inside the TEST only, and verified by ``hermes_owner_grant.verify`` through the
stdlib ``ed25519_pure`` implementation. The whole module is skipped when ``cryptography`` is
not importable. Nothing here touches ``~/.hermes``, the Keychain or ``/Library``: the anchor
is built in-process (the trusted-caller ``anchor=`` path) and points at a tmp grants dir.
"""

import ast
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ed = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    reason="test keys are generated with the cryptography package",
)
from cryptography.hazmat.primitives import serialization  # noqa: E402

from hermes_owner_grant import anchor as anchor_mod  # noqa: E402
from hermes_owner_grant import envelope as env_mod  # noqa: E402
from hermes_owner_grant import cli as cli_mod  # noqa: E402
from hermes_owner_grant import verify as verify_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SOURCE = REPO_ROOT / "hermes_owner_grant" / "verify.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verify_v1_fixture.json"

SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
NOW = 1_790_000_000_000
UID = 501
SESSION = "20260926_101500_a1b2c3"
OTHER_SESSION = "20260926_101500_ffffff"
CLAUDE = "5b0d8a3e-0c7e-4c2e-9d5f-3f1f7d9a0b11"
TEXT = (
    "Enable the review budget gate for this lane.\n"
    "Do not merge until CI is green. Then ship it."
)
GATE = "conductor:gate:review-budget-enable"
GATE2 = "conductor:gate:pr-discipline-enable"
MARKER = "conductor:marker:restore"
PROD = "conductor:prod:fly-ord"


# -- test-only signer ----------------------------------------------------------------------


class Owner:
    """A real Ed25519 key from the cryptography package (tests only)."""

    def __init__(self, seed=None):
        if seed is None:
            self.key = ed.Ed25519PrivateKey.generate()
        else:
            self.key = ed.Ed25519PrivateKey.from_private_bytes(seed)
        self.pub = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.kid = env_mod.kid_for_pub(self.pub)

    def sign(self, message):
        return self.key.sign(message)

    def anchor_key(self, status="active", not_before=0, retired_at=None):
        return {
            "kid": self.kid,
            "alg": "Ed25519",
            "pub": env_mod.b64url_encode(self.pub),
            "status": status,
            "not_before": not_before,
            "retired_at": retired_at,
        }


def make_payload(**overrides):
    text = overrides.pop("text", TEXT)
    issued_at = overrides.pop("issued_at", NOW - MIN)
    payload = {
        "v": 1,
        "aud": ["hermes-owner-forward", "hermes-owner-verify"],
        "decision_id": "od_" + "a" * 26,
        "issued_at": issued_at,
        "deliver_by": issued_at + MIN,
        "expires_at": issued_at + 12 * HOUR,
        "owner_uid": UID,
        "backend": "spawn-1",
        "nonce": env_mod.b64url_encode(os.urandom(16)),
        "gesture": "proposal",
        "confirm": "native_dialog",
        "source_session": {"session_id": "mgr", "message_id": "m1", "role": "user"},
        "targets": [{"session_id": SESSION, "claude_session_id": CLAUDE}],
        "scope": [GATE],
        "single_use": [],
        "subject": {},
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_len": len(text.encode("utf-8")),
    }
    payload.update(overrides)
    return payload


def seal(owner, payload):
    return env_mod.seal(env_mod.encode_payload(payload), owner.kid, owner.sign)


def make_anchor(grants_dir, keys, owner_uid=UID):
    doc = {
        "format": "hermes-owner-anchor/v1",
        "owner_uid": owner_uid,
        "grants_dir": os.path.realpath(str(grants_dir)),
        "keys": keys,
        "verifier_sha256": "ab" * 32,
    }
    return anchor_mod.parse_anchor(json.dumps(doc).encode("utf-8"))


def write_grant(grants_dir, envelope, name=None):
    issued_at = env_mod.decode_payload(envelope.payload)["issued_at"]
    name = name or env_mod.grant_filename(issued_at, envelope.grant_id)
    path = Path(grants_dir) / name
    path.write_text(envelope.to_json(), encoding="utf-8")
    return path


@pytest.fixture
def owner():
    return Owner()


@pytest.fixture
def grants(tmp_path):
    d = tmp_path / "owner-grants"
    d.mkdir()
    return d


@pytest.fixture
def anchor(grants, owner):
    return make_anchor(grants, [owner.anchor_key()])


def check(anchor, **kw):
    """Lookup-mode verify with trusted defaults for session, uid and now."""
    kw.setdefault("session", SESSION)
    kw.setdefault("claude_session", CLAUDE)
    kw.setdefault("now", NOW)
    kw.setdefault("uid", UID)
    return verify_mod.verify(anchor=anchor, **kw)


def check_env(anchor, envelope, **kw):
    kw.setdefault("session", SESSION)
    kw.setdefault("claude_session", CLAUDE)
    kw.setdefault("now", NOW)
    kw.setdefault("uid", UID)
    return verify_mod.verify_envelope(envelope, anchor=anchor, **kw)


def sha(text=TEXT):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def denied(result, reason):
    assert result.ok is False, result.to_dict()
    assert result.reason == reason, result.to_dict()
    return result


# -- end to end: a valid grant passes -------------------------------------------------------


def test_e2e_valid_grant_passes_by_hash_quote_scope_and_id(owner, grants, anchor):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    by_sha = check(anchor, text_sha=sha(), scopes=[GATE])
    assert by_sha.ok and by_sha.reason is None, by_sha.to_dict()
    assert by_sha.exit_code == 0
    assert by_sha.tier == "signed"
    assert by_sha.grant["id"] == env.grant_id == env_mod.derive_grant_id(env.payload)
    assert by_sha.grant["kid"] == owner.kid
    assert by_sha.match == {"kind": "sha", "segment_index": None}
    assert by_sha.candidates == 1
    out = by_sha.to_dict()
    assert out["schema"] == "hermes-owner-verify/v1"
    assert out["verifier"]["impl"] == "pure"
    assert out["verifier"]["anchor_sha256"] == anchor.sha256
    assert out["checked_at"] == NOW and out["audit"] is False
    assert "text" not in out["grant"]  # owner text stays out of hook logs by default

    exact = check(anchor, quote=TEXT)
    assert exact.ok and exact.match["kind"] == "exact", exact.to_dict()
    segment = check(anchor, quote="Do not merge until CI is green. Then ship it.")
    assert segment.ok and segment.match["kind"] == "segment", segment.to_dict()
    by_scope_only = check(anchor, scopes=[GATE])
    assert by_scope_only.ok, by_scope_only.to_dict()
    by_id = check(anchor, grant_id=env.grant_id, scopes=[GATE])
    assert by_id.ok and by_id.match["kind"] == "grant_id", by_id.to_dict()
    with_text = check(anchor, text_sha=sha(), include_text=True)
    assert with_text.to_dict()["grant"]["text"] == TEXT


def test_e2e_verify_envelope_in_process(owner, anchor):
    env = seal(owner, make_payload())
    result = check_env(anchor, env, text_sha=sha(), scopes=[GATE])
    assert result.ok, result.to_dict()
    # The same envelope as the dict form carried in display_metadata.
    assert check_env(anchor, env.to_dict(), scopes=[GATE]).ok


def test_g13_revoked_list_denies_and_malformed_rows_fail_safe(owner, grants, anchor):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    revoked = grants / "revoked.jsonl"
    revoked.write_text(
        json.dumps({"grant_id": env.grant_id, "revoked_at": NOW, "by": "owner"}) + "\n",
        encoding="utf-8",
    )
    denied(check(anchor, grant_id=env.grant_id), "revoked")
    revoked_output = io.StringIO()
    assert (
        cli_mod.main(
            ["verify", "--session", SESSION, "--grant", env.grant_id],
            anchor=anchor,
            uid=UID,
            now_ms=NOW,
            stdout=revoked_output,
        )
        == 1
    )

    revoked.write_text("not-json\n", encoding="utf-8")
    denied(check(anchor, grant_id=env.grant_id), "revoked")


def test_g14_consume_is_atomic_audited_and_only_single_use(owner, grants, anchor):
    env = seal(
        owner,
        make_payload(
            issued_at=NOW - MIN,
            expires_at=NOW + 2 * HOUR,
            scope=[MARKER, GATE],
            single_use=[MARKER],
        ),
    )
    write_grant(grants, env)

    reusable = check(anchor, grant_id=env.grant_id, scopes=[GATE], consume=True)
    assert reusable.ok and reusable.to_dict()["consumed"] is None
    assert not (grants / "consumed").exists()

    first = check(anchor, grant_id=env.grant_id, scopes=[MARKER], consume=True)
    assert first.ok, first.to_dict()
    consumed = first.to_dict()["consumed"]
    assert consumed == [
        {"scope": MARKER, "scope_hash": hashlib.sha256(MARKER.encode()).hexdigest()}
    ]
    marker = grants / "consumed" / (env.grant_id + "." + consumed[0]["scope_hash"])
    assert marker.is_file()
    rows = [
        json.loads(line)
        for line in (grants / "consumed.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["grant_id"] == env.grant_id
    assert rows[0]["scope"] == MARKER

    second = check(anchor, grant_id=env.grant_id, scopes=[MARKER], consume=True)
    denied(second, "already_consumed")
    with pytest.raises(verify_mod.VerifyUsageError):
        check(anchor, grant_id=env.grant_id, consume=True, audit_at=NOW - 1)


def test_g15_cli_json_schema_and_exit_codes(owner, grants, anchor, monkeypatch):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    output = io.StringIO()
    code = cli_mod.main(
        ["verify", "--session", SESSION, "--claude-session", CLAUDE, "--grant", env.grant_id],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdout=output,
    )
    body = json.loads(output.getvalue())
    assert code == 0 and body["ok"] is True
    assert set(body) == set(cli_mod.JSON_SCHEMA["required"])
    assert body["schema"] == "hermes-owner-verify/v1"

    missing = io.StringIO()
    assert (
        cli_mod.main(
            ["verify", "--session", SESSION, "--grant", "og_" + "a" * 26],
            anchor=anchor,
            uid=UID,
            now_ms=NOW,
            stdout=missing,
        )
        == 4
    )
    assert json.loads(missing.getvalue())["reason"] == "not_found"

    monkeypatch.setattr(
        cli_mod.anchor_mod,
        "load_trusted_anchor",
        lambda: (_ for _ in ()).throw(
            anchor_mod.AnchorError("anchor_missing", "missing")
        ),
    )
    absent = io.StringIO()
    assert cli_mod.main(["anchor-status"], stdout=absent) == 3
    assert json.loads(absent.getvalue())["reason"] == "anchor_missing"

    monkeypatch.setattr(
        cli_mod.verify_mod,
        "verify",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    internal = io.StringIO()
    assert (
        cli_mod.main(
            ["verify", "--session", SESSION, "--grant", env.grant_id],
            anchor=anchor,
            uid=UID,
            now_ms=NOW,
            stdout=internal,
        )
        == 5
    )
    assert json.loads(internal.getvalue())["reason"] == "internal_error"


def test_g15_cli_usage_error_is_json_exit_two():
    output = io.StringIO()
    code = cli_mod.main(
        ["verify", "--session", SESSION, "--anchor", "x"], stdout=output
    )
    assert code == 2
    body = json.loads(output.getvalue())
    assert body["reason"] == "usage_error"
    assert body["schema"] == "hermes-owner-verify/v1"


def test_g16_built_bundle_verifies_fixture_under_100ms(tmp_path, owner, grants):
    python39 = Path("/usr/bin/python3")
    if not python39.is_file():
        pytest.skip("/usr/bin/python3 is unavailable")
    env = seal(owner, make_payload())
    write_grant(grants, env)
    issued_at = NOW - MIN
    wire = env.to_json()
    for index in range(199):
        suffix = base64.b32encode(index.to_bytes(4, "big")).decode("ascii").lower()
        fake_id = "og_" + (suffix + "a" * 26)[:26]
        (grants / (str(issued_at) + "-" + fake_id + ".json")).write_text(
            wire, encoding="utf-8"
        )

    bundle = tmp_path / "hermes_owner_verify.py"
    build_script = REPO_ROOT / "scripts" / "build_owner_verifier.py"
    subprocess.run(
        [str(python39), str(build_script), str(bundle)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    anchor_doc = {
        "format": "hermes-owner-anchor/v1",
        "owner_uid": UID,
        "grants_dir": str(grants),
        "keys": [owner.anchor_key()],
        "verifier_sha256": "ab" * 32,
    }
    code = (
        "import json,sys,time; "
        "exec(compile(open(%r,'rb').read(),%r,'exec'),"
        "{'__name__':'bundle_test','__file__':%r}); "
        "pkg=sys.modules['hermes_owner_grant']; "
        "a=pkg.anchor.parse_anchor((%r).encode()); "
        "start=time.perf_counter(); "
        "out=sys.modules['hermes_owner_grant.cli'].main("
        "['verify','--session',%r,'--claude-session',%r,'--text-sha',%r],anchor=a,uid=%d,now_ms=%d); "
        "elapsed=time.perf_counter()-start; "
        "sys.stderr.write(str(elapsed)); raise SystemExit(out)"
        % (
            str(bundle),
            str(bundle),
            str(bundle),
            json.dumps(anchor_doc),
            SESSION,
            CLAUDE,
            sha(),
            UID,
            NOW,
        )
    )
    proc = subprocess.run(
        [str(python39), "-I", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
    elapsed = float(proc.stderr)
    sys.__stdout__.write("G-16 bundle %s: %.1f ms\n" % (bundle, elapsed * 1000))
    assert elapsed < 0.1, "standalone verifier took %.1f ms" % (elapsed * 1000)


# -- G-7: session binding ------------------------------------------------------------------


def test_g7_session_mismatch(owner, grants, anchor):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    denied(check_env(anchor, env, session=OTHER_SESSION), "session_mismatch")
    denied(
        check(anchor, grant_id=env.grant_id, session=OTHER_SESSION), "session_mismatch"
    )


def test_g7_lookup_for_another_session_finds_no_grant(owner, grants, anchor):
    write_grant(grants, seal(owner, make_payload()))
    result = denied(check(anchor, text_sha=sha(), session=OTHER_SESSION), "not_found")
    assert result.exit_code == 4 and result.candidates == 0


def test_g7_any_listed_target_binds(owner, anchor):
    targets = [
        {"session_id": OTHER_SESSION, "claude_session_id": "claude-other"},
        {"session_id": SESSION, "claude_session_id": CLAUDE},
    ]
    env = seal(owner, make_payload(targets=targets))
    assert check_env(anchor, env, session=SESSION, claude_session=CLAUDE).ok
    assert check_env(anchor, env, session=OTHER_SESSION, claude_session="claude-other").ok
    denied(check_env(anchor, env, session="20260926_000000_000000"), "session_mismatch")


def test_g7_claude_session_mismatch(owner, anchor):
    targets = [{"session_id": SESSION, "claude_session_id": CLAUDE}]
    env = seal(owner, make_payload(targets=targets))
    assert check_env(anchor, env, claude_session=CLAUDE).ok
    denied(
        check_env(anchor, env, claude_session="11111111-2222-3333-4444-555555555555"),
        "claude_session_mismatch",
    )


def test_g7_claude_session_is_required_when_the_grant_carries_one(owner, anchor):
    # Addendum §4.1 (T-6): the Claude id from hook stdin is the binding env can't override,
    # so a caller that omits it can't fall back to the Hermes id alone.
    targets = [{"session_id": SESSION, "claude_session_id": CLAUDE}]
    env = seal(owner, make_payload(targets=targets))
    denied(check_env(anchor, env, claude_session=None), "claude_session_mismatch")


def test_g7_bound_target_binds_by_hermes_and_claude_ids(owner, anchor):
    env = seal(owner, make_payload())
    assert check_env(anchor, env, claude_session=CLAUDE).ok


@pytest.mark.parametrize("claude_binding", [None, "missing"])
def test_g7_conductor_scope_requires_a_claude_session_binding(owner, anchor, claude_binding):
    target = {"session_id": SESSION}
    if claude_binding != "missing":
        target["claude_session_id"] = claude_binding
    env = seal(owner, make_payload(targets=[target]))
    denied(
        check_env(anchor, env, claude_session=CLAUDE),
        "claude_session_unbound",
    )


def test_g7_quote_only_grant_can_have_a_null_claude_session_binding(owner, anchor):
    env = seal(
        owner,
        make_payload(
            scope=[], targets=[{"session_id": SESSION, "claude_session_id": None}]
        ),
    )
    assert check_env(anchor, env, claude_session=CLAUDE).ok


def test_g7_requested_conductor_scope_requires_binding_on_quote_only_grant(owner, anchor):
    env = seal(
        owner,
        make_payload(
            scope=[], targets=[{"session_id": SESSION, "claude_session_id": None}]
        ),
    )
    result = check_env(anchor, env, claude_session=CLAUDE, scopes=[GATE])
    denied(result, "claude_session_unbound")
    assert result.exit_code == 1


def test_g7_duplicate_target_sessions_are_malformed(owner, anchor):
    targets = [
        {"session_id": SESSION, "claude_session_id": CLAUDE},
        {"session_id": SESSION, "claude_session_id": None},
    ]
    denied(check_env(anchor, seal(owner, make_payload(targets=targets))), "malformed")


# -- G-8: exact scope matching -------------------------------------------------------------


def test_g8_exact_scope_passes_and_a_missing_scope_fails(owner, grants, anchor):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    assert check_env(anchor, env, scopes=[GATE]).ok
    denied(check_env(anchor, env, scopes=[GATE, GATE2]), "scope_missing")
    denied(check(anchor, grant_id=env.grant_id, scopes=[GATE2]), "scope_missing")


@pytest.mark.parametrize(
    "requested",
    ["conductor:gate:review-budget", "conductor:gate:review-budget-enable-x"],
)
def test_g8_prefixes_and_extensions_do_not_match(owner, anchor, requested):
    denied(
        check_env(anchor, seal(owner, make_payload()), scopes=[requested]),
        "scope_missing",
    )


@pytest.mark.parametrize(
    "requested",
    [
        "conductor:gate:*",
        "conductor:gate:review-*",
        "conductor:*",
        "Conductor:gate:review-budget-enable",
        "conductor:gate:review-budget-enable ",
        "",
    ],
)
def test_g8_wildcards_and_out_of_grammar_requests_are_usage_errors(
    owner, anchor, requested
):
    with pytest.raises(verify_mod.VerifyUsageError):
        check_env(anchor, seal(owner, make_payload()), scopes=[requested])


def test_g8_a_wildcard_inside_a_signed_grant_is_malformed(owner, anchor):
    env = seal(owner, make_payload(scope=["conductor:gate:*"]))
    denied(check_env(anchor, env), "malformed")


def test_g8_quote_only_grant_authorizes_no_scope(owner, anchor):
    env = seal(owner, make_payload(scope=[]))
    assert check_env(anchor, env, text_sha=sha()).ok
    denied(check_env(anchor, env, text_sha=sha(), scopes=[GATE]), "scope_missing")


def test_g8_lookup_filters_on_scope(owner, grants, anchor):
    old = seal(owner, make_payload(scope=[GATE2], issued_at=NOW - 2 * HOUR))
    new = seal(owner, make_payload(scope=[GATE], issued_at=NOW - HOUR))
    write_grant(grants, old)
    write_grant(grants, new)
    assert check(anchor, scopes=[GATE2]).grant["id"] == old.grant_id
    assert check(anchor, scopes=[GATE]).grant["id"] == new.grant_id
    denied(check(anchor, scopes=[GATE, GATE2]), "not_found")


# -- G-9: prod subject ---------------------------------------------------------------------


def _prod_payload(**kw):
    kw.setdefault("scope", [PROD])
    kw.setdefault("single_use", [PROD])
    kw.setdefault("subject", {PROD: "sha256:" + "c" * 64})
    kw.setdefault("expires_at", NOW + 10 * MIN)
    return make_payload(**kw)


def test_g9_prod_requires_the_signed_subject(owner, anchor):
    env = seal(owner, _prod_payload())
    ok = check_env(anchor, env, scopes=[PROD], subject="sha256:" + "c" * 64)
    assert ok.ok, ok.to_dict()
    assert ok.grant["single_use"] == [PROD]
    denied(check_env(anchor, env, scopes=[PROD]), "subject_required")
    denied(
        check_env(anchor, env, scopes=[PROD], subject="sha256:" + "d" * 64),
        "subject_mismatch",
    )


def test_g9_a_prod_grant_signed_without_a_subject_is_malformed(owner, anchor):
    env = seal(owner, _prod_payload(subject={}))
    denied(check_env(anchor, env, scopes=[PROD], subject="anything"), "malformed")


def test_g9_single_use_must_cover_every_single_use_class_scope(owner, anchor):
    # A signer that left the prod scope out of single_use would make it reusable.
    env = seal(owner, _prod_payload(single_use=[]))
    denied(
        check_env(anchor, env, scopes=[PROD], subject="sha256:" + "c" * 64), "malformed"
    )
    env = seal(
        owner, make_payload(scope=[MARKER], expires_at=NOW + HOUR, single_use=[])
    )
    denied(check_env(anchor, env, scopes=[MARKER]), "malformed")


def test_g9_a_subject_on_a_non_prod_scope_is_enforced_too(owner, anchor):
    env = seal(owner, make_payload(subject={GATE: "lane-7"}))
    denied(check_env(anchor, env, scopes=[GATE]), "subject_required")
    denied(check_env(anchor, env, scopes=[GATE], subject="lane-8"), "subject_mismatch")
    assert check_env(anchor, env, scopes=[GATE], subject="lane-7").ok
    # A quote-only citation of the same grant doesn't claim the scope, so no subject needed.
    assert check_env(anchor, env, text_sha=sha()).ok


# -- G-10: time ----------------------------------------------------------------------------


def test_g10_expiry_with_five_second_skew(owner, anchor):
    payload = make_payload()
    env = seal(owner, payload)
    exp = payload["expires_at"]
    assert check_env(anchor, env, now=exp).ok
    assert check_env(anchor, env, now=exp + 5 * SEC).ok
    denied(check_env(anchor, env, now=exp + 5 * SEC + 1), "expired")


def test_g10_not_yet_issued_beyond_skew_fails(owner, anchor):
    payload = make_payload(issued_at=NOW + 10 * SEC)
    env = seal(owner, payload)
    assert check_env(anchor, env, now=NOW + 5 * SEC).ok
    denied(check_env(anchor, env, now=NOW + 5 * SEC - 1), "expired")


def test_g10_max_age(owner, anchor):
    env = seal(owner, make_payload(issued_at=NOW - 10 * MIN))
    assert check_env(anchor, env, max_age_s=600).ok
    denied(check_env(anchor, env, max_age_s=599), "expired")


def test_g10_deliver_by_is_not_a_verify_check(owner, anchor):
    payload = make_payload(issued_at=NOW - HOUR)
    assert payload["deliver_by"] < NOW
    assert check_env(anchor, seal(owner, payload)).ok


def test_g10_audit_at_re_evaluates_time_at_a_past_instant(owner, grants, anchor):
    payload = make_payload(issued_at=NOW - 2 * DAY, expires_at=NOW - DAY)
    env = seal(owner, payload)
    write_grant(grants, env)
    denied(check_env(anchor, env), "expired")
    audited = check_env(anchor, env, audit_at=NOW - DAY - HOUR)
    assert audited.ok and audited.audit is True, audited.to_dict()
    assert audited.to_dict()["audit"] is True
    looked_up = check(anchor, text_sha=sha(), audit_at=NOW - DAY - HOUR)
    assert looked_up.ok and looked_up.audit is True, looked_up.to_dict()


# -- (a) TTL cap per scope class -----------------------------------------------------------


@pytest.mark.parametrize(
    "scope,single_use,ttl,ok",
    [
        ([GATE], [], 72 * HOUR, True),
        ([GATE], [], 72 * HOUR + 1, False),
        ([MARKER], [MARKER], 4 * HOUR, True),
        ([MARKER], [MARKER], 4 * HOUR + 1, False),
        ([GATE, MARKER], [MARKER], 5 * HOUR, False),  # mixed classes: min(max TTLs)
        ([], [], 7 * DAY, True),
        ([], [], 7 * DAY + 1, False),
    ],
)
def test_ttl_cap_follows_the_tightest_scope_class(
    owner, anchor, scope, single_use, ttl, ok
):
    env = seal(
        owner,
        make_payload(scope=scope, single_use=single_use, expires_at=NOW - MIN + ttl),
    )
    result = check_env(anchor, env)
    if ok:
        assert result.ok, result.to_dict()
    else:
        denied(result, "ttl_exceeded")


def test_prod_ttl_cap_is_one_hour(owner, anchor):
    env = seal(owner, _prod_payload(expires_at=NOW - MIN + HOUR + 1))
    denied(
        check_env(anchor, env, scopes=[PROD], subject="sha256:" + "c" * 64),
        "ttl_exceeded",
    )


def test_a_leaked_retired_key_cannot_mint_a_backdated_never_expiring_grant(grants):
    retired_at = NOW - DAY
    thief = Owner()
    current = Owner()
    anchor = make_anchor(
        grants,
        [
            current.anchor_key(),
            thief.anchor_key(status="retired", retired_at=retired_at),
        ],
    )
    forever = make_payload(issued_at=retired_at - 1, expires_at=retired_at + 3650 * DAY)
    denied(check_env(anchor, seal(thief, forever)), "ttl_exceeded")
    # Within the class cap the backdated grant still dies with the retired key's window.
    capped = make_payload(
        scope=[], issued_at=retired_at - 1, expires_at=retired_at - 1 + 7 * DAY
    )
    env = seal(thief, capped)
    # Rotation semantics: a pre-retirement grant finishes its (capped) TTL...
    assert check_env(anchor, env, text_sha=sha(), now=NOW).ok
    # ...and nothing under the retired key verifies past retired_at + the longest class TTL.
    late = retired_at + anchor_mod.MAX_GRANT_TTL_MS + 1
    denied(check_env(anchor, env, now=late, audit_at=None), "key_retired")


# -- (b) anchor owner uid ------------------------------------------------------------------


def test_anchor_for_another_uid_is_refused(owner, grants):
    anchor = make_anchor(grants, [owner.anchor_key()], owner_uid=UID)
    env = seal(owner, make_payload())
    write_grant(grants, env)
    result = denied(check(anchor, text_sha=sha(), uid=UID + 1), "anchor_untrusted")
    assert result.exit_code == 3
    assert str(UID + 1) in result.detail
    denied(check_env(anchor, env, uid=UID + 1), "anchor_untrusted")


def test_payload_owner_uid_must_match_the_verifying_uid(owner, anchor):
    denied(
        check_env(anchor, seal(owner, make_payload(owner_uid=UID + 7))), "uid_mismatch"
    )


def test_uid_session_and_now_are_required_parameters(owner, anchor):
    env = seal(owner, make_payload())
    with pytest.raises(TypeError):
        verify_mod.verify_envelope(env, anchor=anchor, session=SESSION, now=NOW)
    with pytest.raises(TypeError):
        verify_mod.verify_envelope(env, anchor=anchor, session=SESSION, uid=UID)
    with pytest.raises(TypeError):
        verify_mod.verify(anchor=anchor, now=NOW, uid=UID, text_sha=sha())
    for bad in (True, "501", None, -1):
        with pytest.raises(verify_mod.VerifyUsageError):
            verify_mod.verify_envelope(
                env, anchor=anchor, session=SESSION, now=NOW, uid=bad
            )


def test_verify_reads_no_ambient_uid_clock_or_environment():
    tree = ast.parse(VERIFY_SOURCE.read_text(encoding="utf-8"))
    banned_attrs = {"getuid", "geteuid", "getpid", "time_ns", "monotonic", "utcnow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_attrs, "verify.py:%d uses .%s" % (
                node.lineno,
                node.attr,
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            for name in names:
                assert name.split(".")[0] not in {"time", "datetime", "sqlite3"}, name


# -- usage errors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"session": "", "text_sha": sha()},
        {"session": None, "text_sha": sha()},
        {"session": SESSION, "text_sha": sha(), "scopes": "conductor:gate:x"},
        {"text_sha": "abc"},
        {"text_sha": "g" * 64},
        {"text_sha": sha(), "quote": TEXT},
        {"grant_id": "og_" + "a" * 26, "quote": TEXT},
        {"grant_id": "og_nope"},
        {"text_sha": None},  # no selector and no scope
        {"text_sha": sha(), "max_age_s": -1},
        {"text_sha": sha(), "claude_session": ""},
    ],
)
def test_usage_errors(anchor, kw):
    kw = dict(kw)
    with pytest.raises(verify_mod.VerifyUsageError):
        check(anchor, **kw)


# -- G-12: never state.db; forged files never verify ---------------------------------------


def test_g12_verifier_never_opens_sqlite(owner, grants, anchor, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("verifier opened sqlite")

    monkeypatch.setattr(sqlite3, "connect", boom)
    env = seal(owner, make_payload())
    write_grant(grants, env)
    # A state.db with a fake owner_forward row sits beside the grants; it proves nothing.
    (grants / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    assert check(anchor, text_sha=sha()).ok
    assert check(anchor, grant_id=env.grant_id).ok


def test_g12_unsigned_or_tampered_files_never_verify(owner, grants, anchor):
    payload_bytes = env_mod.encode_payload(make_payload())
    forged = env_mod.Envelope(kid=owner.kid, payload=payload_bytes, sig=os.urandom(64))
    write_grant(grants, forged)
    denied(check(anchor, text_sha=sha()), "bad_signature")
    denied(check(anchor, grant_id=forged.grant_id), "bad_signature")

    real = seal(owner, make_payload(issued_at=NOW - 2 * MIN))
    tampered_payload = json.loads(real.payload)
    tampered_payload["scope"] = [GATE, GATE2]
    tampered = env_mod.Envelope(
        kid=owner.kid, payload=env_mod.encode_payload(tampered_payload), sig=real.sig
    )
    denied(check_env(anchor, tampered), "bad_signature")


def test_g12_a_key_outside_the_anchor_is_unknown(grants, anchor):
    intruder = Owner()
    env = seal(intruder, make_payload())
    write_grant(grants, env)
    denied(check_env(anchor, env), "unknown_kid")
    denied(check(anchor, grant_id=env.grant_id), "unknown_kid")


def test_g12_forged_newest_file_does_not_hide_the_real_grant(owner, grants, anchor):
    real = seal(owner, make_payload(issued_at=NOW - 2 * MIN))
    write_grant(grants, real)
    fake_payload = env_mod.encode_payload(make_payload(issued_at=NOW - MIN))
    write_grant(
        grants, env_mod.Envelope(kid=owner.kid, payload=fake_payload, sig=bytes(64))
    )
    result = check(anchor, text_sha=sha())
    assert result.ok and result.grant["id"] == real.grant_id, result.to_dict()
    assert result.candidates == 2


def test_g12_junk_entries_in_the_grants_dir_are_skipped(owner, grants, anchor):
    env = seal(owner, make_payload())
    write_grant(grants, env)
    name = env_mod.grant_filename(NOW - 30 * SEC, "og_" + "a" * 26)
    (grants / name).write_text("{not json", encoding="utf-8")
    (grants / ("%d-og_%s.json" % (NOW - 20 * SEC, "b" * 26))).mkdir()
    os.symlink(
        str(grants / env_mod.grant_filename(NOW - MIN, env.grant_id)),
        str(grants / ("%d-og_%s.json" % (NOW - 10 * SEC, "c" * 26))),
    )
    os.mkfifo(str(grants / ("%d-og_%s.json" % (NOW - 5 * SEC, "d" * 26))))
    (grants / "README.txt").write_text("hello", encoding="utf-8")
    result = check(anchor, text_sha=sha())
    assert result.ok and result.candidates == 1, result.to_dict()


def test_g12_missing_grants_dir_is_not_found(owner, grants, anchor):
    shutil.rmtree(str(grants))
    denied(check(anchor, text_sha=sha()), "not_found")


def test_g12_grant_id_is_derived_not_taken_from_the_file_name(owner, grants, anchor):
    env = seal(owner, make_payload())
    other_id = seal(
        owner, make_payload(text="A different owner line entirely.")
    ).grant_id
    write_grant(grants, env, name=env_mod.grant_filename(NOW - MIN, other_id))
    denied(check(anchor, grant_id=other_id), "not_found")


# -- G-17: candidate-first lookup ----------------------------------------------------------


def test_g17_only_candidates_are_signature_checked(owner, grants, anchor, monkeypatch):
    calls = []
    real_sig = verify_mod._signature_ok

    def counting(pub, message, sig):
        calls.append(message)
        return real_sig(pub, message, sig)

    monkeypatch.setattr(verify_mod, "_signature_ok", counting)
    for i in range(8):
        write_grant(
            grants,
            seal(
                owner,
                make_payload(
                    targets=[{"session_id": "other-%d" % i, "claude_session_id": None}],
                    issued_at=NOW - (i + 2) * MIN,
                ),
            ),
        )
    for i in range(6):
        write_grant(
            grants,
            seal(
                owner,
                make_payload(
                    text="Some other owner decision number %d." % i,
                    issued_at=NOW - (i + 20) * MIN,
                ),
            ),
        )
    for i in range(4):
        write_grant(
            grants,
            seal(owner, make_payload(scope=[GATE2], issued_at=NOW - (i + 40) * MIN)),
        )
    match = seal(owner, make_payload())
    write_grant(grants, match)
    result = check(anchor, text_sha=sha(), scopes=[GATE])
    assert result.ok and result.grant["id"] == match.grant_id, result.to_dict()
    assert result.candidates == 1
    assert len(calls) == 1


def test_g17_names_outside_the_ttl_window_are_never_opened(
    owner, grants, anchor, monkeypatch
):
    opened = []
    real_read = verify_mod._read_grant_file

    def counting(path):
        opened.append(os.path.basename(path))
        return real_read(path)

    monkeypatch.setattr(verify_mod, "_read_grant_file", counting)
    old = seal(
        owner, make_payload(issued_at=NOW - 8 * DAY, expires_at=NOW - 8 * DAY + HOUR)
    )
    future = seal(owner, make_payload(issued_at=NOW + HOUR))
    live = seal(owner, make_payload())
    for env in (old, future, live):
        write_grant(grants, env)
    assert check(anchor, text_sha=sha()).ok
    assert opened == [env_mod.grant_filename(NOW - MIN, live.grant_id)]


def test_g17_newest_valid_grant_wins(owner, grants, anchor):
    older = seal(owner, make_payload(issued_at=NOW - 3 * HOUR))
    newer = seal(owner, make_payload(issued_at=NOW - HOUR))
    write_grant(grants, older)
    write_grant(grants, newer)
    result = check(anchor, text_sha=sha())
    assert result.grant["id"] == newer.grant_id and result.candidates == 2


def test_g17_an_invalid_newest_candidate_falls_back_to_an_older_valid_one(
    owner, grants, anchor
):
    older = seal(owner, make_payload(issued_at=NOW - 3 * HOUR))
    expired = seal(owner, make_payload(issued_at=NOW - 2 * HOUR, expires_at=NOW - HOUR))
    write_grant(grants, older)
    write_grant(grants, expired)
    result = check(anchor, text_sha=sha())
    assert result.ok and result.grant["id"] == older.grant_id, result.to_dict()
    only_expired = check(anchor, grant_id=expired.grant_id)
    denied(only_expired, "expired")


def test_g17_all_candidates_invalid_reports_the_newest_reason(owner, grants, anchor):
    write_grant(
        grants,
        seal(owner, make_payload(issued_at=NOW - 2 * HOUR, expires_at=NOW - HOUR)),
    )
    result = denied(check(anchor, text_sha=sha()), "expired")
    assert result.exit_code == 1 and result.candidates == 1


def test_g17_signature_checks_are_bounded(owner, grants, anchor, monkeypatch):
    calls = []
    monkeypatch.setattr(
        verify_mod, "_signature_ok", lambda *a: calls.append(1) or False
    )
    for i in range(verify_mod.MAX_SIGNATURE_CHECKS + 5):
        write_grant(grants, seal(owner, make_payload(issued_at=NOW - (i + 1) * SEC)))
    denied(check(anchor, text_sha=sha()), "bad_signature")
    assert len(calls) == verify_mod.MAX_SIGNATURE_CHECKS


# -- quote tiers through the pipeline ------------------------------------------------------


def test_fragment_quote_is_refused(owner, grants, anchor):
    write_grant(grants, seal(owner, make_payload()))
    denied(check(anchor, quote="merge until CI is green"), "quote_fragment")
    audit_only = check(anchor, quote="merge until CI is green", allow_fragment=True)
    assert audit_only.ok and audit_only.match["kind"] == "fragment"


def test_quote_mismatch_and_hash_mismatch(owner, anchor):
    env = seal(owner, make_payload())
    denied(
        check_env(anchor, env, quote="Please merge everything right now."),
        "quote_mismatch",
    )
    denied(check_env(anchor, env, quote="short"), "quote_too_short")
    denied(check_env(anchor, env, text_sha="0" * 64), "quote_mismatch")


def test_a_too_short_quote_is_refused_before_lookup(owner, grants, anchor):
    write_grant(grants, seal(owner, make_payload(text="yes")))
    result = denied(check(anchor, quote="yes"), "quote_too_short")
    assert result.exit_code == 1 and result.candidates == 0


def test_a_caller_built_envelope_with_a_bad_shape_is_malformed(owner, anchor):
    good = seal(owner, make_payload())
    bad = env_mod.Envelope(kid=good.kid, payload=good.payload, sig=b"\x00" * 10)
    denied(check_env(anchor, bad), "malformed")
    denied(check_env(anchor, dict(good.to_dict(), extra="x")), "malformed")


def test_text_sha256_must_match_the_signed_text(owner, anchor):
    env = seal(owner, make_payload(text_sha256="0" * 64))
    denied(check_env(anchor, env), "malformed")


def test_text_len_must_match_utf8_bytes(owner, anchor):
    text = "approved 🚀"
    env = seal(owner, make_payload(text=text, text_len=len(text)))
    denied(check_env(anchor, env), "malformed")


# -- end to end: every denial with its reason code -----------------------------------------


def test_e2e_denials_with_reason_codes(grants):
    active, retired, revoked = Owner(), Owner(), Owner()
    anchor = make_anchor(
        grants,
        [
            active.anchor_key(),
            retired.anchor_key(status="retired", retired_at=NOW - 10 * DAY),
            revoked.anchor_key(status="revoked"),
        ],
    )
    cases = {}

    def add(label, owner, payload, **kw):
        env = seal(owner, payload)
        write_grant(grants, env)
        cases[label] = check(anchor, grant_id=env.grant_id, **kw)

    add("valid", active, make_payload(), scopes=[GATE])
    add("wrong_session", active, make_payload(nonce="n1"), session=OTHER_SESSION)
    add(
        "expired",
        active,
        make_payload(nonce="n2", issued_at=NOW - DAY, expires_at=NOW - HOUR),
    )
    add("wrong_scope", active, make_payload(nonce="n3"), scopes=[GATE2])
    add("revoked_key", revoked, make_payload(nonce="n4"))
    add(
        "retired_key",
        retired,
        make_payload(
            nonce="n5", issued_at=NOW - 11 * DAY, expires_at=NOW - 11 * DAY + 72 * HOUR
        ),
    )
    add("over_ttl", active, make_payload(nonce="n6", expires_at=NOW + 30 * DAY))
    add("uid", active, make_payload(nonce="n7"), uid=UID + 1)
    other_text = "Do not deploy until the canary is healthy."
    write_grant(grants, seal(active, make_payload(nonce="n8", text=other_text)))
    cases["fragment"] = check(anchor, quote="deploy until the canary is healthy")

    assert cases["valid"].ok, cases["valid"].to_dict()
    expected = {
        "wrong_session": ("session_mismatch", 1),
        "expired": ("expired", 1),
        "wrong_scope": ("scope_missing", 1),
        "fragment": ("quote_fragment", 1),
        "revoked_key": ("key_revoked", 1),
        "retired_key": ("key_retired", 1),
        "over_ttl": ("ttl_exceeded", 1),
        "uid": ("anchor_untrusted", 3),
    }
    for label, (reason, exit_code) in expected.items():
        got = cases[label]
        assert (got.ok, got.reason, got.exit_code) == (False, reason, exit_code), (
            label,
            got.to_dict(),
        )


# -- py3.9 isolated interpreter with a pre-computed signed fixture --------------------------

FIXTURE_SEED = bytes(range(100, 132))


def build_fixture():
    owner = Owner(seed=FIXTURE_SEED)
    payload = make_payload(
        nonce="AAAAAAAAAAAAAAAAAAAAAA",
        targets=[{"session_id": SESSION, "claude_session_id": CLAUDE}],
    )
    env = seal(owner, payload)
    return {
        "note": "Generated by tests/hermes_owner_grant/test_verify.py::build_fixture "
        "(deterministic Ed25519 seed). Test data only; not a real owner key.",
        "now": NOW,
        "uid": UID,
        "session": SESSION,
        "claude_session": CLAUDE,
        "scope": GATE,
        "text_sha256": sha(),
        "segment": "Do not merge until CI is green. Then ship it.",
        "fragment": "merge until CI is green",
        "anchor_key": owner.anchor_key(),
        "envelope": env.to_dict(),
        "grant_id": env.grant_id,
        "file_name": env_mod.grant_filename(payload["issued_at"], env.grant_id),
        "expires_at": payload["expires_at"],
    }


def test_fixture_is_reproducible_from_its_seed():
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == build_fixture()


EXERCISE = r"""
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
from hermes_owner_grant import anchor, verify
fx = json.load(open(sys.argv[2]))
d = os.path.realpath(tempfile.mkdtemp())
with open(os.path.join(d, fx["file_name"]), "w") as f:
    json.dump(fx["envelope"], f)
a = anchor.parse_anchor(json.dumps({"format": "hermes-owner-anchor/v1", "owner_uid": fx["uid"],
    "grants_dir": d, "keys": [fx["anchor_key"]]}).encode())
base = dict(anchor=a, session=fx["session"], claude_session=fx["claude_session"],
            now=fx["now"], uid=fx["uid"])
def run(label, **kw):
    args = dict(base); args.update(kw)
    r = verify.verify(**args)
    print(json.dumps({"case": label, "ok": r.ok, "reason": r.reason, "exit": r.exit_code,
                      "match": r.match["kind"] if r.match else None, "impl": r.to_dict()["verifier"]["impl"]}))
run("sha", text_sha=fx["text_sha256"], scopes=[fx["scope"]])
run("segment", quote=fx["segment"])
run("grant_id", grant_id=fx["grant_id"], scopes=[fx["scope"]])
run("fragment", quote=fx["fragment"])
run("wrong_session", text_sha=fx["text_sha256"], session="nope")
run("wrong_claude", grant_id=fx["grant_id"], claude_session="nope")
run("expired", grant_id=fx["grant_id"], now=fx["expires_at"] + 6000)
run("uid", text_sha=fx["text_sha256"], uid=fx["uid"] + 1)
import shutil; shutil.rmtree(d)
print(sys.version_info[:2] == (3, 9) or sys.version_info[:2])
"""


@pytest.mark.skipif(
    not os.path.exists("/usr/bin/python3"), reason="no /usr/bin/python3"
)
def test_isolated_system_python_verifies_the_precomputed_fixture():
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-c", EXERCISE, str(REPO_ROOT), str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = [
        json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")
    ]
    got = {r["case"]: (r["ok"], r["reason"], r["exit"], r["match"]) for r in rows}
    assert got == {
        "sha": (True, None, 0, "sha"),
        "segment": (True, None, 0, "segment"),
        "grant_id": (True, None, 0, "grant_id"),
        "fragment": (False, "quote_fragment", 1, "fragment"),
        "wrong_session": (False, "not_found", 4, None),
        "wrong_claude": (False, "claude_session_mismatch", 1, None),
        "expired": (False, "expired", 1, None),
        "uid": (False, "anchor_untrusted", 3, None),
    }
    assert all(r["impl"] == "pure" for r in rows)


# -- evidence-only results never exit 0 (review P2) ----------------------------------------


def test_allow_fragment_match_is_evidence_only_exit_6(owner, grants, anchor):
    write_grant(grants, seal(owner, make_payload()))
    result = check(anchor, quote="merge until CI is green", allow_fragment=True)
    assert result.ok is True and result.match["kind"] == "fragment"
    assert result.exit_code == 6 == verify_mod.EXIT_EVIDENCE
    assert result.to_dict()["match"]["kind"] == "fragment"


def test_audit_at_result_is_evidence_only_exit_6(owner, grants, anchor):
    payload = make_payload(issued_at=NOW - 2 * DAY, expires_at=NOW - DAY)
    env = seal(owner, payload)
    write_grant(grants, env)
    audited = check_env(anchor, env, audit_at=NOW - DAY - HOUR)
    assert audited.ok is True and audited.audit is True
    assert audited.exit_code == 6
    looked_up = check(anchor, text_sha=sha(), audit_at=NOW - DAY - HOUR)
    assert looked_up.ok is True and looked_up.to_dict()["audit"] is True
    assert looked_up.exit_code == 6
    # A denied audit stays a deny, not evidence.
    denied_audit = check_env(anchor, env, audit_at=NOW)
    assert denied_audit.ok is False and denied_audit.exit_code == 1


def test_whole_segment_quote_still_exits_0(owner, grants, anchor):
    write_grant(grants, seal(owner, make_payload()))
    result = check(anchor, quote="Do not merge until CI is green. Then ship it.")
    assert result.ok and result.match["kind"] == "segment" and result.exit_code == 0


def test_cli_evidence_only_results_exit_6(owner, grants, anchor):
    env = seal(owner, make_payload(issued_at=NOW - 2 * DAY, expires_at=NOW - DAY))
    write_grant(grants, env)
    fresh = seal(owner, make_payload(nonce="fresh"))
    write_grant(grants, fresh)

    def run(argv, quote_text=None):
        output = io.StringIO()
        code = cli_mod.main(
            ["verify", "--session", SESSION, "--claude-session", CLAUDE] + argv,
            anchor=anchor,
            uid=UID,
            now_ms=NOW,
            stdin=io.StringIO(quote_text or ""),
            stdout=output,
        )
        return code, json.loads(output.getvalue())

    code, body = run(["--grant", env.grant_id, "--audit-at", str(NOW - DAY - HOUR)])
    assert code == 6 and body["ok"] is True and body["audit"] is True
    code, body = run(
        ["--quote-stdin", "--allow-fragment"], quote_text="merge until CI is green"
    )
    assert code == 6 and body["ok"] is True and body["match"]["kind"] == "fragment"
    code, body = run(["--quote-stdin"], quote_text="merge until CI is green")
    assert code == 1 and body["reason"] == "quote_fragment"
    code, body = run(["--grant", fresh.grant_id])
    assert code == 0 and body["ok"] is True and body["audit"] is False
