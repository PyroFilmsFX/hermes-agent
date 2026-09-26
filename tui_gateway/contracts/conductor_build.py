"""Read-only status for the conductor build armed in a session workspace."""

from __future__ import annotations

from typing import Literal

from .base import Result
from .common import SessionParams
from .registry import method


class ConductorBuild(Result):
    state: Literal["active", "waiting", "blocked", "lease_expired"]
    plan: str
    run_id: str
    session_id: str
    wave_current: int | None
    waves_total: int | None
    waves_done: int
    waiting_on: str
    wait_since: str | int | float | None
    armed_at: str | int | float | None
    lease_expires_at: str | int | float | None
    lanes_running: int
    lanes_stale: int
    lanes_build_matched: int
    usd: None = None
    usd_source: Literal["missing"] = "missing"
    stage: None
    unit_id: None


class ConductorBuildResult(Result):
    build: ConductorBuild | None
    unreadable: bool = False


method(
    "conductor_build.get",
    params=SessionParams,
    result=ConductorBuildResult,
    doc="Read the armed conductor build status for the calling session's workspace.",
)
