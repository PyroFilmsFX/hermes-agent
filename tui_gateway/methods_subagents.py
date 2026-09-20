"""Session-scoped roster and bounded live transcript snapshots for shared clients.

Async projection adapted from JoaoMarcos44's PR #70899; controls reuse the
existing subagent.steer RPC rather than introducing a second steering runtime.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_SUBAGENT_SNAPSHOT_FIELDS = (
    "subagent_id", "kind", "parent_id", "depth", "goal", "delegation_id", "model",
    "started_at", "status", "tool_count", "last_tool", "accepting_steer",
)
_SUBAGENT_TAIL_BYTES = 16384
# Identity fields a plugin may not overwrite: enrichment adds what core cannot know (the real worker
# behind a relay wrapper), it does not get to rename the row core is tracking.
_PROTECTED_META_KEYS = frozenset({"sdk_agent_id", "sdk_parent_session_id", "source", "subagent_type"})


def _enriched_meta(record: dict, session_id: str) -> dict:
    """Base SDK metadata plus whatever a plugin that owns this child contributes."""
    from hermes_cli.plugins import invoke_hook

    meta = dict(record.get("subagent_meta") or {})
    try:
        contributions = invoke_hook(
            "subagent_metadata",
            subagent_id=str(record.get("subagent_id") or ""),
            kind=str(record.get("kind") or ""),
            session_id=session_id,
            meta=dict(meta),
            record={key: record.get(key) for key in ("goal", "status", "tool_count", "last_tool", "started_at")},
        )
    except Exception:  # noqa: BLE001 - a plugin must never break the roster
        return meta
    for contribution in contributions or ():
        if not isinstance(contribution, dict):
            continue
        meta.update({str(k): v for k, v in contribution.items() if str(k) not in _PROTECTED_META_KEYS})
    return meta


def _plugin_tail(record: dict, session_id: str, meta: dict, limit: int) -> dict | None:
    """A tail served by the plugin that owns this child, or None to use the built-in readers."""
    from hermes_cli.plugins import invoke_hook

    try:
        results = invoke_hook(
            "subagent_tail",
            subagent_id=str(record.get("subagent_id") or ""),
            kind=str(record.get("kind") or ""),
            session_id=session_id,
            meta=dict(meta),
            limit=limit,
        )
    except Exception:  # noqa: BLE001 - fall back to the built-in readers
        return None
    for result in results or ():
        if isinstance(result, dict) and result.get("text"):
            text = str(result.get("text") or "")
            return {
                "available": True,
                "text": text[-limit:],
                "truncated": bool(result.get("truncated")) or len(text) > limit,
                "state": str(result.get("state") or "ready"),
                "source": str(result.get("source") or "plugin"),
            }
    return None


def _owned_subagent_records(session_id, transport, owner):
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, _subagent_transport_matches

    with _active_subagents_lock:
        return [dict(r) for r in _active_subagents.values()
                if r.get("owner_session_id") == session_id
                and _subagent_transport_matches(r, transport)
                and r.get("owner_session_record") is owner]


@method("subagent.list")
def _(rid, params):
    session_id = _str_param(params, "session_id")
    transport, owner = _current_session_steer_authority(session_id)
    if transport is None or owner is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    live = _owned_subagent_records(session_id, transport, owner)
    snapshots = []
    for record in live:
        snapshot = {key: record.get(key) for key in _SUBAGENT_SNAPSHOT_FIELDS if key != "kind"}
        if record.get("kind"):
            snapshot["kind"] = record["kind"]
        if meta := _enriched_meta(record, session_id):
            snapshot["meta"] = meta
        snapshots.append(snapshot)
    return _ok(rid, {"subagents": snapshots, "delegations": []})


@method("subagent.interrupt")
def _(rid, params):
    from agent.interrupt_compat import request_hard_interrupt

    subagent_id = _str_param(params, "subagent_id")
    if not subagent_id:
        return _err(rid, 4000, "subagent_id required")
    session_id = _str_param(params, "session_id")
    transport, owner = _current_session_steer_authority(session_id)
    if transport is None or owner is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    record = next((r for r in _owned_subagent_records(session_id, transport, owner)
                   if r.get("subagent_id") == subagent_id), None)
    agent = record.get("agent") if record else None
    # Interrupt the authorized object, never re-resolve a globally recyclable id.
    found = False
    if record and record.get("kind") == "sdk":
        session = record.get("sdk_session")
        stop_task = getattr(session, "stop_task", None)
        if callable(stop_task):
            try:
                found = bool(stop_task(subagent_id))
            except Exception:
                logger.debug("SDK subagent stop failed", exc_info=True)
    elif agent is not None:
        try:
            found = bool(request_hard_interrupt(agent, f"Interrupted via TUI ({subagent_id})"))
        except Exception:
            logger.debug("subagent interrupt failed", exc_info=True)
    return _ok(rid, {"found": found, "subagent_id": subagent_id})


@method("subagent.tail")
def _(rid, params):
    session_id = _str_param(params, "session_id")
    subagent_id = _str_param(params, "subagent_id")
    if not subagent_id:
        return _err(rid, 4000, "subagent_id required")
    transport, owner = _current_session_steer_authority(session_id)
    if transport is None or owner is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    # `state` distinguishes "no transcript yet" from "the read failed"; the UI showed one
    # "unavailable" for both.
    result = {"subagent_id": subagent_id, "available": False, "text": "", "truncated": False,
              "state": "waiting", "source": ""}
    record = next((r for r in _owned_subagent_records(session_id, transport, owner)
                   if r.get("subagent_id") == subagent_id), None)
    if record is None:
        return _ok(rid, result)
    meta = _enriched_meta(record, session_id)
    if served := _plugin_tail(record, session_id, meta, _SUBAGENT_TAIL_BYTES):
        return _ok(rid, {**result, **served})
    path = getattr(record.get("agent"), "_live_transcript_path", None)
    if record.get("kind") == "sdk":
        # The CLI's own child transcript is the real history; the in-memory buffer only ever holds
        # forwarded text (off by default), so it is the fallback, not the source.
        from tui_gateway.sdk_subagent_transcript import read_tail

        owner_session = owner if isinstance(owner, dict) else {}
        on_disk = read_tail(meta, owner_session.get("cwd"), limit=_SUBAGENT_TAIL_BYTES)
        if on_disk.get("available"):
            return _ok(rid, {**result, **on_disk})
        text = str(record.get("transcript") or "")
        dropped = int(record.get("transcript_dropped") or 0)
        return _ok(rid, {
            **result,
            "available": bool(text),
            "text": text[-_SUBAGENT_TAIL_BYTES:],
            "truncated": bool(dropped) or len(text) > _SUBAGENT_TAIL_BYTES,
            "state": "ready" if text else on_disk.get("state", "waiting"),
            "source": "sdk-buffer" if text else str(on_disk.get("source") or ""),
        })
    if not path:
        return _ok(rid, result)
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - _SUBAGENT_TAIL_BYTES))
            text = stream.read(_SUBAGENT_TAIL_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        # Creation/cleanup races are normal while a child starts or ends.
        return _ok(rid, {**result, "state": "error", "source": "child-transcript"})
    return _ok(rid, {**result, "available": True, "text": text, "state": "ready",
                     "source": "child-transcript", "truncated": size > _SUBAGENT_TAIL_BYTES})


def register(server):
    bind_module(globals(), server)
