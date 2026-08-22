"""Stable public API for SQL parsing and versioned Lineage Core artifacts."""

# ruff: noqa: F401 -- imports below are the intentionally declared public facade.

from .contract import (
    to_lineage_dict,
    to_lineage_json,
    to_task_lineage_dict,
    to_task_lineage_json,
    validate_cross_references,
    validate_diagnostics_document,
    validate_lineage_document,
    write_lineage,
    write_task_lineage,
)
from .contract.lineage import to_dict, to_json
from .metadata.schema_metadata import (
    DictSchemaProvider,
    MetadataFileError,
    SchemaMap,
    SchemaProvider,
    column_details_for_table,
    check_metadata_file,
    catalog_prefixes,
    load_schema,
    load_schema_sources,
    materialize_schema,
    metadata_dict_reader,
    normalize_schema_map,
    normalize_table_name,
    table_details_for_table,
)
from .metadata.target_table_metadata import (
    TargetColumnMetadata,
    TargetMetadataMap,
    TargetTableMetadata,
    load_target_table_metadata,
    lookup_target_table_metadata,
)
from .scope.expression_refs import extract_qualified_field_refs
from .scope.end_to_end import build_end_to_end_lineage
from .scope.parser import resolve_display_expression
from .scope.scope_builder import (
    NoSupportedWriteStatementError,
    parse_all_scope_lineage,
    parse_scope_lineage,
)
from .scope.scope_types import (
    CONSTANT_SCOPE_ID,
    NON_PHYSICAL_SOURCE_SCOPES,
    SYSTEM_SCOPE_ID,
    DiagnosticWarning,
    Diagnostics,
    ScopeColumn,
    ScopeData,
    ScopeFieldUsage,
    ScopeGraph,
    ScopeGraphEdge,
    ScopeInputEdge,
    ScopeLineageResult,
    ScopeLogicBlock,
    ScopeOutputField,
    SourceRef,
)
from .scope.sqlglot_config import suppress_invalid_json_path_warnings
from .scope.task_lineage import TaskLineageResult, parse_task_lineage
from .contract.fold import fold_session_scoped
from .render.mapping_markdown import render_mapping_markdown, render_warnings_markdown
from .serialize.scope_profile import build_scope_profile


PUBLIC_CORE_API = frozenset({
    "PUBLIC_CORE_API",
    "CONSTANT_SCOPE_ID",
    "DiagnosticWarning",
    "Diagnostics",
    "fold_session_scoped",
    "DictSchemaProvider",
    "MetadataFileError",
    "NoSupportedWriteStatementError",
    "NON_PHYSICAL_SOURCE_SCOPES",
    "SchemaMap",
    "SchemaProvider",
    "ScopeColumn",
    "ScopeData",
    "ScopeFieldUsage",
    "ScopeGraph",
    "ScopeGraphEdge",
    "ScopeInputEdge",
    "ScopeLineageResult",
    "ScopeLogicBlock",
    "ScopeOutputField",
    "SourceRef",
    "SYSTEM_SCOPE_ID",
    "TargetColumnMetadata",
    "TargetMetadataMap",
    "TargetTableMetadata",
    "TaskLineageResult",
    "build_end_to_end_lineage",
    "build_scope_profile",
    "catalog_prefixes",
    "column_details_for_table",
    "check_metadata_file",
    "extract_qualified_field_refs",
    "load_schema",
    "load_schema_sources",
    "load_target_table_metadata",
    "lookup_target_table_metadata",
    "materialize_schema",
    "metadata_dict_reader",
    "normalize_schema_map",
    "normalize_table_name",
    "parse_all_scope_lineage",
    "parse_scope_lineage",
    "parse_task_lineage",
    "render_mapping_markdown",
    "render_warnings_markdown",
    "resolve_display_expression",
    "suppress_invalid_json_path_warnings",
    "table_details_for_table",
    "to_dict",
    "to_json",
    "to_lineage_dict",
    "to_lineage_json",
    "to_task_lineage_dict",
    "to_task_lineage_json",
    "validate_diagnostics_document",
    "validate_cross_references",
    "validate_lineage_document",
    "write_lineage",
    "write_task_lineage",
})

__all__ = sorted(PUBLIC_CORE_API)
