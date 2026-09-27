"""HE-SECRET-HYGIENE S2: gateway platform ingest edges (E3 doc cache, E4 inbound text).

``BasePlatformAdapter.handle_message`` masks ``event.text`` before the active/busy split, so
the queue, steer and interrupt paths only ever see masked text. Text documents cached by
``cache_document_from_bytes`` are masked (0600); binary documents are byte-identical. The
failure-path transcript row is masked. Gateway platforms get no opt-out in v1.
Fake secrets only; the root conftest pins the tag key.
"""

from __future__ import annotations

import asyncio
import random
import re
import stat
import string
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.platforms import base as platform_base
from gateway.session import build_session_key
from tests.gateway.test_active_session_text_merge import _make_adapter, _make_event

_ALNUM = string.ascii_letters + string.digits


def fake(n: int, seed: int) -> str:
    rng = random.Random(seed)
    while True:
        value = "".join(rng.choice(_ALNUM) for _ in range(n))
        if re.search("[a-z]", value) and re.search("[A-Z]", value) and re.search("[0-9]", value):
            return value


def _gh(seed: int) -> str:
    return "ghp_" + fake(36, seed)


@pytest.mark.asyncio
async def test_handle_message_masks_text_before_the_busy_queue():
    adapter = _make_adapter()
    token = _gh(1)
    event = _make_event(f"also try {token}")
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()  # a turn is running
    seen: list[str] = []

    async def _while_active(ev, key):
        seen.append(ev.text)

    adapter._handle_message_while_active = _while_active
    adapter._heal_stale_session_lock = lambda _key: None
    await adapter.handle_message(event)
    assert seen and token not in seen[0]
    assert "[REDACTED:github-token:" in seen[0]


@pytest.mark.asyncio
async def test_handle_message_masks_text_on_a_fresh_turn():
    adapter = _make_adapter()
    token = _gh(2)
    event = _make_event(f"deploy with {token}")
    started: list[str] = []
    adapter._start_session_processing = lambda ev, key: started.append(ev.text) or True
    await adapter.handle_message(event)
    assert started and token not in started[0]


def test_cache_document_from_bytes_masks_text_docs(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_base, "get_document_cache_dir", lambda: tmp_path)
    pw = fake(22, 3)
    data = f"DATABASE_URL=postgresql://app:{pw}@db.internal:5432/app\n".encode()
    path = Path(platform_base.cache_document_from_bytes(data, "config.env"))
    body = path.read_bytes()
    assert pw.encode() not in body
    assert b"[REDACTED:postgres-url:" in body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cache_document_from_bytes_leaves_binary_docs_identical(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_base, "get_document_cache_dir", lambda: tmp_path)
    data = b"PK\x03\x04" + _gh(4).encode() + b"\x00\x01\x02"
    path = Path(platform_base.cache_document_from_bytes(data, "report.docx"))
    assert path.read_bytes() == data


def test_failure_transcript_row_is_masked():
    from gateway.run_turn import GatewayTurnMixin

    token = _gh(5)
    prepared = SimpleNamespace(persist_user_message=None, message_text=f"use {token}",
                               persist_user_timestamp=None, persist_user_display_kind=None,
                               persistence_owner=None)
    row = GatewayTurnMixin._hmwa_user_transcript_entry(SimpleNamespace(message_id="m1"), prepared, 1.0)
    assert token not in row["content"]
    assert "[REDACTED:github-token:" in row["content"]
