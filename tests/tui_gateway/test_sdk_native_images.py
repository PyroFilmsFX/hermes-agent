"""Native image turns on the Claude Agent SDK lane: routing, payload budget, fallback notes, durable row."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tui_gateway import server

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
_NATIVE = {"agent": {"image_input_mode": "native"}}


def _png(path, padding=0):
    path.write_bytes(_PNG + b"\0" * padding)
    return str(path)


def _agent(api_mode):
    return SimpleNamespace(api_mode=api_mode, provider="claude-agent-sdk", model="claude-fable-5-1")


def _images(parts):
    return [p for p in parts if p.get("type") == "image_url"]


@pytest.fixture
def small_sdk_buffer():
    # 400 KiB of stdout buffer: one ~130 KiB image fits, a ~400 KiB one cannot.
    with patch(
        "agent.transports.claude_agent_sdk_session_config._configured_max_buffer_size",
        return_value=400 * 1024,
    ), patch("hermes_cli.config.load_config", return_value=_NATIVE):
        yield


def test_sdk_turn_sends_what_fits_and_notes_each_attachment_it_could_not(tmp_path, small_sdk_buffer):
    ok = _png(tmp_path / "ok.png", padding=100 * 1024)
    big = _png(tmp_path / "big.png", padding=400 * 1024)
    missing = str(tmp_path / "missing.png")

    parts = server._route_turn_images(_agent("claude_agent_sdk"), "compare these", [ok, big, missing])

    assert isinstance(parts, list)
    (image,) = _images(parts)
    assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == (tmp_path / "ok.png").read_bytes()
    text = parts[0]["text"]
    assert text.startswith("compare these")
    assert f"[Image attached at: {ok}]" in text
    assert f"[Image attached at: {big}]" not in text
    assert f"too large to send inline: big.png]\n[Examine it with the vision_analyze tool using image_url: {big}]" in text
    assert "could not be read: missing.png]" in text
    assert f"image_url: {missing}" not in text


def test_same_attachments_on_another_native_lane_are_not_budgeted(tmp_path, small_sdk_buffer):
    ok = _png(tmp_path / "ok.png", padding=100 * 1024)
    big = _png(tmp_path / "big.png", padding=400 * 1024)

    sdk = server._route_turn_images(_agent("claude_agent_sdk"), "look", [ok, big])
    other = server._route_turn_images(_agent("anthropic_messages"), "look", [ok, big])

    assert len(_images(sdk)) == 1
    assert len(_images(other)) == 2
    assert "too large" not in other[0]["text"]


def test_sdk_turn_with_nothing_that_fits_becomes_text_with_notes(tmp_path, small_sdk_buffer):
    big = _png(tmp_path / "big.png", padding=400 * 1024)
    missing = str(tmp_path / "gone.png")

    message = server._route_turn_images(_agent("claude_agent_sdk"), "what is this", [big, missing])

    assert isinstance(message, str)
    assert message.endswith("what is this")
    assert f"image_url: {big}" in message
    assert "could not be read: gone.png]" in message


def test_sdk_durable_row_is_the_caption_and_image_directive_not_the_bytes(tmp_path):
    from agent.image_routing import build_native_content_parts
    from run_agent import AIAgent

    img = _png(tmp_path / "cat.png")
    native_parts, _ = build_native_content_parts("what is in this photo?", [img])

    def flush(override):
        agent = AIAgent.__new__(AIAgent)
        agent._session_db = MagicMock()
        agent._session_db_created = True
        agent.session_id = "s-1"
        agent._last_flushed_db_idx = 0
        agent._persist_disabled = False
        agent._flushed_db_message_ids = set()
        agent._flushed_db_message_session_id = None
        agent._pending_cli_user_message = None
        agent._persist_user_message_timestamp = None
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = override
        messages = [{"role": "user", "content": native_parts}]
        agent._flush_messages_to_session_db(messages, [])
        agent._apply_persist_user_message_override(messages)
        row = agent._session_db.append_messages_batch.call_args.kwargs["messages"][0]
        return row, messages[0]["content"]

    sdk_row, sdk_live = flush(server._build_persist_user_message(
        "what is in this photo?", [img], native_parts, runtime_owns_media=True))
    generic_row, generic_live = flush(server._build_persist_user_message("what is in this photo?", [img], native_parts))

    expected = server._build_persist_message_with_image_refs("what is in this photo?", [img])
    assert type(sdk_row["content"]) is str and sdk_row["content"] == expected
    assert type(sdk_live) is str and sdk_live == expected
    assert "base64" not in str(sdk_row) and "base64" not in str(sdk_live)
    # Other lanes keep the pixels in live history and a placeholder in the row, as before.
    assert generic_row["content"] != expected and "[screenshot]" in generic_row["content"]
    assert any(part.get("type") == "image_url" for part in generic_live)
