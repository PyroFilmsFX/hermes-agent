"""REST surface for the cntrl-groups sidebar integration."""

from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("cntrl_groups_registry", _PLUGIN_ROOT / "groups.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cntrl-groups registry could not be loaded")
_GROUPS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GROUPS)

router = APIRouter()


class GroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: Optional[bool] = None
    order: Optional[int] = Field(default=None, ge=0)
    # Rename is used by the desktop header menu; keeping it in the same
    # transaction preserves all membership and preference rows.
    name: Optional[str] = None


def _validate_name(name: str) -> str:
    if (
        not name
        or len(name) > 64
        or name != name.strip()
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise HTTPException(status_code=400, detail="Invalid group name")
    return name


def _validate_session_id(session_id: str) -> str:
    if (
        not session_id
        or len(session_id) > 256
        or "/" in session_id
        or "\\" in session_id
        or any(ord(character) < 32 for character in session_id)
    ):
        raise HTTPException(status_code=400, detail="Invalid session id")
    return session_id


def _connect() -> sqlite3.Connection:
    try:
        return _GROUPS.connect()
    except SystemExit as exc:
        raise HTTPException(status_code=503, detail="Hermes state database is unavailable") from exc


@router.get("/groups")
def list_groups():
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT g.group_name, COALESCE(p.pinned, 0) AS pinned, "
            "COALESCE(p.sort_order, 0) AS sort_order "
            f"FROM {_GROUPS.TABLE} g JOIN sessions s ON s.id = g.session_id "
            "LEFT JOIN cntrl_group_prefs p ON p.group_name = g.group_name"
        ).fetchall()
        return [
            {
                "name": row["group_name"],
                "session_ids": [
                    session["session_id"]
                    for session in conn.execute(
                        f"SELECT g.session_id FROM {_GROUPS.TABLE} g JOIN sessions s ON s.id = g.session_id "
                        "WHERE g.group_name = ? ORDER BY g.session_id",
                        (row["group_name"],),
                    ).fetchall()
                ],
                "pinned": bool(row["pinned"]),
                "order": int(row["sort_order"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


@router.put("/groups/{name}/sessions/{session_id}")
def tag_session(name: str, session_id: str):
    name = _validate_name(name)
    session_id = _validate_session_id(session_id)
    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Session not found")
        with conn:
            conn.execute(
                f"INSERT INTO {_GROUPS.TABLE}(session_id, group_name, added_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET group_name=excluded.group_name, added_at=excluded.added_at",
                (session_id, name, time.time()),
            )
        return {"name": name, "session_id": session_id}
    finally:
        conn.close()


@router.delete("/groups/{name}/sessions/{session_id}")
def untag_session(name: str, session_id: str):
    name = _validate_name(name)
    session_id = _validate_session_id(session_id)
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute(
                f"DELETE FROM {_GROUPS.TABLE} WHERE group_name = ? AND session_id = ?",
                (name, session_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "DELETE FROM cntrl_group_prefs WHERE group_name = ? AND NOT EXISTS "
                    f"(SELECT 1 FROM {_GROUPS.TABLE} WHERE group_name = ?)",
                    (name, name),
                )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Group membership not found")
        return {"name": name, "session_id": session_id}
    finally:
        conn.close()


@router.patch("/groups/{name}")
def update_group(name: str, patch: GroupPatch):
    name = _validate_name(name)
    new_name = _validate_name(patch.name) if patch.name is not None else name
    if patch.pinned is None and patch.order is None and patch.name is None:
        raise HTTPException(status_code=400, detail="At least one group field is required")
    conn = _connect()
    try:
        if not conn.execute(f"SELECT 1 FROM {_GROUPS.TABLE} WHERE group_name = ? LIMIT 1", (name,)).fetchone():
            raise HTTPException(status_code=404, detail="Group not found")
        if new_name != name and conn.execute(
            f"SELECT 1 FROM {_GROUPS.TABLE} WHERE group_name = ? LIMIT 1", (new_name,)
        ).fetchone():
            raise HTTPException(status_code=409, detail="Group already exists")
        with conn:
            conn.execute(
                "INSERT INTO cntrl_group_prefs(group_name, pinned, sort_order) VALUES(?,?,?) "
                "ON CONFLICT(group_name) DO NOTHING",
                (name, 0, 0),
            )
            if patch.pinned is not None:
                conn.execute("UPDATE cntrl_group_prefs SET pinned = ? WHERE group_name = ?", (int(patch.pinned), name))
            if patch.order is not None:
                conn.execute("UPDATE cntrl_group_prefs SET sort_order = ? WHERE group_name = ?", (patch.order, name))
            if new_name != name:
                conn.execute(f"UPDATE {_GROUPS.TABLE} SET group_name = ? WHERE group_name = ?", (new_name, name))
                conn.execute("UPDATE cntrl_group_prefs SET group_name = ? WHERE group_name = ?", (new_name, name))
        row = conn.execute("SELECT pinned, sort_order FROM cntrl_group_prefs WHERE group_name = ?", (new_name,)).fetchone()
        return {"name": new_name, "pinned": bool(row["pinned"]), "order": int(row["sort_order"])}
    finally:
        conn.close()
