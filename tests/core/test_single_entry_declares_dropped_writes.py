"""`parse_scope_lineage` models the first write statement only — that boundary must be
declared in the artifact, not silently applied.

A two-write script fed to the singular entry used to come back as the first write's
document with no warning, no fact record, and no `skipped_statements`: undeclared data
loss on a PUBLIC_CORE_API symbol (issue: v1-v2-contract-gaps 3.3).
"""

from __future__ import annotations

from scope_lineage.scope.scope_builder import parse_scope_lineage

TWO_WRITES = "INSERT INTO db.t1 SELECT 1 AS id; INSERT INTO db.t2 SELECT 2 AS id"


def test_the_first_write_is_still_the_one_modeled():
    result = parse_scope_lineage(TWO_WRITES, task_name="demo")
    assert result.target_table == "db.t1"


def test_the_dropped_write_is_recorded_with_its_script_position():
    result = parse_scope_lineage(TWO_WRITES, task_name="demo")
    records = [
        item
        for item in result.skipped_statements
        if item["category"] == "additional_write_statement"
    ]
    assert len(records) == 1
    (record,) = records
    assert record["statement_index"] == 1
    assert record["statement_id"] == "stmt:002"
    assert record["model_status"] == "not_modeled"
    assert "db.t2" in record["normalized_sql"]


def test_the_loss_is_warned_about_naming_the_lost_target():
    result = parse_scope_lineage(TWO_WRITES, task_name="demo")
    warnings = [
        w for w in result.diagnostics.warnings
        if w.type == "additional_write_statements_not_modeled"
    ]
    assert len(warnings) == 1
    assert "db.t2" in warnings[0].msg


def test_nonwrite_statements_are_recorded_like_the_plural_entry():
    sql = "SET x.y=1; INSERT INTO db.t1 SELECT 1 AS id; DELETE FROM db.z WHERE id = 1"
    result = parse_scope_lineage(sql, task_name="demo")
    categories = {item["category"] for item in result.skipped_statements}
    assert "control_statement" in categories
    assert "row_mutation" in categories
    # row mutations warn, control statements are ignored by design — same as plural
    assert any(w.type == "unsupported_statement" for w in result.diagnostics.warnings)


def test_a_single_write_script_gains_nothing():
    result = parse_scope_lineage("INSERT INTO db.t1 SELECT 1 AS id", task_name="demo")
    assert not result.skipped_statements
    assert not any(
        w.type == "additional_write_statements_not_modeled"
        for w in result.diagnostics.warnings
    )


def test_a_caller_handing_over_a_tree_is_unaffected():
    import sqlglot

    tree = sqlglot.parse_one("INSERT INTO db.t1 SELECT 1 AS id", read="spark")
    result = parse_scope_lineage(
        "INSERT INTO db.t1 SELECT 1 AS id", task_name="demo", tree=tree
    )
    assert not result.skipped_statements
