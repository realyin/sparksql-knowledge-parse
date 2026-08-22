"""A relation that only lives in the session is not a table in the warehouse.

A script that stages its work in `CREATE OR REPLACE TEMP VIEW` produces lineage in which
those views are indistinguishable from physical tables: `final_table_states` gains an entry
for each, and `metadata_coverage.covered_tables` counts them as covered. A consumer
reconciling that against the catalogue concludes the warehouse grew tables that do not
exist (TEMPVIEW-001).

`is_cached_relation` already carries exactly this meaning ("只存活于会话,消费者不应据此登记
仓库中新增了一张表") but only for `CACHE [LAZY] TABLE`. Widening that field would redefine a
published one; instead this adds `is_session_scoped_relation`, decided by one predicate over
AST facts — `Create kind=VIEW` with a temporary property, or `exp.Cache` — so a later reader
cannot fix the temp-view branch and leave the cache branch behind, or the reverse.

The boundary that matters is the one against a real CTAS. `CREATE TABLE db.r AS SELECT` and
`CREATE OR REPLACE TEMP VIEW r AS SELECT` produce byte-identical lineage today: both are
`stmt_kind=CTAS` with `is_cached_relation=False`. Core has the information in the AST and
drops it. Test 3 below is that boundary, and it is new capability rather than a regression
guard — nothing in Core could answer it before.
"""

from __future__ import annotations

import json

import pytest

from .statement_document import write_statement_documents
from scope_lineage.scope.scope_builder import parse_all_scope_lineage, parse_scope_lineage
from scope_lineage.scope.task_lineage import parse_task_lineage

SCHEMA = {"ods.real": ["id", "v"]}


@pytest.mark.parametrize("sql", [
    "create or replace temp view tmp_v as select id, v from ods.real",
    "create temporary view tmp_v as select id, v from ods.real",
    "create global temporary view tmp_v as select id, v from ods.real",
    "cache table tmp_v as select id, v from ods.real",
    "cache lazy table tmp_v as select id, v from ods.real",
    # TEMPORARY on a TABLE, not a VIEW. sqlglot reports kind=TABLE with the same
    # TemporaryProperty, and a predicate keyed on kind=VIEW misses it -- which is the
    # "which keyword produced it" mistake this predicate exists to avoid.
    "create temporary table tmp_v as select id, v from ods.real",
    "create temp table tmp_v as select id, v from ods.real",
])
def test_a_session_scoped_relation_is_marked(sql):
    result = parse_scope_lineage(sql, "t", schema=SCHEMA)

    assert result.stmt_kind == "CTAS"
    assert result.is_session_scoped_relation is True


@pytest.mark.parametrize("sql", [
    "create table db.r as select id, v from ods.real",
    "create table if not exists db.r as select id, v from ods.real",
    "create or replace view db.r as select id, v from ods.real",
])
def test_a_relation_that_persists_is_not_marked(sql):
    """The boundary Core could not draw before: a real CTAS stays a real table.

    A non-temporary `CREATE VIEW` is included deliberately — it is registered in the
    catalogue and survives the session, so it is not session-scoped even though it stores
    no rows.
    """
    result = parse_scope_lineage(sql, "t", schema=SCHEMA)

    assert result.is_session_scoped_relation is False


def test_the_cache_marker_is_left_exactly_as_it_was():
    """`is_cached_relation` keeps its published meaning: CACHE, and only CACHE."""
    cached = parse_scope_lineage("cache lazy table c as select id from ods.real", "t", schema=SCHEMA)
    view = parse_scope_lineage("create or replace temp view v as select id from ods.real", "t", schema=SCHEMA)

    assert cached.is_cached_relation is True
    assert view.is_cached_relation is False
    assert view.is_session_scoped_relation is True


def test_the_two_hop_chain_is_still_in_the_artifact():
    """Marking the relation must not collapse the hop through it.

    Shape C deliberately leaves `source_kind` alone, so the artifact keeps recording what the
    SQL says: `mart.t.v` reads `tmp_v.v`, and `tmp_v.v` reads `ods.real.v`. Folding the two
    into one edge is a consumer's decision — Core states facts and marks which relations do
    not outlive the session.
    """
    sql = (
        "create or replace temp view tmp_v as select id, v from ods.real;\n"
        "insert overwrite table mart.t select id, v from tmp_v"
    )
    result = parse_task_lineage(sql, task_name="t", schema=SCHEMA)
    edges = {
        (item.get("table"), item["column"], source.get("table"), source.get("column"))
        for item in result.end_to_end_lineage
        for source in item.get("value_sources") or []
    }

    assert ("mart.t", "v", "tmp_v", "v") in edges, edges
    assert ("tmp_v", "v", "ods.real", "v") in edges, edges


def test_task_lineage_carries_the_marker_on_the_producing_statement():
    """v2 is where the leak is visible: `final_table_states` gained an entry per temp view."""
    sql = (
        "create or replace temp view tmp_v as select id, v from ods.real;\n"
        "insert overwrite table mart.t select id, v from tmp_v"
    )
    result = parse_task_lineage(sql, task_name="t", schema=SCHEMA)
    producing = [s for s in result.statements if s.get("target_table", "").endswith("tmp_v")]

    assert producing, [s.get("target_table") for s in result.statements]
    assert producing[0]["is_session_scoped_relation"] is True
    writing = [s for s in result.statements if s.get("target_table") == "mart.t"]
    assert "is_session_scoped_relation" not in writing[0]


def test_the_marker_survives_serialization(tmp_path):
    result = parse_scope_lineage(
        "create or replace temp view tmp_v as select id, v from ods.real", "t", schema=SCHEMA
    )
    write_statement_documents(result, str(tmp_path))
    document = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))

    assert document["is_session_scoped_relation"] is True


def test_a_persisting_relation_omits_the_key_entirely(tmp_path):
    """Absent and false mean the same thing; only true is serialized."""
    result = parse_scope_lineage("create table db.r as select id from ods.real", "t", schema=SCHEMA)
    write_statement_documents(result, str(tmp_path))
    document = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))

    assert "is_session_scoped_relation" not in document


def test_a_script_marks_each_produced_relation_independently():
    sql = (
        "create or replace temp view tmp_v as select id from ods.real;\n"
        "create table db.kept as select id from ods.real"
    )
    results = parse_all_scope_lineage(sql, task_name="t", schema=SCHEMA)

    assert [r.is_session_scoped_relation for r in results] == [True, False]


def test_a_script_that_stages_in_temp_views_says_so_in_diagnostics():
    """The flag alone is not enough in v2, because it is nowhere near the misleading data.

    In v1 `is_session_scoped_relation` sits beside `target_table`, so a consumer registering
    tables cannot miss it. In v2 the flag is on `statement_sequence[]` while the entry that
    misleads is in `final_table_states` -- a different part of the document -- and
    `analysis_status` stays `complete`. A consumer who does not know to cross-reference the
    two reads a clean, confident artifact that names tables the warehouse does not have
    (TEMPVIEW-001).
    """
    sql = (
        "create or replace temp view tmp_v as select id, v from ods.real;\n"
        "cache lazy table tmp_c as select id from ods.real;\n"
        "insert overwrite table mart.t select id, v from tmp_v"
    )
    result = parse_task_lineage(sql, task_name="t", schema=SCHEMA)
    warnings = [
        w for w in result.diagnostics["warnings"]
        if w["type"] == "session_scoped_relations_present"
    ]

    assert len(warnings) == 1, result.diagnostics["warnings"]
    assert warnings[0]["scope"] == "TASK"
    # Naming them is the point: the consumer has to filter `final_table_states` by these.
    assert "tmp_v" in warnings[0]["msg"]
    assert "tmp_c" in warnings[0]["msg"]
    assert "final_table_states" in warnings[0]["msg"]


def test_a_script_without_session_scoped_relations_says_nothing():
    result = parse_task_lineage(
        "insert overwrite table mart.t select id, v from ods.real",
        task_name="t",
        schema=SCHEMA,
    )

    assert not [
        w for w in result.diagnostics["warnings"]
        if w["type"] == "session_scoped_relations_present"
    ]


def test_a_real_ctas_does_not_trigger_the_warning():
    """The boundary again: a relation that persists is not something to warn about."""
    result = parse_task_lineage(
        "create table db.kept as select id from ods.real;\n"
        "insert overwrite table mart.t select id from db.kept",
        task_name="t",
        schema=SCHEMA,
    )

    assert not [
        w for w in result.diagnostics["warnings"]
        if w["type"] == "session_scoped_relations_present"
    ]
