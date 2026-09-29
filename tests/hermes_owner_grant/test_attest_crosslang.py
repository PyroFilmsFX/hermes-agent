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
