"""Per-scope column resolver: resolve every column reference to SourceRef(scope_id, column_name).

Works on the scope tree built by scope_builder.py. Populates each ScopeData's
columns, joins, filters, group_by, having, order_by, and depends_on.
"""

from __future__ import annotations



from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from .parser import (
    _normalize_table_name,
    _qualified_table,
)
from .scope_types import (
    AMBIGUOUS_SCOPE_ID,
    CONSTANT_SCOPE_ID,
    SYSTEM_SCOPE_ID,
    SourceRef,
    ScopeColumn,
    ScopeData,
    ScopeGraphEdge,
    ScopeLineageResult,
    DiagnosticWarning,
)
from ._constants import DIALECT, _SCOPE_ID_ATTR
from .source_refs import _constant_sources
from .sqlglot_walk import _classify_extended, _inside_nested_set_op, _source_free_leaf_sources
from .column_ref_resolver import _ambiguous_ref, _materialized_star_column_state, _resolve_column_refs_in_expr  # noqa: F401
from .sqlglot_walk import _REGEX_COLUMN_METACHARACTERS, _compiled_column_pattern
from .select_scope import _resolve_select_scope, _star_modifiers  # noqa: F401
from .target_field_binding import apply_target_field_binding


# Attribute name on sqlglot Scope objects holding the scope_id

# Known Hive/Spark aggregate functions that sqlglot parses as exp.Anonymous


def _is_dependency_scope(scope_id: str | None) -> bool:
    return bool(scope_id and scope_id not in {"UNKNOWN", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID})


def resolve_all(
    result: ScopeLineageResult,
    all_scopes: list,
    schema: dict | None = None,
    target_metadata=None,
    explicit_target_columns: list[str] | None = None,
    insert_by_name: bool = False,
    *,
    merge_node: exp.Merge | None = None,
    merge_using_scope: Scope | None = None,
    regex_columns_enabled: bool = True,
    merge_target_columns: list[str] | None = None,
) -> None:
    """Resolve columns for all scopes in the result.

    Walks the sqlglot scope tree, resolves projections/joins/filters/etc.,
    populates depends_on, and builds scope_graph edges.

    all_scopes is the full list from traverse_scope(qualified_expr). MERGE receives
    its statement AST and USING scope explicitly because SQLGlot does not expose the
    DML itself as a root query scope.
    """
    # Step 1: Resolve all Select-based scopes (root, cte, subquery, union_branch)
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None or scope_id not in result.scopes:
            continue

        scope_data = result.scopes[scope_id]

        if scope_data.kind in ("root", "cte", "subquery", "union_branch"):
            if isinstance(sg_scope.expression, exp.Values):
                _resolve_values_scope(sg_scope, scope_id, scope_data, result)
            elif isinstance(sg_scope.expression, exp.Select):
                _resolve_select_scope(sg_scope, scope_id, scope_data, result, schema)
            elif isinstance(sg_scope.expression, exp.Lateral):
                _resolve_lateral_scope(sg_scope, scope_id, scope_data, result, schema)

    # Step 2: Resolve synthetic UNION scopes in bottom-up order
    # (nested unions must be resolved before their parent union)
    # Iterate until all union scopes have columns (handles arbitrary nesting depth)
    _resolve_union_scopes_bottom_up(result)

    # Step 3: Handle scopes with Union expression (e.g. ROOT, CTE containing UNION)
    # Their columns are a passthrough from the corresponding union scope
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None or scope_id not in result.scopes:
            continue
        scope_data = result.scopes[scope_id]
        if isinstance(sg_scope.expression, exp.Union) and not scope_data.columns:
            _resolve_scope_union_passthrough(scope_id, scope_data, result)

    # Step 3b: Expand wildcard (*) columns into concrete columns where upstream is known.
    # Iterates until stable so that chains like  subq:a.* → subq:aa.* → union:aa.[cols]
    # are fully unrolled.
    _expand_regex_column_selection(
        result, all_scopes, schema, regex_columns_enabled=regex_columns_enabled
    )
    _expand_star_columns(result)
    _materialize_referenced_star_columns(result)
    _refresh_union_scopes_after_star_expansion(result, all_scopes)
    _reconcile_ambiguous_column_sources_after_star_expansion(result, schema)
    _apply_star_except_lists(result, all_scopes)
    apply_target_field_binding(
        result,
        target_metadata=target_metadata,
        explicit_target_columns=explicit_target_columns,
        insert_by_name=insert_by_name,
    )

    # Step 4: Resolve MERGE columns (special handling)
    if result.stmt_kind == "MERGE":
        if merge_node is None:
            raise ValueError(
                "MERGE statement reached column resolution without a Merge AST node"
            )
        _resolve_merge_columns(
            merge_node,
            merge_using_scope,
            all_scopes,
            result,
            schema,
            target_columns=merge_target_columns,
        )
        _materialize_referenced_star_columns(result)

    # Step 5: Populate depends_on and build scope_graph edges
    _build_depends_on_and_graph(result)


def _reconcile_ambiguous_column_sources_after_star_expansion(
    result: ScopeLineageResult,
    schema: dict | None,
) -> None:
    """Remove false ambiguity once wildcard scopes have concrete output columns.

    Initial column resolution necessarily runs before the iterative ``SELECT *`` expansion.
    A nested wildcard source can therefore look capable of supplying any unqualified field
    even though its final materialized outputs prove the field absent. Keep real ambiguities,
    but collapse a candidate set when expansion leaves exactly one possible source.
    """
    resolved_warning_keys: set[tuple[str, str]] = set()
    for scope_id, scope_data in result.scopes.items():
        for column in scope_data.columns:
            rewritten_sources: list[SourceRef] = []
            for source in column.sources:
                if source.scope != AMBIGUOUS_SCOPE_ID or not source.candidates:
                    rewritten_sources.append(source)
                    continue
                viable_candidates = [
                    candidate
                    for candidate in source.candidates
                    if _candidate_column_state_after_star_expansion(
                        candidate, result, schema
                    )
                    != "absent"
                ]
                if len(viable_candidates) != 1:
                    rewritten_sources.append(source)
                    continue
                candidate = viable_candidates[0]
                rewritten_sources.append(
                    SourceRef(
                        scope=str(candidate.get("scope") or "UNKNOWN"),
                        column=str(candidate.get("column") or source.column),
                        qualifier=(
                            str(candidate["qualifier"])
                            if candidate.get("qualifier")
                            else None
                        ),
                        binding_scope_id=(
                            str(candidate["binding_scope_id"])
                            if candidate.get("binding_scope_id")
                            else None
                        ),
                        input_ref_id=(
                            str(candidate["input_ref_id"])
                            if candidate.get("input_ref_id")
                            else None
                        ),
                    )
                )
                resolved_warning_keys.add((scope_id, str(source.column or "")))
            column.sources = rewritten_sources

    if resolved_warning_keys:
        result.diagnostics.warnings = [
            warning
            for warning in result.diagnostics.warnings
            if not (
                warning.type == "ambiguous_unqualified"
                and any(
                    warning.scope == scope_id
                    and f"'{column_name}'" in warning.msg
                    for scope_id, column_name in resolved_warning_keys
                )
            )
        ]


def _candidate_column_state_after_star_expansion(
    candidate: dict[str, str],
    result: ScopeLineageResult,
    schema: dict | None,
) -> str:
    scope_id = str(candidate.get("scope") or "")
    column = str(candidate.get("column") or "")
    if scope_id in result.scopes:
        return _materialized_star_column_state(scope_id, column, result, schema)
    return "unknown"


def _resolve_values_scope(
    sg_scope: Scope, scope_id: str, scope_data: ScopeData,
    result: ScopeLineageResult,
) -> None:
    """Resolve Spark VALUES (...) AS alias(col1, col2, ...) into named columns."""
    values = sg_scope.expression
    alias = values.args.get("alias") if isinstance(values, exp.Values) else None
    alias_columns = list(alias.columns or []) if alias is not None else []
    rows = list(values.expressions or []) if isinstance(values, exp.Values) else []
    first_row = rows[0].expressions if rows and hasattr(rows[0], "expressions") else []

    if alias_columns:
        names = [c.name if hasattr(c, "name") else str(c) for c in alias_columns]
    else:
        names = [f"_col_{i}" for i in range(len(first_row))]

    for i, name in enumerate(names):
        expr = first_row[i] if i < len(first_row) else None
        expression = expr.sql(dialect=DIALECT) if expr is not None else ""
        transform = _classify_extended(expr) if expr is not None else "CONSTANT"
        scope_data.columns.append(ScopeColumn(
            name=name,
            transform=transform,
            expression=expression,
            sources=_source_free_leaf_sources(expr, expression) if expr is not None else _constant_sources(expression),
        ))


def _resolve_lateral_scope(
    sg_scope: Scope,
    scope_id: str,
    scope_data: ScopeData,
    result: ScopeLineageResult,
    schema: dict | None = None,
) -> None:
    """Resolve LATERAL VIEW generator output columns."""
    lateral = sg_scope.expression
    alias = lateral.args.get("alias")
    alias_columns = list(alias.columns or []) if alias is not None else []
    names = [c.name if hasattr(c, "name") else str(c) for c in alias_columns]

    inner = lateral.this
    if not names:
        names = _infer_lateral_output_names(inner)
    if not names:
        return

    if inner is not None:
        scope_data.lateral_views.append({
            "alias": _lateral_alias_name(alias, scope_id),
            "function": _lateral_function_name(inner),
            "expression": _compact_sql(inner),
            "output_columns": list(names),
        })

    # A LATERAL VIEW generator is evaluated against the FROM/JOIN namespace of its
    # containing SELECT. Resolving an originally unqualified input against the synthetic
    # UDTF scope exposes every visible CTE and can manufacture ambiguity with unrelated
    # CTEs; the parent SELECT carries the actual selected sources.
    resolution_scope = sg_scope.parent or sg_scope
    sources = _resolve_column_refs_in_expr(inner, resolution_scope, result, schema)
    if not sources and inner is not None:
        # A generator over a literal -- EXPLODE(ARRAY(...)), INLINE(ARRAY(STRUCT(...))) --
        # has no column references, so the resolver above returns nothing. Minting the
        # column with an empty source list makes it a dead end that reports itself as
        # fully traced: end_to_end_lineage renders source_kind 'unresolved' while
        # trace_complete stays true, the one pair LINEAGE-002 says must never coincide.
        # The VALUES / table-valued-function path above routes source-free leaves through
        # _source_free_leaf_sources for exactly this reason; this is its missing twin.
        sources = _source_free_leaf_sources(inner, inner.sql(dialect=DIALECT))
    for name in names:
        scope_data.columns.append(ScopeColumn(
            name=name,
            transform="EXPRESSION",
            expression=inner.sql(dialect=DIALECT) if inner is not None else "",
            sources=list(sources),
        ))


def _lateral_alias_name(alias: exp.Expression | None, scope_id: str) -> str:
    if alias is not None and alias.this is not None:
        return alias.this.name if hasattr(alias.this, "name") else str(alias.this)
    if ":" in scope_id:
        return scope_id.split(":", 1)[1]
    return scope_id


def _compact_sql(expression: exp.Expression) -> str:
    return expression.sql(dialect=DIALECT).replace("`", "")


def _lateral_function_name(inner: exp.Expression) -> str:
    if isinstance(inner, exp.Posexplode):
        return "POSEXPLODE"
    if isinstance(inner, exp.Explode):
        return "EXPLODE"
    if isinstance(inner, exp.Inline):
        return "INLINE"
    key = getattr(inner, "key", "") or inner.__class__.__name__
    return str(key).upper()


def _infer_lateral_output_names(inner: exp.Expression | None) -> list[str]:
    if isinstance(inner, exp.Posexplode):
        return ["pos", "col"]
    if isinstance(inner, exp.Explode):
        return ["col"]
    if isinstance(inner, exp.Inline):
        names = _field_names_from_from_json_schema(inner)
        if names:
            return names
    return []


def _field_names_from_from_json_schema(expr: exp.Expression) -> list[str]:
    for func in expr.find_all(exp.Anonymous):
        if str(func.this).lower() != "from_json":
            continue
        args = list(func.expressions or [])
        if len(args) < 2 or not isinstance(args[1], exp.Literal):
            continue
        names = _extract_struct_field_names(args[1].this or "")
        if names:
            return names
    return []


def _extract_struct_field_names(schema_text: str) -> list[str]:
    marker = "array<struct<"
    lower = schema_text.lower()
    start = lower.rfind(marker)
    if start < 0:
        start = lower.find("struct<")
        if start < 0:
            return []
        start += len("struct<")
    else:
        start += len(marker)

    depth = 0
    fields = []
    token = []
    for ch in schema_text[start:]:
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            fields.append("".join(token).strip())
            token = []
        else:
            token.append(ch)
    if token:
        fields.append("".join(token).strip())

    names = []
    for field in fields:
        name = field.split(":", 1)[0].strip().strip("`")
        if name:
            names.append(name.lower())
    return names


def _resolve_union_scope(
    union_scope_id: str, scope_data: ScopeData, result: ScopeLineageResult,
) -> None:
    """Resolve columns for a synthetic UNION scope from its branch scopes."""
    branch_ids = scope_data.branches or []
    if not branch_ids:
        return

    branch_cols = [result.scopes.get(bid) for bid in branch_ids]
    branch_cols = [sd.columns for sd in branch_cols if sd is not None]
    if not branch_cols:
        return

    n_cols = len(branch_cols[0]) if branch_cols else 0
    used_names: set[str] = set()
    for i in range(n_cols):
        # Collect sources and branches from each branch's corresponding column
        sources = []
        branches = []
        # Use the first branch's column name; for positional alignment in UNION,
        # later branches with unnamed columns (_col_N) should adopt the first branch's name
        first_name = branch_cols[0][i].name if i < len(branch_cols[0]) else f"col_{i}"
        col_name = _union_output_name_for_position(first_name, branch_cols, i, used_names)

        for j, (bid, cols) in enumerate(zip(branch_ids, branch_cols)):
            if i < len(cols):
                branch_col = cols[i]
                # Rename unnamed or duplicate positional names to match the resolved
                # UNION output name. This preserves positional columns when one branch
                # repeats a name (for example SELECT mobile_no, mobile_no).
                if (
                    branch_col.name.startswith("_col_")
                    or branch_col.name in used_names
                ) and not col_name.startswith("_col_"):
                    branch_col.name = col_name
                sources.append(SourceRef(scope=bid, column=branch_col.name))
                branches.append({"branch": bid, "from_column": branch_col.name})

        scope_data.columns.append(ScopeColumn(
            name=col_name,
            transform="UNION",
            expression=col_name,
            sources=sources,
            branches=branches,
        ))
        used_names.add(col_name)


def _union_output_name_for_position(
    first_name: str,
    branch_cols: list[list[ScopeColumn]],
    position: int,
    used_names: set[str],
) -> str:
    """Return a stable output name for a UNION position.

    UNION aligns columns by position, not by name. If the first branch repeats an
    output name, use a later branch's non-duplicate name at the same position
    before falling back to a generated suffix.
    """
    if first_name and not first_name.startswith("_col_") and first_name not in used_names:
        return first_name

    for cols in branch_cols[1:]:
        if position >= len(cols):
            continue
        candidate = cols[position].name
        if candidate and not candidate.startswith("_col_") and candidate not in used_names:
            return candidate

    base = first_name if first_name and not first_name.startswith("_col_") else f"col_{position + 1}"
    index = 2
    candidate = f"{base}_{index}"
    while candidate in used_names:
        index += 1
        candidate = f"{base}_{index}"
    return candidate


def _resolve_union_scopes_bottom_up(result: ScopeLineageResult) -> None:
    """Resolve union scopes in bottom-up order to handle nested unions.

    A union scope can only be resolved after all its branch scopes have columns.
    For nested unions (union inside a union branch), we must resolve the inner
    union first. We iterate until all union scopes have been resolved.
    """
    union_scopes = {sid: sd for sid, sd in result.scopes.items() if sd.kind == "union"}
    if not union_scopes:
        return

    resolved = set()
    max_iterations = len(union_scopes) + 1  # safety limit

    for _ in range(max_iterations):
        progress = False
        for scope_id, scope_data in union_scopes.items():
            if scope_id in resolved:
                continue

            # Check if all branch scopes have columns (meaning they're resolved)
            branch_ids = scope_data.branches or []
            all_branches_ready = True
            for bid in branch_ids:
                branch_sd = result.scopes.get(bid)
                if branch_sd is None or not branch_sd.columns:
                    # A branch with no columns might be a nested union that hasn't been resolved yet
                    if bid in union_scopes and bid not in resolved:
                        all_branches_ready = False
                        break
                    # Or it might be a branch that was already resolved to empty
                    # (shouldn't happen, but handle gracefully)

            if all_branches_ready:
                _resolve_union_scope(scope_id, scope_data, result)
                resolved.add(scope_id)
                progress = True

        if not progress or len(resolved) == len(union_scopes):
            break


def _resolve_scope_union_passthrough(
    scope_id: str, scope_data: ScopeData, result: ScopeLineageResult,
) -> None:
    """When a scope has a Union expression, its columns are passthrough from its union child scope.

    Works for ROOT, CTE, or any scope whose expression is Union.
    The scope's columns are copies of the union scope's columns,
    but each column's sources point to the union scope instead of the branches.
    """
    # Find the union scope that belongs to this scope
    # Convention: union scope ID is "union:<context>" where <context> comes from this scope's ID
    context = None
    if scope_id == "ROOT":
        context = "main"
    elif ":" in scope_id:
        context = scope_id.split(":", 1)[1]
    else:
        context = scope_id

    union_scope_id = f"union:{context}"
    union_scope = result.scopes.get(union_scope_id)
    if union_scope is None:
        return

    for col in union_scope.columns:
        passthrough_col = ScopeColumn(
            name=col.name,
            transform=col.transform,
            expression=col.expression,
            sources=[SourceRef(scope=union_scope_id, column=col.name)],
            branches=col.branches,
        )
        scope_data.columns.append(passthrough_col)


def _refresh_union_scopes_after_star_expansion(
    result: ScopeLineageResult,
    all_scopes: list,
) -> None:
    """Re-resolve UNION scopes after SELECT * branches have concrete columns."""
    union_scopes = [scope for scope in result.scopes.values() if scope.kind == "union"]
    if not union_scopes:
        return

    for scope_data in union_scopes:
        scope_data.columns = []
    _resolve_union_scopes_bottom_up(result)

    for sg_scope in all_scopes:
        if not isinstance(sg_scope.expression, exp.Union):
            continue
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None or scope_id not in result.scopes:
            continue
        scope_data = result.scopes[scope_id]
        if scope_data.kind == "union":
            continue
        scope_data.columns = []
        _resolve_scope_union_passthrough(scope_id, scope_data, result)




def _expand_regex_column_selection(
    result: ScopeLineageResult,
    all_scopes: list[Scope],
    schema: dict | None,
    *,
    regex_columns_enabled: bool = True,
) -> None:
    """Expand Spark's quoted regex column selection into the columns it matches.

    ``SELECT `(dt)?+.+` `` selects every column whose name matches the pattern. Read as a
    literal name it produces a column no table has, so the projection resolves to nothing
    and every downstream reference to that scope follows it down (REGEX-COLUMN-001).

    The schema is consulted before the pattern is: a column that genuinely exists under
    that name — metacharacters and all — is a name, not a pattern, and stays literal.
    Without the source's columns there is nothing to match against, so the projection is
    left as it is and keeps reporting its gap rather than inventing column names.
    """
    expanded: list[tuple[str, str]] = []
    if not regex_columns_enabled:
        # spark.sql.parser.quotedRegexColumnNames is off -- Spark's own default. There the
        # backtick-quoted name is an ordinary column, the statement fails analysis and never
        # runs, so expanding it would invent lineage for SQL that cannot execute. The column
        # is left exactly as an unexpandable pattern already is when no schema is available;
        # only the warning changes, from "no such column" to the reason.
        _report_regex_selection_disabled(result, all_scopes, schema)
        return
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        scope_data = result.scopes.get(scope_id) if scope_id else None
        if scope_data is None:
            continue
        inputs = _regex_selection_input_columns(sg_scope, result, schema)
        patterns = [
            column
            for column in scope_data.columns
            if _looks_like_regex_column_selection(column, inputs)
        ]
        if not patterns:
            continue
        existing = {column.name for column in scope_data.columns}
        for pattern_column in patterns:
            matched = _columns_matching_regex_selection(pattern_column.name, inputs)
            if not matched:
                continue
            for source_id, column_name in matched:
                if column_name in existing:
                    continue
                scope_data.columns.append(ScopeColumn(
                    name=column_name,
                    transform="DIRECT",
                    expression=column_name,
                    sources=[SourceRef(scope=source_id, column=column_name)],
                ))
                existing.add(column_name)
            scope_data.columns.remove(pattern_column)
            expanded.append((scope_id, pattern_column.name))
    _retract_warnings_for_expanded_patterns(result, expanded)


def _retract_warnings_for_expanded_patterns(
    result: ScopeLineageResult,
    expanded: list[tuple[str, str]],
) -> None:
    """Drop the "no such column" warnings a successful expansion has just disproved.

    Column-reference resolution runs before this pass and cannot know a name is a
    pattern, so it warns that the column exists nowhere -- `column_not_found` for the
    bare form, `column_not_in_table_schema` for the qualified one. Once the pattern has
    been matched and removed, both warnings describe a column the scope no longer has.

    Gated on `expanded`, i.e. on the match having actually happened, and deliberately
    not on "the name looks like a regex": that predicate is only a metacharacter test,
    so it is also true of a genuinely missing column called `amount$usd`, of a pattern
    that matched nothing, and of a pattern in a WHERE clause that is never expanded.
    Suppressing on it would trade a false alarm for a false silence. Same shape as
    `_prune_resolved_star_warnings`, which retracts only what a later pass disproved.
    """
    if not expanded:
        return
    retractable = {"column_not_found", "column_not_in_table_schema"}
    keys = {(scope_id, name) for scope_id, name in expanded}
    result.diagnostics.warnings = [
        warning
        for warning in result.diagnostics.warnings
        if warning.type not in retractable
        or not any(
            warning.scope == scope_id and f"'{name}'" in (warning.msg or "")
            for scope_id, name in keys
        )
    ]


def _report_regex_selection_disabled(
    result: ScopeLineageResult,
    all_scopes: list[Scope],
    schema: dict | None,
) -> None:
    """Replace the pattern column's "not found" warning with the real reason.

    Only that one warning is touched, matched on scope and name. Everything downstream --
    the dangling refs, their warnings and their gaps -- is the state an unexpandable
    pattern already produces, and `_drop_dangling_column_refs` documents its warning as the
    audit trail for rewriting those sources to UNKNOWN (LINEAGE-001). Retracting those
    would leave a silent UNKNOWN, and the session setting disproves none of them: the refs
    genuinely dangle.
    """
    disabled: set[tuple[str, str]] = set()
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        scope_data = result.scopes.get(scope_id) if scope_id else None
        if scope_data is None:
            continue
        inputs = _regex_selection_input_columns(sg_scope, result, schema)
        for column in scope_data.columns:
            if _looks_like_regex_column_selection(column, inputs):
                disabled.add((scope_id, column.name))
    if not disabled:
        return
    retractable = {"column_not_found", "column_not_in_table_schema"}
    result.diagnostics.warnings = [
        warning
        for warning in result.diagnostics.warnings
        if warning.type not in retractable
        or not any(
            warning.scope == scope_id and f"'{name}'" in (warning.msg or "")
            for scope_id, name in disabled
        )
    ]
    for scope_id, name in sorted(disabled):
        result.diagnostics.warnings.append(DiagnosticWarning(
            type="regex_column_selection_disabled",
            scope=scope_id,
            msg=(
                f"Projection '{name}' is a quoted regex column selection, but "
                f"spark.sql.parser.quotedRegexColumnNames is off in this session "
                f"(Spark's default). Spark reads it as an ordinary column name and the "
                f"statement fails analysis, so it was not expanded."
            ),
        ))


def _looks_like_regex_column_selection(
    column: ScopeColumn,
    inputs: dict[str, list[str]],
) -> bool:
    name = column.name or ""
    if not name or name == "*" or not (set(name) & _REGEX_COLUMN_METACHARACTERS):
        return False
    if column.transform == "EXPAND_ALL" or name.endswith(".*"):
        # `a.*` is an unexpanded star waiting for its upstream columns, not a pattern —
        # and read as a pattern it matches whatever happens to start with "a", so a 63
        # column star became the one column named app_code. The placeholder only exists
        # when the upstream was still empty at construction time, which is why deep
        # scripts hit this and small ones never do (REGEX-COLUMN-002).
        return False
    if any(name in names for names in inputs.values()):
        return False
    return _compiled_column_pattern(name) is not None






def _columns_matching_regex_selection(
    pattern: str,
    inputs: dict[str, list[str]],
) -> list[tuple[str, str]]:
    compiled = _compiled_column_pattern(pattern)
    if compiled is None:
        return []
    matched: list[tuple[str, str]] = []
    for source_id, names in inputs.items():
        for name in names:
            if compiled.fullmatch(name):
                matched.append((source_id, name))
    return matched


def _regex_selection_input_columns(
    sg_scope: Scope,
    result: ScopeLineageResult,
    schema: dict | None,
) -> dict[str, list[str]]:
    """Columns each of this scope's sources exposes, physical tables and scopes alike.

    Read from the sqlglot scope rather than ``input_edges``: those are populated later, in
    the facts pass, and this runs while columns are still being resolved.
    """
    names: dict[str, list[str]] = {}
    for source in (sg_scope.sources or {}).values():
        if isinstance(source, Scope):
            scope_id = getattr(source, _SCOPE_ID_ATTR, None)
            upstream = result.scopes.get(scope_id) if scope_id else None
            if upstream is not None:
                names[scope_id] = [
                    column.name for column in upstream.columns if column.name != "*"
                ]
        elif isinstance(source, exp.Table) and schema is not None:
            table = _qualified_table(source)
            columns = schema.get(_normalize_table_name(table))
            if columns:
                names[table] = list(columns)
    return names


def _apply_star_except_lists(result: ScopeLineageResult, all_scopes: list) -> None:
    """Remove the columns a `SELECT * EXCEPT (...)` excludes, once, for every scope.

    Deliberately one pass rather than a filter at each expansion site. A star is
    materialized in at least three places -- projection-time expansion, the deferred
    expander, and union passthrough -- and which one runs depends on how the query was
    written, not on what it means. `SELECT * EXCEPT (c) FROM (… UNION …)` never reaches
    the projection-time expander at all, so filtering there answered the same construct
    two different ways.

    Runs before target_field_binding, whose positional binding is gated on the projection
    count matching the target's: the excluded columns have to be gone before that compares.
    """
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        scope_data = result.scopes.get(scope_id or "")
        if scope_data is None:
            continue
        select = sg_scope.expression
        if not isinstance(select, exp.Select):
            continue
        except_names: list[str] = []
        unsupported: list[str] = []
        for projection in select.expressions:
            names, bad = _star_modifiers(projection)
            except_names.extend(names)
            unsupported.extend(bad)
        if unsupported:
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="star_modifier_not_supported",
                scope=scope_id or "UNKNOWN",
                msg=(
                    f"star modifier(s) {', '.join(sorted(set(unsupported))).upper()} are "
                    f"not part of Spark's grammar (only EXCEPT is); left unapplied rather "
                    f"than modelled"
                ),
            ))
        if not except_names:
            continue
        if any(_is_star_name(column.name) for column in scope_data.columns):
            # The star never expanded, so there is nothing to remove and the placeholder
            # stands for "all columns" -- including the excluded ones. Saying so keeps it
            # from reading as a complete answer.
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="star_modifier_not_applied",
                scope=scope_id or "UNKNOWN",
                msg=(
                    f"SELECT * EXCEPT ({', '.join(except_names)}) could not be applied: "
                    f"the star itself did not expand"
                ),
            ))
            continue
        wanted = {name.lower() for name in except_names}
        present = {column.name.lower() for column in scope_data.columns}
        missing = sorted(wanted - present)
        if missing:
            # sqlglot accepts EXCEPT (nosuch); Spark fails analysis on it. Treating it as a
            # no-op would publish a full expansion for a statement the engine rejects.
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="star_except_column_not_found",
                scope=scope_id or "UNKNOWN",
                msg=(
                    f"SELECT * EXCEPT names {', '.join(missing)}, which the star does not "
                    f"produce; Spark fails analysis on this"
                ),
            ))
        scope_data.columns = [
            column for column in scope_data.columns if column.name.lower() not in wanted
        ]


def _is_star_name(name: str) -> bool:
    return name == "*" or name.endswith(".*")


def _expand_star_columns(result: ScopeLineageResult) -> None:
    """Expand wildcard (*) columns into concrete columns when the upstream scope has explicit ones.

    If scope S has  * <- [(upstream, '*')]  and upstream has concrete columns [c1, c2, ...],
    add  cI <- [(upstream, cI)]  to S for each cI not already explicitly defined in S.
    Repeats until stable to unroll chains (e.g. subq:a.* -> subq:aa.* -> union:aa.[cols]).
    """
    changed = True
    while changed:
        changed = False
        for scope_data in result.scopes.values():
            star_cols = _star_passthrough_columns(scope_data)
            if not star_cols:
                continue
            existing = {c.name for c in scope_data.columns}
            removable_star_col_ids: set[int] = set()
            for star_col in star_cols:
                star_fully_expanded = bool(star_col.sources)
                for src_ref in star_col.sources:
                    upstream = result.scopes.get(src_ref.scope)
                    if upstream is None:
                        star_fully_expanded = False
                        continue
                    if src_ref.scope not in scope_data.star_expanded_from:
                        scope_data.star_expanded_from.append(src_ref.scope)
                    concrete_upstream_columns = [
                        up_col for up_col in upstream.columns
                        if up_col.name != "*" and not up_col.name.endswith(".*")
                    ]
                    if not concrete_upstream_columns:
                        star_fully_expanded = False
                        continue
                    if len(concrete_upstream_columns) != len(upstream.columns):
                        # Upstream still carries an unexpanded star of its own, so its
                        # concrete columns are only a partial enumeration — this star
                        # is NOT fully expanded and must survive for later
                        # reference-driven materialization.
                        star_fully_expanded = False
                    for up_col in upstream.columns:
                        if up_col.name == "*" or up_col.name in existing:
                            continue
                        scope_data.columns.append(ScopeColumn(
                            name=up_col.name,
                            transform="DIRECT",
                            expression=up_col.name,
                            sources=[_source_ref_with_column(
                                src_ref,
                                up_col.name,
                            )],
                        ))
                        existing.add(up_col.name)
                        changed = True
                if star_fully_expanded:
                    removable_star_col_ids.add(id(star_col))
            if removable_star_col_ids:
                scope_data.columns = [
                    col for col in scope_data.columns
                    if id(col) not in removable_star_col_ids
                ]
                changed = True


def _scope_star_may_be_incomplete(result: ScopeLineageResult, scope_id: str, _depth: int = 0) -> bool:
    """Whether a scope's column enumeration may be incomplete (open star chain).

    True when the scope still carries a star column, was expanded from a
    physical table's (possibly incomplete) schema, or was star-expanded from an
    upstream scope that is itself open.
    """
    if _depth > 10:
        return False
    scope_data = result.scopes.get(scope_id)
    if scope_data is None:
        return False
    if _star_passthrough_columns(scope_data) or scope_data.star_schema_sources:
        return True
    return any(
        _scope_star_may_be_incomplete(result, up, _depth + 1)
        for up in scope_data.star_expanded_from
    )


def _materialize_referenced_star_columns(result: ScopeLineageResult) -> None:
    """Add pass-through columns for references into a scope that still has SELECT *.

    Without physical table schemas, a scope like ``SELECT * FROM physical`` cannot be
    fully expanded. If a downstream scope later references ``a.call_id``, however, we
    can still materialize just that referenced column as a pass-through from the star
    source. This keeps the graph internally consistent while preserving the broader
    schema limitation.
    """
    changed = True
    while changed:
        changed = False
        known = {
            sid: {c.name for c in sd.columns}
            for sid, sd in result.scopes.items()
        }
        needed: dict[str, set[str]] = {}
        for scope_data in result.scopes.values():
            for col in scope_data.columns:
                for src in col.sources:
                    if src.scope in result.scopes and src.column not in known[src.scope] and src.column != "*":
                        needed.setdefault(src.scope, set()).add(src.column)

        for scope_id, col_names in needed.items():
            scope_data = result.scopes[scope_id]
            star_cols = _star_passthrough_columns(scope_data)
            if not star_cols:
                # Star already expanded — but the enumeration may be incomplete:
                # schema exports can omit columns, and that incompleteness carries
                # through star chains (subq -> subq -> physical). A downstream
                # reference proves the column exists (the SQL is production-valid),
                # so materialize it as a pass-through instead of leaving it dangling.
                fallback_sources: list[SourceRef] = []
                if scope_data.star_schema_sources:
                    fallback_sources = [
                        SourceRef(scope=fq, column="")
                        for fq in scope_data.star_schema_sources
                    ]
                else:
                    open_ups = [
                        up for up in scope_data.star_expanded_from
                        if _scope_star_may_be_incomplete(result, up)
                    ]
                    fallback_sources = [SourceRef(scope=up, column="") for up in open_ups]
                if fallback_sources:
                    existing = known[scope_id]
                    for col_name in sorted(col_names):
                        if col_name in existing:
                            continue
                        scope_data.columns.append(ScopeColumn(
                            name=col_name,
                            transform="DIRECT",
                            expression=col_name,
                            sources=[
                                _source_ref_with_column(src, col_name)
                                for src in fallback_sources
                            ],
                        ))
                        existing.add(col_name)
                        changed = True
                continue
            existing = known[scope_id]
            for col_name in sorted(col_names):
                if col_name in existing:
                    continue
                sources = []
                for star_col in star_cols:
                    for star_src in star_col.sources:
                        upstream = result.scopes.get(star_src.scope)
                        if upstream is not None and not _scope_can_passthrough_column(upstream, col_name):
                            continue
                        sources.append(
                            _source_ref_with_column(star_src, col_name)
                        )
                if not sources:
                    continue
                scope_data.columns.append(ScopeColumn(
                    name=col_name,
                    transform="DIRECT",
                    expression=col_name,
                    sources=sources,
                ))
                existing.add(col_name)
                changed = True


def _source_ref_with_column(ref: SourceRef, column: str) -> SourceRef:
    """Retarget a star source without dropping its SQL input occurrence."""
    return SourceRef(
        scope=ref.scope,
        column=column,
        candidates=[dict(item) for item in ref.candidates],
        qualifier=ref.qualifier,
        binding_scope_id=ref.binding_scope_id,
        input_ref_id=ref.input_ref_id,
    )


def _scope_can_passthrough_column(scope_data: ScopeData, col_name: str) -> bool:
    """Return whether an internal scope can plausibly provide a star-materialized column."""
    if any(c.name == col_name for c in scope_data.columns):
        return True
    return bool(_star_passthrough_columns(scope_data))


def _star_passthrough_columns(scope_data: ScopeData) -> list[ScopeColumn]:
    """Return wildcard passthrough columns, covering both ``*`` and ``alias.*``."""
    return [
        c for c in scope_data.columns
        if (
            c.transform == "EXPAND_ALL"
            or c.name == "*"
            or c.name.endswith(".*")
        )
    ]


def _expand_merge_star(
    result: ScopeLineageResult,
    using_scope_id: str | None,
    using_node,
    target_columns: list[str] | None,
    *,
    branch: str,
    when_index: int,
) -> None:
    """Expand a MERGE `*` branch the way Spark does: over the target's columns.

    Spark's InsertStarAction/UpdateStarAction iterate `targetTable.output` and pull each
    attribute from the source by name, so the columns written -- and their order -- come
    from the target, not the USING side. Walking the source instead published source
    names as target columns, which is wrong whenever the two disagree (MERGESTAR-001).

    Two things it deliberately does not do:

    * Without a target column list it keeps the old source-driven shape. Guessing target
      names from the source is what this fixes; doing it silently when we simply were not
      given the DDL would be the same error wearing a different hat.
    * When the source's own columns are unknown it gets out of the way entirely. "The
      source lacks this column" is a claim about the user's SQL -- Spark would fail the
      analysis -- and we cannot make it from an absent schema. The honest
      `star_not_expanded` that the projection already carries says the true thing.
    """
    scope_data = result.scopes.get(using_scope_id or "")
    source_columns = list(scope_data.columns) if scope_data else []
    source_alias = (
        using_node.alias_or_name
        if using_node is not None and using_node.alias_or_name
        else "source"
    )

    def emit(name: str, source_name: str | None) -> None:
        result.scopes["ROOT"].columns.append(ScopeColumn(
            name=name,
            transform="DIRECT",
            expression=f"{source_alias}.{source_name or name}",
            sources=(
                [SourceRef(scope=using_scope_id, column=source_name)]
                if source_name is not None
                else [SourceRef(scope="UNKNOWN", column=name)]
            ),
            merge_branch=branch,
            merge_when_index=when_index,
        ))

    if not target_columns:
        for source_column in source_columns:
            emit(source_column.name, source_column.name)
        return

    by_name = {column.name.lower(): column.name for column in source_columns}
    source_columns_known = bool(source_columns) and not any(
        column.name == "*" or str(column.name or "").endswith(".*")
        for column in source_columns
    )
    if not source_columns_known:
        for source_column in source_columns:
            emit(source_column.name, source_column.name)
        return

    for target_name in target_columns:
        matched = by_name.get(target_name.lower())
        emit(target_name, matched)
        if matched is None:
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="merge_star_target_column_missing_in_source",
                scope="ROOT",
                msg=(
                    f"MERGE ... {branch.upper()} * writes target column "
                    f"'{target_name}', but the source has no column of that name. "
                    f"Spark resolves a star branch against the target's columns and "
                    f"fails analysis when one cannot be found in the source."
                ),
            ))


def _resolve_merge_columns(
    merge_node: exp.Merge,
    using_scope: Scope | None,
    all_scopes: list[Scope],
    result: ScopeLineageResult,
    schema: dict | None = None,
    target_columns: list[str] | None = None,
) -> None:
    """Resolve MERGE WHEN clauses into ROOT scope columns."""
    using_node = merge_node.args.get("using")
    merge_target = merge_node.this
    target_qualifiers: set[str] = set()
    if isinstance(merge_target, exp.Table):
        target_qualifiers.update(
            value
            for value in (
                merge_target.alias_or_name,
                merge_target.name,
                result.target_table,
                result.target_table.rsplit(".", 1)[-1] if result.target_table else "",
            )
            if value
        )

    using_scope_id = getattr(using_scope, _SCOPE_ID_ATTR, None)

    whens = merge_node.args.get("whens")
    if whens is None:
        return

    when_items = whens.expressions if hasattr(whens, "expressions") else [whens]
    for when_index, when in enumerate(when_items):
        # The branch is read from the WHEN clause itself, not inferred from the THEN
        # action. Spark has three clause kinds and the action shape only distinguishes
        # two of them, so inferring silently mislabels NOT MATCHED BY SOURCE -- and,
        # worse, resolves its values against a relation that branch cannot see.
        matched = bool(when.args.get("matched"))
        by_source = not matched and bool(when.args.get("source"))
        branch = "matched" if matched else "not_matched"
        # Contract 1.0's enum names two of Spark's three clause kinds; the third is
        # carried by the qualifier instead of being forced into a name that would state
        # the wrong rowset semantics.
        branch_label = None if by_source else branch
        branch_qualifier = "not_matched_by_source" if by_source else None
        policy = "target" if by_source else ("both" if matched else "source")
        then = when.args.get("then")
        if by_source and isinstance(then, exp.Update):
            # Say why the label is absent. Without this a consumer sees only that
            # merge_branch is gone and cannot tell "contract 1.0 has no name for this
            # clause" from "this is not a MERGE write".
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="merge_branch_not_representable",
                scope="ROOT",
                msg=(
                    "MERGE WHEN NOT MATCHED BY SOURCE is one of Spark's three WHEN "
                    "clause kinds and contract 1.0's merge_branch enum names only two; "
                    "merge_branch is omitted and merge_branch_qualifier carries the "
                    "clause kind instead."
                ),
            ))
        if isinstance(then, exp.Update):
            if any(isinstance(item, exp.Star) for item in then.expressions):
                # WHEN MATCHED THEN UPDATE SET * -- same target-driven expansion as
                # INSERT *. Only legal under a matched clause, so the branch label is
                # unambiguous (SqlBaseParser.g4: notMatchedBySourceAction has no SET *).
                _expand_merge_star(
                    result, using_scope_id, using_node, target_columns,
                    branch="matched", when_index=when_index,
                )
                continue
            # WHEN MATCHED THEN UPDATE SET
            # Only direct Update expressions are assignments. A recursive find_all(EQ)
            # also visits predicates inside scalar subqueries and used to publish e.g.
            # ``lookup.id = target.id`` as a second target-column assignment.
            for eq in then.expressions:
                if not isinstance(eq, exp.EQ):
                    continue
                dst_col = eq.this
                src_expr = eq.expression
                dst_name = dst_col.name if isinstance(dst_col, exp.Column) else None
                if dst_name is None:
                    continue
                transform = _classify_extended(src_expr)
                expression = src_expr.sql(dialect=DIALECT)
                sources = _resolve_merge_value_sources(
                    src_expr,
                    expression,
                    using_scope,
                    all_scopes,
                    target_qualifiers,
                    result,
                    policy=policy,
                    schema=schema,
                )

                if any(source.scope == result.target_table for source in sources):
                    result.source_tables = sorted({
                        *result.source_tables,
                        result.target_table,
                    })

                result.scopes["ROOT"].columns.append(ScopeColumn(
                    name=dst_name, transform=transform, expression=expression,
                    sources=sources, merge_branch=branch_label,
                    merge_branch_qualifier=branch_qualifier,
                    merge_when_index=when_index,
                ))

        elif isinstance(then, exp.Insert):
            # WHEN NOT MATCHED THEN INSERT (cols) VALUES (exprs)
            ins_cols = then.this
            values = then.expression
            if isinstance(ins_cols, exp.Star):
                _expand_merge_star(
                    result, using_scope_id, using_node, target_columns,
                    branch="not_matched", when_index=when_index,
                )
            elif isinstance(ins_cols, exp.Tuple) and isinstance(values, exp.Tuple):
                for dst_col_node, val_expr in zip(ins_cols.expressions, values.expressions):
                    dst_name = dst_col_node.name if hasattr(dst_col_node, "name") else str(dst_col_node)
                    transform = _classify_extended(val_expr)
                    expression = val_expr.sql(dialect=DIALECT)
                    sources = _resolve_merge_value_sources(
                        val_expr,
                        expression,
                        using_scope,
                        all_scopes,
                        target_qualifiers,
                        result,
                        policy=policy,
                        schema=schema,
                        )

                    result.scopes["ROOT"].columns.append(ScopeColumn(
                        name=dst_name, transform=transform, expression=expression,
                        sources=sources, merge_branch=branch_label,
                        merge_branch_qualifier=branch_qualifier,
                        merge_when_index=when_index,
                    ))
        elif _is_merge_delete_then(then):
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="merge_delete_ignored",
                scope="ROOT",
                msg=(
                    # Named from the clause actually seen: DELETE is legal under both
                    # MATCHED and NOT MATCHED BY SOURCE, and a warning that names the
                    # wrong one sends the reader looking for a clause that is not there.
                    f"MERGE WHEN {'NOT MATCHED BY SOURCE' if by_source else 'MATCHED'} "
                    "THEN DELETE is a row-level operation and does not produce ROOT "
                    "output columns."
                ),
            ))


def _merge_using_column_passes_through(
    result: ScopeLineageResult,
    using_scope_id: str | None,
    col_name: str,
    table: str,
) -> bool:
    """Does the USING relation expose ``col_name`` straight from ``table``?

    True for a bare table wrapped as a subquery, where binding the reference directly to the
    table is the lexical source a previous fix deliberately preserves. False for a rename, a
    literal or any computed projection — there the column belongs to the subquery, and
    naming the inner table invents one it does not have.
    """
    scope_data = result.scopes.get(using_scope_id or "")
    if scope_data is None:
        return True
    column = next(
        (item for item in scope_data.columns if item.name == col_name),
        None,
    )
    if column is None:
        return True
    return any(
        source.scope == table and source.column == col_name
        for source in column.sources or []
    )


def _resolve_merge_value_sources(
    value_expr: exp.Expression,
    expression_sql: str,
    using_scope: Scope | None,
    all_scopes: list[Scope],
    target_qualifiers: set[str],
    result: ScopeLineageResult,
    *,
    policy: str = "source",
    schema: dict | None = None,
) -> list[SourceRef]:
    """Resolve one MERGE assignment value in the scope its WHEN branch makes visible.

    ``policy`` mirrors Spark's ``MergeResolvePolicy`` (Analyzer.resolveAssignments): a
    MATCHED action resolves against target *and* source, a NOT MATCHED action against the
    source, and a NOT MATCHED BY SOURCE action against the target only. Resolving every
    branch against the USING relation -- which is what this function used to do -- invents
    a source edge for the branches where Spark cannot see the source at all, and publishes
    it with ``trace_complete: true`` and no fact gap.
    """
    sources: list[SourceRef] = []
    seen: set[tuple[str, str]] = set()

    def append(scope_id: str | None, column: str) -> None:
        if not scope_id or not column or (scope_id, column) in seen:
            return
        seen.add((scope_id, column))
        sources.append(SourceRef(scope=scope_id, column=column))

    # A scalar query is a rowset dependency, not a collection of columns to resolve
    # against MERGE USING. Match the query to its SQLGlot scope by AST identity and
    # reference the one value that the scalar scope exposes.
    for subquery in value_expr.find_all(exp.Subquery):
        if subquery is not value_expr and _inside_nested_set_op(value_expr, subquery):
            continue
        query = subquery.unnest()
        scalar_scope = next(
            (scope for scope in all_scopes if scope.expression is query),
            None,
        )
        scalar_scope_id = getattr(scalar_scope, _SCOPE_ID_ATTR, None)
        scalar_scope_data = result.scopes.get(scalar_scope_id or "")
        if scalar_scope_data and scalar_scope_data.columns:
            append(scalar_scope_id, scalar_scope_data.columns[0].name)

    using_scope_id = getattr(using_scope, _SCOPE_ID_ATTR, None)
    for col_ref in value_expr.find_all(exp.Column):
        if _inside_nested_set_op(value_expr, col_ref):
            continue
        col_table = col_ref.table
        col_name = col_ref.name
        if col_table in target_qualifiers and result.target_table:
            append(result.target_table, col_name)
            continue
        if col_table and policy == "target":
            # Spark fails analysis here: a BY SOURCE action cannot see the source relation,
            # so this qualifier names nothing in scope. Publishing a source edge would be a
            # fact we already know to be false.
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="dangling_column_ref_dropped",
                scope="ROOT",
                msg=(
                    f"'{col_table}.{col_name}' in a MERGE NOT MATCHED BY SOURCE action "
                    f"references a relation that branch cannot see; dropped rather than "
                    f"attributed to the source"
                ),
            ))
            continue
        if col_table:
            source = using_scope.sources.get(col_table) if using_scope else None
            if isinstance(source, exp.Table):
                # The USING alias names the whole subquery. When an inner table carries the
                # same alias it wins this lookup, and `alias.<col>` was published as a column
                # of that table — turning `record_id AS biz_no` into a column the table does
                # not have, and a literal into a physical field (MERGE-ALIAS-001). The direct
                # lexical binding is still right where the subquery passes the column
                # straight through, which is what the bare-table shape relies on, so only a
                # column the subquery derives is redirected to the subquery itself.
                if _merge_using_column_passes_through(
                    result, using_scope_id, col_name, _qualified_table(source)
                ):
                    append(_qualified_table(source), col_name)
                else:
                    append(using_scope_id, col_name)
            elif isinstance(source, Scope):
                append(getattr(source, _SCOPE_ID_ATTR, None), col_name)
            else:
                append(using_scope_id, col_name)
        else:
            resolved = _resolve_merge_unqualified(
                col_name,
                policy=policy,
                using_scope=using_scope,
                using_scope_id=using_scope_id,
                result=result,
                schema=schema,
            )
            if resolved is not None:
                if resolved.scope == AMBIGUOUS_SCOPE_ID and (
                    resolved.scope, resolved.column
                ) not in seen:
                    seen.add((resolved.scope, resolved.column))
                    sources.append(resolved)
                else:
                    append(resolved.scope, resolved.column)

    return sources or _source_free_leaf_sources(value_expr, expression_sql)


def _merge_relation_exposes(columns: list[str] | None, col_name: str) -> bool | None:
    """True/False if the relation's columns are known, None if they are not.

    Three-valued on purpose: "we cannot see the target's columns" must not collapse into
    "the target does not have it", or an unknowable name silently resolves to the source
    -- which is the guess this whole change exists to stop publishing.
    """
    if columns is None:
        return None
    return col_name.lower() in {c.lower() for c in columns}


def _resolve_merge_unqualified(
    col_name: str,
    *,
    policy: str,
    using_scope: Scope | None,
    using_scope_id: str | None,
    result: ScopeLineageResult,
    schema: dict | None,
) -> SourceRef | None:
    """Resolve an unqualified MERGE assignment value under one branch's resolve policy."""
    if policy == "source":
        return SourceRef(scope=using_scope_id, column=col_name) if using_scope_id else None

    target_columns = _merge_target_columns(result, schema)
    if policy == "target":
        # TARGET needs no existence check: the target is the only relation in scope, so
        # the name belongs to it whether or not we hold its schema.
        return (
            SourceRef(scope=result.target_table, column=col_name)
            if result.target_table
            else None
        )

    source_columns = _merge_source_columns(result, using_scope_id)
    in_target = _merge_relation_exposes(target_columns, col_name)
    in_source = _merge_relation_exposes(source_columns, col_name)

    if in_target and in_source:
        return _ambiguous_ref(
            col_name, using_scope, result,
            [
                {"scope": result.target_table, "column": col_name},
                {"scope": using_scope_id, "column": col_name},
            ],
            "MERGE relations",
        )
    if in_target and in_source is False:
        return SourceRef(scope=result.target_table, column=col_name)
    if in_source and in_target is False:
        return SourceRef(scope=using_scope_id, column=col_name)

    # At least one side's columns are unknown, so neither "ambiguous" nor "resolves to the
    # side I can see" is established. Unknowable is not ambiguous, and neither licenses
    # picking a side.
    result.diagnostics.warnings.append(DiagnosticWarning(
        type="unresolved_unqualified_no_schema",
        scope="ROOT",
        msg=(
            f"Unqualified column '{col_name}' in a MERGE MATCHED action could come from "
            f"the target or the source, and at least one of their column lists is "
            f"unavailable; left unresolved rather than attributed to one"
        ),
    ))
    return SourceRef(scope="UNKNOWN", column=col_name)


def _merge_target_columns(result, schema: dict | None) -> list[str] | None:
    if not schema or not result.target_table:
        return None
    columns = schema.get(_normalize_table_name(result.target_table))
    if not columns:
        return None
    return [c["name"] if isinstance(c, dict) else str(c) for c in columns]


def _merge_source_columns(result, using_scope_id: str | None) -> list[str] | None:
    scope_data = result.scopes.get(using_scope_id or "")
    if scope_data is None or not scope_data.columns:
        return None
    return [c.name for c in scope_data.columns]


def _is_merge_delete_then(then: exp.Expression | None) -> bool:
    if then is None:
        return False
    if isinstance(then, exp.Var):
        return str(then.this).upper() == "DELETE"
    return then.sql(dialect=DIALECT).strip().upper() == "DELETE"


def _build_depends_on_and_graph(result: ScopeLineageResult) -> None:
    """Populate depends_on for each scope and build scope_graph edges."""
    all_nodes = set(result.scopes.keys())

    for scope_id, scope_data in result.scopes.items():
        referenced = set()

        for col in scope_data.columns:
            for src in col.sources:
                if _is_dependency_scope(src.scope):
                    referenced.add(src.scope)

        for join in scope_data.joins:
            if _is_dependency_scope(join.left_scope):
                referenced.add(join.left_scope)
            if _is_dependency_scope(join.right_scope):
                referenced.add(join.right_scope)
            for cc in join.condition_columns:
                if _is_dependency_scope(cc.scope):
                    referenced.add(cc.scope)

        for f in scope_data.filters:
            for c in f.columns:
                if _is_dependency_scope(c.scope):
                    referenced.add(c.scope)

        for g in scope_data.group_by:
            if _is_dependency_scope(g.scope):
                referenced.add(g.scope)

        for h in scope_data.having:
            for c in h.columns:
                if _is_dependency_scope(c.scope):
                    referenced.add(c.scope)

        for o in scope_data.order_by:
            if _is_dependency_scope(o.get("scope")):
                referenced.add(o["scope"])

        # Remove self-reference
        referenced.discard(scope_id)
        scope_data.depends_on = sorted(referenced)
        all_nodes.update(referenced)

    # Build edges
    result.scope_graph.nodes = sorted(all_nodes | set(result.source_tables))
    result.scope_graph.edges = []
    for scope_id, scope_data in result.scopes.items():
        for dep in scope_data.depends_on:
            result.scope_graph.edges.append(ScopeGraphEdge(from_=dep, to=scope_id))


# Delegation shims preserve the free-function surface used by callers/tests.
