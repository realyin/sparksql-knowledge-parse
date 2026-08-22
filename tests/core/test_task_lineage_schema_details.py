"""The task contract must not degrade the schema it hands to per-statement parsing.

`SchemaMap` is a dict subclass whose column types and comments live on the
`column_details` attribute. Copying it through `dict(schema)` keeps the keys and
silently drops the attribute, so every `related_metadata.input_tables[*]
.column_details[*]` in a v2 artifact lost its type/comment while the v1 artifact
for the same statement kept them (issue: v1-v2-contract-gaps 2.1).
"""

from __future__ import annotations

from scope_lineage.metadata.schema_metadata import SchemaMap
from scope_lineage.scope.scope_builder import parse_scope_lineage
from scope_lineage.scope.task_lineage import parse_task_lineage
from scope_lineage.contract.lineage import to_lineage_dict

SQL = "INSERT INTO mart.t SELECT id, v FROM ods.a"


def _schema() -> SchemaMap:
    return SchemaMap(
        {"ods.a": ["id", "v"]},
        column_details={
            "ods.a": [
                {"name": "id", "type": "bigint", "comment": "primary key"},
                {"name": "v", "type": "string", "comment": "value"},
            ]
        },
    )


def _details(document: dict) -> list[dict]:
    return document["related_metadata"]["input_tables"]["ods.a"]["column_details"]


def test_v2_nested_statement_keeps_column_types_and_comments():
    task = parse_task_lineage(SQL, task_name="demo", schema=_schema())
    (nested,) = task.statement_lineage.values()
    assert _details(nested) == [
        {"name": "id", "type": "bigint", "comment": "primary key"},
        {"name": "v", "type": "string", "comment": "value"},
    ]


def test_v2_nested_details_match_v1_for_the_same_statement():
    v1 = to_lineage_dict(parse_scope_lineage(SQL, task_name="demo", schema=_schema()))
    task = parse_task_lineage(SQL, task_name="demo", schema=_schema())
    (nested,) = task.statement_lineage.values()
    assert _details(nested) == _details(v1)
