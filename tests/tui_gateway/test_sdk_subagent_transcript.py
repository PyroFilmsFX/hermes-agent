"""The SDK child transcript that the subagent panel reads, and the two plugin hooks around it."""

from __future__ import annotations

import json

import pytest

from tui_gateway import sdk_subagent_transcript as transcripts

AGENT_ID = "agent-7f3c"
PARENT_SESSION = "7c1d0f42-1111-2222-3333-444455556666"


def _write_transcript(config_dir, cwd, entries, *, name=f"agent-{AGENT_ID}.jsonl"):
    slug = str(cwd).replace("/", "-").replace(".", "-")
    directory = config_dir / "projects" / slug / PARENT_SESSION / "subagents"
    directory.mkdir(parents=True)
    path = directory / name
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _meta(**overrides):
    return {"sdk_agent_id": AGENT_ID, "sdk_parent_session_id": PARENT_SESSION, **overrides}


def test_a_running_child_reads_its_own_transcript(config_dir, tmp_path):
    cwd = tmp_path / "project"
    _write_transcript(config_dir, cwd, [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "Reading the config."}]}},
        {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "exit 0"}]}]}},
    ])

    tail = transcripts.read_tail(_meta(), str(cwd))

    assert tail["available"] is True
    assert tail["state"] == "ready"
    assert "Reading the config." in tail["text"]
    assert "[tool] Bash" in tail["text"]
    assert "[result] exit 0" in tail["text"]
    assert tail["truncated"] is False


def test_a_child_that_has_not_written_yet_is_waiting_not_failed(config_dir, tmp_path):
    tail = transcripts.read_tail(_meta(), str(tmp_path / "project"))

    assert tail["available"] is False
    assert tail["state"] == "waiting", "no file yet must not look like a failed read"


def test_a_row_without_the_join_ids_is_not_searched_for(config_dir, tmp_path):
    assert transcripts.transcript_path({}, str(tmp_path)) is None
    assert transcripts.transcript_path({"sdk_agent_id": AGENT_ID}, str(tmp_path)) is None


def test_the_transcript_is_found_when_the_session_moved_directories(config_dir, tmp_path):
    """The parent-session directory is the durable part of the path; cwd can be renamed."""
    _write_transcript(config_dir, tmp_path / "original-name", [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "still here"}]}}])

    tail = transcripts.read_tail(_meta(), str(tmp_path / "renamed-since"))

    assert tail["available"] is True
    assert "still here" in tail["text"]


def test_a_long_transcript_reports_that_it_was_clipped(config_dir, tmp_path):
    cwd = tmp_path / "project"
    _write_transcript(config_dir, cwd, [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}}
        for _ in range(40)
    ])

    tail = transcripts.read_tail(_meta(), str(cwd), limit=2048)

    assert tail["available"] is True
    assert len(tail["text"]) <= 2048
    assert tail["truncated"] is True


def test_garbage_lines_do_not_break_the_read(config_dir, tmp_path):
    cwd = tmp_path / "project"
    path = _write_transcript(config_dir, cwd, [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "good line"}]}}])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{not json at all\n")

    tail = transcripts.read_tail(_meta(), str(cwd))

    assert tail["available"] is True
    assert "good line" in tail["text"]
