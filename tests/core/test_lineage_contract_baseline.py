"""Byte-for-byte Core contract baseline captured before package extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scope_lineage.metadata.target_table_metadata import (
    TargetColumnMetadata,
    TargetMetadataMap,
    TargetTableMetadata,
)
from .statement_document import write_statement_documents
from scope_lineage.scope.scope_builder import parse_scope_lineage
from scope_lineage.scope.scope_types import ScopeGraphEdge


FIXTURES = Path(__file__).parent / "fixtures" / "lineage_contract"
CASES = tuple(sorted(path.parent for path in FIXTURES.glob("*/case.json")))


def _target_metadata(document: dict | None) -> TargetMetadataMap | None:
    if not document:
        return None
    item = TargetTableMetadata(
        table_name=document["table_name"],
        full_table_name=document["full_table_name"],
        columns=[TargetColumnMetadata(**column) for column in document["columns"]],
        partition_columns=document["partition_columns"],
        ddl=document["ddl"],
        source_file=document["source_file"],
        structure_source=document["structure_source"],
    )
    return TargetMetadataMap({item.table_name: item})


def _render(case_dir: Path, output_dir: Path) -> None:
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    result = parse_scope_lineage(
        case["sql"],
        case["task_id"],
        schema=case.get("schema"),
        target_metadata=_target_metadata(case.get("target_metadata")),
    )
    write_statement_documents(result, output_dir)


def _golden_bytes(path: Path) -> bytes:
    """Account only for the source file's conventional final newline.

    ``json.dump`` deliberately emits no final newline. Text fixtures tracked in the repository
    do, so the comparison removes exactly that transport newline and no JSON content.
    """
    content = path.read_bytes()
    return content[:-1] if content.endswith(b"\n") else content


@pytest.mark.parametrize("case_dir", CASES, ids=lambda path: path.name)
def test_lineage_and_diagnostics_match_golden_bytes(
    case_dir: Path,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _render(case_dir, first)
    _render(case_dir, second)

    for name in ("lineage.json", "diagnostics.json"):
        expected = _golden_bytes(case_dir / name)
        first_bytes = (first / name).read_bytes()
        second_bytes = (second / name).read_bytes()
        assert first_bytes == expected
        assert second_bytes == expected
        assert second_bytes == first_bytes


def test_baseline_covers_the_required_contract_shapes() -> None:
    assert {case.name for case in CASES} == {
        "simple_insert",
        "complex_scope",
        "merge",
        "star_without_schema",
        "target_ddl_binding",
        "syntax_recovered",
        "directory_target",
        "self_join",
        "fact_gap",
        "special_literals",
    }


def test_pure_writer_rejects_dangling_scope_before_contract_files_exist(
    tmp_path: Path,
) -> None:
    result = parse_scope_lineage(
        "INSERT INTO mart.t SELECT id FROM ods.source",
        "dangling_scope",
        schema={"ods.source": ["id"]},
    )
    result.scope_graph.edges.append(ScopeGraphEdge("MISSING_SCOPE", "ROOT"))
    output_dir = tmp_path / "invalid"

    with pytest.raises(ValueError, match="Cross-reference validation failed"):
        write_statement_documents(result, output_dir)

    assert not (output_dir / "lineage.json").exists()
    assert not (output_dir / "diagnostics.json").exists()
