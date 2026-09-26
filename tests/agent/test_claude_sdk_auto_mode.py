"""agent.claude_agent_sdk.auto_mode — Claude Code auto-mode classifier rules
handed to the spawned CLI through the SDK ``settings`` option (``--settings``,
the CLI's "flag settings" layer) while ``setting_sources`` stays ``[]``.

Only the ``autoMode`` object may cross: nothing else from ``~/.claude`` (no
permissions, hooks, plugins, env, model or statusLine) is allowed to leak into
a Hermes session.
"""

import json
import logging

import pytest

from tests.agent.claude_sdk_fakes import (
    ResultMessage,
    _make_session,
    isolate_provider_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(monkeypatch):
    yield from isolate_provider_config(monkeypatch)


_USER_AUTO_MODE = {
    "environment": ["Trusted repo: github.com/example/*"],
    "soft_deny": ["Force-pushing to main"],
    "allow": ["Running the project's test suite"],
}


def _set_provider(monkeypatch, block):
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg,
        "load_config_readonly",
        lambda *a, **k: {"agent": {"claude_agent_sdk": block}},
        raising=False,
    )


def _write_user_settings(home, payload):
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "settings.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _full_user_settings():
    # Everything a real operator settings.json carries — only autoMode may
    # survive the trip into the session options.
    return {
        "autoMode": _USER_AUTO_MODE,
        "permissions": {"allow": ["Bash(rm:*)"], "defaultMode": "bypassPermissions"},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "evil"}]}]},
        "env": {"ANTHROPIC_API_KEY": "sk-leak"},
        "enabledPlugins": {"conductor@thinkbot": True},
        "plugins": ["x"],
        "model": "claude-haiku-leak",
        "statusLine": {"type": "command", "command": "leak"},
    }


def _fields(session):
    try:
        return session.build_option_fields()
    finally:
        session.close()


def _settings_obj(fields):
    raw = fields["settings"]
    # The SDK forwards the string verbatim to --settings; the CLI treats a
    # value that starts with "{" and ends with "}" as inline JSON.
    assert isinstance(raw, str)
    assert raw.startswith("{") and raw.endswith("}")
    return json.loads(raw)


class TestInheritUser:
    def test_only_auto_mode_crosses(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})

        session, _ = _make_session(script=[ResultMessage(result="ok")])
        fields = _fields(session)

        assert _settings_obj(fields) == {"autoMode": _USER_AUTO_MODE}
        raw = fields["settings"]
        for leaked in ("permissions", "hooks", "sk-leak", "enabledPlugins",
                       "claude-haiku-leak", "statusLine", "bypassPermissions"):
            assert leaked not in raw
        # No --settings smuggled through extra_args either.
        assert "settings" not in (fields.get("extra_args") or {})
        # The session env is Hermes-built, never the settings.json env block.
        assert (fields.get("env") or {}).get("ANTHROPIC_API_KEY") != "sk-leak"

    def test_reaches_the_sdk_client_options(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})

        session, holder = _make_session(script=[ResultMessage(result="ok")])
        try:
            session.run_turn("ping")
        finally:
            session.close()
        options = holder["client"].options
        assert json.loads(options["settings"]) == {"autoMode": _USER_AUTO_MODE}
        assert options["setting_sources"] == []

    def test_value_is_case_and_space_tolerant(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": "  Inherit_User "})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _settings_obj(_fields(session)) == {"autoMode": _USER_AUTO_MODE}


class TestOff:
    @pytest.mark.parametrize("block", [
        {},
        {"auto_mode": "off"},
        {"auto_mode": "OFF"},
        {"auto_mode": ""},
        {"auto_mode": None},
        # YAML 1.1 parses an unquoted `auto_mode: off` as boolean False.
        {"auto_mode": False},
    ])
    def test_no_settings_field(self, monkeypatch, tmp_path, block, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, block)
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        assert "settings" not in (fields.get("extra_args") or {})
        assert not [r for r in caplog.records if "auto_mode" in r.getMessage()]


class TestInlineMapping:
    def test_mapping_passed_through_as_is(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        # A user settings file must NOT be consulted for an inline mapping.
        _write_user_settings(tmp_path, _full_user_settings())
        inline = {
            "environment": ["Inline env line"],
            "soft_deny": ["Inline soft deny"],
            "hard_deny": ["Inline hard deny"],
            "classifyAllShell": True,
        }
        _set_provider(monkeypatch, {"auto_mode": inline})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _settings_obj(_fields(session)) == {"autoMode": inline}

    def test_mapping_keys_stay_nested_under_auto_mode(self, monkeypatch, tmp_path):
        # A mapping cannot promote itself to a top-level settings key.
        monkeypatch.setenv("HOME", str(tmp_path))
        inline = {"environment": ["e"], "permissions": {"allow": ["Bash(*)"]}}
        _set_provider(monkeypatch, {"auto_mode": inline})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert list(_settings_obj(_fields(session))) == ["autoMode"]


class TestMalformedFallsBackOff:
    @pytest.mark.parametrize("payload", [
        "{not json",
        json.dumps({"autoMode": ["a", "list"]}),
        json.dumps({"autoMode": "a string"}),
        json.dumps({"permissions": {}}),  # no autoMode at all
        json.dumps(["top", "level", "array"]),
    ])
    def test_bad_user_settings_warn_and_off(self, monkeypatch, tmp_path, payload, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, payload)
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        assert any("auto_mode" in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_missing_user_settings_warn_and_off(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))  # no .claude/settings.json
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        assert any("auto_mode" in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.WARNING)

    @pytest.mark.parametrize("value", ["inherit", "on", True, 7, ["inherit_user"]])
    def test_invalid_config_value_warn_and_off(self, monkeypatch, tmp_path, value, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": value})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        assert any("auto_mode" in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_unserializable_inline_mapping_warn_and_off(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        _set_provider(monkeypatch, {"auto_mode": {"environment": [object()]}})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        assert any("auto_mode" in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.WARNING)


class TestSnapshot:
    def test_later_settings_edit_does_not_alter_live_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        path = _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        try:
            before = session.build_option_fields()["settings"]
            # Operator edits ~/.claude/settings.json and flips config.yaml
            # mid-conversation: the live session's options must not move
            # (prompt-cache safety; changes land on the next session).
            path.write_text(json.dumps({"autoMode": {"environment": ["CHANGED"]}}))
            _set_provider(monkeypatch, {"auto_mode": "off"})
            after = session.build_option_fields()["settings"]
        finally:
            session.close()
        assert after == before
        assert json.loads(after) == {"autoMode": _USER_AUTO_MODE}

        # ...and the NEXT session picks the change up.
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        fresh, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _settings_obj(_fields(fresh)) == {"autoMode": {"environment": ["CHANGED"]}}

    def test_inline_mapping_mutation_does_not_alter_live_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        inline = {"environment": ["one"]}
        _set_provider(monkeypatch, {"auto_mode": inline})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        try:
            before = session.build_option_fields()["settings"]
            inline["environment"].append("two")
            after = session.build_option_fields()["settings"]
        finally:
            session.close()
        assert after == before


class TestIsolationUnchanged:
    @pytest.mark.parametrize("value", ["inherit_user", {"environment": ["e"]}, "off"])
    def test_setting_sources_stays_empty(self, monkeypatch, tmp_path, value):
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": value})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _fields(session)["setting_sources"] == []

    def test_sdk_builds_inline_settings_flag(self, monkeypatch, tmp_path):
        # End-to-end through the installed SDK's own command builder: the
        # value lands as ONE inline-JSON --settings argument next to an empty
        # --setting-sources= (isolation unchanged), with no temp file.
        sdk = pytest.importorskip("claude_agent_sdk")
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, _full_user_settings())
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        fields = _fields(session)
        opts = sdk.ClaudeAgentOptions(
            settings=fields["settings"],
            setting_sources=fields["setting_sources"],
            cli_path="/bin/echo",
        )
        cmd = SubprocessCLITransport(prompt="x", options=opts)._build_command()
        idx = cmd.index("--settings")
        assert json.loads(cmd[idx + 1]) == {"autoMode": _USER_AUTO_MODE}
        assert "--setting-sources=" in cmd


# ---------- hostile / non-regular ~/.claude/settings.json ----------
# inherit_user reads a file the session does not control. Whatever sits at
# that path — a FIFO, a device symlink, a multi-GB file, pathologically deep
# JSON — must resolve to off with a warning, promptly, and never raise out of
# session start.

_PROMPT_S = 5.0


def _resolve_in_thread(timeout=_PROMPT_S):
    """Run the auto_mode resolver on a daemon thread, bounded by a join
    timeout (no sleeps). Returns (finished, result, exc)."""
    import threading

    from agent.transports import claude_agent_sdk_session_config as sc

    box = {}

    def _run():
        try:
            box["result"] = sc._configured_auto_mode_settings()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test
            box["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    return worker, box


def _assert_off_with_warning(caplog):
    assert any("auto_mode" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


class TestHostileUserSettingsFile:
    @pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="no FIFOs")
    def test_fifo_is_off_and_does_not_block(self, monkeypatch, tmp_path, caplog):
        import os

        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        fifo = tmp_path / ".claude" / "settings.json"
        os.mkfifo(fifo)
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            worker, box = _resolve_in_thread()
            blocked = worker.is_alive()
            if blocked:
                # Release a reader stuck in open() so no thread leaks: a
                # non-blocking writer open succeeds once a reader waits.
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                worker.join(_PROMPT_S)
        assert not blocked, "reading a FIFO settings.json blocked session start"
        assert "exc" not in box
        assert box["result"] is None
        _assert_off_with_warning(caplog)

    @pytest.mark.skipif(not __import__("os").path.exists("/dev/zero"), reason="no /dev/zero")
    def test_symlink_to_dev_zero_is_off_promptly(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").symlink_to("/dev/zero")
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            worker, box = _resolve_in_thread()
        assert not worker.is_alive(), "reading /dev/zero blocked session start"
        assert "exc" not in box
        assert box["result"] is None
        _assert_off_with_warning(caplog)

    def test_directory_is_off(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude" / "settings.json").mkdir(parents=True)
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            worker, box = _resolve_in_thread()
        assert not worker.is_alive()
        assert "exc" not in box
        assert box["result"] is None
        _assert_off_with_warning(caplog)

    def test_file_over_the_cap_is_off(self, monkeypatch, tmp_path, caplog):
        from agent.transports import claude_agent_sdk_session_config as sc

        cap = getattr(sc, "_USER_SETTINGS_MAX_BYTES", 1024 * 1024)
        assert cap == 1024 * 1024
        monkeypatch.setenv("HOME", str(tmp_path))
        # Valid JSON with a usable autoMode — only the size makes it off.
        payload = _full_user_settings()
        payload["padding"] = "x" * cap
        path = _write_user_settings(tmp_path, payload)
        assert path.stat().st_size > cap
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        _assert_off_with_warning(caplog)

    def test_file_just_under_the_cap_still_works(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        payload = _full_user_settings()
        base = len(json.dumps({**payload, "padding": ""}).encode())
        payload["padding"] = "x" * (1024 * 1024 - base)
        path = _write_user_settings(tmp_path, payload)
        assert path.stat().st_size == 1024 * 1024
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _settings_obj(_fields(session)) == {"autoMode": _USER_AUTO_MODE}

    def test_2000_deep_nested_document_is_off(self, monkeypatch, tmp_path, caplog):
        # Raises RecursionError from the json scanner on 3.11-3.13; on 3.14
        # the (stack-based) scanner parses it into a non-object. Off either
        # way, and nothing escapes session start.
        monkeypatch.setenv("HOME", str(tmp_path))
        _write_user_settings(tmp_path, "[" * 2000 + "]" * 2000)
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        _assert_off_with_warning(caplog)

    def test_nesting_deep_enough_to_raise_recursion_error_is_off(
        self, monkeypatch, tmp_path, caplog
    ):
        # Deep enough to raise RecursionError on EVERY supported Python
        # (3.14's stack guard included) while staying under the 1 MiB cap,
        # next to an otherwise valid autoMode: only catching the error can
        # make this off.
        depth = 200_000
        monkeypatch.setenv("HOME", str(tmp_path))
        body = json.dumps({"autoMode": _USER_AUTO_MODE})[:-1]
        _write_user_settings(
            tmp_path, body + ', "junk": ' + "[" * depth + "]" * depth + "}"
        )
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        with pytest.raises(RecursionError):
            json.loads((tmp_path / ".claude" / "settings.json").read_text())
        with caplog.at_level(logging.WARNING):
            session, _ = _make_session(script=[ResultMessage(result="ok")])
            fields = _fields(session)
        assert "settings" not in fields
        _assert_off_with_warning(caplog)

    def test_symlink_to_a_regular_settings_file_still_works(self, monkeypatch, tmp_path):
        # Dotfile managers (stow, chezmoi, home-manager) symlink settings.json
        # to a regular file elsewhere — that is normal and must keep working.
        monkeypatch.setenv("HOME", str(tmp_path))
        real = tmp_path / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps(_full_user_settings()))
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").symlink_to(real)
        _set_provider(monkeypatch, {"auto_mode": "inherit_user"})
        session, _ = _make_session(script=[ResultMessage(result="ok")])
        assert _settings_obj(_fields(session)) == {"autoMode": _USER_AUTO_MODE}
