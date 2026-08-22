"""Conversion and writing for the task-level Lineage 2.0 contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from ..scope.task_lineage import TaskLineageResult
from .lineage import _diagnostics_summary
from .validation import (
    validate_cross_references,
    validate_diagnostics_document,
    validate_lineage_document,
)


def to_task_lineage_dict(result: TaskLineageResult) -> dict:
    state_graph = copy.deepcopy(result.table_state_graph)
    state_graph.pop("nodes_by_id", None)
    return {
        "schema_version": "2.0",
        "artifact_kind": "task_lineage",
        "task_id": result.task_id,
        "parse_status": result.parse_status,
        "syntax_status": result.syntax_status,
        "syntax_errors": copy.deepcopy(result.syntax_errors),
        "analysis_status": copy.deepcopy(result.analysis_status),
        "statement_sequence": copy.deepcopy(result.statements),
        "table_state_graph": state_graph,
        "final_table_states": copy.deepcopy(result.final_table_states),
        "statement_lineage": copy.deepcopy(result.statement_lineage),
        "end_to_end_lineage": copy.deepcopy(result.end_to_end_lineage),
        "task_dependencies": copy.deepcopy(result.task_dependencies),
        "diagnostics": copy.deepcopy(result.diagnostics),
    }


def to_task_lineage_json(result: TaskLineageResult, indent: int = 2) -> str:
    return json.dumps(
        to_task_lineage_dict(result),
        ensure_ascii=False,
        indent=indent,
        default=str,
    )


def write_task_lineage(
    result: TaskLineageResult,
    output_dir: str | Path,
    *,
    compact: bool = False,
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data = to_task_lineage_dict(result)
    xref_errors = validate_cross_references(data)
    if xref_errors:
        raise ValueError(
            f"Cross-reference validation failed ({len(xref_errors)} errors):\n"
            + "\n".join(xref_errors[:5])
        )

    diagnostics_full = _canonical_diagnostics(data["diagnostics"])
    statement_diagnostics = {}
    lineage_data = copy.deepcopy(data)
    for statement_id, statement in lineage_data["statement_lineage"].items():
        full = _canonical_diagnostics(statement.get("diagnostics") or {})
        statement_diagnostics[statement_id] = full
        statement["diagnostics"] = _diagnostics_summary(full)
        # The v2 schema types statement_lineage values as bare objects, so the outer
        # validation below never looks inside them. Each nested document claims the v1
        # contract; hold it to that here, in the exact form it is about to be published
        # (i.e. with its diagnostics already summarized), or a drifted nested document
        # ships without complaint (NESTEDVAL-001).
        validate_lineage_document(statement)
    lineage_data["diagnostics"] = _diagnostics_summary(diagnostics_full)
    diagnostics_data = {
        "schema_version": "2.0",
        "task_id": result.task_id,
        "analysis_status": copy.deepcopy(result.analysis_status),
        **diagnostics_full,
        "statement_diagnostics": statement_diagnostics,
    }
    validate_lineage_document(lineage_data)
    validate_diagnostics_document(diagnostics_data)
    dump_options = {
        "ensure_ascii": False,
        "indent": None if compact else 2,
        "separators": (",", ":") if compact else None,
        "default": str,
    }
    with open(output_dir / "lineage.json", "w", encoding="utf-8") as stream:
        json.dump(lineage_data, stream, **dump_options)
    with open(output_dir / "diagnostics.json", "w", encoding="utf-8") as stream:
        json.dump(diagnostics_data, stream, **dump_options)
    return output_dir


def _canonical_diagnostics(diagnostics: dict) -> dict:
    result = copy.deepcopy(diagnostics)
    result["warnings"] = sorted(
        result.get("warnings") or [],
        key=lambda item: (
            str(item.get("statement_id") or ""),
            str(item.get("scope") or ""),
            str(item.get("type") or ""),
            str(item.get("msg") or ""),
        ),
    )
    result["lineage_fact_gaps"] = sorted(
        result.get("lineage_fact_gaps") or [],
        key=lambda item: (
            str(item.get("statement_id") or ""),
            str(item.get("evidence_path") or ""),
            str(item.get("gap_type") or ""),
            str(item.get("target_table") or ""),
        ),
    )
    return result
