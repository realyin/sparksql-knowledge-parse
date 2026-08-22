"""Conservative upstream column metadata selection for lineage outputs."""

from __future__ import annotations

from typing import Iterable, Mapping

from ..metadata.schema_metadata import column_details_for_table, normalize_table_name, table_details_for_table
from .scope_types import (
    CONSTANT_SCOPE_ID,
    SYSTEM_SCOPE_ID,
    ScopeLineageResult,
    SourceRef,
)


def build_related_metadata(
    result: ScopeLineageResult,
    schema: Mapping[str, Iterable[str]] | None,
) -> dict:
    """Return input/output table metadata useful for LLM task profiling.

    Input table metadata is conservative: if a table has a wildcard,
    unresolved, or otherwise uncertain reference, all known metadata is kept.
    Tables missing from schema metadata are still represented with columns
    inferred from scope references and ``metadata_complete=false``.
    """
    usage = _collect_usage(result)
    if usage.keep_all_source_tables:
        usage.keep_all_tables.update(normalize_table_name(t) for t in result.source_tables)

    return {
        "input_tables": _input_table_metadata(result, schema, usage),
        "output_tables": _output_table_metadata(result, schema),
    }


def _input_table_metadata(
    result: ScopeLineageResult,
    schema: Mapping[str, Iterable[str]] | None,
    usage: "_Usage",
) -> dict:
    tables = {}
    for table in result.source_tables:
        table_key = normalize_table_name(table)
        details = column_details_for_table(schema, table) if schema else []
        used_columns = usage.used_columns.get(table_key, [])
        if table_key in usage.keep_all_tables:
            selected = details or [_unknown_column_detail("*")]
            complete = bool(details)
        else:
            selected = [item for item in details if item["name"] in used_columns]
            if not selected and used_columns:
                selected = [_unknown_column_detail(name) for name in used_columns]
            complete = bool(details)

        if selected:
            item = {
                "column_details": selected,
                "metadata_complete": complete,
            }
            table_detail = table_details_for_table(schema, table) if schema else {}
            if table_detail:
                item["table_metadata"] = table_detail
            tables[table] = item
    return tables


def _output_table_metadata(
    result: ScopeLineageResult,
    schema: Mapping[str, Iterable[str]] | None,
) -> dict:
    root = result.scopes.get("ROOT")
    if root is None or not result.target_table:
        return {}
    output_names = [column.name for column in root.columns if column.name]
    schema_details = column_details_for_table(schema, result.target_table) if schema else []
    if schema_details:
        details = [item for item in schema_details if item["name"] in output_names]
        complete = True
    else:
        details = [_unknown_column_detail(name) for name in output_names]
        complete = False
    item = {
        "column_details": details,
        "metadata_complete": complete,
    }
    table_detail = table_details_for_table(schema, result.target_table) if schema else {}
    if table_detail:
        item["table_metadata"] = table_detail
    return {result.target_table: item}


def _unknown_column_detail(name: str) -> dict:
    return {"name": name, "type": None, "comment": None}


class _Usage:
    def __init__(self) -> None:
        self.used_columns: dict[str, list[str]] = {}
        self._seen_columns: set[tuple[str, str]] = set()
        self.keep_all_tables: set[str] = set()
        self.keep_all_source_tables = False


def _collect_usage(result: ScopeLineageResult) -> _Usage:
    usage = _Usage()
    for scope_data in result.scopes.values():
        for column in scope_data.columns:
            _add_sources(column.sources, usage)
        for join in scope_data.joins:
            _add_sources(join.condition_columns, usage)
        for scope_filter in scope_data.filters:
            _add_sources(scope_filter.columns, usage)
        for scope_filter in scope_data.having:
            _add_sources(scope_filter.columns, usage)
        _add_sources(scope_data.group_by, usage)
        for item in scope_data.order_by:
            _add_source_ref(SourceRef(item.get("scope", ""), item.get("column", "")), usage)
    return usage


def _add_sources(sources: Iterable[SourceRef], usage: _Usage) -> None:
    for source in sources:
        _add_source_ref(source, usage)


def _add_source_ref(source: SourceRef, usage: _Usage) -> None:
    if source.scope in {"", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}:
        return
    if source.scope == "UNKNOWN":
        usage.keep_all_source_tables = True
        return
    _add_scope_if_physical(source.scope, usage, source.column)


def _add_scope_if_physical(scope_id: str, usage: _Usage, column: str = "*") -> None:
    if not scope_id or ":" in scope_id or scope_id in {"ROOT", "UNKNOWN"}:
        if scope_id == "UNKNOWN":
            usage.keep_all_source_tables = True
        return

    table_key = normalize_table_name(scope_id)
    if not table_key:
        return
    if not column or "*" in column:
        usage.keep_all_tables.add(table_key)
        return
    key = (table_key, column)
    if key in usage._seen_columns:
        return
    usage._seen_columns.add(key)
    usage.used_columns.setdefault(table_key, []).append(column)
