"""SourceRef construction, conversion, dedupe, and resolution normalization."""
from __future__ import annotations

from typing import cast

from .fact_protocols import ExpressionResolution
from .scope_types import (
    AMBIGUOUS_SCOPE_ID,
    CONSTANT_SCOPE_ID,
    NON_PHYSICAL_SOURCE_SCOPES,
    ScopeLineageResult,
    ScopeOutputField,
    SourceRef,
    SYSTEM_SCOPE_ID,
)
from .sequences import _unique_ordered

def _source_type_from_id(source_id: str) -> str:
    # AMBIGUOUS has no colon, so without this it classified as a physical table and downstream
    # reported a table literally named "AMBIGUOUS" (LINEAGE-002).
    if not source_id or source_id in {"UNKNOWN", AMBIGUOUS_SCOPE_ID}:
        return "unknown"
    return "scope" if ":" in source_id or source_id == "ROOT" else "physical_table"


def _source_refs_from_detail_fields(items: list[dict | None]) -> list[SourceRef]:
    refs: list[SourceRef] = []
    for item in items:
        if not item:
            continue
        scope = item.get("scope")
        column = item.get("column")
        if scope and column:
            refs.append(SourceRef(
                scope=scope,
                column=column,
                qualifier=item.get("qualifier"),
                binding_scope_id=item.get("binding_scope_id"),
                input_ref_id=item.get("input_ref_id"),
            ))
    return refs


def _source_ref_binding_key(ref: SourceRef) -> tuple[str, str, str, str, str]:
    """Return the field identity without collapsing distinct SQL input occurrences."""
    return (
        str(ref.scope or ""),
        str(ref.column or ""),
        str(ref.binding_scope_id or ""),
        str(ref.input_ref_id or ""),
        str(ref.qualifier or ""),
    )


def _is_internal_scope_id(scope_id: str) -> bool:
    return scope_id == "ROOT" or scope_id.startswith(("cte:", "subq:", "union:", "udtf:"))


def _is_cross_join_type(join_type: str | None) -> bool:
    return str(join_type or "").upper() == "CROSS"


def _physical_source_ids_for_input(
    result: ScopeLineageResult,
    source_id: str,
    *,
    seen: set[str] | None = None,
    memo: dict[str, list[str]] | None = None,
) -> list[str]:
    if memo is None:
        memo = {}
    if not source_id or source_id == "UNKNOWN":
        return []
    if source_id in memo:
        return list(memo[source_id])
    if _source_type_from_id(source_id) == "physical_table":
        memo[source_id] = [source_id]
        return [source_id]
    if seen is None:
        seen = set()
    if source_id in seen:
        return []
    seen.add(source_id)
    scope_data = result.scopes.get(source_id)
    if scope_data is None:
        memo[source_id] = []
        return []
    physical_sources: list[str] = []
    for edge in scope_data.input_edges:
        for physical_source in _physical_source_ids_for_input(
            result,
            edge.source_id,
            seen=set(seen),
            memo=memo,
        ):
            if physical_source not in physical_sources:
                physical_sources.append(physical_source)
    if not physical_sources:
        for column in scope_data.columns:
            for ref in column.sources:
                if ref.scope in NON_PHYSICAL_SOURCE_SCOPES:
                    continue
                for physical_source in _physical_source_ids_for_input(
                    result,
                    ref.scope,
                    seen=set(seen),
                    memo=memo,
                ):
                    if physical_source not in physical_sources:
                        physical_sources.append(physical_source)
    memo[source_id] = list(physical_sources)
    return list(physical_sources)


def _source_ref_to_dict(ref: SourceRef) -> dict[str, object]:
    item: dict[str, object] = {"scope": ref.scope, "column": ref.column}
    if ref.candidates:
        item["candidates"] = [dict(candidate) for candidate in ref.candidates]
    if ref.qualifier:
        item["qualifier"] = ref.qualifier
    if ref.binding_scope_id:
        item["binding_scope_id"] = ref.binding_scope_id
    if ref.input_ref_id:
        item["input_ref_id"] = ref.input_ref_id
    return item


def _generated_sources_from_refs(refs: list[SourceRef], transform: str | None = None) -> list[dict[str, str]]:
    generated: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        if ref.scope not in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}:
            continue
        generated_transform = ref.scope if ref.scope in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID} else (transform or "")
        key = (ref.scope, ref.column, generated_transform)
        if key in seen:
            continue
        seen.add(key)
        generated.append(
            {
                "source_type": ref.scope,
                "value": ref.column,
                "transform": generated_transform,
            }
        )
    return generated


def _source_kind_for_resolution(
    physical_fields: list[dict],
    generated_sources: list[dict[str, str]],
    rowset_sources: list[dict[str, str]] | None = None,
    *,
    rowset_dominates_generated: bool = False,
) -> str:
    if rowset_dominates_generated and rowset_sources and not physical_fields:
        return "rowset"
    kinds = sum(
        bool(items)
        for items in (
            physical_fields,
            generated_sources,
            rowset_sources or [],
        )
    )
    if kinds > 1:
        return "mixed"
    if physical_fields:
        return "physical"
    if generated_sources:
        return "generated"
    if rowset_sources:
        return "rowset"
    return "unresolved"


def _normalize_expression_resolution(
    resolution: "ExpressionResolution",
    *,
    scope_id: str | None = None,
    field: str | None = None,
    expression: str | None = None,
) -> "ExpressionResolution":
    physical_fields = [
        dict(item)
        for item in resolution.get("physical_source_fields") or []
        if isinstance(item, dict)
    ]
    generated_sources = _dedupe_generated_source_dicts(
        [
            dict(item)
            for item in resolution.get("generated_sources") or []
            if isinstance(item, dict)
        ]
    )
    rowset_sources = _dedupe_rowset_source_dicts(
        [
            dict(item)
            for item in resolution.get("rowset_sources") or []
            if isinstance(item, dict)
        ]
    )
    union_branch_mappings = [
        dict(item)
        for item in resolution.get("union_branch_mappings") or []
        if isinstance(item, dict)
    ]
    missing_reasons = [
        str(reason)
        for reason in resolution.get("missing_reasons") or []
        if reason
    ]
    status = str(resolution.get("status") or "unresolved")
    source_kind = str(
        resolution.get("source_kind")
        or _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources)
    )
    if source_kind == "rowset" and not rowset_sources:
        rowset_sources = [
            {
                "source_type": "rowset",
                "scope": str(scope_id or resolution.get("scope") or ""),
                "field": str(field or resolution.get("field") or ""),
                "expression": str(expression or resolution.get("expanded_expression") or ""),
            }
        ]
    has_source_fact = bool(
        physical_fields
        or generated_sources
        or rowset_sources
        or union_branch_mappings
    )
    if status == "resolved" and not has_source_fact:
        status = "unresolved"
        source_kind = "unresolved"
        missing_reasons = _unique_ordered(
            [*missing_reasons, "resolved_without_source_fact"]
        )
    elif status in {"unresolved", "partially_resolved"} and not missing_reasons:
        missing_reasons = ["expression_resolution_incomplete"]
    normalized = {
        **resolution,
        "status": status,
        "physical_source_fields": physical_fields,
        "generated_sources": generated_sources,
        "source_kind": source_kind,
        "missing_reasons": missing_reasons,
    }
    if expression and not normalized.get("expanded_expression"):
        normalized["expanded_expression"] = str(expression)
    if rowset_sources:
        normalized["rowset_sources"] = rowset_sources
    elif "rowset_sources" in normalized:
        normalized.pop("rowset_sources", None)
    if union_branch_mappings:
        normalized["union_branch_mappings"] = union_branch_mappings
    if resolution.get("candidate_source_refs"):
        normalized["candidate_source_refs"] = list(resolution.get("candidate_source_refs") or [])
    if resolution.get("scope_output_trace"):
        normalized["scope_output_trace"] = list(resolution.get("scope_output_trace") or [])
    return cast(ExpressionResolution, normalized)


def _dedupe_generated_source_dicts(sources: list[dict]) -> list[dict]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or "")
        value = str(item.get("value") or "")
        transform = str(item.get("transform") or "")
        key = (source_type, value, transform)
        if not source_type or key in seen:
            continue
        seen.add(key)
        deduped.append({"source_type": source_type, "value": value, "transform": transform})
    return deduped


def _dedupe_rowset_source_dicts(sources: list[dict]) -> list[dict]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or "")
        scope = str(item.get("scope") or "")
        field = str(item.get("field") or "")
        expression = str(item.get("expression") or "")
        key = (source_type, scope, field, expression)
        if not source_type or key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "source_type": source_type,
                "scope": scope,
                "field": field,
                "expression": expression,
            }
        )
    return deduped


def _rowset_sources_from_upstream_output(
    source_id: str,
    field: str,
    upstream: ScopeOutputField,
) -> list[dict[str, str]]:
    resolution = upstream.expression_resolution or {}
    rowset_sources = [
        dict(item)
        for item in resolution.get("rowset_sources") or []
        if isinstance(item, dict)
    ]
    if rowset_sources:
        return _dedupe_rowset_source_dicts(rowset_sources)
    if str(resolution.get("source_kind") or "") != "rowset":
        return []
    return [
        {
            "source_type": "rowset",
            "scope": source_id,
            "field": field,
            "expression": str(upstream.expanded_expression or upstream.expression or ""),
        }
    ]


def _physical_source_fields_from_refs(refs: list[SourceRef]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if (
            not ref.scope
            or ref.scope in NON_PHYSICAL_SOURCE_SCOPES
            or _is_internal_scope_id(ref.scope)
        ):
            continue
        key = (ref.scope, ref.column)
        if key in seen:
            continue
        seen.add(key)
        fields.append({"table": ref.scope, "field": ref.column})
    return fields


def _physical_source_fields_for_ref(
    result: ScopeLineageResult,
    ref: SourceRef,
    *,
    seen: set[tuple[str, str]] | None = None,
) -> list[dict]:
    if not ref.scope or ref.scope in NON_PHYSICAL_SOURCE_SCOPES:
        return []
    if not _is_internal_scope_id(ref.scope):
        return [{"table": ref.scope, "field": ref.column}]

    if seen is None:
        seen = set()
    ref_key = (ref.scope, ref.column)
    if ref_key in seen:
        return []
    seen.add(ref_key)

    scope_data = result.scopes.get(ref.scope)
    if scope_data is None:
        return []
    output = next((item for item in scope_data.outputs if item.name == ref.column), None)
    if output is None:
        return []

    resolution = output.expression_resolution or {}
    physical_fields = resolution.get("physical_source_fields") or []
    if resolution.get("status") == "resolved" and physical_fields:
        return [dict(item) for item in physical_fields if isinstance(item, dict)]
    return []


def _physical_source_fields_for_refs(
    result: ScopeLineageResult,
    refs: list[SourceRef],
) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        for field in _physical_source_fields_for_ref(result, ref):
            key = (str(field.get("table") or ""), str(field.get("field") or ""))
            if not key[0] or not key[1] or key in seen:
                continue
            seen.add(key)
            fields.append(field)
    return fields


def _qualified_physical_field_sql(table: str, field: str) -> str:
    return f"`{table}`.`{field}`"


def _dedupe_physical_field_dicts(fields: list[dict]) -> list[dict]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in fields:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "")
        field = str(item.get("field") or "")
        key = (table, field)
        if not table or not field or key in seen:
            continue
        seen.add(key)
        deduped.append({"table": table, "field": field})
    return deduped


def _constant_sources(expression: str | None) -> list[SourceRef]:
    """Represent a literal as a traceable leaf instead of an empty lineage edge."""
    literal = expression if expression else "<constant>"
    return [SourceRef(scope=CONSTANT_SCOPE_ID, column=literal)]


def _system_sources(expression: str | None) -> list[SourceRef]:
    """Represent runtime/system expressions as traceable non-table leaves."""
    label = expression if expression else "<system>"
    return [SourceRef(scope=SYSTEM_SCOPE_ID, column=label)]
