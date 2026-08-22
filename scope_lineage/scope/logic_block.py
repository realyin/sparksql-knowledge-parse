"""Logic Block — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the LogicBlockEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope
from .scope_types import (
    CONSTANT_SCOPE_ID,
    ScopeData,
    ScopeColumn,
    ScopeFieldUsage,
    ScopeInputEdge,
    ScopeLineageResult,
    ScopeLogicBlock,
    SYSTEM_SCOPE_ID,
    SourceRef,
)
from .column_ref_resolver import _resolve_column_refs_in_expr
from ._constants import DIALECT, PARSE_OPTS, _SCOPE_ID_ATTR
from .expression_text import _function_names
from .sequences import _extend_unique, _unique_ordered
from .source_refs import _is_cross_join_type, _is_internal_scope_id, _normalize_expression_resolution, _physical_source_fields_for_refs, _physical_source_fields_from_refs, _physical_source_ids_for_input, _source_ref_binding_key, _source_ref_to_dict, _source_refs_from_detail_fields, _source_type_from_id
from .column_expression_resolution import _expression_resolution_for_scope_column


def _populate_logic_blocks(
    scope_id: str,
    scope_data: ScopeData,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> None:
    blocks: list[ScopeLogicBlock] = []
    join_asts = sg_scope.expression.args.get("joins") or [] if isinstance(sg_scope.expression, exp.Select) else []

    for index, join in enumerate(scope_data.joins, start=1):
        raw = join.condition_expression
        logic_block_id = _logic_block_id(scope_id, "join", index)
        join_ast = join_asts[index - 1] if index - 1 < len(join_asts) else None
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=logic_block_id,
                logic_type="join",
                raw_expression=raw,
                normalized_expression=_normalize_expression(raw),
                fingerprint=_fingerprint("join", raw),
                fields=list(join.condition_columns),
                join_type=join.join_type,
                input_sources=_unique_ordered([join.left_scope, join.right_scope]),
                field_usage=_field_usage_from_refs(join.condition_columns, logic_block_id=logic_block_id),
                left_input=join.left_scope,
                right_input=join.right_scope,
                join_keys=list(join.condition_columns),
                join_relation_detail=_join_relation_detail(
                    join,
                    join_ast,
                    sg_scope,
                    result,
                    schema,
                    scope_data.input_edges,
                ),
            )
        )

    where_ast = sg_scope.expression.args.get("where") if isinstance(sg_scope.expression, exp.Select) else None
    for index, scope_filter in enumerate(scope_data.filters, start=1):
        logic_block_id = _logic_block_id(scope_id, "filter", index)
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=logic_block_id,
                logic_type="filter",
                raw_expression=scope_filter.expression,
                normalized_expression=_normalize_expression(scope_filter.expression),
                fingerprint=_fingerprint("filter", scope_filter.expression),
                subtype=_filter_subtype(scope_filter),
                fields=list(scope_filter.columns),
                input_sources=_source_ids_from_refs(scope_filter.columns),
                field_usage=_field_usage_from_refs(scope_filter.columns, logic_block_id=logic_block_id),
                filter_predicate_detail=_filter_predicate_detail(
                    scope_filter,
                    where_ast,
                    sg_scope,
                    result,
                    schema,
                    predicate_type="where",
                ),
            )
        )

    having_ast = sg_scope.expression.args.get("having") if isinstance(sg_scope.expression, exp.Select) else None
    for index, scope_filter in enumerate(scope_data.having, start=1):
        logic_block_id = _logic_block_id(scope_id, "having", index)
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=logic_block_id,
                logic_type="having",
                raw_expression=scope_filter.expression,
                normalized_expression=_normalize_expression(scope_filter.expression),
                fingerprint=_fingerprint("having", scope_filter.expression),
                fields=list(scope_filter.columns),
                input_sources=_source_ids_from_refs(scope_filter.columns),
                field_usage=_field_usage_from_refs(scope_filter.columns, logic_block_id=logic_block_id),
                filter_predicate_detail=_filter_predicate_detail(
                    scope_filter,
                    having_ast,
                    sg_scope,
                    result,
                    schema,
                    predicate_type="having",
                ),
            )
        )

    if scope_data.group_by:
        logic_block_id = _logic_block_id(scope_id, "group_by", 1)
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=logic_block_id,
                logic_type="group_by",
                fields=list(scope_data.group_by),
                input_sources=_source_ids_from_refs(scope_data.group_by),
                field_usage=_field_usage_from_refs(scope_data.group_by, logic_block_id=logic_block_id),
                aggregation_detail=_aggregation_detail_for_scope(scope_id, scope_data, sg_scope),
            )
        )

    if scope_data.distinct:
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=_logic_block_id(scope_id, "distinct", 1),
                logic_type="distinct",
            )
        )

    counters: dict[str, int] = {}
    for column in scope_data.columns:
        logic_type = _logic_type_for_column_transform(column.transform)
        if not logic_type:
            continue
        counters[logic_type] = counters.get(logic_type, 0) + 1
        logic_block_id = _logic_block_id(scope_id, logic_type, counters[logic_type])
        blocks.append(
            ScopeLogicBlock(
                logic_block_id=logic_block_id,
                logic_type=logic_type,
                raw_expression=column.expression,
                normalized_expression=_normalize_expression(column.expression),
                fingerprint=_fingerprint(logic_type, column.expression),
                subtype=column.transform_subkind,
                fields=list(column.sources),
                output_fields=[column.name] if column.name else [],
                input_sources=_source_ids_from_refs(column.sources),
                field_usage=_field_usage_from_refs(
                    column.sources,
                    output_field=column.name,
                    logic_block_id=logic_block_id,
                ),
                window_specification=(
                    _window_specification_for_column(scope_data, column)
                    if logic_type == "window"
                    else {}
                ),
            )
        )

    scope_data.logic_blocks = blocks


def _window_specification_for_column(scope_data: ScopeData, column) -> dict[str, object]:
    expression = column.expression or ""
    window_info = column.window or {}
    parsed_window = _parse_window_expression(expression)
    partition_exprs = []
    order_exprs = []
    if parsed_window is not None:
        partition_exprs = list(parsed_window.args.get("partition_by") or [])
        order = parsed_window.args.get("order")
        order_exprs = list(order.expressions if hasattr(order, "expressions") else [])

    partition_refs = [_window_ref_to_source_ref(ref) for ref in window_info.get("partition_by") or []]
    order_refs = [_window_ref_to_source_ref(ref) for ref in window_info.get("order_by") or []]

    return {
        "window_function": _window_function_name(expression),
        "output_field": column.name,
        "expression_sql": expression,
        "partition_by": _window_expression_items(scope_data, partition_exprs, partition_refs),
        "order_by": _window_expression_items(scope_data, order_exprs, order_refs, include_direction=True),
        "filter_after_window": [],
        "trace_status": "complete" if parsed_window is not None else "expression_only",
    }


def _parse_window_expression(expression: str) -> exp.Window | None:
    if not expression:
        return None
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except Exception:  # noqa: BLE001 - best-effort re-parse of a fragment; None means no window facts
        return None
    if isinstance(parsed, exp.Window):
        return parsed
    return parsed.find(exp.Window)


def _window_function_name(expression: str) -> str | None:
    functions = _function_names((expression or "").lower())
    return functions[0] if functions else None


def _window_ref_to_source_ref(ref) -> SourceRef | None:
    if isinstance(ref, SourceRef):
        return ref
    if isinstance(ref, dict) and ref.get("scope") and ref.get("column"):
        return SourceRef(
            scope=ref["scope"],
            column=ref["column"],
            candidates=[
                dict(item)
                for item in ref.get("candidates") or []
                if isinstance(item, dict) and item.get("scope") and item.get("column")
            ],
            qualifier=ref.get("qualifier"),
            binding_scope_id=ref.get("binding_scope_id"),
            input_ref_id=ref.get("input_ref_id"),
        )
    return None


def _window_expression_items(
    scope_data: ScopeData,
    expressions: list[exp.Expression],
    refs: list[SourceRef | None],
    *,
    include_direction: bool = False,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    ref_offset = 0
    for index, expression in enumerate(expressions):
        inner = expression.this if isinstance(expression, exp.Ordered) else expression
        ref_count = max(1, len(list(inner.find_all(exp.Column))))
        item_refs = [ref for ref in refs[ref_offset: ref_offset + ref_count] if ref is not None]
        ref_offset += ref_count
        item: dict[str, object] = {
            "expression_sql": inner.sql(dialect=DIALECT),
            "source_fields": [_source_ref_to_dict(ref) for ref in item_refs],
        }
        expression_fact = _expression_resolution_for_refs(
            scope_data,
            item["expression_sql"],
            item_refs,
            transform="EXPRESSION",
        )
        item["expression_resolution"] = _normalize_expression_resolution(
            expression_fact.get("expression_resolution") or {},
            expression=expression_fact.get("expanded_expression") or item["expression_sql"],
        )
        if include_direction:
            if isinstance(expression, exp.Ordered):
                item["direction"] = "DESC" if expression.args.get("desc") is True else "ASC"
            else:
                item["direction"] = "ASC"
        items.append(item)
    if items:
        return items
    return [
        {
            "expression_sql": ref.column,
            "source_fields": [_source_ref_to_dict(ref)],
            "expression_resolution": _normalize_expression_resolution(
                _expression_resolution_for_refs(
                    ScopeData(kind="root"),
                    ref.column,
                    [ref],
                    transform="EXPRESSION",
                ).get("expression_resolution") or {},
                expression=ref.column,
            ),
            **({"direction": "ASC"} if include_direction else {}),
        }
        for ref in refs
        if ref is not None
    ]


def _aggregation_detail_for_scope(
    scope_id: str,
    scope_data: ScopeData,
    sg_scope: Scope,
) -> dict[str, object]:
    group_expressions: list[exp.Expression] = []
    if isinstance(sg_scope.expression, exp.Select):
        group = sg_scope.expression.args.get("group")
        if group is not None:
            group_expressions = list(group.expressions if hasattr(group, "expressions") else [group])

    group_by_items = _aggregation_group_by_items(scope_data, group_expressions)
    aggregate_items = [
        _aggregation_item_for_column(scope_data, column)
        for column in scope_data.columns
        if column.transform == "AGGREGATE"
    ]
    having_items = [
        {
            "logic_block_id": _logic_block_id(scope_id, "having", index),
            "expression": having.expression,
            "fields": [_source_ref_to_dict(ref) for ref in having.columns],
        }
        for index, having in enumerate(scope_data.having, start=1)
    ]
    return {
        "group_by_items": group_by_items,
        "aggregate_items": aggregate_items,
        "having": having_items,
        "trace_status": "complete" if group_by_items or aggregate_items else "empty",
    }


def _aggregation_group_by_items(scope_data: ScopeData, group_expressions: list[exp.Expression]) -> list[dict[str, object]]:
    items = _window_expression_items(scope_data, group_expressions, list(scope_data.group_by))
    for index, item in enumerate(items):
        ordinal_fact: dict[str, object] = {}
        expression_sql = str(item.get("expression_sql") or "")
        refs = _source_refs_from_detail_fields(item.get("source_fields") or [])
        if index < len(group_expressions):
            ordinal = _group_by_ordinal_position(group_expressions[index])
            if ordinal is not None:
                expression_sql, refs, ordinal_fact = _group_by_ordinal_expression_fact(
                    scope_data,
                    group_expressions[index],
                    ordinal,
                )
                item["expression_sql"] = expression_sql
                item["source_fields"] = [_source_ref_to_dict(ref) for ref in refs]
                item.update(ordinal_fact)
        expression_fact = _expression_resolution_for_refs(
            scope_data,
            expression_sql,
            refs,
            transform="EXPRESSION",
        )
        item["expanded_expression"] = expression_fact.get("expanded_expression")
        item["expression_resolution"] = expression_fact.get("expression_resolution") or {}
        item["physical_source_fields"] = (item["expression_resolution"] or {}).get("physical_source_fields") or []
        item["trace_status"] = _aggregation_trace_status(refs, item["expression_resolution"])
    return items


def _group_by_ordinal_position(expression: exp.Expression) -> int | None:
    inner = expression.this if isinstance(expression, exp.Ordered) else expression
    if not isinstance(inner, exp.Literal) or not inner.is_int:
        return None
    try:
        return int(str(inner.this))
    except ValueError:
        return None


def _group_by_ordinal_expression_fact(
    scope_data: ScopeData,
    expression: exp.Expression,
    ordinal: int,
) -> tuple[str, list[SourceRef], dict[str, object]]:
    original_expression_sql = expression.sql(dialect=DIALECT)
    if 1 <= ordinal <= len(scope_data.columns):
        column = scope_data.columns[ordinal - 1]
        expression_sql = column.expression or column.name
        return (
            expression_sql,
            list(column.sources),
            {
                "ordinal_position": ordinal,
                "resolved_from_select_position": ordinal,
                "original_expression_sql": original_expression_sql,
            },
        )
    return (
        original_expression_sql,
        [],
        {
            "ordinal_position": ordinal,
            "ordinal_resolution_status": "out_of_range",
            "original_expression_sql": original_expression_sql,
        },
    )


def _aggregation_item_for_column(scope_data: ScopeData, column: ScopeColumn) -> dict[str, object]:
    expression_fact = _expression_resolution_for_scope_column(scope_data, column)
    expression_resolution = expression_fact.get("expression_resolution") or {}
    return {
        "output_field": column.name,
        "aggregate_function": _aggregate_function_name(column),
        "expression_sql": column.expression,
        "expanded_expression": expression_fact.get("expanded_expression"),
        "argument_expression_sql": _aggregate_argument_expression_sql(column.expression),
        "physical_source_fields": expression_resolution.get("physical_source_fields") or [],
        "distinct": _aggregate_distinct(column.expression),
        "filter_expression": None,
        "trace_status": _aggregation_trace_status(column.sources, expression_resolution),
        "expression_resolution": expression_resolution,
        "source_fields": [_source_ref_to_dict(ref) for ref in column.sources],
    }


def _expression_resolution_for_refs(
    scope_data: ScopeData,
    expression_sql: str,
    refs: list[SourceRef],
    *,
    transform: str,
) -> dict[str, object]:
    return _expression_resolution_for_scope_column(
        scope_data,
        ScopeColumn(
            name="",
            transform=transform,
            expression=expression_sql,
            sources=refs,
        ),
    )


def _aggregation_trace_status(refs: list[SourceRef], expression_resolution: dict[str, object]) -> str:
    missing_reasons = set(expression_resolution.get("missing_reasons") or [])
    if any(str(reason).startswith("alias_not_bound_to_input_source:") for reason in missing_reasons):
        return "partial"
    if any(ref.scope not in {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID} and _is_internal_scope_id(ref.scope) for ref in refs):
        return "partial"
    return "complete"


def _aggregate_function_name(column) -> str | None:
    if column.agg_function:
        return str(column.agg_function).lower()
    functions = _function_names((column.expression or "").lower())
    return functions[0] if functions else None


def _aggregate_argument_expression_sql(expression_sql: str | None) -> str | None:
    aggregate = _parse_first_aggregate_expression(expression_sql)
    if aggregate is None:
        return None
    argument = aggregate.this
    if isinstance(argument, exp.Distinct):
        expressions = list(argument.expressions or [])
        if len(expressions) == 1:
            return expressions[0].sql(dialect=DIALECT)
    return argument.sql(dialect=DIALECT) if argument is not None else None


def _aggregate_distinct(expression_sql: str | None) -> bool:
    aggregate = _parse_first_aggregate_expression(expression_sql)
    if aggregate is None:
        return False
    return isinstance(aggregate.this, exp.Distinct)


def _parse_first_aggregate_expression(expression_sql: str | None) -> exp.AggFunc | None:
    if not expression_sql:
        return None
    try:
        parsed = sqlglot.parse_one(expression_sql, read=DIALECT)
    except sqlglot.errors.SqlglotError:
        return None
    if isinstance(parsed, exp.AggFunc):
        return parsed
    return next(parsed.find_all(exp.AggFunc), None)


def _join_relation_detail(
    join,
    join_ast: exp.Join | None,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None,
    input_edges: list[ScopeInputEdge],
) -> dict[str, object]:
    alias_by_source = {
        edge.source_id: edge.alias
        for edge in input_edges
        if edge.source_id and edge.alias
    }
    left_alias = alias_by_source.get(join.left_scope)
    right_alias = alias_by_source.get(join.right_scope) or join.alias_in_parent
    if join.left_scope == join.right_scope:
        # Self-join: one source_id, two sides — the dict above collapsed both onto the
        # later alias (left_alias == right_alias, JOINALIAS-001). Edge order still holds
        # each side's alias: the FROM-positioned edge entered first, the joined side after.
        aliases = [
            edge.alias
            for edge in input_edges
            if edge.source_id == join.left_scope and edge.alias
        ]
        if len(aliases) >= 2:
            right_alias = join.alias_in_parent or aliases[-1]
            left_candidates = [alias for alias in aliases if alias != right_alias]
            left_alias = left_candidates[0] if left_candidates else aliases[0]
    detail: dict[str, object] = {
        "join_type": join.join_type,
        "left_input": join.left_scope,
        "right_input": join.right_scope,
        "left_alias": left_alias,
        "right_alias": right_alias,
        "condition_expression": join.condition_expression,
        "condition_fields": [_source_ref_to_dict(ref) for ref in join.condition_columns],
        "join_key_pairs": [],
        "condition_filters": [],
        "missing_reasons": [],
    }
    logic_scope_data = _scope_data_for_logic_resolution(result, input_edges)
    condition_fact = _expression_resolution_for_refs(
        logic_scope_data,
        join.condition_expression,
        list(join.condition_columns),
        transform="EXPRESSION",
    )
    detail["condition_expression_resolution"] = _normalize_expression_resolution(
        condition_fact.get("expression_resolution") or {},
        expression=condition_fact.get("expanded_expression") or join.condition_expression,
    )
    on_expr = join_ast.args.get("on") if join_ast is not None else None
    root_expr = on_expr
    if root_expr is not None:
        detail["join_condition_source"] = "on"
    elif _is_cross_join_type(join.join_type) and isinstance(sg_scope.expression, exp.Select):
        where_expr = sg_scope.expression.args.get("where")
        root_expr = getattr(where_expr, "this", None)
        if root_expr is not None:
            detail["condition_expression"] = root_expr.sql(dialect=DIALECT)
            detail["join_condition_source"] = "where_filter"
    if root_expr is None:
        detail["trace_status"] = "partial"
        detail["missing_reasons"] = ["missing_join_condition"]
        return detail

    for conjunct in _split_conjuncts(root_expr):
        refs = _resolve_column_refs_in_expr(conjunct, sg_scope, result, schema)
        key_pair = _join_key_pair_from_expr(
            conjunct,
            refs,
            join.left_scope,
            join.right_scope,
            left_alias=left_alias,
            right_alias=right_alias,
        )
        if key_pair is not None:
            detail["join_key_pairs"].append(key_pair)
        elif refs:
            detail["condition_filters"].append(
                {
                    "expression": conjunct.sql(dialect=DIALECT),
                    "fields": [_source_ref_to_dict(ref) for ref in refs],
                    "physical_fields": _physical_source_fields_from_refs(refs),
                    "expression_resolution": _normalize_expression_resolution(
                        _expression_resolution_for_refs(
                            logic_scope_data,
                            conjunct.sql(dialect=DIALECT),
                            refs,
                            transform="EXPRESSION",
                        ).get("expression_resolution") or {},
                        expression=conjunct.sql(dialect=DIALECT),
                    ),
                }
            )
    if any(
        not pair.get("left_fields") or not pair.get("right_fields")
        for pair in detail["join_key_pairs"]
    ):
        detail["trace_status"] = "partial"
        detail["missing_reasons"].append("join_key_physical_fields_unresolved")
    elif detail["join_key_pairs"] or _is_cross_join_type(join.join_type):
        detail["trace_status"] = "complete"
    else:
        detail["trace_status"] = "partial"
        detail["missing_reasons"].append("missing_join_key_pairs")
    return detail


def _scope_data_for_logic_resolution(
    result: ScopeLineageResult,
    input_edges: list[ScopeInputEdge],
) -> ScopeData:
    refs: list[dict[str, object]] = []
    physical_source_memo: dict[str, list[str]] = {}
    for index, edge in enumerate(input_edges, start=1):
        physical_source_ids = _physical_source_ids_for_input(
            result,
            edge.source_id,
            memo=physical_source_memo,
        )
        refs.append(
            {
                "input_ref_id": f"logic-input:{index:03d}",
                "source_id": edge.source_id,
                "source_type": edge.source_type,
                "alias": edge.alias,
                "physical_source_id": physical_source_ids[0] if len(physical_source_ids) == 1 else None,
                "physical_source_ids": physical_source_ids,
            }
        )
    return ScopeData(kind="root", input_source_refs=refs)


def _filter_predicate_detail(
    scope_filter,
    filter_ast: exp.Expression | None,
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None,
    *,
    predicate_type: str,
) -> dict[str, object]:
    root_expr = getattr(filter_ast, "this", None) if isinstance(filter_ast, (exp.Where, exp.Having)) else filter_ast
    conjuncts = []
    ordered_refs: list[SourceRef] = []
    seen_refs: set[tuple[str, str, str, str, str]] = set()
    all_subquery_dependencies: list[dict[str, object]] = []
    seen_subquery_dependencies: set[tuple[str, str]] = set()
    for conjunct in _split_conjuncts(root_expr) if root_expr is not None else []:
        refs = _resolve_column_refs_in_expr(conjunct, sg_scope, result, schema)
        for ref in refs:
            ref_key = _source_ref_binding_key(ref)
            if ref_key not in seen_refs:
                seen_refs.add(ref_key)
                ordered_refs.append(ref)
        subquery_dependencies = _subquery_predicate_dependencies(conjunct, sg_scope, result)
        for dependency in subquery_dependencies:
            dep_key = (
                str(dependency.get("subquery_scope_id") or ""),
                str(dependency.get("predicate_expression") or ""),
            )
            if dep_key not in seen_subquery_dependencies:
                seen_subquery_dependencies.add(dep_key)
                all_subquery_dependencies.append(dependency)
        conjuncts.append(
            {
                "expression": conjunct.sql(dialect=DIALECT),
                "fields": [_source_ref_to_dict(ref) for ref in refs],
                **({"subquery_dependencies": subquery_dependencies} if subquery_dependencies else {}),
                "expression_resolution": _normalize_expression_resolution(
                    _expression_resolution_for_refs(
                        ScopeData(kind="root"),
                        conjunct.sql(dialect=DIALECT),
                        refs,
                        transform="EXPRESSION",
                    ).get("expression_resolution") or {},
                    expression=conjunct.sql(dialect=DIALECT),
                ),
            }
        )
    if not ordered_refs:
        ordered_refs = list(scope_filter.columns)
    fields = [_source_ref_to_dict(ref) for ref in ordered_refs]
    field_names = {ref.column.lower() for ref in ordered_refs}
    has_partition_predicate = bool(field_names & {"dt", "ds", "pt", "bizdate", "biz_date"})
    predicate_fact = _expression_resolution_for_refs(
        ScopeData(kind="root"),
        scope_filter.expression,
        ordered_refs,
        transform="EXPRESSION",
    )
    return {
        "predicate_type": predicate_type,
        "expression": scope_filter.expression,
        "fields": fields,
        "conjuncts": conjuncts,
        "expression_resolution": _normalize_expression_resolution(
            predicate_fact.get("expression_resolution") or {},
            expression=predicate_fact.get("expanded_expression") or scope_filter.expression,
        ),
        "subquery_dependencies": all_subquery_dependencies,
        "is_partition_filter": bool(field_names) and field_names <= {"dt", "ds", "pt", "bizdate", "biz_date"},
        "has_partition_predicate": has_partition_predicate,
    }


def _subquery_predicate_dependencies(
    expr: exp.Expression,
    sg_scope: Scope,
    result: ScopeLineageResult,
) -> list[dict[str, object]]:
    predicate_sql = expr.sql(dialect=DIALECT)
    dependencies: list[dict[str, object]] = []
    for sub_scope in getattr(sg_scope, "subquery_scopes", []) or []:
        subquery_scope_id = getattr(sub_scope, _SCOPE_ID_ATTR, None)
        if not subquery_scope_id:
            continue
        try:
            subquery_sql = sub_scope.expression.sql(dialect=DIALECT)
        except Exception:  # noqa: BLE001 - rendering a subquery is best-effort evidence, not a required step
            subquery_sql = ""
        if subquery_sql and subquery_sql not in predicate_sql:
            continue
        scope_data = result.scopes.get(subquery_scope_id)
        if scope_data is None:
            continue
        refs = _source_refs_from_logic_blocks(scope_data.logic_blocks)
        physical_fields = _physical_source_fields_for_refs(result, refs)
        dependencies.append(
            {
                "subquery_scope_id": subquery_scope_id,
                "predicate_expression": predicate_sql,
                "subquery_expression": subquery_sql,
                "fields": [_source_ref_to_dict(ref) for ref in refs],
                "physical_source_fields": physical_fields,
                "input_source_refs": scope_data.input_source_refs,
                "trace_status": "complete" if physical_fields else "partial",
                "missing_reasons": [] if physical_fields else ["subquery_physical_fields_unresolved"],
            }
        )
    return dependencies


def _source_refs_from_logic_blocks(blocks: list[ScopeLogicBlock]) -> list[SourceRef]:
    refs: list[SourceRef] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for block in blocks:
        for ref in block.fields:
            key = _source_ref_binding_key(ref)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


def _split_conjuncts(expr: exp.Expression) -> list[exp.Expression]:
    if isinstance(expr, exp.And):
        return _split_conjuncts(expr.left) + _split_conjuncts(expr.right)
    return [expr]


def _join_key_pair_from_expr(
    expr: exp.Expression,
    refs: list[SourceRef],
    left_input: str,
    right_input: str,
    *,
    left_alias: str | None = None,
    right_alias: str | None = None,
) -> dict[str, object] | None:
    if not isinstance(expr, exp.EQ) or len(refs) != 2:
        return None
    left_ref = refs[0]
    right_ref = refs[1]
    if left_ref.scope == right_ref.scope:
        # Same scope on both sides is a self-join key, not a non-key, when the two refs
        # carry the two sides' distinct qualifiers. Refusing it sent every self-join
        # equality into condition_filters, where the expanded rendering collapsed
        # `a.batch_id = b.batch_id` into a tautology on one table (JOINALIAS-001).
        # Orientation comes from the qualifiers; anything less than an exact two-sided
        # match stays refused rather than guessed.
        left_qualifier = left_ref.qualifier
        right_qualifier = right_ref.qualifier
        if (
            not left_alias
            or not right_alias
            or {left_qualifier, right_qualifier} != {left_alias, right_alias}
        ):
            return None
        if left_qualifier == right_alias:
            left_ref, right_ref = right_ref, left_ref
        return _join_key_pair_dict(expr, left_ref, right_ref)
    if right_ref.scope == right_input:
        pass
    elif left_ref.scope == right_input:
        left_ref, right_ref = right_ref, left_ref
    elif left_input in {left_ref.scope, right_ref.scope} and right_input in {left_ref.scope, right_ref.scope}:
        if left_ref.scope != left_input:
            left_ref, right_ref = right_ref, left_ref
    else:
        return None
    return _join_key_pair_dict(expr, left_ref, right_ref)


def _join_key_pair_dict(
    expr: exp.Expression,
    left_ref: SourceRef,
    right_ref: SourceRef,
) -> dict[str, object]:
    return {
        "left": _source_ref_to_dict(left_ref),
        "right": _source_ref_to_dict(right_ref),
        "left_fields": _physical_source_fields_from_refs([left_ref]),
        "right_fields": _physical_source_fields_from_refs([right_ref]),
        "operator": "=",
        "expression": expr.sql(dialect=DIALECT),
    }


def _source_ids_from_refs(refs: list[SourceRef]) -> list[str]:
    return _unique_ordered([ref.scope for ref in refs])


def _field_usage_from_refs(
    refs: list[SourceRef],
    *,
    logic_block_id: str | None = None,
    output_field: str | None = None,
) -> list[ScopeFieldUsage]:
    grouped: dict[str, ScopeFieldUsage] = {}
    for ref in refs:
        usage = grouped.setdefault(
            ref.scope,
            ScopeFieldUsage(
                source_id=ref.scope,
                source_type=_source_type_from_id(ref.scope),
            ),
        )
        _extend_unique(usage.used_fields, [ref.column])
        if logic_block_id:
            _extend_unique(usage.used_by_logic_blocks, [logic_block_id])
        if output_field:
            _extend_unique(usage.used_by_output_fields, [output_field])
    return list(grouped.values())


def _logic_type_for_column_transform(transform: str) -> str | None:
    return {
        "CONDITIONAL": "case_when",
        "WINDOW": "window",
        "AGGREGATE": "aggregate",
        "UNION": "union",
    }.get(transform)


def _logic_block_id(scope_id: str, logic_type: str, index: int) -> str:
    return f"logic:{scope_id}:{logic_type}:{index:03d}"


def _normalize_expression(expression: str | None) -> str | None:
    if expression is None:
        return None
    return " ".join(expression.strip().lower().split())


def _fingerprint(logic_type: str, expression: str | None) -> str | None:
    normalized = _normalize_expression(expression)
    return f"{logic_type}:{normalized}" if normalized else None


def _filter_subtype(scope_filter) -> str | None:
    normalized = _normalize_expression(scope_filter.expression) or ""
    if any(ref.column.lower() in {"is_deleted", "is_delete", "deleted", "delete_flag"} for ref in scope_filter.columns):
        return "delete_filter"
    if "test" in normalized:
        return "test_data_filter"
    return None
