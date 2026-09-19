"""Native image input on the Claude Agent SDK lane: payload budget, envelope, replayed-echo ownership."""

from __future__ import annotations

import asyncio
import time

from agent.transports.claude_agent_sdk_session_input import (
    SDK_IMAGE_BASE64_LIMIT,
    _sdk_user_message_stream,
    fit_images_to_sdk_budget,
)
from agent.transports.claude_agent_sdk_session_turn import _is_own_prompt_echo

MiB = 1024 * 1024


def _image(size):
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * size}}


def test_budget_keeps_images_in_order_until_the_echo_would_overflow_stdout():
    text = {"type": "text", "text": "compare"}
    parts = [text, _image(4 * MiB), _image(4 * MiB), _image(4 * MiB)]

    kept, dropped = fit_images_to_sdk_budget(parts, max_buffer_size=10 * MiB)

    assert kept == parts[:3]
    assert dropped == [2]


def test_budget_drops_one_oversized_image_and_keeps_later_ones_that_fit():
    parts = [_image(SDK_IMAGE_BASE64_LIMIT + 1), _image(1024)]

    kept, dropped = fit_images_to_sdk_budget(parts, max_buffer_size=64 * MiB)

    assert kept == parts[1:]
    assert dropped == [0]


def test_budget_counts_the_text_the_echo_repeats():
    long_text = {"type": "text", "text": "x" * (2 * MiB)}
    images = [_image(4 * MiB), _image(4 * MiB)]

    assert fit_images_to_sdk_budget(images, max_buffer_size=10 * MiB)[1] == []
    assert fit_images_to_sdk_budget([long_text, *images], max_buffer_size=10 * MiB)[1] == [1]


def _sent(content, **kwargs):
    async def collect():
        return [message async for message in _sdk_user_message_stream(content, **kwargs)]

    return asyncio.run(collect())


def test_turns_stay_unattributed_and_steers_carry_human_origin():
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}]

    (turn,) = _sent(blocks)
    (steer,) = _sent("stop", origin={"kind": "human"})

    assert "origin" not in turn
    assert turn["message"] == {"role": "user", "content": blocks}
    assert steer["origin"] == {"kind": "human"}
    assert steer["message"] == {"role": "user", "content": "stop"}


def test_installed_parser_image_echo_is_recognized_as_the_host_prompt():
    from claude_agent_sdk._internal.message_parser import parse_message

    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
    (image_only,) = _sent([image])
    (captioned,) = _sent([{"type": "text", "text": "look"}, image])
    peer = {**image_only, "origin": {"kind": "peer", "from": "uds:/tmp/x.sock"}}
    tool_result = {**image_only, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}
    subagent = {**image_only, "parent_tool_use_id": "task-1"}

    def parsed(raw):
        return parse_message({**raw, "uuid": "u", "session_id": "s"})

    assert _is_own_prompt_echo(parsed(image_only))
    assert _is_own_prompt_echo(parsed(captioned))
    assert not _is_own_prompt_echo(parsed(peer))
    assert not _is_own_prompt_echo(parsed(tool_result))
    assert not _is_own_prompt_echo(parsed(subagent))


def test_image_only_host_turn_reclaims_the_stream_from_an_open_peer_burst():
    """The CLI replays an image-only prompt as an empty user message; that echo must still close an
    open peer burst, or the host's own answer is delivered as background output."""
    from tests.agent.claude_sdk_fakes import (
        AssistantMessage, ResultMessage, TextBlock, UserMessage, _make_session,
    )

    delivered = []
    host_script = [AssistantMessage(content=[TextBlock("a red square")]),
                   ResultMessage(result="a red square", uuid="host-1")]
    session, holder = _make_session(
        script=[],
        on_unsolicited_result=lambda texts, items=None: delivered.append((texts, items)),
    )
    peer_origin = {"kind": "peer", "from": "uds:/tmp/x.sock", "name": "hermes:other", "body": "ping"}
    try:
        session.ensure_started()
        client = holder["client"]
        peer_in = UserMessage(content="ping")
        peer_in.origin = peer_origin
        client.feed(peer_in, AssistantMessage(content=[TextBlock("partial peer text")]))
        deadline = time.time() + 2
        while not session._unsolicited_burst_open and time.time() < deadline:
            time.sleep(0.01)
        assert session._unsolicited_burst_open

        async def query_with_parsed_echo(prompt):
            client.queried.append([message async for message in prompt])
            client.feed(UserMessage(content=[]), *host_script)

        client.query = query_with_parsed_echo
        turn = session.run_turn(
            [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}}],
            turn_timeout=5.0,
        )
    finally:
        session.close()

    assert turn.error is None
    assert turn.final_text == "a red square"
    assert session._unsolicited_burst_open is False
    assert all("a red square" not in " ".join(texts) for texts, _items in delivered)


def test_session_boundary_omits_images_past_the_reader_it_started_with():
    """Every surface (CLI, messaging, delegation) reaches run_turn; the session budgets against the
    max_buffer_size its CLI was started with, not whatever the config says now."""
    from unittest.mock import patch

    from tests.agent.claude_sdk_fakes import ResultMessage, _make_session

    with patch(
        "agent.transports.claude_agent_sdk_session._configured_max_buffer_size", return_value=1 * MiB
    ):
        session, holder = _make_session(script=[ResultMessage(result="ok")])
    small = "data:image/png;base64," + "A" * 1024
    large = "data:image/png;base64," + "A" * (2 * MiB)
    try:
        with patch(
            "agent.transports.claude_agent_sdk_session._configured_max_buffer_size", return_value=64 * MiB
        ):
            turn = session.run_turn([
                {"type": "text", "text": "compare"},
                {"type": "image_url", "image_url": {"url": small}},
                {"type": "image_url", "image_url": {"url": large}},
            ])
        assert holder["client"].options["max_buffer_size"] == 1 * MiB
    finally:
        session.close()

    assert turn.error is None
    ((sent,),) = holder["client"].queried
    content = sent["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image", "text"]
    assert "1 attached image(s) were too large" in content[-1]["text"]


def _real_png(path, width=2400, height=1600):
    from PIL import Image

    # Noise, not flat colour: a flat image compresses to nothing and never trips the budget.
    import random

    rng = random.Random(7)
    image = Image.new("RGB", (width, height))
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(width * height)])
    image.save(path)
    return str(path)


def test_an_oversized_screenshot_is_downscaled_instead_of_dropped(tmp_path):
    """A retina screenshot encodes past the per-image ceiling; dropping it loses the attachment."""
    from agent.image_routing import build_native_content_parts
    from agent.transports.claude_agent_sdk_session_input import _image_part_payload_size

    parts, skipped = build_native_content_parts("what is this", [_real_png(tmp_path / "shot.png")])
    assert not skipped
    original = next(_image_part_payload_size(p) for p in parts if _image_part_payload_size(p) is not None)
    assert original > SDK_IMAGE_BASE64_LIMIT, "fixture must exceed the per-image ceiling"

    kept, dropped = fit_images_to_sdk_budget(parts, max_buffer_size=10 * MiB)

    assert dropped == []
    sent = [p for p in kept if _image_part_payload_size(p) is not None]
    assert len(sent) == 1
    assert _image_part_payload_size(sent[0]) <= SDK_IMAGE_BASE64_LIMIT
    # Still a usable image, not a truncated blob.
    from PIL import Image
    import base64, io

    data = sent[0]["image_url"]["url"].split(",", 1)[1]
    Image.open(io.BytesIO(base64.b64decode(data))).verify()


def test_an_image_that_cannot_be_shrunk_still_becomes_a_note():
    parts = [{"type": "text", "text": "look"}, _image(SDK_IMAGE_BASE64_LIMIT + 1)]

    kept, dropped = fit_images_to_sdk_budget(parts, max_buffer_size=10 * MiB)

    assert dropped == [0]
    assert kept == parts[:1]
