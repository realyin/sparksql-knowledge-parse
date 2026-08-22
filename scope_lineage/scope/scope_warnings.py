"""Post-pass semantic warning detectors.

All detectors take a ScopeLineageResult and append DiagnosticWarning objects
to result.diagnostics.warnings. Run after resolve_all + stats.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import ErrorLevel
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .scope_types import DiagnosticWarning, ScopeLineageResult
from ..sqlglot_config import suppress_invalid_json_path_warnings

PARSE_OPTS = {"error_level": ErrorLevel.IGNORE}
suppress_invalid_json_path_warnings()


def detect_warnings(result: ScopeLineageResult) -> None:
    """Run all detectors; appends warnings to result.diagnostics.warnings."""
    _detect_filter_in_join_on(result)
    _detect_magic_numbers(result)
    _detect_duplicate_table_in_union(result)
    _detect_union_branch_arity_mismatch(result)
    _detect_complex_aggregate_with_case(result)
    _detect_missing_or_ambiguous_output_alias(result)


# ---------------------------------------------------------------------------
# filter_in_join_on_clause
# ---------------------------------------------------------------------------

def _detect_filter_in_join_on(result: ScopeLineageResult) -> None:
    """Warn when a JOIN ON clause contains a constant-comparison row filter."""
    for scope_id, scope_data in result.scopes.items():
        for join in scope_data.joins:
            expr = join.condition_expression or ""
            if _has_constant_filter(expr):
                result.diagnostics.warnings.append(DiagnosticWarning(
                    type="filter_in_join_on_clause",
                    scope=scope_id,
                    msg=(
                        f"JOIN ON clause contains a row filter (constant comparison). "
                        f"Expression: {expr[:100]}"
                    ),
                ))


def _has_constant_filter(expr: str) -> bool:
    """Return True if the expression contains a comparison to a literal value."""
    if not expr:
        return False
    try:
        tree = sqlglot.parse_one(expr, dialect="spark", **PARSE_OPTS)
    except SqlglotError:
        return False
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = node.left, node.right
        if isinstance(right, (exp.Literal, exp.Null, exp.Boolean)):
            return True
        if isinstance(left, (exp.Literal, exp.Null, exp.Boolean)):
            return True
    return False


# ---------------------------------------------------------------------------
# magic_number
# ---------------------------------------------------------------------------

_WHITELISTED_MAGIC = {0.0, 1.0, -1.0, 100.0}


def _detect_magic_numbers(result: ScopeLineageResult) -> None:
    """Warn when a column expression contains unexplained numeric literals."""
    for scope_id, scope_data in result.scopes.items():
        for col in scope_data.columns:
            expr = col.expression or ""
            if not expr:
                continue
            try:
                tree = sqlglot.parse_one(expr, dialect="spark", **PARSE_OPTS)
            except SqlglotError:
                continue
            for lit in tree.find_all(exp.Literal):
                if not lit.is_number:
                    continue
                try:
                    val = float(lit.this)
                except (ValueError, TypeError):
                    continue
                # sqlglot parses -1 as Neg(Literal('1')); check parent for sign
                if isinstance(lit.parent, exp.Neg):
                    val = -val
                if val not in _WHITELISTED_MAGIC:
                    result.diagnostics.warnings.append(DiagnosticWarning(
                        type="magic_number",
                        scope=scope_id,
                        msg=(
                            f"Column '{col.name}' uses unexplained numeric literal {lit.this} "
                            f"in expression: {expr[:80]}"
                        ),
                    ))
                    break  # one warning per column


# ---------------------------------------------------------------------------
# duplicate_table_in_union
# ---------------------------------------------------------------------------

def _detect_duplicate_table_in_union(result: ScopeLineageResult) -> None:
    """Warn when the same physical table is a FROM/JOIN source of multiple UNION branches."""
    for scope_id, scope_data in result.scopes.items():
        if scope_data.kind != "union":
            continue
        branch_tables: dict[str, list[str]] = {}
        for branch_id in (scope_data.branches or []):
            branch = result.scopes.get(branch_id)
            if not branch:
                continue
            # A branch's sources are its FROM/JOIN relations. `depends_on` also carries
            # tables a filter subquery reads, which are not what this warning is about.
            # One branch can hold several edges to the same table -- a self-join -- and
            # that is not a duplicate across branches, so count each branch once.
            for table_id in dict.fromkeys(
                edge.source_id
                for edge in branch.input_edges
                if edge.source_type == "physical_table"
            ):
                branch_tables.setdefault(table_id, []).append(branch_id)
        for table_id, branches in branch_tables.items():
            if len(branches) > 1:
                result.diagnostics.warnings.append(DiagnosticWarning(
                    type="duplicate_table_in_union",
                    scope=scope_id,
                    msg=(
                        f"Table '{table_id}' appears in {len(branches)} UNION branches: "
                        f"{branches}. Verify this is intentional."
                    ),
                ))


# ---------------------------------------------------------------------------
# union_branch_arity_mismatch
# ---------------------------------------------------------------------------


def _detect_union_branch_arity_mismatch(
    result: ScopeLineageResult,
) -> None:
    """Expose an invalid UNION instead of silently aligning by position."""
    for scope_id, scope_data in result.scopes.items():
        if scope_data.kind != "union":
            continue
        alignment = scope_data.union_branch_alignment or {}
        branches = [
            branch
            for branch in alignment.get("branches") or []
            if isinstance(branch, dict)
        ]
        widths = [
            len(branch.get("select_items") or [])
            for branch in branches
        ]
        if len(widths) < 2 or len(set(widths)) == 1:
            continue
        details = ", ".join(
            f"{branch.get('branch_id') or '<unknown>'}={width}"
            for branch, width in zip(branches, widths)
        )
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="union_branch_arity_mismatch",
            scope=scope_id,
            msg=(
                "UNION branch column counts differ "
                f"({'/'.join(str(width) for width in widths)}): "
                f"{details}. Positional field alignment is incomplete."
            ),
        ))


# ---------------------------------------------------------------------------
# complex_aggregate_with_case
# ---------------------------------------------------------------------------

def _detect_complex_aggregate_with_case(result: ScopeLineageResult) -> None:
    """Warn on SUM(CASE WHEN...) / COUNT(CASE WHEN...) patterns."""
    for scope_id, scope_data in result.scopes.items():
        for col in scope_data.columns:
            if col.transform != "AGGREGATE":
                continue
            expr = col.expression or ""
            if not expr:
                continue
            try:
                tree = sqlglot.parse_one(expr, dialect="spark", **PARSE_OPTS)
            except SqlglotError:
                continue
            for agg in tree.find_all(exp.Sum, exp.Count, exp.Max, exp.Min, exp.Avg):
                if any(True for _ in agg.find_all(exp.Case)):
                    result.diagnostics.warnings.append(DiagnosticWarning(
                        type="complex_aggregate_with_case",
                        scope=scope_id,
                        msg=(
                            f"Column '{col.name}' uses aggregate-with-CASE pattern: "
                            f"{expr[:80]}"
                        ),
                    ))
                    break


def _detect_missing_or_ambiguous_output_alias(result: ScopeLineageResult) -> None:
    """Warn when an expression's generated name cannot be trusted as a target field."""
    for scope_id, scope_data in result.scopes.items():
        if not scope_data.writes_to:
            continue
        for col in scope_data.columns:
            if not col.name_is_generated and not re.fullmatch(r"\d+", str(col.name or "")):
                continue
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="output_alias_missing_or_ambiguous",
                scope=scope_id,
                msg=(
                    f"Output expression has generated name '{col.name}' and no reliable alias. "
                    f"Do not treat '{scope_data.writes_to}.{col.name}' as a confirmed physical field. "
                    f"Expression: {(col.expression or '')[:100]}"
                ),
            ))
