"""Live RCA 2026-09-28 (#60): "Forward to…" from the manager session to cntrl-core-worker delivered nothing.

Regression cover for the gateway half of that path:

* a grant shaped exactly like the one Electron main signs for a quote-only "Forward to…" (gesture
  ``selection``, no message id, unbound target CLI) reaches the target as an OWNER turn carrying the
  envelope, through ``prompt.submit`` and never the peer mailbox, and the target-side hook check
  (``hermes_owner_grant.verify.verify_envelope`` against the same anchor) verifies it;
* the key-enrolled-after-backend-spawn case: the backend started with ``HERMES_OWNER_GRANT_KEYS`` empty
  and no anchor, the owner enabled owner grants afterwards, and the SAME backend (no restart) accepts
  the grant, because the anchor is read per call and the spawn-time keys hint is never read;
* every refusal leaves one log line with a code, never the text.

Temp anchor (in-process ``parse_anchor``), temp state.db, temp HERMES_HOME: never the real ones.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hermes_owner_grant import anchor as anchor_mod
from hermes_owner_grant import envelope as grant_envelope
from hermes_owner_grant import verify as verify_mod
from hermes_state import SessionDB

ORIGIN = "20260909_193713_ce3d96"  # the manager session (Claude Agent SDK)
TARGET = "20260924_200237_d5276f"  # cntrl-core-worker
TEXT = ("approve: merge w6/wd-ci-hold (both commits), and add the drafted PR/CI discipline rules to "
        "CLAUDE.md/AGENTS.md")
QUOTE_ONLY_TTL_MS = 7 * 24 * 60 * 60 * 1000


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class _ClientTransport:
    session_spawn_capability = None

    def write(self, obj) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def live(monkeypatch, tmp_path):
    import tui_gateway.server as server
    from tui_gateway import owner_forward as ofm
    from tui_gateway import session_mailbox as mb

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(tmp_path / "state.db")
    db.create_session(ORIGIN, "desktop")
    db.set_session_title(ORIGIN, "manager")
    db.create_session(TARGET, "desktop")
    db.set_session_title(TARGET, "cntrl-core-worker")
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(mb, "policy", lambda: dict(mb._DEFAULTS, startup_delay_s=0, retry_interval_s=0))
    monkeypatch.setattr(ofm, "policy", lambda: dict(ofm._DEFAULTS))

    # The target is live (a runtime session bound to its stored id), mid-turn like the real worker.
    sessions["rt-target"] = {"session_key": TARGET, "profile_home": None, "transport": server._detached_ws_transport}

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = grant_envelope.kid_for_pub(pub)
    anchor = anchor_mod.parse_anchor(json.dumps({
        "format": "hermes-owner-anchor/v1", "owner_uid": os.getuid(), "grants_dir": str(tmp_path / "owner-grants"),
        "keys": [{"kid": kid, "alg": "Ed25519", "pub": _b64u(pub), "status": "active", "not_before": 0,
                  "retired_at": None}]}).encode("utf-8"))

    # The root anchor as the backend sees it on each call: absent until the owner's one-time enable.
    installed = {"anchor": None}

    def load_trusted_anchor():
        if installed["anchor"] is None:
            raise anchor_mod.AnchorError("anchor_missing", "no anchor installed")
        return installed["anchor"]

    monkeypatch.setattr(ofm, "_load_anchor", load_trusted_anchor)
    ofm._reset_for_tests()

    submits: list = []

    def submit(rid, params):
        submits.append(dict(params))
        return {"result": {"status": "queued"}}

    def peer_send(*_a, **_k):  # the peer mailbox must never carry an owner forward
        raise AssertionError("owner forward went through the peer mailbox")

    monkeypatch.setitem(server._methods, "prompt.submit", submit)
    if hasattr(mb, "send_peer_message"):
        monkeypatch.setattr(mb, "send_peer_message", peer_send)

    def call(params):
        from tui_gateway.transport import bind_transport, reset_transport

        token = bind_transport(_ClientTransport())
        try:
            return server.handle_request({"id": "of-live", "method": "owner.forward", "params": params})
        finally:
            reset_transport(token)

    def main_signed_grant(backend_id: str, text: str = TEXT) -> dict:
        """The envelope Electron main signs for this forward (owner-grant-sign.ts, quote-only)."""
        profile = server._current_profile_name()
        now = int(time.time() * 1000)
        forward_targets = [f"{profile}:{TARGET}"]
        claims = {
            "v": 1, "aud": ["hermes-owner-forward", "hermes-owner-verify"], "decision_id": "od_" + "b" * 26,
            "issued_at": now, "deliver_by": now + 60_000, "expires_at": now + QUOTE_ONLY_TTL_MS,
            "owner_uid": os.getuid(), "backend": backend_id, "nonce": _b64u(os.urandom(16)),
            "gesture": "selection", "confirm": "native_dialog",
            "source_session": {"session_id": ORIGIN, "message_id": None, "role": None},
            "targets": [{"session_id": TARGET, "claude_session_id": None}], "forward_targets": forward_targets,
            "scope": [], "single_use": [], "subject": {}, "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "text_len": len(text.encode()),
        }
        env = grant_envelope.seal(grant_envelope.encode_payload(claims), kid, key.sign)
        return {"envelope": env.to_dict(), "text": text, "targets": forward_targets}

    yield SimpleNamespace(server=server, ofm=ofm, anchor=anchor, installed=installed, submits=submits,
                          call=call, grant=main_signed_grant)
    ofm._reset_for_tests()


def _bind_backend_at_spawn(monkeypatch, ofm, backend_id: str) -> None:
    """What the backend does at start: bind from the spawn env Electron passed. Here the owner had not
    enabled owner grants yet, so ``HERMES_OWNER_GRANT_KEYS`` was the empty string (main.ts)."""
    verifier = ofm.verifier_at_startup({"HERMES_OWNER_GRANT_BACKEND": backend_id, "HERMES_OWNER_GRANT_KEYS": ""})
    assert verifier is not None
    monkeypatch.setattr(ofm, "_verifier", verifier)


def test_forward_delivers_an_owner_turn_that_the_target_hook_verifies(live, monkeypatch):
    backend_id = "spawn_" + "1" * 32
    _bind_backend_at_spawn(monkeypatch, live.ofm, backend_id)
    live.installed["anchor"] = live.anchor  # the one-time enable, AFTER the backend started

    params = live.grant(backend_id)
    response = live.call(params)

    assert "error" not in response, response
    assert [r["status"] for r in response["result"]["results"]] == ["queued"]
    # Delivered as the target's own user turn (prompt.submit), stamped: an OWNER turn, not a peer message.
    assert len(live.submits) == 1
    submitted = live.submits[0]
    assert submitted["session_id"] == "rt-target"
    assert submitted["text"] == TEXT
    stamp = submitted["_owner_forward"]
    assert isinstance(stamp, live.ofm.OwnerForwardStamp)
    meta = stamp.display_metadata()
    assert meta["kind"] == "owner_forward"
    assert meta["from_session_id"] == ORIGIN
    assert meta["owner_grant"]["envelope"] == params["envelope"]

    # The target's hook (`hermes owner verify`) checks the envelope the turn carries against the anchor.
    result = verify_mod.verify_envelope(
        meta["owner_grant"]["envelope"], session=TARGET, uid=os.getuid(), now=int(time.time() * 1000),
        text_sha=hashlib.sha256(TEXT.encode()).hexdigest(), anchor=live.anchor)
    assert result.ok, result
    assert result.exit_code == 0


def test_key_enrolled_after_spawn_needs_no_backend_restart(live, monkeypatch, caplog):
    backend_id = "spawn_" + "2" * 32
    _bind_backend_at_spawn(monkeypatch, live.ofm, backend_id)
    caplog.set_level(logging.INFO, logger="tui_gateway.owner_forward")

    # Before the enable: refused (4126), and the refusal is logged with its code.
    before = live.call(live.grant(backend_id))
    assert before["error"]["code"] == live.ofm.ERR_DISABLED
    assert live.submits == []

    # The owner enables owner grants while this backend keeps running: the next forward verifies.
    live.installed["anchor"] = live.anchor
    after = live.call(live.grant(backend_id))
    assert "error" not in after, after
    assert len(live.submits) == 1


def test_every_refusal_is_logged_with_a_code_and_never_the_text(live, monkeypatch, caplog):
    backend_id = "spawn_" + "3" * 32
    _bind_backend_at_spawn(monkeypatch, live.ofm, backend_id)
    live.installed["anchor"] = live.anchor
    caplog.set_level(logging.INFO, logger="tui_gateway.owner_forward")

    # A grant bound to another (e.g. a respawned) backend, then a replayed nonce.
    wrong_backend = live.call(live.grant("spawn_" + "9" * 32))
    params = live.grant(backend_id)
    first = live.call(params)
    replay = live.call(params)

    assert wrong_backend["error"]["code"] == live.ofm.ERR_GRANT
    assert "error" not in first
    assert replay["error"]["code"] == live.ofm.ERR_GRANT
    lines = [r.getMessage() for r in caplog.records if r.name == "tui_gateway.owner_forward"]
    refused = [line for line in lines if line.startswith("owner.forward refused:")]
    assert len(refused) == 2, lines
    assert f"code={live.ofm.ERR_GRANT}" in refused[0]
    assert "reason=nonce_used" in refused[1]
    assert any(line.startswith("owner.forward delivered: queued") for line in lines), lines
    assert not any(TEXT[:20] in line for line in lines)


def test_no_binding_refusal_is_logged(live, monkeypatch, caplog):
    monkeypatch.setattr(live.ofm, "_verifier", None)
    caplog.set_level(logging.WARNING, logger="tui_gateway.owner_forward")
    response = live.call(live.grant("spawn_" + "4" * 32))
    assert response["error"]["code"] == live.ofm.ERR_DISABLED
    assert any("owner.forward refused: code=4126 reason=no_binding" in r.getMessage() for r in caplog.records)
