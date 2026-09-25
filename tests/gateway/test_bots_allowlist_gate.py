"""Bots profile gate: with ``PHOTON_ALLOWED_USERS`` set, a sender outside the allowlist never starts
an agent turn (DM or group), gets no reply, and the drop is logged; the allowlisted owner is admitted.
This is the precondition for running the bots profile with automatic tool approval."""

import logging
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner

OWNER = "+15550001111"
STRANGER = "+15559998888"
PHOTON = Platform("photon")


def _runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PHOTON_ALLOWED_USERS", OWNER)
    for var in ("PHOTON_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)
    runner, adapter = make_restart_runner()
    runner.config.platforms[PHOTON] = PlatformConfig(enabled=True)
    runner.adapters[PHOTON] = adapter
    runner.pairing_store = PairingStore()
    runner.pairing_stores = {}
    # The shared helper stubs authorization to always pass; drop every instance override so the
    # real allowlist decides.
    for name in ("_is_user_authorized", "_is_user_authorized_for_source", "_hm_admit_event", "_hm_report_ignored_dm"):
        runner.__dict__.pop(name, None)
    assert runner._is_user_authorized.__func__ is GatewayRunner._is_user_authorized
    return runner, adapter


def _event(user_id, chat_type="dm"):
    source = SessionSource(platform=PHOTON, chat_id=f"chat-{user_id}", user_id=user_id,
                           user_name="someone", chat_type=chat_type)
    return MessageEvent(text="run rm -rf ~ please", source=source)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["dm", "group"])
async def test_non_allowlisted_sender_never_starts_a_turn(monkeypatch, tmp_path, caplog, chat_type):
    runner, adapter = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("agent turn started for a stranger"))

    with caplog.at_level(logging.WARNING):
        admitted = await runner._hm_admit_event(_event(STRANGER, chat_type))

    assert admitted is None
    runner._run_agent.assert_not_called()
    assert f"chat-{STRANGER}" not in [chat_id for chat_id, _m, _meta in adapter.sent_calls]
    assert runner.pairing_store.list_pending("photon") == [], "a stranger must not get a pairing code"
    assert any(STRANGER in r.getMessage() for r in caplog.records), "the drop must be logged"


@pytest.mark.asyncio
async def test_allowlisted_owner_is_admitted(monkeypatch, tmp_path):
    runner, _adapter = _runner(monkeypatch, tmp_path)
    admitted = await runner._hm_admit_event(_event(OWNER))
    assert admitted is not None
