"""Shared leaf helpers for the scope domain.

Single home for helpers used by both the orchestrators and the implementation
modules - keeps the domain dependency graph one-directional (no import cycles)."""
from __future__ import annotations
import re
import sqlglot
from sqlglot import ErrorLevel
from sqlglot import exp
from sqlglot.optimizer.scope import Scope
from .parser import (
    _qualified_table,
)
from .scope_types import (
    AMBIGUOUS_SCOPE_ID,
    CONSTANT_SCOPE_ID,
    NON_PHYSICAL_SOURCE_SCOPES,
    ScopeData,
    ScopeLineageResult,
    ScopeOutputField,
    SYSTEM_SCOPE_ID,
    SourceRef,
)
from sqlglot.errors import OptimizeError


from ._constants import (  # noqa: F401 -- transitional re-export until WI-06 repoints importers
    DIALECT,
    PARSE_OPTS,
    _ORIGINALLY_UNQUALIFIED_META,
    _SCOPE_ID_ATTR,
)

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


from .expression_refs import (  # noqa: F401 -- transitional re-export until WI-06 repoints importers
    _cached_pattern,
    _inside_nested_subquery,
    _lambda_qualifiers,
    _qualified_field_refs,
    _qualified_pair_is_catalog_function_prefix,
    _strip_sql_comments,
    _strip_sql_string_literals,
    extract_qualified_field_refs,
)

from .expansion_budget import (  # noqa: F401 -- transitional re-export (WI-06)
    EXPANSION_MAX_CHARS,
    EXPANSION_MAX_SUBSTITUTIONS,
    ExpansionBudget,
)

from .expression_text import (  # noqa: F401 -- transitional re-export (WI-06)
    _function_names,
    _parenthesize_replacement_expression,
    _qualifier_present,
    _replace_qualified_ref_with_expression,
    _replace_unqualified_ref_with_expression,
    _unexpanded_bound_aliases_in_expression,
)

from .function_catalog import (  # noqa: F401 -- transitional re-export (WI-06)
    _AGGREGATE_FUNCTIONS,
    _CLEANING_FUNCTIONS,
    _KNOWN_SCALAR_FUNCTIONS,
    _KNOWN_UDAFS,
)

from .sequences import (  # noqa: F401 -- transitional re-export (WI-06)
    _extend_unique,
    _unique_ordered,
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
    physical_fields: list[dict[str, str]],
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
    resolution: dict[str, object],
    *,
    scope_id: str | None = None,
    field: str | None = None,
    expression: str | None = None,
) -> dict[str, object]:
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
    return normalized


def _dedupe_generated_source_dicts(sources: list[dict[str, str]]) -> list[dict[str, str]]:
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

def _dedupe_rowset_source_dicts(sources: list[dict[str, str]]) -> list[dict[str, str]]:
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
) -> list[dict[str, str]]:
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

def _resolve_expression_resolution_from_output_sources(result: ScopeLineageResult) -> None:
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for _scope_id, scope_data in result.scopes.items():
        for output in scope_data.outputs:
            if not output.sources:
                continue
            current_resolution = output.expression_resolution or {}
            if (
                current_resolution.get("physical_source_fields")
                or current_resolution.get("generated_sources")
                or current_resolution.get("source_kind") == "rowset"
            ):
                continue
            if not _all_source_refs_have_resolution(result, output.sources, output_lookup):
                continue
            refreshed = _resolved_expression_fact_from_source_refs(
                result,
                str(output.expression or ""),
                list(output.sources),
                output_lookup,
            )
            if not refreshed:
                continue
            output.expanded_expression = str(refreshed.get("expanded_expression") or output.expression or "")
            output.expression_resolution = refreshed["expression_resolution"]

def _all_source_refs_have_resolution(
    result: ScopeLineageResult,
    refs: list[SourceRef],
    output_lookup: dict[tuple[str, str], ScopeOutputField],
) -> bool:
    if not refs:
        return False
    for ref in refs:
        if ref.scope in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}:
            continue
        if ref.scope == "UNKNOWN":
            return False
        if _physical_source_fields_for_ref(result, ref):
            continue
        if _is_internal_scope_id(ref.scope) and _star_passthrough_output_fact(result, ref.scope, ref.column, output_lookup):
            continue
        return False
    return True

def _resolved_expression_fact_from_source_refs(
    result: ScopeLineageResult,
    expression: str,
    refs: list[SourceRef],
    output_lookup: dict[tuple[str, str], ScopeOutputField],
) -> dict[str, object] | None:
    if not expression or not refs:
        return None
    physical_fields = _physical_source_fields_for_refs(result, refs)
    generated_sources = _dedupe_generated_source_dicts(_generated_sources_from_refs(refs))
    rowset_sources: list[dict[str, str]] = []
    expanded_expression = expression
    budget = ExpansionBudget()
    for ref in refs:
        if not _is_internal_scope_id(ref.scope):
            continue
        passthrough_fact = _star_passthrough_output_fact(result, ref.scope, ref.column, output_lookup)
        if not passthrough_fact:
            continue
        physical_fields.extend(
            dict(item)
            for item in passthrough_fact.get("physical_source_fields") or []
            if isinstance(item, dict)
        )
        generated_sources = _dedupe_generated_source_dicts(
            [
                *generated_sources,
                *[
                    dict(item)
                    for item in passthrough_fact.get("generated_sources") or []
                    if isinstance(item, dict)
                ],
            ]
        )
        rowset_sources = _dedupe_rowset_source_dicts(
            [
                *rowset_sources,
                *[
                    dict(item)
                    for item in passthrough_fact.get("rowset_sources") or []
                    if isinstance(item, dict)
                ],
            ]
        )
        replacement = str(passthrough_fact.get("expanded_expression") or "")
        if replacement:
            expanded_expression = budget.substitute(
                expanded_expression, replacement,
                lambda expr, repl: _replace_unqualified_ref_with_expression(expr, ref.column, repl),
                ref=ref.column, scope_id=ref.scope, field=ref.column,
            )
    if not physical_fields and not generated_sources and not rowset_sources:
        return None
    if len(refs) == 1 and _is_internal_scope_id(refs[0].scope):
        upstream = output_lookup.get((refs[0].scope, refs[0].column))
        upstream_expanded_expression = ""
        if upstream is not None:
            upstream_resolution = upstream.expression_resolution or {}
            upstream_expanded_expression = str(
                upstream_resolution.get("expanded_expression")
                or upstream.expanded_expression
                or ""
            )
        if upstream_expanded_expression:
            # Replace a complete ``alias.struct_col.leaf`` path before the generic
            # ``alias.struct_col`` substitution. Doing it in the opposite order leaves
            # ``(<whole struct expression>).leaf`` and loses member-level lineage.
            expanded_expression = budget.substitute(
                expanded_expression, upstream_expanded_expression,
                lambda expr, repl: _replace_struct_field_access_from_upstream(
                    expr, refs[0].column, repl
                ),
                ref=refs[0].column, scope_id=refs[0].scope, field=refs[0].column,
            )
            for qualifier, field in _qualified_field_refs(expression):
                if field == refs[0].column:
                    expanded_expression = budget.substitute(
                        expanded_expression, upstream_expanded_expression,
                        lambda expr, repl: _replace_qualified_ref_with_expression(
                            expr, qualifier, field, repl
                        ),
                        ref=f"{qualifier}.{field}", scope_id=refs[0].scope, field=field,
                    )
    # Narrowing candidates to the ones that literally appear in the text is only sound while
    # the text is COMPLETE. Once the budget declines a substitution, an upstream field's name
    # is no longer in the string even though the field is still a real source — filtering then
    # would turn a size limit into lost lineage (PERF-001).
    physical_fields = (
        _ordered_physical_fields_in_expression(expanded_expression, physical_fields)
        if budget.stop_reason
        else (
            _physical_fields_referenced_in_expression(expanded_expression, physical_fields)
            or _ordered_physical_fields_in_expression(expanded_expression, physical_fields)
        )
    )
    return {
        "expanded_expression": expanded_expression,
        **({"expansion_status": budget.status,
            "expansion_stop_reason": budget.stop_reason,
            "unexpanded_refs": budget.skipped_refs} if budget.stop_reason else {}),
        "expression_resolution": {
            "status": "resolved",
            "resolution_type": "expression_sources_from_source_refs",
            "physical_source_fields": physical_fields,
            "generated_sources": generated_sources,
            **({"rowset_sources": rowset_sources} if rowset_sources else {}),
            "source_kind": _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources),
            "missing_reasons": [],
        },
    }

def _replace_struct_field_access_from_upstream(
    expression: str,
    struct_output_field: str,
    upstream_expanded_expression: str,
) -> str:
    if not expression or not struct_output_field or not upstream_expanded_expression:
        return expression

    def replacement(match: re.Match[str]) -> str:
        leaf_field = match.group("leaf")
        leaf_expression = _struct_leaf_expression(upstream_expanded_expression, leaf_field)
        if not leaf_expression:
            return match.group(0)
        aggregate_member = _aggregate_struct_member_expression(
            upstream_expanded_expression,
            leaf_field,
        )
        return aggregate_member or leaf_expression

    quoted_pattern = _cached_pattern(
        rf"`(?P<qualifier>[^`]+)`\.`{re.escape(struct_output_field)}`\.`(?P<leaf>[^`]+)`"
    )
    expression = quoted_pattern.sub(replacement, expression)
    bare_pattern = _cached_pattern(
        rf"(?<![.`\w])(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\."
        rf"{re.escape(struct_output_field)}\.(?P<leaf>[A-Za-z_][A-Za-z0-9_]*)(?![`.\w])"
    )
    return bare_pattern.sub(replacement, expression)


def _aggregate_struct_member_expression(
    upstream_expanded_expression: str,
    leaf_field: str,
) -> str | None:
    """Keep row-selection semantics when projecting a member from an aggregate STRUCT."""
    try:
        parsed = sqlglot.parse_one(
            upstream_expanded_expression,
            dialect=DIALECT,
            error_level=ErrorLevel.RAISE,
        )
    except Exception:
        return None
    while isinstance(parsed, exp.Paren):
        parsed = parsed.this
    if not isinstance(parsed, (exp.Max, exp.Min)):
        return None
    struct_expression = parsed.this
    is_struct = isinstance(struct_expression, exp.Struct)
    is_named_struct = (
        isinstance(struct_expression, exp.Anonymous)
        and struct_expression.name.lower() == "named_struct"
    )
    if not is_struct and not is_named_struct:
        return None
    member_expression = exp.Dot(
        this=exp.Paren(this=parsed.copy()),
        expression=exp.Identifier(this=leaf_field, quoted=True),
    )
    rendered = member_expression.sql(dialect=DIALECT)
    try:
        sqlglot.parse_one(
            rendered,
            dialect=DIALECT,
            error_level=ErrorLevel.RAISE,
        )
    except Exception:
        return None
    return rendered


def _struct_leaf_expression(upstream_expanded_expression: str, leaf_field: str) -> str | None:
    leaf_pattern = re.compile(
        rf"(?P<source>`[^`]+`\.`[^`]+`)\s+AS\s+`?{re.escape(leaf_field)}`?(?=[,)])",
        re.IGNORECASE,
    )
    match = leaf_pattern.search(upstream_expanded_expression)
    if match:
        return match.group("source")
    named_struct_pattern = re.compile(
        rf"['\"]{re.escape(leaf_field)}['\"]\s*,\s*"
        rf"(?P<source>`[^`]+`\.`[^`]+`)(?=\s*[,)]|\s*$)",
        re.IGNORECASE,
    )
    match = named_struct_pattern.search(upstream_expanded_expression)
    if match:
        return match.group("source")
    return None

def _physical_fields_referenced_in_expression(
    expression: str,
    candidate_fields: list[dict[str, str]],
) -> list[dict[str, str]]:
    candidate_keys = {
        (str(item.get("table") or ""), str(item.get("field") or ""))
        for item in candidate_fields
        if item.get("table") and item.get("field")
    }
    if not candidate_keys:
        return []
    selected: list[dict[str, str]] = []
    selected_keys: set[tuple[str, str]] = set()
    for table, field in _qualified_field_refs(expression):
        key = (table, field)
        if key not in candidate_keys or key in selected_keys:
            continue
        selected_keys.add(key)
        selected.append({"table": table, "field": field})
    return selected

def _ordered_physical_fields_in_expression(
    expression: str,
    candidate_fields: list[dict[str, str]],
) -> list[dict[str, str]]:
    deduped_candidates = _dedupe_physical_field_dicts(candidate_fields)
    if len(expression or "") > 200_000 or len(deduped_candidates) > 1_000:
        return deduped_candidates
    candidate_keys = {
        (str(item.get("table")), str(item.get("field")))
        for item in deduped_candidates
        if item.get("table") and item.get("field")
    }
    ordered: list[dict[str, str]] = []
    ordered_keys: set[tuple[str, str]] = set()
    for table, field in _qualified_field_refs(expression):
        key = (table, field)
        if key not in candidate_keys or key in ordered_keys:
            continue
        ordered_keys.add(key)
        ordered.append({"table": table, "field": field})
    for item in deduped_candidates:
        normalized = {"table": str(item.get("table")), "field": str(item.get("field"))}
        key = (normalized["table"], normalized["field"])
        if normalized["table"] and normalized["field"] and key not in ordered_keys:
            ordered_keys.add(key)
            ordered.append(normalized)
    return ordered

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

def _dedupe_physical_field_dicts(fields: list[dict[str, str]]) -> list[dict[str, str]]:
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















def _find_alias_in_parent(sg_scope: Scope) -> str | None:
    """Find the alias this scope uses in its parent scope's sources."""
    if sg_scope.is_udtf and isinstance(sg_scope.expression, exp.Lateral):
        alias = sg_scope.expression.args.get("alias")
        if alias is not None and alias.this is not None:
            return alias.this.name if hasattr(alias.this, "name") else str(alias.this)
    if sg_scope.parent is None:
        return None
    for name, src in sg_scope.parent.sources.items():
        if src is sg_scope:
            return name
    return None


def _constant_sources(expression: str | None) -> list[SourceRef]:
    """Represent a literal as a traceable leaf instead of an empty lineage edge."""
    literal = expression if expression else "<constant>"
    return [SourceRef(scope=CONSTANT_SCOPE_ID, column=literal)]

def _system_sources(expression: str | None) -> list[SourceRef]:
    """Represent runtime/system expressions as traceable non-table leaves."""
    label = expression if expression else "<system>"
    return [SourceRef(scope=SYSTEM_SCOPE_ID, column=label)]

def _selected_sources(sg_scope: Scope) -> dict:
    """Return only sources that participate in the current SELECT FROM/JOIN list."""
    try:
        selected = sg_scope.selected_sources
    except OptimizeError:
        return _selected_sources_from_ast(sg_scope)
    if not selected and sg_scope.sources:
        reconstructed = _selected_sources_from_ast(sg_scope)
        return reconstructed or sg_scope.sources
    return {alias: source for alias, (_node, source) in selected.items()}


def _selected_sources_from_ast(sg_scope: Scope) -> dict:
    """Rebuild selected inputs when sqlglot rejects a repeated source alias.

    ``Scope.sources`` also contains visible CTEs that are not selected by this query. Returning
    it after ``selected_sources`` raises would make those unrelated CTEs candidates for every
    unqualified column.
    """
    expression = sg_scope.expression
    if not isinstance(expression, exp.Select):
        return {}

    items: list[tuple[str, Scope | exp.Table]] = []
    from_ = expression.args.get("from_")
    if from_ is not None:
        item = _source_item_from_ast_node(getattr(from_, "this", None), sg_scope)
        if item:
            items.append(item)
    for join in expression.args.get("joins") or []:
        item = _source_item_from_ast_node(join.this, sg_scope)
        if item:
            items.append(item)
    for udtf_scope in getattr(sg_scope, "udtf_scopes", []) or []:
        alias = _find_alias_in_parent(udtf_scope) or "udtf"
        items.append((alias, udtf_scope))

    selected: dict[str, Scope | exp.Table] = {}
    for alias, source in items:
        key = alias
        suffix = 2
        while key in selected:
            key = f"{alias}#{suffix}"
            suffix += 1
        selected[key] = source
    return selected


def _source_free_leaf_sources(inner: exp.Expression, expression: str) -> list[SourceRef]:
    if _contains_runtime_function(inner):
        return _system_sources(expression)
    return _constant_sources(expression)

def _contains_runtime_function(node: exp.Expression) -> bool:
    runtime_names = {
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "CURRENT_TIME",
        "NOW",
        "RAND",
        "RANDOM",
        "UUID",
        "UNIX_TIMESTAMP",
    }
    for expr in node.walk():
        if isinstance(expr, (exp.CurrentDate, exp.CurrentTimestamp, exp.Rand)):
            return True
        if isinstance(expr, exp.Anonymous):
            name = expr.name.upper() if hasattr(expr, "name") else ""
            if name in runtime_names:
                return True
    sql = node.sql(dialect=DIALECT).upper()
    return any(f"{name}(" in sql or name in {"CURRENT_DATE", "CURRENT_TIMESTAMP"} and name in sql for name in runtime_names)

def render_sql_or_none(tree: exp.Expression) -> str | None:
    """Print a parsed tree back to SQL, or give up without taking the caller down.

    Generation is not total. A statement whose identifier collides with a tokenizer keyword
    parses into a node the Spark generator cannot render -- `CAST(out AS DOUBLE)` yields a Cast
    whose `to` is None and `cast_sql` dereferences it -- and the AttributeError escaped the
    public API entirely (REGEN-001). One statement that cannot be *printed* must not cost a
    batch its other results, which is the whole reason broken statements are kept.

    The lineage is built from the AST, never from this string, so failing here costs a
    convenience field and nothing else. Returns None so the caller decides what to record.
    """
    try:
        return tree.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001 - any generator failure, not a known subset
        return None


def _inside_nested_set_op(root: exp.Expression, node: exp.Expression) -> bool:
    """Return True when ``node`` sits inside a nested SELECT or set-op branch of ``root``.

    Deliberately distinct from expression_refs._inside_nested_subquery: this one treats a
    UNION between node and root as nesting (resolvers must not attribute a set-op branch's
    columns to the outer expression) but does NOT stop at a bare exp.Subquery wrapper, and
    it accepts any node -- the resolvers also probe subquery nodes, not just columns.
    """
    if node is root:
        return False
    parent = node.parent
    while parent is not None and parent is not root:
        if isinstance(parent, (exp.Select, exp.Union)):
            return True
        parent = parent.parent
    return False

def _source_item_from_ast_node(
    node: exp.Expression | None,
    sg_scope: Scope,
) -> tuple[str, Scope | exp.Table] | None:
    if node is None:
        return None
    alias = node.alias if isinstance(node, (exp.Table, exp.Subquery)) else None
    source: Scope | exp.Table | None = None
    if isinstance(node, exp.Table):
        # A table reference may actually name a CTE; resolve that through the
        # scope source map by table name. Physical tables can be used directly.
        named_source = sg_scope.sources.get(node.name)
        if isinstance(named_source, Scope):
            source = named_source
        else:
            source = node
        alias = alias or node.name
    elif isinstance(node, exp.Subquery):
        if alias:
            # Preserve AST identity before consulting the alias dictionary. sqlglot stores
            # sources by alias, so a repeated alias keeps only the last subquery and would
            # otherwise make every duplicate appear to be that same scope.
            for scope_list_name in ("derived_table_scopes", "subquery_scopes"):
                for sub_scope in getattr(sg_scope, scope_list_name, []) or []:
                    if sub_scope.expression is node.this:
                        source = sub_scope
                        break
                if source is not None:
                    break
            if source is None:
                mapped = sg_scope.sources.get(alias)
                if isinstance(mapped, Scope):
                    source = mapped
    if alias and source is not None:
        return alias, source
    return None

def _source_ref_for_source(
    alias: str,
    source: Scope | exp.Table,
    col_name: str,
    result: ScopeLineageResult,
) -> SourceRef:
    if isinstance(source, Scope):
        upstream_id = _source_scope_id(alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)
        return SourceRef(scope="UNKNOWN", column=col_name)
    return SourceRef(scope=_qualified_table(source), column=col_name)

def _source_scope_id(alias: str, source: Scope, result: ScopeLineageResult) -> str | None:
    """Return a stable result scope id for a sqlglot Scope source."""
    upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
    if upstream_id in result.scopes:
        return upstream_id
    for candidate in (f"cte:{alias}", f"subq:{alias}", f"union:{alias}"):
        if candidate in result.scopes:
            return candidate
    return upstream_id

def _classify_extended(node: exp.Expression) -> str:
    """Classify expression type. Extends parser._classify with UNION and EXPAND_ALL."""
    if isinstance(node, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Window):
        return "WINDOW"
    if isinstance(node, exp.AggFunc):
        return "AGGREGATE"
    if isinstance(node, (exp.Case, exp.If)):
        return "CONDITIONAL"
    if isinstance(node, exp.Subquery):
        return "EXPRESSION"  # LITERAL_SUBQUERY mapped to EXPRESSION per design decision
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return "CONSTANT"
    if isinstance(node, exp.Column):
        return "DIRECT"
    # Check for Anonymous UDAFs
    if isinstance(node, exp.Anonymous):
        func_name = node.name.upper() if hasattr(node, "name") else ""
        if func_name in _KNOWN_UDAFS:
            return "AGGREGATE"
    return "EXPRESSION"


_REGEX_COLUMN_METACHARACTERS = set(".*+?[]()|^$\\")

_POSSESSIVE_QUANTIFIER = re.compile(r"\(((?:[^()\\]|\\.)*)\)([?*+])\+")

def _compiled_column_pattern(pattern: str):
    """Compile a Spark column pattern the same way on every supported Python.

    Spark's exclusion idiom uses a possessive quantifier — ``(dt)?+.+`` reads as "every
    column except dt", because ``(dt)?+`` consumes ``dt`` without giving it back. Python
    only accepts that syntax from 3.11, and this project supports 3.9, so it is rewritten
    to the lookahead-and-backreference form that behaves identically everywhere. Letting
    the compile simply fail on older interpreters would make the same SQL produce different
    lineage depending on the Python running it.
    """
    for candidate in (pattern, _POSSESSIVE_QUANTIFIER.sub(r"(?=((?:\1)\2))\\1", pattern)):
        try:
            return re.compile(candidate)
        except re.error:
            continue
    return None


def _pivot_of_source_node(node) -> object | None:
    """Return the PIVOT attached to a FROM/JOIN item, if it has one."""
    pivots = getattr(node, "args", {}).get("pivots") or []
    return pivots[0] if pivots else None


def _pivot_output_names(pivot) -> list[str] | None:
    """Column names a PIVOT produces, or None when the IN list is not a literal list.

    The IN list is the column set: ``FOR k IN ('A', 'B')`` produces columns A and B. A
    subquery or ANY in that position leaves the set unknowable, and the caller must report
    a gap rather than bind to a name it guessed (PIVOT-001).
    """
    from sqlglot import exp

    fields = getattr(pivot, "args", {}).get("fields") or []
    names: list[str] = []
    for field in fields:
        if not isinstance(field, exp.In):
            return None
        for item in field.expressions:
            if isinstance(item, exp.Alias):
                names.append(item.alias)
                continue
            if isinstance(item, exp.Literal):
                names.append(str(item.this))
                continue
            return None
    return names or None
