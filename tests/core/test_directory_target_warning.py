"""A `directory:` target in v2 gets the same phantom-table guard session relations have.

`INSERT OVERWRITE DIRECTORY` writes a path, not a table, yet its `directory:/...` entry
lands in `final_table_states` like any table. v1 documents the exclusion rule on
`target_table`; v2 said nothing, so a consumer reconciling final states against a
catalogue registers a phantom (issue: v1-v2-contract-gaps 4.3).
"""

from __future__ import annotations

from scope_lineage import parse_task_lineage

SQL = (
    "INSERT OVERWRITE DIRECTORY '/warehouse/export/daily' "
    "SELECT id FROM ods.src; "
    "INSERT INTO mart.t SELECT id FROM ods.src"
)


def test_directory_targets_raise_a_task_level_warning():
    result = parse_task_lineage(SQL, task_name="demo", schema={"ods.src": ["id"]})
    warnings = [
        w for w in result.diagnostics["warnings"]
        if w["type"] == "directory_targets_present"
    ]
    assert len(warnings) == 1
    assert warnings[0]["scope"] == "TASK"
    assert "directory:/warehouse/export/daily" in warnings[0]["msg"]


def test_no_warning_without_a_directory_target():
    result = parse_task_lineage(
        "INSERT INTO mart.t SELECT id FROM ods.src",
        task_name="demo",
        schema={"ods.src": ["id"]},
    )
    assert not [
        w for w in result.diagnostics["warnings"]
        if w["type"] == "directory_targets_present"
    ]
