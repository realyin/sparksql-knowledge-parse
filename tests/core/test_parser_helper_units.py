"""Unit coverage for five small parser and reference-rewriting helpers.

Migrated from the integration repository, which held the only tests for each while Core had none.
They are small enough to look obviously correct and were not: `_extract_name_inner` must not name
an unaliased projection after text inside an expression or a comment, `_select_has_star_projection`
has to see a star that sits in a UNION branch rather than the top SELECT, `_stmt_kind_for_tree`
must not label a failed MERGE an INSERT, and the two reference replacements must leave qualified
refs and string literals alone.

Three arrived as methods of classes whose other tests stay where they are; none touched `self`,
so they travel as plain functions. Each test keeps the local imports it came with.
"""

from __future__ import annotations

# Only these two need a module-level import: the other three carry the local imports they were
# written with. From the module that defines them, not scope_builder's re-export.
from scope_lineage.scope.expression_text import _replace_qualified_ref_with_expression, _replace_unqualified_ref_with_expression


def test_qualified_ref_replacement_removes_existing_alias_expression_wrapper():
    expression = "`t1`.(IF(`ods.limit_adjust`.`adjust_flag` = 1, `ods.limit_adjust`.`orig_limit`, NULL))"
    replacement = "IF(`ods.limit_adjust`.`adjust_flag` = 1, `ods.limit_adjust`.`orig_limit`, NULL)"

    expanded = _replace_qualified_ref_with_expression(
        expression,
        "t1",
        "orig_temp_lim",
        replacement,
    )

    assert expanded == "(IF(`ods.limit_adjust`.`adjust_flag` = 1, `ods.limit_adjust`.`orig_limit`, NULL))"


def test_unqualified_ref_replacement_does_not_modify_qualified_refs_or_string_literals():
    expression = "`b1`.`p_id` = 'p_id'"
    replacement = "MAX(CASE WHEN `ods.meta`.`name` = 'p_id' THEN `ods.attr`.`value` END)"

    expanded = _replace_unqualified_ref_with_expression(expression, "p_id", replacement)

    assert expanded == expression


def test_a_failed_merge_is_not_labelled_insert():
    """Even when the build genuinely fails, the recorded statement kind must be true —
    everything else about a failed record (parse_status, empty scopes, the diagnostic) was
    already honest, and this one field was not."""
    from scope_lineage.scope.scope_builder import _stmt_kind_for_tree
    import sqlglot

    merge = sqlglot.parse_one(
        "MERGE INTO dwd.t USING ods.s ON dwd.t.id = ods.s.id WHEN MATCHED THEN DELETE",
        dialect="spark",
    )
    assert _stmt_kind_for_tree(merge) == "MERGE"
    insert = sqlglot.parse_one("INSERT INTO dwd.t SELECT id FROM ods.s", dialect="spark")
    assert _stmt_kind_for_tree(insert) == "INSERT"


def test_extract_name_inner_avoids_expression_and_comment():
    import sqlglot
    from scope_lineage.scope.parser import _extract_name_inner

    def name_of(sql):
        return _extract_name_inner(sqlglot.parse_one(sql, dialect="spark").selects[0])[0]

    # the failing shape: null-default over ONE source column -> that column (not the expr)
    assert name_of("SELECT COALESCE(x.amt, 0) /* end as real_amt */ FROM t") == "amt"
    assert name_of("SELECT nvl(x.exe_int_rate, 0) FROM t") == "exe_int_rate"
    # genuine multi-column expression -> comment-free SQL name (never with /* */)
    multi = name_of("SELECT COALESCE(a.x, a.y) /* note */ FROM t")
    assert "/*" not in multi and "note" not in multi and multi
    # aliased / plain column still use the alias / name
    assert name_of("SELECT foo(x) AS bar FROM t") == "bar"
    assert name_of("SELECT t.contr_no FROM t") == "contr_no"


def test_star_inside_a_union_branch_still_counts_as_a_star():
    """The star may sit in one branch of the set operation. Asking only the top-level
    expression for its projections misses it, and the derived table then looks like it
    exposes nothing."""
    from scope_lineage.scope.column_ref_resolver import _select_has_star_projection

    import sqlglot

    union = sqlglot.parse_one(
        "SELECT id FROM ods.a UNION ALL SELECT * FROM ods.b", dialect="spark"
    )
    assert _select_has_star_projection(union), "UNION 任一分支里的 * 都应被识别"
    no_star = sqlglot.parse_one(
        "SELECT id FROM ods.a UNION ALL SELECT id FROM ods.b", dialect="spark"
    )
    assert not _select_has_star_projection(no_star)


# --- WI-03: one ordered-dedupe implementation, two deliberately distinct nesting probes ---

def test_unique_ordered_dedupes_preserves_order_and_skips_falsy():
    from scope_lineage.scope.sequences import _unique_ordered

    assert _unique_ordered(["b", "a", "b", "", None, "a", "c"]) == ["b", "a", "c"]


def test_the_shared_grab_bag_module_stays_deleted():
    # WI-06 split _shared.py into themed modules; the resolver-alias trap it hosted
    # (WI-03) went with it. A revived grab-bag would be architecture drift.
    import importlib.util

    assert importlib.util.find_spec("scope_lineage.scope._shared") is None


def test_inside_nested_subquery_stops_at_subquery_boundary():
    import sqlglot
    from sqlglot import exp
    from scope_lineage.scope.expression_refs import _inside_nested_subquery

    parsed = sqlglot.parse_one("SELECT a, (SELECT b FROM u) FROM t", dialect="spark")
    columns = {col.name: col for col in parsed.find_all(exp.Column)}
    assert _inside_nested_subquery(parsed, columns["b"]) is True
    assert _inside_nested_subquery(parsed, columns["a"]) is False


def test_inside_nested_set_op_takes_root_first_and_guards_root_identity():
    import sqlglot
    from sqlglot import exp
    from scope_lineage.scope.sqlglot_walk import _inside_nested_set_op

    parsed = sqlglot.parse_one("SELECT a FROM t UNION ALL SELECT b FROM u", dialect="spark")
    columns = {col.name: col for col in parsed.find_all(exp.Column)}
    # Columns live inside each branch's SELECT, which is nested relative to the UNION root.
    assert _inside_nested_set_op(parsed, columns["a"]) is True
    # The root itself is never "inside" the root.
    assert _inside_nested_set_op(parsed, parsed) is False
