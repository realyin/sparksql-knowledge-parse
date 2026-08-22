"""`write_task_lineage` must validate the nested v1 documents, not only the envelope.

`lineage-v2.schema.json` types `statement_lineage` values as bare objects, and the write
path used to call `validate_lineage_document` once on the outer document only — a nested
document that drifted from the v1 contract would have been published without complaint
(issue: v1-v2-contract-gaps 2.2, a regression risk rather than an observed corruption).
"""

from __future__ import annotations

import pytest
import jsonschema

from scope_lineage import parse_task_lineage, write_task_lineage

SQL = "INSERT INTO mart.t SELECT id FROM ods.src"


def test_a_valid_nested_document_still_writes(tmp_path):
    result = parse_task_lineage(SQL, task_name="demo", schema={"ods.src": ["id"]})
    output = write_task_lineage(result, tmp_path / "ok")
    assert (output / "lineage.json").exists()


def test_a_corrupted_nested_document_is_refused(tmp_path):
    result = parse_task_lineage(SQL, task_name="demo", schema={"ods.src": ["id"]})
    (nested,) = result.statement_lineage.values()
    nested["parse_status"] = 123  # violates the v1 schema's enum/string type

    with pytest.raises(jsonschema.ValidationError):
        write_task_lineage(result, tmp_path / "bad")

    assert not (tmp_path / "bad" / "lineage.json").exists()
