"""sqlglot AST and optimizer-Scope traversal adapters for the scope domain."""
from __future__ import annotations

import re

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import Scope

from ._constants import DIALECT, _SCOPE_ID_ATTR
from .function_catalog import _KNOWN_UDAFS
from .parser import _qualified_table
from .scope_types import ScopeLineageResult, SourceRef
from .source_refs import _constant_sources, _system_sources

def _find_alias_in_parent(sg_scope: Scope) -> str | None:
    """Find the alias this scope uses in its parent scope's sources."""
    if sg_scope.is_udtf and isinstance(sg_scope.expression, exp.Lateral):
        alias = sg_scope.expression.args.get("alias")
        if alias is not None and alias.this is not None:
            return alias.this.name if hasattr(alias.this, "name") else str(alias.this)
    if sg_scope.parent is None:
        return None
    for name, src in sg_scope.parent.sources.items():
        if src is sg_scope:
            return name
    return None


def _selected_sources(sg_scope: Scope) -> dict:
    """Return only sources that participate in the current SELECT FROM/JOIN list."""
    try:
        selected = sg_scope.selected_sources
    except OptimizeError:
        return _selected_sources_from_ast(sg_scope)
    if not selected and sg_scope.sources:
        reconstructed = _selected_sources_from_ast(sg_scope)
        return reconstructed or sg_scope.sources
    return {alias: source for alias, (_node, source) in selected.items()}


def _selected_sources_from_ast(sg_scope: Scope) -> dict:
    """Rebuild selected inputs when sqlglot rejects a repeated source alias.

    ``Scope.sources`` also contains visible CTEs that are not selected by this query. Returning
    it after ``selected_sources`` raises would make those unrelated CTEs candidates for every
    unqualified column.
    """
    expression = sg_scope.expression
    if not isinstance(expression, exp.Select):
        return {}

    items: list[tuple[str, Scope | exp.Table]] = []
    from_ = expression.args.get("from_")
    if from_ is not None:
        item = _source_item_from_ast_node(getattr(from_, "this", None), sg_scope)
        if item:
            items.append(item)
    for join in expression.args.get("joins") or []:
        item = _source_item_from_ast_node(join.this, sg_scope)
        if item:
            items.append(item)
    for udtf_scope in getattr(sg_scope, "udtf_scopes", []) or []:
        alias = _find_alias_in_parent(udtf_scope) or "udtf"
        items.append((alias, udtf_scope))

    selected: dict[str, Scope | exp.Table] = {}
    for alias, source in items:
        key = alias
        suffix = 2
        while key in selected:
            key = f"{alias}#{suffix}"
            suffix += 1
        selected[key] = source
    return selected


def _source_free_leaf_sources(inner: exp.Expression, expression: str) -> list[SourceRef]:
    if _contains_runtime_function(inner):
        return _system_sources(expression)
    return _constant_sources(expression)


def _contains_runtime_function(node: exp.Expression) -> bool:
    runtime_names = {
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "CURRENT_TIME",
        "NOW",
        "RAND",
        "RANDOM",
        "UUID",
        "UNIX_TIMESTAMP",
    }
    for expr in node.walk():
        if isinstance(expr, (exp.CurrentDate, exp.CurrentTimestamp, exp.Rand)):
            return True
        if isinstance(expr, exp.Anonymous):
            name = expr.name.upper() if hasattr(expr, "name") else ""
            if name in runtime_names:
                return True
    sql = node.sql(dialect=DIALECT).upper()
    return any(f"{name}(" in sql or name in {"CURRENT_DATE", "CURRENT_TIMESTAMP"} and name in sql for name in runtime_names)


def render_sql_or_none(tree: exp.Expression) -> str | None:
    """Print a parsed tree back to SQL, or give up without taking the caller down.

    Generation is not total. A statement whose identifier collides with a tokenizer keyword
    parses into a node the Spark generator cannot render -- `CAST(out AS DOUBLE)` yields a Cast
    whose `to` is None and `cast_sql` dereferences it -- and the AttributeError escaped the
    public API entirely (REGEN-001). One statement that cannot be *printed* must not cost a
    batch its other results, which is the whole reason broken statements are kept.

    The lineage is built from the AST, never from this string, so failing here costs a
    convenience field and nothing else. Returns None so the caller decides what to record.
    """
    try:
        return tree.sql(dialect=DIALECT)
    except Exception:  # noqa: BLE001 - any generator failure, not a known subset
        return None


def _inside_nested_set_op(root: exp.Expression, node: exp.Expression) -> bool:
    """Return True when ``node`` sits inside a nested SELECT or set-op branch of ``root``.

    Deliberately distinct from expression_refs._inside_nested_subquery: this one treats a
    UNION between node and root as nesting (resolvers must not attribute a set-op branch's
    columns to the outer expression) but does NOT stop at a bare exp.Subquery wrapper, and
    it accepts any node -- the resolvers also probe subquery nodes, not just columns.
    """
    if node is root:
        return False
    parent = node.parent
    while parent is not None and parent is not root:
        if isinstance(parent, (exp.Select, exp.Union)):
            return True
        parent = parent.parent
    return False


def _source_item_from_ast_node(
    node: exp.Expression | None,
    sg_scope: Scope,
) -> tuple[str, Scope | exp.Table] | None:
    if node is None:
        return None
    alias = node.alias if isinstance(node, (exp.Table, exp.Subquery)) else None
    source: Scope | exp.Table | None = None
    if isinstance(node, exp.Table):
        # A table reference may actually name a CTE; resolve that through the
        # scope source map by table name. Physical tables can be used directly.
        named_source = sg_scope.sources.get(node.name)
        if isinstance(named_source, Scope):
            source = named_source
        else:
            source = node
        alias = alias or node.name
    elif isinstance(node, exp.Subquery):
        if alias:
            # Preserve AST identity before consulting the alias dictionary. sqlglot stores
            # sources by alias, so a repeated alias keeps only the last subquery and would
            # otherwise make every duplicate appear to be that same scope.
            for scope_list_name in ("derived_table_scopes", "subquery_scopes"):
                for sub_scope in getattr(sg_scope, scope_list_name, []) or []:
                    if sub_scope.expression is node.this:
                        source = sub_scope
                        break
                if source is not None:
                    break
            if source is None:
                mapped = sg_scope.sources.get(alias)
                if isinstance(mapped, Scope):
                    source = mapped
    if alias and source is not None:
        return alias, source
    return None


def _source_ref_for_source(
    alias: str,
    source: Scope | exp.Table,
    col_name: str,
    result: ScopeLineageResult,
) -> SourceRef:
    if isinstance(source, Scope):
        upstream_id = _source_scope_id(alias, source, result)
        if upstream_id:
            return SourceRef(scope=upstream_id, column=col_name)
        return SourceRef(scope="UNKNOWN", column=col_name)
    return SourceRef(scope=_qualified_table(source), column=col_name)


def _source_scope_id(alias: str, source: Scope, result: ScopeLineageResult) -> str | None:
    """Return a stable result scope id for a sqlglot Scope source."""
    upstream_id = getattr(source, _SCOPE_ID_ATTR, None)
    if upstream_id in result.scopes:
        return upstream_id
    for candidate in (f"cte:{alias}", f"subq:{alias}", f"union:{alias}"):
        if candidate in result.scopes:
            return candidate
    return upstream_id


def _classify_extended(node: exp.Expression) -> str:
    """Classify expression type. Extends parser._classify with UNION and EXPAND_ALL."""
    if isinstance(node, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return "EXPAND_ALL"
    if isinstance(node, exp.Window):
        return "WINDOW"
    if isinstance(node, exp.AggFunc):
        return "AGGREGATE"
    if isinstance(node, (exp.Case, exp.If)):
        return "CONDITIONAL"
    if isinstance(node, exp.Subquery):
        return "EXPRESSION"  # LITERAL_SUBQUERY mapped to EXPRESSION per design decision
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return "CONSTANT"
    if isinstance(node, exp.Column):
        return "DIRECT"
    # Check for Anonymous UDAFs
    if isinstance(node, exp.Anonymous):
        func_name = node.name.upper() if hasattr(node, "name") else ""
        if func_name in _KNOWN_UDAFS:
            return "AGGREGATE"
    return "EXPRESSION"


_REGEX_COLUMN_METACHARACTERS = set(".*+?[]()|^$\\")


_POSSESSIVE_QUANTIFIER = re.compile(r"\(((?:[^()\\]|\\.)*)\)([?*+])\+")


def _compiled_column_pattern(pattern: str):
    """Compile a Spark column pattern the same way on every supported Python.

    Spark's exclusion idiom uses a possessive quantifier — ``(dt)?+.+`` reads as "every
    column except dt", because ``(dt)?+`` consumes ``dt`` without giving it back. Python
    only accepts that syntax from 3.11, and this project supports 3.9, so it is rewritten
    to the lookahead-and-backreference form that behaves identically everywhere. Letting
    the compile simply fail on older interpreters would make the same SQL produce different
    lineage depending on the Python running it.
    """
    for candidate in (pattern, _POSSESSIVE_QUANTIFIER.sub(r"(?=((?:\1)\2))\\1", pattern)):
        try:
            return re.compile(candidate)
        except re.error:
            continue
    return None


def _pivot_of_source_node(node) -> object | None:
    """Return the PIVOT attached to a FROM/JOIN item, if it has one."""
    pivots = getattr(node, "args", {}).get("pivots") or []
    return pivots[0] if pivots else None


def _pivot_output_names(pivot) -> list[str] | None:
    """Column names a PIVOT produces, or None when the IN list is not a literal list.

    The IN list is the column set: ``FOR k IN ('A', 'B')`` produces columns A and B. A
    subquery or ANY in that position leaves the set unknowable, and the caller must report
    a gap rather than bind to a name it guessed (PIVOT-001).
    """
    from sqlglot import exp

    fields = getattr(pivot, "args", {}).get("fields") or []
    names: list[str] = []
    for field in fields:
        if not isinstance(field, exp.In):
            return None
        for item in field.expressions:
            if isinstance(item, exp.Alias):
                names.append(item.alias)
                continue
            if isinstance(item, exp.Literal):
                names.append(str(item.this))
                continue
            return None
    return names or None
