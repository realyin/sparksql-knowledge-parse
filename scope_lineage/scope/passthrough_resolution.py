"""Passthrough Resolution — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the PassthroughResolutionEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import re
from .scope_types import (
    AMBIGUOUS_SCOPE_ID,
    CONSTANT_SCOPE_ID,
    ScopeLineageResult,
    ScopeOutputField,
    SYSTEM_SCOPE_ID,
    SourceRef,
)
# Leaf helpers come from `_shared`, never from the orchestrator: importing it back
# formed a cycle that only worked because Python hands out a partially-initialised
# module, making import order load-bearing (ARCH-001).
from .expression_expansion import _physical_fields_referenced_in_expression, _replace_struct_field_access_from_upstream
from .expression_refs import _cached_pattern
from .expression_text import _qualifier_present, _replace_qualified_ref_with_expression, _replace_unqualified_ref_with_expression, _unexpanded_bound_aliases_in_expression
from .source_refs import _dedupe_generated_source_dicts, _generated_sources_from_refs, _is_internal_scope_id, _qualified_physical_field_sql, _source_kind_for_resolution
from .expansion_budget import ExpansionBudget


def _propagate_passthrough_expression_resolution(result: ScopeLineageResult) -> None:  # noqa: C901 - legacy exemption (WI-11): complexity 33, the pipeline convergence pass
    output_lookup = {(scope_id, output.name): output for (scope_id, scope_data) in result.scopes.items() for output in scope_data.outputs}
    changed = True
    while changed:
        changed = False
        for (scope_id, scope_data) in result.scopes.items():
            for output in scope_data.outputs:
                if not output.sources:
                    continue
                allow_expression_source_propagation = scope_id.startswith('udtf:') and output.transform in {'EXPRESSION', 'CONDITIONAL', 'AGGREGATE', 'WINDOW'}
                allow_bare_identifier_source_propagation = output.transform not in {'DIRECT', 'UNION'} and _is_bare_identifier_expression(output.expression)
                if output.transform not in {'DIRECT', 'UNION'} and (not allow_expression_source_propagation) and (not allow_bare_identifier_source_propagation):
                    continue
                current_resolution = output.expression_resolution or {}
                previous_expanded_expression = output.expanded_expression
                previous_expression_resolution = dict(current_resolution)
                current_expanded_expression = str(current_resolution.get('expanded_expression') or output.expanded_expression or output.expression or '')
                has_unexpanded_bound_alias = bool(_unexpanded_bound_aliases_in_expression(scope_data, current_expanded_expression))
                has_internal_source = any((_is_internal_scope_id(source.scope) for source in output.sources))
                if current_resolution.get('status') == 'resolved' and (current_resolution.get('physical_source_fields') or current_resolution.get('generated_sources') or current_resolution.get('rowset_sources') or (current_resolution.get('source_kind') == 'rowset')) and (not has_unexpanded_bound_alias) and (not (has_internal_source and (output.transform in {'DIRECT', 'UNION'} or allow_bare_identifier_source_propagation))) and (not (allow_expression_source_propagation and _expression_has_internal_source_alias(current_expanded_expression, output.sources))):
                    continue
                source_facts = [_resolved_output_source_fact(output_lookup, source) for source in output.sources]
                if any((fact is None for fact in source_facts)):
                    continue
                physical_fields: list[dict[str, str]] = []
                physical_field_keys: set[tuple[str, str]] = set()
                generated_sources: list[dict[str, str]] = []
                rowset_sources: list[dict[str, str]] = []
                rowset_source_keys: set[tuple[str, str, str]] = set()
                expanded_expressions: list[str] = []
                expanded_expression_keys: set[str] = set()
                source_scope_ids: list[str] = []
                source_output_fields: list[str] = []
                source_resolution_types: list[str] = []
                for fact in source_facts:
                    if fact is None:
                        continue
                    for field in fact['physical_source_fields']:
                        key = (str(field.get('table') or ''), str(field.get('field') or ''))
                        if not key[0] or not key[1] or key in physical_field_keys:
                            continue
                        physical_field_keys.add(key)
                        physical_fields.append(field)
                    generated_sources.extend((dict(item) for item in fact.get('generated_sources') or [] if isinstance(item, dict)))
                    for item in fact.get('rowset_sources') or []:
                        if not isinstance(item, dict):
                            continue
                        rowset_key = (str(item.get('source_type') or ''), str(item.get('scope') or ''), str(item.get('field') or ''))
                        if not rowset_key[0] or rowset_key in rowset_source_keys:
                            continue
                        rowset_source_keys.add(rowset_key)
                        rowset_sources.append(dict(item))
                    expanded = fact.get('expanded_expression')
                    if expanded:
                        expanded_key = str(expanded)
                        if expanded_key not in expanded_expression_keys:
                            expanded_expression_keys.add(expanded_key)
                            expanded_expressions.append(expanded_key)
                    source_scope = fact.get('source_scope_id')
                    source_output = fact.get('source_output_field')
                    source_resolution_type = fact.get('resolution_type')
                    if source_scope and str(source_scope) not in source_scope_ids:
                        source_scope_ids.append(str(source_scope))
                    if source_output and str(source_output) not in source_output_fields:
                        source_output_fields.append(str(source_output))
                    if source_resolution_type and str(source_resolution_type) not in source_resolution_types:
                        source_resolution_types.append(str(source_resolution_type))
                generated_sources = _dedupe_generated_source_dicts(generated_sources)
                if not physical_fields and (not generated_sources) and (not rowset_sources):
                    continue
                has_struct_member_access = _expression_has_struct_member_access(
                    output.expression, output.sources
                )
                if (output.transform in {'DIRECT', 'UNION'} or allow_bare_identifier_source_propagation) and len(expanded_expressions) == 1:
                    if has_struct_member_access:
                        output.expanded_expression = (
                            _expanded_expression_from_source_facts(
                                output.expression or "", source_facts
                            )
                            or output.expression
                        )
                    else:
                        output.expanded_expression = expanded_expressions[0]
                elif output.transform == 'UNION' and len(expanded_expressions) > 1:
                    output.expanded_expression = _union_branches_expanded_expression(expanded_expressions)
                elif allow_expression_source_propagation:
                    output.expanded_expression = _expanded_expression_from_source_facts(output.expression or '', source_facts) or output.expression
                elif output.expanded_expression is None:
                    output.expanded_expression = output.expression
                if has_struct_member_access:
                    selected_fields = _physical_fields_referenced_in_expression(
                        output.expanded_expression or "", physical_fields
                    )
                    if selected_fields:
                        physical_fields = selected_fields
                source_anchor = {}
                if len(source_scope_ids) == 1:
                    source_anchor['source_scope_id'] = source_scope_ids[0]
                if len(source_output_fields) == 1:
                    source_anchor['source_output_field'] = source_output_fields[0]
                if len(source_scope_ids) > 1:
                    source_anchor['source_scope_ids'] = source_scope_ids
                if len(source_output_fields) > 1:
                    source_anchor['source_output_fields'] = source_output_fields
                new_expression_resolution = {'status': 'resolved', 'resolution_type': _passthrough_resolution_type(output, allow_bare_identifier_source_propagation, source_resolution_types), 'expanded_expression': output.expanded_expression or output.expression, 'physical_source_fields': physical_fields, 'generated_sources': generated_sources, **({'rowset_sources': rowset_sources} if rowset_sources else {}), 'source_kind': _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources, rowset_dominates_generated=True), 'missing_reasons': [], **source_anchor}
                if previous_expanded_expression == output.expanded_expression and previous_expression_resolution == new_expression_resolution:
                    continue
                output.expression_resolution = new_expression_resolution
                changed = True


def _is_bare_identifier_expression(expression: str | None) -> bool:
    return bool(re.fullmatch('`?[A-Za-z_][A-Za-z0-9_]*`?', str(expression or '').strip()))


def _union_branches_expanded_expression(expressions: list[str]) -> str:
    return 'UNION_BRANCHES(' + ' || '.join(expressions) + ')'


def _passthrough_resolution_type(output: ScopeOutputField, allow_bare_identifier_source_propagation: bool, source_resolution_types: list[str]) -> str:
    if output.transform == 'UNION' and len(output.sources) > 1:
        return 'union_branch_alignment'
    if allow_bare_identifier_source_propagation:
        return 'bare_identifier_from_unique_upstream_output'
    if output.transform == 'DIRECT':
        if _source_resolution_types_include_expression(source_resolution_types):
            return 'expanded_from_upstream_scope_expression'
        return 'expanded_from_upstream_scope'
    return 'expression_sources_from_upstream_scope'


def _source_resolution_types_include_expression(resolution_types: list[str]) -> bool:
    expression_resolution_types = {'expanded_from_upstream_scope_expression', 'expression_sources_from_upstream_scope', 'qualified_expression', 'unqualified_expression_from_sources'}
    return any((resolution_type in expression_resolution_types for resolution_type in resolution_types))


def _expression_has_internal_source_alias(expression: str | None, sources: list[SourceRef]) -> bool:
    if not expression:
        return False
    return any((_qualifier_present(expression, qualifier) for qualifier in (_internal_scope_alias(source.scope) for source in sources) if qualifier))


# The same question scope_facts asks, asked here about a scope's sources instead of its
# refs. Profiling put it at roughly a quarter of the run on a large statement — the answer depends only
# on the expression and the source columns, so it is remembered (PERF-002).
_STRUCT_ACCESS_CACHE: dict[tuple[str, tuple[str, ...]], bool] = {}


def _expression_has_struct_member_access(
    expression: str | None,
    sources: list[SourceRef],
) -> bool:
    expression = str(expression or "")
    key = (
        expression,
        tuple(
            source.column or ""
            for source in sources
            if _is_internal_scope_id(source.scope) and source.column
        ),
    )
    cached = _STRUCT_ACCESS_CACHE.get(key)
    if cached is None:
        cached = _expression_has_struct_member_access_uncached(expression, sources)
        _STRUCT_ACCESS_CACHE[key] = cached
    return cached


def _expression_has_struct_member_access_uncached(
    expression: str,
    sources: list[SourceRef],
) -> bool:
    for source in sources:
        if not _is_internal_scope_id(source.scope) or not source.column:
            continue
        field = re.escape(source.column)
        if _cached_pattern(rf"`[^`]+`\.`{field}`\.`[^`]+`").search(expression):
            return True
        if _cached_pattern(
            rf"(?<![.`\w])[A-Za-z_][A-Za-z0-9_]*\.{field}\."
            rf"[A-Za-z_][A-Za-z0-9_]*(?![`.\w])"
        ).search(expression):
            return True
    return False


def _expanded_expression_from_source_facts(
    expression: str,
    source_facts: list[dict[str, object] | None],
    budget: ExpansionBudget | None = None,
) -> str:
    """Inline each source's expanded text, within the caller's expansion budget.

    Callers that pass a budget get the growth bounded and the declined references recorded;
    the default budget still bounds the result, it just discards the record (PERF-001).
    """
    budget = budget if budget is not None else ExpansionBudget()
    expanded_expression = expression
    for fact in source_facts:
        if not fact:
            continue
        replacement = str(fact.get('expanded_expression') or '')
        field = str(fact.get('source_output_field') or '')
        source_scope = str(fact.get('source_scope_id') or '')
        if not replacement or not field:
            continue
        qualifier = _internal_scope_alias(source_scope)
        expanded_expression = budget.substitute(
            expanded_expression, replacement,
            lambda expr, repl: _replace_struct_field_access_from_upstream(
                expr, field, repl
            ),
            ref=field, scope_id=source_scope, field=field,
        )
        if qualifier:
            expanded_expression = budget.substitute(
                expanded_expression, replacement,
                lambda expr, repl: _replace_qualified_ref_with_expression(expr, qualifier, field, repl),
                ref=f"{qualifier}.{field}", scope_id=source_scope, field=field,
            )
        expanded_expression = budget.substitute(
            expanded_expression, replacement,
            lambda expr, repl: _replace_unqualified_ref_with_expression(expr, field, repl),
            ref=field, scope_id=source_scope, field=field,
        )
    return expanded_expression


def _internal_scope_alias(scope_id: str | None) -> str:
    scope_id = str(scope_id or '')
    if not _is_internal_scope_id(scope_id):
        return ''
    return scope_id.split(':', 1)[1]


def _resolved_output_source_fact(output_lookup: dict[tuple[str, str], ScopeOutputField], source: SourceRef) -> dict[str, object] | None:
    if source.scope in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}:
        return {'physical_source_fields': [], 'generated_sources': _generated_sources_from_refs([source], source.scope), 'expanded_expression': source.column, 'source_scope_id': source.scope, 'source_output_field': source.column}
    if source.scope in {'UNKNOWN', AMBIGUOUS_SCOPE_ID}:
        # AMBIGUOUS is not a table. Falling through would have produced a physical
        # source literally named "AMBIGUOUS" and marked the field resolved (LINEAGE-002).
        return None
    if not _is_internal_scope_id(source.scope):
        return {'physical_source_fields': [{'table': source.scope, 'field': source.column}], 'generated_sources': [], 'expanded_expression': _qualified_physical_field_sql(source.scope, source.column), 'source_scope_id': source.scope, 'source_output_field': source.column}
    upstream = output_lookup.get((source.scope, source.column))
    if upstream is None:
        return None
    resolution = upstream.expression_resolution or {}
    physical_fields = resolution.get('physical_source_fields') or []
    generated_sources = resolution.get('generated_sources') or []
    rowset_sources = resolution.get('rowset_sources') or []
    upstream_source_kind = str(resolution.get('source_kind') or '')
    if resolution.get('status') != 'resolved' or not (physical_fields or generated_sources or rowset_sources or (upstream_source_kind == 'rowset')):
        return None
    if upstream_source_kind == 'rowset' and (not rowset_sources):
        rowset_sources = [{'source_type': 'rowset', 'scope': source.scope, 'field': source.column, 'expression': str(upstream.expanded_expression or upstream.expression or '')}]
    return {'physical_source_fields': [dict(item) for item in physical_fields if isinstance(item, dict)], 'generated_sources': _dedupe_generated_source_dicts([dict(item) for item in generated_sources if isinstance(item, dict)]), 'rowset_sources': [dict(item) for item in rowset_sources if isinstance(item, dict)], 'resolution_type': resolution.get('resolution_type'), 'source_kind': upstream_source_kind, 'expanded_expression': resolution.get('expanded_expression') or upstream.expanded_expression or upstream.expression, 'source_scope_id': source.scope, 'source_output_field': source.column}
