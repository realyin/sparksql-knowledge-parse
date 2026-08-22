"""v1 and v2 artifacts for the same script must share one designated statement key.

v1 numbers its per-statement artifacts by *write ordinal* (`task#0`, `task#1`), v2 by
*script position* (`task#1`, `task#3` behind `stmt:002`, `stmt:004`). The same `task#1`
existed in both and pointed at different statements, and no serialized field related
them (issue: v1-v2-contract-gaps 3.1). The join key is the v2-style `statement_id`
(with its zero-based `statement_index`), now also emitted on v1 documents.
"""

from __future__ import annotations

from scope_lineage.contract.lineage import to_lineage_dict
from scope_lineage.scope.scope_builder import parse_all_scope_lineage, parse_scope_lineage
from scope_lineage.scope.task_lineage import parse_task_lineage

SCRIPT = (
    "SET x.y = 1;\n"
    "INSERT INTO db.t1 SELECT 1 AS id;\n"
    "DELETE FROM db.z WHERE id = 0;\n"
    "INSERT INTO db.t2 SELECT 2 AS id"
)


def test_v1_results_carry_script_position_identity():
    results = parse_all_scope_lineage(SCRIPT, task_name="demo")
    assert [(r.task_id, r.statement_id, r.statement_index) for r in results] == [
        ("demo#0", "stmt:002", 1),
        ("demo#1", "stmt:004", 3),
    ]


def test_v1_document_serializes_the_join_key():
    documents = [
        to_lineage_dict(r) for r in parse_all_scope_lineage(SCRIPT, task_name="demo")
    ]
    assert [(d["statement_id"], d["statement_index"]) for d in documents] == [
        ("stmt:002", 1),
        ("stmt:004", 3),
    ]


def test_the_key_matches_v2_statement_sequence_for_the_same_statement():
    v1_by_target = {
        d["target_table"]: d
        for d in (
            to_lineage_dict(r) for r in parse_all_scope_lineage(SCRIPT, task_name="demo")
        )
    }
    task = parse_task_lineage(SCRIPT, task_name="demo")
    v2_by_target = {
        s["target_table"]: s
        for s in task.statements
        if s["statement_id"] in task.statement_lineage
    }
    assert set(v1_by_target) == set(v2_by_target)
    for target, v1_doc in v1_by_target.items():
        assert v1_doc["statement_id"] == v2_by_target[target]["statement_id"]
        assert v1_doc["statement_index"] == v2_by_target[target]["statement_index"]


def test_v2_nested_documents_self_describe_their_key():
    task = parse_task_lineage(SCRIPT, task_name="demo")
    for statement_id, nested in task.statement_lineage.items():
        assert nested["statement_id"] == statement_id


def test_single_write_script_still_emits_the_key():
    (result,) = parse_all_scope_lineage(
        "INSERT INTO db.t1 SELECT 1 AS id", task_name="demo"
    )
    assert result.task_id == "demo"
    assert (result.statement_id, result.statement_index) == ("stmt:001", 0)


def test_singular_entry_emits_the_first_write_position():
    result = parse_scope_lineage(SCRIPT, task_name="demo")
    assert (result.statement_id, result.statement_index) == ("stmt:002", 1)


def test_a_caller_handing_over_a_tree_gets_no_guessed_key():
    import sqlglot

    tree = sqlglot.parse_one("INSERT INTO db.t1 SELECT 1 AS id", read="spark")
    result = parse_scope_lineage(
        "INSERT INTO db.t1 SELECT 1 AS id", task_name="demo", tree=tree
    )
    assert result.statement_id is None
    document = to_lineage_dict(result)
    assert "statement_id" not in document
    assert "statement_index" not in document
