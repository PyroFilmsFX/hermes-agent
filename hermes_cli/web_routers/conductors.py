"""GET /api/profiles/conductors route implementation."""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import logging
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from agent.secret_hygiene import mask_stored_text
from hermes_cli.web_read_coalescing import coalesced_read
from tui_gateway.conductor_roster import (
    Attribution,
    AttributionContext,
    Build,
    DerivedStatus,
    IndexScan,
    Row,
    StatusRead,
    _clean_free_text,
    _get_passwd_home,
    attribute_build,
    collect_live_cli_map,
    derive_row_status,
    group_rows,
    read_build_status,
    read_marker_index,
)
from tui_gateway.methods_conductor_build import _read_json_file
from tui_gateway.methods_relay_jobs import _list_relay_jobs

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

_STALE_KEY = "\x00hermes_stale"
_REUSE_SECONDS = 5.0
_ABANDONED_SECONDS = 7 * 86400  # 7 days


# ── Response Schema Models (§6) ──────────────────────────────────────────────


class ConductorsSources(BaseModel):
    marker_index: str
    state_dirs: int
    skipped: int


class OrchestratorModel(BaseModel):
    hermes_session_id: Optional[str] = None
    profile: Optional[str] = None
    title: Optional[str] = None
    role: Optional[str] = None
    attribution: str
    claude_sid_short: Optional[str] = None
    live: str


class ProjectModel(BaseModel):
    name: str
    root_display: str
    branch: Optional[str] = None
    bound: bool = False
    nested_in: Optional[str] = None


class WavesProgress(BaseModel):
    done: int
    total: Optional[int] = None
    current: Optional[int] = None


class UnitsProgress(BaseModel):
    done: Optional[int] = None
    running: Optional[int] = None
    failed: Optional[int] = None
    remaining: Optional[int] = None
    total: Optional[int] = None


class EstimateModel(BaseModel):
    unit: str = "work_hours"
    p50: float
    p90: float
    basis: str
    as_of: Optional[str] = None
    prediction_id: Optional[str] = None


class LanesModel(BaseModel):
    running: int
    stale: int
    cap: Optional[int] = None


class BuildModel(BaseModel):
    run_id: str
    build_id: Optional[str] = None
    plan_title: Optional[str] = None
    armed_at: Optional[str] = None
    marker_state: str
    phase: Optional[str] = None
    waves: Optional[WavesProgress] = None
    units: Optional[UnitsProgress] = None
    current_units: List[Dict[str, Any]] = []
    remaining_waves: List[Dict[str, Any]] = []
    estimate: Optional[EstimateModel] = None
    gates: List[Dict[str, Any]] = []
    seats: Dict[str, Any] = {}
    refusals: List[Dict[str, Any]] = []
    lanes: Optional[LanesModel] = None
    ci: List[Dict[str, Any]] = []
    owner_blockers: List[Dict[str, Any]] = []
    last_activity_at: Optional[float] = None
    liveness: str
    idle_since: Optional[float] = None
    blocked: bool


class RecordModel(BaseModel):
    present: bool
    valid: bool
    reason: str
    fresh: bool
    status_at: Optional[float] = None
    seq: Optional[int] = None


class OtherBuildModel(BaseModel):
    run_id: str
    plan_title: Optional[str] = None
    liveness: str
    waves: Optional[Dict[str, Any]] = None


class ConductorRow(BaseModel):
    key: str
    orchestrator: OrchestratorModel
    project: ProjectModel
    build: BuildModel
    record: RecordModel
    field_sources: Dict[str, str] = {}
    other_builds: List[OtherBuildModel] = []
    # §4: rows past 7 days go behind "Show abandoned (n)"; they are never silently dropped.
    abandoned: bool = False


class ConductorsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field("hermes-conductors/v1", alias="schema")
    generated_at: float
    rows: List[ConductorRow] = []
    sources: ConductorsSources
    abandoned: int = 0

    @property
    def schema(self) -> str:
        return self.schema_version


# ── Helpers ──────────────────────────────────────────────────────────────────


def _format_root_display(path: str | Path | None) -> str:
    """Abbreviate $HOME to ~ in project root display; no other absolute paths leave."""
    if not path:
        return ""
    p_str = str(path).replace("\\", "/")
    home_str = str(_get_passwd_home()).replace("\\", "/")
    real_home = str(Path(home_str).resolve()).replace("\\", "/")
    for h in (real_home, home_str):
        if p_str == h:
            return "~"
        if p_str.startswith(h + "/"):
            return "~" + p_str[len(h):]
    return p_str


def _safe_common_repo_root(cwd: Path | str | None) -> str | None:
    """Resolve common repo root without subprocess."""
    if not cwd:
        return None
    try:
        p = Path(cwd).resolve()
        parts = p.parts
        for i in range(len(parts) - 1):
            if parts[i] in (".claude", "_claude") and parts[i + 1] == "worktrees":
                enclosing = Path(*parts[:i])
                if (enclosing / ".git").exists():
                    return str(enclosing).replace("\\", "/")
        curr = p
        while curr != curr.parent:
            git_entry = curr / ".git"
            if git_entry.is_dir():
                return str(curr).replace("\\", "/")
            elif git_entry.is_file():
                try:
                    content = git_entry.read_text(encoding="utf-8").strip()
                    if content.startswith("gitdir:"):
                        gitdir_path = Path(content[len("gitdir:"):].strip())
                        if not gitdir_path.is_absolute():
                            gitdir_path = (curr / gitdir_path).resolve()
                        if gitdir_path.parent.name == "worktrees" and gitdir_path.parent.parent.name == ".git":
                            return str(gitdir_path.parent.parent.parent).replace("\\", "/")
                        commondir_file = gitdir_path / "commondir"
                        if commondir_file.is_file():
                            cdir = commondir_file.read_text(encoding="utf-8").strip()
                            common_git = (gitdir_path / cdir).resolve()
                            if common_git.name == ".git":
                                return str(common_git.parent).replace("\\", "/")
                except Exception:
                    pass
                return str(curr).replace("\\", "/")
            curr = curr.parent
    except Exception:
        pass
    try:
        from tui_gateway import git_probe
        return git_probe.common_repo_root(str(cwd)) or None
    except Exception:
        return None


def _safe_get_branch(cwd: Path | str | None) -> str | None:
    """Resolve git branch without subprocess."""
    if not cwd:
        return None
    try:
        p = Path(cwd).resolve()
        curr = p
        while curr != curr.parent:
            git_entry = curr / ".git"
            if git_entry.is_file():
                try:
                    content = git_entry.read_text(encoding="utf-8").strip()
                    if content.startswith("gitdir:"):
                        gitdir_path = Path(content[len("gitdir:"):].strip())
                        if not gitdir_path.is_absolute():
                            gitdir_path = (curr / gitdir_path).resolve()
                        head_file = gitdir_path / "HEAD"
                        if head_file.is_file():
                            head_content = head_file.read_text(encoding="utf-8").strip()
                            if head_content.startswith("ref: refs/heads/"):
                                return head_content[len("ref: refs/heads/"):].strip()
                            return head_content[:8]
                except Exception:
                    pass
            elif git_entry.is_dir():
                head_file = git_entry / "HEAD"
                if head_file.is_file():
                    try:
                        head_content = head_file.read_text(encoding="utf-8").strip()
                        if head_content.startswith("ref: refs/heads/"):
                            return head_content[len("ref: refs/heads/"):].strip()
                        return head_content[:8]
                    except Exception:
                        pass
            curr = curr.parent
    except Exception:
        pass
    try:
        from tui_gateway import git_probe
        return git_probe.branch(str(cwd)) or None
    except Exception:
        return None


def compute_liveness(
    owner_live: str,
    lanes: dict[str, Any] | None,
    matched_jobs: list[dict[str, Any]],
    last_activity_at: float | None,
    gates: list[dict[str, Any]],
    lease_expires_at: float | str | None,
    m_mtime: float | None,
    status_at: float | None,
    now: float,
) -> tuple[str, float | None, bool]:
    """Compute (liveness, idle_since, is_abandoned) per §4."""
    is_owner_live = owner_live in ("busy", "attached")

    lease_ts = None
    if isinstance(lease_expires_at, (int, float)):
        lease_ts = float(lease_expires_at)
    elif isinstance(lease_expires_at, str) and lease_expires_at:
        try:
            lease_ts = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00")).timestamp()
        except (ValueError, OSError):
            lease_ts = None

    lease_expired = (lease_ts is not None and lease_ts <= now)
    lease_unexpired = (lease_ts is not None and lease_ts > now)

    # 1. active: owner live (busy or attached) OR any build lane running with heartbeat <= 5 min
    has_running_lane = False
    for job in matched_jobs:
        if isinstance(job, dict) and job.get("status") == "running":
            hb = job.get("heartbeat_epoch")
            if isinstance(hb, (int, float)):
                if (now - hb) <= 300:
                    has_running_lane = True
                    break
            elif isinstance(job.get("heartbeat_at"), str):
                try:
                    hb_ts = datetime.fromisoformat(job["heartbeat_at"].replace("Z", "+00:00")).timestamp()
                    if (now - hb_ts) <= 300:
                        has_running_lane = True
                        break
                except (ValueError, OSError):
                    pass
    if not has_running_lane and lanes and isinstance(lanes, dict):
        if (lanes.get("running") or 0) > 0 and last_activity_at is not None and (now - last_activity_at) <= 300:
            has_running_lane = True

    if is_owner_live or has_running_lane:
        return "active", None, False

    # 2. quiet: last_activity <= 30 min, OR every open gate is ci with waiter: true, OR lease unexpired
    recent_activity = (last_activity_at is not None and (now - last_activity_at) <= 1800)
    open_gates = [g for g in gates if isinstance(g, dict) and g.get("state") in ("waiting", None)]
    all_ci_waiters = bool(open_gates) and all(g.get("kind") == "ci" and g.get("waiter") is True for g in open_gates)

    if recent_activity or all_ci_waiters or lease_unexpired:
        return "quiet", None, False

    # 3 & 4. idle / stale (lease expired)
    is_abandoned = False
    if not is_owner_live:
        if lease_ts is not None and (now - lease_ts) > _ABANDONED_SECONDS:
            is_abandoned = True
        elif lease_ts is None and last_activity_at is not None and (now - last_activity_at) > _ABANDONED_SECONDS:
            is_abandoned = True

    idle_since = lease_ts

    activity_timestamps = []
    if m_mtime is not None:
        activity_timestamps.append(float(m_mtime))
    if status_at is not None:
        activity_timestamps.append(float(status_at))
    if last_activity_at is not None:
        activity_timestamps.append(float(last_activity_at))

    newest_activity = max(activity_timestamps) if activity_timestamps else (lease_ts or 0.0)
    is_stale = (now - newest_activity) > 7200  # > 2 h

    liveness = "stale" if is_stale else "idle"
    return liveness, idle_since, is_abandoned


# ── Builder Pipeline ─────────────────────────────────────────────────────────


def _build_conductors_payload() -> dict[str, Any]:
    """Execute conductor roster read and projection synchronously."""
    now_ts = time.time()
    from hermes_cli.web_routers.profiles import _profile_targets

    targets = _profile_targets("GET /api/profiles/conductors")

    # 1. State.db session maps
    claude_sid_to_session: dict[str, tuple[str, str]] = {}
    workspace_sessions: list[dict[str, Any]] = []
    sessions_by_profile_sid: dict[tuple[str, str], dict[str, Any]] = {}

    for prof_name, prof_home in targets:
        db = None
        try:
            from hermes_cli.web_server_sessions import _open_session_db_for_profile
            db = _open_session_db_for_profile(prof_name, read_only=True)
        except Exception:
            db = None
        if db is None:
            db_path = Path(prof_home) / "state.db"
            if db_path.exists():
                try:
                    from hermes_cli.web_server_sessions import _open_session_db_at_path
                    db = _open_session_db_at_path(db_path, read_only=True)
                except Exception:
                    db = None
        if db is not None:
            try:
                from agent.claude_sdk_runtime_continuity import _SDK_RESUME_BINDING_PREFIX as prefix
                rows = db.list_sessions_rich(limit=500) if hasattr(db, "list_sessions_rich") else db.list_sessions()
                for s in rows:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("id") or s.get("session_id")
                    if not sid:
                        continue
                    sid_str = str(sid)
                    sessions_by_profile_sid[(prof_name, sid_str)] = s
                    raw_claude = s.get("claude_sdk_session_id")
                    if raw_claude and isinstance(raw_claude, str):
                        decoded = raw_claude
                        if decoded.startswith(prefix):
                            try:
                                d_val = json.loads(decoded[len(prefix):]).get("id")
                                if d_val:
                                    decoded = str(d_val)
                            except Exception:
                                pass
                        claude_sid_to_session[decoded] = (prof_name, sid_str)
                    last_act = s.get("last_active") or s.get("started_at")
                    workspace_sessions.append({
                        "profile": prof_name,
                        "session_id": sid_str,
                        "cwd": s.get("cwd") or s.get("workspace"),
                        "last_active_at": last_act,
                    })
            except Exception:
                pass
            finally:
                if hasattr(db, "close"):
                    db.close()

    # 2. Live CLI map and AttributionContext
    live_cli_map = collect_live_cli_map()
    ctx = AttributionContext(
        live_cli_map=live_cli_map,
        bindings={},
        sessions=sessions_by_profile_sid,
        claude_sid_to_session=claude_sid_to_session,
        workspace_sessions=workspace_sessions,
        common_repo_root=_safe_common_repo_root,
        get_branch=_safe_get_branch,
        now=now_ts,
    )

    # 3. Read marker index
    effective_home = _get_passwd_home()
    custom_index = os.environ.get("TB_MARKER_INDEX_PATH")
    index_path = Path(custom_index) if custom_index else (effective_home / ".claude" / "state" / "tb-marker-index.jsonl")

    marker_index_status = "missing"
    if index_path.exists():
        try:
            st = os.lstat(index_path)
            if not stat.S_ISREG(st.st_mode):
                marker_index_status = "unreadable"
            elif st.st_size > 2 * 1024 * 1024:
                marker_index_status = "truncated"
            else:
                marker_index_status = "ok"
        except OSError:
            marker_index_status = "unreadable"

    scan = read_marker_index(index_path, home=effective_home)
    if marker_index_status == "ok" and scan.bytes_read == 0 and index_path.exists() and os.lstat(index_path).st_size > 0:
        marker_index_status = "unreadable"

    # 4. Read markers and build objects
    builds: list[Build] = []
    state_roots: set[str] = set()
    build_matched_jobs_map: dict[str, list[dict[str, Any]]] = {}

    for entry in scan.entries:
        marker_path = entry.get("marker_path")
        if not marker_path:
            continue
        p = Path(marker_path)
        parts = p.parts
        if len(parts) >= 4 and parts[-3:] == (".claude", "state", "tb-build-active.json"):
            state_dir = p.parent
            subdirs = ()
        elif len(parts) >= 6 and parts[-5:-2] == (".claude", "state", "builds") and parts[-1] == "tb-build-active.json":
            state_dir = p.parent.parent.parent
            subdirs = ("builds", parts[-2])
        else:
            continue
        state_roots.add(str(state_dir))

        marker_rec, mtime, unreadable = _read_json_file(state_dir, "tb-build-active.json", subdirs=subdirs)
        if marker_rec is None or marker_rec.get("done") is True:
            continue

        marker_dict = dict(marker_rec)
        marker_dict["_mtime"] = mtime
        marker_dict["mtime"] = mtime
        marker_dict["marker_path"] = marker_path
        if "context_path" not in marker_dict and entry.get("context_path"):
            marker_dict["context_path"] = entry["context_path"]
        if "run_id" not in marker_dict and entry.get("run_id"):
            marker_dict["run_id"] = entry["run_id"]

        status_read = read_build_status(marker_dict, marker_path, now=now_ts)

        context_path = marker_dict.get("context_path") or str(
            state_dir.parent.parent if state_dir.name == "state" and state_dir.parent.name == ".claude" else state_dir
        )
        try:
            jobs = _list_relay_jobs(context_path, internal=True)
        except Exception:
            jobs = []

        target_rid = marker_dict.get("build_run_id") or marker_dict.get("run_id")
        target_rid_str = str(target_rid) if target_rid else ""
        matched_jobs = [j for j in jobs if isinstance(j, dict) and str(j.get("build_run_id") or "") == target_rid_str]
        build_matched_jobs_map[marker_path] = matched_jobs

        derived = derive_row_status(marker_dict, status_read, jobs, now_ts)
        attr = attribute_build(marker_dict, marker_path, status_read, ctx)

        build_obj = Build(
            marker=marker_dict,
            marker_path=marker_path,
            status=status_read,
            attribution=attr,
            derived=derived,
            last_activity_at=derived.last_activity_at,
            armed_at=marker_dict.get("armed_at"),
        )
        builds.append(build_obj)

    # 5. Group rows
    grouped_rows = group_rows(builds, ctx)

    # 6. Liveness and projection
    try:
        from tui_gateway import server as gateway_server
        gateway_sessions = getattr(gateway_server, "_sessions", {})
    except Exception:
        gateway_server = None
        gateway_sessions = {}

    active_rows: list[ConductorRow] = []
    abandoned_count = 0

    for r in grouped_rows:
        primary = r.primary
        m = primary.marker if hasattr(primary, "marker") else primary.get("marker", primary)
        m_path = str(primary.marker_path if hasattr(primary, "marker_path") else primary.get("marker_path", ""))
        d = primary.derived if hasattr(primary, "derived") else primary.get("derived")
        st = primary.status if hasattr(primary, "status") else primary.get("status")
        attr = primary.attribution if hasattr(primary, "attribution") else primary.get("attribution")

        hsid = attr.hermes_session_id if attr else None
        owner_live = "none"
        session_title = None
        session_role = None

        if hsid:
            live_sess = None
            for s_key, s_val in list(gateway_sessions.items()):
                if isinstance(s_val, dict) and not s_val.get("_finalized"):
                    sess_id = (
                        gateway_server._session_lookup_key(s_val, fallback=s_key)
                        if (gateway_server and hasattr(gateway_server, "_session_lookup_key"))
                        else s_key
                    )
                    if sess_id == hsid or s_val.get("session_key") == hsid:
                        live_sess = s_val
                        break
            if live_sess is not None:
                if live_sess.get("running"):
                    owner_live = "busy"
                elif live_sess.get("transport") and not getattr(live_sess.get("transport"), "_closed", False):
                    detached = getattr(gateway_server, "_detached_ws_transport", None) if gateway_server else None
                    if live_sess.get("transport") is not detached:
                        owner_live = "attached"
                if owner_live == "none":
                    agent = live_sess.get("agent")
                    if agent:
                        try:
                            from agent.claude_sdk_runtime_continuity import live_claude_cli_session
                            c_state, _ = live_claude_cli_session(agent)
                            if c_state == "live":
                                owner_live = "cli"
                        except Exception:
                            pass

            prof_name = attr.profile or "default"
            stored_s = sessions_by_profile_sid.get((prof_name, hsid))
            if stored_s:
                session_title = stored_s.get("title")
                session_role = stored_s.get("role")
            if not session_title and live_sess:
                session_title = live_sess.get("title")

        primary_jobs = build_matched_jobs_map.get(m_path, [])
        liveness, idle_since, is_abandoned = compute_liveness(
            owner_live=owner_live,
            lanes=d.lanes if d else None,
            matched_jobs=primary_jobs,
            last_activity_at=d.last_activity_at if d else None,
            gates=d.gates if d else [],
            lease_expires_at=m.get("lease_expires_at"),
            m_mtime=m.get("_mtime") or m.get("mtime"),
            status_at=st.status_at if st else None,
            now=now_ts,
        )

        if is_abandoned:
            abandoned_count += 1

        # Project Orchestrator
        claude_sid = m.get("session_id")
        claude_sid_short = str(claude_sid)[:8] if claude_sid else None
        orchestrator = OrchestratorModel(
            hermes_session_id=hsid,
            profile=attr.profile if attr else None,
            title=_clean_free_text(session_title, 80) if session_title else None,
            role=_clean_free_text(session_role, 32) if session_role else None,
            attribution=attr.via_short if attr else "none",
            claude_sid_short=claude_sid_short,
            live=owner_live,
        )

        # Project Project
        project_root = (attr.project if attr else None) or m.get("context_path") or str(Path(m_path).parent.parent.parent)
        project_name = Path(project_root).name if project_root else "unknown"
        root_display = _format_root_display(project_root)
        branch = (attr.branch if (attr and attr.branch) else None) or m.get("branch")
        if not branch and st and st.record and isinstance(st.record.get("build"), dict):
            branch = st.record["build"].get("branch")

        project = ProjectModel(
            name=project_name,
            root_display=root_display,
            branch=_clean_free_text(branch, 120) if branch else None,
            bound=False,
            nested_in=attr.nested_in if attr else None,
        )

        # Project Build
        lease_exp = False
        lease_raw = m.get("lease_expires_at")
        if isinstance(lease_raw, (int, float)):
            lease_exp = float(lease_raw) <= now_ts
        elif isinstance(lease_raw, str) and lease_raw:
            try:
                lease_exp = datetime.fromisoformat(lease_raw.replace("Z", "+00:00")).timestamp() <= now_ts
            except (ValueError, OSError):
                pass

        if m.get(_STALE_KEY) is True:
            marker_state = "stale"
        elif m.get("blocked") is True:
            marker_state = "blocked"
        elif lease_exp:
            marker_state = "lease_expired"
        elif m.get("waiting_on"):
            marker_state = "waiting"
        else:
            marker_state = "active"

        plan_title_raw = (
            (st.record.get("build", {}).get("plan_title") if st and st.record and isinstance(st.record.get("build"), dict) else None)
            or (d.progress.get("current_wave", {}).get("title") if d and d.progress and isinstance(d.progress.get("current_wave"), dict) else None)
            or m.get("plan_title")
            or (Path(m["plan"]).stem if m.get("plan") else None)
        )
        plan_title = _clean_free_text(plan_title_raw, 120)

        waves_obj = None
        if d and d.progress and isinstance(d.progress.get("waves"), dict):
            w_done = d.progress["waves"].get("done")
            w_total = d.progress["waves"].get("total")
            cw = d.progress.get("current_wave")
            w_curr = cw.get("index") if isinstance(cw, dict) else None
            waves_obj = WavesProgress(
                done=w_done if w_done is not None else 0,
                total=w_total,
                current=w_curr,
            )

        units_obj = None
        if d and d.progress and isinstance(d.progress.get("units"), dict):
            u = d.progress["units"]
            units_obj = UnitsProgress(
                done=u.get("done"),
                running=u.get("running"),
                failed=u.get("failed"),
                remaining=u.get("remaining"),
                total=u.get("total"),
            )

        estimate_obj = None
        if d and d.estimate and isinstance(d.estimate, dict):
            p50 = d.estimate.get("p50")
            p90 = d.estimate.get("p90")
            if p50 is not None and p90 is not None:
                estimate_obj = EstimateModel(
                    unit=str(d.estimate.get("unit") or "work_hours"),
                    p50=float(p50),
                    p90=float(p90),
                    basis=str(d.estimate.get("basis") or "other"),
                    as_of=d.estimate.get("as_of"),
                    prediction_id=d.estimate.get("prediction_id"),
                )

        lanes_obj = None
        if d and d.lanes and isinstance(d.lanes, dict):
            lanes_obj = LanesModel(
                running=d.lanes.get("running") or 0,
                stale=d.lanes.get("stale") or 0,
                cap=d.lanes.get("cap"),
            )

        build_m = BuildModel(
            run_id=str(m.get("build_run_id") or m.get("run_id") or ""),
            build_id=str(m.get("build_id")) if m.get("build_id") else (
                st.record.get("build", {}).get("build_id") if st and st.record and isinstance(st.record.get("build"), dict) else None
            ),
            plan_title=plan_title,
            armed_at=m.get("armed_at"),
            marker_state=marker_state,
            phase=d.phase if d else "build",
            waves=waves_obj,
            units=units_obj,
            current_units=d.progress.get("current_units") if d and d.progress else [],
            remaining_waves=d.progress.get("remaining_waves") if d and d.progress else [],
            estimate=estimate_obj,
            gates=d.gates if d else [],
            seats=d.seats if d else {},
            refusals=d.refusals if d else [],
            lanes=lanes_obj,
            ci=d.ci if d else [],
            owner_blockers=d.owner_blockers if d else [],
            last_activity_at=d.last_activity_at if d else None,
            liveness=liveness,
            idle_since=idle_since,
            blocked=bool(d.owner_blockers) if d else bool(m.get("blocked")),
        )

        record_m = RecordModel(
            present=st.present if st else False,
            valid=st.valid if st else False,
            reason=st.reason if st else "missing",
            fresh=st.fresh if st else False,
            status_at=st.status_at if st else None,
            seq=st.seq if st else None,
        )

        # Other builds
        extra_builds_list: list[OtherBuildModel] = []
        for eb in r.extra_builds:
            eb_m = eb.marker if hasattr(eb, "marker") else eb.get("marker", eb)
            eb_d = eb.derived if hasattr(eb, "derived") else eb.get("derived")
            eb_st = eb.status if hasattr(eb, "status") else eb.get("status")
            eb_path = str(eb.marker_path if hasattr(eb, "marker_path") else eb.get("marker_path", ""))
            eb_jobs = build_matched_jobs_map.get(eb_path, [])
            eb_liveness, _, _ = compute_liveness(
                owner_live=owner_live,
                lanes=eb_d.lanes if eb_d else None,
                matched_jobs=eb_jobs,
                last_activity_at=eb_d.last_activity_at if eb_d else None,
                gates=eb_d.gates if eb_d else [],
                lease_expires_at=eb_m.get("lease_expires_at"),
                m_mtime=eb_m.get("_mtime") or eb_m.get("mtime"),
                status_at=eb_st.status_at if eb_st else None,
                now=now_ts,
            )
            eb_title = _clean_free_text(
                (eb_st.record.get("build", {}).get("plan_title") if eb_st and eb_st.record and isinstance(eb_st.record.get("build"), dict) else None)
                or eb_m.get("plan_title")
                or (Path(eb_m["plan"]).stem if eb_m.get("plan") else None),
                120,
            )
            extra_builds_list.append(OtherBuildModel(
                run_id=str(eb_m.get("build_run_id") or eb_m.get("run_id") or ""),
                plan_title=eb_title,
                liveness=eb_liveness,
                waves=eb_d.waves if eb_d else None,
            ))

        key = hashlib.sha256(m_path.encode("utf-8")).hexdigest()[:16]
        active_rows.append(ConductorRow(
            key=key,
            orchestrator=orchestrator,
            project=project,
            build=build_m,
            record=record_m,
            field_sources=dict(d.field_sources) if d else {},
            other_builds=extra_builds_list,
            abandoned=is_abandoned,
        ))

    response_payload = ConductorsResponse(
        schema="hermes-conductors/v1",
        generated_at=now_ts,
        rows=active_rows,
        sources=ConductorsSources(
            marker_index=marker_index_status,
            state_dirs=len(state_roots),
            skipped=scan.skipped,
        ),
        abandoned=abandoned_count,
    )
    return response_payload.model_dump(by_alias=True)


# ── Coalesced Read & Cache ───────────────────────────────────────────────────


_CACHE: dict[str, Any] = {"time": 0.0, "result": None}
_CACHE_LOCK = threading.Lock()


def _clear_cache() -> None:
    """Clear 5 s cache and high-water caches (testing hook)."""
    with _CACHE_LOCK:
        _CACHE["time"] = 0.0
        _CACHE["result"] = None


@functools.partial(coalesced_read, thread_runner=lambda func: asyncio.to_thread(func))
def _read_conductors_coalesced() -> dict[str, Any]:
    return _build_conductors_payload()


# ── Route Endpoint ───────────────────────────────────────────────────────────


@router.get("/api/profiles/conductors", response_model=ConductorsResponse)
async def get_conductors(
    fresh: int = Query(0, ge=0, le=1),
):
    """GET /api/profiles/conductors returning hermes-conductors/v1."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_result = _CACHE["result"]
        cached_time = _CACHE["time"]

    if cached_result is not None:
        age = now - cached_time
        # Reused within 5s; ?fresh=1 bypasses reuse only when age >= 5s
        if age < _REUSE_SECONDS:
            return copy.deepcopy(cached_result)

    payload = await _read_conductors_coalesced()
    with _CACHE_LOCK:
        _CACHE["result"] = payload
        _CACHE["time"] = time.monotonic()
    return copy.deepcopy(payload)
