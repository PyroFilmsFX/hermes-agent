"""Tests for GET /api/session-launches long-poll feed and token-only auth (#49 / b10 H3)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent import claude_sdk_launch_table
from agent.claude_sdk_launch_table import _reset_table_for_tests, record_launch
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from tests.hermes_cli.test_dashboard_auth_password_login import PasswordProvider


@pytest.fixture(autouse=True)
def _clean_table():
    _reset_table_for_tests()
    yield
    _reset_table_for_tests()


@pytest.fixture
def auth_client():
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def test_launches_requires_token_401_without_token():
    client = TestClient(web_server.app)
    resp = client.get("/api/session-launches")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


def test_launches_401_with_only_cookie_session_in_gated_mode():
    clear_providers()
    provider = PasswordProvider()
    register_provider(provider)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    try:
        client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
        # Log in to mint valid session cookies in the client's cookie jar
        login = client.post(
            "/auth/password-login",
            json={"provider": "testpw", "username": "admin", "password": "hunter2"},
        )
        assert login.status_code == 200

        # /api/auth/me succeeds with cookie session
        me_resp = client.get("/api/auth/me")
        assert me_resp.status_code == 200

        # /api/session-launches must reject cookie-only session with 401
        launches_resp = client.get("/api/session-launches")
        assert launches_resp.status_code == 401
        assert launches_resp.json()["detail"] == "Unauthorized"

        # Providing the session token succeeds
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        authed_resp = client.get("/api/session-launches")
        assert authed_resp.status_code == 200
        assert "launches" in authed_resp.json()
    finally:
        clear_providers()
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
        web_server.app.state.auth_required = prev_required


def test_returns_launches_newer_than_since(auth_client):
    record_launch(hermes_session_id="h-1", claude_session_id="c-1", profile="default")
    record_launch(hermes_session_id="h-2", claude_session_id="c-2", profile="default")
    record_launch(hermes_session_id="h-3", claude_session_id="c-3", profile="custom")

    # since=0 returns all 3
    resp0 = auth_client.get("/api/session-launches", params={"since": 0})
    assert resp0.status_code == 200
    body0 = resp0.json()
    assert body0["seq"] == 3
    assert len(body0["launches"]) == 3
    assert [x["claude_session_id"] for x in body0["launches"]] == ["c-1", "c-2", "c-3"]

    # since=2 returns only the 3rd
    resp2 = auth_client.get("/api/session-launches", params={"since": 2})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["seq"] == 3
    assert len(body2["launches"]) == 1
    assert body2["launches"][0]["claude_session_id"] == "c-3"

    # since=3 returns none
    resp3 = auth_client.get("/api/session-launches", params={"since": 3})
    assert resp3.status_code == 200
    body3 = resp3.json()
    assert body3["seq"] == 3
    assert body3["launches"] == []

    # Consumer timestamp was recorded
    assert claude_sdk_launch_table.get_last_consumer_seen() > 0.0


def test_wait_wakes_on_new_launch_within_clamp(auth_client):
    record_launch(hermes_session_id="h-init", claude_session_id="c-init")

    def bg_record():
        time.sleep(0.04)
        record_launch(hermes_session_id="h-new", claude_session_id="c-new")

    t = threading.Thread(target=bg_record)
    t.start()

    t0 = time.monotonic()
    resp = auth_client.get("/api/session-launches", params={"since": 1, "wait": 1.5})
    elapsed = time.monotonic() - t0
    t.join()

    assert resp.status_code == 200
    body = resp.json()
    assert body["seq"] == 2
    assert len(body["launches"]) == 1
    assert body["launches"][0]["claude_session_id"] == "c-new"
    assert elapsed < 1.0


def test_wait_clamped_to_2_seconds(auth_client, monkeypatch):
    captured_timeouts = []
    orig_wait = claude_sdk_launch_table.wait_for_change

    def stub_wait(since, timeout):
        captured_timeouts.append(timeout)
        return orig_wait(since, timeout=0.01)

    monkeypatch.setattr(claude_sdk_launch_table, "wait_for_change", stub_wait)

    # wait=10.0 should be clamped to 2.0
    resp_high = auth_client.get("/api/session-launches", params={"since": 0, "wait": 10.0})
    assert resp_high.status_code == 200
    assert captured_timeouts == [2.0]

    # wait=1.2 should remain 1.2
    captured_timeouts.clear()
    resp_mid = auth_client.get("/api/session-launches", params={"since": 0, "wait": 1.2})
    assert resp_mid.status_code == 200
    assert captured_timeouts == [1.2]

    # wait=-5.0 clamped to 0.0, so wait_for_change is not called
    captured_timeouts.clear()
    resp_neg = auth_client.get("/api/session-launches", params={"since": 0, "wait": -5.0})
    assert resp_neg.status_code == 200
    assert captured_timeouts == []


def test_only_whitelisted_fields_leave(auth_client):
    record_launch(
        hermes_session_id="h-field-check",
        claude_session_id="c-field-check",
        profile="default",
        lineage=["root-h"],
    )

    resp = auth_client.get("/api/session-launches", params={"since": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["launches"]) == 1
    item = body["launches"][0]

    whitelisted = {
        "hermes_session_id",
        "claude_session_id",
        "profile",
        "launch_seq",
        "hermes_lineage",
        "recorded_at",
    }
    assert set(item.keys()) == whitelisted
    assert "lineage" not in item
    assert item["hermes_session_id"] == "h-field-check"
    assert item["claude_session_id"] == "c-field-check"
    assert item["profile"] == "default"
    assert item["launch_seq"] == 1
    assert item["hermes_lineage"] == ["root-h"]
    assert isinstance(item["recorded_at"], (int, float))
