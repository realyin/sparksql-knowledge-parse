"""The differential-comparison harness must itself be trustworthy.

deep_diff is the referee that once caught three real output changes the golden corpus
missed (2026-08-23); these tests pin its reporting semantics. The self-comparison smoke
runs the whole harness against the current commit twice -- same engine on both sides
must always come back IDENTICAL, proving the plumbing (worktree, manifest, subprocess
isolation) adds no noise of its own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .differential_compare import build_manifest, deep_diff

REPO = Path(__file__).resolve().parents[2]


def test_deep_diff_reports_value_key_and_length_changes_with_paths() -> None:
    old = {"a": {"b": [1, 2]}, "gone": 1, "same": "x"}
    new = {"a": {"b": [1, 3]}, "added": 2, "same": "x"}

    diffs = deep_diff(old, new)

    rendered = "\n".join(diffs)
    assert "a.b[1]" in rendered and "1" in rendered and "3" in rendered
    assert any("gone" in d and "removed" in d.lower() for d in diffs)
    assert any("added" in d and "added" in d.lower() for d in diffs)
    assert not any("same" in d for d in diffs)


def test_deep_diff_returns_nothing_for_equal_documents() -> None:
    doc = {"k": [{"x": 1}, {"y": None}], "s": "v"}
    assert deep_diff(doc, dict(doc)) == []


def test_manifest_covers_tasks_sql_files_and_golden_cases() -> None:
    manifest = build_manifest(REPO)

    kinds = {key.split(":", 1)[0] for key in manifest}
    assert kinds == {"task", "sql", "golden"}
    assert len(manifest) >= 15
    for key, item in manifest.items():
        assert item["task_name"], key
        assert item["sql"].strip(), key
        assert item["schema"] is None or isinstance(item["schema"], dict), key


def test_self_comparison_is_identical() -> None:
    """Same commit on both sides: the harness must report IDENTICAL, exit 0."""
    result = subprocess.run(
        [sys.executable, str(REPO / "tests" / "architecture" / "differential_compare.py"), "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IDENTICAL" in result.stdout, result.stdout
