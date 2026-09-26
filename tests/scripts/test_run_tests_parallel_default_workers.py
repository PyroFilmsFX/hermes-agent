"""The runner's default parallelism stays bounded regardless of core count, and an explicit
HERMES_TEST_WORKERS always wins (2026-09-25: cpu_count*2 = 36 workers pinned the machine)."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_tests_parallel.py"


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("run_tests_parallel_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("cores", [1, 4, 18, 64])
def test_default_never_exceeds_cap_or_core_count(runner, monkeypatch, cores):
    monkeypatch.delenv("HERMES_TEST_WORKERS", raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: cores)
    workers = runner._default_workers()
    assert 1 <= workers <= min(cores, runner._DEFAULT_MAX_WORKERS)


def test_env_override_wins_over_the_cap(runner, monkeypatch):
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 18)
    monkeypatch.setenv("HERMES_TEST_WORKERS", str(runner._DEFAULT_MAX_WORKERS + 5))
    assert runner._default_workers() == runner._DEFAULT_MAX_WORKERS + 5
