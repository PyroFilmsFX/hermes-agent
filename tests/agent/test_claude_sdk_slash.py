"""agent.claude_sdk_slash — plugin skills the claude-agent-sdk lane dispatches natively.

Behaviour contracts, no snapshots: what the resolver returns is a function of the
plugin roots on disk, the live init list, and the provider — never a frozen name.
"""

from __future__ import annotations

import json

import pytest

from agent import claude_sdk_slash as M


def _plugin(tmp_path, plugin_name="conductor", skills=()):
    """A Claude Code plugin root: .claude-plugin/plugin.json + skills/<dir>/SKILL.md."""
    root = tmp_path / plugin_name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin_name}))
    for dirname, frontmatter in skills:
        skill_dir = root / "skills" / dirname
        skill_dir.mkdir(parents=True)
        body = "---\n" + "".join(f"{k}: {v}\n" for k, v in frontmatter.items()) + "---\n\nDo the thing.\n"
        (skill_dir / "SKILL.md").write_text(body)
    return root


@pytest.fixture
def isolated(monkeypatch):
    """No ambient config: provider and plugin roots come only from the test."""
    monkeypatch.setattr(M, "active_provider", lambda: "")
    monkeypatch.setattr(M, "configured_plugin_roots", lambda: [])


class TestProviderPredicate:
    @pytest.mark.parametrize("value", ["claude-agent-sdk", "claude_agent_sdk", " Claude-Agent-SDK "])
    def test_accepts_every_spelling(self, value):
        assert M.is_claude_sdk_provider(value)

    @pytest.mark.parametrize("value", ["anthropic", "codex", "", None])
    def test_rejects_other_lanes(self, value):
        assert not M.is_claude_sdk_provider(value)


class TestPluginSkillNames:
    def test_bare_and_namespaced_names_from_frontmatter(self, tmp_path):
        root = _plugin(tmp_path, skills=[("tb-ship", {"name": "tb-ship", "description": '"Ship it"'})])
        names = M.plugin_skill_slash_names([str(root)])
        assert names == {"tb-ship", "conductor:tb-ship"}

    def test_directory_name_when_frontmatter_has_no_name(self, tmp_path):
        root = _plugin(tmp_path, skills=[("tb-verify", {"description": "verify"})])
        assert "tb-verify" in M.plugin_skill_slash_names([str(root)])

    def test_user_invocable_false_is_excluded(self, tmp_path):
        root = _plugin(tmp_path, skills=[
            ("hidden", {"name": "hidden", "user-invocable": "false"}),
            ("shown", {"name": "shown"}),
        ])
        names = M.plugin_skill_slash_names([str(root)])
        assert "shown" in names and "hidden" not in names

    def test_plugin_name_falls_back_to_directory(self, tmp_path):
        root = tmp_path / "my-plugin"
        (root / "skills" / "go").mkdir(parents=True)
        (root / "skills" / "go" / "SKILL.md").write_text("---\nname: go\n---\n")
        assert "my-plugin:go" in M.plugin_skill_slash_names([str(root)])

    def test_missing_roots_and_skill_dirs_are_skipped(self, tmp_path):
        bare = tmp_path / "no-skills"
        bare.mkdir()
        assert M.plugin_skill_slash_names([str(bare), str(tmp_path / "absent")]) == set()

    def test_nested_frontmatter_values_do_not_break_parsing(self, tmp_path):
        root = _plugin(tmp_path, skills=[("tb-plan", {
            "name": "tb-plan",
            "when_to_use": ">",
            "  The optional gate": "before build",  # indented continuation line
        })])
        assert "tb-plan" in M.plugin_skill_slash_names([str(root)])


class TestResolve:
    @pytest.fixture
    def roots(self, tmp_path, monkeypatch, isolated):
        root = _plugin(tmp_path, skills=[("tb-ship", {"name": "tb-ship"})])
        monkeypatch.setattr(M, "configured_plugin_roots", lambda: [str(root)])
        return root

    def test_forwards_plugin_skill_with_args_on_sdk_lane(self, roots):
        out = M.resolve_sdk_slash("/tb-ship  --dry-run  now", provider="claude-agent-sdk")
        assert out == "/tb-ship --dry-run  now"

    def test_other_provider_never_forwards(self, roots):
        assert M.resolve_sdk_slash("/tb-ship", provider="anthropic") is None

    def test_provider_defaults_to_active_config(self, roots, monkeypatch):
        monkeypatch.setattr(M, "active_provider", lambda: "claude-agent-sdk")
        assert M.resolve_sdk_slash("/tb-ship") == "/tb-ship"
        monkeypatch.setattr(M, "active_provider", lambda: "openrouter")
        assert M.resolve_sdk_slash("/tb-ship") is None

    def test_live_init_list_counts_without_any_plugin_root(self, isolated):
        out = M.resolve_sdk_slash("/conductor:tb-ship go", provider="claude-agent-sdk",
                                  live_names=["compact", "conductor:tb-ship"])
        assert out == "/conductor:tb-ship go"

    def test_unknown_name_is_none(self, isolated):
        assert M.resolve_sdk_slash("/definitely-not", provider="claude-agent-sdk",
                                   live_names=["tb-ship"]) is None

    def test_no_known_names_is_none(self, isolated):
        assert M.resolve_sdk_slash("/tb-ship", provider="claude-agent-sdk") is None

    @pytest.mark.parametrize("typed", ["/TB-Ship", "/tb_ship"])
    def test_case_and_underscore_normalise_to_the_canonical_name(self, typed, isolated):
        out = M.resolve_sdk_slash(typed, provider="claude-agent-sdk", live_names=["tb-ship"])
        assert out == "/tb-ship"

    @pytest.mark.parametrize("text", ["", "tb-ship", "/", "/ ", "   "])
    def test_non_slash_input_is_none(self, text, isolated):
        assert M.resolve_sdk_slash(text, provider="claude-agent-sdk", live_names=["tb-ship"]) is None


class TestCompletionEntries:
    """plugin_skill_commands / merged_skill_commands feed the shared SlashCommandCompleter
    (desktop popover, TUI, CLI) and commands.catalog — the "No matches for /tb" half."""

    def test_entries_are_shaped_like_hermes_skill_commands(self, tmp_path):
        root = _plugin(tmp_path, skills=[("tb-ship", {"name": "tb-ship", "description": '"Ship it"'})])
        cmds = M.plugin_skill_commands([str(root)])
        entry = cmds["/tb-ship"]
        assert entry["name"] == "tb-ship"
        assert entry["description"] == "Ship it"
        assert entry["source"] == "claude-plugin"
        assert entry["plugin"] == "conductor"
        assert entry["skill_dir"].endswith("skills/tb-ship")
        assert "/conductor:tb-ship" not in cmds  # listed once in the popover

    def test_description_falls_back_to_the_plugin_name(self, tmp_path):
        root = _plugin(tmp_path, skills=[("tb-x", {"name": "tb-x"})])
        assert M.plugin_skill_commands([str(root)])["/tb-x"]["description"] == "conductor plugin skill"

    def test_merged_adds_plugin_skills_on_the_sdk_lane_only(self, tmp_path, monkeypatch, isolated):
        root = _plugin(tmp_path, skills=[("tb-ship", {"name": "tb-ship"})])
        monkeypatch.setattr(M, "configured_plugin_roots", lambda: [str(root)])
        base = {"/local-skill": {"name": "local-skill", "description": "mine"}}
        on_lane = M.merged_skill_commands(base, provider="claude-agent-sdk")
        assert set(on_lane) == {"/local-skill", "/tb-ship"}
        assert on_lane["/local-skill"] is base["/local-skill"]
        off_lane = M.merged_skill_commands(base, provider="anthropic")
        assert off_lane == base and off_lane is not base

    def test_hermes_wins_every_collision(self, tmp_path, monkeypatch, isolated):
        """An existing HERMES_HOME skill and a registry command (/plan) are never shadowed."""
        root = _plugin(tmp_path, skills=[
            ("tb-ship", {"name": "tb-ship", "description": "plugin copy"}),
            ("plan", {"name": "plan", "description": "plugin plan"}),
        ])
        monkeypatch.setattr(M, "configured_plugin_roots", lambda: [str(root)])
        base = {"/tb-ship": {"name": "tb-ship", "description": "hermes copy"}}
        merged = M.merged_skill_commands(base, provider="claude-agent-sdk")
        assert merged["/tb-ship"]["description"] == "hermes copy"
        assert "/plan" not in merged  # hermes_cli.commands owns /plan

    def test_sdk_slash_names_is_empty_off_lane(self, tmp_path, monkeypatch, isolated):
        root = _plugin(tmp_path, skills=[("tb-ship", {"name": "tb-ship"})])
        monkeypatch.setattr(M, "configured_plugin_roots", lambda: [str(root)])
        assert M.sdk_slash_names("anthropic") == set()
        assert M.sdk_slash_names("claude-agent-sdk") == {"tb-ship", "conductor:tb-ship"}
