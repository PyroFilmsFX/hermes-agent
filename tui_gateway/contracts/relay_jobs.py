"""Read-only projection of conductor tb-workers jobs for a session workspace."""

from __future__ import annotations

from pydantic import Field

from .base import Result
from .common import SessionParams
from .registry import method


class RelayJobRow(Result):
    job_id: str
    worker: str
    model: str = ""
    model_resolved: str = ""
    lane: str = ""
    role: str = ""
    status: str
    spawned_at: str
    heartbeat_at: str = ""
    duration_sec: float | None = None


class RelayJobsListResult(Result):
    jobs: list[RelayJobRow] = Field(default_factory=list)


method("relay_jobs.list", params=SessionParams, result=RelayJobsListResult,
       doc="Read recent conductor relay worker jobs for the calling session's workspace.")
