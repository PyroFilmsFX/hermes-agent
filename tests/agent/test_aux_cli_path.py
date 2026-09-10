"""Auxiliary one-shots must spawn the same Claude Code binary as the persistent lane.

Seen 2026-09-10 on the desktop: 35 `Using bundled Claude Code CLI` spawns in one morning for
approval screening — each a full node process — while chat turns ran on the pinned
~/.local/bin/claude. (cntrl carry)"""
import agent.claude_sdk_aux_client as aux
import agent.transports.claude_agent_sdk_session as sess


def test_aux_options_carry_the_pinned_cli(monkeypatch):
    monkeypatch.setattr(sess, "_configured_cli_path", lambda: "/opt/claude")
    fields = aux._build_aux_option_fields("claude-fable-5-1")
    assert fields["cli_path"] == "/opt/claude"
    assert fields["max_turns"] == 1 and fields["setting_sources"] == []


def test_aux_options_omit_cli_path_when_unpinned(monkeypatch):
    monkeypatch.setattr(sess, "_configured_cli_path", lambda: "")
    assert "cli_path" not in aux._build_aux_option_fields("m")


def test_aux_options_survive_a_broken_reader(monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(sess, "_configured_cli_path", boom)
    fields = aux._build_aux_option_fields("m")
    assert "cli_path" not in fields and fields["model"] == "m"
