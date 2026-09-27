"""Tests for hardware RAM detection timeouts and caching."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from hermes_cli.local_runtime import hardware


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_cli import web_server

    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return test_client


@pytest.fixture(autouse=True)
def _reset_ram_cache():
    if hasattr(hardware, "_ram_cache"):
        hardware._ram_cache = None
    yield
    if hasattr(hardware, "_ram_cache"):
        hardware._ram_cache = None


def test_ram_bytes_timeout_darwin_returns_zero(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def _timeout(*argv):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5)

    monkeypatch.setattr(hardware, "_stdout", _timeout)
    assert hardware._ram_bytes() == (0, 0)


def test_ram_bytes_timeout_returns_cached_value(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    total = 17179869184
    avail = 8589934592

    def _good_stdout(*argv):
        if "hw.memsize" in argv:
            return str(total)
        if any("vm_stat" in a for a in argv):
            return (
                "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                f"Pages free: {avail // 16384}.\n"
                "Pages inactive: 0.\n"
                "Pages purgeable: 0.\n"
                "Pages speculative: 0.\n"
            )
        return ""

    monkeypatch.setattr(hardware, "_stdout", _good_stdout)
    first = hardware._ram_bytes()
    assert first == (total, avail)

    def _timeout(*argv):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5)

    monkeypatch.setattr(hardware, "_stdout", _timeout)
    assert hardware._ram_bytes() == first


def test_ram_bytes_vm_stat_timeout_returns_total_half(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    total = 17179869184

    def _sysctl_ok_vmstat_timeout(*argv):
        if "hw.memsize" in argv:
            return str(total)
        if any("vm_stat" in a for a in argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5)
        return ""

    monkeypatch.setattr(hardware, "_stdout", _sysctl_ok_vmstat_timeout)
    assert hardware._ram_bytes() == (total, total // 2)


def test_local_models_hardware_route_timeout_200(client, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def _timeout(*argv):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=5)

    monkeypatch.setattr(hardware, "_stdout", _timeout)
    r = client.get("/api/local-models/hardware")
    assert r.status_code == 200
    data = r.json()
    assert data["ram_total_bytes"] == 0
    assert data["ram_available_bytes"] == 0
