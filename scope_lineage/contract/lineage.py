"""Pure conversion and writer for the versioned Lineage contract.

This module intentionally has no dependency on Profile, Insight, Preset, Refactor, or Pipeline.
"""

from __future__ import annotations

import copy as _copy
import json
from pathlib import Path
from typing import Any

from ..scope.scope_types import (
    DiagnosticWarning,
    Diagnostics,
    ScopeColumn,
    ScopeData,
    ScopeFieldUsage,
    ScopeFilter,
    ScopeGraph,
    ScopeGraphEdge,
    ScopeInputEdge,
    ScopeJoin,
    ScopeLineageResult,
    ScopeLogicBlock,
    ScopeOutputField,
    SourceRef,
)
from ..scope.end_to_end import build_end_to_end_lineage
from ..scope.parser import resolve_display_expression
from ..serialize.scope_profile import build_scope_profile
from .validation import validate_cross_references, validate_lineage_document

def to_dict(obj: Any) -> Any:
    """Recursively convert a dataclass (or nested structure) to a JSON-safe dict.

    - ScopeGraphEdge.from_ -> {"from": ..., "to": ...}
    - None fields are omitted
    - Empty lists/dicts are kept (they convey "no entries")
    """
    if isinstance(obj, ScopeLineageResult):
        return _result_to_dict(obj)
    if isinstance(obj, ScopeData):
        return _scope_data_to_dict(obj)
    if isinstance(obj, ScopeColumn):
        return _scope_column_to_dict(obj)
    if isinstance(obj, ScopeGraphEdge):
        return obj.to_dict()
    if isinstance(obj, ScopeGraph):
        return _scope_graph_to_dict(obj)
    if isinstance(obj, SourceRef):
        ref = {"scope": obj.scope, "column": obj.column}
        if obj.candidates:
            # Only present on AMBIGUOUS refs. Losing these would leave a consumer knowing the
            # column is undetermined without knowing between what (LINEAGE-002).
            ref["candidates"] = [dict(item) for item in obj.candidates]
        if obj.qualifier:
            ref["qualifier"] = obj.qualifier
        if obj.binding_scope_id:
            ref["binding_scope_id"] = obj.binding_scope_id
        if obj.input_ref_id:
            ref["input_ref_id"] = obj.input_ref_id
        return ref
    if isinstance(obj, ScopeJoin):
        return _scope_join_to_dict(obj)
    if isinstance(obj, ScopeFilter):
        return _scope_filter_to_dict(obj)
    if isinstance(obj, ScopeFieldUsage):
        return _scope_field_usage_to_dict(obj)
    if isinstance(obj, ScopeInputEdge):
        return _scope_input_edge_to_dict(obj)
    if isinstance(obj, ScopeLogicBlock):
        return _scope_logic_block_to_dict(obj)
    if isinstance(obj, ScopeOutputField):
        return _scope_output_field_to_dict(obj)
    if isinstance(obj, Diagnostics):
        return _diagnostics_to_dict(obj)
    if isinstance(obj, DiagnosticWarning):
        return {"type": obj.type, "scope": obj.scope, "msg": obj.msg}
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def to_json(result: ScopeLineageResult, indent: int = 2) -> str:
    """Serialize a ScopeLineageResult to a JSON string."""
    return json.dumps(to_dict(result), ensure_ascii=False, indent=indent, default=str)


def _diagnostics_summary(diagnostics: dict) -> dict:
    warnings = diagnostics.get("warnings") or []
    warning_types: dict[str, int] = {}
    for warning in warnings:
        warning_type = warning.get("type") if isinstance(warning, dict) else None
        warning_type = warning_type or "UNKNOWN"
        warning_types[warning_type] = warning_types.get(warning_type, 0) + 1
    lineage_fact_gaps = diagnostics.get("lineage_fact_gaps") or []
    gap_types: dict[str, int] = {}
    for gap in lineage_fact_gaps:
        gap_type = gap.get("gap_type") if isinstance(gap, dict) else None
        gap_type = gap_type or "UNKNOWN"
        gap_types[gap_type] = gap_types.get(gap_type, 0) + 1
    return {
        "fallback_used": bool(diagnostics.get("fallback_used")),
        "warning_count": len(warnings),
        "warning_types": warning_types,
        "lineage_fact_gap_count": len(lineage_fact_gaps),
        "lineage_fact_gap_types": gap_types,
        "lineage_fact_gap_samples": lineage_fact_gaps[:5],
        "stats": diagnostics.get("stats") or {},
        "full_diagnostics_file": "diagnostics.json",
    }



def to_lineage_dict(result: ScopeLineageResult) -> dict:
    """Convert a parser result into the complete Lineage contract document."""
    return to_dict(result)


def to_lineage_json(result: ScopeLineageResult, indent: int = 2) -> str:
    """Serialize the complete Lineage contract document."""
    return to_json(result, indent=indent)


def write_lineage(
    result: ScopeLineageResult,
    output_dir: str | Path,
    *,
    compact: bool = False,
) -> Path:
    """Validate and write only lineage.json plus diagnostics.json."""
    import warnings as _warnings

    # Deprecation applies to the standalone v1 ARTIFACT, not to the statement->dict
    # converter: the v2 task document builds its statement_lineage entries through
    # to_lineage_dict, which therefore stays undeprecated.
    _warnings.warn(
        "write_lineage emits the deprecated contract-1.0 artifact, scheduled for "
        "removal; write_task_lineage (contract 2.0) replaces it",
        DeprecationWarning,
        stacklevel=2,
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    data = to_lineage_dict(result)
    xref_errors = validate_cross_references(data)
    if xref_errors:
        raise ValueError(
            f"Cross-reference validation failed ({len(xref_errors)} errors):\\n"
            + "\\n".join(xref_errors[:5])
        )

    diagnostics_full = data.get("diagnostics", {})
    lineage_data = _copy.deepcopy(data)
    lineage_data["diagnostics"] = _diagnostics_summary(diagnostics_full)
    diagnostics_data = {"schema_version": "1.0", **diagnostics_full}
    validate_lineage_document(lineage_data)
    from .validation import validate_diagnostics_document

    validate_diagnostics_document(diagnostics_data)
    with open(output_dir / "lineage.json", "w", encoding="utf-8") as stream:
        json.dump(
            lineage_data,
            stream,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            default=str,
        )
    with open(output_dir / "diagnostics.json", "w", encoding="utf-8") as stream:
        json.dump(
            diagnostics_data,
            stream,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            default=str,
        )
    return output_dir

def _result_to_dict(r: ScopeLineageResult) -> dict:
    d = {
        "schema_version": "1.0",
        "task_id": r.task_id,
        "target_table": r.target_table,
        "stmt_kind": r.stmt_kind,
        # Optional, present only when true: a CACHE ... AS SELECT builds its relation from
        # a SELECT exactly as CTAS does, but the relation lives for the session. Consumers
        # registering data assets need that distinction, and stmt_kind is a closed enum
        # this major contract version cannot widen (CACHE-001).
        **({"is_cached_relation": True} if r.is_cached_relation else {}),
        **(
            {"is_session_scoped_relation": True}
            if r.is_session_scoped_relation
            else {}
        ),
        # "ok" | "failed" — a failed statement still writes artifacts (diagnosable, non-blocking),
        # so consumers need this to avoid treating an empty scope set as a valid parse
        "parse_status": r.parse_status,
        # "strict_ok" | "recovered" — whether the SQL parsed as written, or only after sqlglot
        # dropped tokens it could not place. "recovered" lineage may describe a query that
        # cannot run; `syntax_errors` carries the position and token window of each repair.
        "syntax_status": r.syntax_status,
        "syntax_errors": r.syntax_errors,
        # Only emitted when the script contained statements this tool does not model,
        # so its absence means "nothing was skipped", not "unknown" (CONTRACT-001).
        **({"skipped_statements": r.skipped_statements} if r.skipped_statements else {}),
        "target_partition_spec": r.target_partition_spec,
        "target_partition_columns": r.target_partition_columns,
        "target_partition_mode": r.target_partition_mode,
        **(
            {"target_field_binding": to_dict(r.target_field_binding)}
            if r.target_field_binding
            else {}
        ),
        # Only when there is no binding, and only to say which of the four unrelated
        # reasons it is: three are harmless, one means the projection may land in the
        # wrong columns. A separate optional key rather than an always-present
        # target_field_binding, so a consumer testing for that key keeps the behaviour
        # it has.
        **(
            {"target_binding_absent_reason": r.target_binding_absence}
            if not r.target_field_binding and r.target_binding_absence
            else {}
        ),
        "task_dependencies": _task_dependencies_to_dict(r.task_dependencies),
        "source_tables": r.source_tables,
        "related_metadata": r.related_metadata,
        "scope_graph": to_dict(r.scope_graph),
        "scopes": {k: to_dict(v) for k, v in r.scopes.items()},
        "field_mapping_chains": _dedupe_chain_expressions(to_dict(r.field_mapping_chains)),
        "scope_profile": build_scope_profile(r),
        "end_to_end_lineage": build_end_to_end_lineage(r),
        "diagnostics": to_dict(r.diagnostics),
    }
    return d


def _task_dependencies_to_dict(task_dependencies: dict | None) -> dict:
    task_dependencies = task_dependencies or {}
    upstream = task_dependencies.get("upstream_tasks") or []
    downstream = task_dependencies.get("downstream_tasks") or []
    source_summary = task_dependencies.get("source_summary") or {}
    return {
        "upstream_tasks": upstream,
        "downstream_tasks": downstream,
        "source_summary": {
            "source_format": source_summary.get("source_format") or "none",
            "upstream_count": source_summary.get("upstream_count", len(upstream)),
            "downstream_count": source_summary.get("downstream_count", len(downstream)),
            "has_declared_task_dependencies": bool(
                source_summary.get("has_declared_task_dependencies")
                or upstream
                or downstream
            ),
        },
    }


def _scope_data_to_dict(sd: ScopeData) -> dict:  # noqa: C901 - legacy exemption (WI-11): shrink when next touched
    d: dict[str, Any] = {"kind": sd.kind}
    if sd.role is not None:
        d["role"] = sd.role
    if sd.distinct:
        d["distinct"] = sd.distinct
    d["depends_on"] = sd.depends_on if sd.depends_on else []
    if sd.writes_to is not None:
        d["writes_to"] = sd.writes_to
    if sd.alias_in_parent is not None:
        d["alias_in_parent"] = sd.alias_in_parent
    if sd.raw_sql is not None:
        d["raw_sql"] = sd.raw_sql
    if sd.raw_sql_available:
        d["raw_sql_available"] = sd.raw_sql_available
    if sd.raw_sql_quality:
        d["raw_sql_quality"] = to_dict(sd.raw_sql_quality)
    if sd.source_coverage:
        d["source_coverage"] = to_dict(sd.source_coverage)
    if sd.input_edges:
        d["input_edges"] = [to_dict(edge) for edge in sd.input_edges]
    if sd.input_source_refs:
        d["input_source_refs"] = to_dict(sd.input_source_refs)
    if sd.alias_source_bindings:
        d["alias_source_bindings"] = to_dict(sd.alias_source_bindings)
    if sd.expression_source_bindings:
        d["expression_source_bindings"] = to_dict(sd.expression_source_bindings)
    if sd.union_branch_alignment:
        d["union_branch_alignment"] = to_dict(sd.union_branch_alignment)
    if sd.logic_blocks:
        d["logic_blocks"] = [to_dict(block) for block in sd.logic_blocks]
    if sd.outputs:
        d["outputs"] = [to_dict(output) for output in sd.outputs]
    if sd.field_usage:
        d["field_usage"] = [to_dict(usage) for usage in sd.field_usage]
    d["columns"] = [to_dict(c) for c in sd.columns] if sd.columns else []
    if sd.joins:
        d["joins"] = [to_dict(j) for j in sd.joins]
    if sd.filters:
        d["filters"] = [to_dict(f) for f in sd.filters]
    if sd.group_by:
        d["group_by"] = [to_dict(g) for g in sd.group_by]
    if sd.having:
        d["having"] = [to_dict(h) for h in sd.having]
    if sd.order_by:
        d["order_by"] = sd.order_by
    if sd.lateral_views:
        d["lateral_views"] = to_dict(sd.lateral_views)
    if sd.set_op is not None:
        d["set_op"] = sd.set_op
    if sd.branches is not None:
        d["branches"] = sd.branches
    if sd.branch_index is not None:
        d["branch_index"] = sd.branch_index
    _stamp_display_expressions(d)
    return d


def _stamp_display_expressions(scope_dict: dict[str, Any]) -> None:
    """Add `display_expression` next to each verbatim expression in a serialized scope, with the
    local FROM aliases resolved to their real table — but ONLY when resolution actually changes
    the text. The lineage facts (`expression` / `expression_resolution`) are untouched; this is a
    separate display form so every consumer that renders an expression reads it instead of
    re-deriving alias resolution. Owned by the parser (`resolve_display_expression`)."""
    bindings = scope_dict.get("alias_source_bindings") or []
    if not bindings:
        return

    def stamp(item: dict[str, Any], *source_keys: str) -> None:
        expr = next((item.get(key) for key in source_keys if item.get(key)), None)
        if not expr:
            return
        resolved = resolve_display_expression(expr, bindings)
        if resolved and resolved != expr:
            item["display_expression"] = resolved

    for output in scope_dict.get("outputs") or []:
        stamp(output, "expression")
    for column in scope_dict.get("columns") or []:
        stamp(column, "expression")
    for block in scope_dict.get("logic_blocks") or []:
        stamp(block, "normalized_expression", "raw_expression")


def _scope_column_to_dict(c: ScopeColumn) -> dict:
    d: dict[str, Any] = {"name": c.name, "transform": c.transform}
    if c.transform_subkind is not None:
        d["transform_subkind"] = c.transform_subkind
    if c.expression is not None:
        d["expression"] = c.expression
    d["sources"] = [to_dict(s) for s in c.sources] if c.sources else []
    if c.case_branches is not None:
        d["case_branches"] = to_dict(c.case_branches)
    if c.window is not None:
        d["window"] = to_dict(c.window)
    if c.agg_function is not None:
        d["agg_function"] = c.agg_function
    if c.branches is not None:
        d["branches"] = to_dict(c.branches)
    if c.merge_branch is not None:
        d["merge_branch"] = c.merge_branch
    if c.merge_branch_qualifier is not None:
        d["merge_branch_qualifier"] = c.merge_branch_qualifier
    if c.merge_when_index is not None:
        d["merge_when_index"] = c.merge_when_index
    if c.parsed_name is not None:
        d["parsed_name"] = c.parsed_name
    if c.target_column_ordinal is not None:
        d["target_column_ordinal"] = c.target_column_ordinal
    if c.target_field_resolution is not None:
        d["target_field_resolution"] = c.target_field_resolution
    if c.target_field_corrected is not None:
        d["target_field_corrected"] = c.target_field_corrected
    if c.target_metadata_table is not None:
        d["target_metadata_table"] = c.target_metadata_table
    return d


def _scope_graph_to_dict(g: ScopeGraph) -> dict:
    return {
        "nodes": g.nodes,
        "edges": [e.to_dict() for e in g.edges] if g.edges else [],
    }


def _scope_filter_to_dict(f: ScopeFilter) -> dict:
    d: dict[str, Any] = {"expression": f.expression}
    if f.columns:
        d["columns"] = [to_dict(c) for c in f.columns]
    return d


def _scope_input_edge_to_dict(edge: ScopeInputEdge) -> dict:
    d: dict[str, Any] = {
        "source_id": edge.source_id,
        "source_type": edge.source_type,
        "position": edge.position,
    }
    if edge.alias is not None:
        d["alias"] = edge.alias
    if edge.join_type is not None:
        d["join_type"] = edge.join_type
    if edge.join_condition is not None:
        d["join_condition"] = edge.join_condition
    if edge.join_fields:
        d["join_fields"] = [to_dict(ref) for ref in edge.join_fields]
    return d


def _scope_logic_block_to_dict(block: ScopeLogicBlock) -> dict:
    d: dict[str, Any] = {
        "logic_block_id": block.logic_block_id,
        "logic_type": block.logic_type,
    }
    if block.subtype is not None:
        d["subtype"] = block.subtype
    if block.raw_expression is not None:
        d["raw_expression"] = block.raw_expression
    if block.normalized_expression is not None:
        d["normalized_expression"] = block.normalized_expression
    if block.fingerprint is not None:
        d["fingerprint"] = block.fingerprint
    if block.fields:
        d["fields"] = [to_dict(ref) for ref in block.fields]
    if block.output_fields:
        d["output_fields"] = block.output_fields
    if block.join_type is not None:
        d["join_type"] = block.join_type
    if block.input_sources:
        d["input_sources"] = block.input_sources
    if block.field_usage:
        d["field_usage"] = [to_dict(usage) for usage in block.field_usage]
    if block.expression_features:
        d["expression_features"] = to_dict(block.expression_features)
    if block.final_target_columns:
        d["final_target_columns"] = block.final_target_columns
    if block.left_input is not None:
        d["left_input"] = block.left_input
    if block.right_input is not None:
        d["right_input"] = block.right_input
    if block.join_keys:
        d["join_keys"] = [to_dict(ref) for ref in block.join_keys]
    if block.join_relation_detail:
        d["join_relation_detail"] = to_dict(block.join_relation_detail)
    if block.filter_predicate_detail:
        d["filter_predicate_detail"] = to_dict(block.filter_predicate_detail)
    if block.window_specification:
        d["window_specification"] = to_dict(block.window_specification)
    if block.aggregation_detail:
        d["aggregation_detail"] = to_dict(block.aggregation_detail)
    return d


def _scope_field_usage_to_dict(usage: ScopeFieldUsage) -> dict:
    return {
        "source_id": usage.source_id,
        "source_type": usage.source_type,
        "used_fields": usage.used_fields,
        "used_field_details": to_dict(usage.used_field_details),
        "used_by_logic_blocks": usage.used_by_logic_blocks,
        "used_by_output_fields": usage.used_by_output_fields,
        "source_metadata": to_dict(usage.source_metadata),
    }



def _dedupe_chain_expressions(node):
    """Drop `expression_resolution.expanded_expression` inside field_mapping_chains when it
    repeats the text already on the same object.

    Applied to the chains only, deliberately. A chain is a derived VIEW over scope outputs —
    everything in it also exists under `scopes`, so removing a duplicate there costs no fact.
    The same duplication exists on scope outputs, but `expression_resolution.expanded_expression`
    is read directly there by consumers inside this repo alone, so the primary fact store keeps
    its shape and only the derived view is compacted (PERF-001).
    """
    if isinstance(node, list):
        return [_dedupe_chain_expressions(item) for item in node]
    if isinstance(node, dict):
        return _drop_redundant_expanded_expression(
            {key: _dedupe_chain_expressions(value) for key, value in node.items()}
        )
    return node


def _drop_redundant_expanded_expression(container: dict) -> dict:
    """Omit a nested `expression_resolution.expanded_expression` that repeats its parent's.

    Dropped only when byte-identical, and `expanded_expression_same_as_parent` is left in its
    place — a missing key must never read as "there is no expression here"; it reads as
    "read the parent's".
    """
    resolution = container.get("expression_resolution")
    parent = container.get("expanded_expression")
    if not isinstance(resolution, dict) or not isinstance(parent, str):
        return container
    if resolution.get("expanded_expression") != parent:
        return container
    trimmed = {k: v for k, v in resolution.items() if k != "expanded_expression"}
    trimmed["expanded_expression_same_as_parent"] = True
    container["expression_resolution"] = trimmed
    return container


def _scope_output_field_to_dict(output: ScopeOutputField) -> dict:
    d: dict[str, Any] = {
        "name": output.name,
        "transform": output.transform,
        "sources": [to_dict(ref) for ref in output.sources],
        "source_logic_blocks": output.source_logic_blocks,
        "downstream_fields": [to_dict(ref) for ref in output.downstream_fields],
        "target_columns": output.target_columns,
        "final_target_columns": output.final_target_columns,
    }
    if output.expression is not None:
        d["expression"] = output.expression
    if output.expanded_expression is not None:
        d["expanded_expression"] = output.expanded_expression
        # Emitted only when the expansion was cut short, so an artifact without these keys
        # means "fully expanded" and existing consumers are unaffected (PERF-001).
        if output.expansion_status != "full":
            d["expansion_status"] = output.expansion_status
            d["expansion_stop_reason"] = output.expansion_stop_reason
            d["unexpanded_refs"] = output.unexpanded_refs
    if output.expression_resolution:
        d["expression_resolution"] = to_dict(output.expression_resolution)
    if output.consumer_readiness:
        d["consumer_readiness"] = to_dict(output.consumer_readiness)
    if output.expression_type is not None:
        d["expression_type"] = output.expression_type
    if output.expression_features:
        d["expression_features"] = to_dict(output.expression_features)
    if output.expression_role is not None:
        d["expression_role"] = output.expression_role
    if output.grain_effect is not None:
        d["grain_effect"] = output.grain_effect
    if output.output_ordinal is not None:
        d["output_ordinal"] = output.output_ordinal
    if output.merge_branch is not None:
        d["merge_branch"] = output.merge_branch
    if output.merge_branch_qualifier is not None:
        d["merge_branch_qualifier"] = output.merge_branch_qualifier
    if output.merge_when_index is not None:
        d["merge_when_index"] = output.merge_when_index
    return d


def _diagnostics_to_dict(d: Diagnostics) -> dict:
    result: dict[str, Any] = {}
    if d.fallback_used:
        result["fallback_used"] = d.fallback_used
    if d.warnings:
        result["warnings"] = [to_dict(w) for w in d.warnings]
    if d.stats:
        result["stats"] = d.stats
    if d.lineage_fact_gaps:
        result["lineage_fact_gaps"] = to_dict(d.lineage_fact_gaps)
    return result


def _scope_join_to_dict(j: ScopeJoin) -> dict:
    d: dict[str, Any] = {
        "join_type": j.join_type,
        "left_scope": j.left_scope,
        "right_scope": j.right_scope,
    }
    if j.alias_in_parent is not None:
        d["alias_in_parent"] = j.alias_in_parent
    if j.condition_expression is not None:
        d["condition_expression"] = j.condition_expression
    if j.condition_columns:
        d["condition_columns"] = [to_dict(c) for c in j.condition_columns]
    return d


# Delegation shims preserve the free-function surface used by callers/tests.
