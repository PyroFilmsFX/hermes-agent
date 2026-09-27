"""#60 U17 threat suite, Python half: T-1..T-5 plus the T-6 and T-7 investigations (VERIFY addendum
§4, §8.2 "Threat suite").

Every test attacks the REAL code paths: the ``hermes_owner_grant`` CLI (``cli.main``, the same code the
root-owned launcher runs under ``/usr/bin/python3 -I -S``), the real anchor loader
(``anchor.load_trusted_anchor`` with its StrictModes chain), and the gateway's real ``owner.forward``
(``tui_gateway.owner_forward.forward_rpc`` through ``server.handle_request``, with the real
``EnvelopeGrantVerifier`` and its default anchor loader). The only fakes are OS boundaries:

* ``/Library`` ownership. ``anchor.OsFileSystem`` is replaced by :class:`FakeLibraryRoot`, which serves
  the hard-coded anchor path and its parent chain with a chosen ``st_uid``/mode. Nothing reads or writes
  the real ``/Library``.
* ``prompt.submit`` / ``session.resume`` in the gateway (recorded, never run a model).
* Keys: test-only Ed25519 keys from ``cryptography``; no Keychain, no ``~/.hermes``.

Each test names, in its docstring, the MUTATION (a deliberately weakened defence) it was shown to fail
against before being restored. ``scripts``-free: the mutation run is recorded in the U17 report.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ed = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    reason="test keys are generated with the cryptography package",
)
from cryptography.hazmat.primitives import serialization  # noqa: E402

import hermes_owner_grant  # noqa: E402
from hermes_owner_grant import anchor as anchor_mod  # noqa: E402
from hermes_owner_grant import cli as cli_mod  # noqa: E402
from hermes_owner_grant import envelope as env_mod  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

UID = os.getuid()
GATE = "conductor:gate:review-budget-enable"
TEXT = "Enable the review budget gate for this lane. Do not merge until CI is green."
MIN = 60_000
HOUR = 60 * MIN


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Key:
    """A test-only Ed25519 key (the owner's, or an attacker's)."""

    def __init__(self) -> None:
        self.priv = ed.Ed25519PrivateKey.generate()
        self.pub = self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = env_mod.kid_for_pub(self.pub)

    def anchor_json(self, grants_dir: Path, *, status: str = "active") -> bytes:
        return json.dumps({
            "format": "hermes-owner-anchor/v1", "owner_uid": UID, "grants_dir": str(grants_dir),
            "keys": [{"kid": self.kid, "alg": "Ed25519", "pub": _b64u(self.pub), "status": status,
                      "not_before": 0, "retired_at": None}]}).encode("utf-8")


# -- the OS boundary: who owns /Library/Application Support/Hermes/owner-grant ------------------


class FakeLibraryRoot:
    """Stands in for the real filesystem at the hard-coded anchor path ONLY. ``file_uid``/``file_mode``
    describe ``anchor.json``; ``dir_uid``/``dir_mode`` the directory that holds it (``/`` and the other
    parents stay root:wheel 0755, as on this Mac)."""

    def __init__(self, data: bytes, *, file_uid: int = 0, file_mode: int = 0o644, dir_uid: int = 0,
                 dir_mode: int = 0o755) -> None:
        self.data = data
        self.file_uid, self.file_mode, self.dir_uid, self.dir_mode = file_uid, file_mode, dir_uid, dir_mode
        self.reads = 0

    def _stat(self, path: str) -> SimpleNamespace:
        chain = anchor_mod._dir_chain(anchor_mod.ANCHOR_DIR)
        if path == anchor_mod.ANCHOR_PATH:
            return SimpleNamespace(st_mode=stat.S_IFREG | self.file_mode, st_uid=self.file_uid, st_dev=7,
                                   st_ino=4242, st_size=len(self.data))
        if path == anchor_mod.ANCHOR_DIR:
            return SimpleNamespace(st_mode=stat.S_IFDIR | self.dir_mode, st_uid=self.dir_uid, st_dev=7,
                                   st_ino=4241, st_size=0)
        if path in chain:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_dev=7,
                                   st_ino=4000 + chain.index(path), st_size=0)
        raise FileNotFoundError(path)

    def lstat(self, path: str):
        return self._stat(path)

    def listdir(self, path: str) -> list:
        return []

    def read_nofollow(self, path: str, limit: int):
        self.reads += 1
        return self._stat(path), self.data[:limit]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The owner's anchored key, a grants dir, and the /Library boundary (root-owned by default)."""
    owner = Key()
    grants = tmp_path / "owner-grants"
    grants.mkdir(mode=0o700)
    state = SimpleNamespace(owner=owner, grants=grants, root=FakeLibraryRoot(owner.anchor_json(grants)))
    monkeypatch.setattr(anchor_mod, "OsFileSystem", lambda: state.root)
    return state


def payload_for(targets, *, text: str = TEXT, backend: str = "spawn-live", scope=(GATE,), now=None, **extra):
    now = int(time.time() * 1000) if now is None else now
    qualified = [f"default:{t['session_id']}" for t in targets]
    body = {
        "v": 1, "aud": ["hermes-owner-forward", "hermes-owner-verify"], "decision_id": "od_" + "b" * 26,
        "issued_at": now, "deliver_by": now + MIN, "expires_at": now + 12 * HOUR, "owner_uid": UID,
        "backend": backend, "nonce": _b64u(os.urandom(16)), "gesture": "menu", "confirm": "native_dialog",
        "source_session": {"session_id": "mgr", "message_id": "m-1", "role": "user"},
        "targets": sorted(targets, key=lambda t: t["session_id"]), "forward_targets": sorted(qualified),
        "scope": sorted(scope), "single_use": [], "subject": {}, "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_len": len(text.encode("utf-8")),
    }
    body.update(extra)
    return body


def mint(world, body, *, signer: Key | None = None, kid: str | None = None, write: bool = True):
    """Seal ``body`` the way Electron main does (exact bytes + domain prefix) and, like main, write the
    grant file at sign time. ``signer``/``kid`` let a test forge with another key."""
    payload = env_mod.encode_payload(body)
    key = signer or world.owner
    env = env_mod.seal(payload, kid or key.kid, key.priv.sign)
    if write:
        (world.grants / env_mod.grant_filename(body["issued_at"], env.grant_id)).write_text(
            json.dumps(env.to_dict()), encoding="utf-8")
    return env


def run_cli(*argv: str, stdin: str = "") -> tuple[int, dict]:
    """The hook path: the same ``cli.main`` the root-owned launcher dispatches to, anchor loaded from
    the hard-coded path (no ``anchor=`` injection)."""
    out = io.StringIO()
    code = cli_mod.main(list(argv), uid=UID, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


# -- the gateway, with the real verifier and the real anchor loader ------------------------------


class _ClientTransport:
    session_spawn_capability = None

    def write(self, obj) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def gw(monkeypatch, tmp_path, world):
    import tui_gateway.server as server
    from tui_gateway import owner_forward as ofm
    from tui_gateway import session_mailbox as mb

    db = SessionDB(tmp_path / "state.db")
    for sid in ("mgr", "worker-a", "worker-b"):
        db.create_session(sid, "desktop")
    sessions: dict = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(mb, "policy", lambda: dict(mb._DEFAULTS, startup_delay_s=0, retry_interval_s=0))
    monkeypatch.setattr(ofm, "policy", lambda: dict(ofm._DEFAULTS))
    # The production verifier: no anchor_loader, so every call re-reads the hard-coded anchor path
    # through the (patched) /Library boundary.
    monkeypatch.setattr(ofm, "_verifier", ofm.EnvelopeGrantVerifier(backend_id="spawn-live"))
    ofm._reset_for_tests()
    submits: list = []
    resumes: list = []

    def submit(rid, params):
        submits.append(dict(params))
        return {"result": {"status": "streaming"}}

    def resume(rid, params):
        resumes.append(dict(params))
        sid = f"rt-{len(resumes)}"
        sessions[sid] = {"session_key": params["session_id"], "profile_home": None,
                         "transport": server._detached_ws_transport}
        return {"result": {"session_id": sid}}

    monkeypatch.setitem(server._methods, "prompt.submit", submit)
    monkeypatch.setitem(server._methods, "session.resume", resume)

    def forward(env, text, targets):
        from tui_gateway.transport import bind_transport, reset_transport

        token = bind_transport(_ClientTransport())
        try:
            return server.handle_request({"id": "t-1", "method": "owner.forward", "params": {
                "envelope": env.to_dict() if hasattr(env, "to_dict") else env, "text": text,
                "targets": list(targets)}})
        finally:
            reset_transport(token)

    yield SimpleNamespace(server=server, ofm=ofm, db=db, submits=submits, resumes=resumes, forward=forward)
    ofm._reset_for_tests()
    db.close()


def _err(resp) -> tuple[int | None, str]:
    error = resp.get("error") or {}
    return error.get("code"), str(error.get("message") or "")


# ── positive control ───────────────────────────────────────────────────────────────────────


def test_control_a_genuine_grant_verifies_on_the_hook_path_and_delivers(world, gw):
    """Without this control every refusal below could be a broken happy path."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))

    code, out = run_cli("verify", "--session", "worker-a", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["ok"], out["tier"]) == (0, True, "signed")

    resp = gw.forward(env, TEXT, ["default:worker-a"])
    assert resp["result"]["results"][0]["status"] == "resumed-and-delivered", resp
    assert len(gw.submits) == 1


# ── T-1: a real grant cited for a DIFFERENT session ───────────────────────────────────────


def test_t1_real_grant_cited_by_another_worker_is_refused_on_the_hook_path(world):
    """T-1 (addendum §4.1). Worker B can read worker A's grant file (same uid) and cite it by id, by
    scope, or by its exact text hash. The hook passes ``--session`` from B's own trusted context, so all
    three are refused; A still verifies.

    MUTATION: step 6 of ``verify._evaluate_signed`` weakened to skip the ``targets[].session_id`` match
    (``target = p["targets"][0]``). The ``--grant`` citation then verified for worker-b (exit 0) and
    this test failed. Restored."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))
    sha = hashlib.sha256(TEXT.encode()).hexdigest()

    code, out = run_cli("verify", "--session", "worker-b", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["ok"], out["reason"]) == (1, False, "session_mismatch")
    code, out = run_cli("verify", "--session", "worker-b", "--scope", GATE)
    assert (code, out["ok"]) == (4, False)
    code, out = run_cli("verify", "--session", "worker-b", "--text-sha", sha)
    assert (code, out["ok"]) == (4, False)
    assert run_cli("verify", "--session", "worker-a", "--grant", env.grant_id)[0] == 0


def test_t1_claude_session_binding_refuses_a_hook_in_another_cli(world):
    """T-1, second binding. A grant that names the target's Claude session id is refused to a hook whose
    stdin ``session_id`` is another CLI's, even when ``--session`` (env-derived) matches.

    MUTATION: the ``claude_session_id`` comparison in ``verify._evaluate_signed`` removed. The citation
    from ``claude-b`` then verified (exit 0) and this test failed. Restored."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": "claude-a"}]))

    code, out = run_cli("verify", "--session", "worker-a", "--claude-session", "claude-b", "--grant", env.grant_id)
    assert (code, out["reason"]) == (1, "claude_session_mismatch")
    assert run_cli("verify", "--session", "worker-a", "--claude-session", "claude-a", "--grant", env.grant_id)[0] == 0


def test_t1_gateway_never_delivers_a_grant_to_a_session_the_owner_did_not_confirm(world, gw):
    """T-1 at delivery. A (compromised) renderer holding a genuine envelope for worker-a asks
    ``owner.forward`` to deliver it to worker-b, alone or alongside worker-a. Refused before any submit.

    MUTATION: in ``owner_forward._resolve_targets`` the two target-binding checks (requested ==
    ``forward_targets`` and ``target_id in targets[].session_id``) disabled. The forward then reached
    ``prompt.submit`` for worker-b and this test failed. Restored."""
    for targets in (["default:worker-b"], ["default:worker-a", "default:worker-b"]):
        # A fresh genuine grant each time: the nonce is burned before the target check.
        env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))
        code, message = _err(gw.forward(env, TEXT, targets))
        assert code == gw.ofm.ERR_TARGET, message
    assert gw.submits == [] and gw.resumes == []


# ── T-2: replay into a new session id; delivery replay ───────────────────────────────────


def test_t2_replay_into_a_branch_or_new_session_id_is_refused(world, gw):
    """T-2 (addendum §4.3). A ``/branch`` fork (``_branched_from`` + ``parent_session_id`` in state.db)
    and a brand-new worker get new ids. The verifier never follows state.db lineage (it never opens
    sqlite: ``sqlite3.connect`` raises here), so the parent's grant fails step 6 for both.

    MUTATION: same step-6 weakening as T-1 (first target always matches). The fork then verified and this
    test failed. Restored."""
    gw.db.create_session("worker-a-fork", "desktop", parent_session_id="worker-a",
                         model_config={"_branched_from": "worker-a"})
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))

    def no_sqlite(*_a, **_k):
        raise AssertionError("the verifier opened sqlite")

    original = sqlite3.connect
    sqlite3.connect = no_sqlite
    try:
        for session in ("worker-a-fork", "worker-new"):
            code, out = run_cli("verify", "--session", session, "--grant", env.grant_id, "--scope", GATE)
            assert (code, out["reason"]) == (1, "session_mismatch")
            assert run_cli("verify", "--session", session, "--scope", GATE)[0] == 4
    finally:
        sqlite3.connect = original


def test_t2_a_delivered_grant_cannot_be_delivered_again(world, gw):
    """T-2 at delivery (D-9). The same envelope + text replayed to ``owner.forward`` is refused on the
    in-memory nonce, so the owner's one confirm yields exactly one delivered turn.

    MUTATION: ``owner_forward._NonceCache.claim`` made to always return True. The replay then reached
    ``prompt.submit`` a second time and this test failed. Restored."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))

    assert "result" in gw.forward(env, TEXT, ["default:worker-a"])
    code, message = _err(gw.forward(env, TEXT, ["default:worker-a"]))
    assert code == gw.ofm.ERR_GRANT and "nonce" in message
    assert len(gw.submits) == 1


def test_t2_a_captured_grant_is_dead_after_a_backend_restart(world, gw):
    """T-2 across restarts (§1.2). A long-lived key means a captured envelope still has a valid
    signature after the backend restarts; the per-spawn ``backend`` binding (new spawn id) refuses it.

    MUTATION: the ``claims.get("backend") != backend_id`` check in ``owner_forward.check_claims``
    removed. The old-spawn grant then delivered and this test failed. Restored."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}], backend="spawn-old"))

    code, message = _err(gw.forward(env, TEXT, ["default:worker-a"]))
    assert code == gw.ofm.ERR_GRANT and "backend" in message
    assert gw.submits == []


# ── T-3: a user-owned file at the anchor path ───────────────────────────────────────────


@pytest.mark.parametrize("where", ["file_user_owned", "file_group_writable", "dir_user_owned", "dir_world_writable"])
def test_t3_an_agent_planted_anchor_is_untrusted_on_the_hook_path_and_the_gateway(world, gw, where):
    """T-3 (§1.3, §4.5). A same-uid agent can't write the real root-owned anchor, so the attack is a
    file it CAN own at that path (or a parent it can write). The anchor pins the attacker's key and the
    attacker's grant is perfectly signed: the hook path exits 3 (``anchor_untrusted``) and the gateway's
    ``owner.forward`` is disabled (4126), nothing submitted.

    MUTATION: ``anchor._require_root_owned`` made a no-op. The attacker grant then verified (exit 0) and
    delivered, and this test failed for every variant. Restored."""
    attacker = Key()
    data = attacker.anchor_json(world.grants)
    world.root = {
        "file_user_owned": FakeLibraryRoot(data, file_uid=UID),
        "file_group_writable": FakeLibraryRoot(data, file_mode=0o664),
        "dir_user_owned": FakeLibraryRoot(data, dir_uid=UID),
        "dir_world_writable": FakeLibraryRoot(data, dir_mode=0o777),
    }[where]
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]), signer=attacker)

    code, out = run_cli("verify", "--session", "worker-a", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["ok"], out["reason"]) == (3, False, "anchor_untrusted")
    code, message = _err(gw.forward(env, TEXT, ["default:worker-a"]))
    assert code == gw.ofm.ERR_DISABLED and "anchor_untrusted" in message
    assert gw.submits == []


def test_t3_no_env_var_or_argument_moves_the_anchor(world, monkeypatch, tmp_path):
    """T-3, the redirect variant. An agent can't swap the anchor, so it points the verifier elsewhere:
    ``HERMES_*``/``PYTHON*`` env and an ``--anchor`` flag. The CLI has no such flag (usage error, exit 2)
    and reads only the hard-coded path, so the root-owned owner anchor still decides.

    MUTATION: ``anchor.load_trusted_anchor`` changed to honour ``HERMES_OWNER_GRANT_ANCHOR``. The
    hard-coded path was then no longer the one read (the verdict moved off ``unknown_kid`` under the
    owner's anchor) and this test failed. Restored."""
    attacker = Key()
    rogue = tmp_path / "rogue-anchor.json"
    rogue.write_bytes(attacker.anchor_json(world.grants))
    for name in ("HERMES_OWNER_GRANT_ANCHOR", "HERMES_ANCHOR", "HERMES_HOME", "PYTHONPATH"):
        monkeypatch.setenv(name, str(rogue))
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]), signer=attacker)

    code, out = run_cli("verify", "--session", "worker-a", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["reason"]) == (1, "unknown_kid")
    assert run_cli("verify", "--anchor", str(rogue), "--session", "worker-a", "--grant", env.grant_id)[0] == 2
    assert world.root.reads >= 1  # the hard-coded path was the one read


# ── T-4: a swapped private-key blob (Python half: what such a key's grants are worth) ────────


def test_t4_a_grant_signed_by_a_swapped_key_under_the_anchored_kid_never_verifies(world, gw):
    """T-4 (§4.5). If an agent swaps main's key blob (or unwraps its own key in dev, R6) and signs under
    the anchored ``kid``, the signature is over the wrong key: ``bad_signature`` on the hook path and
    ``signature`` at the gateway. (The Electron half, main refusing to load or sign with a swapped blob,
    is ``apps/desktop/electron/owner-grant-threats.test.ts``.)

    MUTATION: ``verify._signature_ok`` and the gateway's ``ed25519_pure.verify`` call made to return
    True. The swapped-key grant then verified and delivered, and this test failed. Restored."""
    thief = Key()
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]),
               signer=thief, kid=world.owner.kid)

    code, out = run_cli("verify", "--session", "worker-a", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["reason"]) == (1, "bad_signature")
    code, message = _err(gw.forward(env, TEXT, ["default:worker-a"]))
    assert code == gw.ofm.ERR_GRANT and "signature" in message
    assert gw.submits == []


# ── T-5: the local-capture detection contract ───────────────────────────────────────────


def test_t5_hosted_session_is_true_for_every_hermes_sdk_spawn(monkeypatch):
    """T-5 (§7 item 3, D-16). ``hermes_owner_grant.hosted_session(env)`` is the helper conductor copies to
    switch local owner-line capture OFF. It is fed the env a Hermes SDK CLI really gets: the SDK's own
    merge (``CLAUDE_CODE_ENTRYPOINT=sdk-py``, then ``options.env``) of Hermes' real
    ``_sdk_env_overrides``. An agent-writable config.yaml ``agent.claude_agent_sdk.env`` that sets
    ``CLAUDE_CODE_ENTRYPOINT=cli`` must not flip it: Hermes applies the session id after operator env.

    MUTATION: ``hosted_session`` reduced to ``env.get("CLAUDE_CODE_ENTRYPOINT") != "cli"`` (the
    entrypoint signal only). The config-override spawn then read as a plain terminal and this test
    failed. Restored."""
    from agent.transports import claude_agent_sdk_session_config as cfg

    monkeypatch.setattr(cfg, "_configured_sdk_env", lambda: {"CLAUDE_CODE_ENTRYPOINT": "cli"})
    overrides = cfg._sdk_env_overrides(metered_allowed=True, task_env={}, hermes_session_id="worker-a")
    spawned = {**{"PATH": "/usr/bin"}, "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **overrides}
    assert spawned["CLAUDE_CODE_ENTRYPOINT"] == "cli" and spawned["HERMES_SESSION_ID"] == "worker-a"
    assert hermes_owner_grant.hosted_session(spawned) is True

    assert hermes_owner_grant.hosted_session({"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}) is True
    assert hermes_owner_grant.hosted_session({}) is True  # unknown entrypoint fails safe
    assert hermes_owner_grant.hosted_session({"CLAUDE_CODE_ENTRYPOINT": "cli", "HERMES_SESSION_ID": " "}) is False
    assert hermes_owner_grant.hosted_session({"CLAUDE_CODE_ENTRYPOINT": "cli"}) is False


def test_t5_the_sdk_really_stamps_its_entrypoint():
    """T-5 grounding: the installed claude-agent-sdk sets ``CLAUDE_CODE_ENTRYPOINT=sdk-py`` before
    ``options.env`` (so only an explicit override changes it). A static read of the installed SDK, so a
    future SDK that drops it fails here instead of silently turning capture back on."""
    import inspect

    sdk = pytest.importorskip("claude_agent_sdk._internal.transport.subprocess_cli")
    source = inspect.getsource(sdk)
    assert '"CLAUDE_CODE_ENTRYPOINT": "sdk-py",\n                **self._options.env' in source


# ── T-6 (investigation): settings-env override of HERMES_SESSION_ID ────────────────────────


def test_t6_with_a_bound_claude_session_an_env_override_cannot_borrow_a_sibling_grant(world):
    """T-6 investigation, the closure that works. FINDING (docs, 2026-09-27,
    code.claude.com/docs/en/env-vars "Precedence"): "When the same variable is set in both your shell and
    a settings file env block, the settings file value applies. Claude Code writes each env entry into
    the process environment, replacing the value inherited from the shell", and a running session applies
    a saved change without a relaunch. Project-local ``.claude/settings.local.json`` (agent-writable) is
    such a file once the folder is trusted. So an agent in worker B CAN make its own hooks see
    ``HERMES_SESSION_ID=worker-a``. The addendum's closure is the Claude session id from hook stdin, which
    settings ``env`` can't set. When the grant binds it, B's hook (stdin ``claude-b``) is refused.

    MUTATION: the ``claude_session_id`` comparison in ``verify._evaluate_signed`` removed. The borrowed
    grant then verified and this test failed. Restored."""
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": "claude-a"}]))
    hook_env = {"HERMES_SESSION_ID": "worker-a"}  # overridden by B's settings.local.json
    hook_stdin = {"session_id": "claude-b"}  # B's real CLI session, from the CLI itself

    code, out = run_cli("verify", "--session", hook_env["HERMES_SESSION_ID"], "--claude-session",
                        hook_stdin["session_id"], "--scope", GATE)
    assert (code, out["ok"]) != (0, True)
    assert out["reason"] == "claude_session_mismatch"


@pytest.mark.xfail(strict=True, reason=(
    "P1 (U17 T-6): no backend exposes a Claude session id, so main signs claude_session_id=null for every "
    "target and the T-6 closure never engages; a settings.local.json HERMES_SESSION_ID override lets "
    "worker B's hooks accept worker A's grants"))
def test_t6_a_grant_main_signs_today_is_bound_to_the_target_cli(world, gw):
    """T-6 REAL HOLE (xfail strict; reported P1). Main fills ``targets[].claude_session_id`` from
    ``GET /api/sessions/{id}`` (``main.ts`` ``resolveSession`` reads ``row.claude_session_id``). That route
    returns ``db.get_session(sid)`` (``hermes_cli/web_routers/sessions.py`` ``get_session_detail``), and no
    column, route or writer anywhere in the backend produces ``claude_session_id`` (grep: only
    ``hermes_owner_grant/verify.py`` and tests mention it). So every grant is signed with
    ``claude_session_id: null``, and with the T-6 env override above, worker B's hook passes
    ``--session worker-a`` and accepts worker A's grant, for every scope class.

    Fix direction (not done here, per the no-silent-fix rule): the gateway records the live CLI's Claude
    session id on the session row (or answers it from memory for the detail route), main signs it, and
    for cold targets the hook contract keeps requiring ``--claude-session`` (a grant with ``null`` then
    binds only after first spawn). This test starts passing, and the strict xfail flips, once the row main
    reads carries the id."""
    row = gw.db.get_session("worker-a") or {}
    signed_as_main_does = row.get("claude_session_id") or None
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": signed_as_main_does}]))

    code, _out = run_cli("verify", "--session", "worker-a", "--claude-session", "claude-b",
                         "--grant", env.grant_id, "--scope", GATE)
    assert code != 0, "worker B's hook (HERMES_SESSION_ID overridden to worker-a) accepted worker A's grant"


# ── T-7 (investigation): the session id across compression and a dead-CLI rebuild ──────────


def test_t7_the_cli_session_id_is_fixed_at_spawn_and_follows_agent_session_id(monkeypatch):
    """T-7 investigation. FINDING (code read + this test, 2026-09-27): the CLI's ``HERMES_SESSION_ID`` is
    whatever ``agent.session_id`` was when ``ClaudeAgentSdkSession`` was CONSTRUCTED
    (``claude_sdk_runtime_session.py`` passes ``hermes_session_id=getattr(agent, "session_id")``;
    ``_sdk_env_overrides`` exports it). Hermes compression rotates ``agent.session_id`` to the child
    (``conversation_compression.py``: ``agent.session_id = new_session_id``; SDK lanes skip AUTO
    compression but manual ``/compress`` still runs), and continuity adoption does the same. A live CLI
    keeps the OLD id until it is rebuilt; any rebuild (dead CLI, model switch, tool-surface rotation,
    backend restart + resume onto the tip) exports the NEW id. So the id a hook sees is NOT stable across
    compression: it is the spawn-time id, which may be the parent or the tip.

    Pinned here: the exported id is exactly the construction argument (never a lineage lookup or the
    ambient context), and the runtime passes ``agent.session_id`` at construction.
    MUTATION: ``claude_sdk_runtime_session.py`` changed to ``hermes_session_id=None`` (the id falls back
    to the ambient session context instead of being pinned at spawn). This test then failed. Restored."""
    from agent.transports import claude_agent_sdk_session_config as cfg

    monkeypatch.setattr(cfg, "_configured_sdk_env", lambda: {})
    for spawn_id in ("worker-a", "worker-a-tip"):
        assert cfg._sdk_env_overrides(metered_allowed=True, task_env={}, hermes_session_id=spawn_id)[
            "HERMES_SESSION_ID"] == spawn_id
    runtime = (Path(cfg.__file__).resolve().parents[1] / "claude_sdk_runtime_session.py").read_text("utf-8")
    assert 'hermes_session_id=getattr(agent, "session_id", None)' in runtime


def test_t7_lineage_never_widens_a_grant_delivery_follows_the_tip_but_the_binding_stays_signed(world, gw):
    """T-7 DECISION (addendum §4.3 + D-10, recorded here): lineage from state.db is NEVER accepted as a
    session binding, because an agent can write state.db (R1). Concretely:

    * Delivery: ``owner.forward`` keeps delivering to the compression tip of a signed target (the text
      must land where the conversation lives), but the stamp and the stored ``owner_grant`` keep the
      SIGNED id and envelope; the gateway never re-labels a grant with the tip.
    * Hooks: a hook in the tip CLI (``HERMES_SESSION_ID`` = tip after a rebuild) gets ``session_mismatch``
      for a grant signed for the parent. The owner re-sends to the tip (a new confirm), which is what the
      addendum asks for: "record the id the live CLI runs under as the target at signing time".
    * Chip: main's ``ownerGrant.verify`` gets the viewed (tip) id and shows "unverified" for such a row
      (``owner-grant-threats.test.ts`` pins it). Lineage-aware matching in the gateway verifier or the chip
      is REFUSED until a signed lineage exists (D-10's reversal condition).

    Residual (not a hole, R1 class): because delivery follows state.db lineage, an agent that forges a
    compression child row for worker-a can redirect a forward's TEXT into its own session. The grant
    there never verifies (wrong session), the chip there is unverified, and forging state.db already
    lets it inject turns. Main signs the tip it lists (``SessionInfo.id`` is the live id), so a stale id
    only happens when compression lands between listing and delivery.

    MUTATION: in ``owner_forward._resolve_targets`` the stored id replaced by the tip
    (``resolved.append((..., tip))``, so the stamp claims the tip was confirmed). This test then failed.
    Restored. (The verifier half also fails under the T-1 step-6 mutation.)"""
    gw.db.end_session("worker-a", "compression")
    gw.db.create_session("worker-a-tip", "desktop", parent_session_id="worker-a")
    gw.db.append_message("worker-a-tip", "user", "continued after compression")
    assert gw.db.resolve_resume_session_id("worker-a") == "worker-a-tip"
    env = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))

    resp = gw.forward(env, TEXT, ["default:worker-a"])
    assert resp["result"]["results"][0]["target_session_id"] == "worker-a", resp
    assert gw.resumes[0]["session_id"] == "worker-a-tip"  # delivered where the conversation lives
    stamp = gw.submits[0]["_owner_forward"]
    assert stamp.target_session_id == "worker-a" and stamp.target_session_key == "worker-a-tip"
    assert stamp.display_metadata()["owner_grant"]["envelope"] == env.to_dict()

    code, out = run_cli("verify", "--session", "worker-a-tip", "--grant", env.grant_id, "--scope", GATE)
    assert (code, out["reason"]) == (1, "session_mismatch")
    # The tip session can't be named as a target of the parent's grant either (fresh grant, fresh nonce).
    again = mint(world, payload_for([{"session_id": "worker-a", "claude_session_id": None}]))
    assert _err(gw.forward(again, TEXT, ["default:worker-a-tip"]))[0] == gw.ofm.ERR_TARGET
    assert len(gw.submits) == 1
