"""Owner verifier installation diagnostics (U8 / G-18)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from hermes_owner_grant import doctor


def _anchor(tmp_path, *, verifier_sha256=None):
    grants = tmp_path / "owner-grants"
    grants.mkdir()
    (grants / "grant.json").write_text("{}", encoding="utf-8")
    return SimpleNamespace(
        owner_uid=0,
        grants_dir=str(grants),
        keys=(SimpleNamespace(kid="key-1", status="active"),),
        verifier_sha256=verifier_sha256,
        sha256="anchor-hash",
    )


def test_doctor_reports_anchor_key_verifier_grants_and_reproducible_bundle(
    tmp_path, monkeypatch
):
    verifier = tmp_path / "hermes_owner_verify.py"
    verifier.write_bytes(b"pinned verifier")
    anchor = _anchor(tmp_path, verifier_sha256=hashlib.sha256(verifier.read_bytes()).hexdigest())
    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", lambda: anchor)
    monkeypatch.setattr(doctor, "VERIFIER_PATH", str(verifier))
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (True, "same"))
    monkeypatch.setattr(
        doctor.install_check,
        "check_installed_verifier",
        lambda _anchor: {"state": "match", "mismatched": []},
    )

    result = doctor.run_doctor()

    assert result["ok"] is True
    assert result["checks"] == {
        "anchor": "trusted",
        "keys": [{"kid": "key-1", "status": "active"}],
        "verifier_sha256": "match",
        "verifier_install": "match",
        "grants_dir": "readable",
        "bundle": "reproducible",
    }


def test_doctor_reports_untrusted_anchor_and_unavailable_key_state(monkeypatch):
    def fail_anchor():
        raise doctor.anchor_mod.AnchorError("anchor_untrusted", "bad mode")

    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", fail_anchor)
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (True, "same"))

    result = doctor.run_doctor()

    assert result["ok"] is False
    assert result["checks"]["anchor"] == "anchor_untrusted"
    assert result["checks"]["keys"] == "unavailable"


def test_doctor_fails_when_pinned_verifier_hash_does_not_match(tmp_path, monkeypatch):
    verifier = tmp_path / "hermes_owner_verify.py"
    verifier.write_bytes(b"different verifier")
    anchor = _anchor(tmp_path, verifier_sha256="0" * 64)
    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", lambda: anchor)
    monkeypatch.setattr(doctor, "VERIFIER_PATH", str(verifier))
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (True, "same"))

    result = doctor.run_doctor()

    assert result["ok"] is False
    assert result["checks"]["verifier_sha256"] == "mismatch"


def test_doctor_fails_if_grants_directory_cannot_be_read(tmp_path, monkeypatch):
    anchor = _anchor(tmp_path)
    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", lambda: anchor)
    monkeypatch.setattr(doctor.os, "listdir", lambda _path: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (True, "same"))

    result = doctor.run_doctor()

    assert result["ok"] is False
    assert result["checks"]["grants_dir"] == "unreadable"


def test_doctor_reports_bundle_build_failure(monkeypatch, tmp_path):
    anchor = _anchor(tmp_path)
    monkeypatch.setattr(doctor.anchor_mod, "load_trusted_anchor", lambda: anchor)
    monkeypatch.setattr(doctor, "build_reproducible", lambda: (False, "different output"))

    result = doctor.run_doctor()

    assert result["ok"] is False
    assert result["checks"]["bundle"] == "not_reproducible"


def test_bundle_reproducibility_check_builds_the_real_script_twice():
    reproducible, digest = doctor.build_reproducible()

    assert reproducible is True
    assert len(digest) == 64
