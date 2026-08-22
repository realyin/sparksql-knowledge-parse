"""Star-passthrough output facts and UNION branch mapping."""
from __future__ import annotations

import re

from .scope_types import NON_PHYSICAL_SOURCE_SCOPES, ScopeData, ScopeLineageResult, ScopeOutputField
from .sequences import _unique_ordered
from .source_refs import (
    _dedupe_generated_source_dicts,
    _is_internal_scope_id,
    _qualified_physical_field_sql,
    _rowset_sources_from_upstream_output,
    _normalize_expression_resolution,
)

def _populate_union_output_branch_mappings(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        if scope_data.kind != "union":
            continue
        for output in scope_data.outputs:
            branch_mappings = _union_branch_mappings_for_output(result, scope_data, output.name)
            if not branch_mappings:
                continue
            resolution = dict(output.expression_resolution or {})
            resolution["union_branch_mappings"] = branch_mappings
            branch_statuses = [str(item.get("resolution_status") or "unresolved") for item in branch_mappings]
            if any(status != "resolved" for status in branch_statuses):
                resolved_count = sum(1 for status in branch_statuses if status == "resolved")
                resolution["status"] = "partially_resolved" if resolved_count else "unresolved"
                resolution["missing_reasons"] = _unique_ordered(
                    [
                        *[
                            str(reason)
                            for reason in resolution.get("missing_reasons") or []
                            if reason
                        ],
                        "union_branch_mapping_unresolved",
                    ]
                )
            output.expression_resolution = resolution


def _union_branch_mappings_for_output(
    result: ScopeLineageResult,
    union_scope: ScopeData,
    output_name: str,
) -> list[dict[str, object]]:
    alignment = union_scope.union_branch_alignment or {}
    for item in alignment.get("field_alignment") or []:
        if item.get("aligned_output_name") != output_name:
            continue
        mappings: list[dict[str, object]] = []
        for branch in item.get("branch_items") or []:
            branch_scope_id = str(branch.get("branch_id") or "")
            branch_output_name = str(branch.get("output_name") or output_name or "")
            branch_output, branch_match_evidence = _union_branch_scope_output_for_mapping(
                result,
                branch_scope_id,
                branch_output_name,
                branch.get("position"),
            )
            actual_output_name = branch_output.name if branch_output is not None else branch_output_name
            branch_resolution = (
                dict(branch_output.expression_resolution or {})
                if branch_output is not None
                else dict(branch.get("expression_resolution") or {})
            )
            # Normalize the branch's own resolution before copying its source lists.
            # This pass runs from _populate_union_output_branch_mappings, which
            # scope_facts calls immediately BEFORE _normalize_scope_expression_resolutions
            # -- so a branch classified 'rowset' still has its rowset_sources unsynthesized
            # here. Copying the empty list made the gap detector re-derive 'unresolved' from
            # three empty lists and raise a root-impact gap for COUNT(1) and bare OVER ().
            # The union scope's own normalization must still happen after this function,
            # because it consumes the mappings we are building.
            branch_resolution = _normalize_expression_resolution(
                branch_resolution,
                scope_id=branch_scope_id,
                field=actual_output_name,
                expression=(
                    branch_resolution.get("expanded_expression")
                    or (branch_output.expanded_expression if branch_output is not None else None)
                    or branch.get("expanded_expression")
                    or branch.get("expression_sql")
                ),
            )
            missing_reasons = [
                str(reason)
                for reason in branch_resolution.get("missing_reasons") or []
                if reason
            ]
            if branch.get("missing") and not missing_reasons:
                missing_reasons = ["union_branch_output_missing"]
            elif str(branch_resolution.get("status") or "unresolved") != "resolved" and not missing_reasons:
                missing_reasons = ["expression_resolution_incomplete"]
            expression_sql = (
                branch_resolution.get("expanded_expression")
                or (branch_output.expanded_expression if branch_output is not None else None)
                or branch.get("expanded_expression")
                or branch.get("expression_sql")
            )
            mappings.append(
                {
                    "branch_scope_id": branch_scope_id or branch.get("branch_id"),
                    "branch_index": branch.get("branch_index"),
                    "output_field": actual_output_name,
                    "aligned_output_name": item.get("aligned_output_name"),
                    "expected_output_name": branch_output_name,
                    "expected_position": branch.get("position"),
                    **branch_match_evidence,
                    "expression_sql": expression_sql,
                    "physical_source_fields": branch_resolution.get("physical_source_fields") or [],
                    "generated_sources": branch_resolution.get("generated_sources") or [],
                    "rowset_sources": branch_resolution.get("rowset_sources") or [],
                    "resolution_status": branch_resolution.get("status") or "unresolved",
                    "resolution_type": branch_resolution.get("resolution_type"),
                    "missing_reasons": missing_reasons,
                }
            )
        return mappings
    return []


def _union_branch_scope_output_for_mapping(
    result: ScopeLineageResult,
    branch_scope_id: str,
    output_name: str,
    position: object,
) -> tuple[ScopeOutputField | None, dict[str, object]]:
    if not branch_scope_id:
        return None, {}
    branch_scope = result.scopes.get(branch_scope_id)
    if branch_scope is None:
        return None, {}
    evidence: dict[str, object] = {
        "available_branch_outputs": [output.name for output in branch_scope.outputs],
    }
    name_match = next((output for output in branch_scope.outputs if output.name == output_name), None)
    try:
        position_index = int(position) - 1
    except (TypeError, ValueError):
        return name_match, evidence
    if 0 <= position_index < len(branch_scope.outputs):
        position_match = branch_scope.outputs[position_index]
        if name_match is not None and name_match is not position_match:
            evidence["candidate_rejection_reasons"] = [
                {
                    "candidate_output": name_match.name,
                    "reason": "name_match_not_at_alignment_position",
                }
            ]
        return position_match, evidence
    return name_match, evidence


def _star_passthrough_output_fact(
    result: ScopeLineageResult,
    scope_id: str,
    field: str,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
    *,
    seen: set[tuple[str, str]] | None = None,
) -> dict[str, object] | None:
    """Resolve a missing output field through explicit SELECT * passthrough scopes."""
    if seen is None:
        seen = set()
    key = (scope_id, field)
    if key in seen:
        return None
    seen.add(key)

    output = output_lookup.get((scope_id, field))
    if output is not None:
        resolution = output.expression_resolution or {}
        physical_fields = resolution.get("physical_source_fields") or []
        generated_sources = resolution.get("generated_sources") or []
        rowset_sources = _rowset_sources_from_upstream_output(scope_id, field, output)
        expanded_expression = str(
            resolution.get("expanded_expression")
            or output.expanded_expression
            or ""
        )
        if (
            resolution.get("status") == "resolved"
            and (physical_fields or generated_sources or rowset_sources or resolution.get("source_kind") == "rowset")
            and expanded_expression
        ):
            return {
                "expanded_expression": expanded_expression,
                "physical_source_fields": [dict(item) for item in physical_fields if isinstance(item, dict)],
                "generated_sources": _dedupe_generated_source_dicts(
                    [dict(item) for item in generated_sources if isinstance(item, dict)]
                ),
                "rowset_sources": rowset_sources,
            }

    scope_data = result.scopes.get(scope_id)
    if scope_data is None:
        return None

    star_outputs = [item for item in scope_data.outputs if item.name == "*"]
    for star_output in star_outputs:
        for source in star_output.sources:
            fact = _star_passthrough_source_fact(result, source.scope, source.column, field, output_lookup, seen)
            if fact is not None:
                return fact

    if _scope_raw_sql_is_star_select(scope_data.raw_sql):
        for edge in scope_data.input_edges:
            fact = _star_passthrough_source_fact(result, edge.source_id, "*", field, output_lookup, seen)
            if fact is not None:
                return fact
        for dependency in scope_data.depends_on:
            fact = _star_passthrough_source_fact(result, dependency, "*", field, output_lookup, seen)
            if fact is not None:
                return fact
    return None


def _star_passthrough_source_fact(
    result: ScopeLineageResult,
    source_id: str,
    source_column: str,
    requested_field: str,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
    seen: set[tuple[str, str]],
) -> dict[str, object] | None:
    if not source_id or source_id in NON_PHYSICAL_SOURCE_SCOPES:
        return None
    field = requested_field if source_column == "*" else source_column
    if _is_internal_scope_id(source_id):
        return _star_passthrough_output_fact(result, source_id, field, output_lookup, seen=seen)
    if field == "*":
        return None
    physical_field = {"table": source_id, "field": field}
    return {
        "expanded_expression": _qualified_physical_field_sql(source_id, field),
        "physical_source_fields": [physical_field],
    }


def _scope_raw_sql_is_star_select(raw_sql: str | None) -> bool:
    if not raw_sql:
        return False
    return bool(re.match(r"(?is)^\s*SELECT\s+(?:DISTINCT\s+)?\*\s+FROM\b", raw_sql))
