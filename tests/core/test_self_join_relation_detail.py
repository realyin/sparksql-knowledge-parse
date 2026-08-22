"""Self-join `join_relation_detail` must keep the two sides apart.

`alias_by_source` was keyed by source_id, so a table joined to itself collapsed both
sides onto the later alias (`left_alias == right_alias == "b"`), and the equality
conjuncts were refused as key pairs because both refs resolve to the same scope —
rendering `a.batch_id = b.batch_id` as the tautology `ods.nodes.batch_id =
ods.nodes.batch_id` (issue: v1-v2-contract-gaps 4.1 / join-alias-overwrite-on-self-join).
"""

from __future__ import annotations

from scope_lineage.scope.scope_builder import parse_scope_lineage

SQL = (
    "INSERT INTO mart.t "
    "SELECT a.id FROM ods.nodes a JOIN ods.nodes b "
    "ON a.id = b.parent_id AND a.batch_id = b.batch_id AND b.status = 'ok'"
)

SCHEMA = {"ods.nodes": ["id", "parent_id", "batch_id", "status"]}


def _detail():
    result = parse_scope_lineage(SQL, task_name="demo", schema=SCHEMA)
    for block in result.scopes["ROOT"].logic_blocks:
        if block.logic_type == "join":
            return block.join_relation_detail
    raise AssertionError("no join logic block")


def test_each_side_keeps_its_own_alias():
    detail = _detail()
    assert detail["left_alias"] == "a"
    assert detail["right_alias"] == "b"


def test_equality_conjuncts_become_key_pairs_oriented_by_qualifier():
    detail = _detail()
    pairs = {
        (p["left"]["qualifier"], p["left"]["column"],
         p["right"]["qualifier"], p["right"]["column"])
        for p in detail["join_key_pairs"]
    }
    assert pairs == {
        ("a", "id", "b", "parent_id"),
        ("a", "batch_id", "b", "batch_id"),
    }
    assert "missing_join_key_pairs" not in detail["missing_reasons"]


def test_the_literal_conjunct_stays_a_condition_filter():
    detail = _detail()
    assert [f["expression"] for f in detail["condition_filters"]] == ["`b`.`status` = 'ok'"]


def test_a_distinct_table_join_is_unchanged():
    result = parse_scope_lineage(
        "INSERT INTO mart.t SELECT a.id FROM ods.a a JOIN ods.b b ON a.id = b.a_id",
        task_name="demo",
        schema={"ods.a": ["id"], "ods.b": ["a_id"]},
    )
    for block in result.scopes["ROOT"].logic_blocks:
        if block.logic_type == "join":
            detail = block.join_relation_detail
            assert (detail["left_alias"], detail["right_alias"]) == ("a", "b")
            assert len(detail["join_key_pairs"]) == 1
            return
    raise AssertionError("no join logic block")
