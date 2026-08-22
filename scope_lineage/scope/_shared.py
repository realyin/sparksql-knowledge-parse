"""Shared leaf helpers for the scope domain.

Single home for helpers used by both the orchestrators and the implementation
modules - keeps the domain dependency graph one-directional (no import cycles)."""
from __future__ import annotations
import re
import sqlglot
from sqlglot import ErrorLevel
from sqlglot import exp
from .scope_types import (
    CONSTANT_SCOPE_ID,
    NON_PHYSICAL_SOURCE_SCOPES,
    ScopeData,
    ScopeLineageResult,
    ScopeOutputField,
    SYSTEM_SCOPE_ID,
    SourceRef,
)


from ._constants import (  # noqa: F401 -- transitional re-export until WI-06 repoints importers
    DIALECT,
    PARSE_OPTS,
    _ORIGINALLY_UNQUALIFIED_META,
    _SCOPE_ID_ATTR,
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

from .sqlglot_walk import (  # noqa: F401 -- transitional re-export (WI-06)
    _POSSESSIVE_QUANTIFIER,
    _REGEX_COLUMN_METACHARACTERS,
    _classify_extended,
    _compiled_column_pattern,
    _contains_runtime_function,
    _find_alias_in_parent,
    _inside_nested_set_op,
    _pivot_of_source_node,
    _pivot_output_names,
    _selected_sources,
    _selected_sources_from_ast,
    _source_free_leaf_sources,
    _source_item_from_ast_node,
    _source_ref_for_source,
    _source_scope_id,
    render_sql_or_none,
)

from .source_refs import (  # noqa: F401 -- transitional re-export (WI-06)
    _constant_sources,
    _dedupe_generated_source_dicts,
    _dedupe_physical_field_dicts,
    _dedupe_rowset_source_dicts,
    _generated_sources_from_refs,
    _is_cross_join_type,
    _is_internal_scope_id,
    _normalize_expression_resolution,
    _physical_source_fields_for_ref,
    _physical_source_fields_for_refs,
    _physical_source_fields_from_refs,
    _physical_source_ids_for_input,
    _qualified_physical_field_sql,
    _rowset_sources_from_upstream_output,
    _source_kind_for_resolution,
    _source_ref_binding_key,
    _source_ref_to_dict,
    _source_refs_from_detail_fields,
    _source_type_from_id,
    _system_sources,
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








































