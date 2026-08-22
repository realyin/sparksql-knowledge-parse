"""Select Scope — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the SelectScopeEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import re
from typing import List
from sqlglot import exp
from sqlglot.optimizer.scope import Scope
from .parser import (
    _ORIGINALLY_ANONYMOUS_PROJECTION_META,
    _qualified_table,
    _extract_name_inner,
    _normalize_table_name,
)
from .scope_types import (
    SourceRef,
    ScopeColumn,
    ScopeData,
    ScopeFilter,
    ScopeJoin,
    ScopeLineageResult,
    DiagnosticWarning,
)
from ._constants import DIALECT, _SCOPE_ID_ATTR
from .function_catalog import _KNOWN_UDAFS
from .sequences import _unique_ordered
from .source_refs import _constant_sources, _source_ref_binding_key
from .sqlglot_walk import _classify_extended, _inside_nested_set_op, _pivot_of_source_node, _pivot_output_names, _selected_sources, _source_free_leaf_sources, _source_item_from_ast_node
from .column_ref_resolver import (
    _bound_source_ref,
    _input_ref_id_for_source_alias,
    _resolve_column_refs_in_expr,
)


def _resolve_select_scope(
    sg_scope: Scope, scope_id: str, scope_data: ScopeData,
    result: ScopeLineageResult, schema: dict | None = None,
) -> None:
    """Resolve projections, joins, filters, group_by, having, order_by for a Select scope."""
    sel = sg_scope.expression

    # Resolve projections
    for projection_ordinal, proj in enumerate(sel.expressions):
        cols = _resolve_projection(
            proj,
            sg_scope,
            result,
            schema,
            projection_ordinal=projection_ordinal,
        )
        scope_data.columns.extend(cols)

    # Resolve joins
    for join in sel.args.get("joins") or []:
        j = _resolve_join(join, sg_scope, result, schema)
        if j:
            scope_data.joins.append(j)

    # Resolve WHERE
    where = sel.args.get("where")
    if where:
        scope_data.filters = _resolve_filter(where, sg_scope, result, schema)

    # Resolve GROUP BY
    group = sel.args.get("group")
    if group:
        scope_data.group_by = _resolve_expr_list(
            group.expressions if hasattr(group, "expressions") else [group],
            sg_scope, result, schema,
        )

    # Resolve HAVING
    having = sel.args.get("having")
    if having:
        scope_data.having = _resolve_filter(having, sg_scope, result, schema)

    # Resolve ORDER BY
    order = sel.args.get("order")
    if order:
        for item in (order.expressions if hasattr(order, "expressions") else []):
            direction = "DESC" if isinstance(item, exp.Ordered) and item.desc else "ASC"
            expr = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(expr, exp.Column) and not expr.table:
                output_names = {c.name for c in scope_data.columns}
                if expr.name in output_names:
                    scope_data.order_by.append({
                        "scope": scope_id,
                        "column": expr.name,
                        "direction": direction,
                    })
                    continue
            col_refs = _resolve_column_refs_in_expr(item, sg_scope, result, schema)
            for ref in col_refs:
                scope_data.order_by.append({"scope": ref.scope, "column": ref.column, "direction": direction})


def _resolve_projection(
    proj: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
    *,
    projection_ordinal: int | None = None,
) -> List[ScopeColumn]:
    """Resolve a single SELECT projection into one or more ScopeColumns.

    Star projections (a.* / *) are expanded into individual DIRECT columns
    when the source scope has resolved columns or when schema is available.
    Falls back to a single EXPAND_ALL column when expansion is impossible.
    """
    scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN")
    # UNION output names are positional and come from the first branch. Keep generated
    # placeholders inside branch scopes so the UNION resolver can apply that rule instead
    # of mistaking an inferred source-column name for an explicit branch alias.
    name, inner = _extract_name_inner(
        proj,
        recover_generated_alias=not scope_id.startswith("union:"),
    )
    originally_anonymous = bool(
        inner.meta.get(_ORIGINALLY_ANONYMOUS_PROJECTION_META)
    )
    distinct_source_names = {
        column.name
        for column in inner.find_all(exp.Column)
        if column.name
    }
    name_has_source_evidence = bool(
        len(distinct_source_names) == 1
        and name in distinct_source_names
    )
    if scope_id == "ROOT":
        name_is_generated = bool(
            originally_anonymous and not name_has_source_evidence
        )
    else:
        # Internal UNION/subquery outputs keep their own expression identity; the UNION
        # resolver aligns them by position later.  Mark only sqlglot's explicit generated
        # placeholder shape here, matching the established internal-scope behavior.
        name_is_generated = bool(
            isinstance(proj, exp.Alias)
            and re.fullmatch(r"(?:_col_\d+|\d+)", str(proj.alias or ""))
            and originally_anonymous
            and name == proj.alias
        )
    if (
        scope_id == "ROOT"
        and name_is_generated
        and projection_ordinal is not None
    ):
        # sqlglot may expose expression syntax as ``output_name`` for an anonymous
        # projection: an array index becomes ``0`` and a string literal ``'N'`` becomes
        # ``n``.  Neither is a target field.  Keep a positional placeholder until
        # authoritative target DDL/Schema metadata can bind the physical field.  A wrapper over
        # exactly one source field keeps the evidence-backed recovery performed above.
        name = f"_col_{projection_ordinal}"
    transform = _classify_extended(inner)
    expression = inner.sql(dialect=DIALECT)

    multi_alias_cols = _resolve_multi_alias_projection(proj, sg_scope, result, schema)
    if multi_alias_cols:
        return multi_alias_cols

    udtf_cols = _resolve_projection_udtf(inner, name, sg_scope, result, schema)
    if udtf_cols:
        return udtf_cols

    # Handle EXPAND_ALL (SELECT * / a.*)
    if isinstance(inner, exp.Star) or (
        isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)
    ):
        qualified = isinstance(inner, exp.Column)
        table_alias = inner.table if qualified else None
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, "UNKNOWN")
        expanded = _expand_star_into_columns(sg_scope, table_alias, result, schema)
        if expanded:
            return expanded
        label = f"{table_alias}.*" if qualified else "SELECT *"
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="star_not_expanded",
            scope=scope_id,
            msg=_star_not_expanded_message(sg_scope, table_alias, schema, label),
        ))
        return [ScopeColumn(
            name=f"{table_alias}.*" if qualified else "*",
            transform="EXPAND_ALL",
            expression=expression if qualified else "*",
            sources=_expand_star_sources(sg_scope, table_alias, result, schema))]

    # Handle CONSTANT
    if transform == "CONSTANT":
        return [
            ScopeColumn(
                name=name,
                transform="CONSTANT",
                name_is_generated=name_is_generated,
                expression=expression,
                sources=_constant_sources(expression),
            )
        ]

    # Find all Column references and resolve them
    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    if not sources:
        sources = _fallback_sources_for_source_free_expr(inner, transform, expression, sg_scope, result)

    col = ScopeColumn(
        name=name,
        transform=transform,
        name_is_generated=name_is_generated,
        transform_subkind=_transform_subkind(inner, transform),
        expression=expression,
        sources=sources,
    )

    # Populate optional fields by transform type
    if transform == "CONDITIONAL":
        col.case_branches = _extract_case_branches(inner)

    if transform == "WINDOW":
        col.window = _extract_window_info(inner, sg_scope, result, schema)

    if transform == "AGGREGATE":
        col.agg_function = _extract_agg_function(inner)

    return [col]


def _fallback_sources_for_source_free_expr(
    inner: exp.Expression,
    transform: str,
    expression: str,
    sg_scope: Scope,
    result: ScopeLineageResult,
) -> list[SourceRef]:
    """Give source-free non-literal expressions a meaningful terminal lineage.

    Examples:
    - COUNT(*) and ROW_NUMBER() depend on the current input row set.
    - NOW(), CURRENT_DATE(), RAND() are runtime/system values.
    - DATE_ADD('2026-04-27', 1) and CONCAT('a', 'b') are literal-derived values.
    """
    if transform in {"AGGREGATE", "WINDOW"}:
        rowset_sources = _rowset_sources(sg_scope, result)
        if rowset_sources:
            return rowset_sources

    return _source_free_leaf_sources(inner, expression)


def _rowset_sources(sg_scope: Scope, result: ScopeLineageResult) -> list[SourceRef]:
    sources: list[SourceRef] = []
    seen = set()
    for alias, source in _selected_sources(sg_scope).items():
        ref = _bound_source_ref(alias, source, "*", sg_scope, result)
        key = _source_ref_binding_key(ref)
        if key not in seen:
            seen.add(key)
            sources.append(ref)
    return sources


def _resolve_multi_alias_projection(
    proj: exp.Expression,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> list[ScopeColumn]:
    """Resolve SELECT-list table functions shaped like ``func(x) AS (c1, c2)``."""
    if not isinstance(proj, exp.Aliases):
        return []

    inner = proj.this
    if inner is None:
        return []

    names = [
        alias.name if hasattr(alias, "name") else str(alias)
        for alias in (proj.expressions or [])
    ]
    names = [name for name in names if name]
    if not names:
        return []

    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    expression = proj.sql(dialect=DIALECT)
    return [
        ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=expression,
            sources=list(sources),
        )
        for name in names
    ]


def _resolve_projection_udtf(
    inner: exp.Expression,
    output_name: str,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> list[ScopeColumn]:
    """Resolve generator functions used as SELECT projections.

    Spark/Hive allow ``SELECT posexplode(arr)`` without an explicit alias. The
    engine exposes default columns (``pos``, ``col``), and downstream SQL often
    references those names directly.
    """
    if isinstance(inner, exp.Posexplode):
        names = ["pos", "col"]
    elif isinstance(inner, exp.Explode):
        names = ["col"] if output_name.startswith("_col_") else [output_name]
    else:
        return []

    sources = _resolve_column_refs_in_expr(inner, sg_scope, result, schema)
    expression = inner.sql(dialect=DIALECT)
    if not sources:
        # Source-free generators (e.g. EXPLODE(SEQUENCE(CURRENT_DATE, ...)) date
        # spines) still need a terminal lineage: SYSTEM for runtime values,
        # CONSTANT for literal-derived ones.
        sources = _source_free_leaf_sources(inner, expression)
    return [
        ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=expression,
            sources=list(sources),
        )
        for name in names
    ]


def _expand_star_sources(
    sg_scope: Scope,
    table_alias: str | None,
    result: ScopeLineageResult,
    schema: dict | None,
) -> List[SourceRef]:
    """Expand SELECT * or a.* into source refs when possible.

    Returns refs with column="*" — used as fallback when expansion into
    individual columns is not possible.
    """
    sources = []
    if table_alias:
        # Qualified: a.*
        src = sg_scope.sources.get(table_alias)
        if isinstance(src, (Scope, exp.Table)):
            sources.append(
                _bound_source_ref(table_alias, src, "*", sg_scope, result)
            )
    else:
        # Bare *: all sources
        for alias, source in _selected_sources(sg_scope).items():
            if isinstance(source, (Scope, exp.Table)):
                sources.append(
                    _bound_source_ref(alias, source, "*", sg_scope, result)
                )
    return sources


def _star_not_expanded_message(
    sg_scope: Scope,
    table_alias: str | None,
    schema: dict | None,
    projection: str,
) -> str:
    sources: list[str] = []
    missing_schema_sources: list[str] = []
    unresolved_scope_sources: list[str] = []

    if table_alias:
        source_items = [(table_alias, sg_scope.sources.get(table_alias))]
    else:
        source_items = list(_selected_sources(sg_scope).items())

    for alias, source in source_items:
        if isinstance(source, Scope):
            upstream_id = getattr(source, _SCOPE_ID_ATTR, None) or f"scope:{alias}"
            sources.append(str(upstream_id))
            unresolved_scope_sources.append(str(upstream_id))
            continue
        if isinstance(source, exp.Table):
            table_name = _qualified_table(source)
            sources.append(table_name)
            if not schema or _normalize_table_name(table_name) not in schema:
                missing_schema_sources.append(table_name)

    details: list[str] = []
    if missing_schema_sources:
        details.append(f"missing_schema_sources={','.join(_unique_ordered(missing_schema_sources))}")
    if unresolved_scope_sources:
        details.append(f"unresolved_scope_sources={','.join(_unique_ordered(unresolved_scope_sources))}")
    if sources:
        details.append(f"sources={','.join(_unique_ordered(sources))}")
    suffix = f"; {'; '.join(details)}" if details else ""
    return f"{projection} could not be expanded: no schema and no resolved source columns{suffix}"


def _record_star_expanded_from(sg_scope: Scope, result: ScopeLineageResult, upstream_id: str) -> None:
    """Remember which upstream scope a star expansion enumerated columns from.

    If that upstream's own enumeration is incomplete (open star chain), the
    resolver can later materialize referenced-but-missing columns through it.
    """
    scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
    scope_data = result.scopes.get(scope_id) if scope_id else None
    if scope_data is not None and upstream_id not in scope_data.star_expanded_from:
        scope_data.star_expanded_from.append(upstream_id)


def _record_star_schema_source(sg_scope: Scope, result: ScopeLineageResult, fq: str) -> None:
    """Remember that this scope's star was expanded from a physical table's schema.

    Schema exports can be incomplete; if a downstream scope later references a
    column the schema omitted, the resolver uses this marker to materialize the
    column as a pass-through from the physical table (the SQL names it, so it
    must exist) instead of leaving a dangling reference.
    """
    scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
    scope_data = result.scopes.get(scope_id) if scope_id else None
    if scope_data is not None and fq not in scope_data.star_schema_sources:
        scope_data.star_schema_sources.append(fq)


def _pivot_star_columns(
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None,
) -> List[ScopeColumn]:
    """Columns a `SELECT *` sees over a pivoted relation.

    Each name in the PIVOT's IN list becomes a column whose value comes from the aggregate,
    so that is where its lineage points. A non-literal IN list leaves the set unknowable and
    yields nothing, keeping the caller on its EXPAND_ALL fallback rather than inventing
    names.
    """
    from_ = sg_scope.expression.args.get("from_") if isinstance(sg_scope.expression, exp.Select) else None
    if from_ is None or getattr(from_, "this", None) is None:
        return []
    pivot = _pivot_of_source_node(from_.this)
    if pivot is None:
        return []
    names = _pivot_output_names(pivot)
    if not names:
        return []
    sources: list[SourceRef] = []
    for aggregate in pivot.expressions:
        sources.extend(_resolve_column_refs_in_expr(aggregate, sg_scope, result, schema))
    return [
        # Lowercased to match every other name in the result: qualify normalizes column
        # references, so `IN ('UPPER_NAME')` has to meet a reference written
        # `upper_name`.
        ScopeColumn(
            name=name.lower(),
            transform="DIRECT",
            expression=exp.column(name.lower()).sql(dialect=DIALECT),
            sources=list(sources),
        )
        for name in names
    ]


# Spark's grammar allows exactly one star modifier: `ASTERISK exceptClause?` and
# `qualifiedName DOT ASTERISK exceptClause?` (SqlBaseParser.g4). REPLACE / RENAME / ILIKE
# belong to other engines; sqlglot's base parser accepts them for every dialect, so their
# presence proves nothing about Spark and they are reported rather than modelled.
_UNSUPPORTED_STAR_MODIFIERS = ("replace", "rename", "ilike")


def _star_modifiers(star_node: exp.Expression) -> tuple[list[str], list[str]]:
    """Return (names an EXCEPT list removes, names of modifiers Spark has no grammar for)."""
    node = star_node.this if isinstance(star_node, exp.Column) else star_node
    if not isinstance(node, exp.Star):
        return [], []
    except_names = [
        item.name for item in (node.args.get("except_") or []) if getattr(item, "name", None)
    ]
    unsupported = [key for key in _UNSUPPORTED_STAR_MODIFIERS if node.args.get(key)]
    return except_names, unsupported


def _expand_star_into_columns(
    sg_scope: Scope, table_alias: str | None,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[ScopeColumn]:
    """Expand SELECT * or a.* into individual DIRECT ScopeColumns.

    Tries, in order:
    1. Source scope already has resolved columns (CTE/subquery) → expand from those
    2. Schema provides column names for the physical table → expand from schema
    Returns empty list if expansion is impossible (caller should fall back to EXPAND_ALL).
    """
    columns: List[ScopeColumn] = []

    if table_alias:
        # Qualified: a.*
        src = sg_scope.sources.get(table_alias)
        if isinstance(src, Scope):
            upstream_id = getattr(src, _SCOPE_ID_ATTR, None)
            if upstream_id:
                upstream_sd = result.scopes.get(upstream_id)
                if upstream_sd and upstream_sd.columns:
                    _record_star_expanded_from(sg_scope, result, upstream_id)
                    for col in upstream_sd.columns:
                        columns.append(ScopeColumn(
                            name=col.name,
                            transform="DIRECT",
                            expression=exp.column(
                                col.name,
                                table=table_alias,
                            ).sql(dialect=DIALECT),
                            sources=[SourceRef(
                                scope=upstream_id,
                                column=col.name,
                                qualifier=table_alias,
                                binding_scope_id=getattr(
                                    sg_scope,
                                    _SCOPE_ID_ATTR,
                                    None,
                                ),
                                input_ref_id=_input_ref_id_for_source_alias(
                                    sg_scope,
                                    table_alias,
                                    src,
                                ),
                            )],
                        ))
                    return columns
        elif isinstance(src, exp.Table) and schema:
            fq = _qualified_table(src)
            short = _normalize_table_name(fq)
            col_names = schema.get(short, [])
            if col_names:
                _record_star_schema_source(sg_scope, result, fq)
                col_names = _with_referenced_columns_missing_from_schema(
                    sg_scope, table_alias, col_names
                )
                for cn in col_names:
                    columns.append(ScopeColumn(
                        name=cn,
                        transform="DIRECT",
                        expression=exp.column(
                            cn,
                            table=table_alias,
                        ).sql(dialect=DIALECT),
                        sources=[SourceRef(
                            scope=fq,
                            column=cn,
                            qualifier=table_alias,
                            binding_scope_id=getattr(
                                sg_scope,
                                _SCOPE_ID_ATTR,
                                None,
                            ),
                            input_ref_id=_input_ref_id_for_source_alias(
                                sg_scope,
                                table_alias,
                                src,
                            ),
                        )],
                    ))
                return columns
    else:
        # A PIVOT replaces the relation's columns with the names in its IN list, so `*` over
        # a pivoted source is those names — not the pivoted subquery's own columns. The pivot
        # is usually unaliased and sits directly behind this `SELECT *`, which is the shape
        # that left every downstream reference to it unresolved (PIVOT-001).
        pivoted = _pivot_star_columns(sg_scope, result, schema)
        if pivoted:
            return pivoted
        # Bare *: all sources
        for alias, source in _selected_sources(sg_scope).items():
            if isinstance(source, Scope):
                upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
                if upstream_id:
                    upstream_sd = result.scopes.get(upstream_id)
                    if upstream_sd and upstream_sd.columns:
                        _record_star_expanded_from(sg_scope, result, upstream_id)
                        for col in upstream_sd.columns:
                            columns.append(ScopeColumn(
                                name=col.name,
                                transform="DIRECT",
                                expression=exp.column(
                                    col.name,
                                    table=alias,
                                ).sql(dialect=DIALECT),
                                sources=[_bound_source_ref(
                                    alias,
                                    source,
                                    col.name,
                                    sg_scope,
                                    result,
                                )],
                            ))
                    else:
                        # Source scope has no columns yet — cannot expand
                        return []
            elif isinstance(source, exp.Table):
                if schema:
                    fq = _qualified_table(source)
                    short = _normalize_table_name(fq)
                    col_names = schema.get(short, [])
                    if col_names:
                        _record_star_schema_source(sg_scope, result, fq)
                        col_names = _with_referenced_columns_missing_from_schema(
                            sg_scope, alias, col_names
                        )
                        for cn in col_names:
                            columns.append(ScopeColumn(
                                name=cn,
                                transform="DIRECT",
                                expression=exp.column(
                                    cn,
                                    table=alias,
                                ).sql(dialect=DIALECT),
                                sources=[_bound_source_ref(
                                    alias,
                                    source,
                                    cn,
                                    sg_scope,
                                    result,
                                )],
                            ))
                    else:
                        return []
                else:
                    return []
        if columns:
            return columns

    return []


def _with_referenced_columns_missing_from_schema(
    sg_scope: Scope,
    table_alias: str | None,
    col_names: list[str],
) -> list[str]:
    """Keep explicit filter/join/order refs when schema misses partition-like columns.

    Some metastore exports omit partition columns such as ``dt`` even though
    Spark ``SELECT *`` exposes them. If a star-expanded scope later references
    such a column, treating it as absent creates an internal dangling ref. The
    SQL already names the column, so retaining it as a pass-through is safer
    than dropping it from the expanded star list.
    """
    extra: list[str] = []
    source_count = len(_selected_sources(sg_scope))
    for col_ref in _scope_local_column_refs(sg_scope):
        if col_ref.table:
            if table_alias and col_ref.table != table_alias:
                continue
            if not table_alias:
                continue
        elif table_alias and source_count > 1:
            continue
        name = col_ref.name
        if name and name not in col_names and name not in extra:
            extra.append(name)
    return list(col_names) + extra


def _scope_local_column_refs(sg_scope: Scope) -> list[exp.Column]:
    expr = sg_scope.expression
    if not isinstance(expr, exp.Select):
        return []

    roots = []
    for key in ("where", "having", "qualify"):
        node = expr.args.get(key)
        if node is not None:
            roots.append(node)
    group = expr.args.get("group")
    if group is not None:
        roots.extend(group.expressions if hasattr(group, "expressions") else [group])
    order = expr.args.get("order")
    if order is not None:
        roots.extend(order.expressions if hasattr(order, "expressions") else [order])
    for join in expr.args.get("joins") or []:
        on_expr = join.args.get("on")
        if on_expr is not None:
            roots.append(on_expr)

    refs: list[exp.Column] = []
    for root in roots:
        for col_ref in root.find_all(exp.Column):
            if not _inside_nested_set_op(root, col_ref):
                refs.append(col_ref)
    return refs


def _resolve_join(
    join: exp.Join, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> ScopeJoin | None:
    """Resolve a JOIN into a ScopeJoin."""
    # sqlglot mapping:
    #   LEFT JOIN  -> kind='',  side='LEFT'
    #   RIGHT JOIN -> kind='',  side='RIGHT'
    #   FULL JOIN  -> kind='',  side='FULL'
    #   CROSS JOIN -> kind='CROSS', side=''
    #   INNER JOIN -> kind='INNER', side=''
    #   LEFT OUTER JOIN -> kind='OUTER', side='LEFT'
    kind = (join.kind or "").upper()
    side = (join.side or "").upper()

    if kind == "CROSS":
        join_type = "CROSS"
    elif kind == "INNER":
        join_type = "INNER"
    elif kind == "OUTER" and side in ("LEFT", "RIGHT", "FULL"):
        join_type = f"{side}_OUTER"
    elif side in ("LEFT", "RIGHT", "FULL"):
        join_type = f"{side}_OUTER"
    else:
        join_type = kind or "INNER"

    right = join.this
    right_alias = right.alias if isinstance(right, (exp.Table, exp.Subquery)) else None
    right_scope = _resolve_table_to_scope_id(right, sg_scope)

    # Determine left_scope: the first FROM source
    left_scope = None
    from_ = sg_scope.expression.args.get("from_") if isinstance(sg_scope.expression, exp.Select) else None
    if from_:
        from_src = getattr(from_, "this", None)
        if from_src:
            left_scope = _resolve_table_to_scope_id(from_src, sg_scope)

    # Resolve ON clause
    on = join.args.get("on")
    condition_expression = on.sql(dialect=DIALECT) if on else None
    condition_columns = []
    if on:
        condition_columns = _resolve_column_refs_in_expr(on, sg_scope, result, schema)

    return ScopeJoin(
        join_type=str(join_type),
        left_scope=left_scope or "UNKNOWN",
        right_scope=right_scope or "UNKNOWN",
        alias_in_parent=right_alias,
        condition_expression=condition_expression,
        condition_columns=condition_columns,
    )


def _resolve_table_to_scope_id(table_node: exp.Expression, sg_scope: Scope) -> str | None:
    """Resolve a Table or Subquery node to its scope_id."""
    item = _source_item_from_ast_node(table_node, sg_scope)
    if item:
        alias, src = item
        if isinstance(src, Scope):
            return getattr(src, _SCOPE_ID_ATTR, None)
        if isinstance(src, exp.Table):
            return _qualified_table(src)
    if isinstance(table_node, exp.Table):
        return _qualified_table(table_node)
    return None


def _resolve_filter(
    clause: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[ScopeFilter]:
    """Resolve a WHERE or HAVING clause into ScopeFilter(s)."""
    columns = _resolve_column_refs_in_expr(clause, sg_scope, result, schema)
    return [ScopeFilter(expression=clause.sql(dialect=DIALECT), columns=columns)]


def _resolve_expr_list(
    exprs: list, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> List[SourceRef]:
    """Resolve a list of expressions (e.g. GROUP BY) to SourceRefs."""
    all_refs = []
    seen = set()
    for expr in exprs:
        for ref in _resolve_column_refs_in_expr(expr, sg_scope, result, schema):
            ref_key = _source_ref_binding_key(ref)
            if ref_key not in seen:
                seen.add(ref_key)
                all_refs.append(ref)
    return all_refs


def _transform_subkind(node: exp.Expression, transform: str) -> str | None:
    if transform == "CONDITIONAL" and isinstance(node, exp.If):
        return "if"
    if transform == "CONDITIONAL" and isinstance(node, exp.Case):
        return "case_when"
    if transform == "EXPRESSION" and isinstance(node, exp.Cast):
        return "cast"
    if transform == "EXPRESSION" and isinstance(node, exp.Anonymous):
        return "udf"
    return None


def _extract_case_branches(node: exp.Expression) -> List[dict]:
    """Extract WHEN/THEN branches from CASE/IF expressions."""
    branches = []
    if isinstance(node, exp.Case):
        # sqlglot Spark dialect: CASE WHEN is Case(ifs=[If(...), ...], default=...)
        ifs = node.args.get("ifs", [])
        for if_clause in ifs:
            if isinstance(if_clause, exp.If):
                branches.append({
                    "when_expr": if_clause.this.sql(dialect=DIALECT) if if_clause.this else "",
                    "then_value": if_clause.args.get("true").sql(dialect=DIALECT) if if_clause.args.get("true") else "",
                })
        # Also handle exp.When nodes (some dialects use these)
        for when in node.find_all(exp.When):
            branches.append({
                "when_expr": when.this.sql(dialect=DIALECT) if when.this else "",
                "then_value": when.expression.sql(dialect=DIALECT) if when.expression else "",
            })
        # Default/ELSE
        default = node.args.get("default")
        if default:
            branches.append({
                "when_expr": "ELSE",
                "then_value": default.sql(dialect=DIALECT),
            })
    elif isinstance(node, exp.If):
        branches.append({
            "when_expr": node.this.sql(dialect=DIALECT) if node.this else "",
            "then_value": node.args.get("true").sql(dialect=DIALECT) if node.args.get("true") else "",
        })
        false_expr = node.args.get("false")
        if false_expr:
            branches.append({
                "when_expr": "ELSE",
                "then_value": false_expr.sql(dialect=DIALECT),
            })
    return branches


def _extract_window_info(
    node: exp.Expression, sg_scope: Scope,
    result: ScopeLineageResult, schema: dict | None = None,
) -> dict:
    """Extract partition_by and order_by from a window function."""
    info = {}
    window = node if isinstance(node, exp.Window) else node.find(exp.Window)
    if window is None:
        return info

    # sqlglot stores partition as "partition_by" key (a list of expressions)
    partition = window.args.get("partition_by")
    if partition:
        partition_refs = []
        for p in partition:
            partition_refs.extend(_resolve_column_refs_in_expr(p, sg_scope, result, schema))
        info["partition_by"] = partition_refs

    order = window.args.get("order")
    if order:
        order_refs = []
        for item in (order.expressions if hasattr(order, "expressions") else []):
            # item.args.get("desc") is True for DESC, None/False for ASC
            # (do NOT use item.desc — that's a method that generates a DESC node)
            is_desc = isinstance(item, exp.Ordered) and item.args.get("desc") is True
            direction = "DESC" if is_desc else "ASC"
            # For Ordered, resolve the inner expression
            inner = item.this if isinstance(item, exp.Ordered) else item
            for ref in _resolve_column_refs_in_expr(inner, sg_scope, result, schema):
                order_refs.append({"scope": ref.scope, "column": ref.column, "direction": direction})
        info["order_by"] = order_refs

    return info


def _extract_agg_function(node: exp.Expression) -> str | None:
    """Extract the aggregate function name."""
    if isinstance(node, exp.AggFunc):
        # For nodes like ArrayUniqueAgg (COLLECT_SET), use the dialect SQL
        # which preserves the original function name
        sql = node.sql(dialect=DIALECT)
        # Extract function name: "COLLECT_SET(...)" -> "COLLECT_SET"
        paren = sql.find("(")
        if paren > 0:
            return sql[:paren].strip().upper()
        # Fallback
        if node.name:
            return node.name.upper()
        return node.sql_name()
    if isinstance(node, exp.Anonymous):
        func_name = node.name.upper() if hasattr(node, "name") else ""
        if func_name in _KNOWN_UDAFS:
            return func_name
        return func_name or None
    return None
