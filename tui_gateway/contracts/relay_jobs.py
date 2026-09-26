"""Read-only projection of conductor tb-workers jobs for a session workspace."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Params, Result
from .registry import method


class RelayJobsListParams(Params):
    session_id: str
    profile: str | None = None
    # ``recent``: running rows plus terminal rows inside the linger window (stale rows linger an
    # hour). ``build``: every row whose run id belongs to the armed marker, no age window, cap 40.
    scope: Literal["recent", "build"] = "recent"


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
    # Display-only fields derived from the record and its brief; never an absolute path.
    label: str = ""
    purpose: str = ""
    place: str = ""
    effort: str = ""
    exit_code: int | None = None
    build_match: bool = False


class RelayJobsListResult(Result):
    jobs: list[RelayJobRow] = Field(default_factory=list)


method("relay_jobs.list", params=RelayJobsListParams, result=RelayJobsListResult,
       doc="Read recent conductor relay worker jobs for the calling session's workspace.")
