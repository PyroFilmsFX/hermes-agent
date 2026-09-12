"""Slash names the claude-agent-sdk lane dispatches natively.

Every Hermes slash dispatcher (CLI ``process_command``, tui_gateway ``slash.exec`` /
``command.dispatch``, gateway inbound) resolves ``/name`` against Hermes' own
registries: built-ins, quick commands, plugin commands, bundles, HERMES_HOME
skills. Claude Code plugin skills loaded into the SDK lane through
``agent.claude_agent_sdk.plugins`` live under ``~/.claude/plugins`` and are known
only to the spawned CLI — so ``/tb-ship`` was rejected ("Unknown command") before
the model ever saw it, on every surface.

The Agent SDK dispatches a user-invocable skill when the prompt text is
``/<name> [args]`` (docs: agent-sdk/slash-commands, "Dispatch commands by name"),
and its ``system/init`` message lists the available names in ``slash_commands``.
Proven 2026-09-11 against conductor 3.60.1 through the real CLI: prompt
``/tb-mode`` expanded the skill; init listed the plugin's commands as
``conductor:tb-*`` (the bare name works too).

This module answers one question for all dispatchers: is this slash — unknown to
Hermes — a skill the SDK lane will expand? If so the dispatcher forwards the raw
``/name args`` text as the turn's prompt instead of rejecting it. Hermes-owned
names always win: callers consult their own registries first.

Two sources, unioned:

* **static** — ``skills/<dir>/SKILL.md`` under each configured plugin root. Works
  before the first turn, when no CLI session (and no init message) exists yet.
* **live** — the ``slash_commands`` list the CLI reported on init, exposed by
  ``ClaudeAgentSdkSession.slash_commands`` once a session is up.

Why not ``skills.external_dirs``: Hermes would expand the SKILL.md itself, and
23 of conductor's 39 skills reference ``${CLAUDE_PLUGIN_ROOT}``, which only the
CLI resolves.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

SDK_PROVIDER = "claude-agent-sdk"

_FALSE_WORDS = frozenset({"false", "no", "0", "off"})


def is_claude_sdk_provider(provider: object) -> bool:
    """True for the claude-agent-sdk provider name (``_``/``-`` and case insensitive)."""
    return str(provider or "").strip().lower().replace("_", "-") == SDK_PROVIDER


def active_provider() -> str:
    """``model.provider`` from the active config ('' when unreadable). Honors the
    HERMES_HOME override a profile-scoped gateway session binds."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        return str(((cfg.get("model") or {}).get("provider")) or "").strip()
    except Exception:
        return ""


def _skill_frontmatter(skill_md: Path) -> dict[str, str]:
    """Flat ``key: value`` pairs from the first ``---`` fenced block. Enough for
    ``name`` / ``user-invocable``; nested YAML values are ignored on purpose (no
    PyYAML dependency on the slash hot path)."""
    out: dict[str, str] = {}
    try:
        with skill_md.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return out
            for line in fh:
                stripped = line.strip()
                if stripped == "---":
                    break
                if not stripped or stripped.startswith("#") or line[:1].isspace():
                    continue
                key, sep, value = stripped.partition(":")
                if sep and key.strip():
                    out[key.strip().lower()] = value.strip().strip("\"'")
    except OSError:
        pass
    return out


def _plugin_name(root: Path) -> str:
    """``name`` from ``.claude-plugin/plugin.json``; the directory name otherwise."""
    try:
        with (root / ".claude-plugin" / "plugin.json").open("r", encoding="utf-8") as fh:
            name = (json.load(fh) or {}).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, ValueError):
        pass
    return root.name


def configured_plugin_roots() -> list[str]:
    """Plugin roots the SDK lane loads (``agent.claude_agent_sdk.plugins``, validated)."""
    try:
        from agent.transports.claude_agent_sdk_session import _configured_plugins

        return [str(entry.get("path") or "") for entry in _configured_plugins() if entry.get("path")]
    except Exception:
        logger.debug("configured plugin roots unavailable", exc_info=True)
        return []


def plugin_skill_slash_names(plugin_roots: Optional[Iterable[str]] = None) -> set[str]:
    """User-invocable skill names under ``<root>/skills/*/SKILL.md`` for every
    plugin root — both the bare ``name`` and the namespaced ``plugin:name`` the
    CLI lists on init. ``user-invocable: false`` skills are excluded (the CLI
    refuses to dispatch them by name too)."""
    roots = list(plugin_roots) if plugin_roots is not None else configured_plugin_roots()
    names: set[str] = set()
    for raw_root in roots:
        root = Path(os.path.expanduser(str(raw_root)))
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            continue
        plugin = _plugin_name(root)
        try:
            skill_files = sorted(skills_dir.glob("*/SKILL.md"))
        except OSError:
            continue
        for skill_md in skill_files:
            fm = _skill_frontmatter(skill_md)
            if fm.get("user-invocable", "true").lower() in _FALSE_WORDS:
                continue
            name = (fm.get("name") or skill_md.parent.name).strip()
            if not name:
                continue
            names.add(name)
            names.add(f"{plugin}:{name}")
    return names


def plugin_skill_commands(plugin_roots: Optional[Iterable[str]] = None) -> dict[str, dict]:
    """Skill-command entries shaped like ``agent.skill_commands.get_skill_commands()``
    (``{"/name": {name, description, skill_dir, ...}}``) for every user-invocable
    plugin skill — bare names only, so the popover lists ``/tb-ship`` once.
    ``source`` marks them so no consumer mistakes one for a HERMES_HOME skill."""
    roots = list(plugin_roots) if plugin_roots is not None else configured_plugin_roots()
    out: dict[str, dict] = {}
    for raw_root in roots:
        root = Path(os.path.expanduser(str(raw_root)))
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            continue
        plugin = _plugin_name(root)
        try:
            skill_files = sorted(skills_dir.glob("*/SKILL.md"))
        except OSError:
            continue
        for skill_md in skill_files:
            fm = _skill_frontmatter(skill_md)
            if fm.get("user-invocable", "true").lower() in _FALSE_WORDS:
                continue
            name = (fm.get("name") or skill_md.parent.name).strip()
            if not name:
                continue
            key = f"/{name}"
            out.setdefault(key, {
                "name": name,
                "description": fm.get("description") or f"{plugin} plugin skill",
                "skill_dir": str(skill_md.parent),
                "skill_md_path": str(skill_md),
                "plugin": plugin,
                "source": "claude-plugin",
            })
    return out


def merged_skill_commands(base, *, provider: Optional[str] = None) -> dict[str, dict]:
    """``base`` (Hermes' own skill commands) plus the SDK lane's plugin skills, for
    completion surfaces. Off the SDK lane it is just ``dict(base)``. Hermes wins
    every collision: an existing ``base`` key, or a name the command registry
    owns (``/plan``), is never shadowed by a plugin skill."""
    merged = dict(base or {})
    if not is_claude_sdk_provider(provider if provider is not None else active_provider()):
        return merged
    try:
        from hermes_cli.commands import resolve_command
    except Exception:  # pragma: no cover - registry import failure
        resolve_command = None
    for key, info in plugin_skill_commands().items():
        if key in merged:
            continue
        if resolve_command is not None:
            try:
                if resolve_command(key.lstrip("/")) is not None:
                    continue
            except Exception:
                pass
        merged[key] = info
    return merged


def sdk_slash_names(provider: Optional[str] = None) -> set[str]:
    """Bare + namespaced plugin skill names when the SDK lane is active; empty otherwise.
    For prefix expansion (``/tb-sh`` → ``/tb-ship``) alongside Hermes' own registries."""
    if not is_claude_sdk_provider(provider if provider is not None else active_provider()):
        return set()
    return plugin_skill_slash_names()


def resolve_sdk_slash(
    command: str,
    *,
    provider: Optional[str] = None,
    live_names: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """``/name [args]`` → the prompt to forward when the SDK lane will expand it,
    else None.

    ``provider`` defaults to the active config's ``model.provider``; anything but
    claude-agent-sdk resolves to None (other lanes have no CLI to expand a
    skill). ``live_names`` is the CLI's init ``slash_commands`` when a session
    exists; the static plugin scan is always unioned in. Matching is exact,
    then case-insensitive, then with ``_``→``-`` (the Telegram round-trip)."""
    text = (command or "").strip()
    if not text.startswith("/"):
        return None
    head, _, tail = text.partition(" ")
    name = head[1:].strip()
    if not name or name == "/":
        return None
    if not is_claude_sdk_provider(provider if provider is not None else active_provider()):
        return None
    known = {str(n).strip() for n in (live_names or ()) if str(n).strip()}
    known |= plugin_skill_slash_names()
    if not known:
        return None
    lowered = {k.lower(): k for k in known}
    resolved = None
    for candidate in (name, name.replace("_", "-")):
        if candidate in known:
            resolved = candidate
            break
        if candidate.lower() in lowered:
            resolved = lowered[candidate.lower()]
            break
    if resolved is None:
        return None
    return f"/{resolved} {tail.strip()}".strip()


__all__ = [
    "SDK_PROVIDER",
    "active_provider",
    "configured_plugin_roots",
    "is_claude_sdk_provider",
    "merged_skill_commands",
    "plugin_skill_commands",
    "plugin_skill_slash_names",
    "resolve_sdk_slash",
    "sdk_slash_names",
]
