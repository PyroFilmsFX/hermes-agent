#!/usr/bin/env python3
"""cntrl session groups — a project layer between profiles and sessions.

Hermes has profiles (one HERMES_HOME each) and sessions. Nothing in between,
so a busy profile is a flat list of hundreds of sessions and finding "the ones
about the fork" means reading titles. This adds a name you choose.

Out-of-tree by design: groups live in their OWN table in the Hermes state db,
keyed by session id. Hermes core never reads it, so it survives every upstream
merge. Membership is read back by joining against ``sessions``.

  groups.py tag fork <session-id>...   put sessions in a group
  groups.py ls fork                    sessions in a group, newest first
  groups.py ls                         every group with a count
  groups.py auto                       suggest groups from git repo root
  groups.py untag <session-id>...      remove from its group
  groups.py resume fork                print the newest id (feed to --resume)

Db: $HERMES_HOME/state.db, or $CNTRL_GROUPS_DB.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional

TABLE = "cntrl_session_groups"


def db_path() -> Path:
    explicit = os.environ.get("CNTRL_GROUPS_DB", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "state.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    if not p.exists():
        raise SystemExit(f"cntrl-groups: no Hermes state db at {p} (set HERMES_HOME)")
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Our own table only. Hermes' schema is never touched, which is what keeps
    # this out-of-tree across upstream merges.
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " session_id TEXT PRIMARY KEY,"
        " group_name TEXT NOT NULL,"
        " added_at REAL NOT NULL)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_name ON {TABLE}(group_name)")
    conn.commit()
    return conn


def _known_session_ids(conn: sqlite3.Connection, ids: List[str]) -> set:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM sessions WHERE id IN ({marks})", ids).fetchall()
    return {r["id"] for r in rows}


def cmd_tag(args: argparse.Namespace) -> int:
    conn = connect()
    known = _known_session_ids(conn, args.session_id)
    # Tagging an id that does not exist is a typo, not a new session: it would
    # sit in the group forever and never show up in a listing.
    unknown = [s for s in args.session_id if s not in known]
    if unknown and not args.force:
        print("no such session: " + ", ".join(unknown), file=sys.stderr)
        print("(re-run with --force to tag anyway)", file=sys.stderr)
        return 1
    now = time.time()
    with conn:
        conn.executemany(
            f"INSERT INTO {TABLE}(session_id, group_name, added_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET group_name=excluded.group_name, added_at=excluded.added_at",
            [(s, args.group, now) for s in args.session_id],
        )
    print(f"{len(args.session_id)} session(s) -> {args.group}")
    return 0


def cmd_untag(args: argparse.Namespace) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            f"DELETE FROM {TABLE} WHERE session_id IN ({','.join('?' * len(args.session_id))})",
            args.session_id,
        )
    if not cur.rowcount:
        print("nothing to untag", file=sys.stderr)
        return 1
    print(f"untagged {cur.rowcount}")
    return 0


def _rows_for_group(conn: sqlite3.Connection, group: str, limit: int):
    return conn.execute(
        "SELECT s.id, s.title, s.started_at, s.last_activity_at, s.message_count, s.git_branch "
        f"FROM {TABLE} g JOIN sessions s ON s.id = g.session_id "
        "WHERE g.group_name = ? "
        "ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC LIMIT ?",
        (group, limit),
    ).fetchall()


def cmd_ls(args: argparse.Namespace) -> int:
    conn = connect()
    if not args.group:
        rows = conn.execute(
            f"SELECT group_name, COUNT(*) n FROM {TABLE} GROUP BY group_name ORDER BY n DESC"
        ).fetchall()
        if not rows:
            print("no groups yet — groups.py tag <group> <session-id>")
            return 0
        width = max(len(r["group_name"]) for r in rows)
        for r in rows:
            print(f"{r['group_name']:<{width}}  {r['n']}")
        return 0
    rows = _rows_for_group(conn, args.group, args.limit)
    if not rows:
        print(f"no sessions in {args.group!r}", file=sys.stderr)
        return 1
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["last_activity_at"] or r["started_at"]))
        title = (r["title"] or "(untitled)")[:46]
        branch = f"  [{r['git_branch']}]" if r["git_branch"] else ""
        print(f"{r['id']}  {when}  {r['message_count']:>4} msg  {title}{branch}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Print the newest session id in a group — feed straight to `hermes --resume`."""
    rows = _rows_for_group(connect(), args.group, 1)
    if not rows:
        print(f"no sessions in {args.group!r}", file=sys.stderr)
        return 1
    print(rows[0]["id"])
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    """Suggest groups from git repo root — the grouping that already exists in
    the data. Prints tag commands; it never writes on its own."""
    conn = connect()
    rows = conn.execute(
        "SELECT s.git_repo_root root, COUNT(*) n FROM sessions s "
        f"LEFT JOIN {TABLE} g ON g.session_id = s.id "
        "WHERE s.git_repo_root IS NOT NULL AND s.git_repo_root != '' AND g.session_id IS NULL "
        "GROUP BY s.git_repo_root HAVING n >= ? ORDER BY n DESC",
        (args.min,),
    ).fetchall()
    if not rows:
        print("nothing to suggest (all grouped, or no git metadata)")
        return 0
    print("# untagged sessions by repo — run what you want:")
    for r in rows:
        name = Path(r["root"]).name or "root"
        print(
            f'python3 "$(dirname "$0")/groups.py" tag {name} '
            f'$(sqlite3 "$HERMES_HOME/state.db" '
            f"\"SELECT id FROM sessions WHERE git_repo_root='{r['root']}'\")"
            f"   # {r['n']} session(s)"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="groups.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tag", help="put sessions in a group")
    p.add_argument("group")
    p.add_argument("session_id", nargs="+")
    p.add_argument("--force", action="store_true", help="tag ids not in the sessions table")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("untag", help="remove sessions from their group")
    p.add_argument("session_id", nargs="+")
    p.set_defaults(func=cmd_untag)

    p = sub.add_parser("ls", help="list groups, or the sessions in one")
    p.add_argument("group", nargs="?")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("resume", help="print the newest session id in a group")
    p.add_argument("group")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("auto", help="suggest groups from git repo root")
    p.add_argument("--min", type=int, default=2)
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("path", help="print the db path")
    p.set_defaults(func=lambda a: (print(db_path()), 0)[1])

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
