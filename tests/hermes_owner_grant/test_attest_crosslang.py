"""b10 contract: the Python verifier accepts what Electron main signs (H6 vectors) and nothing across domains."""
from __future__ import annotations

import json
from pathlib import Path

from hermes_owner_grant import anchor as anchor_mod
from hermes_owner_grant import attest as attest_mod
from hermes_owner_grant import envelope as env_mod

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "attest_v1_fixture.json").read_text())


def _anchor(tmp_path):
    key = FIXTURE["anchor_key"]
    return anchor_mod.Anchor(
        owner_uid=FIXTURE["owner_uid"],
        grants_dir=str(tmp_path),
        keys=(anchor_mod.AnchorKey(
            kid=key["kid"], alg=key["alg"], pub=env_mod.b64url_decode(key["pub"]),
            status=key["status"], not_before=key["not_before"], retired_at=key["retired_at"],
        ),),
        verifier_sha256=None,
        sha256="0" * 64,
    )


def _verify(envelope, tmp_path):
    payload = FIXTURE["vectors"]["launch_attestation"]["payload"]
    return attest_mod.verify_attestation_envelope(
        envelope,
        claude_session=FIXTURE["claude_session"],
        uid=FIXTURE["owner_uid"],
        now=payload["issued_at"] + 60_000,
        anchor=_anchor(tmp_path),
    )


def test_ts_signed_attestation_verifies_in_python(tmp_path):
    result = _verify(FIXTURE["envelope"], tmp_path)
    assert result.ok, (result.reason, result.detail)
    assert result.hermes_session_id == FIXTURE["session"]
    assert result.binding_nonce == FIXTURE["binding_nonce"]
    assert result.project_root == FIXTURE["project_root"]


def test_ts_signed_binding_and_grant_never_verify_as_attestations(tmp_path):
    for name in ("binding_envelope", "grant_envelope"):
        assert not _verify(FIXTURE[name], tmp_path).ok, name


ISSUER_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "attest_issuer_vector.json"


def test_issuer_emitted_attestation_verifies(tmp_path):
    assert ISSUER_FIXTURE_PATH.exists(), "attest_issuer_vector.json fixture must exist"
    data = json.loads(ISSUER_FIXTURE_PATH.read_text())
    key = data["anchor_key"]
    anchor = anchor_mod.Anchor(
        owner_uid=data["owner_uid"],
        grants_dir=str(tmp_path),
        keys=(
            anchor_mod.AnchorKey(
                kid=key["kid"],
                alg=key["alg"],
                pub=env_mod.b64url_decode(key["pub"]),
                status=key["status"],
                not_before=key.get("not_before", 0),
                retired_at=key.get("retired_at"),
            ),
        ),
        verifier_sha256=None,
        sha256="0" * 64,
    )
    result = attest_mod.verify_attestation_envelope(
        data["envelope"],
        claude_session=data["claude_session_id"],
        uid=data["owner_uid"],
        now=data["issued_at"] + 60_000,
        session=data["hermes_session_id"],
        anchor=anchor,
    )
    assert result.ok, (result.reason, result.detail)
    assert result.hermes_session_id == data["hermes_session_id"]
    assert result.claude_session_id == data["claude_session_id"]
    assert result.binding_nonce == data["binding_nonce"]
    assert result.project_root == data["project_root"]


def test_ts_signed_binding_verifies_in_python(tmp_path):
    result = attest_mod.verify_binding_envelope(
        FIXTURE["binding_envelope"],
        profile=FIXTURE["profile"],
        session=FIXTURE["session"],
        uid=FIXTURE["owner_uid"],
        anchor=_anchor(tmp_path),
    )
    assert result.ok, (result.reason, result.detail)
    assert result.project_root == FIXTURE["project_root"]
    assert result.hermes_session_id == FIXTURE["session"]
    assert result.binding_nonce == FIXTURE["binding_nonce"]


def test_ts_binding_verifier_rejects_attestation_and_grant_envelopes(tmp_path):
    for name in ("envelope", "grant_envelope", "unbound_envelope"):
        res = attest_mod.verify_binding_envelope(
            FIXTURE[name],
            profile=FIXTURE["profile"],
            session=FIXTURE["session"],
            uid=FIXTURE["owner_uid"],
            anchor=_anchor(tmp_path),
        )
        assert not res.ok, name
        assert res.reason == attest_mod.REASON_MALFORMED


def test_ts_binding_verifier_rejects_retired_kid(tmp_path):
    key = FIXTURE["anchor_key"]
    retired_anchor = anchor_mod.Anchor(
        owner_uid=FIXTURE["owner_uid"],
        grants_dir=str(tmp_path),
        keys=(anchor_mod.AnchorKey(
            kid=key["kid"], alg=key["alg"], pub=env_mod.b64url_decode(key["pub"]),
            status="retired", not_before=key["not_before"], retired_at=1790000000000,
        ),),
        verifier_sha256=None,
        sha256="0" * 64,
    )
    res = attest_mod.verify_binding_envelope(
        FIXTURE["binding_envelope"],
        profile=FIXTURE["profile"],
        session=FIXTURE["session"],
        uid=FIXTURE["owner_uid"],
        anchor=retired_anchor,
    )
    assert not res.ok
    assert res.reason == anchor_mod.REASON_KEY_RETIRED


def test_ts_binding_verifier_rejects_state_unbound(tmp_path):
    import pytest
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization

    priv = ed.Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = env_mod.kid_for_pub(pub_bytes)
    test_anchor = anchor_mod.Anchor(
        owner_uid=FIXTURE["owner_uid"],
        grants_dir=str(tmp_path),
        keys=(anchor_mod.AnchorKey(
            kid=kid, alg="Ed25519", pub=pub_bytes,
            status="active", not_before=0, retired_at=None,
        ),),
        verifier_sha256=None,
        sha256="0" * 64,
    )
    payload = {
        "v": 1,
        "aud": ["hermes-main"],
        "owner_uid": FIXTURE["owner_uid"],
        "profile": FIXTURE["profile"],
        "hermes_session_id": FIXTURE["session"],
        "state": "unbound",
        "seq": 1,
        "binding_nonce": "nonce123",
        "bound_at": 1789999940000,
        "project_root": None,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    env = attest_mod.seal_binding(payload_bytes, kid, priv.sign)
    res = attest_mod.verify_binding_envelope(
        env,
        profile=FIXTURE["profile"],
        session=FIXTURE["session"],
        uid=FIXTURE["owner_uid"],
        anchor=test_anchor,
    )
    assert not res.ok
    assert res.reason == attest_mod.REASON_UNBOUND


def test_ts_binding_verifier_rejects_profile_and_session_mismatch(tmp_path):
    res_prof = attest_mod.verify_binding_envelope(
        FIXTURE["binding_envelope"],
        profile="mismatched_profile",
        session=FIXTURE["session"],
        uid=FIXTURE["owner_uid"],
        anchor=_anchor(tmp_path),
    )
    assert not res_prof.ok
    assert res_prof.reason == attest_mod.REASON_PROFILE_MISMATCH

    res_sess = attest_mod.verify_binding_envelope(
        FIXTURE["binding_envelope"],
        profile=FIXTURE["profile"],
        session="mismatched_session",
        uid=FIXTURE["owner_uid"],
        anchor=_anchor(tmp_path),
    )
    assert not res_sess.ok
    assert res_sess.reason == attest_mod.REASON_SESSION_MISMATCH
