"""Turn-input translation for the SDK: image/content blocks and the streaming
user-message shape. Extracted from ``claude_agent_sdk_session.py``;
``agent.claude_sdk_aux_client`` reaches these through the facade.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.transports.claude_agent_sdk_session")


def _sdk_image_content_block(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Translate one Hermes/OpenAI image part to an SDK-native image block.

    The Agent SDK accepts structured user messages in streaming-input mode.
    Preserve base64 data URIs and http(s) image URLs instead of pretending a
    text-only query still carries the attachment. Return ``None`` for an
    unsupported/malformed source; callers add an explicit user-visible marker.
    """
    import base64 as _base64
    import re as _re
    from urllib.parse import urlsplit as _urlsplit

    source = item.get("source")
    if isinstance(source, dict):
        source_type = source.get("type")
        if source_type == "base64":
            media_type = source.get("media_type")
            data = source.get("data")
            if (
                isinstance(media_type, str)
                and media_type.startswith("image/")
                and isinstance(data, str)
                and data
            ):
                try:
                    _base64.b64decode(data, validate=True)
                except Exception:
                    return None
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
        elif source_type == "url":
            raw_url = source.get("url")
            if isinstance(raw_url, str):
                parsed = _urlsplit(raw_url)
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    return {
                        "type": "image",
                        "source": {"type": "url", "url": raw_url},
                    }

    raw_url: Any = item.get("image_url")
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("url")
    if not isinstance(raw_url, str):
        raw_url = item.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        return None

    data_match = _re.fullmatch(
        r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)",
        raw_url,
        flags=_re.DOTALL,
    )
    if data_match:
        media_type, data = data_match.groups()
        try:
            _base64.b64decode(data, validate=True)
        except Exception:
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    parsed = _urlsplit(raw_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {
            "type": "image",
            "source": {"type": "url", "url": raw_url},
        }
    return None


def _coerce_turn_input(user_input: Any) -> Any:
    """Preserve Hermes/OpenAI rich images while keeping text-only turns plain.

    ClaudeSDKClient.query accepts either a string or an async stream of SDK
    message dictionaries. A content list with at least one valid image becomes
    SDK-native blocks; a text-only list keeps the historical joined-string
    behavior. Invalid image sources become truthful text, never a fabricated
    claim that an image is attached.
    """
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        blocks: list[dict[str, Any]] = []
        has_valid_image = False
        for item in user_input:
            if isinstance(item, str):
                if item.strip():
                    blocks.append({"type": "text", "text": item})
                continue
            if not isinstance(item, dict):
                if item is not None:
                    blocks.append({"type": "text", "text": str(item)})
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": str(text)})
            elif item_type in {"image", "image_url", "input_image"}:
                image = _sdk_image_content_block(item)
                if image is not None:
                    blocks.append(image)
                    has_valid_image = True
                else:
                    logger.warning(
                        "claude-agent-sdk: image attachment has an unsupported "
                        "or malformed source; sending an explicit unavailable marker"
                    )
                    blocks.append({
                        "type": "text",
                        "text": (
                            "[image attachment unavailable: unsupported or "
                            "malformed source]"
                        ),
                    })
        if has_valid_image:
            return blocks
        return "\n\n".join(
            str(block.get("text") or "")
            for block in blocks
            if block.get("type") == "text"
        ).strip()
    return "" if user_input is None else str(user_input)


# Per-image ceiling: the bundled CLI's conservative default image budget (5 MiB of base64). The whole
# message has a second ceiling: with replay-user-messages the CLI echoes the user message back on
# stdout, and a stdout line over max_buffer_size kills the SDK reader (and the session) mid-turn.
SDK_IMAGE_BASE64_LIMIT = 5 * 1024 * 1024
_SDK_ECHO_ENVELOPE_RESERVE = 64 * 1024
_SDK_IMAGE_BLOCK_OVERHEAD = 256


def _image_part_payload_size(part: dict[str, Any]) -> Optional[int]:
    """Bytes of base64 an image part will put on the wire (a URL costs its length), or None if not an image."""
    if not isinstance(part, dict) or part.get("type") not in {"image_url", "input_image", "image"}:
        return None
    source = part.get("source")
    if isinstance(source, dict):
        return len(str(source.get("data") or source.get("url") or ""))
    raw_url = part.get("image_url")
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("url")
    raw_url = raw_url if isinstance(raw_url, str) else str(part.get("url") or "")
    _head, sep, data = raw_url.partition(";base64,")
    return len(data) if sep else len(raw_url)


def _image_part_data_url(part: dict[str, Any]) -> Optional[str]:
    """The data URL an image part carries, in either the Hermes or the SDK block shape."""
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        media_type, data = source.get("media_type"), source.get("data")
        if isinstance(media_type, str) and isinstance(data, str):
            return f"data:{media_type};base64,{data}"
        return None
    raw_url = part.get("image_url")
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("url")
    if not isinstance(raw_url, str):
        raw_url = part.get("url")
    return raw_url if isinstance(raw_url, str) and raw_url.startswith("data:") else None


def _with_image_data_url(part: dict[str, Any], url: str) -> dict[str, Any]:
    """``part`` carrying ``url``, keeping whichever shape it already had."""
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        header, _, data = url.partition(",")
        media_type = header[len("data:"):].split(";", 1)[0] or source.get("media_type")
        return {**part, "source": {**source, "data": data, "media_type": media_type}}
    if isinstance(part.get("image_url"), dict):
        return {**part, "image_url": {**part["image_url"], "url": url}}
    return {**part, "image_url": {"url": url}} if "image_url" in part else {**part, "url": url}


def _shrink_image_part(part: dict[str, Any], budget: int) -> Optional[dict[str, Any]]:
    """Downscale an over-budget image so it fits, or None when it cannot.

    Retina screenshots routinely encode to 6+ MiB of base64 — well over the per-image ceiling — and
    dropping them lost the attachment the user actually sent. The CLI downscales its own inputs
    anyway (2000px default), so this loses nothing the model would have seen.
    """
    url = _image_part_data_url(part)
    if not url:
        return None
    try:
        from agent.conversation_compression import _shrink_data_url
        from tools.vision_tools import _resize_image_for_vision
    except Exception:  # noqa: BLE001 - Pillow/vision stack missing: fall back to the note
        logger.debug("claude-agent-sdk: image shrink unavailable", exc_info=True)
        return None
    for max_dimension in (2000, 1400, 1000):
        try:
            resized, _unshrinkable = _shrink_data_url(
                url, max_dimension=max_dimension, resize_fn=_resize_image_for_vision
            )
        except Exception:  # noqa: BLE001 - never fail a turn over a resize
            logger.debug("claude-agent-sdk: image shrink raised", exc_info=True)
            return None
        if not resized:
            continue
        candidate = _with_image_data_url(part, resized)
        size = _image_part_payload_size(candidate)
        if size is not None and size <= budget:
            logger.info(
                "claude-agent-sdk: attached image downscaled to %dpx to fit the payload budget", max_dimension
            )
            return candidate
    return None


def fit_images_to_sdk_budget(
    parts: list[dict[str, Any]], *, max_buffer_size: Optional[int] = None
) -> tuple[list[dict[str, Any]], list[int]]:
    """Drop image parts the SDK lane cannot carry, in order: one over the per-image ceiling, or one that
    would push the echoed message past the stdout buffer. Returns ``(kept_parts, dropped)`` where
    ``dropped`` indexes the image parts (0-based among images) that were removed."""
    import json as _json

    if max_buffer_size is None:
        from agent.transports.claude_agent_sdk_session_config import _configured_max_buffer_size

        max_buffer_size = _configured_max_buffer_size()
    budget = max(0, int(max_buffer_size) - _SDK_ECHO_ENVELOPE_RESERVE)
    used = sum(
        len(_json.dumps(part, ensure_ascii=False)) for part in parts if _image_part_payload_size(part) is None
    )
    kept: list[dict[str, Any]] = []
    dropped: list[int] = []
    image_index = 0
    for part in parts:
        size = _image_part_payload_size(part)
        if size is None:
            kept.append(part)
            continue
        cost = size + _SDK_IMAGE_BLOCK_OVERHEAD
        if size > SDK_IMAGE_BASE64_LIMIT or used + cost > budget:
            # Shrink before giving up: the attachment is what the user actually sent.
            headroom = min(SDK_IMAGE_BASE64_LIMIT, max(0, budget - used - _SDK_IMAGE_BLOCK_OVERHEAD))
            smaller = _shrink_image_part(part, headroom) if headroom else None
            smaller_size = _image_part_payload_size(smaller) if smaller is not None else None
            if smaller is not None and smaller_size is not None:
                used += smaller_size + _SDK_IMAGE_BLOCK_OVERHEAD
                kept.append(smaller)
            else:
                dropped.append(image_index)
        else:
            used += cost
            kept.append(part)
        image_index += 1
    return kept, dropped


def fit_sdk_turn_blocks(prompt: Any, *, max_buffer_size: int) -> Any:
    """Session-boundary backstop for every caller (desktop, CLI, messaging, delegation): images the CLI
    cannot carry are replaced by one note, so a turn never kills the reader. Path-aware notes are the
    desktop gateway's job; here only counts are known."""
    if not isinstance(prompt, list):
        return prompt
    kept, dropped = fit_images_to_sdk_budget(prompt, max_buffer_size=max_buffer_size)
    if not dropped:
        return prompt
    logger.warning("claude-agent-sdk: %d attached image(s) exceed the payload budget; omitted", len(dropped))
    note = (
        f"[{len(dropped)} attached image(s) were too large to send inline and were omitted; "
        "ask the user to resend a smaller version if they matter]"
    )
    has_image = any(_image_part_payload_size(block) is not None for block in kept)
    if not has_image:
        text = "\n\n".join(
            str(block.get("text") or "") for block in kept if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return f"{text}\n\n{note}" if text else note
    return [*kept, {"type": "text", "text": note}]


def _sdk_user_message(content: Any, *, origin: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The one streaming-input user envelope for host turns and steers.

    Host turns stay unattributed: steer accounting identifies a steer's result by its human origin, so a
    turn stamped human would be counted as a steer."""
    message = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    if origin is not None:
        message["origin"] = origin
    return message


async def _sdk_user_message_stream(content: Any, *, origin: Optional[dict[str, Any]] = None):
    """One-message async stream accepted by ``ClaudeSDKClient.query``."""
    yield _sdk_user_message(content, origin=origin)
