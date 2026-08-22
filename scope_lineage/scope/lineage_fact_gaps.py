"""Lineage Fact Gaps — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the LineageFactGapsEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import re
import sqlglot
from sqlglot import exp
from .scope_types import (
    ScopeData,
    ScopeLineageResult,
    ScopeOutputField,
)
# Leaf helpers come from `_shared`, never from the orchestrator: importing it back
# formed a cycle that only worked because Python hands out a partially-initialised
# module, making import order load-bearing (ARCH-001).
from ._constants import DIALECT, PARSE_OPTS
from .sequences import _unique_ordered
from .source_refs import _source_kind_for_resolution


def _populate_lineage_fact_gaps(result: ScopeLineageResult) -> None:
    gaps: list[dict[str, object]] = []
    for (scope_id, scope_data) in result.scopes.items():
        for (index, output) in enumerate(scope_data.outputs):
            branch_mappings = (output.expression_resolution or {}).get('union_branch_mappings') or []
            gap = _lineage_gap_from_expression_resolution(result=result, scope_data=scope_data, scope_id=scope_id, object_type='output', object_name=output.name, expression_sql=output.expression, expression_resolution=output.expression_resolution or {}, evidence_path=f'lineage.scopes.{scope_id}.outputs[{index}]', output_fields=[output.name] if output.name else [], target_columns=output.final_target_columns or output.target_columns)
            if gap:
                branch_evidence = _union_branch_mappings_gap_evidence(branch_mappings)
                if branch_evidence:
                    evidence_summary = dict(gap.get('evidence_summary') or {})
                    evidence_summary['union_branch_mappings'] = branch_evidence
                    gap['evidence_summary'] = evidence_summary
                gaps.append(gap)
            for (branch_index, branch_mapping) in enumerate(branch_mappings):
                if not isinstance(branch_mapping, dict):
                    continue
                branch_gap = _lineage_gap_from_union_branch_mapping(result=result, scope_data=scope_data, scope_id=scope_id, output=output, branch_mapping=branch_mapping, evidence_path=f'lineage.scopes.{scope_id}.outputs[{index}].expression_resolution.union_branch_mappings[{branch_index}]')
                if branch_gap:
                    gaps.append(branch_gap)
        for (block_index, block) in enumerate(scope_data.logic_blocks):
            detail = block.aggregation_detail or {}
            for item_key in ('group_by_items', 'aggregate_items'):
                for (item_index, item) in enumerate(detail.get(item_key) or []):
                    if not isinstance(item, dict):
                        continue
                    gap = _lineage_gap_from_expression_resolution(result=result, scope_data=scope_data, scope_id=scope_id, object_type=f'aggregation_detail.{item_key}', object_name=str(item.get('output_field') or item.get('expression_sql') or ''), expression_sql=str(item.get('expression_sql') or ''), expression_resolution=item.get('expression_resolution') or {}, evidence_path=f'lineage.scopes.{scope_id}.logic_blocks[{block_index}].aggregation_detail.{item_key}[{item_index}]', output_fields=[str(item.get('output_field'))] if item.get('output_field') else [], target_columns=[])
                    if gap:
                        gaps.append(gap)
    for (index, gap) in enumerate(gaps, start=1):
        gap['gap_id'] = f'lineage_gap:{index:04d}'
    result.diagnostics.lineage_fact_gaps = gaps


def _mark_gaps_from_recovered_syntax(result: ScopeLineageResult) -> None:
    """Say which gaps are shadows of a repaired parse rather than facts about the query.

    A repaired parse drops the tokens sqlglot could not place, so a gap derived from what
    survived describes the truncation, not the SQL — the statement said FROM, and the gap
    says the field has no source. Both kinds land in one list, and counting them together
    turned a single syntax problem into hundreds of apparent capability gaps in one statement
    (PARSE-002).

    Applied after ``syntax_status`` is known, which is later than the gaps are built.
    """
    if result.syntax_status != 'recovered':
        return
    for gap in result.diagnostics.lineage_fact_gaps:
        gap['derived_from_recovered_syntax'] = True


def _lineage_gap_from_union_branch_mapping(*, result: ScopeLineageResult, scope_data: ScopeData, scope_id: str, output: ScopeOutputField, branch_mapping: dict[str, object], evidence_path: str) -> dict[str, object] | None:
    status = str(branch_mapping.get('resolution_status') or 'unresolved')
    physical_fields = branch_mapping.get('physical_source_fields') or []
    generated_sources = branch_mapping.get('generated_sources') or []
    rowset_sources = branch_mapping.get('rowset_sources') or []
    source_kind = str(branch_mapping.get('source_kind') or _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources))
    missing_reasons = [str(reason) for reason in branch_mapping.get('missing_reasons') or [] if reason]
    if status == 'resolved' and source_kind in {'physical', 'generated', 'mixed', 'rowset'} and (not missing_reasons):
        return None
    if status in {'unresolved', 'partially_resolved'} and (not missing_reasons):
        missing_reasons = ['expression_resolution_incomplete']
    expression_resolution = {'status': status, 'resolution_type': branch_mapping.get('resolution_type') or 'union_branch_mapping', 'physical_source_fields': physical_fields, 'generated_sources': generated_sources, **({'rowset_sources': rowset_sources} if rowset_sources else {}), 'source_kind': source_kind, 'missing_reasons': missing_reasons, 'expanded_expression': branch_mapping.get('expression_sql')}
    object_name = f"{output.name}@{branch_mapping.get('branch_scope_id')}" if branch_mapping.get('branch_scope_id') else output.name
    gap = _lineage_gap_from_expression_resolution(result=result, scope_data=scope_data, scope_id=scope_id, object_type='output.union_branch_mapping', object_name=object_name, expression_sql=str(branch_mapping.get('expression_sql') or ''), expression_resolution=expression_resolution, evidence_path=evidence_path, output_fields=[output.name] if output.name else [], target_columns=output.final_target_columns or output.target_columns)
    if gap is None:
        return None
    branch_evidence = _union_branch_mapping_gap_evidence(branch_mapping)
    if branch_evidence:
        evidence_summary = dict(gap.get('evidence_summary') or {})
        evidence_summary['union_branch_mappings'] = [branch_evidence]
        gap['evidence_summary'] = evidence_summary
    return gap


def _union_branch_mappings_gap_evidence(branch_mappings: object) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for branch_mapping in branch_mappings or []:
        if not isinstance(branch_mapping, dict):
            continue
        branch_evidence = _union_branch_mapping_gap_evidence(branch_mapping)
        if branch_evidence:
            evidence.append(branch_evidence)
    return evidence


def _union_branch_mapping_gap_evidence(branch_mapping: dict[str, object]) -> dict[str, object]:
    branch_evidence_keys = ('branch_scope_id', 'branch_index', 'output_field', 'aligned_output_name', 'expected_output_name', 'expected_position', 'available_branch_outputs', 'candidate_branch_output_fields', 'candidate_rejection_reasons', 'missing_reasons')
    return {key: branch_mapping.get(key) for key in branch_evidence_keys if branch_mapping.get(key) not in (None, [], {})}


def _lineage_gap_from_expression_resolution(*, result: ScopeLineageResult, scope_data: ScopeData, scope_id: str, object_type: str, object_name: str, expression_sql: str | None, expression_resolution: dict[str, object], evidence_path: str, output_fields: list[str], target_columns: list[str]) -> dict[str, object] | None:
    status = str(expression_resolution.get('status') or 'unknown')
    physical_fields = expression_resolution.get('physical_source_fields') or []
    generated_sources = expression_resolution.get('generated_sources') or []
    source_kind = str(expression_resolution.get('source_kind') or _source_kind_for_resolution(physical_fields, generated_sources))
    missing_reasons = [str(reason) for reason in expression_resolution.get('missing_reasons') or [] if reason]
    if status == 'resolved' and source_kind in {'physical', 'generated', 'mixed', 'rowset'} and (not missing_reasons):
        return None
    gap_type = _lineage_gap_type(missing_reasons, source_kind)
    needed_fact = _lineage_gap_needed_fact(gap_type)
    gap_bucket = _lineage_gap_bucket(expression_sql, object_type, missing_reasons)
    evidence_summary = _lineage_gap_evidence_summary(result, scope_data, object_name, expression_sql, target_columns)
    root_impact = bool(evidence_summary.get('has_target_impact'))
    return {'gap_type': gap_type, 'gap_bucket': gap_bucket, 'gap_sub_bucket': _lineage_gap_sub_bucket(gap_bucket, scope_id, object_type, expression_sql, missing_reasons, evidence_summary), 'scope_id': scope_id, 'object_type': object_type, 'object_name': object_name, 'expression_sql': expression_sql, 'expression_resolution_status': status, 'source_kind': source_kind, 'missing_reasons': missing_reasons, 'needed_fact': needed_fact, 'root_impact': root_impact, 'owner_hint': _lineage_gap_owner_hint(gap_type, gap_bucket, root_impact), 'evidence_path': evidence_path, 'evidence_summary': evidence_summary, 'downstream_impact': {'output_fields': [field for field in output_fields if field], 'target_columns': [target for target in target_columns if target]}}


def _lineage_gap_bucket(expression_sql: str | None, object_type: str, missing_reasons: list[str]) -> str:
    expression = str(expression_sql or '').strip()
    if any((reason.startswith('upstream_output_not_found:') or reason.startswith('upstream_output_unresolved:') for reason in missing_reasons)):
        return 'upstream_output_mapping'
    if any((reason.startswith('alias_not_bound_to_input_source:') for reason in missing_reasons)):
        return 'alias_binding'
    if re.fullmatch('`?[A-Za-z_][A-Za-z0-9_]*`?', expression):
        return 'bare_unqualified_field'
    if expression.startswith('`'):
        return 'qualified_expression_unresolved'
    return 'other_expression_unresolved'


def _lineage_gap_sub_bucket(gap_bucket: str, scope_id: str, object_type: str, expression_sql: str | None, missing_reasons: list[str], evidence_summary: dict[str, object]) -> str:
    if gap_bucket == 'upstream_output_mapping':
        return 'upstream_output_unresolved'
    if gap_bucket == 'alias_binding':
        if object_type.startswith('aggregation_detail') and any((reason.startswith('alias_not_bound_to_input_source:item') for reason in missing_reasons)):
            return 'udtf_struct_alias_unresolved'
        return 'alias_binding_unresolved'
    if gap_bucket == 'bare_unqualified_field':
        if scope_id == 'ROOT' and evidence_summary.get('has_target_impact'):
            if evidence_summary.get('scope_input_count') == 1:
                return 'root_bare_single_input_candidate'
            return 'root_bare_no_unique_input'
        return 'bare_unqualified_no_target_impact'
    if object_type.startswith('aggregation_detail'):
        if '/*' in str(expression_sql or ''):
            return 'comment_wrapped_qualified_ref'
        return 'aggregation_detail_expression_refs_missing'
    if gap_bucket == 'qualified_expression_unresolved':
        if '/*' in str(expression_sql or ''):
            return 'comment_wrapped_qualified_ref'
        return 'physical_table_qualified_ref_missing'
    return 'non_actionable_no_source_ref'


def _lineage_gap_evidence_summary(result: ScopeLineageResult, scope_data: ScopeData, object_name: str, expression_sql: str | None, target_columns: list[str]) -> dict[str, object]:
    candidate_source_ids = [str(ref.get('source_id')) for ref in scope_data.input_source_refs if ref.get('source_id')]
    candidate_output_fields: list[str] = []
    for source_id in candidate_source_ids:
        source_scope = result.scopes.get(source_id)
        if source_scope is None:
            continue
        if any((output.name == object_name for output in source_scope.outputs)):
            candidate_output_fields.append(f'{source_id}.{object_name}')
    return {'has_target_impact': bool([target for target in target_columns if target]), 'scope_input_count': len(candidate_source_ids), 'candidate_source_ids': _unique_ordered(candidate_source_ids), 'candidate_output_fields': _unique_ordered(candidate_output_fields), 'expression_ref_count': _expression_column_ref_count(expression_sql)}


def _expression_column_ref_count(expression: str | None) -> int:
    if not expression:
        return 0
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except sqlglot.errors.SqlglotError:
        return 0
    return sum((1 for column in parsed.find_all(exp.Column)))


def _lineage_gap_type(missing_reasons: list[str], source_kind: str) -> str:
    if any((reason.startswith('alias_not_bound_to_input_source:') for reason in missing_reasons)):
        return 'alias_binding_missing'
    if any((reason.startswith('upstream_output_not_found:') or reason.startswith('upstream_output_unresolved:') for reason in missing_reasons)):
        return 'scope_output_mapping_missing'
    if source_kind == 'unresolved' or 'no_physical_source_fields' in missing_reasons:
        return 'expression_source_unresolved'
    return 'expression_resolution_incomplete'


def _lineage_gap_needed_fact(gap_type: str) -> str:
    mapping = {'alias_binding_missing': 'input alias to source binding', 'scope_output_mapping_missing': 'upstream scope output field mapping', 'expression_source_unresolved': 'physical or generated expression source', 'expression_resolution_incomplete': 'complete expression source resolution'}
    return mapping.get(gap_type, 'lineage expression fact')


def _lineage_gap_owner_hint(gap_type: str, gap_bucket: str, root_impact: bool) -> str:
    if not root_impact:
        return 'parser_internal_fact_backfill'
    if gap_type in {'alias_binding_missing', 'scope_output_mapping_missing', 'expression_source_unresolved', 'expression_resolution_incomplete'}:
        return 'parser_fact_backfill'
    if gap_bucket == 'bare_unqualified_field':
        return 'parser_or_metadata_review'
    return 'lineage_review'
