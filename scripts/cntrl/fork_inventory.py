#!/usr/bin/env python3
"""Regenerate the machine-derived half of the cntrl fork inventory.

Writes:
  docs/cntrl/fork-inventory.generated.md   — full snapshot (overwritten)
  docs/cntrl/fork-inventory-log.md         — one row per run (append-only)

The hand-written half (why each core edit exists) stays in
docs/cntrl/fork-inventory.md. This script only reports what git can prove:
which commits we carry, when, which core files they touch, and how far we are
from upstream main and from the PR #65982 head.

Run after every upstream sync and after any commit that touches a core file:
    .venv/bin/python scripts/cntrl/fork_inventory.py
Add --check to exit 1 when a carried commit touches a core file that is not
listed in fork-inventory.md §3 (use it as a pre-commit / CI guard).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "cntrl"
GENERATED = DOCS / "fork-inventory.generated.md"
LOG = DOCS / "fork-inventory-log.md"
HANDWRITTEN = DOCS / "fork-inventory.md"

UPSTREAM = "origin/main"
PR_REF = "pr-65982-sep7"  # refreshed by: git fetch origin pull/65982/head:<ref>

# Paths that carry no merge cost: anything here is not a "core edit".
OUT_OF_TREE = (
    "cntrl-plugins/",
    "docs/cntrl/",
    "docs/councils/",
    "scripts/cntrl/",
    ".claude/skills/",
    "CNTRL-HERMES.md",
    "CLAUDE.md",
    ".gitignore",
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()


def ref_exists(ref: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "-q", ref], cwd=REPO, capture_output=True
    ).returncode == 0


def counts(a: str, b: str) -> tuple[int, int]:
    left, right = git("rev-list", "--left-right", "--count", f"{a}...{b}").split()
    return int(left), int(right)


def carried_commits(base: str) -> list[dict]:
    out = git("log", "--reverse", "--format=%H%x1f%h%x1f%cs%x1f%s", f"{base}..HEAD")
    rows = []
    for line in out.splitlines():
        full, short, date, subject = line.split("\x1f")
        files = git("show", "--name-only", "--format=", full).splitlines()
        files = [f for f in files if f]
        core = [f for f in files if not f.startswith(OUT_OF_TREE) and f not in OUT_OF_TREE]
        rows.append({"sha": short, "date": date, "subject": subject, "files": files, "core": core})
    return rows


def listed_core_files() -> set[str]:
    """Backtick-quoted paths in §3 of the hand-written inventory."""
    if not HANDWRITTEN.exists():
        return set()
    text = HANDWRITTEN.read_text()
    m = re.search(r"## 3\..*?(?=\n## 4\.)", text, re.S)
    section = m.group(0) if m else ""
    return set(re.findall(r"`([^`\s]+\.[a-z]+)`", section))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if a core file is unlisted in §3")
    args = ap.parse_args()

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    today = dt.date.today().isoformat()
    head = git("rev-parse", "--short", "HEAD")
    ahead_main, behind_main = counts("HEAD", UPSTREAM)
    base_main = git("merge-base", "HEAD", UPSTREAM)
    base_main_date = git("log", "-1", "--format=%cs", base_main)

    pr_line = "PR ref not fetched (git fetch origin pull/65982/head:%s)" % PR_REF
    carry_base = base_main
    if ref_exists(PR_REF):
        pr_ahead, pr_behind = counts(PR_REF, UPSTREAM)
        on_pr = git("merge-base", "--is-ancestor", PR_REF, "HEAD") == "" and subprocess.run(
            ["git", "merge-base", "--is-ancestor", PR_REF, "HEAD"], cwd=REPO
        ).returncode == 0
        pr_line = f"`{PR_REF}` = {git('rev-parse', '--short', PR_REF)} · {pr_ahead} ahead / {pr_behind} behind {UPSTREAM} · " + (
            "HEAD is built on it" if on_pr else "HEAD is NOT built on it"
        )
        if on_pr:
            carry_base = PR_REF

    rows = carried_commits(carry_base)
    core_files: dict[str, list[str]] = {}
    for r in rows:
        for f in r["core"]:
            core_files.setdefault(f, []).append(r["sha"])

    listed = listed_core_files()
    unlisted = sorted(f for f in core_files if f not in listed)

    lines = [
        "# Fork inventory — generated",
        "",
        f"Generated {today} by `scripts/cntrl/fork_inventory.py`. Do not edit; edit `fork-inventory.md` for the why.",
        "",
        "| | |",
        "|---|---|",
        f"| branch | `{branch}` @ {head} |",
        f"| vs `{UPSTREAM}` | {ahead_main} ahead / {behind_main} behind · common base {base_main[:10]} ({base_main_date}) |",
        f"| PR #65982 | {pr_line} |",
        f"| carried commits (since {carry_base[:10] if len(carry_base) > 20 else carry_base}) | {len(rows)} |",
        f"| core files touched | {len(core_files)} ({len(unlisted)} unlisted in §3) |",
        "",
        "## Carried commits (oldest first)",
        "",
        "| date | sha | subject | core files |",
        "|---|---|---|---|",
    ]
    for r in rows:
        core = ", ".join(f"`{f}`" for f in r["core"]) or "—"
        subj = r["subject"].replace("|", "\\|")[:110]
        lines.append(f"| {r['date']} | {r['sha']} | {subj} | {core} |")
    lines += ["", "## Core files we touch", "", "| file | commits | listed in §3 |", "|---|---|---|"]
    for f in sorted(core_files):
        lines.append(f"| `{f}` | {', '.join(core_files[f])} | {'yes' if f in listed else '**NO**'} |")
    lines.append("")
    GENERATED.write_text("\n".join(lines))

    if not LOG.exists():
        LOG.write_text(
            "# Fork inventory — run log (append-only)\n\n"
            "| date | branch | head | ahead/behind main | carried | core files | unlisted |\n"
            "|---|---|---|---|---|---|---|\n"
        )
    with LOG.open("a") as fh:
        fh.write(
            f"| {today} | `{branch}` | {head} | {ahead_main}/{behind_main} | {len(rows)} | {len(core_files)} | {len(unlisted)} |\n"
        )

    print(f"wrote {GENERATED.relative_to(REPO)} and appended {LOG.relative_to(REPO)}")
    print(f"{ahead_main} ahead / {behind_main} behind {UPSTREAM}; {len(rows)} carried; {len(core_files)} core files")
    if unlisted:
        print("core files NOT listed in fork-inventory.md §3:")
        for f in unlisted:
            print("  -", f)
        if args.check:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
