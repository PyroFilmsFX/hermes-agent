"""Tests for scripts/ci/select_tests.py.

Verifies:
- Rule a: Full suite triggers (pyproject.toml, uv.lock, pm/**, scripts/run_tests*.sh,
  scripts/run_tests_parallel.py, scripts/ci/**, .github/**, any conftest.py,
  tests/fixtures/**, hermes_constants.py).
- Rule a escalation: >40 Python source files changed -> mode 'full'.
- Rule b: Direct selection of changed tests/**/test_*.py files.
- Rule c: Dotted module imports (import m, from m import, from <parent> import <leaf>)
  and tests/**/test_<stem>*.py name-based selection.
- Rule d: Mode 'none' when changes are only under apps/**, website/**, docs/**, *.md, _ops/**.
- Zero-before fallback input shape handling.
- Desktop JS job detection for apps/desktop/** changes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.select_tests import (
    is_zero_or_missing,
    resolve_diff_range,
    select_tests,
)


@pytest.mark.parametrize(
    "rule_a_file",
    [
        "pyproject.toml",
        "uv.lock",
        "pm/lock.json",
        "pm/build.py",
        "scripts/run_tests.sh",
        "scripts/run_tests_parallel.sh",
        "scripts/run_tests_parallel.py",
        "scripts/ci/classify_changes.py",
        ".github/workflows/cntrl-fork-tests.yml",
        ".github/actions/setup-pm/action.yml",
        "conftest.py",
        "tests/conftest.py",
        "tests/agent/conftest.py",
        "tests/fixtures/data.json",
        "tests/fixtures/sample.txt",
        "hermes_constants.py",
    ],
)
def test_rule_a_full_suite_triggers(tmp_path: Path, rule_a_file: str) -> None:
    """Rule a: Any match triggers full suite immediately."""
    result = select_tests([rule_a_file], repo_root=tmp_path)
    assert result["mode"] == "full"
    assert result["files"] == []


def test_rule_a_escalation_more_than_40_python_sources(tmp_path: Path) -> None:
    """Rule a: More than 40 Python source files changed escalates to full suite."""
    files = [f"agent/module_{i}.py" for i in range(41)]
    result = select_tests(files, repo_root=tmp_path)
    assert result["mode"] == "full"
    assert result["files"] == []


def test_rule_a_not_escalated_under_threshold(tmp_path: Path) -> None:
    """40 or fewer source files does not trigger >40 escalation."""
    files = [f"agent/module_{i}.py" for i in range(10)]
    # In an empty fake repo with no test files found, fallback is full only if no tests found
    # But let's create a matching test so we see it select tests rather than escalate
    tests_dir = tmp_path / "tests" / "agent"
    tests_dir.mkdir(parents=True)
    for i in range(10):
        t = tests_dir / f"test_module_{i}.py"
        t.write_text(f"def test_{i}(): pass\n", encoding="utf-8")

    result = select_tests(files, repo_root=tmp_path)
    assert result["mode"] == "selected"
    assert len(result["files"]) == 10


def test_rule_b_changed_test_selected_directly(tmp_path: Path) -> None:
    """Rule b: Changed tests/**/test_*.py files are selected directly."""
    tests_dir = tmp_path / "tests" / "tools"
    tests_dir.mkdir(parents=True)
    t1 = tests_dir / "test_terminal.py"
    t2 = tests_dir / "test_editor.py"
    t1.write_text("def test_term(): pass\n", encoding="utf-8")
    t2.write_text("def test_edit(): pass\n", encoding="utf-8")

    changed = ["tests/tools/test_terminal.py", "tests/tools/test_editor.py"]
    result = select_tests(changed, repo_root=tmp_path)

    assert result["mode"] == "selected"
    assert result["files"] == sorted(changed)


def test_rule_c_stem_and_import_grep(tmp_path: Path) -> None:
    """Rule c: Changed X.py selects test_<stem>*.py and tests importing m or from <parent> import <leaf>."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "turn_loop.py").write_text("# turn loop\n", encoding="utf-8")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)

    # 1. Stem match: tests/**/test_turn_loop*.py
    t_stem = tests_dir / "test_turn_loop.py"
    t_stem.write_text("# stem test\n", encoding="utf-8")

    # 2. Direct import m: 'import agent.turn_loop'
    t_import_m = tests_dir / "test_feature_a.py"
    t_import_m.write_text("import agent.turn_loop\n", encoding="utf-8")

    # 3. from m import ...: 'from agent.turn_loop import run_turn'
    t_from_m = tests_dir / "test_feature_b.py"
    t_from_m.write_text("from agent.turn_loop import run_turn\n", encoding="utf-8")

    # 4. from <parent> import <leaf>: 'from agent import turn_loop'
    t_parent_leaf = tests_dir / "test_feature_c.py"
    t_parent_leaf.write_text("from agent import turn_loop as tl\n", encoding="utf-8")

    # 5. Multiline from <parent> import (..., <leaf>, ...):
    t_parent_multiline = tests_dir / "test_feature_d.py"
    t_parent_multiline.write_text(
        "from agent import (\n    other_thing,\n    turn_loop,\n)\n",
        encoding="utf-8",
    )

    # 6. Unrelated test: should NOT be selected
    t_unrelated = tests_dir / "test_unrelated.py"
    t_unrelated.write_text("import other_module\n", encoding="utf-8")

    changed = ["agent/turn_loop.py"]
    result = select_tests(changed, repo_root=tmp_path)

    assert result["mode"] == "selected"
    expected = [
        "tests/test_feature_a.py",
        "tests/test_feature_b.py",
        "tests/test_feature_c.py",
        "tests/test_feature_d.py",
        "tests/test_turn_loop.py",
    ]
    assert result["files"] == sorted(expected)
    assert "tests/test_unrelated.py" not in result["files"]


def test_rule_c_top_level_module(tmp_path: Path) -> None:
    """Rule c: Changed top-level module (e.g. model_tools.py) selects stem and imports."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)

    t_stem = tests_dir / "test_model_tools_orchestration.py"
    t_stem.write_text("def test_orc(): pass\n", encoding="utf-8")

    t_import = tests_dir / "test_client.py"
    t_import.write_text("from model_tools import handle_function_call\n", encoding="utf-8")

    result = select_tests(["model_tools.py"], repo_root=tmp_path)
    assert result["mode"] == "selected"
    assert result["files"] == [
        "tests/test_client.py",
        "tests/test_model_tools_orchestration.py",
    ]


def test_rule_d_non_python_paths_none_mode(tmp_path: Path) -> None:
    """Rule d: Changes only under apps/**, website/**, docs/**, *.md or _ops/** give mode 'none'."""
    changed = [
        "website/docs/reference/config.md",
        "docs/setup.md",
        "README.md",
        "CONTRIBUTING.md",
        "_ops/deploy.yaml",
        "apps/shared/types.ts",
    ]
    result = select_tests(changed, repo_root=tmp_path)
    assert result["mode"] == "none"
    assert result["files"] == []


def test_rule_d_empty_diff_none_mode(tmp_path: Path) -> None:
    """Empty changed files list gives mode 'none'."""
    result = select_tests([], repo_root=tmp_path)
    assert result["mode"] == "none"
    assert result["files"] == []


def test_zero_before_fallback_input_shape() -> None:
    """Zero-before (e.g. 40 zeros from new branch push) and missing before fall back to base merge-base."""
    assert is_zero_or_missing("0000000000000000000000000000000000000000") is True
    assert is_zero_or_missing("0" * 40) is True
    assert is_zero_or_missing("") is True
    assert is_zero_or_missing(None) is True
    assert is_zero_or_missing("null") is True
    assert is_zero_or_missing("None") is True

    # Real commit SHA is not zero or missing
    real_sha = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    assert is_zero_or_missing(real_sha) is False


def test_resolve_diff_range_normal() -> None:
    before = "1111111111111111111111111111111111111111"
    sha = "2222222222222222222222222222222222222222"
    base_ref, head_ref = resolve_diff_range(before=before, sha=sha)
    assert base_ref == before
    assert head_ref == sha


def test_desktop_js_job_detection(tmp_path: Path) -> None:
    """Changed paths under apps/desktop/** flag desktop=True and collect sibling or changed test files."""
    desktop_src = tmp_path / "apps" / "desktop" / "src" / "store"
    desktop_src.mkdir(parents=True)

    # Source file with sibling test file
    (desktop_src / "composer.ts").write_text("export const c = 1;\n", encoding="utf-8")
    (desktop_src / "composer.test.ts").write_text("test('c', () => {});\n", encoding="utf-8")

    # Another test file changed directly
    (desktop_src / "profile.test.tsx").write_text("test('p', () => {});\n", encoding="utf-8")

    changed = [
        "apps/desktop/src/store/composer.ts",
        "apps/desktop/src/store/profile.test.tsx",
    ]
    result = select_tests(changed, repo_root=tmp_path)

    assert result["desktop"] is True
    assert "src/store/composer.test.ts" in result["desktop_files"]
    assert "src/store/profile.test.tsx" in result["desktop_files"]


def test_cli_invocation_json(tmp_path: Path) -> None:
    """CLI prints valid JSON with mode and files."""
    script = REPO_ROOT / "scripts" / "ci" / "select_tests.py"
    proc = subprocess.run(
        [sys.executable, str(script), "pyproject.toml"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["mode"] == "full"
    assert data["files"] == []
