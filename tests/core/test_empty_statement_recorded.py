"""A `;;` leaves a hole in the statement numbering; v1 must account for it.

sqlglot models the two empty shapes differently: a bare `;` after a comment parses to
``exp.Semicolon``, while `;;` yields ``None``. v1 recorded the first and dropped the
second, so its statement indices -- which count both -- had gaps a consumer could not
explain from any published field. v2 recorded both all along.
"""
from __future__ import annotations

import json

import jsonschema

from scope_lineage import parse_all_scope_lineage, parse_task_lineage
from scope_lineage.scope.scope_builder import _collect_insert_trees

SCHEMA = {"db.s": [{"name": "a"}], "db.t": [{"name": "a"}], "db.t2": [{"name": "a"}]}
WRITE = "INSERT INTO db.t SELECT a FROM db.s"


def _skipped(sql: str) -> list[dict]:
    return _collect_insert_trees(sql)[1]


def _of_kind(entries: list[dict], kind: str) -> list[dict]:
    return [e for e in entries if e.get("statement_kind") == kind]


def test_a_double_semicolon_is_recorded_as_an_empty_statement():
    entries = _of_kind(_skipped(f"{WRITE};;"), "EMPTY")
    assert len(entries) == 1, _skipped(f"{WRITE};;")
    assert entries[0]["category"] == "empty_statement"
    assert entries[0]["model_status"] == "ignored"


def test_the_recorded_entry_satisfies_the_published_schema():
    # The previous draft of this fix omitted `supported`, which the schema requires --
    # The statement-document assembly validates before writing, so that draft would have raised at
    # serialization rather than produced a wrong document.
    with open("scope_lineage/schemas/lineage.schema.json") as handle:
        item_schema = json.load(handle)["properties"]["skipped_statements"]["items"]
    for entry in _skipped(f"SET x=1;;{WRITE};"):
        jsonschema.validate(entry, item_schema)


def test_the_numbering_has_no_unexplained_holes():
    entries = _skipped(f"SET x=1;;{WRITE};")
    assert sorted(e["statement_index"] for e in entries) == [0, 1]


def test_an_empty_statement_reaches_every_write_in_the_script():
    # skipped_statements is copied onto each write result, so the record count is
    # entries x writes -- the reason this change moves more output than it first appears.
    results = parse_all_scope_lineage(
        f"{WRITE};;\nINSERT INTO db.t2 SELECT a FROM db.s;", "t", SCHEMA
    )
    assert len(results) == 2
    for result in results:
        assert _of_kind(result.skipped_statements, "EMPTY")


def test_v1_now_accounts_for_the_same_positions_v2_does():
    sql = f"SET x=1;;{WRITE};"
    v1 = {e["statement_index"] for e in _skipped(sql)}
    v2 = {
        s["statement_index"]
        for s in parse_task_lineage(sql, "t", SCHEMA).statements
        if s.get("category") == "empty_statement" or s.get("stmt_kind") == "SET"
    }
    assert v1 == v2


# --- guards: the other empty shape, and everything else, must not move ---------------

def test_a_bare_semicolon_after_a_comment_is_still_recorded_as_semicolon():
    # The two shapes stay distinguishable: this one is exp.Semicolon and keeps its own
    # kind, matching what v2 calls it. Collapsing both into one name would lose the
    # distinction rather than complete it.
    entries = _skipped(f"{WRITE};\n-- note\n;")
    # This shape yields [Insert, Semicolon, None]: the comment's semicolon and a trailing
    # empty position. Both are recorded and they keep separate kinds -- exactly what v2
    # publishes for the same text.
    assert [(e["statement_index"], e["statement_kind"]) for e in entries] == [
        (1, "SEMICOLON"),
        (2, "EMPTY"),
    ]


def test_an_empty_statement_raises_no_unsupported_warning():
    result = parse_all_scope_lineage(f"{WRITE};;", "t", SCHEMA)[0]
    assert "unsupported_statement" not in {w.type for w in result.diagnostics.warnings}


def test_a_script_without_empty_statements_is_unchanged():
    assert _skipped(f"{WRITE};") == []
    assert _of_kind(_skipped(f"SET x=1;{WRITE};"), "EMPTY") == []
