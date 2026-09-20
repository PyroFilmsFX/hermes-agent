"""Bounded reads of a Claude Agent SDK subagent's on-disk transcript.

The SDK forwards a child's tool calls but not its prose unless ``forward_subagent_text`` is on, so
Hermes' in-memory buffer is usually empty and the panel said "Live transcript unavailable" while the
real transcript sat on disk. The CLI writes one JSONL file per child under

    <config dir>/projects/<cwd slug>/<parent session uuid>/subagents/agent-<agent id>.jsonl

joined to a row by ``TaskStarted.task_id == agentId`` (the ids kept in ``subagent_meta`` as
``sdk_agent_id`` / ``sdk_parent_session_id``).

Reads are tail-bounded and never parse the whole file: a long-running child's transcript grows
without limit, and this runs behind a UI poll.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

_MAX_TAIL_BYTES = 256 * 1024  # bytes of JSONL to inspect; the rendered text is capped by the caller


def _config_dir() -> Path:
    """The CLI's config dir: ``CLAUDE_CONFIG_DIR`` when set, else ``~/.claude``."""
    configured = str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _cwd_slugs(cwd: str | None) -> list[str]:
    """Project-directory spellings the CLI uses for ``cwd`` (it replaces separators with dashes)."""
    raw = str(cwd or "").strip()
    if not raw:
        return []
    candidates = {raw}
    with_real = os.path.realpath(raw)
    candidates.add(with_real)
    return [path.replace(os.sep, "-").replace(".", "-") for path in candidates]


def transcript_path(meta: dict[str, Any] | None, cwd: str | None = None) -> Path | None:
    """The child's transcript file, or None when the ids/file are not there (yet)."""
    meta = meta or {}
    agent_id = str(meta.get("sdk_agent_id") or "").strip()
    parent_session = str(meta.get("sdk_parent_session_id") or "").strip()
    if not agent_id or not parent_session:
        return None
    projects = _config_dir() / "projects"
    names = [f"agent-{agent_id}.jsonl", f"{agent_id}.jsonl"]
    for slug in _cwd_slugs(cwd):
        for name in names:
            candidate = projects / slug / parent_session / "subagents" / name
            if candidate.is_file():
                return candidate
    # cwd unknown or renamed: the parent-session directory is unique enough to scan for.
    try:
        for session_dir in projects.glob(f"*/{parent_session}/subagents"):
            for name in names:
                candidate = session_dir / name
                if candidate.is_file():
                    return candidate
    except OSError:
        return None
    return None


def _entry_text(entry: dict[str, Any]) -> str:
    """One transcript line rendered for display, or "" for lines with nothing to show."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    role = str(message.get("role") or entry.get("type") or "")
    content = message.get("content")
    if isinstance(content, str):
        return f"{role}: {content}".strip() if content.strip() else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and str(block.get("text") or "").strip():
            parts.append(str(block["text"]).strip())
        elif kind == "thinking" and str(block.get("thinking") or "").strip():
            parts.append(f"[thinking] {str(block['thinking']).strip()}")
        elif kind == "tool_use":
            parts.append(f"[tool] {block.get('name') or 'tool'}")
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(str(item.get("text") or "") for item in body if isinstance(item, dict))
            summary = " ".join(str(body or "").split())
            parts.append(f"[result] {summary[:200]}" if summary else "[result]")
    joined = "\n".join(part for part in parts if part)
    return f"{role}: {joined}" if joined and role else joined


def _iter_entries(raw: str) -> Iterable[dict[str, Any]]:
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue  # a partial first line from the tail cut, or a blank
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def read_tail(meta: dict[str, Any] | None, cwd: str | None = None, *, limit: int = 16384) -> dict[str, Any]:
    """``{available, text, truncated, state, source}`` for a child's transcript.

    ``state`` separates the cases the UI used to collapse into one "unavailable": ``ready`` (text),
    ``waiting`` (no file yet, or nothing renderable — a child that has only just started), and
    ``error`` (the file is there but unreadable).
    """
    empty = {"available": False, "text": "", "truncated": False, "state": "waiting", "source": "sdk-transcript"}
    path = transcript_path(meta, cwd)
    if path is None:
        return empty
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - _MAX_TAIL_BYTES))
            raw = stream.read(_MAX_TAIL_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return {**empty, "state": "error"}
    rendered = [text for entry in _iter_entries(raw) if (text := _entry_text(entry))]
    if not rendered:
        return empty
    text = "\n\n".join(rendered)
    clipped = text[-limit:]
    return {
        "available": True,
        "text": clipped,
        # Honest: either the file was longer than the window we read, or the render was clipped.
        "truncated": size > _MAX_TAIL_BYTES or len(text) > len(clipped),
        "state": "ready",
        "source": "sdk-transcript",
    }
