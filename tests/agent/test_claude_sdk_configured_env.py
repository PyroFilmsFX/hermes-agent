"""`agent.claude_agent_sdk.env` — operator knobs for the spawned CLI.

The Claude Code CLI reads operational knobs from its environment that the SDK
exposes no typed option for. Measured against claude-agent-sdk 0.2.120 via
get_context_usage(): CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000 moves maxTokens to
300000 and the autocompact threshold to 267000, while
CLAUDE_CODE_MAX_CONTEXT_TOKENS and CLAUDE_AUTOCOMPACT_PCT_OVERRIDE are inert.
Three of four plausible knobs doing nothing is why this is a generic config
surface rather than a named option per knob.

The security edge: this env is applied AFTER the metered-billing scrub, so
without a guard `env: {ANTHROPIC_API_KEY: ...}` would overwrite the scrub's ""
and silently re-arm metered billing behind `allow_metered_key: false`.
"""

from __future__ import annotations

import pytest

from agent.transports import claude_agent_sdk_session as M


@pytest.fixture
def env_config(monkeypatch):
    """Drive _configured_sdk_env / the metered flag / the scrub from the test."""

    def _apply(env=None, metered_allowed=False, scrubbed=None):
        monkeypatch.setattr(
            M, "_provider_config", lambda: {"env": env} if env is not None else {}
        )
        monkeypatch.setattr(
            M, "_provider_flag", lambda name: metered_allowed
        )
        monkeypatch.setattr(M, "_scrubbed_sdk_env", lambda: dict(scrubbed or {}))

    return _apply


def test_values_are_stringified(env_config):
    """YAML gives ints for numeric knobs; the CLI env must be all strings."""
    env_config(env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": 300000})

    assert M._configured_sdk_env() == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}


def test_absent_or_non_mapping_env_yields_empty(env_config):
    for bad in (None, "not-a-map", ["a"], 7):
        env_config(env=bad)
        assert M._configured_sdk_env() == {}


def test_none_values_are_skipped(env_config):
    """`KEY:` with no value parses as None; str(None) would set the literal
    string "None" in the CLI's environment, so it is dropped instead."""
    env_config(env={"A": None, "B": "keep"})

    assert M._configured_sdk_env() == {"B": "keep"}


def test_configured_env_reaches_the_overrides(env_config):
    env_config(env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"})

    assert M._sdk_env_overrides()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"


def test_scrub_is_preserved_alongside_configured_env(env_config):
    """A knob must not displace the metered scrub that ships with it."""
    env_config(
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"},
        scrubbed={"ANTHROPIC_API_KEY": ""},
    )

    overrides = M._sdk_env_overrides()

    assert overrides["ANTHROPIC_API_KEY"] == ""
    assert overrides["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"


def test_config_env_cannot_resurrect_a_scrubbed_credential(env_config, caplog):
    """The whole point of the guard: config must not defeat allow_metered_key: false."""
    env_config(
        env={"ANTHROPIC_API_KEY": "sk-ant-metered"},
        metered_allowed=False,
        scrubbed={"ANTHROPIC_API_KEY": ""},
    )

    with caplog.at_level("WARNING", logger=M.logger.name):
        overrides = M._sdk_env_overrides()

    assert overrides["ANTHROPIC_API_KEY"] == ""
    assert "metered billing vector" in caplog.text


@pytest.mark.parametrize("key", M._METERED_ENV_DENYLIST)
def test_every_denylisted_key_is_guarded(env_config, key):
    env_config(env={key: "x"}, metered_allowed=False, scrubbed={key: ""})

    assert M._sdk_env_overrides()[key] == ""


def test_metered_opt_in_permits_the_key(env_config):
    """With the explicit opt-in the scrub is off and the operator's value stands."""
    env_config(env={"ANTHROPIC_API_KEY": "sk-ant-metered"}, metered_allowed=True)

    assert M._sdk_env_overrides()["ANTHROPIC_API_KEY"] == "sk-ant-metered"


def test_subscription_shaped_anthropic_token_is_preserved(env_config, monkeypatch):
    """ANTHROPIC_TOKEN is shared by metered and setup-token flows in Hermes."""
    monkeypatch.setattr(M, "_is_subscription_oauth_token", lambda value: True)
    env_config(env={"ANTHROPIC_TOKEN": "oauth-token"}, metered_allowed=False)

    assert M._sdk_env_overrides()["ANTHROPIC_TOKEN"] == "oauth-token"


def test_metered_anthropic_token_is_scrubbed_from_parent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TOKEN", "metered-token")
    monkeypatch.setattr(M, "_is_subscription_oauth_token", lambda value: False)

    assert M._scrubbed_sdk_env()["ANTHROPIC_TOKEN"] == ""


# ── Interpreter-path scrub ────────────────────────────────────────────────────
# The desktop backend inherits PYTHONPATH=<repo>:<venv>/lib/python3.11/site-packages from
# Electron; the SDK merges os.environ into the CLI child, which hands it to every plugin MCP it
# spawns, and a plugin's `uv run --python >=3.12` server then imports 3.11-built pydantic_core
# and dies (conductor tb-workers "Connection closed", 2026-09-11).


def test_pythonpath_and_pythonhome_are_blanked_when_present(env_config, monkeypatch):
    env_config(env=None)
    monkeypatch.setenv("PYTHONPATH", "/repo:/repo/.venv/lib/python3.11/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/repo/.venv")

    overrides = M._sdk_env_overrides()

    assert overrides["PYTHONPATH"] == ""
    assert overrides["PYTHONHOME"] == ""


def test_absent_interpreter_vars_are_not_introduced(env_config, monkeypatch):
    """Only PRESENT keys are overridden — an empty var the child never had is a new fact."""
    env_config(env=None)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)

    overrides = M._sdk_env_overrides()

    assert "PYTHONPATH" not in overrides
    assert "PYTHONHOME" not in overrides


def test_operator_env_may_still_set_pythonpath_deliberately(env_config, monkeypatch):
    """The scrub is a default, not a security boundary: `env: {PYTHONPATH: ...}` is a knob and wins."""
    env_config(env={"PYTHONPATH": "/deliberate"})
    monkeypatch.setenv("PYTHONPATH", "/repo/.venv/lib/python3.11/site-packages")

    assert M._sdk_env_overrides()["PYTHONPATH"] == "/deliberate"


def test_interpreter_scrub_is_independent_of_the_metered_opt_in(env_config, monkeypatch):
    """allow_metered_key disables the BILLING scrub only; a metered opt-in still must not
    hand plugin MCPs a wrong-ABI import path."""
    env_config(env=None, metered_allowed=True)
    monkeypatch.setenv("PYTHONPATH", "/repo/.venv/lib/python3.11/site-packages")

    assert M._sdk_env_overrides()["PYTHONPATH"] == ""
