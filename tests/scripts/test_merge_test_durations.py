"""Tests for scripts/ci/merge_test_durations.py.

Verifies:
- Disjoint slice artifacts union into one dictionary.
- Later values win on key conflict.
- Directory traversal finds nested slice test_durations.json files.
- CLI arguments (-o, multiple inputs).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.merge_test_durations import merge_durations, main


def test_merge_disjoint_slices(tmp_path: Path) -> None:
    slice1 = tmp_path / "slice1.json"
    slice2 = tmp_path / "slice2.json"
    slice1.write_text(json.dumps({"tests/test_a.py": 1.25, "tests/test_b.py": 2.50}), encoding="utf-8")
    slice2.write_text(json.dumps({"tests/test_c.py": 0.75, "tests/test_d.py": 3.10}), encoding="utf-8")

    out_file = tmp_path / "test_durations.json"
    result = merge_durations([slice1, slice2], output_path=out_file)

    assert result == {
        "tests/test_a.py": 1.25,
        "tests/test_b.py": 2.50,
        "tests/test_c.py": 0.75,
        "tests/test_d.py": 3.10,
    }
    assert out_file.is_file()
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved == result


def test_merge_conflict_resolution(tmp_path: Path) -> None:
    """On conflict, later input file value wins."""
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first.write_text(json.dumps({"tests/shared.py": 1.5, "tests/only_first.py": 2.0}), encoding="utf-8")
    second.write_text(json.dumps({"tests/shared.py": 4.2, "tests/only_second.py": 3.0}), encoding="utf-8")

    result = merge_durations([first, second])

    assert result["tests/shared.py"] == 4.2
    assert result["tests/only_first.py"] == 2.0
    assert result["tests/only_second.py"] == 3.0


def test_merge_directory_artifacts(tmp_path: Path) -> None:
    """Finding test_durations.json in subdirectories (actions/download-artifact structure)."""
    artifacts_dir = tmp_path / "slice-durations"
    slice1_dir = artifacts_dir / "test-durations-slice-1"
    slice2_dir = artifacts_dir / "test-durations-slice-2"
    slice1_dir.mkdir(parents=True)
    slice2_dir.mkdir(parents=True)

    (slice1_dir / "test_durations.json").write_text(
        json.dumps({"tests/s1.py": 1.0}), encoding="utf-8"
    )
    (slice2_dir / "test_durations.json").write_text(
        json.dumps({"tests/s2.py": 2.0}), encoding="utf-8"
    )

    out_file = tmp_path / "merged.json"
    result = merge_durations([artifacts_dir], output_path=out_file)

    assert result == {
        "tests/s1.py": 1.0,
        "tests/s2.py": 2.0,
    }
    assert out_file.is_file()


def test_merge_cli(tmp_path: Path) -> None:
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    out = tmp_path / "out.json"

    f1.write_text(json.dumps({"tests/a.py": 1.0}), encoding="utf-8")
    f2.write_text(json.dumps({"tests/b.py": 2.0}), encoding="utf-8")

    script = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
    cmd = [sys.executable, str(script), str(f1), str(f2), "-o", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)

    assert proc.returncode == 0
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "tests/a.py": 1.0,
        "tests/b.py": 2.0,
    }


def test_merge_empty_or_nonexistent(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.json"
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")

    result = merge_durations([missing, empty])
    assert result == {}
