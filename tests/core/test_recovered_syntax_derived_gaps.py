"""A gap produced by a truncated parse is not a gap about the query.

When sqlglot cannot place a token it drops the rest, and lineage built from what survives is
shaped like lineage built from valid SQL. `syntax_status` already says the parse was
repaired, but the field-level gaps that follow from the truncation sit in the same list as
gaps about real missing metadata — and counting them together turns one syntax problem into
hundreds of apparent capability gaps (PARSE-002).

Marking them lets a consumer filter without having to correlate two documents.
"""

from __future__ import annotations

from scope_lineage.scope.scope_builder import parse_scope_lineage

# `select` here is an output alias the author forgot to quote. sqlglot cannot place it and
# drops the rest of the statement — taking `FROM ods.src` with it, which is what makes the
# gaps below meaningless as lineage facts.
#
# This was `AS not` until Core learned to quote keyword-colliding identifiers, which repairs
# that statement rather than truncating it. Clause keywords are excluded from that repair on
# purpose — quoting one can turn malformed SQL into an AST that parses and means something
# else (KEYWORD-IDENT-001) — so they still reach the truncating path this test needs.
TRUNCATING_SQL = "INSERT INTO mart.t SELECT a, CAST(a AS DOUBLE) AS SELECT, b FROM ods.src"
CLEAN_SQL = "INSERT INTO mart.t SELECT a, b FROM ods.src"
SCHEMA = {"ods.src": ["a", "b"]}


def test_truncated_parse_still_reports_recovered():
    result = parse_scope_lineage(TRUNCATING_SQL, task_name="t", schema=SCHEMA)

    assert result.syntax_status == "recovered"
    # The dropped FROM is what makes the gaps below meaningless as lineage facts.
    assert result.scopes["ROOT"].input_source_refs == []


def test_gaps_from_a_recovered_parse_are_marked():
    result = parse_scope_lineage(TRUNCATING_SQL, task_name="t", schema=SCHEMA)
    gaps = result.diagnostics.lineage_fact_gaps

    assert gaps, "this shape is expected to produce at least one gap"
    assert all(gap.get("derived_from_recovered_syntax") is True for gap in gaps)


def test_gaps_from_a_clean_parse_carry_no_marker():
    result = parse_scope_lineage(
        "INSERT INTO mart.t SELECT unknown_col FROM ods.src",
        task_name="t",
        schema=SCHEMA,
    )

    assert result.syntax_status == "strict_ok"
    for gap in result.diagnostics.lineage_fact_gaps:
        assert "derived_from_recovered_syntax" not in gap


def test_a_clean_statement_is_untouched():
    """The marker must not appear anywhere in a strict_ok document."""
    result = parse_scope_lineage(CLEAN_SQL, task_name="t", schema=SCHEMA)

    assert result.syntax_status == "strict_ok"
    assert all(
        "derived_from_recovered_syntax" not in gap
        for gap in result.diagnostics.lineage_fact_gaps
    )


def test_task_lineage_marks_them_too():
    """v2 needs its own answer: the truncation is invisible by the time it looks.

    Statement lineage is built from ``tree.sql()``, and the rendered statement parses
    cleanly — the dropped tokens are simply not in it. Only the script-level verdict knows.
    """
    from scope_lineage.scope.task_lineage import parse_task_lineage

    result = parse_task_lineage(TRUNCATING_SQL, task_name="t", schema=SCHEMA)
    gaps = result.diagnostics["lineage_fact_gaps"]

    assert "syntax_recovered" in result.analysis_status["blocking_reasons"]
    assert gaps
    assert all(gap.get("derived_from_recovered_syntax") is True for gap in gaps)


def test_task_lineage_leaves_a_clean_script_unmarked():
    from scope_lineage.scope.task_lineage import parse_task_lineage

    result = parse_task_lineage(
        "INSERT INTO mart.t SELECT unknown_col FROM ods.src",
        task_name="t",
        schema=SCHEMA,
    )

    assert "syntax_recovered" not in result.analysis_status["blocking_reasons"]
    for gap in result.diagnostics["lineage_fact_gaps"]:
        assert "derived_from_recovered_syntax" not in gap


def test_marker_survives_serialization(tmp_path):
    from .statement_document import write_statement_documents

    result = parse_scope_lineage(TRUNCATING_SQL, task_name="t", schema=SCHEMA)
    write_statement_documents(result, str(tmp_path))

    import json

    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    gaps = diagnostics["lineage_fact_gaps"]
    assert gaps
    assert all(gap.get("derived_from_recovered_syntax") is True for gap in gaps)
