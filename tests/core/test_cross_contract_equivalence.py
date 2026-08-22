"""Claims the gap review left "reviewed but not independently verified", pinned as tests.

Two claims from dev-notes/issues/v1-v2-contract-gaps.md ("未测，因此不列入结论"):
multi-write scripts populate `final_table_states` correctly, and per-statement
`source_tables` agree between the two contracts.
"""

from __future__ import annotations

from scope_lineage import parse_task_lineage
from scope_lineage.scope.scope_builder import parse_all_scope_lineage

MULTI_WRITE_SCRIPT = (
    "CREATE OR REPLACE TEMPORARY VIEW tmp_v AS SELECT id, v FROM ods.a;\n"
    "INSERT OVERWRITE TABLE mart.x SELECT id, v FROM tmp_v;\n"
    "TRUNCATE TABLE mart.y;\n"
    "INSERT INTO mart.y SELECT id FROM mart.x;\n"
    "INSERT INTO mart.y SELECT id FROM ods.b"
)

SCHEMA = {
    "ods.a": ["id", "v"],
    "ods.b": ["id"],
    "mart.x": ["id", "v"],
    "mart.y": ["id"],
}


def test_multi_write_final_table_states_cover_every_produced_relation():
    task = parse_task_lineage(MULTI_WRITE_SCRIPT, task_name="demo", schema=SCHEMA)
    assert set(task.final_table_states) == {"tmp_v", "mart.x", "mart.y"}


def test_a_twice_written_table_ends_on_its_last_state():
    task = parse_task_lineage(MULTI_WRITE_SCRIPT, task_name="demo", schema=SCHEMA)
    # mart.y: TRUNCATE -> INSERT -> INSERT, three transitions past the initial state
    states = [
        node["state_id"]
        for node in task.table_state_graph["nodes"]
        if node.get("table") == "mart.y"
    ]
    assert task.final_table_states["mart.y"] == states[-1]
    assert len(states) >= 3


def test_source_tables_agree_between_the_contracts_per_statement():
    scripts = [
        MULTI_WRITE_SCRIPT,
        "INSERT INTO mart.t SELECT a.id FROM ods.a a JOIN ods.b b ON a.id = b.id",
        (
            "SET x.y = 1;\n"
            "INSERT INTO mart.t WITH s AS (SELECT id FROM ods.a) "
            "SELECT id FROM s UNION ALL SELECT id FROM ods.b"
        ),
    ]
    for script in scripts:
        v1 = parse_all_scope_lineage(script, task_name="demo", schema=SCHEMA)
        v1_by_key = {r.statement_id: sorted(r.source_tables) for r in v1}
        task = parse_task_lineage(script, task_name="demo", schema=SCHEMA)
        v2_by_key = {
            statement_id: sorted(nested.get("source_tables") or [])
            for statement_id, nested in task.statement_lineage.items()
        }
        assert v1_by_key == v2_by_key, script
