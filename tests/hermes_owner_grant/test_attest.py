"""Behavior contract tests for launch-attestation verifier (b10 H5, §0, §3, §4, §9)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

ed = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    reason="test keys are generated with the cryptography package",
)
from cryptography.hazmat.primitives import serialization  # noqa: E402

from hermes_owner_grant import anchor as anchor_mod  # noqa: E402
from hermes_owner_grant import attest as attest_mod  # noqa: E402
from hermes_owner_grant import cli as cli_mod  # noqa: E402
from hermes_owner_grant import envelope as env_mod  # noqa: E402
from hermes_owner_grant import verify as verify_mod  # noqa: E402

SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
NOW = 1_790_000_000_000
UID = 501
HERMES_SID = "20260929_101500_a1b2c3"
OTHER_HERMES_SID = "20260929_101500_ffffff"
CLAUDE_SID = "5b0d8a3e-0c7e-4c2e-9d5f-3f1f7d9a0b11"
OTHER_CLAUDE_SID = "99999999-0c7e-4c2e-9d5f-3f1f7d9a0b99"
PARENT_SID = "20260929_100000_root01"
PROJECT_ROOT = "/Users/justin/Documents/Projects/Business/hermes-cntrl"
REPO_COMMON_ROOT = "/Users/justin/Documents/Projects/Business/hermes-cntrl"
NONCE = "nonce_abc12345"


class Owner:
    """A real Ed25519 key from the cryptography package (tests only)."""

    def __init__(self, seed: Any = None) -> None:
        if seed is None:
            self.key = ed.Ed25519PrivateKey.generate()
        else:
            self.key = ed.Ed25519PrivateKey.from_private_bytes(seed)
        self.pub = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.kid = env_mod.kid_for_pub(self.pub)

    def sign(self, message: bytes) -> bytes:
        return self.key.sign(message)

    def anchor_key(
        self,
        status: str = "active",
        not_before: int = 0,
        retired_at: Any = None,
    ) -> Dict[str, Any]:
        return {
            "kid": self.kid,
            "alg": "Ed25519",
            "pub": env_mod.b64url_encode(self.pub),
            "status": status,
            "not_before": not_before,
            "retired_at": retired_at,
        }


def make_payload(**overrides: Any) -> Dict[str, Any]:
    issued_at = overrides.pop("issued_at", NOW - MIN)
    payload = {
        "v": 1,
        "aud": [attest_mod.AUDIENCE],
        "owner_uid": UID,
        "profile": "default",
        "backend": "spawn-1",
        "hermes_session_id": HERMES_SID,
        "claude_session_id": CLAUDE_SID,
        "launch_seq": 1,
        "project_root": PROJECT_ROOT,
        "repo_common_root": REPO_COMMON_ROOT,
        "binding_nonce": NONCE,
        "binding_seq": 1,
        "repo_remote": "git@github.com:example/repo.git",
        "issued_at": issued_at,
        "expires_at": issued_at + 15 * MIN,
        "hermes_lineage": [PARENT_SID],
    }
    payload.update(overrides)
    return payload


def make_grant_payload(**overrides: Any) -> Dict[str, Any]:
    issued_at = overrides.pop("issued_at", NOW - MIN)
    text = "Deploy production gate"
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
        "targets": [{"session_id": HERMES_SID, "claude_session_id": CLAUDE_SID}],
        "scope": ["conductor:gate:review-budget-enable"],
        "single_use": [],
        "subject": {},
        "text": text,
        "text_sha256": env_mod.hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_len": len(text.encode("utf-8")),
    }
    payload.update(overrides)
    return payload


def seal_attestation(owner: Owner, payload: Dict[str, Any]) -> attest_mod.AttestationEnvelope:
    return attest_mod.seal(env_mod.encode_payload(payload), owner.kid, owner.sign)


def make_anchor(grants_dir: Path, keys: Any, owner_uid: int = UID) -> anchor_mod.Anchor:
    doc = {
        "format": "hermes-owner-anchor/v1",
        "owner_uid": owner_uid,
        "grants_dir": os.path.realpath(str(grants_dir)),
        "keys": keys,
        "verifier_sha256": "ab" * 32,
    }
    return anchor_mod.parse_anchor(json.dumps(doc).encode("utf-8"))


def write_attestation(
    grants_dir: Path,
    claude_sid: str,
    envelope: attest_mod.AttestationEnvelope,
    filename: Any = None,
) -> Path:
    payload = env_mod.decode_payload(envelope.payload)
    d = Path(grants_dir) / "session-attest" / claude_sid
    d.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = "%d-%s.json" % (payload["issued_at"], envelope.kid)
    path = d / filename
    path.write_text(envelope.to_json(), encoding="utf-8")
    return path


@pytest.fixture
def owner() -> Owner:
    return Owner()


@pytest.fixture
def grants(tmp_path: Path) -> Path:
    d = tmp_path / "owner-grants"
    d.mkdir()
    return d


@pytest.fixture
def anchor(grants: Path, owner: Owner) -> anchor_mod.Anchor:
    return make_anchor(grants, [owner.anchor_key()])


def test_valid_attestation_verifies_and_returns_fields(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    payload = make_payload()
    env = seal_attestation(owner, payload)
    write_attestation(grants, CLAUDE_SID, env)

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is True
    assert result.reason is None
    assert result.detail is None
    assert result.exit_code == 0
    assert result.hermes_session_id == HERMES_SID
    assert result.claude_session_id == CLAUDE_SID
    assert result.project_root == PROJECT_ROOT
    assert result.repo_common_root == REPO_COMMON_ROOT
    assert result.binding_nonce == NONCE
    assert result.hermes_lineage == [PARENT_SID]
    assert result.candidates == 1
    assert result["hermes_session_id"] == HERMES_SID
    assert result.to_dict()["hermes_lineage"] == [PARENT_SID]


def test_grant_envelope_rejected_as_attestation_and_attestation_rejected_by_grant_verify(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # 1. Grant envelope passed to attest verifier is rejected (format mismatch)
    grant_payload = make_grant_payload()
    grant_env = env_mod.seal(env_mod.encode_payload(grant_payload), owner.kid, owner.sign)
    res_grant_on_attest = attest_mod.verify_attestation_envelope(
        grant_env,
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert res_grant_on_attest.ok is False
    assert res_grant_on_attest.reason == "malformed"
    assert "format is not hermes-launch-attestation/v1" in (res_grant_on_attest.detail or "")

    # 2. Attestation envelope passed to grant verifier is rejected (format mismatch)
    attest_payload = make_payload()
    attest_env = seal_attestation(owner, attest_payload)
    res_attest_on_grant = verify_mod.verify_envelope(
        attest_env.to_dict(),
        session=HERMES_SID,
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
        scopes=["conductor:gate:review-budget-enable"],
    )
    assert res_attest_on_grant.ok is False
    assert res_attest_on_grant.reason == "malformed"

    # 3. Change format string to bypass format check; Ed25519 domain prefix rejects signature
    tampered_grant = dict(grant_env.to_dict())
    tampered_grant["format"] = attest_mod.FORMAT
    res_tampered_grant = attest_mod.verify_attestation_envelope(
        tampered_grant,
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert res_tampered_grant.ok is False
    assert res_tampered_grant.reason == "bad_signature"

    tampered_attest = dict(attest_env.to_dict())
    tampered_attest["format"] = env_mod.FORMAT
    res_tampered_attest = verify_mod.verify_envelope(
        tampered_attest,
        session=HERMES_SID,
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
        scopes=["conductor:gate:review-budget-enable"],
    )
    assert res_tampered_attest.ok is False
    assert res_tampered_attest.reason == "bad_signature"


def test_ttl_greater_than_30_min_rejected(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    issued_at = NOW - MIN
    # Exactly 30 min + 1 ms exceeds the 30 min cap
    payload = make_payload(issued_at=issued_at, expires_at=issued_at + 30 * MIN + 1)
    env = seal_attestation(owner, payload)
    write_attestation(grants, CLAUDE_SID, env)

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is False
    assert result.reason == "ttl_exceeded"
    assert result.exit_code == 1


def test_expired_rejected(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # 1. Past expiration
    issued_at = NOW - 20 * MIN
    expires_at = NOW - 5 * MIN
    payload = make_payload(issued_at=issued_at, expires_at=expires_at)
    env = seal_attestation(owner, payload)
    write_attestation(grants, CLAUDE_SID, env)

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is False
    assert result.reason == "expired"
    assert result.exit_code == 1

    # 2. Future issued beyond skew
    future_issued = NOW + 10 * MIN
    payload_future = make_payload(
        claude_session_id="future-claude-sid",
        issued_at=future_issued,
        expires_at=future_issued + 20 * MIN,
    )
    env_future = seal_attestation(owner, payload_future)
    write_attestation(grants, "future-claude-sid", env_future)

    res_future = attest_mod.verify_attestation(
        claude_session="future-claude-sid",
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert res_future.ok is False
    assert res_future.reason == "expired"
    assert res_future.exit_code == 1


def test_tampered_payload_rejected(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    payload = make_payload()
    env = seal_attestation(owner, payload)
    raw = env.to_dict()
    # Tamper payload
    decoded = json.loads(env_mod.b64url_decode(raw["payload"]))
    decoded["project_root"] = "/tampered/path"
    raw["payload"] = env_mod.b64url_encode(json.dumps(decoded).encode("utf-8"))

    d = Path(grants) / "session-attest" / CLAUDE_SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "tampered.json").write_text(json.dumps(raw), encoding="utf-8")

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is False
    assert result.reason == "bad_signature"
    assert result.exit_code == 1


def test_newest_valid_wins_among_several_for_one_claude_sid(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # Older valid
    p1 = make_payload(issued_at=NOW - 15 * MIN, project_root="/project/older")
    e1 = seal_attestation(owner, p1)
    write_attestation(grants, CLAUDE_SID, e1, filename="1-older.json")

    # Newer valid
    p2 = make_payload(issued_at=NOW - 8 * MIN, project_root="/project/newer")
    e2 = seal_attestation(owner, p2)
    write_attestation(grants, CLAUDE_SID, e2, filename="2-newer.json")

    # Newest but bad signature
    p3 = make_payload(issued_at=NOW - 2 * MIN, project_root="/project/tampered")
    e3 = seal_attestation(owner, p3)
    raw3 = e3.to_dict()
    raw3["payload"] = env_mod.b64url_encode(b'{"forged": true}')
    (Path(grants) / "session-attest" / CLAUDE_SID / "3-bad-sig.json").write_text(
        json.dumps(raw3), encoding="utf-8"
    )

    # Newest but corrupt non-json file
    (Path(grants) / "session-attest" / CLAUDE_SID / "4-corrupt.json").write_text(
        "not json at all", encoding="utf-8"
    )

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is True
    assert result.project_root == "/project/newer"
    assert result.issued_at == NOW - 8 * MIN


def test_file_outside_anchor_pinned_dir_is_ignored(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor, tmp_path: Path, monkeypatch: Any
) -> None:
    payload = make_payload()
    env = seal_attestation(owner, payload)

    # Put attestation outside anchor.grants_dir
    outside_dir = tmp_path / "somewhere_else"
    outside_dir.mkdir(parents=True)
    (outside_dir / "attest.json").write_text(env.to_json(), encoding="utf-8")

    # Even if diagnostic hint env var is set, it must be ignored
    monkeypatch.setenv("HERMES_SESSION_ATTEST_DIR", str(outside_dir))

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is False
    assert result.reason == "not_found"
    assert result.exit_code == 4


def test_lineage_greater_than_16_rejected(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # 17 entries -> rejected
    lineage_17 = ["parent_%d" % i for i in range(17)]
    payload_17 = make_payload(hermes_lineage=lineage_17)
    env_17 = seal_attestation(owner, payload_17)
    write_attestation(grants, CLAUDE_SID, env_17)

    result = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result.ok is False
    assert result.reason == "malformed"
    assert "hermes_lineage exceeds 16 entries" in (result.detail or "")

    # 16 entries -> accepted
    lineage_16 = ["parent_%d" % i for i in range(16)]
    payload_16 = make_payload(claude_session_id="claude-16", hermes_lineage=lineage_16)
    env_16 = seal_attestation(owner, payload_16)
    write_attestation(grants, "claude-16", env_16)

    res_16 = attest_mod.verify_attestation(
        claude_session="claude-16",
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert res_16.ok is True
    assert len(res_16.hermes_lineage) == 16


def test_unbind_reports_unbound_and_deletion_restores_previous(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # Older bound
    p1 = make_payload(issued_at=NOW - 10 * MIN, binding_nonce="nonce_active")
    e1 = seal_attestation(owner, p1)
    write_attestation(grants, CLAUDE_SID, e1, filename="1-bound.json")

    # Newer unbound (project_root and binding_nonce null)
    p2 = make_payload(
        issued_at=NOW - 5 * MIN,
        binding_nonce=None,
        project_root=None,
    )
    e2 = seal_attestation(owner, p2)
    unbind_path = write_attestation(grants, CLAUDE_SID, e2, filename="2-unbound.json")

    result_unbound = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result_unbound.ok is False
    assert result_unbound.reason == "unbound"
    assert result_unbound.exit_code == 1
    assert result_unbound.binding_nonce is None
    assert result_unbound.hermes_session_id == HERMES_SID

    # Delete unbound file; previous bound attestation verifies
    unbind_path.unlink()

    result_restored = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert result_restored.ok is True
    assert result_restored.binding_nonce == "nonce_active"
    assert result_restored.project_root == PROJECT_ROOT


def test_expected_hermes_session_check(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    payload = make_payload()
    env = seal_attestation(owner, payload)
    write_attestation(grants, CLAUDE_SID, env)

    # Matching session
    ok_res = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        session=HERMES_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert ok_res.ok is True

    # Mismatched session
    bad_res = attest_mod.verify_attestation(
        claude_session=CLAUDE_SID,
        session=OTHER_HERMES_SID,
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert bad_res.ok is False
    assert bad_res.reason == "session_mismatch"
    assert bad_res.exit_code == 1


def test_project_root_non_absolute_or_non_normalized_rejected(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    cases = [
        ("relative/path", "relative"),
        ("/abs/path/../sub", "non-normalized dotdot"),
        ("/abs/path//double", "double slash"),
        ("/abs/path/", "trailing slash"),
    ]
    for bad_path, label in cases:
        sid = "claude-" + env_mod.derive_grant_id(label.encode())[:10]
        payload = make_payload(claude_session_id=sid, project_root=bad_path)
        env = seal_attestation(owner, payload)
        write_attestation(grants, sid, env)

        res = attest_mod.verify_attestation(
            claude_session=sid,
            uid=UID,
            now=NOW,
            anchor=anchor,
        )
        assert res.ok is False, label
        assert res.reason == "malformed", label


def test_uid_and_audience_checks(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    # 1. UID mismatch
    p_uid = make_payload(claude_session_id="c-uid", owner_uid=UID + 1)
    write_attestation(grants, "c-uid", seal_attestation(owner, p_uid))
    r_uid = attest_mod.verify_attestation(
        claude_session="c-uid",
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert r_uid.ok is False
    assert r_uid.reason == "uid_mismatch"

    # 2. Wrong audience
    p_aud = make_payload(claude_session_id="c-aud", aud=["other:audience"])
    write_attestation(grants, "c-aud", seal_attestation(owner, p_aud))
    r_aud = attest_mod.verify_attestation(
        claude_session="c-aud",
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert r_aud.ok is False
    assert r_aud.reason == "wrong_audience"

    # 3. Disallowed audience (hermes-owner-verify)
    p_dis = make_payload(
        claude_session_id="c-dis",
        aud=[attest_mod.AUDIENCE, attest_mod.DISALLOWED_AUDIENCE],
    )
    write_attestation(grants, "c-dis", seal_attestation(owner, p_dis))
    r_dis = attest_mod.verify_attestation(
        claude_session="c-dis",
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert r_dis.ok is False
    assert r_dis.reason == "wrong_audience"


def test_key_status_checks(
    owner: Owner, grants: Path
) -> None:
    # Revoked key
    revoked_anchor = make_anchor(grants, [owner.anchor_key(status="revoked")])
    p1 = make_payload(claude_session_id="c-rev")
    write_attestation(grants, "c-rev", seal_attestation(owner, p1))
    r_rev = attest_mod.verify_attestation(
        claude_session="c-rev",
        uid=UID,
        now=NOW,
        anchor=revoked_anchor,
    )
    assert r_rev.ok is False
    assert r_rev.reason == "key_revoked"

    # Retired key past retirement
    retired_anchor = make_anchor(
        grants, [owner.anchor_key(status="retired", retired_at=NOW - 5 * MIN)]
    )
    p2 = make_payload(claude_session_id="c-ret", issued_at=NOW - 2 * MIN)
    write_attestation(grants, "c-ret", seal_attestation(owner, p2))
    r_ret = attest_mod.verify_attestation(
        claude_session="c-ret",
        uid=UID,
        now=NOW,
        anchor=retired_anchor,
    )
    assert r_ret.ok is False
    assert r_ret.reason == "key_retired"

    # Unknown kid
    other_owner = Owner()
    p3 = make_payload(claude_session_id="c-unk")
    write_attestation(grants, "c-unk", seal_attestation(other_owner, p3))
    r_unk = attest_mod.verify_attestation(
        claude_session="c-unk",
        uid=UID,
        now=NOW,
        anchor=retired_anchor,
    )
    assert r_unk.ok is False
    assert r_unk.reason == "unknown_kid"


def test_cli_verify_attestation_op(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    payload = make_payload()
    env = seal_attestation(owner, payload)
    write_attestation(grants, CLAUDE_SID, env)

    # 1. CLI with --claude-session
    out1 = io.StringIO()
    code1 = cli_mod.main(
        ["verify-attestation", "--claude-session", CLAUDE_SID],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdout=out1,
    )
    assert code1 == 0
    body1 = json.loads(out1.getvalue())
    assert body1["ok"] is True
    assert body1["hermes_session_id"] == HERMES_SID
    assert body1["claude_session_id"] == CLAUDE_SID
    assert body1["project_root"] == PROJECT_ROOT
    assert body1["repo_common_root"] == REPO_COMMON_ROOT
    assert body1["binding_nonce"] == NONCE
    assert body1["hermes_lineage"] == [PARENT_SID]

    # 2. CLI with JSON on stdin (hook stdin style)
    out2 = io.StringIO()
    in2 = io.StringIO(json.dumps({"session_id": CLAUDE_SID}))
    code2 = cli_mod.main(
        ["verify-attestation"],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdin=in2,
        stdout=out2,
    )
    assert code2 == 0
    body2 = json.loads(out2.getvalue())
    assert body2["ok"] is True
    assert body2["hermes_session_id"] == HERMES_SID

    # 3. CLI with plain text claude sid on stdin
    out3 = io.StringIO()
    in3 = io.StringIO(CLAUDE_SID)
    code3 = cli_mod.main(
        ["verify-attestation", "--claude-session-stdin"],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdin=in3,
        stdout=out3,
    )
    assert code3 == 0
    body3 = json.loads(out3.getvalue())
    assert body3["ok"] is True

    # 4. CLI with mismatched --session
    out4 = io.StringIO()
    code4 = cli_mod.main(
        ["verify-attestation", "--claude-session", CLAUDE_SID, "--session", "wrong_sid"],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdout=out4,
    )
    assert code4 == 1
    body4 = json.loads(out4.getvalue())
    assert body4["ok"] is False
    assert body4["reason"] == "session_mismatch"

    # 5. CLI with non-existent claude session
    out5 = io.StringIO()
    code5 = cli_mod.main(
        ["verify-attestation", "--claude-session", "non-existent-sid"],
        anchor=anchor,
        uid=UID,
        now_ms=NOW,
        stdout=out5,
    )
    assert code5 == 4
    body5 = json.loads(out5.getvalue())
    assert body5["ok"] is False
    assert body5["reason"] == "not_found"


def make_binding_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "v": 1,
        "aud": [attest_mod.BINDING_AUDIENCE],
        "owner_uid": UID,
        "profile": "default",
        "hermes_session_id": HERMES_SID,
        "state": "bound",
        "seq": 1,
        "binding_nonce": NONCE,
        "bound_at": NOW - MIN,
        "project_root": PROJECT_ROOT,
        "repo_common_root": REPO_COMMON_ROOT,
        "repo_remote": "git@github.com:example/repo.git",
        "project_id": "p_123",
        "carried_from": None,
    }
    payload.update(overrides)
    return payload


def test_binding_envelope_and_seal(tmp_path: Path) -> None:
    owner = Owner()
    payload = make_binding_payload()
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    env = attest_mod.seal_binding(payload_bytes, owner.kid, owner.sign)
    assert env.format == attest_mod.BINDING_FORMAT
    assert env.kid == owner.kid

    parsed = attest_mod.parse_binding_envelope(env.to_dict())
    assert parsed.kid == owner.kid
    assert parsed.payload == payload_bytes

    parsed_json = attest_mod.parse_binding_envelope(env.to_json())
    assert parsed_json.kid == owner.kid

    file_path = tmp_path / "binding.json"
    file_path.write_text(env.to_json(), encoding="utf-8")
    from_file = attest_mod.read_binding_file(str(file_path))
    assert from_file.kid == owner.kid


def test_verify_binding_envelope_unit(tmp_path: Path) -> None:
    owner = Owner()
    anchor = anchor_mod.Anchor(
        owner_uid=UID,
        grants_dir=str(tmp_path),
        keys=(
            anchor_mod.AnchorKey(
                kid=owner.kid,
                alg="Ed25519",
                pub=owner.pub,
                status="active",
                not_before=0,
                retired_at=None,
            ),
        ),
        verifier_sha256=None,
        sha256="0" * 64,
    )
    payload = make_binding_payload()
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    env = attest_mod.seal_binding(payload_bytes, owner.kid, owner.sign)

    # Success
    res = attest_mod.verify_binding_envelope(
        env,
        profile="default",
        session=HERMES_SID,
        uid=UID,
        anchor=anchor,
    )
    assert res.ok
    assert res.project_root == PROJECT_ROOT
    assert res.hermes_session_id == HERMES_SID

    # Bad signature
    tampered_env = attest_mod.BindingEnvelope(kid=owner.kid, payload=payload_bytes, sig=b"\x00" * 64)
    res_bad_sig = attest_mod.verify_binding_envelope(
        tampered_env, profile="default", session=HERMES_SID, uid=UID, anchor=anchor,
    )
    assert not res_bad_sig.ok
    assert res_bad_sig.reason == attest_mod.REASON_BAD_SIGNATURE

    # Wrong audience
    wrong_aud_payload = make_binding_payload(aud=["wrong-aud"])
    env_wrong_aud = attest_mod.seal_binding(
        json.dumps(wrong_aud_payload).encode("utf-8"), owner.kid, owner.sign
    )
    res_aud = attest_mod.verify_binding_envelope(
        env_wrong_aud, profile="default", session=HERMES_SID, uid=UID, anchor=anchor
    )
    assert not res_aud.ok
    assert res_aud.reason == attest_mod.REASON_WRONG_AUDIENCE

    # UID mismatch
    res_uid = attest_mod.verify_binding_envelope(
        env, profile="default", session=HERMES_SID, uid=999, anchor=anchor
    )
    assert not res_uid.ok
    assert res_uid.reason in (anchor_mod.REASON_ANCHOR_UNTRUSTED, attest_mod.REASON_UID_MISMATCH)


def test_verify_binding_file_helper(tmp_path: Path) -> None:
    owner = Owner()
    anchor = anchor_mod.Anchor(
        owner_uid=UID,
        grants_dir=str(tmp_path),
        keys=(
            anchor_mod.AnchorKey(
                kid=owner.kid,
                alg="Ed25519",
                pub=owner.pub,
                status="active",
                not_before=0,
                retired_at=None,
            ),
        ),
        verifier_sha256=None,
        sha256="0" * 64,
    )
    payload = make_binding_payload()
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    env = attest_mod.seal_binding(payload_bytes, owner.kid, owner.sign)

    binding_path = tmp_path / "binding.json"
    binding_path.write_text(env.to_json(), encoding="utf-8")

    res = attest_mod.verify_binding_file(
        str(binding_path),
        profile="default",
        session=HERMES_SID,
        uid=UID,
        anchor=anchor,
    )
    assert res.ok
    assert res.project_root == PROJECT_ROOT

    # Missing file
    res_missing = attest_mod.verify_binding_file(
        str(tmp_path / "missing.json"),
        profile="default",
        session=HERMES_SID,
        uid=UID,
        anchor=anchor,
    )
    assert not res_missing.ok
    assert res_missing.reason == attest_mod.REASON_NOT_FOUND


# --- b10 review (grok #3): attestations need the ACTIVE kid --------------------------------


def test_retired_kid_attestation_issued_before_retirement_is_rejected(
    owner: Owner, grants: Path
) -> None:
    """The grant rule honours a retired kid for artifacts issued before retirement; attestations
    must not (main re-signs live attestations at rotation)."""
    retired_anchor = make_anchor(
        grants, [owner.anchor_key(status="retired", retired_at=NOW - MIN)]
    )
    payload = make_payload(claude_session_id="c-ret-before", issued_at=NOW - 2 * MIN)
    env = seal_attestation(owner, payload)
    write_attestation(grants, "c-ret-before", env)
    res = attest_mod.verify_attestation(
        claude_session="c-ret-before", uid=UID, now=NOW, anchor=retired_anchor
    )
    assert res.ok is False
    assert res.reason == "key_retired"
    direct = attest_mod.verify_attestation_envelope(
        env, claude_session="c-ret-before", uid=UID, now=NOW, anchor=retired_anchor
    )
    assert direct.ok is False
    assert direct.reason == "key_retired"


def test_retired_kid_rejected_even_when_an_active_kid_exists(owner: Owner, grants: Path) -> None:
    new_owner = Owner()
    rotated = make_anchor(
        grants,
        [
            owner.anchor_key(status="retired", retired_at=NOW - MIN),
            new_owner.anchor_key(status="active", not_before=NOW - MIN),
        ],
    )
    old = make_payload(claude_session_id="c-rot", issued_at=NOW - 2 * MIN)
    write_attestation(grants, "c-rot", seal_attestation(owner, old))
    res = attest_mod.verify_attestation(claude_session="c-rot", uid=UID, now=NOW, anchor=rotated)
    assert res.ok is False
    assert res.reason == "key_retired"
    # The re-signed copy under the new active kid verifies.
    fresh = make_payload(claude_session_id="c-rot", issued_at=NOW - 10 * SEC)
    write_attestation(grants, "c-rot", seal_attestation(new_owner, fresh))
    ok = attest_mod.verify_attestation(claude_session="c-rot", uid=UID, now=NOW, anchor=rotated)
    assert ok.ok is True


# --- b10 review (codex #1): resume provenance --------------------------------------------


def test_launch_provenance_accepts_an_expired_signed_attestation(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor
) -> None:
    old = make_payload(issued_at=NOW - 5 * HOUR)  # long expired
    write_attestation(grants, CLAUDE_SID, seal_attestation(owner, old))
    res = attest_mod.verify_launch_provenance(
        claude_session=CLAUDE_SID, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=anchor
    )
    assert res.ok is True
    assert res.hermes_session_id == HERMES_SID
    # An ancestor in the caller's lineage is also accepted.
    via_lineage = attest_mod.verify_launch_provenance(
        claude_session=CLAUDE_SID,
        hermes_sessions=["20260929_120000_child1", HERMES_SID],
        uid=UID,
        now=NOW,
        anchor=anchor,
    )
    assert via_lineage.ok is True


def test_launch_provenance_refuses_other_session_forgery_retired_kid_and_symlink(
    owner: Owner, grants: Path, anchor: anchor_mod.Anchor, tmp_path: Path
) -> None:
    write_attestation(grants, CLAUDE_SID, seal_attestation(owner, make_payload()))
    other = attest_mod.verify_launch_provenance(
        claude_session=CLAUDE_SID, hermes_sessions=[OTHER_HERMES_SID], uid=UID, now=NOW, anchor=anchor
    )
    assert other.ok is False
    assert other.reason == attest_mod.REASON_SESSION_MISMATCH

    wrong_profile = attest_mod.verify_launch_provenance(
        claude_session=CLAUDE_SID,
        hermes_sessions=[HERMES_SID],
        uid=UID,
        now=NOW,
        profile="work",
        anchor=anchor,
    )
    assert wrong_profile.ok is False

    # Forged: signed by a key the anchor does not know, and a tampered signature.
    forger = Owner()
    forged_sid = "11111111-2222-4333-8444-555555555555"
    write_attestation(grants, forged_sid, seal_attestation(forger, make_payload(claude_session_id=forged_sid)))
    unk = attest_mod.verify_launch_provenance(
        claude_session=forged_sid, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=anchor
    )
    assert unk.ok is False and unk.reason == "unknown_kid"
    bad_sid = "22222222-2222-4333-8444-555555555555"
    good = seal_attestation(owner, make_payload(claude_session_id=bad_sid))
    tampered = attest_mod.AttestationEnvelope(kid=good.kid, payload=good.payload, sig=bytes(64))
    write_attestation(grants, bad_sid, tampered)
    bad = attest_mod.verify_launch_provenance(
        claude_session=bad_sid, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=anchor
    )
    assert bad.ok is False and bad.reason == attest_mod.REASON_BAD_SIGNATURE

    retired = make_anchor(grants, [owner.anchor_key(status="retired", retired_at=NOW - MIN)])
    ret = attest_mod.verify_launch_provenance(
        claude_session=CLAUDE_SID, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=retired
    )
    assert ret.ok is False and ret.reason == "key_retired"

    # A symlinked sid directory is never followed.
    real = tmp_path / "elsewhere"
    real.mkdir()
    link_sid = "33333333-2222-4333-8444-555555555555"
    env = seal_attestation(owner, make_payload(claude_session_id=link_sid))
    (real / "1-x.json").write_text(env.to_json(), encoding="utf-8")
    os.symlink(str(real), str(grants / "session-attest" / link_sid))
    linked = attest_mod.verify_launch_provenance(
        claude_session=link_sid, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=anchor
    )
    assert linked.ok is False and linked.reason == attest_mod.REASON_NOT_FOUND

    for unsafe in ("/etc", "..", ".", "a/b", "a\\b", "x\x00y", ""):
        with pytest.raises(attest_mod.AttestUsageError):
            attest_mod.verify_launch_provenance(
                claude_session=unsafe, hermes_sessions=[HERMES_SID], uid=UID, now=NOW, anchor=anchor
            )
