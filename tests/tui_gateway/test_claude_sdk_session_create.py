"""Tests for claude-agent-sdk session.create and model resolution contracts."""

import yaml
from hermes_state import SessionDB
from tui_gateway import server


def _quiet_create(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)


def test_session_create_preserves_configured_provider_when_catalog_serves_model(monkeypatch, tmp_path):
    """Contract 1: A session.create override naming a model without provider must keep the
    configured provider when that provider's catalog serves the model (claude-agent-sdk
    delegates to anthropic via _PROVIDER_CATALOG_DELEGATES)."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    cfg_file = hermes_home / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk"}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(server, "_hermes_home", hermes_home)
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None

    db = SessionDB(db_path=tmp_path / "state.db")
    _quiet_create(monkeypatch, db)

    resp = server.handle_request({
        "id": "create-1",
        "method": "session.create",
        "params": {"model": "claude-opus-4-6", "cols": 96, "source": "desktop"},
    })
    assert "result" in resp, resp
    result = resp["result"]
    sid = result["session_id"]
    session_rec = server._sessions[sid]

    # Stored model override keeps configured provider
    assert session_rec["model_override"] == {"model": "claude-opus-4-6", "provider": "claude-agent-sdk"}
    # Info payload reflects both model and provider
    assert result["info"]["model"] == "claude-opus-4-6"
    assert result["info"]["provider"] == "claude-agent-sdk"


def test_resolve_model_reads_model_and_falls_back_to_provider_default(monkeypatch, tmp_path):
    """Contract 2: _resolve_model reads `model` from dict block, falls back to `default` (legacy),
    or default model for configured provider via get_default_model_for_provider."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    cfg_file = hermes_home / "config.yaml"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(server, "_hermes_home", hermes_home)

    # Case A: provider only (no model.model) -> resolves provider default (claude-fable-5-1)
    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    assert server._resolve_model() == "claude-fable-5-1"

    # Case B: model key specified
    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk", "model": "claude-3-5-haiku-20241022"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    assert server._resolve_model() == "claude-3-5-haiku-20241022"

    # Case C: legacy default key fallback
    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk", "default": "claude-legacy-default"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    assert server._resolve_model() == "claude-legacy-default"


def test_session_create_info_always_includes_provider(monkeypatch, tmp_path):
    """Contract 3: session.create info payload must always include provider (configured provider
    when no override)."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    cfg_file = hermes_home / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk"}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(server, "_hermes_home", hermes_home)
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None

    db = SessionDB(db_path=tmp_path / "state.db")
    _quiet_create(monkeypatch, db)

    # Plain session create with no overrides
    resp = server.handle_request({
        "id": "create-plain",
        "method": "session.create",
        "params": {"cols": 96, "source": "desktop"},
    })
    assert "result" in resp, resp
    info = resp["result"]["info"]
    assert "provider" in info
    assert info["provider"] == "claude-agent-sdk"
    assert info["model"] == "claude-fable-5-1"


def test_non_sdk_model_resolution_keeps_legacy_precedence(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)

    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "anthropic"}})
    assert server._resolve_model() == ""

    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"model": {"provider": "anthropic", "default": "legacy-default", "model": "new-key"}},
    )
    assert server._resolve_model() == "legacy-default"
    assert server._config_model_target() == ("legacy-default", "anthropic")


def test_config_model_target_sdk_default_only_keeps_default(monkeypatch, tmp_path):
    """A4: an SDK config with only the default key set must surface that model to config sync
    (model_switch), exactly as _resolve_model does; otherwise default changes never reach
    unpinned live sessions. Non-SDK providers keep reading the default key only."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    cfg_file = hermes_home / "config.yaml"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(server, "_hermes_home", hermes_home)

    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk", "default": "claude-opus-5"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    assert server._config_model_target() == ("claude-opus-5", "claude-agent-sdk")

    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "claude-agent-sdk", "default": "claude-opus-5", "model": "claude-fable-5-1"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    # Upstream canonicalization: default > model (hermes_cli/config.py _normalize_root_model_keys).
    assert server._config_model_target() == ("claude-opus-5", "claude-agent-sdk")

    cfg_file.write_text(yaml.safe_dump({"model": {"provider": "anthropic", "default": "claude-opus-4-6", "model": "ignored-for-non-sdk"}}))
    server._cfg_cache = server._cfg_mtime = server._cfg_sig = server._cfg_path = None
    assert server._config_model_target() == ("claude-opus-4-6", "anthropic")

