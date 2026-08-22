"""Task-level modelling must not re-parse a statement from generated SQL.

Every statement was serialized back to SQL and parsed again before being modelled. sqlglot
does not round-trip a WITH carried by an individual UNION branch: the per-branch clauses are
hoisted to statement level and concatenated, so same-named CTEs shadow each other, qualify
raises on a column the shadowed CTE owned, and the whole statement degrades to an
unqualified parse. Everything downstream then reports gaps for lineage the AST could have
resolved — and the same script parsed through the v1 entry point resolves it.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from scope_lineage.scope._constants import DIALECT, PARSE_OPTS
from scope_lineage.scope.scope_builder import parse_all_scope_lineage
from scope_lineage.scope.task_lineage import parse_task_lineage


SCHEMA = {
    "ods.left_events": ["id", "day_idx"],
    "ods.right_events": ["id", "day_span"],
    "mart.summary": ["side", "day_idx", "day_span"],
}

# The branches must project differently named columns. Alias them to a common name and the
# CTEs still merge, but the survivor happens to satisfy both references and nothing fails.
PER_BRANCH_WITH_SQL = """
INSERT OVERWRITE TABLE mart.summary
WITH staged AS (SELECT id, day_idx FROM ods.left_events)
SELECT 'left' AS side, staged.day_idx AS day_idx, NULL AS day_span FROM staged
UNION ALL
WITH staged AS (SELECT id, day_span FROM ods.right_events)
SELECT 'right' AS side, NULL AS day_idx, staged.day_span AS day_span FROM staged
"""


def _physical_sources(items) -> set[tuple[str, str, str]]:
    return {
        (item.get("column"), source["table"], source["column"])
        for item in items
        for source in item.get("value_sources") or []
        if source.get("source_kind") == "physical_field"
    }


def test_sqlglot_still_loses_a_per_branch_with_on_round_trip() -> None:
    """The upstream defect this works around, asserted directly.

    When sqlglot starts round-tripping these clauses, this test fails and the workaround
    can go — rather than outliving the reason it exists.
    """
    original = sqlglot.parse_one(PER_BRANCH_WITH_SQL, dialect=DIALECT, **PARSE_OPTS)
    assert [type(node.parent).__name__ for node in original.find_all(exp.With)] == [
        "Union",
        "Select",
    ]

    round_tripped = sqlglot.parse_one(
        original.sql(dialect=DIALECT), dialect=DIALECT, **PARSE_OPTS
    )
    assert [type(node.parent).__name__ for node in round_tripped.find_all(exp.With)] == [
        "Insert"
    ]
    assert [
        cte.alias_or_name
        for node in round_tripped.find_all(exp.With)
        for cte in node.expressions
    ] == ["staged", "staged"]


def test_a_per_branch_with_survives_task_level_modelling() -> None:
    result = parse_task_lineage(
        PER_BRANCH_WITH_SQL, task_name="per_branch_with", schema=SCHEMA
    )

    assert result.diagnostics["lineage_fact_gaps"] == []
    assert result.analysis_status == {"status": "complete", "blocking_reasons": []}


def test_both_contracts_report_the_same_physical_sources_for_one_statement() -> None:
    """Two contracts disagreeing about one statement is a defect on its own.

    This is the guard that turns "v2 has gaps v1 does not" from something to be explained
    away into a failing test.
    """
    v1 = parse_all_scope_lineage(PER_BRANCH_WITH_SQL, "per_branch_with", schema=SCHEMA)
    assert [item.diagnostics.fallback_used for item in v1] == [False]

    from scope_lineage.scope.end_to_end import build_end_to_end_lineage

    v1_sources = {
        (column.name, source["table"], source["column"])
        for column in v1[0].scopes["ROOT"].columns
        for item in build_end_to_end_lineage(v1[0])
        if item["column"] == column.name
        for source in item["physical_sources"]
    }

    v2 = parse_task_lineage(
        PER_BRANCH_WITH_SQL, task_name="per_branch_with", schema=SCHEMA
    )
    v2_sources = _physical_sources(v2.end_to_end_lineage)

    assert v2_sources == v1_sources
    assert v2_sources == {
        ("day_idx", "ods.left_events", "day_idx"),
        ("day_span", "ods.right_events", "day_span"),
    }


def test_a_normalized_sql_that_lost_a_per_branch_with_says_so() -> None:
    """The lineage is right; the text beside it still is not.

    ``normalized_sql`` is generated from the AST, so it carries the same sqlglot defect and
    comes out with two CTEs of the same name — SQL that cannot run. Publishing it silently
    invites a consumer to execute it, so the statement carries a warning instead.
    """
    result = parse_task_lineage(
        PER_BRANCH_WITH_SQL, task_name="per_branch_with", schema=SCHEMA
    )

    assert [warning["type"] for warning in result.diagnostics["warnings"]] == [
        "normalized_sql_not_equivalent"
    ]
    warning = result.diagnostics["warnings"][0]
    assert warning["statement_id"] == "stmt:001"
    assert warning["scope"] == "TASK"

    # The lineage itself stays clean: this is a text problem, not a fact problem.
    assert result.diagnostics["lineage_fact_gaps"] == []
    assert result.analysis_status["status"] == "complete"


def test_an_ordinary_statement_gets_no_normalization_warning() -> None:
    result = parse_task_lineage(
        "INSERT INTO mart.summary SELECT id AS side, day_idx, day_idx AS day_span "
        "FROM ods.left_events",
        task_name="ordinary",
        schema=SCHEMA,
    )

    assert result.diagnostics["warnings"] == []


def test_a_with_that_merely_moves_is_not_reported() -> None:
    """A re-rendered WITH is not a lost one.

    ``INSERT INTO t WITH c AS (...) SELECT ...`` comes back rendered with the clause ahead
    of the INSERT. The AST attachment point differs, the SQL does not: no CTE is shadowed
    and the text still runs. Reporting it would train consumers to ignore this warning on
    the day it means something.
    """
    result = parse_task_lineage(
        "INSERT INTO mart.summary "
        "WITH c AS (SELECT id, day_idx FROM ods.left_events) "
        "SELECT c.id AS side, c.day_idx AS day_idx, c.day_idx AS day_span FROM c",
        task_name="with_inside_insert",
        schema=SCHEMA,
    )

    assert result.diagnostics["warnings"] == []
    assert result.diagnostics["lineage_fact_gaps"] == []


def test_a_comment_only_statement_is_not_reported() -> None:
    """A block of comments has no query structure to preserve.

    Its rendering does not parse, which is not the same as failing to round-trip — there
    was nothing to round-trip. Treating "unparseable" as "not equivalent" turned every
    commented-out DDL in a script into a warning.
    """
    result = parse_task_lineage(
        "/* CREATE TABLE mart.summary(id string) */;\n"
        "INSERT INTO mart.summary SELECT id AS side, day_idx, day_idx AS day_span "
        "FROM ods.left_events",
        task_name="comment_only",
        schema=SCHEMA,
    )

    assert result.diagnostics["warnings"] == []
