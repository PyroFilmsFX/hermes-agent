"""HE-SECRET-HYGIENE S2: desktop/TUI ingest edges (E2 prompt.submit, E3 file.attach, @file adopt).

The submit row, the @file-expanded row, ``title_preview`` and staged text attachments are
masked before their first write; a binary attachment is byte-identical. The per-message
opt-out needs a single-use nonce minted by ``secrets.mask`` for that exact text; without one
the message stays masked. Fake secrets only; the root conftest pins the tag key.
"""

from __future__ import annotations

import base64
import json
import random
import re
import stat
import string

from agent.secret_hygiene import mask_ingress_text
from hermes_state import SessionDB
from tests.tui_gateway.test_submit_time_user_row import _desktop_session, _flush_agent
from tui_gateway import server

_ALNUM = string.ascii_letters + string.digits


def fake(n: int, seed: int) -> str:
    rng = random.Random(seed)
    while True:
        value = "".join(rng.choice(_ALNUM) for _ in range(n))
        if re.search("[a-z]", value) and re.search("[A-Z]", value) and re.search("[0-9]", value):
            return value


def _gh(seed: int) -> str:
    return "ghp_" + fake(36, seed)


def _db_url(pw: str) -> str:
    return f"postgresql://neondb_owner:{pw}@ep-fake-9.us-east-2.aws.neon.tech/neondb"


class _InlineThread:
    def __init__(self, target, **_kw):
        self.target = target

    def start(self):
        self.target()


def _submit_harness(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    runs: list[tuple] = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: False)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a: runs.append(a))
    return db, sid, key, session, runs


def _submit(sid, text, **extra):
    return server.handle_request({"id": "p", "method": "prompt.submit",
                                  "params": {"session_id": sid, "text": text, **extra}})


def test_submit_row_is_masked_before_first_write(monkeypatch, tmp_path):
    db, sid, key, session, runs = _submit_harness(monkeypatch, tmp_path)
    token = _gh(1)
    try:
        assert "result" in _submit(sid, f"push with {token} please")
        rows = db.get_messages_as_conversation(key)
        assert [r["role"] for r in rows] == ["user"]
        assert token not in rows[0]["content"]
        assert "[REDACTED:github-token:" in rows[0]["content"]
        # The turn thread receives the same masked text (arg 3 = text).
        assert runs and token not in runs[0][3] and runs[0][3] == rows[0]["content"]
        assert token not in json.dumps(session.get("inflight_turn") or {}, default=str)
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_title_preview_is_masked(monkeypatch, tmp_path):
    db, sid, _key, _session, runs = _submit_harness(monkeypatch, tmp_path)
    pw = fake(22, 2)
    try:
        assert "result" in _submit(sid, "see pasted file", title_preview=f"url {_db_url(pw)}")
        display_metadata = runs[0][5]
        assert pw not in display_metadata["title_preview"]
        assert "[REDACTED:neon-url:" in display_metadata["title_preview"]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_expanded_row_after_file_adopt_is_masked(monkeypatch, tmp_path):
    """The @file adopt UPDATEs the submit row to the expanded prompt; the expansion carries the
    attachment's contents, so the updated row must be masked too."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    pw = fake(24, 3)
    try:
        submit = "look at @file:notes.md"
        with session["history_lock"]:
            session["running"] = True
            server._start_inflight_turn(session, submit)
        assert server._persist_session_row_for_submit("rid", session, submit, None) is None
        expanded = f"{submit}\n\n--- notes.md ---\nDATABASE_URL={_db_url(pw)}\n"
        agent = _flush_agent(db, key)
        server._adopt_submit_user_row(session, agent, expanded, submit)
        rows = db.get_messages_as_conversation(key)
        assert len(rows) == 1 and pw not in rows[0]["content"]
        assert "[REDACTED:neon-url:" in rows[0]["content"]
        staged = agent._pending_cli_user_message
        assert pw not in staged["content"]
        assert staged["content"].strip() == rows[0]["content"].strip()  # the row store trims whitespace
        # The backstop re-masks the raw expanded turn input to the same bytes: the handoff holds.
        assert mask_ingress_text(expanded)[0] == staged["content"]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_turn_prep_masks_the_expanded_prompt(monkeypatch, tmp_path):
    from tui_gateway import prompt_turn

    pw = fake(24, 4)
    (tmp_path / "notes.md").write_text(f"DATABASE_URL={_db_url(pw)}\n")
    agent = type("A", (), {"model": "m", "base_url": "", "api_key": "", "provider": "",
                           "_config_context_length": 100_000})()
    session = {"agent": agent, "session_key": "k", "history": [], "history_lock": __import__("threading").Lock()}
    st = prompt_turn._TurnRun(agent, None, None, receipt_committed=True)
    for name in ("_wire_callbacks", "_register_session_cwd", "_apply_pending_model_switch",
                 "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_sync_agent_fallback_with_config", "_sync_bot_capabilities", "_adopt_out_of_band_turns"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda *_a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: str(tmp_path))
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_start_turn_voice", lambda: (None, False))
    prepared = server._prepare_turn_input("sid", session, st, "summarize @file:notes.md", [])
    assert prepared is not None
    prompt, run_message, _cols, _streamer = prepared
    assert pw not in prompt and pw not in str(run_message)
    assert "[REDACTED:neon-url:" in prompt


def test_turn_prep_hands_a_confirmed_optout_to_its_turn_only(monkeypatch, tmp_path):
    from tui_gateway import prompt_turn

    token = _gh(10)
    text = f"write {token} into .env"
    tags = frozenset(t["tag"] for t in __import__("agent.secret_hygiene", fromlist=["x"]).ingress_secret_tags(text))
    agent = type("A", (), {"model": "m", "base_url": "", "api_key": "", "provider": "",
                           "_config_context_length": 100_000})()
    session = {"agent": agent, "session_key": "k", "history": [], "history_lock": __import__("threading").Lock(),
               "_secret_optout": {"text": text, "tags": tags}}
    for name in ("_wire_callbacks", "_register_session_cwd", "_apply_pending_model_switch",
                 "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_sync_agent_fallback_with_config", "_sync_bot_capabilities", "_adopt_out_of_band_turns"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda *_a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: str(tmp_path))
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_start_turn_voice", lambda: (None, False))
    st = prompt_turn._TurnRun(agent, None, None, receipt_committed=True)
    prompt, *_ = server._prepare_turn_input("sid", session, st, text, [])
    assert token in prompt
    assert agent._secret_optout_tags == tags
    assert "_secret_optout" not in session
    # A later turn with the same text gets no opt-out.
    agent._secret_optout_tags = frozenset()
    st = prompt_turn._TurnRun(agent, None, None, receipt_committed=True)
    prompt, *_ = server._prepare_turn_input("sid", session, st, text, [])
    assert token not in prompt and not agent._secret_optout_tags


def _attach(sid, **params):
    return server.handle_request({"id": "a", "method": "file.attach",
                                  "params": {"session_id": sid, **params}})


def test_file_attach_text_is_masked_with_private_mode(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    session["profile_home"] = str(tmp_path / "home")
    session["agent_ready"] = session.get("agent_ready") or __import__("threading").Event()
    session["agent_ready"].set()
    pw = fake(22, 5)
    payload = f"# creds\nDATABASE_URL={_db_url(pw)}\n".encode()
    try:
        resp = _attach(sid, data_url="data:text/plain;base64," + base64.b64encode(payload).decode(),
                       name="pasted_content.txt")
        assert "result" in resp, resp
        stored = (tmp_path / "home" / "attachments" / "pasted_content.txt")
        assert stored.exists()
        body = stored.read_bytes()
        assert pw.encode() not in body and b"[REDACTED:neon-url:" in body
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_file_attach_binary_is_byte_identical(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    session["profile_home"] = str(tmp_path / "home")
    session["agent_ready"] = session.get("agent_ready") or __import__("threading").Event()
    session["agent_ready"].set()
    # A PDF-signature payload that happens to contain a secret-shaped string stays untouched.
    payload = b"%PDF-1.7\n" + f"ghp_{fake(36, 6)}".encode() + b"\n\xff\xfe\x00\x01"
    try:
        resp = _attach(sid, data_url="data:application/pdf;base64," + base64.b64encode(payload).decode(),
                       name="doc.pdf")
        assert "result" in resp, resp
        stored = tmp_path / "home" / "attachments" / "doc.pdf"
        assert stored.read_bytes() == payload
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_optout_without_valid_nonce_stays_masked(monkeypatch, tmp_path):
    db, sid, key, session, runs = _submit_harness(monkeypatch, tmp_path)
    token = _gh(7)
    text = f"write {token} into .env"
    try:
        tags = [t["tag"] for t in server.handle_request({"id": "m", "method": "secrets.mask", "params": {
            "session_id": sid, "text": text}})["result"]["tags"]]
        assert tags
        for optout in ({"tags": tags}, {"tags": tags, "confirm_nonce": "forged"}):
            session["running"] = False
            assert "result" in _submit(sid, text, secret_optout=optout)
        rows = db.get_messages_as_conversation(key)
        assert len(rows) == 2 and all(token not in r["content"] for r in rows)
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_optout_with_nonce_is_single_use_and_bound_to_text(monkeypatch, tmp_path):
    db, sid, key, session, runs = _submit_harness(monkeypatch, tmp_path)
    token = _gh(8)
    text = f"write {token} into .env"
    try:
        minted = server.handle_request({"id": "m", "method": "secrets.mask", "params": {
            "session_id": sid, "text": text, "optout_tags": None}})["result"]
        assert token not in json.dumps(minted)
        tags = [t["tag"] for t in minted["tags"]]

        def _mint():
            confirmed = server.handle_request({"id": "m", "method": "secrets.mask", "params": {
                "session_id": sid, "text": text, "optout_tags": tags}})["result"]
            assert confirmed["confirm_nonce"]
            return confirmed["confirm_nonce"]

        # A different text cannot spend the nonce (and the attempt burns it).
        session["running"] = False
        stale = _mint()
        _submit(sid, text + " (edited)", secret_optout={"tags": tags, "confirm_nonce": stale})
        # The bound text spends a fresh nonce once; the turn is told which tags to leave raw.
        nonce = _mint()
        session["running"] = False
        _submit(sid, text, secret_optout={"tags": tags, "confirm_nonce": nonce})
        session["running"] = False
        _submit(sid, text, secret_optout={"tags": tags, "confirm_nonce": nonce})
        contents = [r["content"] for r in db.get_messages_as_conversation(key)]
        assert [token in c for c in contents] == [False, True, False]
        assert runs[1][5]["secret_optout"] == {"kinds": {"github-token": 1}}
        # Only the spending submit hands the tags to its turn.
        assert session.get("_secret_optout") is None
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_secrets_mask_rpc_never_returns_values(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key = _desktop_session(monkeypatch, db)
    pw = fake(22, 9)
    try:
        result = server.handle_request({"id": "m", "method": "secrets.mask", "params": {
            "session_id": sid, "text": f"x {_db_url(pw)}"}})["result"]
        assert pw not in json.dumps(result)
        assert result["kinds"] == {"neon-url": 1}
        assert "[REDACTED:neon-url:" in result["text"]
        assert result.get("confirm_nonce") in (None, "")
    finally:
        server._sessions.pop(sid, None)
        db.close()
