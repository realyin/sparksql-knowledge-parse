"""A trailing comment is recorded as a COMMENT, not an empty statement's phantom SQL.

`INSERT ...;\n-- done\n` produced a record of kind SEMICOLON whose `normalized_sql`
was `/* done */` — a record calling itself an empty statement while holding a comment
(issue: v1-v2-contract-gaps 4.2). The category stays `empty_statement` (the position
holds no executable statement); the kind now names what the record holds.
"""

from __future__ import annotations

from scope_lineage import parse_task_lineage
from scope_lineage.scope.scope_builder import parse_scope_lineage

SQL = "INSERT INTO mart.t SELECT 1 AS id;\n-- done\n"


def test_v1_records_the_comment_kind():
    result = parse_scope_lineage(SQL, task_name="demo")
    (record,) = result.skipped_statements
    assert record["statement_kind"] == "COMMENT"
    assert record["category"] == "empty_statement"
    assert record["model_status"] == "ignored"
    assert "done" in record["normalized_sql"]


def test_v2_records_the_comment_kind():
    task = parse_task_lineage(SQL, task_name="demo")
    record = task.statements[1]
    assert record["stmt_kind"] == "COMMENT"
    assert record["category"] == "empty_statement"
    assert record["model_status"] == "ignored"
    assert "done" in record["normalized_sql"]


def test_a_genuinely_empty_statement_keeps_the_semicolon_kind():
    task = parse_task_lineage(
        "INSERT INTO mart.t SELECT 1 AS id;\n;", task_name="demo"
    )
    kinds = [s["stmt_kind"] for s in task.statements[1:]]
    assert "COMMENT" not in kinds
