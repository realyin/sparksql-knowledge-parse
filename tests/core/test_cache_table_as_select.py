"""``cache lazy table x as select`` produces a relation this task then reads.

Spark's cache statement builds a queryable relation from a SELECT, exactly as CTAS does —
sqlglot even parses it into the same shape, with the produced table on ``this`` and the
projection on ``expression``. The write-statement collector only recognised ``exp.Create``,
so the statement was skipped, the relation was never registered as script-local, and every
downstream reference to it was resolved against a physical table nobody has metadata for.
In practice that turns into hundreds of gaps.
"""

from __future__ import annotations

import sqlglot

from scope_lineage.scope._constants import DIALECT, PARSE_OPTS
from scope_lineage.scope.scope_builder import parse_all_scope_lineage
from scope_lineage.scope.task_lineage import parse_task_lineage


SCHEMA = {"ods.s": ["id", "v"], "mart.t": ["id", "v"]}

CACHE_THEN_READ_SQL = """
cache lazy table tmp_part1 OPTIONS ('storageLevel' 'DISK_ONLY') as select id, v from ods.s;
INSERT INTO mart.t SELECT t.id, t.v FROM tmp_part1 t
"""


def test_a_cached_relation_is_modelled_like_a_table_built_from_a_select() -> None:
    results = parse_all_scope_lineage(CACHE_THEN_READ_SQL, "cache_then_read", schema=SCHEMA)

    assert [item.stmt_kind for item in results] == ["CTAS", "INSERT"]
    # CTAS is the closed-enum value that fits: a relation built from a SELECT. The flag
    # keeps the part CTAS does not say — this one lives for the session, so a downstream
    # catalog must not register it as a table the warehouse now has.
    assert [item.is_cached_relation for item in results] == [True, False]


def test_a_downstream_read_of_a_cached_relation_resolves_to_its_real_source() -> None:
    result = parse_task_lineage(
        CACHE_THEN_READ_SQL, task_name="cache_then_read", schema=SCHEMA
    )

    assert [item["model_status"] for item in result.statements] == ["modeled", "modeled"]
    assert result.diagnostics["lineage_fact_gaps"] == []
    assert result.analysis_status == {"status": "complete", "blocking_reasons": []}

    coverage = result.diagnostics["metadata_coverage"]
    assert "tmp_part1" not in coverage["missing_tables"]

    # The cached relation is this statement's real input, so it is named as one; what
    # carries the read back to ods.s is the state graph, not a collapsed source list.
    assert {
        (item["column"], source["table"], source["column"])
        for item in result.end_to_end_lineage
        if item["table"] == "mart.t"
        for source in item["value_sources"]
        if source.get("source_kind") == "physical_field"
    } == {("id", "tmp_part1", "id"), ("v", "tmp_part1", "v")}
    assert {
        (item["column"], source["table"], source["column"])
        for item in result.end_to_end_lineage
        if item["table"] == "tmp_part1"
        for source in item["value_sources"]
        if source.get("source_kind") == "physical_field"
    } == {("id", "ods.s", "id"), ("v", "ods.s", "v")}
    assert [
        (node["table"], node["producer_statement_id"])
        for node in result.table_state_graph["nodes"]
        if node["table"] == "tmp_part1"
    ] == [("tmp_part1", "stmt:001")]


def test_a_cached_relation_whose_projection_stayed_a_wildcard_is_not_registered() -> None:
    """The same restraint CTAS has: a star that could not expand proves no column name.

    Registering the relation with whatever the star left behind would put a guessed column
    list in front of every statement that reads it.
    """
    results = parse_all_scope_lineage(
        """
        cache lazy table tmp_part1 as select * from ods.undocumented;
        INSERT INTO mart.t SELECT id, v FROM tmp_part1
        """,
        "cache_star",
        schema=SCHEMA,
    )

    assert [item.stmt_kind for item in results] == ["CTAS", "INSERT"]
    assert [column.name for column in results[0].scopes["ROOT"].columns] == ["*"]
    # The read still resolves against tmp_part1 itself; it is simply not backed by
    # columns this script proved.
    assert results[1].parse_status == "ok"


def test_cache_without_a_select_is_still_not_a_write_statement() -> None:
    """``CACHE TABLE existing_table`` builds nothing; it only pins what is already there."""
    tree = sqlglot.parse_one("CACHE TABLE ods.s", dialect=DIALECT, **PARSE_OPTS)
    assert tree.expression is None

    result = parse_task_lineage(
        "CACHE TABLE ods.s;\nINSERT INTO mart.t SELECT id, v FROM ods.s",
        task_name="cache_only",
        schema=SCHEMA,
    )
    assert [item["model_status"] for item in result.statements] == [
        "unsupported",
        "modeled",
    ]


def test_the_cached_relation_flag_reaches_the_contract(tmp_path) -> None:
    """The flag is only useful if a consumer can see it.

    It is optional and present only when true, so every existing artifact is unchanged and
    the closed stmt_kind enum stays as it is.
    """
    import json

    from scope_lineage import parse_scope_lineage, write_lineage

    cached = parse_scope_lineage(
        "cache lazy table tmp_part1 as select id, v from ods.s", "cached", schema=SCHEMA
    )
    output = write_lineage(cached, tmp_path / "cached")
    document = json.loads((output / "lineage.json").read_text(encoding="utf-8"))
    assert document["stmt_kind"] == "CTAS"
    assert document["is_cached_relation"] is True

    plain = parse_scope_lineage(
        "create table mart.t2 as select id, v from ods.s", "plain", schema=SCHEMA
    )
    output = write_lineage(plain, tmp_path / "plain")
    document = json.loads((output / "lineage.json").read_text(encoding="utf-8"))
    assert document["stmt_kind"] == "CTAS"
    assert "is_cached_relation" not in document
