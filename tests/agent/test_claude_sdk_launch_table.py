"""Tests for in-memory launch table + runtime lineage across /compress (#49 / b10 H2)."""

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from agent.claude_sdk_launch_table import (
    _reset_table_for_tests,
    get_last_consumer_seen,
    has_recent_consumer,
    record_consumer_seen,
    record_launch,
    retire,
    snapshot,
    update_lineage,
    wait_for_change,
)
from agent import conversation_compression as cc
from hermes_state import SessionDB
from tests.agent.claude_sdk_fakes import ResultMessage, _make_session


@pytest.fixture(autouse=True)
def _clean_launch_table():
    _reset_table_for_tests()
    yield
    _reset_table_for_tests()


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _make_dummy_agent(db, session_id):
    return types.SimpleNamespace(
        _session_db=db,
        session_id=session_id,
        platform="cli",
        model="test-model",
        _session_init_model_config=None,
        working_directory=None,
        _memory_manager=None,
        context_compressor=types.SimpleNamespace(),
        _flush_messages_to_session_db=lambda *a, **k: None,
        _persist_user_message_idx=None,
        _session_messages=None,
        _gateway_session_key=None,
        _cached_system_prompt="sys",
    )


class TestClaudeSdkLaunchTable:
    def test_record_and_snapshot_monotonic_seq(self):
        cur_seq, entries = snapshot(0)
        assert cur_seq == 0
        assert entries == []

        e1 = record_launch(
            hermes_session_id="h-1",
            claude_session_id="c-1",
            profile="default",
            lineage=["root"],
        )
        assert e1["launch_seq"] == 1
        assert e1["hermes_session_id"] == "h-1"
        assert e1["claude_session_id"] == "c-1"
        assert e1["profile"] == "default"
        assert e1["lineage"] == ["root"]

        seq1, snap1 = snapshot(0)
        assert seq1 == 1
        assert len(snap1) == 1
        assert snap1[0]["claude_session_id"] == "c-1"

        e2 = record_launch(
            hermes_session_id="h-2",
            claude_session_id="c-2",
            profile="custom",
        )
        assert e2["launch_seq"] == 2

        seq2, snap_all = snapshot(0)
        assert seq2 == 2
        assert len(snap_all) == 2
        assert [e["claude_session_id"] for e in snap_all] == ["c-1", "c-2"]

        seq_since, snap_since = snapshot(1)
        assert seq_since == 2
        assert len(snap_since) == 1
        assert snap_since[0]["claude_session_id"] == "c-2"

        seq_none, snap_none = snapshot(2)
        assert seq_none == 2
        assert snap_none == []

    def test_wait_for_change_timeout_and_wake(self):
        # Times out without change
        t0 = time.monotonic()
        seq = wait_for_change(since_seq=0, timeout=0.05)
        elapsed = time.monotonic() - t0
        assert seq == 0
        assert elapsed >= 0.04

        # Wakes up when a launch is recorded
        def bg_record():
            time.sleep(0.03)
            record_launch(
                hermes_session_id="h-wake",
                claude_session_id="c-wake",
            )

        thread = threading.Thread(target=bg_record)
        t1 = time.monotonic()
        thread.start()
        seq_woken = wait_for_change(since_seq=0, timeout=1.0)
        thread.join()
        elapsed_wake = time.monotonic() - t1
        assert seq_woken == 1
        assert elapsed_wake < 0.5

        # Immediate return when seq is already newer than since_seq
        seq_imm = wait_for_change(since_seq=0, timeout=1.0)
        assert seq_imm == 1

    def test_retire_removes(self):
        record_launch(hermes_session_id="h-1", claude_session_id="c-1")
        record_launch(hermes_session_id="h-2", claude_session_id="c-2")

        retired = retire("c-1")
        assert retired is not None
        assert retired["claude_session_id"] == "c-1"

        _, snap = snapshot(0)
        assert len(snap) == 1
        assert snap[0]["claude_session_id"] == "c-2"

        # Retiring an already retired or nonexistent entry returns None
        assert retire("c-1") is None
        assert retire("nonexistent") is None

    def test_lineage_capped_at_16_oldest_to_newest(self):
        long_lineage = [f"sid-{i}" for i in range(25)]
        entry = record_launch(
            hermes_session_id="h-long",
            claude_session_id="c-long",
            lineage=long_lineage,
        )
        expected = [f"sid-{i}" for i in range(9, 25)]
        assert len(entry["lineage"]) == 16
        assert entry["lineage"] == expected
        assert entry["hermes_lineage"] == expected

        updated = update_lineage(
            "c-long",
            hermes_session_id="h-long-2",
            lineage=[f"new-{i}" for i in range(30)],
        )
        assert updated is not None
        assert len(updated["lineage"]) == 16
        assert updated["lineage"] == [f"new-{i}" for i in range(14, 30)]
        assert updated["hermes_lineage"] == [f"new-{i}" for i in range(14, 30)]


class TestLineageAcrossCompression:
    def test_compress_rotation_appends_parent_and_updates_live_entry(self, db):
        parent_id = "root-hermes-session"
        db.create_session(parent_id, source="cli")
        db.append_message(parent_id, "user", "turn 1")
        db.append_message(parent_id, "assistant", "resp 1")

        agent = _make_dummy_agent(db, parent_id)
        planned_sid = "planned-claude-uuid-1"
        live_session = types.SimpleNamespace(
            planned_cli_session_id=lambda: planned_sid,
            _planned_session_id=planned_sid,
            _hermes_session_id=parent_id,
        )
        agent._claude_sdk_session = live_session

        entry = record_launch(
            hermes_session_id=parent_id,
            claude_session_id=planned_sid,
            profile="default",
        )
        assert entry["hermes_session_id"] == parent_id
        assert entry["lineage"] == []

        # First compression rotation
        cc._publish_rotated_compaction(
            agent,
            [{"role": "user", "content": "turn 1"}, {"role": "assistant", "content": "resp 1"}],
            [{"role": "user", "content": "[handoff 1]"}],
            new_system_prompt="sys",
            lease=types.SimpleNamespace(holder=None, ttl=60.0, watermark=None),
            old_session_id=parent_id,
            compressed_user_turn_outcome="none",
        )

        child1_id = agent.session_id
        assert child1_id != parent_id
        assert agent._claude_sdk_hermes_lineage == [parent_id]
        assert live_session._hermes_session_id == child1_id

        _, snap1 = snapshot(0)
        table_entry1 = next(e for e in snap1 if e["claude_session_id"] == planned_sid)
        assert table_entry1["hermes_session_id"] == child1_id
        assert table_entry1["lineage"] == [parent_id]

        # Second compression rotation
        db.append_message(child1_id, "user", "turn 2")
        db.append_message(child1_id, "assistant", "resp 2")
        cc._publish_rotated_compaction(
            agent,
            [{"role": "user", "content": "turn 2"}, {"role": "assistant", "content": "resp 2"}],
            [{"role": "user", "content": "[handoff 2]"}],
            new_system_prompt="sys",
            lease=types.SimpleNamespace(holder=None, ttl=60.0, watermark=None),
            old_session_id=child1_id,
            compressed_user_turn_outcome="none",
        )

        child2_id = agent.session_id
        assert child2_id != child1_id
        assert agent._claude_sdk_hermes_lineage == [parent_id, child1_id]
        assert live_session._hermes_session_id == child2_id

        _, snap2 = snapshot(0)
        table_entry2 = next(e for e in snap2 if e["claude_session_id"] == planned_sid)
        assert table_entry2["hermes_session_id"] == child2_id
        assert table_entry2["lineage"] == [parent_id, child1_id]

    def test_forged_state_db_child_does_not_extend_lineage(self, db):
        legit_sid = "legit-root-session"
        db.create_session(legit_sid, source="cli")
        db.append_message(legit_sid, "user", "legit")

        agent = _make_dummy_agent(db, legit_sid)
        agent._claude_sdk_hermes_lineage = ["prior-ancestor"]

        # Attacker injects a forged continuation row in state.db
        forged_child = "forged-attacker-child"
        db.create_session(forged_child, source="cli", parent_session_id=legit_sid)
        db.append_message(forged_child, "user", "forged handoff")

        # Simulate _adopt_live_compression_child resolving the tip from state.db
        cc._adopt_live_compression_child(agent, db, legit_sid)

        # Lineage remains strictly the backend's in-memory record
        assert agent._claude_sdk_hermes_lineage == ["prior-ancestor"]

        # record_compression_continuation also does not extend lineage
        from tui_gateway.session_task_handoff import record_compression_continuation

        record_compression_continuation(legit_sid, forged_child, None)
        assert agent._claude_sdk_hermes_lineage == ["prior-ancestor"]


class TestSessionLifecycleLaunchTable:
    def test_session_lifecycle_records_and_retires(self):
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-lifecycle",
            hermes_lineage=["ancestor-0"],
        )
        planned_id = session.planned_cli_session_id()
        try:
            assert snapshot(0)[1] == []
            session.ensure_started()
            _, snap = snapshot(0)
            entry = next(e for e in snap if e["claude_session_id"] == planned_id)
            assert entry["hermes_session_id"] == "h-lifecycle"
            assert entry["lineage"] == ["ancestor-0"]
            assert entry["profile"] == "default"
        finally:
            session.close()

        # Retired after close
        _, snap_closed = snapshot(0)
        assert not any(e["claude_session_id"] == planned_id for e in snap_closed)


class TestAttestationBarrier:
    def test_consumer_seen_setter_and_getter(self):
        assert get_last_consumer_seen() == 0.0
        assert not has_recent_consumer(60.0)

        record_consumer_seen(100.0)
        assert get_last_consumer_seen() == 100.0

        # Reset clears it
        _reset_table_for_tests()
        assert get_last_consumer_seen() == 0.0

    def _setup_clock(self, monkeypatch):
        cur_time = [1000.0]
        sleep_calls = []
        caller_tid = threading.get_ident()
        orig_monotonic = time.monotonic
        orig_sleep = time.sleep

        def fake_monotonic():
            if threading.get_ident() == caller_tid:
                return cur_time[0]
            return orig_monotonic()

        def fake_sleep(secs):
            if threading.get_ident() == caller_tid:
                sleep_calls.append(secs)
                cur_time[0] += secs
            else:
                orig_sleep(secs)

        monkeypatch.setattr(time, "monotonic", fake_monotonic)
        monkeypatch.setattr(time, "sleep", fake_sleep)
        return cur_time, sleep_calls

    def test_barrier_no_consumer_no_wait(self, monkeypatch):
        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-no-consumer",
        )
        try:
            start = cur_time[0]
            session.ensure_started()
            elapsed = cur_time[0] - start
            assert elapsed < 0.1
            assert len(sleep_calls) == 0
        finally:
            session.close()

    def test_barrier_consumer_and_attest_dir_appears_proceeds_early(self, monkeypatch, tmp_path):
        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        record_consumer_seen(cur_time[0])
        grants_dir = tmp_path / "grants"
        grants_dir.mkdir()
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-attest-present",
        )
        planned_sid = session.planned_cli_session_id()
        attest_dir = grants_dir / "session-attest" / planned_sid
        attest_dir.mkdir(parents=True)
        (attest_dir / "1759140000000-test.json").write_text('{"v":1}')

        import hermes_owner_grant.anchor as anchor_mod

        monkeypatch.setattr(
            anchor_mod,
            "load_trusted_anchor",
            lambda *a, **k: types.SimpleNamespace(grants_dir=str(grants_dir)),
        )

        try:
            start = cur_time[0]
            session.ensure_started()
            elapsed = cur_time[0] - start
            assert elapsed < 0.1
            assert len(sleep_calls) == 0
        finally:
            session.close()

    def test_barrier_consumer_and_never_appears_proceeds_after_barrier(self, monkeypatch, tmp_path):
        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        record_consumer_seen(cur_time[0])
        grants_dir = tmp_path / "grants"
        grants_dir.mkdir()
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-attest-never",
        )

        import hermes_owner_grant.anchor as anchor_mod

        monkeypatch.setattr(
            anchor_mod,
            "load_trusted_anchor",
            lambda *a, **k: types.SimpleNamespace(grants_dir=str(grants_dir)),
        )

        try:
            start = cur_time[0]
            session.ensure_started()
            elapsed = cur_time[0] - start
            assert 1.45 <= elapsed <= 1.55
            assert len(sleep_calls) >= 25
        finally:
            session.close()

    def test_barrier_anchor_load_failure_no_wait(self, monkeypatch):
        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        record_consumer_seen(cur_time[0])
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-anchor-fail",
        )

        import hermes_owner_grant.anchor as anchor_mod

        def _raise(*a, **k):
            raise RuntimeError("anchor load failure")

        monkeypatch.setattr(anchor_mod, "load_trusted_anchor", _raise)

        try:
            start = cur_time[0]
            session.ensure_started()
            elapsed = cur_time[0] - start
            assert elapsed < 0.1
            assert len(sleep_calls) == 0
        finally:
            session.close()


# --- b10 review fixes: resume provenance (codex #1) and a barrier that can't hang (grok #4) ---

_UUID_A = "5b0d8a3e-0c7e-4c2e-9d5f-3f1f7d9a0b11"


class _Signer:
    """A real Ed25519 owner key plus an anchor pinning a tmp grants dir (tests only)."""

    def __init__(self, grants_dir, status="active"):
        import os as _os

        ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        from cryptography.hazmat.primitives import serialization
        from hermes_owner_grant import anchor as anchor_mod, envelope as env_mod

        self.key = ed.Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.kid = env_mod.kid_for_pub(pub)
        self.grants_dir = grants_dir
        self.anchor = anchor_mod.Anchor(
            owner_uid=_os.getuid(),
            grants_dir=str(grants_dir),
            keys=(anchor_mod.AnchorKey(
                kid=self.kid, alg="Ed25519", pub=pub, status=status, not_before=0,
                retired_at=(1 if status == "retired" else None),
            ),),
            verifier_sha256=None,
            sha256="0" * 64,
        )

    def write(self, claude_sid, hermes_sid, *, issued_at=None, lineage=None, bad_sig=False):
        import os as _os
        from hermes_owner_grant import attest as attest_mod, envelope as env_mod

        issued = int(time.time() * 1000) - 3 * 3600 * 1000 if issued_at is None else issued_at
        payload = {
            "v": 1, "aud": [attest_mod.AUDIENCE], "owner_uid": _os.getuid(),
            "profile": "default", "backend": "spawn-1",
            "hermes_session_id": hermes_sid, "claude_session_id": claude_sid,
            "launch_seq": 1, "project_root": "/abs/project", "repo_common_root": None,
            "binding_nonce": "nonce-1", "binding_seq": 1, "repo_remote": None,
            "issued_at": issued, "expires_at": issued + 15 * 60 * 1000,
            "hermes_lineage": list(lineage or []),
        }
        env = attest_mod.seal(env_mod.encode_payload(payload), self.kid, self.key.sign)
        if bad_sig:
            env = attest_mod.AttestationEnvelope(kid=env.kid, payload=env.payload, sig=bytes(64))
        d = self.grants_dir / "session-attest" / claude_sid
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%d-1.json" % issued)).write_text(env.to_json(), encoding="utf-8")


class TestLaunchSidOrigin:
    def test_record_launch_flags_origin_and_fails_closed_by_default(self):
        fresh = record_launch(hermes_session_id="h", claude_session_id="c-fresh", sid_origin="fresh")
        assert fresh["sid_origin"] == "fresh" and fresh["resumed_unverified"] is False
        resumed = record_launch(hermes_session_id="h", claude_session_id="c-res", sid_origin="resumed")
        assert resumed["sid_origin"] == "resumed" and resumed["resumed_unverified"] is True
        authed = record_launch(
            hermes_session_id="h", claude_session_id="c-auth", sid_origin="resumed",
            resume_authenticated=True,
        )
        assert authed["resumed_unverified"] is False
        legacy = record_launch(hermes_session_id="h", claude_session_id="c-legacy")
        assert legacy["sid_origin"] == "unknown" and legacy["resumed_unverified"] is True
        # /compress lineage updates keep the provenance flags.
        updated = update_lineage("c-res", hermes_session_id="h2", lineage=["h"])
        assert updated["resumed_unverified"] is True and updated["sid_origin"] == "resumed"


class TestAuthenticatedResume:
    def test_no_prior_attestation_is_unverified(self, tmp_path):
        from agent.claude_sdk_launch_table import authenticated_resume

        signer = _Signer(tmp_path / "grants")
        assert authenticated_resume(_UUID_A, "h-victim", [], anchor=signer.anchor) is False

    def test_signed_prior_attestation_for_same_session_or_ancestor_authenticates(self, tmp_path):
        from agent.claude_sdk_launch_table import authenticated_resume

        signer = _Signer(tmp_path / "grants")
        signer.write(_UUID_A, "h-parent")  # issued 3 h ago: long expired, still provenance
        assert authenticated_resume(_UUID_A, "h-parent", [], anchor=signer.anchor) is True
        assert authenticated_resume(_UUID_A, "h-child", ["h-parent"], anchor=signer.anchor) is True
        # The same signed sid does not authenticate for a different Hermes session.
        assert authenticated_resume(_UUID_A, "h-attacker", ["h-other"], anchor=signer.anchor) is False

    def test_forged_or_retired_attestation_is_unverified(self, tmp_path):
        from agent.claude_sdk_launch_table import authenticated_resume

        signer = _Signer(tmp_path / "grants")
        signer.write(_UUID_A, "h-victim", bad_sig=True)
        assert authenticated_resume(_UUID_A, "h-victim", [], anchor=signer.anchor) is False

        retired = _Signer(tmp_path / "grants2", status="retired")
        retired.write(_UUID_A, "h-victim")
        assert authenticated_resume(_UUID_A, "h-victim", [], anchor=retired.anchor) is False

    def test_unsafe_sid_is_unverified(self, tmp_path):
        from agent.claude_sdk_launch_table import authenticated_resume

        signer = _Signer(tmp_path / "grants")
        for sid in ("/etc/passwd", "..", "a/b", ""):
            assert authenticated_resume(sid, "h", [], anchor=signer.anchor) is False

    def test_resumed_session_launch_is_recorded_unverified_without_attestation(self, monkeypatch, tmp_path):
        import hermes_owner_grant.anchor as anchor_mod

        signer = _Signer(tmp_path / "grants")
        monkeypatch.setattr(anchor_mod, "load_trusted_anchor", lambda *a, **k: signer.anchor)
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-victim",
            resume_session_id=_UUID_A,
        )
        try:
            session.ensure_started()
            _, snap = snapshot(0)
            entry = next(e for e in snap if e["claude_session_id"] == _UUID_A)
            assert entry["sid_origin"] == "resumed"
            assert entry["resumed_unverified"] is True
        finally:
            session.close()

    def test_resumed_session_launch_is_authenticated_by_prior_attestation(self, monkeypatch, tmp_path):
        import hermes_owner_grant.anchor as anchor_mod

        signer = _Signer(tmp_path / "grants")
        signer.write(_UUID_A, "h-owner")
        monkeypatch.setattr(anchor_mod, "load_trusted_anchor", lambda *a, **k: signer.anchor)
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")],
            hermes_session_id="h-owner",
            resume_session_id=_UUID_A,
        )
        try:
            session.ensure_started()
            _, snap = snapshot(0)
            entry = next(e for e in snap if e["claude_session_id"] == _UUID_A)
            assert entry["sid_origin"] == "resumed"
            assert entry["resumed_unverified"] is False
        finally:
            session.close()

    def test_fresh_session_launch_is_recorded_fresh(self):
        session, _holder = _make_session(
            script=[ResultMessage(result="ok")], hermes_session_id="h-fresh"
        )
        planned = session.planned_cli_session_id()
        try:
            session.ensure_started()
            _, snap = snapshot(0)
            entry = next(e for e in snap if e["claude_session_id"] == planned)
            assert entry["sid_origin"] == "fresh"
            assert entry["resumed_unverified"] is False
        finally:
            session.close()


class TestAttestationBarrierHardening:
    _setup_clock = TestAttestationBarrier._setup_clock

    def _anchor(self, monkeypatch, grants_dir):
        import hermes_owner_grant.anchor as anchor_mod

        monkeypatch.setattr(
            anchor_mod,
            "load_trusted_anchor",
            lambda *a, **k: types.SimpleNamespace(grants_dir=str(grants_dir)),
        )

    @pytest.mark.parametrize("bad_sid", ["/abs/elsewhere", "..", "a/b", "x\\y", "x\x00y"])
    def test_barrier_refuses_unsafe_planned_sid_without_probing(self, monkeypatch, tmp_path, bad_sid):
        from agent import claude_sdk_launch_table as lt

        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        record_consumer_seen(cur_time[0])
        grants_dir = tmp_path / "grants"
        (grants_dir / "session-attest").mkdir(parents=True)
        self._anchor(monkeypatch, grants_dir)
        probes = []
        monkeypatch.setattr(lt, "attestation_files_present", lambda *a: probes.append(a) or True)
        session, _holder = _make_session(script=[ResultMessage(result="ok")], hermes_session_id="h-bad")
        session._planned_session_id = bad_sid
        try:
            start = cur_time[0]
            session._wait_for_attestation_barrier()
            assert cur_time[0] - start < 0.1
            assert sleep_calls == []
            assert probes == []
        finally:
            session.close()

    @pytest.mark.parametrize("level", ["sid", "parent"])
    def test_barrier_does_not_follow_a_symlinked_attest_dir(self, monkeypatch, tmp_path, level):
        import os as _os

        cur_time, sleep_calls = self._setup_clock(monkeypatch)
        record_consumer_seen(cur_time[0])
        grants_dir = tmp_path / "grants"
        grants_dir.mkdir()
        self._anchor(monkeypatch, grants_dir)
        session, _holder = _make_session(script=[ResultMessage(result="ok")], hermes_session_id="h-link")
        planned = session.planned_cli_session_id()
        outside = tmp_path / "outside"
        if level == "sid":
            (outside).mkdir()
            (outside / "1-x.json").write_text("{}")
            (grants_dir / "session-attest").mkdir()
            _os.symlink(str(outside), str(grants_dir / "session-attest" / planned))
        else:
            (outside / planned).mkdir(parents=True)
            (outside / planned / "1-x.json").write_text("{}")
            _os.symlink(str(outside), str(grants_dir / "session-attest"))
        try:
            start = cur_time[0]
            session._wait_for_attestation_barrier()
            # Refused as absent: the barrier runs to its deadline instead of proceeding early.
            assert 1.45 <= cur_time[0] - start <= 1.55
        finally:
            session.close()

    def test_barrier_returns_by_the_deadline_when_the_probe_blocks(self, monkeypatch, tmp_path):
        import os as _os

        record_consumer_seen()
        grants_dir = tmp_path / "grants"
        self._anchor(monkeypatch, grants_dir)
        session, _holder = _make_session(script=[ResultMessage(result="ok")], hermes_session_id="h-hang")
        (grants_dir / "session-attest" / session.planned_cli_session_id()).mkdir(parents=True)
        release = threading.Event()
        entered = threading.Event()
        real_listdir = _os.listdir

        def blocking_listdir(path="."):
            if isinstance(path, int):  # the barrier probe lists its O_NOFOLLOW dir fd
                entered.set()
                release.wait(30)
            return real_listdir(path)

        monkeypatch.setattr(_os, "listdir", blocking_listdir)
        try:
            start = time.monotonic()
            session._wait_for_attestation_barrier()
            elapsed = time.monotonic() - start
            assert entered.is_set(), "the probe must have reached the blocking listdir"
            assert 1.4 <= elapsed <= 1.65
        finally:
            release.set()
            session.close()
