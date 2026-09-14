"""Frozen SDK tool.complete payload contract."""

import tui_gateway.server as server


class _SdkResult(str):
    def __new__(cls, value, *, is_error=False, error=None, tool_use_result=None, truncated=None):
        result = str.__new__(cls, value)
        result._sdk_tool_result = True
        result._sdk_is_error = is_error
        result._sdk_error = error
        result._sdk_tool_use_result = tool_use_result
        result._sdk_truncated = truncated
        return result


def _capture(monkeypatch):
    events = []
    sid = "sdk-payload"
    monkeypatch.setitem(
        server._sessions,
        sid,
        {"edit_snapshots": {}, "tool_started_at": {}, "tool_progress_mode": "all"},
    )
    monkeypatch.setattr(server, "_connector_lifecycle_is_stale", lambda *args: False)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: True)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(server, "_session_verbose", lambda _sid: False)
    monkeypatch.setattr(server, "_tool_summary", lambda *args: None)
    monkeypatch.setattr(server, "_emit_tool_lifecycle", lambda event, sid, name, args, payload: events.append(payload))
    return sid, events


def test_error_result_payload(monkeypatch):
    sid, events = _capture(monkeypatch)
    server._on_tool_complete(
        sid,
        "tool-1",
        "Bash",
        {"command": "false"},
        _SdkResult("boom", is_error=True, error="boom"),
        is_error=True,
        error="boom",
        truncated=None,
    )
    assert events == [{
        "tool_id": "tool-1",
        "name": "Bash",
        "args": {"command": "false"},
        "is_error": True,
        "error": "boom",
        "result": "boom",
    }]


def test_plain_text_result_payload(monkeypatch):
    sid, events = _capture(monkeypatch)
    server._on_tool_complete(sid, "tool-2", "Echo", {}, _SdkResult("plain text"))
    assert events == [{
        "tool_id": "tool-2",
        "name": "Echo",
        "args": {},
        "is_error": False,
        "result": "plain text",
    }]


def test_capped_result_payload(monkeypatch):
    sid, events = _capture(monkeypatch)
    server._on_tool_complete(
        sid,
        "tool-3",
        "Read",
        {},
        _SdkResult("x" * 12000, truncated={"shown": 4000, "total": 12000}),
        truncated={"shown": 4000, "total": 12000},
    )
    assert events == [{
        "tool_id": "tool-3",
        "name": "Read",
        "args": {},
        "is_error": False,
        "result": "x" * 4000,
        "truncated": {"shown": 4000, "total": 12000},
    }]


def test_tool_use_result_metadata_passthrough(monkeypatch):
    sid, events = _capture(monkeypatch)
    metadata = {"tool_use_id": "tool-4", "duration_ms": 42}
    server._on_tool_complete(
        sid,
        "tool-4",
        "Read",
        {},
        _SdkResult("ok", tool_use_result=metadata),
        tool_use_result=metadata,
    )
    assert events == [{
        "tool_id": "tool-4",
        "name": "Read",
        "args": {},
        "is_error": False,
        "result": "ok",
        "tool_use_result": metadata,
    }]


def test_sdk_scalar_json_text_results_stay_verbatim_strings(monkeypatch):
    """A3-1: "42\\n", "null\\n", "\\"quoted\\"" must reach the card as the original text."""
    sid, events = _capture(monkeypatch)
    for text in ("42\n", "false\n", "null\n", '"quoted"\n'):
        events.clear()
        server._on_tool_complete(sid, "t-scalar", "Bash", {}, _SdkResult(text))
        assert events and events[-1]["result"] == text


def test_native_results_keep_json_decoding(monkeypatch):
    sid, events = _capture(monkeypatch)
    server._on_tool_complete(sid, "t-native", "Bash", {}, '{"ok": true}')
    assert events[-1]["result"] == {"ok": True}
