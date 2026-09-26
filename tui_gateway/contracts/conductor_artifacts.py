"""Local conductor artifact viewer methods."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from .base import Params, Result
from .registry import method


class ArtifactListParams(Params):
    session_id: str
    job_id: str | None = None
    run_id: str | None = None


class DurableArtifacts(Result):
    state: Literal["unavailable"] = "unavailable"
    reason: Literal["not_configured"] = "not_configured"


class LocalSource(Result):
    job_id: str
    variant: Literal["log", "agy_log"]
    bytes: int
    lines: int | None
    mtime: str
    data_class: Literal["A", "B", "C", "unknown"]
    viewable: Literal["text", "metadata"]
    sha256: str | None
    status: str
    worker: str


class ArtifactListResult(Result):
    durable: DurableArtifacts
    local: list[LocalSource] | None
    local_reason: Literal["none", "no_record", "path_mismatch", "unreadable"] | None


class ArtifactReadParams(Params):
    session_id: str
    source: Literal["durable", "local"]
    artifact_id: UUID | None = None
    job_id: str | None = None
    variant: Literal["log", "agy_log"] | None = None
    mode: Literal["text", "image", "bytes"]
    offset: int | None = None
    length: int | None = None
    from_end: bool = False


class ArtifactReadResult(Result):
    mode: Literal["text", "image", "bytes"]
    text: str | None = None
    data_url: str | None = None
    base64: str | None = None
    offset: int | None = None
    length: int | None = None
    total_bytes: int | None = None
    eof: bool | None = None
    bof: bool | None = None
    redacted: bool | None = None
    elided: bool | None = None


method(
    "conductor_artifacts.list",
    params=ArtifactListParams,
    result=ArtifactListResult,
    doc="List local conductor worker log sources for the calling session's workspace.",
)
method(
    "conductor_artifacts.read",
    params=ArtifactReadParams,
    result=ArtifactReadResult,
    doc="Read a bounded local conductor worker log window for the calling session.",
)
