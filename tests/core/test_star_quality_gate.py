"""An unexpanded `SELECT *` must trip v1's quality gates, as it always did v2's.

"Source table has no schema, the star never expanded" is the most common shape of lost
lineage, yet v1 recorded only a warning and no fact gap — so `--fail-on-root-gap` and
`--quality-policy strict` pipelines passed the exact artifact they exist to stop
(issue: v1-v2-contract-gaps 1.1). The gap type matches v2's for the same condition.
"""

from __future__ import annotations

from pathlib import Path

from scope_lineage.cli import main
from scope_lineage.scope.scope_builder import parse_scope_lineage

STAR_SQL = "INSERT INTO db.t SELECT * FROM unknown.tbl"


def _star_gaps(result):
    return [
        gap
        for gap in result.diagnostics.lineage_fact_gaps
        if gap.get("gap_type") == "projection_wildcard_unexpanded"
    ]


def test_unexpanded_star_is_a_root_impact_fact_gap():
    result = parse_scope_lineage(STAR_SQL, task_name="demo")
    (gap,) = _star_gaps(result)
    assert gap["root_impact"] is True
    assert gap["scope_id"] == "ROOT"
    assert gap["gap_id"]


def test_an_expanded_star_leaves_no_gap():
    result = parse_scope_lineage(
        "INSERT INTO db.t SELECT * FROM ods.src",
        task_name="demo",
        schema={"ods.src": ["id", "v"]},
    )
    assert _star_gaps(result) == []


def test_fail_on_root_gap_now_rejects_the_star(tmp_path: Path):
    sql = tmp_path / "q.sql"
    sql.write_text(STAR_SQL, encoding="utf-8")
    assert main([
        "parse", "--sql-file", str(sql),
        "--fail-on-root-gap", "--out", str(tmp_path / "out"),
    ]) == 1


def test_strict_policy_now_rejects_the_star(tmp_path: Path):
    sql = tmp_path / "q.sql"
    sql.write_text(STAR_SQL, encoding="utf-8")
    assert main([
        "parse", "--sql-file", str(sql),
        "--quality-policy", "strict", "--out", str(tmp_path / "out"),
    ]) == 1


def test_permissive_default_still_exits_zero(tmp_path: Path):
    sql = tmp_path / "q.sql"
    sql.write_text(STAR_SQL, encoding="utf-8")
    assert main([
        "parse", "--sql-file", str(sql), "--out", str(tmp_path / "out"),
    ]) == 0
