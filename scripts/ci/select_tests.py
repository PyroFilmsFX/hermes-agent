#!/usr/bin/env python3
"""Select tests for push CI runs based on path classification.

Prints a JSON object:
    {"mode": "full"|"selected"|"none", "files": [...], "desktop": bool, "desktop_files": [...]}

Rules:
a. Full whenever any changed path matches:
   pyproject.toml, uv.lock, pm/**, scripts/run_tests*.sh, scripts/run_tests_parallel.py,
   scripts/ci/**, .github/**, any conftest.py, tests/fixtures/**, or hermes_constants.py.
   Also full when more than 40 Python source files changed.
b. A changed tests/**/test_*.py file is selected directly.
c. For a changed source file X.py (dotted module m), select test files whose text
   imports m or `from <parent> import <leaf>`, found with a plain text grep over tests/.
   Also select tests/**/test_<stem>*.py.
d. Changes only under apps/**, website/**, docs/**, *.md or _ops/** give mode "none" for Python.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple


def is_zero_or_missing(sha: str | None) -> bool:
    """Return True if sha is None, empty, 'null', or all zeros."""
    if not sha or sha in ("null", "None", ""):
        return True
    return set(sha) == {"0"}


def resolve_diff_range(
    before: str | None,
    sha: str,
    base_branch: str = "origin/cntrl-hermes-worker",
    repo_root: Path | None = None,
) -> Tuple[str, str]:
    """Resolve base and head refs for git diff.

    When before is missing or all zeros (e.g. new branch push), diffs against
    the merge-base with base_branch.
    """
    if not is_zero_or_missing(before):
        return str(before), str(sha)

    cwd = repo_root or Path.cwd()
    branch_name = base_branch.split("/")[-1]
    candidates = [
        base_branch,
        f"origin/{branch_name}",
        f"myfork/{branch_name}",
        branch_name,
        f"remotes/origin/{branch_name}",
        f"remotes/myfork/{branch_name}",
    ]

    for candidate in candidates:
        try:
            proc = subprocess.run(
                ["git", "merge-base", candidate, str(sha)],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            mb = proc.stdout.strip()
            if mb:
                return mb, str(sha)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    # If merge-base failed, try fetching depth before falling back
    for remote in ("origin", "myfork"):
        try:
            subprocess.run(
                ["git", "fetch", "--depth=100", remote, branch_name],
                cwd=cwd,
                capture_output=True,
                check=False,
            )
            proc = subprocess.run(
                ["git", "merge-base", f"{remote}/{branch_name}", str(sha)],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            mb = proc.stdout.strip()
            if mb:
                return mb, str(sha)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    return base_branch, str(sha)


def get_changed_files_git(
    base_ref: str,
    head_ref: str,
    repo_root: Path | None = None,
) -> List[str]:
    """Run git diff --name-only between base_ref and head_ref."""
    cwd = repo_root or Path.cwd()
    cmd = ["git", "diff", "--name-only", f"{base_ref}..{head_ref}"]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
        return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", f"{head_ref}~1..{head_ref}"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]
        except subprocess.CalledProcessError:
            return []


def _is_rule_a_match(norm: str) -> bool:
    """Rule a: global build/test/infra files that trigger full suite."""
    if norm in (
        "pyproject.toml",
        "uv.lock",
        "scripts/run_tests_parallel.py",
        "hermes_constants.py",
    ):
        return True
    if norm == "pm" or norm.startswith("pm/"):
        return True
    if fnmatch.fnmatch(norm, "scripts/run_tests*.sh"):
        return True
    if norm == "scripts/ci" or norm.startswith("scripts/ci/"):
        return True
    if norm == ".github" or norm.startswith(".github/"):
        return True
    if norm == "conftest.py" or norm.endswith("/conftest.py"):
        return True
    if norm == "tests/fixtures" or norm.startswith("tests/fixtures/"):
        return True
    return False


def _is_rule_d_match(norm: str) -> bool:
    """Rule d: directories/files that do not touch Python suite."""
    return (
        norm.startswith("apps/")
        or norm.startswith("website/")
        or norm.startswith("docs/")
        or norm.endswith(".md")
        or norm.startswith("_ops/")
    )


def _is_python_source(norm: str) -> bool:
    """A python source file outside tests."""
    return norm.endswith(".py") and not norm.startswith("tests/")


def _source_module_info(norm: str) -> Tuple[str, str, str | None, str]:
    """Return (stem, dotted_module, parent, leaf) for a python source file."""
    path_obj = Path(norm)
    stem = path_obj.stem
    if norm.endswith("/__init__.py"):
        parts = list(path_obj.parent.parts)
        stem = parts[-1] if parts else stem
    else:
        parts = list(path_obj.with_suffix("").parts)

    dotted_module = ".".join(parts)
    if "." in dotted_module:
        parent, leaf = dotted_module.rsplit(".", 1)
    else:
        parent, leaf = None, dotted_module

    return stem, dotted_module, parent, leaf


def _find_test_files_for_source(
    norm: str,
    tests_root: Path,
    repo_root: Path,
) -> Set[str]:
    """Find tests matching stem or importing dotted module m or parent.leaf."""
    selected: Set[str] = set()
    stem, dotted_module, parent, leaf = _source_module_info(norm)

    # 1. Stem match: tests/**/test_<stem>*.py
    for test_file in tests_root.rglob(f"test_{stem}*.py"):
        if test_file.is_file():
            rel = str(test_file.relative_to(repo_root)).replace("\\", "/")
            selected.add(rel)

    # 2. Text scan for import patterns
    # Patterns for dotted_module m:
    # - import ... <m> ...
    # - from <m> import ...
    # - from <parent> import ... <leaf> ...
    import_m_re = re.compile(rf"\bimport\s+([a-zA-Z0-9_.,\s]*\b)?{re.escape(dotted_module)}\b")
    from_m_re = re.compile(rf"\bfrom\s+{re.escape(dotted_module)}\s+import\b")
    from_parent_re = (
        re.compile(
            rf"\bfrom\s+{re.escape(parent)}\s+import\s+(?:\([^)]*\)|[^\n;]+)",
            re.MULTILINE,
        )
        if parent
        else None
    )
    leaf_word_re = re.compile(rf"\b{re.escape(leaf)}\b") if leaf else None

    for test_file in tests_root.rglob("test_*.py"):
        if not test_file.is_file():
            continue
        rel = str(test_file.relative_to(repo_root)).replace("\\", "/")
        if rel in selected:
            continue

        try:
            content = test_file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

        if import_m_re.search(content) or from_m_re.search(content):
            selected.add(rel)
            continue

        if from_parent_re and leaf_word_re:
            for match in from_parent_re.finditer(content):
                if leaf_word_re.search(match.group(0)):
                    selected.add(rel)
                    break

    return selected


def select_tests(
    changed_files: Sequence[str],
    repo_root: Path | None = None,
) -> Dict[str, Any]:
    """Classify changed files and return test selection result."""
    root = (repo_root or Path.cwd()).resolve()
    normalized = [p.replace("\\", "/").strip() for p in changed_files if p.strip()]

    # Desktop detection: any change under apps/desktop/** (tests) or apps/shared/** (the desktop
    # typecheck compiles apps/shared/src into both the renderer and the electron main build)
    desktop = False
    desktop_files: Set[str] = set()
    for norm in normalized:
        if norm.startswith("apps/shared/"):
            desktop = True
            continue
        if norm == "apps/desktop" or norm.startswith("apps/desktop/"):
            desktop = True
            rel = norm[len("apps/desktop/") :] if norm.startswith("apps/desktop/") else ""
            if not rel:
                continue
            if rel.endswith(".test.ts") or rel.endswith(".test.tsx"):
                if (root / norm).is_file():
                    desktop_files.add(rel)
            else:
                # Look for sibling test files in apps/desktop
                p_rel = Path(rel)
                stem = p_rel.name.split(".", 1)[0]
                parent_dir = root / "apps/desktop" / p_rel.parent
                if parent_dir.is_dir():
                    for cand in parent_dir.glob(f"{stem}.test.ts*"):
                        if cand.is_file() and (cand.name.endswith(".test.ts") or cand.name.endswith(".test.tsx")):
                            rel_cand = str(cand.relative_to(root / "apps/desktop")).replace("\\", "/")
                            desktop_files.add(rel_cand)

    # Empty diff -> mode none
    if not normalized:
        return {
            "mode": "none",
            "files": [],
            "desktop": desktop,
            "desktop_files": sorted(desktop_files),
        }

    # Rule a: trigger full immediately on match
    if any(_is_rule_a_match(norm) for norm in normalized):
        return {
            "mode": "full",
            "files": [],
            "desktop": desktop,
            "desktop_files": sorted(desktop_files),
        }

    # Rule a escalation: >40 Python source files changed
    py_sources = [norm for norm in normalized if _is_python_source(norm)]
    if len(py_sources) > 40:
        return {
            "mode": "full",
            "files": [],
            "desktop": desktop,
            "desktop_files": sorted(desktop_files),
        }

    # Rule d: changes only under apps/**, website/**, docs/**, *.md or _ops/**
    if all(_is_rule_d_match(norm) for norm in normalized):
        return {
            "mode": "none",
            "files": [],
            "desktop": desktop,
            "desktop_files": sorted(desktop_files),
        }

    # Test selection
    selected: Set[str] = set()
    tests_root = root / "tests"

    # Rule b: changed tests/**/test_*.py directly selected
    for norm in normalized:
        if norm.startswith("tests/") and Path(norm).name.startswith("test_") and norm.endswith(".py"):
            if (root / norm).is_file():
                selected.add(norm)

    # Rule c: source files -> find matching tests
    if tests_root.is_dir():
        for norm in py_sources:
            found = _find_test_files_for_source(norm, tests_root, root)
            selected.update(found)

    # If specific tests were selected, return selected mode
    if selected:
        return {
            "mode": "selected",
            "files": sorted(selected),
            "desktop": desktop,
            "desktop_files": sorted(desktop_files),
        }

    # Fail-open: non-rule-d files changed, but no specific tests selected -> full suite
    return {
        "mode": "full",
        "files": [],
        "desktop": desktop,
        "desktop_files": sorted(desktop_files),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify changed files and select tests.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Changed file paths. If omitted, uses --before and --sha or reads from stdin.",
    )
    parser.add_argument("--before", help="Git commit SHA before the push.")
    parser.add_argument("--sha", "--head", help="Git commit SHA at head.")
    parser.add_argument(
        "--base",
        default="origin/cntrl-hermes-worker",
        help="Base branch to diff against when before is zero/missing (default: origin/cntrl-hermes-worker).",
    )
    args = parser.parse_args(argv)

    changed_files: List[str] = []
    if args.paths:
        changed_files = args.paths
    elif args.sha:
        base_ref, head_ref = resolve_diff_range(args.before, args.sha, base_branch=args.base)
        changed_files = get_changed_files_git(base_ref, head_ref)
    elif not sys.stdin.isatty():
        changed_files = [line.strip() for line in sys.stdin if line.strip()]
    else:
        # Fallback to diffing against default base branch
        base_ref, head_ref = resolve_diff_range(None, "HEAD", base_branch=args.base)
        changed_files = get_changed_files_git(base_ref, head_ref)

    result = select_tests(changed_files)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
