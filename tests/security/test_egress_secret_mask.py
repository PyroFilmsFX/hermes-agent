"""HE-SECRET-HYGIENE S4: every egress surface masks secrets with the S1 detector.

Fake secrets only; the tag key is the fixed test key pinned by tests/conftest.py. Each
test names the surface it covers. A leak is checked by the secret's distinctive head and
tail fragments, because the legacy log redactor keeps up to 6 head + 4 tail characters.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tarfile
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "cntrl-plugins") not in sys.path:
    sys.path.insert(0, str(_REPO / "cntrl-plugins"))

# Built by concatenation so repo secret scanners do not flag the fixtures.
FLY = "FlyV1 " + "fm2_" + "FAKEflyMacaroonBody0123456789QRSTWXYZ"
PG_PW = "Zq8Test0nlyFake" + "Pw4XyQ"
PGPASSWORD = "PGPASSWORD=" + PG_PW
DB_PW = "npgFakeTest" + "Pw12345QZ"
DB_URL = "postgresql://neondb_owner:" + DB_PW + "@ep-fake-123.us-east-2.aws.neon.tech/neondb"
SK_ANT = "sk-ant-" + "api03-FAKEantKEY0123456789abcdefghijklmnopUVWX"
SECRET_TEXT = f"deploy with {FLY}\nexport {PGPASSWORD}\nDATABASE_URL={DB_URL}\nkey {SK_ANT}\n"

# (head, tail) fragments of each secret body: neither may survive in any egress copy.
_FRAGMENTS = (("FAKEfly", "WXYZ"), ("Zq8Tes", "4XyQ"), ("npgFake", "45QZ"), ("FAKEantKEY", "UVWX"))


def _assert_clean(text) -> None:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for head, tail in _FRAGMENTS:
        assert head not in text, f"secret head {head!r} leaked"
        assert tail not in text, f"secret tail {tail!r} leaked"


def _assert_masked(text) -> None:
    _assert_clean(text)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    assert "[REDACTED:fly-token:" in text


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# cntrl_sync
# ---------------------------------------------------------------------------

class _FakeCntrl:
    def __init__(self, tasks):
        self.tasks = {t["id"]: dict(t) for t in tasks}
        self.moves, self.comments = [], []

    def my_tasks(self, limit=200):
        return list(self.tasks.values())

    def get_task(self, task_id):
        return dict(self.tasks[task_id])

    def move(self, task_id, status, position):
        self.moves.append((task_id, status, position))
        self.tasks[task_id]["status"] = status

    def comment(self, task_id, text, field="content"):
        self.comments.append((task_id, text))


_CNTRL_TASK = {"id": "00000000-0000-0000-0000-000000000001", "title": "t", "description": "d", "status": "todo",
               "priority": "high", "labels": ["hermes"], "category": "engineering", "position": 3}


@pytest.fixture
def kanban_conn(tmp_path, hermes_home):
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(db_path=tmp_path / "kanban.db")
    yield conn
    conn.close()


def _synced_task(conn, client):
    from cntrl_sync import core

    state = core.FileState(str(conn.execute("PRAGMA database_list").fetchone()[2]) + ".state.json")
    core.sync_down(client, conn, cfg=dict(core.DEFAULTS, landing="ready"), state=state)
    return conn.execute("SELECT id FROM tasks").fetchone()["id"]


def test_cntrl_flow_up_done_comment_is_masked(kanban_conn):
    from cntrl_sync import core
    from hermes_cli import kanban_db as kdb

    client = _FakeCntrl([_CNTRL_TASK])
    tid = _synced_task(kanban_conn, client)
    assert kdb.complete_task(kanban_conn, tid, result=SECRET_TEXT, fire_lifecycle_hook=False)
    core.flow_up(client, kanban_conn, tid)
    assert client.comments
    _assert_masked(client.comments[0][1])
    assert "Hermes completed this task." in client.comments[0][1]


def test_cntrl_flow_up_blocked_reason_is_masked(kanban_conn):
    from cntrl_sync import core
    from hermes_cli import kanban_db as kdb

    client = _FakeCntrl([_CNTRL_TASK])
    tid = _synced_task(kanban_conn, client)
    kanban_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    kanban_conn.commit()
    assert kdb.get_task(kanban_conn, tid).status == "blocked"
    core.flow_up(client, kanban_conn, tid, reason=SECRET_TEXT)
    _assert_masked(client.comments[0][1])


def test_cntrl_flow_up_fails_closed_when_masking_fails(kanban_conn, monkeypatch):
    from agent import secret_hygiene
    from agent.secret_egress import EgressMaskError
    from cntrl_sync import core
    from hermes_cli import kanban_db as kdb

    client = _FakeCntrl([_CNTRL_TASK])
    tid = _synced_task(kanban_conn, client)
    assert kdb.complete_task(kanban_conn, tid, result=SECRET_TEXT, fire_lifecycle_hook=False)

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(secret_hygiene, "mask_secrets_for_ingest", _boom)
    with pytest.raises(EgressMaskError):
        core.flow_up(client, kanban_conn, tid)
    assert client.comments == [] and client.moves == []


def test_cntrl_client_request_body_is_masked(monkeypatch):
    from cntrl_sync import core

    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _urlopen(req, timeout=None):
        sent.append(req.data)
        return _Resp()

    monkeypatch.setattr(core.urllib.request, "urlopen", _urlopen)
    client = core.CntrlClient("http://127.0.0.1:9", "tb_fake")
    client.comment("abc", SECRET_TEXT)
    client.move("abc", "done", 1)
    _assert_masked(sent[0])
    assert json.loads(sent[1]) == {"status": "done", "position": 1}


# ---------------------------------------------------------------------------
# Skill Sync
# ---------------------------------------------------------------------------

def test_skill_sync_blobs_are_masked_binary_untouched_mode_kept(tmp_path):
    from tools.skills_sync_client_wire import DEFAULT_MAX_OBJECT_BYTES, KIND_BLOB, ObjectSet, build_tree, wire_address

    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n" + SECRET_TEXT, encoding="utf-8")
    script = skill / "run.sh"
    script.write_text("#!/bin/sh\n" + PGPASSWORD + " psql\n", encoding="utf-8")
    script.chmod(0o755)
    binary = b"\x89PNG\r\n\x1a\n\x00\x00" + FLY.encode()
    (skill / "icon.png").write_bytes(binary)

    objects = ObjectSet()
    tree_hash = build_tree(skill, objects, max_object_bytes=DEFAULT_MAX_OBJECT_BYTES)
    tree = json.loads(objects.objects[tree_hash][1])
    by_name = {e["name"]: e for e in tree["entries"]}
    skill_md = objects.objects[by_name["SKILL.md"]["hash"]][1]
    _assert_masked(skill_md)
    assert b"name: my-skill" in skill_md
    run_sh = objects.objects[by_name["run.sh"]["hash"]][1]
    _assert_clean(run_sh)
    assert by_name["run.sh"]["mode"] == "exec"
    assert objects.objects[by_name["icon.png"]["hash"]] == (KIND_BLOB, binary)
    # the wire address is the hash of the MASKED bytes (the server rehashes)
    assert by_name["SKILL.md"]["hash"] == wire_address(skill_md)
    # live skill untouched
    assert FLY in (skill / "SKILL.md").read_text(encoding="utf-8")


def test_skill_sync_commit_message_is_masked():
    from tools.skills_sync_client_wire import ObjectSet, build_commit

    objects = ObjectSet()
    commit = build_commit("sha256:" + "0" * 64, [], owner="me", device="d", message="sync " + FLY,
                          objects=objects, ts="2026-09-27T00:00:00Z")
    _assert_masked(objects.objects[commit][1])


# ---------------------------------------------------------------------------
# Hugging Face trace upload
# ---------------------------------------------------------------------------

_TRACE_MESSAGES = [
    {"role": "user", "content": SECRET_TEXT},
    {"role": "assistant", "content": "ok", "tool_calls": [
        {"id": "call_1", "function": {"name": "terminal", "arguments": json.dumps({"command": f"export {PGPASSWORD}; "
                                                                                              f"psql {DB_URL}"})}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": FLY},
]


@pytest.mark.parametrize("redact", [True, False])
def test_trace_upload_jsonl_is_masked(redact):
    from agent.trace_upload import build_trace_jsonl

    out = build_trace_jsonl(_TRACE_MESSAGES, session_id="s1", model="m", cwd="/tmp", redact=redact)
    _assert_masked(out)
    for line in out.splitlines():
        json.loads(line)  # still valid JSONL


def test_trace_upload_blocked_when_masking_fails(monkeypatch):
    from agent import secret_hygiene
    from agent.trace_upload import TraceRedactionError, build_trace_jsonl

    monkeypatch.setattr(secret_hygiene, "mask_secrets_for_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(TraceRedactionError):
        build_trace_jsonl(_TRACE_MESSAGES, session_id="s1", redact=True)


# ---------------------------------------------------------------------------
# hermes debug share
# ---------------------------------------------------------------------------

def test_debug_share_log_redaction_uses_the_masker():
    from hermes_cli.debug import _redact_log_text

    out = _redact_log_text("2026-09-27 INFO " + SECRET_TEXT)
    _assert_masked(out)


@pytest.mark.parametrize("redact", [True, False])
def test_debug_share_bundle_is_masked(monkeypatch, redact):
    from hermes_cli import debug

    monkeypatch.setattr(debug, "_capture_dump", lambda: "dump FLY_API_TOKEN=" + FLY + "\n")
    snaps = {name: debug.LogSnapshot(path=None, tail_text=SECRET_TEXT, full_text=SECRET_TEXT)
             for name in set(debug._REPORT_LOGS) | set(debug._FULL_LOGS)}
    monkeypatch.setattr(debug, "_capture_default_log_snapshots", lambda log_lines, redact=True: snaps)
    bundle = debug.collect_share_bundle(log_lines=10, redact=redact)
    assert bundle
    for text in bundle.values():
        _assert_masked(text)


# ---------------------------------------------------------------------------
# /save (CLI and gateway share render_session_for_save)
# ---------------------------------------------------------------------------

_SESSION = {"id": "20260927_abc", "title": "deploy " + FLY, "model": "m", "started_at": 1790000000,
            "messages": [{"role": "user", "content": SECRET_TEXT},
                         {"role": "tool", "content": json.dumps({"out": DB_URL})}]}


@pytest.mark.parametrize("fmt", ["json", "md", "html"])
def test_save_masks_by_default(fmt):
    from hermes_cli.session_export import render_session_for_save

    out = render_session_for_save(json.loads(json.dumps(_SESSION)), fmt)
    _assert_masked(out)


def test_save_redact_token_keeps_placeholders_whole():
    from hermes_cli.session_export import render_session_for_save

    session = json.loads(json.dumps(_SESSION))
    session["messages"][0]["content"] = "DATABASE_URL=[REDACTED:db-password:01234567]"
    out = render_session_for_save(session, "json")
    assert "[REDACTED:db-password:01234567]" in out


def test_save_masks_short_value_using_credential_key_context():
    from agent.secret_egress import mask_egress_text
    from hermes_cli.session_export import render_session_for_save

    session = {"id": "s1", "messages": [], "credentials": {"PGPASSWORD": "Tr0ub4dor&3"}}
    out = render_session_for_save(session, "json")
    if "Tr0ub4dor&3" in out:
        pytest.fail("/save retained a credential under its structured key")
    assert "[REDACTED:" in out
    env_line = mask_egress_text("export PGPASSWORD=Tr0ub4dor&3\n", surface="test")
    if "Tr0ub4dor&3" in env_line:
        pytest.fail("egress retained an environment credential under its key")
    assert "[REDACTED:" in env_line


def test_trace_upload_no_redact_masks_short_value_using_credential_key_context():
    from agent.trace_upload import build_trace_jsonl

    args = json.dumps({"PGPASSWORD": "Tr0ub4dor&3"})
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "function": {"name": "terminal", "arguments": args}}
    ]}]
    out = build_trace_jsonl(messages, session_id="s1", redact=False)
    if "Tr0ub4dor&3" in out:
        pytest.fail("trace upload retained a credential under its structured key")
    assert "[REDACTED:" in out


# ---------------------------------------------------------------------------
# hermes backup
# ---------------------------------------------------------------------------

def _seed_state_db(path: Path) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=path)
    try:
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content=SECRET_TEXT)
        db.append_message("s1", role="assistant", content="noted")
    finally:
        db.close()


def _db_text(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT content FROM messages").fetchall()
        return "\n".join(str(r[0]) for r in rows)
    finally:
        conn.close()


def _fts_hits(path: Path, token: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                                (f'"{token}"',)).fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def backup_home(tmp_path, hermes_home, monkeypatch):
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "ensure_gateway_service", lambda **kw: False, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_state_db(hermes_home / "state.db")
    (hermes_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (hermes_home / ".env").write_text("OPENROUTER_API_KEY=" + SK_ANT + "\n", encoding="utf-8")
    pastes = hermes_home / "composer-pastes"
    pastes.mkdir()
    paste = pastes / "pasted_content_1.txt"
    paste.write_text(SECRET_TEXT, encoding="utf-8")
    paste.chmod(0o600)
    att = hermes_home / "profiles" / "work" / "attachments"
    att.mkdir(parents=True)
    (att / "creds.txt").write_text(SECRET_TEXT, encoding="utf-8")
    (att / "creds.txt").chmod(0o644)
    (att / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + FLY.encode())
    return hermes_home


def test_backup_archive_masks_staged_copies_only(tmp_path, backup_home):
    from hermes_cli.backup import run_backup

    out_zip = tmp_path / "out" / "backup.zip"
    out_zip.parent.mkdir()
    assert run_backup(Namespace(output=str(out_zip))) is not False
    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
        paste = zf.getinfo("composer-pastes/pasted_content_1.txt")
        _assert_masked(zf.read(paste))
        assert stat.S_IMODE(paste.external_attr >> 16) == 0o600
        att = zf.getinfo("profiles/work/attachments/creds.txt")
        _assert_masked(zf.read(att))
        assert stat.S_IMODE(att.external_attr >> 16) == 0o644
        assert zf.read("profiles/work/attachments/photo.png").endswith(FLY.encode())  # binary untouched
        _assert_clean(zf.read(".env"))
        assert "[REDACTED:anthropic-key:" in zf.read(".env").decode()
        assert "state.db" in names
        extracted = tmp_path / "restored-state.db"
        extracted.write_bytes(zf.read("state.db"))
    _assert_masked(_db_text(extracted))
    assert _fts_hits(extracted, "FAKEflyMacaroonBody0123456789QRSTWXYZ") == 0
    assert _fts_hits(extracted, "noted") == 1  # the index was rebuilt, not dropped
    # the live copies are untouched
    assert FLY in _db_text(backup_home / "state.db")
    assert FLY in (backup_home / "composer-pastes" / "pasted_content_1.txt").read_text(encoding="utf-8")


def test_backup_egress_masking_is_not_disabled_by_sweep_setting(tmp_path, backup_home):
    from hermes_cli.backup import run_backup

    (backup_home / "config.yaml").write_text(
        "security:\n  secret_hygiene:\n    sweep:\n      on_backup: false\n", encoding="utf-8")
    out_zip = tmp_path / "out" / "backup.zip"
    out_zip.parent.mkdir()
    run_backup(Namespace(output=str(out_zip)))
    with zipfile.ZipFile(out_zip) as zf:
        _assert_masked(zf.read("composer-pastes/pasted_content_1.txt"))


# ---------------------------------------------------------------------------
# profile export
# ---------------------------------------------------------------------------

def test_profile_export_masks_staged_tree(tmp_path, hermes_home, monkeypatch):
    from hermes_cli.profiles import export_profile

    profiles_root = tmp_path / "profiles"
    profile = profiles_root / "work"
    (profile / "memories").mkdir(parents=True)
    (profile / "memories" / "MEMORY.md").write_text(SECRET_TEXT, encoding="utf-8")
    (profile / "memories" / "MEMORY.md").chmod(0o640)
    (profile / "attachments").mkdir()
    (profile / "attachments" / "pasted_blob").write_text(SECRET_TEXT, encoding="utf-8")  # no suffix
    (profile / "sessions").mkdir()
    (profile / "sessions" / "s1.jsonl").write_text(json.dumps({"role": "user", "content": SECRET_TEXT}) + "\n",
                                                   encoding="utf-8")
    _seed_state_db(profile / "state.db")
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: profile)
    monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)

    archive = export_profile("work", str(tmp_path / "work-export.tar.gz"))
    out = tmp_path / "x"
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(out, filter="data")
    root = out / "work"
    _assert_masked((root / "memories" / "MEMORY.md").read_bytes())
    assert stat.S_IMODE((root / "memories" / "MEMORY.md").stat().st_mode) == 0o640
    _assert_masked((root / "attachments" / "pasted_blob").read_bytes())
    line = (root / "sessions" / "s1.jsonl").read_text(encoding="utf-8")
    _assert_masked(line)
    json.loads(line)
    _assert_masked(_db_text(root / "state.db"))
    assert _fts_hits(root / "state.db", "FAKEflyMacaroonBody0123456789QRSTWXYZ") == 0
    # live profile untouched
    assert FLY in (profile / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert FLY in _db_text(profile / "state.db")


# ---------------------------------------------------------------------------
# kanban board export
# ---------------------------------------------------------------------------

def test_kanban_export_masks_db_and_attachments(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_transfer as kt

    root = tmp_path / "kroot"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.create_board("alpha", name="Alpha")
    with kbc.connect_closing(board="alpha") as conn:
        tid = kb.create_task(conn, title="deploy " + FLY, body=SECRET_TEXT, assignee="coder")
        kb.add_comment(conn, tid, "me", "use " + PGPASSWORD)
        kb.store_attachment_bytes(conn, tid, "notes.txt", SECRET_TEXT.encode(), board="alpha")

    archive = kt.export_board("alpha", str(tmp_path / "alpha"))["archive"]
    out = tmp_path / "x"
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(out, filter="data")
    conn = sqlite3.connect(str(out / "alpha" / "kanban.db"))
    try:
        title, body = conn.execute("SELECT title, body FROM tasks").fetchone()
        (comment,) = conn.execute("SELECT body FROM task_comments").fetchone()
        (task_id,) = conn.execute("SELECT id FROM tasks").fetchone()
    finally:
        conn.close()
    _assert_masked(title + body)
    _assert_clean(comment)
    assert task_id == tid  # ids never masked
    notes = [p for p in (out / "alpha" / "attachments").rglob("notes.txt")]
    assert notes
    _assert_masked(notes[0].read_bytes())
    # live board untouched
    with kbc.connect_closing(board="alpha") as conn:
        assert FLY in conn.execute("SELECT body FROM tasks").fetchone()[0]


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------

def test_egress_masking_off_switch(monkeypatch):
    from agent import secret_egress

    from agent.secret_hygiene import SecretHygieneConfig

    monkeypatch.setattr(secret_egress._sh, "load_secret_hygiene_config",
                        lambda: SecretHygieneConfig(enabled=False))
    assert secret_egress.mask_egress_text(FLY, surface="t") == FLY


def test_egress_file_mask_preserves_mode_and_replaces_symlink(tmp_path):
    from agent.secret_egress import EgressMasker

    src = tmp_path / "live.txt"
    src.write_text(SECRET_TEXT, encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    link = staged / "link.txt"
    os.symlink(src, link)
    plain = staged / "plain.md"
    plain.write_text(SECRET_TEXT, encoding="utf-8")
    plain.chmod(0o604)
    masker = EgressMasker("test")
    assert masker.file_in_place(link) and masker.file_in_place(plain)
    assert not link.is_symlink()
    _assert_masked(link.read_bytes())
    assert FLY in src.read_text(encoding="utf-8")  # never written through the link
    assert stat.S_IMODE(plain.stat().st_mode) == 0o604
    assert masker.counts["fly-token"] == 2


def test_sqlite_egress_masks_every_table_text_column(tmp_path):
    from agent.secret_egress import EgressMasker, STATE_DB_COLUMNS

    secret = "npg_" + "fakeTestCredential0123456789"
    path = tmp_path / "staged.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE messages (reasoning_details TEXT, codex_message_items TEXT)")
        conn.execute("INSERT INTO messages VALUES (?, ?)",
                     (secret, json.dumps({"token": secret})))
        conn.execute("CREATE TABLE system_prompts (prompt TEXT)")
        conn.execute("INSERT INTO system_prompts VALUES (?)", (secret,))

    EgressMasker("test").sqlite_file(path, STATE_DB_COLUMNS)
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT reasoning_details, codex_message_items FROM messages").fetchone()
        prompt = conn.execute("SELECT prompt FROM system_prompts").fetchone()[0]
    if any(secret in value for value in (*rows, prompt)):
        pytest.fail("staged SQLite text column retained a detected secret")
    assert all("[REDACTED:" in value for value in (*rows, prompt))


@pytest.mark.parametrize("name", ["data.sqlite", "data.sqlite3", "data.bin"])
def test_backup_masks_sqlite_files_by_extension_or_header(tmp_path, name):
    from agent.secret_egress import EgressMasker
    from hermes_cli.backup import _zip_egress_file

    secret = "npg_" + "fakeBackupCredential0123456789"
    live = tmp_path / name
    with sqlite3.connect(live) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES (?)", (secret,))
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        _zip_egress_file(zf, live, Path(name), tmp_path, EgressMasker("test"), tmp_path)
    with zipfile.ZipFile(archive) as zf, sqlite3.connect(":memory:") as conn:
        conn.deserialize(zf.read(name))
        cell = conn.execute("SELECT value FROM sample").fetchone()[0]
    if secret in cell:
        pytest.fail("backup SQLite snapshot retained a detected secret")
    assert "[REDACTED:" in cell
    with sqlite3.connect(live) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == secret
