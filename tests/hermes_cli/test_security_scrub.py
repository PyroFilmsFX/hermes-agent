"""HE-SECRET-HYGIENE S3: ``hermes security scrub`` sweep engine.

Temp data only: every test builds a throwaway Hermes root under ``tmp_path``
(root-level ``composer-pastes/``, a ``thinkbot`` profile with attachments,
transcripts and a real SessionDB ``state.db``) plus a throwaway Claude config
dir. Secrets are fake values from a seeded PRNG; the tag key is fixed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import string
import time
from pathlib import Path

import pytest

from agent.secret_hygiene import SecretHygieneConfig, StaticTagKeyProvider
from hermes_cli import security_scrub as scrub

_ALNUM = string.ascii_letters + string.digits
KEY = StaticTagKeyProvider(b"\x11" * 32)


def fake(n: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(_ALNUM) for _ in range(n - 3)) + "aZ9"


NEON_PW = "npg_" + fake(12, 1)
NEON_URL = f"postgresql://neondb_owner:{NEON_PW}@ep-demo-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
GH_BODY = fake(36, 2)
GH_TOKEN = "ghp_" + GH_BODY
FLY = "FlyV1 fm2_" + fake(60, 3)
PG_PW = fake(14, 4)
LIVE_SECRET = "sk-ant-api03-" + fake(60, 5)
OPTOUT_SECRET = "ghp_" + fake(36, 6)
SDK_SECRET = "fo1_" + fake(43, 7)
OWN_CLAUDE_SECRET = "ghp_" + fake(36, 8)
AMBIG_SECRET = "ghp_" + fake(36, 9)
ALL_FAKES = [NEON_PW, GH_TOKEN, GH_BODY, FLY, PG_PW, LIVE_SECRET, OPTOUT_SECRET, SDK_SECRET,
             OWN_CLAUDE_SECRET, AMBIG_SECRET]

SDK_ID = "5b0c1d2e-0000-4000-8000-00000000abcd"
AMBIG_SDK_ID = "5b0c1d2e-0000-4000-8000-00000000ef01"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row_hash(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, content, api_content, tool_calls, reasoning, display_metadata FROM messages ORDER BY id"
        ).fetchall()
        rows += conn.execute("SELECT id, title FROM sessions ORDER BY id").fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


class Home:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "hermes-root"
        self.profile = self.root / "profiles" / "thinkbot"
        self.claude = tmp_path / "claude-config"
        self.db = self.profile / "state.db"
        self.paste = self.root / "composer-pastes" / "pasted_content_1_ab12.txt"
        self.attachment = self.profile / "attachments" / "pasted_content_1_ab12.txt"
        self.image = self.profile / "attachments" / "shot.png"
        self.transcript = self.profile / "sessions" / "s_closed.jsonl"
        proj = self.claude / "projects" / "-tmp-demo-project"
        self.sdk_transcript = proj / f"{SDK_ID}.jsonl"
        self.own_transcript = proj / "11111111-2222-4333-8444-555555555555.jsonl"
        self.ambiguous_transcript = proj / f"{AMBIG_SDK_ID}.jsonl"

    def targets(self):
        return [self.paste, self.attachment, self.image, self.transcript, self.sdk_transcript]

    def untouchables(self):
        return [self.own_transcript, self.ambiguous_transcript]


def _write(path: Path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    os.chmod(path, mode)


@pytest.fixture
def home(tmp_path):
    from hermes_state import SessionDB

    h = Home(tmp_path)
    paste = f"here is the db\n{NEON_URL}\nthanks\n"
    _write(h.paste, paste)
    _write(h.attachment, paste)
    _write(h.image, b"\x89PNG\r\n\x1a\n\x00\x00" + GH_TOKEN.encode())
    _write(h.transcript, "\n".join([
        json.dumps({"role": "user", "content": f"export GITHUB_TOKEN={GH_TOKEN}"}),
        json.dumps({"role": "assistant", "content": "ok"}),
        "not json but has " + FLY,
    ]) + "\n")
    sdk_lines = [{"type": "user", "entrypoint": "sdk-py", "sessionId": SDK_ID,
                  "message": {"role": "user", "content": f"org token {SDK_SECRET}"}},
                 {"type": "assistant", "entrypoint": "sdk-py", "sessionId": SDK_ID,
                  "message": {"role": "assistant", "content": "noted"}}]
    _write(h.sdk_transcript, "\n".join(json.dumps(x) for x in sdk_lines) + "\n")
    _write(h.own_transcript, json.dumps({"type": "user", "entrypoint": "cli",
                                         "message": {"content": OWN_CLAUDE_SECRET}}) + "\n")
    _write(h.ambiguous_transcript, json.dumps({"type": "user", "entrypoint": "cli", "sessionId": AMBIG_SDK_ID,
                                               "message": {"content": AMBIG_SECRET}}) + "\n")

    h.profile.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=h.db)
    try:
        for sid in ("s_closed", "s_live", "s_optout", "s_sdk", "s_ambig"):
            db.create_session(sid, "desktop")
        db.append_message("s_closed", "user", f"my db is {NEON_URL}")
        db.append_message("s_closed", "user", f"PGPASSWORD={PG_PW} psql -h db")
        db.append_message("s_closed", "tool", json.dumps({"output": f"token={GH_TOKEN}"}))
        db.append_message("s_closed", "assistant", "done",
                          tool_calls=[{"id": "c1", "type": "function",
                                       "function": {"name": "terminal", "arguments": json.dumps(
                                           {"command": f"fly deploy --access-token '{FLY}'"})}}])
        db.append_message("s_closed", "user", f"search me {GH_TOKEN}")
        db.append_message("s_live", "user", f"live key {LIVE_SECRET}")
        db.append_message("s_optout", "user", f"send raw {OPTOUT_SECRET}",
                          display_metadata={"secret_optout": {"kinds": {"github-token": 1}}})
        db.append_message("s_sdk", "user", "hello")
        db.append_message("s_ambig", "user", "hello")
    finally:
        db.close()
    conn = sqlite3.connect(h.db)
    now = time.time()
    conn.execute("UPDATE sessions SET ended_at = ?, title = ? WHERE id = 's_closed'",
                 (now - 3600, f"debug {GH_TOKEN}"))
    conn.execute("UPDATE sessions SET claude_sdk_session_id = ? WHERE id = 's_sdk'", (SDK_ID,))
    conn.execute("UPDATE sessions SET claude_sdk_session_id = ? WHERE id = 's_ambig'", (AMBIG_SDK_ID,))
    conn.execute("INSERT INTO session_turn_leases(conversation_id, holder, acquired_at, expires_at) "
                 "VALUES ('s_live', 'pid:999999', ?, ?)", (now, now + 600))
    conn.commit()
    conn.close()
    return h


def run(h: Home, **kw):
    kw.setdefault("apply", False)
    kw.setdefault("recent_write_grace_s", 0)  # fixture files were written a moment ago
    kw.setdefault("open_writers_fn", lambda _p: [])  # the real lsof probe has its own test
    kw.setdefault("backend_probe", lambda _root, _profiles: [])  # isolate from backends on this machine
    return scrub.run_scrub(
        root=h.root, profiles=[("thinkbot", h.profile)], config=SecretHygieneConfig(),
        key_provider=KEY, claude_config_dir=h.claude, holders_fn=lambda _p: [], **kw)


def _items(report, **match):
    return [i for i in report.items if all(getattr(i, k) == v for k, v in match.items())]


def _snapshot(h: Home):
    files = {p: _sha(p) for p in h.targets() + h.untouchables()}
    return files, _row_hash(h.db)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_changes_nothing_and_report_holds_no_values(home):
    before = _snapshot(home)
    report = run(home)
    assert _snapshot(home) == before
    blob = json.dumps(report.to_json()) + report.render_text()
    for value in ALL_FAKES:
        assert value not in blob
    assert report.exit_code == 0
    assert report.backup_path is None
    assert not (home.root / "backups").exists()
    locators = {i.locator for i in report.items if i.action == "would-mask"}
    assert "composer-pastes/pasted_content_1_ab12.txt" in locators
    assert "profiles/thinkbot/attachments/pasted_content_1_ab12.txt" in locators
    assert "profiles/thinkbot/sessions/s_closed.jsonl" in locators
    assert any(loc.startswith("profiles/thinkbot/state.db:messages#") for loc in locators)
    assert "profiles/thinkbot/state.db:sessions#s_closed.title" in locators
    assert _items(report, locator="composer-pastes/pasted_content_1_ab12.txt")[0].kind == "neon-url"
    assert _items(report, action="skipped-binary")[0].locator.endswith("attachments/shot.png")
    assert report.totals()["neon-url"] >= 3


def test_dry_run_lists_live_optout_and_ambiguous(home):
    report = run(home)
    assert _items(report, action="deferred-live")
    assert all("s_live" in i.detail for i in _items(report, action="deferred-live"))
    assert _items(report, action="skipped-optout")
    (amb,) = _items(report, action="ambiguous-skip")
    assert AMBIG_SDK_ID in amb.locator
    (sdk,) = _items(report, target="sdk-transcripts", action="would-mask")
    assert sdk.kind == "fly-token" and SDK_ID in sdk.locator
    assert not any("11111111-2222" in i.locator for i in report.items)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_backs_up_first_masks_and_second_run_is_noop(home, monkeypatch):
    seen_backup = []
    real_rewrite = scrub._atomic_rewrite_unit

    def spy(*a, **kw):
        backups = list((home.root / "backups" / "secret-scrub").glob("*/manifest.json"))
        seen_backup.append(bool(backups))
        return real_rewrite(*a, **kw)

    monkeypatch.setattr(scrub, "_atomic_rewrite_unit", spy)
    report = run(home, apply=True)
    assert report.exit_code == 0, report.render_text()
    assert seen_backup and all(seen_backup)
    backup = Path(report.backup_path)
    assert backup.is_dir() and backup.stat().st_mode & 0o777 == 0o700
    assert str(backup) in report.render_text()
    for f in backup.rglob("*"):
        if f.is_file():
            assert f.stat().st_mode & 0o777 == 0o600, f
    manifest = json.loads((backup / "manifest.json").read_text())
    assert all(value not in json.dumps(manifest) for value in ALL_FAKES)

    for path in (home.paste, home.attachment, home.transcript, home.sdk_transcript):
        text = path.read_text()
        assert all(v not in text for v in ALL_FAKES), path
        assert "[REDACTED:" in text
        assert path.stat().st_mode & 0o777 == 0o644  # the original mode is preserved (P2 b)
    assert "@ep-demo-123456.us-east-2.aws.neon.tech/neondb" in home.paste.read_text()
    for line in home.transcript.read_text().splitlines()[:2]:
        json.loads(line)
    for line in home.sdk_transcript.read_text().splitlines():
        assert json.loads(line)["entrypoint"] == "sdk-py"

    conn = sqlite3.connect(home.db)
    dump = repr(conn.execute("SELECT content, tool_calls, display_metadata FROM messages "
                             "WHERE session_id = 's_closed'").fetchall())
    title = conn.execute("SELECT title FROM sessions WHERE id = 's_closed'").fetchone()[0]
    tool_calls = conn.execute("SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL").fetchone()[0]
    conn.close()
    assert all(v not in dump for v in ALL_FAKES)
    assert "[REDACTED:" in title and GH_TOKEN not in title
    json.loads(tool_calls)

    second = run(home, apply=True)
    assert second.exit_code == 0
    assert not _items(second, action="masked")
    assert second.backup_path is None


def test_apply_skips_live_session_and_records_deferral(home):
    report = run(home, apply=True)
    conn = sqlite3.connect(home.db)
    live = conn.execute("SELECT content FROM messages WHERE session_id = 's_live'").fetchone()[0]
    conn.close()
    assert LIVE_SECRET in live
    assert _items(report, action="deferred-live")
    deferred = json.loads((home.root / "secret-scrub" / "deferred.json").read_text())
    assert any(d["session_id"] == "s_live" for d in deferred["sessions"])
    assert LIVE_SECRET not in json.dumps(deferred)


def test_optout_row_skipped_unless_included(home):
    run(home, apply=True)
    conn = sqlite3.connect(home.db)
    q = "SELECT content FROM messages WHERE session_id = 's_optout'"
    assert OPTOUT_SECRET in conn.execute(q).fetchone()[0]
    conn.close()
    run(home, apply=True, include_optouts=True)
    conn = sqlite3.connect(home.db)
    assert OPTOUT_SECRET not in conn.execute(q).fetchone()[0]
    conn.close()


def test_sdk_transcripts_only_hermes_created_are_masked(home):
    own, amb = _sha(home.own_transcript), _sha(home.ambiguous_transcript)
    report = run(home, apply=True)
    assert _sha(home.own_transcript) == own
    assert _sha(home.ambiguous_transcript) == amb
    assert SDK_SECRET not in home.sdk_transcript.read_text()
    assert _items(report, action="ambiguous-skip")


def test_binary_attachment_byte_identical(home):
    before = _sha(home.image)
    report = run(home, apply=True)
    assert _sha(home.image) == before
    assert _items(report, action="skipped-binary")


def test_fts_index_no_longer_matches_fake_token(home):
    def counts():
        conn = sqlite3.connect(home.db)
        try:
            fts = conn.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",
                               (f'"{GH_BODY}"',)).fetchone()[0]
            tri = conn.execute("SELECT count(*) FROM messages_fts_trigram WHERE messages_fts_trigram MATCH ?",
                               (f'"{GH_BODY[4:20]}"',)).fetchone()[0]
        finally:
            conn.close()
        return fts, tri

    before = counts()
    assert before[0] >= 1 and before[1] >= 1
    assert run(home, apply=True).exit_code == 0
    assert counts() == (0, 0)


def test_refuses_while_fts_rebuild_in_progress(home):
    conn = sqlite3.connect(home.db)
    conn.execute("INSERT INTO state_meta(key, value) VALUES ('fts_rebuild_progress', '3')")
    conn.commit()
    conn.close()
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 2
    assert "rebuild" in (report.refused or "")
    assert _snapshot(home) == before
    assert not (home.root / "backups").exists()


def test_refuses_while_another_scrub_holds_the_lock(home):
    (home.root / ".secret-scrub.lock").write_text(str(os.getpid()))
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 2 and "lock" in report.refused
    assert _snapshot(home) == before


def test_integrity_failure_exits_nonzero_and_keeps_backup(home, monkeypatch):
    monkeypatch.setattr(scrub, "_integrity_problems", lambda conn: ["*** in database main ***"])
    report = run(home, apply=True)
    assert report.exit_code == 1
    assert report.backup_path and Path(report.backup_path).is_dir()
    assert str(report.backup_path) in report.render_text()
    assert any("integrity" in e for e in report.errors)


def test_resume_after_failure_at_batch_two(home, monkeypatch):
    conn = sqlite3.connect(home.db)
    for n in range(6):
        conn.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s_closed', 'user', ?, ?)",
                     (f"extra {n} ghp_{fake(36, 100 + n)}", time.time()))
    conn.commit()
    conn.close()
    monkeypatch.setattr(scrub, "BATCH_SIZE", 2)
    calls = []

    def boom(batch_no):
        calls.append(batch_no)
        if batch_no == 2:
            raise RuntimeError("injected failure at batch 2")

    monkeypatch.setattr(scrub, "_after_batch_commit", boom)
    first = run(home, apply=True)
    assert first.exit_code == 1
    conn = sqlite3.connect(home.db)
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    remaining = conn.execute("SELECT count(*) FROM messages WHERE content LIKE '%ghp_%' "
                             "AND session_id = 's_closed'").fetchone()[0]
    conn.close()
    assert progress["complete"] is False and progress["pending"]
    assert remaining > 0

    monkeypatch.setattr(scrub, "_after_batch_commit", lambda batch_no: None)
    second = run(home, apply=True)
    assert second.exit_code == 0, second.render_text()
    conn = sqlite3.connect(home.db)
    assert conn.execute("SELECT count(*) FROM messages WHERE content LIKE '%ghp_%' "
                        "AND session_id = 's_closed'").fetchone()[0] == 0
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    conn.close()
    # s_live is still deferred, so the run is not complete while it stays pending
    assert progress["pending"] and progress["complete"] is False
    assert [p for p in progress["pending"] if "s_live" not in p and "#6." not in p] == []


# ---------------------------------------------------------------------------
# Owner rules: backup must restore before any write; never delete or truncate a target
# ---------------------------------------------------------------------------

def test_corrupted_backup_aborts_apply_with_zero_writes(home, monkeypatch):
    def corrupt(backup_dir: Path):
        victim = next(p for p in sorted(backup_dir.rglob("*")) if p.is_file() and p.name.endswith(".txt"))
        victim.write_bytes(b"corrupted")

    monkeypatch.setattr(scrub, "_after_backup_written", corrupt)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 1
    assert any("verif" in e for e in report.errors)
    assert _snapshot(home) == before


def test_corrupted_db_backup_aborts_apply_with_zero_writes(home, monkeypatch):
    def corrupt(backup_dir: Path):
        victim = next(backup_dir.rglob("state.db"))
        data = bytearray(victim.read_bytes())
        data[4096:8192] = b"\xff" * 4096
        victim.write_bytes(bytes(data))

    monkeypatch.setattr(scrub, "_after_backup_written", corrupt)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 1
    assert _snapshot(home) == before


def test_scrub_never_unlinks_or_truncates_a_target(home, monkeypatch):
    targets = {str(p.resolve()) for p in home.targets() + home.untouchables() + [home.db]}
    targets |= {t + suffix for t in list(targets) for suffix in ("-wal", "-shm")}
    removed = []

    def _record(name, real):
        def wrapper(path, *a, **kw):
            removed.append((name, str(Path(os.fsdecode(path)).resolve()) if not isinstance(path, int) else path))
            return real(path, *a, **kw)
        return wrapper

    monkeypatch.setattr(os, "unlink", _record("unlink", os.unlink))
    monkeypatch.setattr(os, "remove", _record("remove", os.remove))
    monkeypatch.setattr(os, "truncate", _record("truncate", os.truncate))
    monkeypatch.setattr(shutil, "rmtree", _record("rmtree", shutil.rmtree))
    sizes = {p: p.stat().st_size for p in home.targets()}

    report = run(home, apply=True)
    assert report.exit_code == 0, report.render_text()
    hit = [(n, p) for n, p in removed if p in targets or any(str(p).startswith(t) for t in targets)]
    assert hit == []
    for path in home.targets() + home.untouchables():
        assert path.exists() and path.stat().st_size > 0
    assert all(p.exists() for p in sizes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_parser_and_dry_run_json(home, monkeypatch, capsys):
    import argparse

    from hermes_cli.subcommands.security import build_security_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_security_parser(sub, cmd_security=lambda a: None, enable_scrub=True)
    args = parser.parse_args(["security", "scrub", "--json", "--targets", "pastes,state-db"])
    assert args.security_command == "scrub" and args.apply is False
    assert args.targets == "pastes,state-db"

    monkeypatch.setenv("HERMES_HOME", str(home.profile))
    monkeypatch.setattr(scrub, "_default_holders", lambda _p: [])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.claude))
    code = scrub.cmd_security_scrub(args, config=SecretHygieneConfig(), key_provider=KEY)
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["mode"] == "dry-run"
    assert {i["target"] for i in payload["items"]} <= {"pastes", "state-db"}
    assert any(i["locator"].startswith("composer-pastes/") for i in payload["items"])
    assert all(v not in out for v in ALL_FAKES)


def test_scrub_cli_is_not_registered_by_default():
    import argparse

    from hermes_cli.subcommands.security import build_security_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_security_parser(sub, cmd_security=lambda a: None)
    with pytest.raises(SystemExit):
        parser.parse_args(["security", "scrub"])


# ---------------------------------------------------------------------------
# Security review round 1 (P1 1-8, P2 a-b)
# ---------------------------------------------------------------------------

def _cli_args(*extra):
    import argparse

    from hermes_cli.subcommands.security import build_security_parser

    parser = argparse.ArgumentParser()
    build_security_parser(
        parser.add_subparsers(dest="command"), cmd_security=lambda a: None, enable_scrub=True)
    return parser.parse_args(["security", "scrub", *extra])


def _cli(home, monkeypatch, *extra):
    monkeypatch.setenv("HERMES_HOME", str(home.profile))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.claude))
    monkeypatch.setattr(scrub, "_default_holders", lambda _p: [])
    return scrub.cmd_security_scrub(_cli_args(*extra), config=SecretHygieneConfig(), key_provider=KEY)


# P1-1: --report only ever creates a NEW private file outside every scrub root.

def test_report_refuses_an_existing_file_and_keeps_it(home, monkeypatch, tmp_path, capsys):
    out = tmp_path / "existing-report.json"
    out.write_text("keep me")
    assert _cli(home, monkeypatch, "--report", str(out)) != 0
    assert out.read_text() == "keep me"
    assert "exists" in capsys.readouterr().err


@pytest.mark.parametrize("dangling", [False, True])
def test_report_refuses_a_symlink(home, monkeypatch, tmp_path, dangling):
    victim = tmp_path / "victim.txt"
    if not dangling:
        victim.write_text("victim bytes")
    link = tmp_path / "report-link.json"
    link.symlink_to(victim)
    assert _cli(home, monkeypatch, "--report", str(link)) != 0
    assert link.is_symlink()
    if dangling:
        assert not victim.exists()
    else:
        assert victim.read_text() == "victim bytes"


@pytest.mark.parametrize("where", ["pastes", "profile", "claude-projects", "via-symlinked-parent"])
def test_report_refuses_paths_inside_a_scrub_root(home, monkeypatch, tmp_path, where):
    if where == "pastes":
        out = home.root / "composer-pastes" / "report.json"
    elif where == "profile":
        out = home.profile / "sessions" / "report.json"
    elif where == "claude-projects":
        out = home.claude / "projects" / "report.json"
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(home.root / "composer-pastes")
        out = alias / "report.json"
    assert _cli(home, monkeypatch, "--report", str(out)) != 0
    assert not out.exists()
    assert not (home.root / "composer-pastes" / "report.json").exists()


def test_report_new_file_is_created_0600_without_values(home, monkeypatch, tmp_path):
    out = tmp_path / "fresh" / "report.json"
    out.parent.mkdir()
    assert _cli(home, monkeypatch, "--report", str(out)) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    body = out.read_text()
    assert json.loads(body)["mode"] == "dry-run"
    assert all(v not in body for v in ALL_FAKES)


# P1-2: a file that changes between plan and replace is left alone and reported.

def test_append_between_plan_and_replace_leaves_file_untouched(home, monkeypatch):
    appended = b"late line appended by a live writer\n"
    hits = []

    def append(path):
        if Path(path).name == home.paste.name and Path(path).parent.name == "composer-pastes":
            hits.append(path)
            with open(home.paste, "ab") as fh:
                fh.write(appended)

    original = home.paste.read_bytes()
    monkeypatch.setattr(scrub, "_before_replace", append, raising=False)
    report = run(home, apply=True)
    assert hits, "the pre-replace seam must run"
    assert home.paste.read_bytes() == original + appended
    changed = _items(report, locator="composer-pastes/pasted_content_1_ab12.txt", action="changed-during-apply")
    assert changed
    assert not _items(report, locator="composer-pastes/pasted_content_1_ab12.txt", action="masked")
    assert NEON_PW not in home.attachment.read_text()  # other files still masked
    leftovers = [p for p in home.paste.parent.iterdir() if p.name.endswith(".scrub-tmp")]
    assert leftovers == []


def test_file_with_an_open_writer_is_deferred(home):
    report = run(home, apply=True, open_writers_fn=lambda p: [4242] if Path(p).name == home.paste.name
                 and Path(p).parent.name == "composer-pastes" else [])
    assert NEON_PW in home.paste.read_text()
    (item,) = _items(report, locator="composer-pastes/pasted_content_1_ab12.txt")
    assert item.action == "deferred-live" and "open for writing" in item.detail
    assert NEON_PW not in home.attachment.read_text()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
@pytest.mark.live_system_guard_bypass
def test_open_writer_probe_sees_a_real_writer(tmp_path):
    import subprocess
    import sys

    target = tmp_path / "held.txt"
    target.write_text("x")
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c",
                              "import sys,time,pathlib;f=open(sys.argv[1],'a');"
                              "pathlib.Path(sys.argv[2]).write_text('1');time.sleep(30)",
                              str(target), str(ready)])
    try:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert child.pid in scrub._open_writer_pids(target)
        reader_only = tmp_path / "idle.txt"
        reader_only.write_text("y")
        assert scrub._open_writer_pids(reader_only) == []
    finally:
        child.kill()
        child.wait()


# P1-3: a deferred row stays pending across a crashed run and is masked once its session closes.

def test_deferred_row_before_a_crash_is_masked_on_a_later_run(home, monkeypatch):
    conn = sqlite3.connect(home.db)
    conn.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s_closed', 'user', ?, ?)",
                 (f"late {'ghp_' + fake(36, 300)}", time.time()))
    conn.commit()
    live_id = conn.execute("SELECT id FROM messages WHERE session_id = 's_live'").fetchone()[0]
    max_id = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
    conn.close()
    assert live_id < max_id
    monkeypatch.setattr(scrub, "BATCH_SIZE", 2)
    batches = []

    def crash_on_last(batch_no):
        batches.append(batch_no)
        if batch_no == 3:
            raise RuntimeError("injected crash after the batch holding the newest row")

    monkeypatch.setattr(scrub, "_after_batch_commit", crash_on_last)
    first = run(home, apply=True)
    assert first.exit_code == 1 and 3 in batches
    conn = sqlite3.connect(home.db)
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    assert any(f"messages#{live_id}." in p for p in progress["pending"])
    # the live session closes
    conn.execute("DELETE FROM session_turn_leases")
    conn.commit()
    conn.close()
    monkeypatch.setattr(scrub, "_after_batch_commit", lambda batch_no: None)
    second = run(home, apply=True)
    assert second.exit_code == 0, second.render_text()
    conn = sqlite3.connect(home.db)
    live = conn.execute("SELECT content FROM messages WHERE id = ?", (live_id,)).fetchone()[0]
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    conn.close()
    assert LIVE_SECRET not in live and "[REDACTED:" in live
    assert progress["complete"] is True and progress["pending"] == []


# P1-4: every line of every SDK file (subagents included) must belong to a stored SDK session.

def test_unrelated_sdk_transcript_under_qualifying_subagent_dir_is_skipped(home):
    sub = home.sdk_transcript.parent / SDK_ID / "subagents"
    own_secret, foreign_secret = "ghp_" + fake(36, 400), "ghp_" + fake(36, 401)
    ours = sub / "agent-ours.jsonl"
    foreign = sub / "agent-foreign.jsonl"
    mixed = sub / "agent-mixed.jsonl"
    _write(ours, json.dumps({"entrypoint": "sdk-py", "sessionId": SDK_ID,
                             "message": {"content": f"sub {own_secret}"}}) + "\n")
    _write(foreign, json.dumps({"entrypoint": "sdk-py", "sessionId": "0f0f0f0f-1111-4222-8333-444444444444",
                                "message": {"content": f"x {foreign_secret}"}}) + "\n")
    _write(mixed, "\n".join([
        json.dumps({"entrypoint": "sdk-py", "sessionId": SDK_ID, "message": {"content": "hi"}}),
        json.dumps({"entrypoint": "cli", "sessionId": SDK_ID, "message": {"content": f"y {foreign_secret}"}}),
    ]) + "\n")
    before = {p: _sha(p) for p in (foreign, mixed)}
    report = run(home, apply=True)
    assert report.exit_code == 0, report.render_text()
    assert own_secret not in ours.read_text()
    assert {p: _sha(p) for p in (foreign, mixed)} == before
    skipped = {i.locator for i in _items(report, action="ambiguous-skip")}
    assert any(loc.endswith("agent-foreign.jsonl") for loc in skipped)
    assert any(loc.endswith("agent-mixed.jsonl") for loc in skipped)


# P1-5: symlinked dirs/files inside a target are reported and never followed.

def test_symlinked_dirs_and_files_are_skipped_and_outside_untouched(home, tmp_path):
    outside = tmp_path / "outside"
    out_secret = "ghp_" + fake(36, 500)
    _write(outside / "evil.jsonl", json.dumps({"content": f"x {out_secret}"}) + "\n")
    _write(outside / "nested" / "deep.txt", f"token {out_secret}\n")
    _write(outside / "loose.txt", f"token {out_secret}\n")
    before = {p: _sha(p) for p in outside.rglob("*") if p.is_file()}
    shutil.rmtree(home.profile / "sessions")
    (home.profile / "sessions").symlink_to(outside, target_is_directory=True)
    (home.profile / "attachments" / "linked-dir").symlink_to(outside / "nested", target_is_directory=True)
    (home.root / "composer-pastes" / "linked.txt").symlink_to(outside / "loose.txt")
    proj_link = home.claude / "projects" / "-linked-project"
    _write(outside / "proj" / f"{SDK_ID}.jsonl", json.dumps(
        {"entrypoint": "sdk-py", "sessionId": SDK_ID, "message": {"content": out_secret}}) + "\n")
    before[outside / "proj" / f"{SDK_ID}.jsonl"] = _sha(outside / "proj" / f"{SDK_ID}.jsonl")
    proj_link.symlink_to(outside / "proj", target_is_directory=True)

    dry = run(home)
    report = run(home, apply=True)
    assert report.exit_code == 0, report.render_text()
    assert {p: _sha(p) for p in before} == before
    for r in (dry, report):
        skipped = {i.locator for i in _items(r, action="skipped-symlink")}
        assert "profiles/thinkbot/sessions" in skipped
        assert "profiles/thinkbot/attachments/linked-dir" in skipped
        assert "composer-pastes/linked.txt" in skipped
        assert any(loc.endswith("-linked-project") for loc in skipped)
        assert not any(out_secret in json.dumps(r.to_json()) for _ in [0])
        assert not [i for i in r.items if i.action in ("would-mask", "masked") and "outside" in i.locator]


# P1-7: the report never echoes a secret through a locator, detail, warning or ledger.

def test_report_masks_secrets_in_file_names_and_entrypoints(home):
    name_secret = "ghp_" + fake(36, 700)
    glued_secret = "ghp_" + fake(36, 701)
    entry_secret = "ghp_" + fake(36, 702)
    _write(home.root / "composer-pastes" / f"{name_secret}.txt", f"token {'ghp_' + fake(36, 703)}\n")
    _write(home.root / "composer-pastes" / f"leak-{glued_secret}.txt", f"token {'ghp_' + fake(36, 704)}\n")
    _write(home.ambiguous_transcript, json.dumps({"type": "user", "entrypoint": f"cli {entry_secret}",
                                                  "sessionId": AMBIG_SDK_ID, "message": {"content": "x"}}) + "\n")
    for apply in (False, True):
        report = run(home, apply=apply)
        blob = json.dumps(report.to_json()) + report.render_text()
        for secret in (name_secret, glued_secret, entry_secret):
            assert secret not in blob
            assert secret[4:20] not in blob
        (amb,) = _items(report, action="ambiguous-skip")
        assert "entrypoint" in amb.detail and "cli" not in amb.detail
    ledgers = "".join(p.read_text() for p in (home.root / "secret-scrub").glob("*.json"))
    for secret in (name_secret, glued_secret, entry_secret):
        assert secret not in ledgers


# P1-8: an incomplete PASSIVE checkpoint fails the run loudly; a later clean run finishes it.

WAL_MSG = ("state.db changes are committed but old WAL frames may still hold secrets. Close Hermes and "
           "rerun `hermes security scrub --apply` to finish.")


def test_busy_wal_checkpoint_exits_nonzero_then_later_run_completes(home, monkeypatch):
    modes = []
    real = scrub._wal_checkpoint if hasattr(scrub, "_wal_checkpoint") else None

    def busy(conn):
        modes.append("busy")
        return (1, 12, 3)

    monkeypatch.setattr(scrub, "_wal_checkpoint", busy, raising=False)
    first = run(home, apply=True)
    assert modes and first.exit_code != 0
    assert WAL_MSG in first.errors
    assert WAL_MSG in first.render_text()

    monkeypatch.setattr(scrub, "_wal_checkpoint", real)
    second = run(home, apply=True)
    assert second.exit_code == 0, second.render_text()
    assert not _items(second, action="masked")
    assert WAL_MSG not in second.render_text()


def test_wal_checkpoint_uses_passive_not_truncate(home, monkeypatch):
    import inspect

    src = inspect.getsource(scrub)
    assert "wal_checkpoint(TRUNCATE)" not in src
    assert "wal_checkpoint(PASSIVE)" in src


def test_real_reader_blocking_the_checkpoint_is_reported(home):
    # This interpreter's SQLite may make Hermes pick journal_mode=DELETE; force WAL for the probe.
    conn = sqlite3.connect(home.db)
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    conn.close()
    if mode != "wal":
        pytest.skip("WAL mode unavailable")
    reader = sqlite3.connect(home.db)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM messages").fetchone()
    try:
        report = run(home, apply=True)
    finally:
        reader.rollback()
        reader.close()
    assert report.exit_code != 0
    assert WAL_MSG in report.errors


# P1-6 at the scrub level: tool-call ids survive in stored JSON.

def test_tool_call_ids_in_state_db_json_survive_the_scrub(home):
    call_id = "call_cfedFhJjGmu1RvRc1OUC38j8"
    conn = sqlite3.connect(home.db)
    conn.execute("INSERT INTO messages(session_id, role, content, tool_calls, timestamp) "
                 "VALUES ('s_closed', 'assistant', 'x', ?, ?)",
                 (json.dumps([{"id": call_id, "type": "function", "function": {
                     "name": "t", "arguments": json.dumps({"command": f"echo {GH_TOKEN}"})}}]), time.time()))
    conn.execute("INSERT INTO messages(session_id, role, content, tool_call_id, timestamp) "
                 "VALUES ('s_closed', 'tool', ?, ?, ?)",
                 (json.dumps({"tool_call_id": call_id, "output": "done"}), call_id, time.time()))
    conn.commit()
    conn.close()
    assert run(home, apply=True).exit_code == 0
    conn = sqlite3.connect(home.db)
    rows = conn.execute("SELECT content, tool_calls FROM messages WHERE tool_call_id = ? OR tool_calls LIKE ?",
                        (call_id, "%call_%")).fetchall()
    conn.close()
    blob = repr(rows)
    assert blob.count(call_id) >= 2
    assert GH_TOKEN not in blob


# P2-a: DB backup verification hashes the rows it is about to rewrite.

def test_db_backup_with_tampered_planned_row_aborts_with_zero_writes(home, monkeypatch):
    def tamper(backup_dir: Path):
        victim = next(backup_dir.rglob("state.db"))
        conn = sqlite3.connect(victim)
        conn.execute("UPDATE messages SET content = 'swapped' WHERE session_id = 's_closed' AND id = "
                     "(SELECT min(id) FROM messages WHERE session_id = 's_closed')")
        conn.commit()
        conn.close()

    monkeypatch.setattr(scrub, "_after_backup_written", tamper)
    before = _snapshot(home)
    report = run(home, apply=True, targets=["state-db"])
    assert report.exit_code == 1
    assert any("verif" in e for e in report.errors), report.errors
    assert _snapshot(home) == before


# P2-b: the rewrite keeps the original mode (and owner).

def test_rewrite_preserves_original_mode_and_owner(home):
    os.chmod(home.paste, 0o640)
    os.chmod(home.transcript, 0o600)
    st = home.paste.stat()
    assert run(home, apply=True).exit_code == 0
    assert NEON_PW not in home.paste.read_text()
    assert home.paste.stat().st_mode & 0o7777 == 0o640
    assert home.transcript.stat().st_mode & 0o7777 == 0o600
    assert (home.paste.stat().st_uid, home.paste.stat().st_gid) == (st.st_uid, st.st_gid)


# ---------------------------------------------------------------------------
# Security review round 2
# ---------------------------------------------------------------------------

CLOSE_MSG = "Close Hermes (all windows) and rerun `hermes security scrub --apply`. Use `--dry-run` any time."


def _support_dirs_untouched(h: Home) -> bool:
    return not (h.root / "backups").exists() and not (h.root / "secret-scrub").exists()


def _hold_open(path: Path, tmp_path: Path, how: str = "sqlite"):
    """A child process holding ``path`` open until killed."""
    import subprocess
    import sys

    ready = tmp_path / f"ready-{path.name}"
    code = {
        "sqlite": ("import sqlite3,sys,time,pathlib;c=sqlite3.connect(sys.argv[1]);"
                   "c.execute('SELECT count(*) FROM messages').fetchone();"),
        "sqlite-wal": ("import sqlite3,sys,time,pathlib;c=sqlite3.connect(sys.argv[1]);"
                       "assert c.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal';"
                       "c.execute('BEGIN IMMEDIATE');"
                       "c.execute(\"UPDATE messages SET content=content || ' ' WHERE id=1\");"),
    }.get(how, "import sys,time,pathlib;f=open(sys.argv[1],'ab');")
    child = subprocess.Popen([sys.executable, "-c", code + "pathlib.Path(sys.argv[2]).write_text('1');time.sleep(60)",
                              str(path), str(ready)])
    deadline = time.time() + 10
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    return child


# A: --apply refuses while any other process holds state.db / -wal / -shm, or a backend owns the home.

@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("which", ["state.db", "state.db-wal"])
def test_apply_refuses_while_another_process_holds_state_db(home, tmp_path, which):
    target = home.db.parent / which
    child = _hold_open(target if which == "state.db" else home.db, tmp_path,
                       "sqlite" if which == "state.db" else "sqlite-wal")
    try:
        before = _snapshot(home)
        report = run(home, apply=True)
        assert report.exit_code == 2, report.render_text()
        assert report.refused == CLOSE_MSG
        assert CLOSE_MSG in report.render_text()
        assert _snapshot(home) == before
        assert _support_dirs_untouched(home)
        dry = run(home)  # the dry run stays allowed while the app is running
        assert dry.exit_code == 0 and _items(dry, action="would-mask")
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("failure", ["missing", "timeout", "error"])
def test_apply_refuses_when_lsof_cannot_prove_quiet(home, monkeypatch, failure):
    import subprocess

    real_which, real_run = shutil.which, subprocess.run
    if failure == "missing":
        monkeypatch.setattr(scrub.shutil, "which", lambda name, *a, **k: None if name == "lsof"
                            else real_which(name, *a, **k))
    else:
        def fake_run(cmd, *a, **kw):
            if cmd and "lsof" in os.path.basename(str(cmd[0])):
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 5)
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="lsof: status error\n")
            return real_run(cmd, *a, **kw)
        monkeypatch.setattr(scrub.subprocess, "run", fake_run)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 2, report.render_text()
    assert report.refused == CLOSE_MSG
    assert _snapshot(home) == before
    assert _support_dirs_untouched(home)


def test_apply_refuses_while_a_backend_owns_the_home(home):
    before = _snapshot(home)
    report = run(home, apply=True, backend_probe=lambda _root, _profiles: ["gateway running (profile thinkbot)"])
    assert report.exit_code == 2 and report.refused == CLOSE_MSG
    assert _snapshot(home) == before
    assert _support_dirs_untouched(home)
    assert run(home, backend_probe=lambda _r, _p: ["x"]).exit_code == 0  # dry run allowed


def test_backend_probe_finds_nothing_for_an_idle_temp_home(tmp_path, monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [psutil.Process(os.getpid())])
    root = tmp_path / "idle-root"
    (root / "profiles" / "zz-scrub-idle").mkdir(parents=True)
    assert scrub._backend_owners(root, [("default", root),
                                        ("zz-scrub-idle", root / "profiles" / "zz-scrub-idle")]) == []


def test_row_changed_after_verification_is_skipped_and_pending(home, monkeypatch):
    new_secret = "ghp_" + fake(36, 900)
    conn = sqlite3.connect(home.db)
    victim = conn.execute("SELECT min(id) FROM messages WHERE session_id = 's_closed'").fetchone()[0]
    conn.close()
    paste_new = home.paste.read_bytes().replace(b"thanks", b"THANKS")
    stat = home.paste.stat()

    def change(_backup_dir):
        c = sqlite3.connect(home.db)
        c.execute("UPDATE messages SET content = ? WHERE id = ?", (f"changed {new_secret}", victim))
        c.commit()
        c.close()
        home.paste.write_bytes(paste_new)  # same size, same mtime: only the hash can tell
        os.utime(home.paste, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    monkeypatch.setattr(scrub, "_after_backup_verified", change, raising=False)
    report = run(home, apply=True)
    conn = sqlite3.connect(home.db)
    content = conn.execute("SELECT content FROM messages WHERE id = ?", (victim,)).fetchone()[0]
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    conn.close()
    assert new_secret in content  # never masked without a verified backup of that value
    assert _items(report, locator=f"profiles/thinkbot/state.db:messages#{victim}.content",
                  action="changed-during-apply")
    assert f"messages#{victim}.content" in progress["pending"]
    assert progress["complete"] is False
    assert home.paste.read_bytes() == paste_new
    assert _items(report, locator="composer-pastes/pasted_content_1_ab12.txt", action="changed-during-apply")
    assert NEON_PW not in home.attachment.read_text()  # untouched units still masked


# B: every non-empty SDK transcript line must carry both a sessionId and an entrypoint.

@pytest.mark.parametrize("second", [
    {"type": "summary", "message": {"content": "{secret}"}},
    {"type": "user", "sessionId": SDK_ID, "message": {"content": "{secret}"}},
    {"type": "user", "entrypoint": "sdk-py", "message": {"content": "{secret}"}},
])
def test_sdk_transcript_with_an_unattributed_line_is_skipped(home, second):
    secret = "ghp_" + fake(36, 950)
    line2 = json.loads(json.dumps(second).replace("{secret}", secret))
    _write(home.sdk_transcript, "\n".join([
        json.dumps({"type": "user", "entrypoint": "sdk-py", "sessionId": SDK_ID,
                    "message": {"content": f"org token {SDK_SECRET}"}}),
        json.dumps(line2)]) + "\n")
    before = _sha(home.sdk_transcript)
    report = run(home, apply=True)
    assert _sha(home.sdk_transcript) == before
    (item,) = _items(report, target="sdk-transcripts", locator=f"claude:projects/-tmp-demo-project/{SDK_ID}.jsonl")
    assert item.action == "ambiguous-skip"


# C: symlinked support dirs and profile chains are refused.

def test_symlinked_profiles_dir_refuses(home, tmp_path):
    outside = tmp_path / "elsewhere"
    shutil.move(str(home.root / "profiles"), str(outside))
    (home.root / "profiles").symlink_to(outside, target_is_directory=True)
    before = {p: _sha(p) for p in outside.rglob("*") if p.is_file()}
    for apply in (True, False):
        report = run(home, apply=apply)
        assert report.exit_code == 2, report.render_text()
        assert "symlink" in (report.refused or "")
    assert {p: _sha(p) for p in outside.rglob("*") if p.is_file()} == before
    assert _support_dirs_untouched(home)


@pytest.mark.parametrize("link", ["backups", "backups/secret-scrub"])
def test_symlinked_backup_dir_refuses_with_zero_writes(home, tmp_path, link):
    outside = tmp_path / "backup-elsewhere"
    outside.mkdir()
    at = home.root / link
    at.parent.mkdir(parents=True, exist_ok=True)
    at.symlink_to(outside, target_is_directory=True)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 2, report.render_text()
    assert "symlink" in (report.refused or "")
    assert _snapshot(home) == before
    assert list(outside.rglob("*")) == []
    assert not (home.root / "secret-scrub").exists()


def test_symlinked_ledger_dir_refuses(home, tmp_path):
    outside = tmp_path / "ledger-elsewhere"
    outside.mkdir()
    (home.root / "secret-scrub").symlink_to(outside, target_is_directory=True)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 2, report.render_text()
    assert "symlink" in (report.refused or "")
    assert _snapshot(home) == before
    assert list(outside.rglob("*")) == []
    assert not (home.root / "backups").exists()


def test_backup_root_inside_a_scrub_target_refuses(home, tmp_path):
    # a Claude config dir whose projects/ IS the Hermes root puts the backup inside a scrub target
    claude = tmp_path / "claude-wrap"
    claude.mkdir()
    (claude / "projects").symlink_to(home.root, target_is_directory=True)
    before = _snapshot(home)
    report = scrub.run_scrub(root=home.root, profiles=[("thinkbot", home.profile)], apply=True,
                             config=SecretHygieneConfig(), key_provider=KEY, claude_config_dir=claude,
                             holders_fn=lambda _p: [], recent_write_grace_s=0, open_writers_fn=lambda _p: [],
                             backend_probe=lambda _r, _p: [])
    assert report.exit_code == 2, report.render_text()
    assert "backup" in (report.refused or "")
    assert _snapshot(home) == before


# D: known-prefix tokens are masked anywhere in report text, even glued to other characters.

_PREFIXES = ["sk-", "sk-ant-", "ghp_", "gho_", "github_pat_", "xoxb-", "xoxp-", "AKIA", "ASIA", "FlyV1 ",
             "fm2_", "fo1_", "npg_", "AIza", "glpat-"]


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_report_text_masks_glued_known_prefixes(prefix):
    body = fake(12, 990).upper() if prefix in ("AKIA", "ASIA") else fake(12, 990)
    secret = prefix + body
    for text in (f"composer-pastes/abc{secret}xyz.txt", f"doc{secret}.md", f"x/{secret}", f"note:{secret}"):
        out = scrub.redact_report_text(text)
        assert body not in out and body[:8] not in out, (prefix, out)


def test_report_text_masks_pem_header_and_db_url_passwords():
    pw = fake(14, 991)
    for text in (f"composer-pastes/postgresql:/owner:{pw}@ep-x.neon.tech.txt",
                 f"postgresql://owner:{pw}@ep-x.neon.tech/db", f"mysql:app:{pw}@db.internal.txt",
                 f"leak-mongodb+srv:/u:{pw}@c0.example.net.md"):
        assert pw not in scrub.redact_report_text(text), text
    assert "PRIVATE KEY" not in scrub.redact_report_text("keys/-----BEGIN RSA PRIVATE KEY-----MIIEabc.pem")


def test_report_masks_glued_prefix_file_names(home):
    gh = "ghp_" + fake(14, 992)
    ant = "sk-ant-" + fake(14, 993)
    _write(home.root / "composer-pastes" / f"abc{gh}xyz.txt", f"token {'ghp_' + fake(36, 994)}\n")
    _write(home.root / "composer-pastes" / f"doc{ant}.md", f"token {'ghp_' + fake(36, 995)}\n")
    for apply in (False, True):
        report = run(home, apply=apply)
        blob = json.dumps(report.to_json()) + report.render_text()
        for secret in (gh, ant):
            assert secret not in blob and secret[-14:] not in blob
    ledgers = "".join(p.read_text() for p in (home.root / "secret-scrub").glob("*.json"))
    assert gh[-14:] not in ledgers and ant[-14:] not in ledgers


# E: ``complete`` is False while anything is pending.

def test_progress_is_incomplete_while_items_are_pending(home):
    report = run(home, apply=True, open_writers_fn=lambda p: [4242] if Path(p).name == home.paste.name
                 and Path(p).parent.name == "composer-pastes" else [])
    assert report.exit_code == 0, report.render_text()
    conn = sqlite3.connect(home.db)
    progress = json.loads(conn.execute(
        "SELECT value FROM state_meta WHERE key = 'secret_scrub_progress'").fetchone()[0])
    conn.close()
    assert progress["pending"] and progress["complete"] is False  # s_live is deferred
    ledger = json.loads((home.root / "secret-scrub" / "progress.json").read_text())
    assert ledger["pending_files"] and ledger["complete"] is False


def test_cli_accepts_explicit_dry_run():
    args = _cli_args("--dry-run")
    assert args.apply is False and args.dry_run is True


def test_ledger_dir_swapped_for_a_symlink_mid_run_stops_before_any_target_write(home, tmp_path, monkeypatch):
    outside = tmp_path / "ledger-race"
    outside.mkdir()
    monkeypatch.setattr(scrub, "_after_backup_verified",
                        lambda _b: (home.root / "secret-scrub").symlink_to(outside, target_is_directory=True))
    before = _snapshot(home)
    report = run(home, apply=True)
    assert report.exit_code == 1, report.render_text()
    assert any("symlink" in e for e in report.errors)
    assert _snapshot(home) == before
    assert list(outside.rglob("*")) == []


# ---------------------------------------------------------------------------
# Round 3 (a): bytes appended to the ORIGINAL inode between the writer probe and
# ``os.replace`` are carried into the new file (masked) and backed up raw.
# ---------------------------------------------------------------------------

from hermes_cli import security_scrub_backup as bk  # noqa: E402

PASTE_LOC = "composer-pastes/pasted_content_1_ab12.txt"


def _is_paste(path) -> bool:
    return Path(path).name == "pasted_content_1_ab12.txt" and Path(path).parent.name == "composer-pastes"


def _progress_ledger(h: Home) -> dict:
    return json.loads((h.root / "secret-scrub" / "progress.json").read_text())


def test_append_after_writer_probe_is_carried_masked_and_backed_up(home, monkeypatch):
    late_secret = "ghp_" + fake(36, 1200)
    partial_secret = "ghp_" + fake(36, 1201)
    appended = f"late line {late_secret}\npartial {partial_secret}".encode()  # partial trailing line
    original = home.paste.read_bytes()
    hits = []

    def append(path):
        if _is_paste(path) and not hits:
            hits.append(path)
            with open(home.paste, "ab") as fh:  # the path still names the ORIGINAL inode here
                fh.write(appended)

    monkeypatch.setattr(scrub, "_after_writer_probe", append, raising=False)
    report = run(home, apply=True)
    assert hits, "the post-probe seam must run"
    assert report.exit_code == 0, report.render_text()
    body = home.paste.read_text()
    for secret in (NEON_PW, late_secret, partial_secret):
        assert secret not in body
    assert "\nlate line [REDACTED:github-token:" in body
    assert "\npartial [REDACTED:github-token:" in body and body.endswith("]")  # masked as-is, no newline added
    assert _items(report, locator=PASTE_LOC, action="masked")
    backup = Path(report.backup_path)
    tails = sorted(backup.rglob("*.tail-*"))
    assert len(tails) == 1 and tails[0].read_bytes() == appended
    assert tails[0].stat().st_mode & 0o777 == 0o600
    entry = next(f for f in json.loads((backup / "manifest.json").read_text())["files"] if f["locator"] == PASTE_LOC)
    assert [t["offset"] for t in entry["tails"]] == [len(original)]
    assert bk.restore_file_bytes(backup, PASTE_LOC) == original + appended
    assert PASTE_LOC not in _progress_ledger(home)["pending_files"]
    assert not [p for p in home.paste.parent.iterdir() if p.name.endswith(".scrub-tmp")]


def test_append_during_tail_carry_loops_until_the_held_fd_is_stable(home, monkeypatch):
    second_secret = "ghp_" + fake(36, 1202)
    first = b"first late line\n"
    second = f"second late line {second_secret}\n".encode()
    original = home.paste.read_bytes()
    writer: list = []  # a live writer's own append fd on the ORIGINAL inode
    carry_hits = []

    def after_probe(path):
        if _is_paste(path) and not writer:
            writer.append(open(home.paste, "ab", buffering=0))
            writer[0].write(first)

    def after_carry(path):
        if _is_paste(path) and not carry_hits:
            carry_hits.append(path)
            writer[0].write(second)  # its name now points at the new file; the writer does not care

    monkeypatch.setattr(scrub, "_after_writer_probe", after_probe, raising=False)
    monkeypatch.setattr(scrub, "_after_tail_carried", after_carry, raising=False)
    try:
        report = run(home, apply=True)
    finally:
        for fh in writer:
            fh.close()
    assert writer and carry_hits
    assert report.exit_code == 0, report.render_text()
    body = home.paste.read_text()
    assert second_secret not in body
    assert "\nfirst late line\nsecond late line [REDACTED:github-token:" in body and body.endswith("]\n")
    backup = Path(report.backup_path)
    assert len(sorted(backup.rglob("*.tail-*"))) == 2
    assert bk.restore_file_bytes(backup, PASTE_LOC) == original + first + second
    assert PASTE_LOC not in _progress_ledger(home)["pending_files"]


def test_endless_appender_exhausts_bounded_retries_and_marks_the_file_pending(home, monkeypatch):
    original = home.paste.read_bytes()
    writer: list = []
    written: list = []

    def tick(path):
        if not _is_paste(path):
            return
        if not writer:
            writer.append(open(home.paste, "ab", buffering=0))
        line = f"tick {len(written)}\n".encode()
        written.append(line)
        writer[0].write(line)

    monkeypatch.setattr(scrub, "_after_writer_probe", tick, raising=False)
    monkeypatch.setattr(scrub, "_after_tail_carried", tick, raising=False)
    try:
        report = run(home, apply=True)
    finally:
        for fh in writer:
            fh.close()
    assert len(written) == 1 + getattr(scrub, "MAX_TAIL_ROUNDS", -1)
    assert home.paste.read_bytes().endswith(b"".join(written))  # every appended byte carried, none lost
    assert bk.restore_file_bytes(Path(report.backup_path), PASTE_LOC) == original + b"".join(written)
    assert PASTE_LOC in _progress_ledger(home)["pending_files"]
    assert report.exit_code == 1 and any("kept appending" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Round 3 (b): key=value / key:value / key-value shaped path components never leak.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("rel", "value", "kept"), [
    ("password=letmein123.txt", "letmein123", "password=[REDACTED:secret].txt"),
    ("API_KEY-abcdef123456.json", "abcdef123456", "API_KEY-[REDACTED:secret].json"),
    ("dir/token:xyz789abc/file.md", "xyz789abc", "dir/token:[REDACTED:secret]/file.md"),
])
def test_report_masks_key_value_shaped_path_components(home, rel, value, kept):
    _write(home.root / "composer-pastes" / rel, f"token {'ghp_' + fake(36, 1300)}\n")
    for apply in (False, True):
        report = run(home, apply=apply)
        text = report.render_text()
        blob = json.dumps(report.to_json()) + text
        assert value not in blob, (apply, rel)
        assert kept in blob
        assert re.search(r"Totals \((?:would mask|masked)\): .*github-token=\d", text)  # kinds are not masked
    ledgers = "".join(p.read_text() for p in (home.root / "secret-scrub").glob("*.json"))
    assert value not in ledgers


def test_report_text_key_value_components_unit():
    r = scrub.redact_report_text
    assert r("composer-pastes/password=letmein123.txt") == "composer-pastes/password=[REDACTED:secret].txt"
    assert r("a/my_secret:hunter2/b.md") == "a/my_secret:[REDACTED:secret]/b.md"
    assert r("x/Bearer-abc.def.log") == "x/Bearer-[REDACTED:secret].log"
    assert "hunter22" not in r("profiles/p/private_key=hunter22.pem: rewrite failed (OSError)")
    for plain in ("profiles/thinkbot/sessions/s_closed.jsonl", "oauth-callback.txt", "secret-scrub/progress.json",
                  "tokens.txt", "authors.md"):
        assert r(plain) == plain
    rep = scrub.ScrubReport(mode="dry-run", root="/tmp/r")
    rep.error("composer-pastes/session:abcd1234zz.txt: rewrite failed (OSError)")
    assert "abcd1234zz" not in rep.errors[0]


# ---------------------------------------------------------------------------
# Round 3 (c): the SQLite snapshot is made in a PRIVATE temp dir and streamed into the
# backup through the walked backup dir fd; nothing writes into the backup tree by path.
# ---------------------------------------------------------------------------

def test_db_snapshot_is_private_then_streamed_byte_exact(home, monkeypatch):
    import hermes_cli.backup_sqlite as bsql

    real = bsql._safe_copy_db
    seen = []

    def spy(src, dst, **kw):
        dst = Path(dst)
        seen.append((dst, _stat_mode(dst.parent)))
        ok = real(src, dst, **kw)
        seen[-1] += (_stat_mode(dst),)
        return ok

    monkeypatch.setattr(bsql, "_safe_copy_db", spy)
    report = run(home, apply=True)
    assert report.exit_code == 0, report.render_text()
    assert len(seen) == 1
    dst, dir_mode, file_mode = seen[0]
    for target_root in (home.root, home.claude):
        assert not str(dst.resolve()).startswith(str(target_root.resolve()) + os.sep)
    assert dir_mode == 0o700 and file_mode == 0o600
    assert not dst.exists() and not dst.parent.exists()  # private snapshot + dir removed
    backup = Path(report.backup_path)
    copy = backup / "db" / "profiles" / "thinkbot" / "state.db"
    entry = json.loads((backup / "manifest.json").read_text())["dbs"][0]
    assert entry["sha256"] == _sha(copy) and entry["size"] == copy.stat().st_size
    assert copy.stat().st_mode & 0o777 == 0o600
    sidecars = [p for p in backup.rglob("*") if p.name.endswith(("-wal", "-shm", "-journal"))]
    assert sidecars == []
    conn = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert NEON_PW in "".join(str(r[0]) for r in conn.execute("SELECT content FROM messages"))
    finally:
        conn.close()
    assert [p for p in backup.rglob("*") if p.name.endswith(("-wal", "-shm", "-journal"))] == []


def _stat_mode(p: Path) -> int:
    return os.stat(p).st_mode & 0o777


_AUDIT: dict = {"on": False, "events": []}


def _audit_hook(event, args):
    if _AUDIT["on"] and event in ("open", "sqlite3.connect"):
        _AUDIT["events"].append((event, args))


def test_no_pathname_write_into_the_backup_tree(home):
    import sys

    if not _AUDIT.get("installed"):
        sys.addaudithook(_audit_hook)
        _AUDIT["installed"] = True
    tree = str((home.root / "backups").resolve())
    _AUDIT["events"] = []
    _AUDIT["on"] = True
    try:
        report = run(home, apply=True)
    finally:
        _AUDIT["on"] = False
    assert report.exit_code == 0, report.render_text()
    bad = []
    for event, args in _AUDIT["events"]:
        target = args[0]
        if isinstance(target, int) or target is None:
            continue
        text = os.fsdecode(target) if isinstance(target, (bytes, os.PathLike)) else str(target)
        if text.startswith("file:"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(text)
            where, ro = unquote(parsed.path), "mode=ro" in parsed.query
        else:
            where, ro = text, False
        if not os.path.isabs(where):
            continue  # dir_fd-relative
        try:
            where = os.path.realpath(where)
        except OSError:
            pass
        if not where.startswith(tree + os.sep):
            continue
        if event == "sqlite3.connect":
            if not ro:
                bad.append((event, text))
        else:
            mode, flags = args[1], args[2]
            writes = (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)) \
                or (isinstance(mode, str) and any(c in mode for c in "wax+"))
            if writes:
                bad.append((event, text))
    assert bad == []
    src = Path(bk.__file__).read_text()
    assert "_path_is_entry_at(dst" not in src and "dir_path.joinpath" not in src


def test_backup_parent_swapped_for_a_symlink_before_db_stream_fails_closed(home, tmp_path, monkeypatch):
    outside = tmp_path / "db-backup-elsewhere"
    outside.mkdir()
    swapped = []

    def swap(_dir_fd, rel):
        (backup_dir,) = list((home.root / "backups" / "secret-scrub").iterdir())
        parent = backup_dir.joinpath(*rel.split("/")[:-1])
        parent.rename(parent.with_name(parent.name + "-moved"))
        parent.symlink_to(outside, target_is_directory=True)
        swapped.append(parent)

    monkeypatch.setattr(bk, "_before_db_stream", swap, raising=False)
    before = _snapshot(home)
    report = run(home, apply=True)
    assert swapped, "the pre-stream seam must run"
    assert list(outside.rglob("*")) == []  # zero bytes outside the home
    assert report.exit_code == 2, report.render_text()
    assert "symlink" in (report.refused or "")
    assert _snapshot(home) == before
