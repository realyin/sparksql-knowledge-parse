"""Column Expression Resolution — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the ColumnExpressionResolutionEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import re
import sqlglot
from sqlglot import exp
from .scope_types import (
    CONSTANT_SCOPE_ID,
    ScopeData,
    ScopeColumn,
    SYSTEM_SCOPE_ID,
)
# Leaf helpers come from `_shared`, never from the orchestrator: importing it back
# formed a cycle that only worked because Python hands out a partially-initialised
# module, making import order load-bearing (ARCH-001).
from ._constants import DIALECT, PARSE_OPTS
from .expression_refs import _cached_pattern, _qualified_field_refs
from .sequences import _unique_ordered
from .source_refs import _generated_sources_from_refs, _physical_source_fields_from_refs, _qualified_physical_field_sql, _source_kind_for_resolution


def _expression_resolution_for_scope_column(scope_data: ScopeData, column: ScopeColumn) -> dict[str, object]:
    expression = column.expression
    if _is_row_count_aggregate(column):
        return {'expanded_expression': expression, 'expression_resolution': {'status': 'resolved', 'resolution_type': 'row_count_aggregate', 'physical_source_fields': [], 'generated_sources': [], 'source_kind': 'rowset', 'missing_reasons': []}}
    if _is_rowset_window_function(column):
        return {'expanded_expression': expression, 'expression_resolution': {'status': 'resolved', 'resolution_type': 'rowset_window_function', 'physical_source_fields': [], 'generated_sources': [], 'source_kind': 'rowset', 'missing_reasons': []}}
    physical_fields = _physical_source_fields_from_refs(column.sources)
    generated_sources = _generated_sources_from_refs(column.sources, column.transform)
    if not generated_sources:
        generated_sources = _generated_sources_from_column_expression(column)
    missing_reasons: list[str] = []
    unresolved_qualifiers: list[str] = []
    expanded_expression = expression
    resolution_type = 'raw_expression'
    qualified_refs = _qualified_field_refs(expression or '')
    if qualified_refs:
        resolution_type = 'qualified_source_projection' if column.transform == 'DIRECT' and len(qualified_refs) == 1 else 'qualified_expression'
        physical_field_keys = {(item.get('table'), item.get('field')) for item in physical_fields}
        for (qualifier, field) in qualified_refs:
            physical_table = _physical_source_for_qualifier(scope_data, qualifier)
            if not physical_table:
                physical_table = _physical_source_for_unbound_qualifier(physical_fields, field)
            if not physical_table:
                if qualifier not in unresolved_qualifiers:
                    unresolved_qualifiers.append(qualifier)
                continue
            if (physical_table, field) not in physical_field_keys:
                if not _qualifier_is_direct_physical_source(scope_data, qualifier, physical_table):
                    continue
                physical_field_keys.add((physical_table, field))
                physical_fields.append({'table': physical_table, 'field': field})
            expanded_expression = _replace_qualified_field_ref(expanded_expression or '', qualifier, field, physical_table)
    elif column.transform != 'DIRECT' and _expression_uses_only_traceable_unqualified_sources(expression, physical_fields, generated_sources):
        resolution_type = 'unqualified_expression_from_sources'
    if generated_sources and _has_any_column_refs(expression) is False:
        physical_fields = [field for field in physical_fields if str(field.get('field') or '') != '*']
        expanded_expression = expression
    elif len(physical_fields) == 1 and resolution_type == 'raw_expression':
        resolution_type = 'single_source_projection' if column.transform == 'DIRECT' else 'single_source_expression'
        expanded_expression = _qualified_physical_field_sql(physical_fields[0]['table'], physical_fields[0]['field'])
    if generated_sources and (not physical_fields) and (not unresolved_qualifiers):
        resolution_type = 'generated_expression'
    if not physical_fields and (not generated_sources):
        missing_reasons.append('no_physical_source_fields')
    for qualifier in unresolved_qualifiers:
        missing_reasons.append(f'alias_not_bound_to_input_source:{qualifier}')
    status = 'resolved'
    if missing_reasons and (physical_fields or generated_sources):
        status = 'partially_resolved'
    elif missing_reasons:
        status = 'unresolved'
    return {'expanded_expression': expanded_expression, 'expression_resolution': {'status': status, 'resolution_type': resolution_type, 'physical_source_fields': physical_fields, 'generated_sources': generated_sources, 'source_kind': _source_kind_for_resolution(physical_fields, generated_sources), 'missing_reasons': missing_reasons, **({'unresolved_qualifiers': unresolved_qualifiers} if unresolved_qualifiers else {})}}


def _has_unqualified_column_refs(expression: str | None) -> bool:
    if not expression:
        return False
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except sqlglot.errors.SqlglotError:
        return False
    return any((isinstance(column, exp.Column) and (not column.table) for column in parsed.find_all(exp.Column)))


def _has_any_column_refs(expression: str | None) -> bool | None:
    if not expression:
        return False
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except sqlglot.errors.SqlglotError:
        return None
    return any((isinstance(column, exp.Column) for column in parsed.find_all(exp.Column)))


def _generated_sources_from_column_expression(column: ScopeColumn) -> list[dict[str, str]]:
    expression = (column.expression or '').strip()
    if not expression:
        return []
    has_column_refs = _has_any_column_refs(expression)
    if has_column_refs is not False:
        return []
    source_type = SYSTEM_SCOPE_ID if _expression_is_system_generated(expression) else CONSTANT_SCOPE_ID
    return [{'source_type': source_type, 'value': expression, 'transform': column.transform or source_type}]


def _expression_is_system_generated(expression: str) -> bool:
    return bool(re.search('(?i)\\b(rand|random|uuid|current_date|current_timestamp|current_user|now|unix_timestamp)\\s*\\(', expression))


def _expression_uses_only_traceable_unqualified_sources(expression: str | None, physical_fields: list[dict[str, str]], generated_sources: list[dict[str, str]]) -> bool:
    if not _has_unqualified_column_refs(expression):
        return False
    return bool(physical_fields or generated_sources)


def _is_row_count_aggregate(column: ScopeColumn) -> bool:
    if column.transform != 'AGGREGATE':
        return False
    expression = (column.expression or '').strip()
    return bool(re.fullmatch('(?i)COUNT\\s*\\(\\s*(1|\\*)\\s*\\)', expression))


def _is_rowset_window_function(column: ScopeColumn) -> bool:
    if column.transform not in {'WINDOW', 'EXPRESSION'}:
        return False
    expression = re.sub('\\s+', ' ', (column.expression or '').strip())
    rowset_only_window_patterns = ['COUNT\\s*\\(\\s*(1|\\*)\\s*\\)', 'ROW_NUMBER\\s*\\(\\s*\\)', 'RANK\\s*\\(\\s*\\)', 'DENSE_RANK\\s*\\(\\s*\\)', 'PERCENT_RANK\\s*\\(\\s*\\)', 'CUME_DIST\\s*\\(\\s*\\)', 'NTILE\\s*\\(\\s*\\d+\\s*\\)']
    return any((re.fullmatch(f'(?i){pattern}\\s+OVER\\s*\\(.*\\)', expression) for pattern in rowset_only_window_patterns))


def _physical_source_for_unbound_qualifier(physical_fields: list[dict[str, str]], field: str) -> str | None:
    matches = [str(item.get('table')) for item in physical_fields if item.get('table') and item.get('field') == field]
    unique_matches = _unique_ordered(matches)
    return unique_matches[0] if len(unique_matches) == 1 else None


def _physical_source_for_qualifier(scope_data: ScopeData, qualifier: str) -> str | None:
    for binding in scope_data.alias_source_bindings:
        if binding.get('alias') == qualifier:
            physical = binding.get('physical_source_id')
            if physical:
                return str(physical)
            physical_ids = binding.get('physical_source_ids') or []
            if len(physical_ids) == 1:
                return str(physical_ids[0])
    candidates: list[str] = []
    for ref in scope_data.input_source_refs:
        physical = ref.get('physical_source_id') or ref.get('source_id')
        if not physical:
            continue
        physical = str(physical)
        aliases = {str(ref.get('alias') or ''), str(ref.get('source_id') or ''), physical, physical.split('.')[-1]}
        if qualifier in aliases and physical not in candidates:
            candidates.append(physical)
    for edge in scope_data.input_edges:
        if edge.source_type != 'physical_table' or not edge.source_id:
            continue
        physical = str(edge.source_id)
        aliases = {str(edge.alias or ''), physical, physical.split('.')[-1]}
        if qualifier in aliases and physical not in candidates:
            candidates.append(physical)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _qualifier_is_direct_physical_source(scope_data: ScopeData, qualifier: str, physical_table: str) -> bool:
    for binding in scope_data.alias_source_bindings:
        if binding.get('alias') != qualifier or binding.get('source_type') != 'physical_table':
            continue
        if binding.get('physical_source_id') == physical_table or binding.get('source_id') == physical_table:
            return True
    for ref in scope_data.input_source_refs:
        if ref.get('source_type') != 'physical_table':
            continue
        aliases = {str(ref.get('alias') or ''), str(ref.get('source_id') or ''), str(ref.get('physical_source_id') or ''), str(ref.get('source_id') or '').split('.')[-1], str(ref.get('physical_source_id') or '').split('.')[-1]}
        if qualifier in aliases and (ref.get('physical_source_id') == physical_table or ref.get('source_id') == physical_table):
            return True
    for edge in scope_data.input_edges:
        if edge.source_type != 'physical_table' or not edge.source_id:
            continue
        aliases = {str(edge.alias or ''), str(edge.source_id), str(edge.source_id).split('.')[-1]}
        if qualifier in aliases and edge.source_id == physical_table:
            return True
    return False


def _replace_qualified_field_ref(expression: str, qualifier: str, field: str, physical_table: str) -> str:
    qualified = _qualified_physical_field_sql(physical_table, field)
    expression = expression.replace(f'`{qualifier}`.`{field}`', qualified)
    expression = _cached_pattern(f'(?<![.`\\w]){re.escape(qualifier)}\\.{re.escape(field)}(?![`.\\w])').sub(lambda _match: qualified, expression)
    return expression
