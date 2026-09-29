#!/usr/bin/env python3
"""Merge test_durations.json artifacts from parallel test slices.

Usage:
    python scripts/ci/merge_test_durations.py slice-durations/ -o test_durations.json
    python scripts/ci/merge_test_durations.py slice1.json slice2.json -o test_durations.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


def _find_json_files(inputs: Iterable[str | Path]) -> List[Path]:
    """Resolve input paths (files or directories) into a deterministic list of JSON files."""
    files: List[Path] = []
    for item in inputs:
        p = Path(item)
        if not p.exists():
            continue
        if p.is_file():
            if p.suffix == ".json":
                files.append(p)
        elif p.is_dir():
            # In artifact download directory, look for test_durations.json or any *.json
            found = sorted(p.rglob("*.json"))
            files.extend(found)
    return files


def merge_durations(
    inputs: Sequence[str | Path],
    output_path: str | Path | None = None,
) -> Dict[str, float]:
    """Union test duration dictionaries. Slice files are disjoint; later value wins on conflict."""
    merged: Dict[str, float] = {}
    json_files = _find_json_files(inputs)

    for path in json_files:
        try:
            content = path.read_text(encoding="utf-8-sig")
            if not content.strip():
                continue
            data = json.loads(content)
            if isinstance(data, dict):
                # Ensure float values
                clean_data = {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}
                merged.update(clean_data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"warning: failed to read {path}: {exc}", file=sys.stderr)

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return merged


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge slice test_durations.json artifacts.")
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input files or directories containing test_durations.json files.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="test_durations.json",
        help="Output path for merged JSON (default: test_durations.json).",
    )
    args = parser.parse_args(argv)

    merged = merge_durations(args.inputs, output_path=args.output)
    print(f"Merged {len(merged)} test duration entries into {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
