"""MERGE assignment values resolve against the scope its WHEN branch makes visible.

Spark picks the name-resolution scope from the branch: MATCHED sees target and source
both, WHEN NOT MATCHED sees only the source, WHEN NOT MATCHED BY SOURCE sees only the
target (Analyzer.resolveAssignments / MergeResolvePolicy). Core used to resolve every
branch against the USING relation, which fabricated source edges and published them with
``trace_complete: true``.

Expected values here come from Spark's resolver and SqlBaseParser.g4, not from what Core
printed before the fix.
"""
from __future__ import annotations

from scope_lineage import parse_scope_lineage
from scope_lineage.scope.scope_types import AMBIGUOUS_SCOPE_ID

TGT = "db.tgt"
SRC = "db.src"
# `both` is in each; `only_t` only in the target; `only_s` only in the source.
SCHEMA = {
    TGT: [{"name": "id"}, {"name": "both"}, {"name": "only_t"}],
    SRC: [{"name": "id"}, {"name": "both"}, {"name": "only_s"}],
}
HEAD = f"MERGE INTO {TGT} t USING {SRC} s ON t.id = s.id "


def _run(when: str):
    return parse_scope_lineage(HEAD + when, "merge_branch_resolution", SCHEMA)


def _cols(result):
    return {c.name: c for c in result.scopes["ROOT"].columns}


def _warns(result):
    return [w.type for w in result.diagnostics.warnings]


def _pairs(column):
    return [(s.scope, s.column) for s in column.sources]


# --- WHEN NOT MATCHED BY SOURCE: the target is the only visible relation -------------

def test_a_by_source_branch_is_not_labelled_matched():
    result = _run("WHEN NOT MATCHED BY SOURCE THEN UPDATE SET t.both = 0")
    column = _cols(result)["both"]
    assert column.merge_branch is None, (
        "contract 1.0 has no vocabulary for this branch; publishing `matched` or "
        "`not_matched` states a rowset semantics that is not the one Spark applies"
    )
    assert column.merge_branch_qualifier == "not_matched_by_source"
    assert column.merge_when_index == 0


def test_an_unqualified_value_under_by_source_reads_the_target():
    result = _run("WHEN NOT MATCHED BY SOURCE THEN UPDATE SET t.both = both + 1")
    assert _pairs(_cols(result)["both"]) == [(TGT, "both")]


def test_by_source_never_attributes_a_value_to_the_source_relation():
    result = _run("WHEN NOT MATCHED BY SOURCE THEN UPDATE SET t.both = s.only_s")
    column = _cols(result)["both"]
    # Spark fails analysis here: `s` is not resolvable in a BY SOURCE action. Publishing
    # a source edge would be a fact we know to be false.
    assert not any(scope == SRC or scope.startswith("subq:") for scope, _ in _pairs(column))
    assert "dangling_column_ref_dropped" in _warns(result)


# --- WHEN MATCHED: both relations are visible, so ambiguity is possible --------------

def test_a_name_both_relations_expose_is_left_ambiguous_under_matched():
    result = _run("WHEN MATCHED THEN UPDATE SET t.both = both + 1")
    column = _cols(result)["both"]
    assert "ambiguous_unqualified" in _warns(result)
    assert [scope for scope, _ in _pairs(column)] == [AMBIGUOUS_SCOPE_ID]
    # The candidate set is the point: narrower than UNKNOWN, and it must not be
    # collapsed to one by a consumer.
    # The USING relation is named by its scope id here, as it is everywhere else in the
    # artifact -- a candidate naming the physical table instead would not match the
    # `sources[]` entries a consumer resolves it against.
    assert {c["scope"] for c in column.sources[0].candidates} == {TGT, "subq:s"}


def test_a_name_only_the_target_exposes_resolves_to_the_target():
    result = _run("WHEN MATCHED THEN UPDATE SET t.both = only_t")
    assert _pairs(_cols(result)["both"]) == [(TGT, "only_t")]


def test_the_same_statement_answers_the_same_whichever_using_shape_is_written():
    bare = _run("WHEN MATCHED THEN UPDATE SET t.both = only_t")
    sub = parse_scope_lineage(
        f"MERGE INTO {TGT} t USING (SELECT id, both, only_s FROM {SRC}) s "
        "ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.both = only_t",
        "merge_branch_resolution",
        SCHEMA,
    )
    assert _pairs(_cols(bare)["both"]) == _pairs(_cols(sub)["both"])


def test_ambiguity_is_not_claimed_when_one_side_of_the_schema_is_missing():
    result = parse_scope_lineage(
        HEAD + "WHEN MATCHED THEN UPDATE SET t.both = both + 1",
        "merge_branch_resolution",
        {SRC: SCHEMA[SRC]},          # target columns unknown
    )
    column = _cols(result)["both"]
    assert "unresolved_unqualified_no_schema" in _warns(result)
    # Unknowable is not the same as ambiguous, and neither licenses picking a side.
    assert _pairs(column) != [(SRC, "both")]


# --- the DELETE warning names the branch it actually saw -----------------------------

def test_the_delete_warning_names_a_by_source_branch_as_such():
    result = _run("WHEN NOT MATCHED BY SOURCE THEN DELETE")
    text = " ".join(w.msg for w in result.diagnostics.warnings if w.type == "merge_delete_ignored")
    assert "NOT MATCHED BY SOURCE" in text


# --- guards: behaviour that must NOT change ------------------------------------------

def test_an_insert_branch_keeps_its_label_and_gains_no_qualifier():
    result = _run("WHEN NOT MATCHED THEN INSERT (id, both) VALUES (s.id, s.both)")
    column = _cols(result)["both"]
    assert column.merge_branch == "not_matched"
    assert column.merge_branch_qualifier is None
    assert _pairs(column) == [("subq:s", "both")]


def test_a_fully_qualified_matched_assignment_is_untouched():
    result = _run("WHEN MATCHED THEN UPDATE SET t.both = s.both")
    column = _cols(result)["both"]
    assert column.merge_branch == "matched"
    assert column.merge_branch_qualifier is None
    assert _pairs(column) == [("subq:s", "both")]
    assert "ambiguous_unqualified" not in _warns(result)


def test_a_matched_delete_still_says_matched_and_not_by_source():
    result = _run("WHEN MATCHED THEN DELETE")
    text = " ".join(w.msg for w in result.diagnostics.warnings if w.type == "merge_delete_ignored")
    assert "MATCHED THEN DELETE" in text and "NOT MATCHED" not in text


# --- the qualifier reaches every surface that publishes merge_branch -----------------

def test_every_surface_that_carries_the_branch_also_carries_the_qualifier(tmp_path):
    import json

    from .statement_document import write_statement_documents

    result = _run("WHEN NOT MATCHED BY SOURCE THEN UPDATE SET t.both = both + 1")
    write_statement_documents(result, str(tmp_path))
    doc = json.loads((tmp_path / "lineage.json").read_text())

    def carriers(node, path=""):
        if isinstance(node, dict):
            if "merge_when_index" in node:
                yield path, node
            for key, value in node.items():
                yield from carriers(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from carriers(value, f"{path}[{index}]")

    found = dict(carriers(doc))
    assert found, "the statement writes a column, so some surface must carry it"
    for path, node in found.items():
        assert "merge_branch" not in node, path
        assert node.get("merge_branch_qualifier") == "not_matched_by_source", path
    # The surfaces the reviewer found are the ones that regress silently if missed.
    assert any(p.startswith("end_to_end_lineage") for p in found)
    assert any(p.startswith("field_mapping_chains") for p in found)


def test_the_missing_label_is_explained_not_merely_absent():
    result = _run("WHEN NOT MATCHED BY SOURCE THEN UPDATE SET t.both = 0")
    assert "merge_branch_not_representable" in _warns(result)
