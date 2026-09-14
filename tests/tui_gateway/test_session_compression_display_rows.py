from __future__ import annotations

import threading
import types

from tui_gateway import server


def test_manual_compression_excludes_display_answer_from_summary_and_restores_it(monkeypatch):
    sentinel = "SENTINEL SDK BACKGROUND ANSWER"
    captured = {}

    def compress(history, *_args, **_kwargs):
        captured["history"] = history
        return ([{"role": "user", "content": "summary " + repr(history)}], "")

    agent = types.SimpleNamespace(_compress_context=compress, model="test", tools=[])
    session = {
        "agent": agent,
        "history": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": sentinel, "display_kind": "sdk_background_result"},
            {"role": "assistant", "content": "ordinary"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "last ordinary"},
        ],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {})

    server._compress_session_history(session)

    assert sentinel not in repr(captured["history"])
    assert sentinel not in session["history"][0]["content"]
    assert any(row.get("content") == sentinel for row in session["history"])


def test_turn_start_compression_excludes_display_answer_from_summary_input(monkeypatch):
    from agent.context_compressor import ContextCompressor

    sentinel = "SENTINEL TURN-START SDK ANSWER"
    captured = {}
    compressor = ContextCompressor(
        model="test", quiet_mode=True, protect_first_n=0, protect_last_n=1,
        config_context_length=200_000,
    )
    monkeypatch.setattr(compressor, "_compress_window", lambda messages: (0, len(messages) - 1))

    def summarize(turns, **_kwargs):
        captured["turns"] = turns
        return "SAFE SUMMARY"

    monkeypatch.setattr(compressor, "_generate_summary", summarize)
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": sentinel, "display_kind": "sdk_background_result"},
        {"role": "user", "content": "ordinary"},
        {"role": "assistant", "content": "ordinary answer"},
        {"role": "user", "content": "tail"},
        {"role": "assistant", "content": "tail answer"},
    ]

    compressed = compressor.compress(messages, current_tokens=180_000, force=True)

    assert sentinel not in repr(captured["turns"])
    assert sentinel not in repr(compressed)
