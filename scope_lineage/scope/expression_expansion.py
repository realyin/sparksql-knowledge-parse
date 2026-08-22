"""Expression-resolution expansion from upstream output facts.

Home of _resolve_expression_resolution_from_output_sources -- one of the three
convergence passes the fact pipeline repeats (see scope_facts and WI-09) -- and
of the struct-member and physical-field expansion helpers it leans on.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import ErrorLevel, exp

from ._constants import DIALECT
from .expansion_budget import ExpansionBudget
from .expression_refs import _cached_pattern, _qualified_field_refs
from .expression_text import (
    _replace_qualified_ref_with_expression,
    _replace_unqualified_ref_with_expression,
)
from .scope_types import CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID, ScopeLineageResult, ScopeOutputField, SourceRef
from .source_refs import (
    _dedupe_generated_source_dicts,
    _dedupe_physical_field_dicts,
    _dedupe_rowset_source_dicts,
    _generated_sources_from_refs,
    _is_internal_scope_id,
    _physical_source_fields_for_ref,
    _physical_source_fields_for_refs,
    _source_kind_for_resolution,
)
from .star_passthrough import _star_passthrough_output_fact

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
