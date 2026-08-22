"""A table qualified by its own name is a resolved reference, not an unexpanded alias.

`qualify` gives an unaliased table an alias equal to its bare name, so `FROM ods.pay` yields
references written `pay.uid`. The expanded-expression check exempts an alias that *is* the
physical source's own name, but it compared the bare alias (`pay`) against the qualified
physical id (`ods.pay`) and never matched -- so a fully resolved direct physical source was
reported as `expanded_expression_contains_unexpanded_alias`, demoting the output to
partially_resolved and the statement to `partial` (BARE-ALIAS-001).

It needs two things at once to show up, which is why it hid: the same table has to be read in
the enclosing FROM *and* inside a subquery in the projection. The FROM is what registers the
alias binding in this scope; the subquery is what puts that same qualifier into the expression
text. The check is textual and cannot tell the two apart.

The exemption still refuses a genuine local alias: `FROM ods.source s` leaves `s`, which is
not the table's name, and an `s.` that survived expansion really did fail to rewrite.
"""

from __future__ import annotations

from scope_lineage.scope.task_lineage import parse_task_lineage

SCHEMA = {"ods.pay": ["uid", "src", "dt"], "ods.source": ["uid", "v"], "mart.t": ["uid", "n"]}

SAME_TABLE = (
    "INSERT INTO mart.t\n"
    "SELECT uid,\n"
    "  (SELECT COUNT(DISTINCT uid) FROM ods.pay WHERE src = 'y') AS n\n"
    "FROM ods.pay WHERE src = 'x'"
)


def _result(sql: str):
    return parse_task_lineage(sql, task_name="t", schema=SCHEMA)


def test_a_table_qualified_by_its_own_name_is_not_an_unexpanded_alias():
    assert (_result(SAME_TABLE).diagnostics.get("lineage_fact_gaps") or []) == []


def test_the_statement_is_not_demoted_to_partial():
    assert _result(SAME_TABLE).analysis_status.get("status") == "complete"


def test_the_column_keeps_its_physical_source():
    """The exemption must not be bought by dropping the lineage it was hiding."""
    result = _result(SAME_TABLE)
    row = next(i for i in result.end_to_end_lineage
               if i.get("table") == "mart.t" and i.get("column") == "n")
    sources = {(s.get("table"), s.get("column")) for s in row.get("value_sources") or []
               if s.get("source_kind") == "physical_field"}

    assert ("ods.pay", "uid") in sources


def test_a_real_local_alias_is_still_reported():
    """`s` is not the table's name, so an `s.` left in the expression is a genuine failure."""
    from scope_lineage.scope.expression_text import _unexpanded_bound_aliases_in_expression
    from scope_lineage.scope.scope_types import ScopeData

    scope_data = ScopeData(kind="root")
    scope_data.alias_source_bindings = [{
        "alias": "s",
        "source_type": "physical_table",
        "physical_source_id": "ods.source",
        "physical_source_ids": ["ods.source"],
    }]

    assert _unexpanded_bound_aliases_in_expression(scope_data, "COUNT(s.uid)") == ["s"]


def test_the_bare_name_of_a_qualified_source_is_exempt():
    from scope_lineage.scope.expression_text import _unexpanded_bound_aliases_in_expression
    from scope_lineage.scope.scope_types import ScopeData

    scope_data = ScopeData(kind="root")
    scope_data.alias_source_bindings = [{
        "alias": "pay",
        "source_type": "physical_table",
        "physical_source_id": "ods.pay",
        "physical_source_ids": ["ods.pay"],
    }]

    assert _unexpanded_bound_aliases_in_expression(scope_data, "COUNT(pay.uid)") == []
