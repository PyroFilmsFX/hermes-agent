#!/usr/bin/env python3
"""cntrl session router — the registry a host session reads to route work.

Hermes sessions now carry a peer-addressable name (``agent.claude_agent_sdk.
session_name``), so the Claude Code ``ListAgents`` / ``SendMessage`` pair can
reach them. What was missing is the map: which session owns which piece of
work. This file is that map, plus the smallest CLI that maintains it.

The host reads it with the tools it already has (``read_file``, or ``Bash``
running ``router.py list``), matches a request to a route, then hands off with
``SendMessage``. No new MCP server, no core edit.

Registry location, in order:
  $CNTRL_ROUTER_ROUTES        explicit override
  $HERMES_HOME/cntrl-routes.json
  ~/.hermes/cntrl-routes.json

Schema (one object per route):
  key      short slug the host matches on           "kanban"
  session  peer name to SendMessage                 "hermes:kanban sync"
  owns     what this session is responsible for     "cntrl <-> hermes board sync"
  repo     optional working directory
  notes    optional free text
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = 1


def routes_path() -> Path:
    explicit = os.environ.get("CNTRL_ROUTER_ROUTES", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "cntrl-routes.json"


def load() -> Dict[str, Any]:
    p = routes_path()
    if not p.exists():
        return {"version": SCHEMA_VERSION, "routes": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt registry must fail loudly: silently routing to nobody is
        # worse than stopping, because the work just disappears.
        raise SystemExit(f"cntrl-router: cannot read {p}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise SystemExit(f"cntrl-router: {p} is not a route registry")
    data.setdefault("version", SCHEMA_VERSION)
    return data


def save(data: Dict[str, Any]) -> Path:
    p = routes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)  # atomic: a half-written registry routes nowhere
    return p


def _routes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in data.get("routes", []) if isinstance(r, dict)]


def cmd_list(args: argparse.Namespace) -> int:
    data = load()
    rows = _routes(data)
    if args.json:
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    if not rows:
        print(f"no routes yet ({routes_path()})")
        print("add one:  router.py add <key> --session <peer name> --owns <what it owns>")
        return 0
    width = max(len(str(r.get("key", ""))) for r in rows)
    for r in sorted(rows, key=lambda x: str(x.get("key", ""))):
        print(f"{str(r.get('key','')):<{width}}  ->  {r.get('session','?')}   {r.get('owns','')}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    data = load()
    rows = _routes(data)
    entry = {"key": args.key, "session": args.session, "owns": args.owns or ""}
    if args.repo:
        entry["repo"] = args.repo
    if args.notes:
        entry["notes"] = args.notes
    for i, r in enumerate(rows):
        if str(r.get("key")) == args.key:
            rows[i] = entry
            break
    else:
        rows.append(entry)
    data["routes"] = rows
    print(f"{args.key} -> {args.session}  ({save(data)})")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    data = load()
    rows = _routes(data)
    kept = [r for r in rows if str(r.get("key")) != args.key]
    if len(kept) == len(rows):
        print(f"no route named {args.key!r}", file=sys.stderr)
        return 1
    data["routes"] = kept
    print(f"removed {args.key}  ({save(data)})")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """Best-effort match of free text to a route. Prints nothing on no match:
    the host must then ask, never guess a destination."""
    needle = " ".join(args.text).lower().strip()
    if not needle:
        return 1
    best: List[Dict[str, Any]] = []
    for r in _routes(load()):
        hay = " ".join(
            str(r.get(k, "")) for k in ("key", "session", "owns", "repo", "notes")
        ).lower()
        key = str(r.get("key", "")).lower()
        if key and key in needle:
            best.insert(0, r)
        elif any(w in hay for w in needle.split() if len(w) > 3):
            best.append(r)
    if not best:
        return 1
    json.dump(best[:3], sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="router.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="print every route")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="add or replace a route")
    p.add_argument("key")
    p.add_argument("--session", required=True, help="peer name from ListAgents")
    p.add_argument("--owns", default="", help="what this session is responsible for")
    p.add_argument("--repo", default="")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="drop a route")
    p.add_argument("key")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("find", help="match free text to routes (exit 1 = no match)")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("path", help="print the registry path")
    p.set_defaults(func=lambda a: (print(routes_path()), 0)[1])

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
