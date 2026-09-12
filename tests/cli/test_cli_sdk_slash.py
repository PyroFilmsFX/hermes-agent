"""HermesCLI.process_command on the claude-agent-sdk lane: a Claude Code plugin skill that none
of Hermes' registries know (/tb-ship) is seeded as the next turn's prompt for the spawned CLI to
expand, instead of printing "Unknown command"."""

from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _make_cli(provider="claude-agent-sdk"):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {}
    cli_obj.console = MagicMock()
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj.session_id = None
    cli_obj._pending_input = MagicMock()
    cli_obj._pending_agent_seed = None
    cli_obj.provider = provider
    return cli_obj


def _printed(mock_cprint):
    return " ".join(str(c) for c in mock_cprint.call_args_list)


def test_sdk_plugin_skill_is_seeded_as_the_next_turn():
    cli_obj = _make_cli()
    with patch("agent.claude_sdk_slash.plugin_skill_slash_names", return_value={"tb-ship", "conductor:tb-ship"}), \
            patch("cli._cprint") as mock_cprint:
        assert cli_obj.process_command("/tb-ship --dry-run") is True

    assert cli_obj._pending_agent_seed == "/tb-ship --dry-run"
    assert "Unknown command" not in _printed(mock_cprint)
    assert "/tb-ship" in _printed(mock_cprint)


def test_other_provider_keeps_the_unknown_command_notice():
    cli_obj = _make_cli(provider="anthropic")
    with patch("agent.claude_sdk_slash.plugin_skill_slash_names", return_value={"tb-ship"}), \
            patch("cli._cprint") as mock_cprint:
        cli_obj.process_command("/tb-ship")

    assert cli_obj._pending_agent_seed is None
    assert "Unknown command" in _printed(mock_cprint)


def test_live_init_list_from_the_agent_session_counts():
    cli_obj = _make_cli()
    cli_obj.agent = MagicMock()
    cli_obj.agent._claude_sdk_session.slash_commands = ["conductor:tb-verify"]
    with patch("agent.claude_sdk_slash.plugin_skill_slash_names", return_value=set()), \
            patch("cli._cprint"):
        cli_obj.process_command("/conductor:tb-verify")

    assert cli_obj._pending_agent_seed == "/conductor:tb-verify"


def test_genuinely_unknown_name_is_still_unknown_on_the_sdk_lane():
    cli_obj = _make_cli()
    with patch("agent.claude_sdk_slash.plugin_skill_slash_names", return_value={"tb-ship"}), \
            patch("cli._cprint") as mock_cprint:
        cli_obj.process_command("/definitely-not-a-thing")

    assert cli_obj._pending_agent_seed is None
    assert "Unknown command" in _printed(mock_cprint)


def test_unique_prefix_expands_to_the_sdk_plugin_skill():
    """`/tb-sh` → `/tb-ship` through the same prefix expansion built-ins and skills get."""
    cli_obj = _make_cli()
    with patch("agent.claude_sdk_slash.plugin_skill_slash_names", return_value={"tb-ship", "conductor:tb-ship"}), \
            patch("cli._cprint") as mock_cprint:
        cli_obj.process_command("/tb-sh now")

    assert cli_obj._pending_agent_seed == "/tb-ship now"
    assert "Unknown command" not in _printed(mock_cprint)
