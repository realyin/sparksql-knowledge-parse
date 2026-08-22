"""Scope tree builder: parse SQL, qualify, build_scope, assign scope_ids, create ScopeData stubs.

Entry point: parse_scope_lineage(sql, task_name, schema=None) -> ScopeLineageResult
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import ParseError

from .ctas_missing_as import repair_ctas_missing_as
from .session_settings import (
    DEFAULT_QUOTED_REGEX_COLUMN_NAMES,
    quoted_regex_column_names_setting,
)
from .keyword_identifiers import repair_keyword_identifiers
from sqlglot.optimizer.qualify import qualify as sg_qualify
from sqlglot.optimizer.scope import traverse_scope, Scope

from .parser import (
    _ORIGINALLY_ANONYMOUS_PROJECTION_META,
    _normalize_table_name,
    _qualified_table,
    _unwrap_target,
)
from .related_metadata import build_related_metadata
from ..metadata.schema_metadata import SchemaMap, normalize_schema_map
from ..metadata.target_table_metadata import lookup_target_table_metadata
from .scope_types import (
    ScopeData,
    ScopeLineageResult,
    Diagnostics,
    DiagnosticWarning,
)
from .scope_resolver import resolve_all
from .select_scope import _star_modifiers
from .scope_warnings import detect_warnings
from .scope_role_inferrer import infer_roles
from ..sqlglot_config import suppress_invalid_json_path_warnings
from ._constants import DIALECT, PARSE_OPTS, _ORIGINALLY_UNQUALIFIED_META, _SCOPE_ID_ATTR
from .expression_refs import _inside_nested_subquery
from .sequences import _unique_ordered
from .sqlglot_walk import _find_alias_in_parent, render_sql_or_none
# Re-exported only for the private integration repository, whose tests reach these through
# this module instead of through ._shared. Nothing in this module uses them.
from .sqlglot_walk import _source_item_from_ast_node
from .lineage_fact_gaps import _mark_gaps_from_recovered_syntax
from .scope_facts import _populate_enhanced_scope_facts

suppress_invalid_json_path_warnings()

# Attr name for attaching scope_id to sqlglot Scope objects


SUPPORTED_STATEMENTS = "INSERT / INSERT OVERWRITE / CTAS / MERGE"


class NoSupportedWriteStatementError(ValueError):
    """The SQL is readable, but contains no write statement modeled by this parser."""

    def __init__(self, skipped_statements: list[dict]) -> None:
        self.skipped_statements = [dict(item) for item in skipped_statements]
        super().__init__(_no_supported_statement_message(skipped_statements))


def _no_supported_statement_message(skipped: list[dict]) -> str:
    """Name what was actually found and what is actually supported.

    The old message was "No INSERT/MERGE statement found" — it omitted CTAS, which IS
    supported, and said nothing about what the SQL contained instead (CONTRACT-001).
    """
    found = ", ".join(sorted({item["statement_kind"] for item in skipped})) or "无可识别语句"
    return (
        f"未找到可解析的写表语句。支持: {SUPPORTED_STATEMENTS};本次发现: {found}。"
        f"独立 UPDATE / DELETE 不在建模范围内(本工具解析的是「从 SELECT 写入一张表」的字段血缘)。"
    )


def _statement_kind_label(tree) -> str:
    """A name for a statement this tool does not model, for the skip record."""
    for node_type, label in ((exp.Update, "UPDATE"), (exp.Delete, "DELETE")):
        if isinstance(tree, node_type):
            return label
    return type(tree).__name__.upper()


def _statement_category(statement_kind: str) -> str:
    if statement_kind in {"DELETE", "UPDATE", "TRUNCATETABLE"}:
        return "row_mutation"
    if statement_kind in {"SET", "USE"}:
        return "control_statement"
    if statement_kind == "SEMICOLON":
        return "empty_statement"
    return "unsupported_statement"


def _collect_insert_trees(sql: str) -> tuple[list, list[dict]]:
    """Top-level write statements, plus a record of every statement skipped.

    A multi-statement script can mix a write with statements this tool does not model. Those
    used to vanish from the result with nothing recorded, so a consumer could not tell a script
    of one INSERT from a script of one INSERT and three DELETEs (CONTRACT-001).
    """
    sql, _ = repair_keyword_identifiers(
        repair_ctas_missing_as(_normalize_directory_insert_sql(sql))[0]
    )
    trees = sqlglot.parse(sql, dialect=DIALECT, **PARSE_OPTS)
    write_trees, skipped = [], []
    # A SET applies from where it appears onward, so the flag is folded in statement order
    # and recorded per write: write_trees is flat and carries no script position.
    regex_flags: list[bool] = []
    regex_columns_enabled = DEFAULT_QUOTED_REGEX_COLUMN_NAMES
    for statement_index, tree in enumerate(trees):
        if tree is None:
            # `;;` -- sqlglot yields None here, where a bare `;` after a comment yields
            # exp.Semicolon. Both are empty statements, but only the second used to be
            # recorded, so the indices below (which count every position) had holes no
            # published field explained. v2 recorded both all along.
            skipped.append({
                "statement_id": f"stmt:{statement_index + 1:03d}",
                "normalized_sql": "",
                "statement_index": statement_index,
                "statement_kind": "EMPTY",
                "category": "empty_statement",
                "model_status": "ignored",
                "reason": "not_a_table_write_from_select",
                "supported": SUPPORTED_STATEMENTS,
            })
            continue
        setting = quoted_regex_column_names_setting(tree)
        if setting is not None:
            regex_columns_enabled = setting
        if (
            isinstance(tree, (exp.Insert, exp.Merge))
            or _is_ctas(tree)
            or tree.find(exp.Insert) is not None
            or tree.find(exp.Merge) is not None
        ):
            write_trees.append(tree)
            regex_flags.append(regex_columns_enabled)
            continue
        statement_kind = _statement_kind_label(tree)
        category = _statement_category(statement_kind)
        skipped.append({
            "statement_id": f"stmt:{statement_index + 1:03d}",
            # The task document has always carried this (task_lineage.py). Without it,
            # dropping the warning below would make the statement itself -- which SET, in
            # particular -- unrecoverable from every artifact this document produces.
            "normalized_sql": render_sql_or_none(tree) or "",
            "statement_index": statement_index,
            "statement_kind": statement_kind,
            "category": category,
            "model_status": (
                "ignored"
                if category in {"control_statement", "empty_statement"}
                else "unsupported"
            ),
            "reason": "not_a_table_write_from_select",
            "supported": SUPPORTED_STATEMENTS,
        })
    return write_trees, skipped, regex_flags


def _normalize_directory_insert_sql(sql: str) -> str:
    """Normalize Spark directory writes into a form sqlglot keeps with SELECT."""
    pattern = re.compile(
        r"(INSERT\s+OVERWRITE\s+(?:LOCAL\s+)?DIRECTORY\s+('[^']+'|\"[^\"]+\"))\s+USING\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?=SELECT|WITH)",
        re.IGNORECASE,
    )
    return pattern.sub(r"\1 STORED AS \3 ", sql)


def _is_ctas(tree: exp.Expression) -> bool:
    return _is_create_as_select(tree) or _is_cache_as_select(tree)


def _is_create_as_select(tree: exp.Expression) -> bool:
    return isinstance(tree, exp.Create) and tree.expression is not None


def _is_session_scoped_relation(tree: exp.Expression) -> bool:
    """Does this statement define a relation that disappears when the session ends?

    `CREATE TABLE db.r AS SELECT` and `CREATE OR REPLACE TEMP VIEW r AS SELECT` produced
    byte-identical lineage: both are CTAS, and nothing in the result said one of them never
    reaches storage. Consumers reconciling `final_table_states` against the catalogue
    concluded the warehouse had grown tables that do not exist (TEMPVIEW-001).

    One predicate covers every spelling on purpose. `is_cached_relation` answers this
    question for `CACHE [LAZY] TABLE` alone, and a fix that added a second, temp-view-only
    branch would leave the next reader to discover for themselves that the two must be asked
    together. The judgement is "does it persist", not "which syntax produced it".

    A non-temporary `CREATE VIEW` stores no rows but *is* registered in the catalogue and
    outlives the session, so it is not session-scoped; the boundary is persistence, not
    whether the relation holds data.
    """
    if isinstance(tree, exp.Cache):
        return True
    if not isinstance(tree, exp.Create):
        return False
    properties = tree.args.get("properties")
    if properties is None:
        return False
    # Deliberately not keyed on `kind`. `CREATE TEMPORARY TABLE x AS SELECT` carries the same
    # TemporaryProperty with kind=TABLE, and an earlier version of this predicate required
    # kind=VIEW and silently missed it -- committing, in code, the "which keyword produced it"
    # mistake the docstring above warns against.
    return any(
        isinstance(prop, exp.TemporaryProperty) for prop in properties.expressions
    )


def _is_cache_as_select(tree: exp.Expression) -> bool:
    """Spark's ``CACHE [LAZY] TABLE x AS SELECT`` builds a relation this script can read.

    sqlglot gives it the same shape CTAS has — produced relation on ``this``, projection on
    ``expression`` — and in lineage terms it is the same thing: a queryable relation defined
    by a SELECT. Recognising only ``exp.Create`` left the relation unregistered, so every
    downstream reference resolved against a physical table nobody has metadata for and
    cascaded into gaps (CACHE-001).

    ``CACHE TABLE existing_table`` has no ``expression``: it pins a relation rather than
    defining one, and stays outside the write statements.
    """
    return isinstance(tree, exp.Cache) and tree.expression is not None


def script_local_schema(
    schema: dict | None,
    known: dict[str, list[str]],
    result: ScopeLineageResult,
) -> None:
    """Record the columns a CREATE ... AS SELECT proves, for later statements to read.

    Statements are modelled one at a time, so a table a script builds is invisible to the
    statements that consume it: its columns cannot expand, and it lands in
    ``metadata_coverage.missing_tables`` as a table nobody can ever supply — it only exists
    inside this script. The producing statement already resolved its projection, so the
    columns are a fact here, not a guess.

    Only CREATE ... AS SELECT is registered. An INSERT does not define a table, and a
    projection that stayed a star did not prove any column name. Supplied metadata is never
    overwritten: a script-local CREATE that shadows a real warehouse table must not silently
    replace its authoritative schema.
    """
    if result.stmt_kind != "CTAS" or result.parse_status != "ok":
        return
    table = _normalize_table_name(result.target_table)
    if not table or (schema is not None and table in schema):
        return
    root = result.scopes.get("ROOT")
    columns = _unique_ordered(
        [column.name for column in (root.columns if root else []) if column.name]
    )
    if not columns or any(name == "*" for name in columns):
        return
    # Last definition wins: re-creating the table mid-script replaces what it exposes.
    known[table] = columns


def _schema_with_script_local_tables(
    schema: dict | None,
    known: dict[str, list[str]],
) -> dict | None:
    if not known:
        return schema
    return {**known, **(schema or {})}


_SYNTAX_ERROR_KEYS = ("description", "line", "col", "start_context", "highlight", "end_context")


def _ordered_syntax_errors(errors: list[dict]) -> list[dict]:
    """Impose an order sqlglot does not guarantee.

    One message is built per entry of ``Expression.required_args``, which is a ``set``, and
    CPython randomises string hashing per process — so a statement missing two required
    keywords yielded the same entries in an order that changed between runs. That order
    reaches ``lineage.json``, where this project treats byte-for-byte determinism as a
    contract invariant, and anyone diffing artifacts across runs saw a phantom change
    (SYNTAX-ORDER-001).

    Sorted by position first, so errors genuinely ordered by where they occur keep that
    order; the description only breaks ties, which is exactly the ambiguous case. A missing
    position sorts first rather than raising: the fallback entry carries a description alone.
    """
    return sorted(
        errors,
        key=lambda item: (
            item.get("line") if isinstance(item.get("line"), int) else -1,
            item.get("col") if isinstance(item.get("col"), int) else -1,
            str(item.get("description") or ""),
        ),
    )


def _syntax_status(sql: str) -> tuple[str, list[dict]]:
    """Parse strictly first, so that "sqlglot repaired this" stops being invisible.

    Statements are parsed with a lenient error level, so that one malformed statement in a
    corpus cannot stop the batch. The cost is that invalid SQL is silently repaired: sqlglot
    drops the tokens it cannot place and returns a partial AST, and lineage built from it is
    indistinguishable from lineage built from valid SQL. A dropped token can be a WHERE, a
    JOIN or a field expression — the artifact can describe a query that would not run at all
    (PARSE-002).

    A strict parse answers the one question the lenient parse cannot: was anything repaired?
    It only classifies; the lenient AST is still what gets used, so no lineage is lost.
    """
    normalized, _ = repair_keyword_identifiers(
        repair_ctas_missing_as(_normalize_directory_insert_sql(sql))[0]
    )
    try:
        sqlglot.parse(normalized, dialect=DIALECT, error_level=ErrorLevel.RAISE)
    except ParseError as exc:
        raw = getattr(exc, "errors", None) or [{"description": str(exc)}]
        return "recovered", _ordered_syntax_errors([
            {key: item[key] for key in _SYNTAX_ERROR_KEYS if key in item} for item in raw
        ])
    except Exception as exc:  # tokenizer-level failures raise their own types
        return "recovered", [{"description": f"{type(exc).__name__}: {exc}"}]
    return "strict_ok", []


def parse_scope_lineage(
    sql: str,
    task_name: str,
    schema: dict | None = None,
    target_metadata=None,
    *,
    tree: exp.Expression | None = None,
    regex_columns_enabled: bool | None = None,
) -> ScopeLineageResult:
    """Parse SQL into a scope-based lineage result with full column resolution.

    ``tree`` lets a caller that has already parsed the statement hand over that AST instead
    of having it re-parsed from ``sql``. Serializing an AST and parsing it back is not
    lossless: sqlglot hoists a WITH carried by an individual UNION branch to statement level
    and concatenates the clauses, so same-named CTEs from different branches shadow each
    other and qualify then fails on a column the shadowed one owned — degrading the whole
    statement to an unqualified parse (ROUNDTRIP-001). ``sql`` is still used for the syntax
    check and for statement identity.
    """
    schema = _prepare_schema(schema)
    # A caller handing us a tree has already split the script and must fold the session
    # settings itself -- it is the one holding them. Guessing here instead made the task
    # document expand a regex projection the statement document declined, from one SET
    # (SESSION-001). None means "not told"; only then do we fold from `sql` ourselves.
    enabled = True if regex_columns_enabled is None else regex_columns_enabled
    if tree is None:
        insert_trees, skipped_statements, single_flags = _collect_insert_trees(sql)
        if not insert_trees:
            raise NoSupportedWriteStatementError(skipped_statements)
        # For now, handle the first INSERT/MERGE only (multi-statement later)
        tree = insert_trees[0]
        enabled = single_flags[0] if single_flags else DEFAULT_QUOTED_REGEX_COLUMN_NAMES
    statement_identity_sql = render_sql_or_none(tree) or ""

    if _is_ctas(tree):
        result = _build_ctas_scope(tree, task_name, schema, regex_columns_enabled=enabled)
    elif isinstance(tree, exp.Merge) or (
        tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
    ):
        result = _build_merge_scope(tree, task_name, schema, regex_columns_enabled=enabled, target_metadata=target_metadata)
    else:
        # Same boundary parse_all_scope_lineage has had: a statement whose scope build raises
        # comes back marked instead of taking the caller down. The single-statement entry point
        # had no such guard, so a tree sqlglot could parse but not render -- CAST(out AS DOUBLE)
        # yields a Cast whose `to` is None, and every one of the 55 render sites dereferences it
        # eventually -- escaped as an AttributeError (REGEN-001). Guarding one boundary rather
        # than each render site: rendering is not the only thing that can fail on a repaired tree.
        try:
            if target_metadata is None:
                result = _build_insert_scope(tree, task_name, schema, regex_columns_enabled=enabled)
            else:
                result = _build_insert_scope(
                    tree,
                    task_name,
                    schema,
                    target_metadata=target_metadata,
                    regex_columns_enabled=enabled,
                )
        except (ValueError, NoSupportedWriteStatementError):
            # This package raises these deliberately to mean "refuse to emit lineage rather
            # than emit something wrong", and tests pin the messages. They are answers, not
            # accidents, and must reach the caller unchanged. (MetadataFileError belongs to
            # metadata loading, which happens before this and never reaches here.)
            raise
        except Exception as exc:  # noqa: BLE001 - mirrors the batch boundary below
            result = ScopeLineageResult(
                task_id=task_name,
                target_table=_target_table_name_for_error_result(tree),
                stmt_kind=_stmt_kind_for_tree(tree),
                parse_status="failed",
            )
            result.diagnostics.warnings.append(
                DiagnosticWarning(
                    type="LINEAGE_ERROR",
                    scope="ROOT",
                    msg=f"{type(exc).__name__}: {exc}",
                )
            )
    result.syntax_status, result.syntax_errors = _syntax_status(sql)
    _mark_gaps_from_recovered_syntax(result)
    result.statement_identity_sql = statement_identity_sql
    return result


def parse_all_scope_lineage(
    sql: str,
    task_name: str,
    schema: dict | None = None,
    target_metadata=None,
) -> list[ScopeLineageResult]:
    """Parse all INSERT/MERGE statements; return one ScopeLineageResult per target."""
    schema = _prepare_schema(schema)
    insert_trees, skipped_statements, regex_flags = _collect_insert_trees(sql)
    if not insert_trees:
        raise NoSupportedWriteStatementError(skipped_statements)

    # One strict parse for the whole text: sqlglot recovers at statement level, so a repair
    # anywhere taints every statement the lenient parse produced from it.
    syntax_status, syntax_errors = _syntax_status(sql)

    results: list[ScopeLineageResult] = []
    script_local: dict[str, list[str]] = {}
    for i, tree in enumerate(insert_trees):
        sub = f"{task_name}#{i}" if len(insert_trees) > 1 else task_name
        statement_identity_sql = render_sql_or_none(tree) or ""
        stmt_schema = _schema_with_script_local_tables(schema, script_local)
        enabled = regex_flags[i] if i < len(regex_flags) else True
        try:
            if _is_ctas(tree):
                results.append(
                    _build_ctas_scope(tree, sub, stmt_schema, regex_columns_enabled=enabled)
                )
            elif isinstance(tree, exp.Merge) or (
                tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
            ):
                results.append(
                    _build_merge_scope(
                        tree, sub, stmt_schema,
                        regex_columns_enabled=enabled, target_metadata=target_metadata,
                    )
                )
            else:
                if target_metadata is None:
                    results.append(
                        _build_insert_scope(
                            tree, sub, stmt_schema, regex_columns_enabled=enabled
                        )
                    )
                else:
                    results.append(
                        _build_insert_scope(
                            tree,
                            sub,
                            stmt_schema,
                            target_metadata=target_metadata,
                            regex_columns_enabled=enabled,
                        )
                    )
        except Exception as e:
            stmt_kind = _stmt_kind_for_tree(tree)
            target_table = _target_table_name_for_error_result(tree)
            result = ScopeLineageResult(
                task_id=sub, target_table=target_table, stmt_kind=stmt_kind, parse_status="failed"
            )
            result.diagnostics.warnings.append(
                DiagnosticWarning(
                    type="LINEAGE_ERROR",
                    scope="ROOT",
                    msg=f"{type(e).__name__}: {e}",
                )
            )
            results.append(result)
        results[-1].statement_identity_sql = statement_identity_sql
        script_local_schema(schema, script_local, results[-1])
    for result in results:
        result.syntax_status = syntax_status
        result.syntax_errors = syntax_errors
        _mark_gaps_from_recovered_syntax(result)
        # A skipped statement must remain visible on every artifact this script produced: a
        # consumer cannot otherwise tell one INSERT from one INSERT plus three DELETEs, and
        # "not modeled" would look the same as "not present" (CONTRACT-001).
        result.skipped_statements = list(skipped_statements)
        for item in skipped_statements:
            if item["category"] in {"control_statement", "empty_statement"}:
                # Ignored by design, and already recorded above with that category and its
                # SQL. Calling it "unsupported" made config and empty statements the
                # largest source of warnings in a run while telling a consumer nothing to
                # act on, and contradicted the task document, which marks the same
                # statements "ignored" (task_lineage.py). The record stays; the misnomer
                # goes.
                continue
            result.diagnostics.warnings.append(DiagnosticWarning(
                type="unsupported_statement",
                scope="ROOT",
                msg=f"{item['statement_kind']} 语句未解析({item['reason']});"
                    f"支持的写表语句: {item['supported']}",
            ))
    return results


def _target_table_name_for_error_result(tree: exp.Expression) -> str:
    if _is_ctas(tree):
        create = tree if isinstance(tree, exp.Create) else tree.find(exp.Create)
        target = _unwrap_target(create.this) if create and create.this is not None else None
        return _qualified_table(target) if isinstance(target, exp.Table) else ""
    insert = tree if isinstance(tree, exp.Insert) else tree.find(exp.Insert)
    if insert is not None:
        return _insert_target_name(insert)
    merge = tree if isinstance(tree, exp.Merge) else tree.find(exp.Merge)
    if merge is not None:
        target = _unwrap_target(merge.this) if merge.this is not None else None
        return _qualified_table(target) if isinstance(target, exp.Table) else ""
    return ""


def _target_partition_facts_from_insert(insert: exp.Insert | None) -> tuple[dict[str, str | None], list[str], str]:
    if insert is None or insert.this is None:
        return {}, [], "none"
    target = insert.this
    if not isinstance(target, exp.Table):
        target = _unwrap_target(target)
    partition = target.args.get("partition") if isinstance(target, exp.Table) else None
    return _partition_facts_from_partition(partition)


def _target_partition_facts_from_create(create: exp.Create | None) -> tuple[dict[str, str | None], list[str], str]:
    if create is None:
        return {}, [], "none"
    properties = create.args.get("properties")
    for prop in getattr(properties, "expressions", []) or []:
        if isinstance(prop, exp.PartitionedByProperty):
            columns = _columns_from_partitioned_by_property(prop)
            return _partition_facts_from_columns(columns)
    return {}, [], "none"


def _columns_from_partitioned_by_property(prop: exp.PartitionedByProperty) -> list[str]:
    node = prop.this
    expressions = getattr(node, "expressions", None) or []
    columns: list[str] = []
    for item in expressions:
        column = item.this if isinstance(item, exp.ColumnDef) else item
        name = _partition_column_name(column)
        if name:
            columns.append(name)
    return columns


def _partition_facts_from_partition(partition: exp.Partition | None) -> tuple[dict[str, str | None], list[str], str]:
    if partition is None:
        return {}, [], "none"

    spec: dict[str, str | None] = {}
    columns: list[str] = []
    for item in partition.expressions:
        if isinstance(item, exp.EQ):
            key = _partition_column_name(item.this)
            value = _partition_value(item.expression)
        else:
            key = _partition_column_name(item)
            value = None
        if not key:
            continue
        spec[key] = value
        columns.append(key)
    return spec, columns, _partition_mode(spec)


def _partition_facts_from_columns(columns: list[str]) -> tuple[dict[str, str | None], list[str], str]:
    spec = {column: None for column in columns if column}
    return spec, list(spec), _partition_mode(spec)


def _partition_column_name(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Identifier):
        return str(node.this or "")
    if isinstance(node, exp.ColumnDef):
        return _partition_column_name(node.this)
    return node.sql(dialect=DIALECT).strip().strip("`")


def _partition_value(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return str(node.this)
    return node.sql(dialect=DIALECT)


def _partition_mode(spec: dict[str, str | None]) -> str:
    if not spec:
        return "none"
    static_count = sum(value is not None for value in spec.values())
    if static_count == len(spec):
        return "static"
    if static_count == 0:
        return "dynamic"
    return "mixed"


# Spark puts a GLOBAL TEMPORARY VIEW in this database, and the bare name it was declared
# with does not resolve. Every read of it is therefore written `global_temp.<name>`.
GLOBAL_TEMP_DATABASE = "global_temp"


def _global_temp_qualified(target_table: str, definition: exp.Expression | None) -> str:
    """The name a GLOBAL TEMPORARY VIEW can actually be read by.

    Recording the relation under its declared bare name meant the statement reading it --
    necessarily qualified -- matched nothing: the read looked like an ordinary physical table,
    a consumer excluding session-scoped relations kept it, and metadata was reported missing
    for a table that does not exist (TEMPVIEW-002).

    This is the identity half of the judgement whose persistence half was fixed earlier. That
    one had been keyed on which keyword produced the relation; this one was still keyed on how
    the relation was spelled where it was declared, rather than on how it can be referred to.
    """
    if not target_table or "." in target_table:
        return target_table
    if not isinstance(definition, exp.Create):
        return target_table
    properties = definition.args.get("properties")
    if properties is None:
        return target_table
    if not any(isinstance(prop, exp.GlobalProperty) for prop in properties.expressions):
        return target_table
    return f"{GLOBAL_TEMP_DATABASE}.{target_table}"


def _build_ctas_scope(
    tree: exp.Expression, task_name: str, schema: dict | None = None,
    *, regex_columns_enabled: bool = True,
) -> ScopeLineageResult:
    # CREATE ... AS SELECT and CACHE [LAZY] TABLE ... AS SELECT define a relation the same
    # way; only persistence differs, and that is carried by is_cached_relation rather than
    # by a separate build path (CACHE-001).
    cached = _is_cache_as_select(tree)
    definition = tree if isinstance(tree, (exp.Create, exp.Cache)) else (
        tree.find(exp.Create) or tree.find(exp.Cache)
    )
    target = _unwrap_target(definition.this) if definition and definition.this is not None else None
    target_table = _qualified_table(target) if isinstance(target, exp.Table) else ""
    target_table = _global_temp_qualified(target_table, definition)
    partition_spec, partition_columns, partition_mode = (
        _target_partition_facts_from_create(definition)
        if isinstance(definition, exp.Create)
        else ({}, [], "none")
    )

    result = ScopeLineageResult(
        task_id=task_name,
        target_table=target_table,
        stmt_kind="CTAS",
        is_cached_relation=cached,
        is_session_scoped_relation=_is_session_scoped_relation(definition)
        if definition is not None
        else False,
        target_partition_spec=partition_spec,
        target_partition_columns=partition_columns,
        target_partition_mode=partition_mode,
        diagnostics=Diagnostics(),
    )

    if definition is None or definition.expression is None:
        return result

    src_expr = definition.expression.copy()
    qualified, qualify_ok = _qualify_ast(src_expr)

    if not qualify_ok:
        result.diagnostics.fallback_used = True

    _build_result_from_scope(
        qualified, result, target_table, schema,
        regex_columns_enabled=regex_columns_enabled,
    )
    _drop_dangling_column_refs(result)
    result.diagnostics.stats = _compute_stats(result)
    detect_warnings(result)
    infer_roles(result)
    return result


def _qualify_ast(ast: exp.Expression) -> tuple[exp.Expression, bool]:
    """Run sqlglot qualify with graceful degradation, and say whether it worked.

    Success cannot be read from the returned object: ``qualify`` mutates the tree it is
    given and hands back that same object, so an identity comparison against the input is
    true either way. Callers used one, so the "did it fail?" branch ran on every statement
    and qualified it a second time to learn what the first call already knew
    (QUALIFY-001).
    """
    # ``qualify`` assigns generated aliases (usually ``_col_N``) to anonymous
    # expressions. Preserve whether an alias existed in the source SQL so the
    # projection resolver can recover a sole referenced field without confusing
    # an explicit alias named ``_col_N`` with a generated placeholder.
    for select in ast.find_all(exp.Select):
        for projection in select.expressions:
            if not isinstance(projection, (exp.Alias, exp.Column, exp.Star)):
                projection.meta[_ORIGINALLY_ANONYMOUS_PROJECTION_META] = True

    # sqlglot may qualify an originally bare column to a CTE whose fields are known while
    # ignoring a joined physical table whose schema is unknown. Preserve the SQL author's
    # qualification intent so the resolver can consider every viable source (LINEAGE-002).
    for column in ast.find_all(exp.Column):
        if not column.table:
            column.meta[_ORIGINALLY_UNQUALIFIED_META] = True
    try:
        return sg_qualify(
            ast,
            dialect=DIALECT,
            validate_qualify_columns=False,
            infer_schema=True,
            expand_stars=False,
        ), True
    except Exception:
        return ast, False


def _build_insert_scope(
    tree: exp.Expression,
    task_name: str,
    schema: dict | None = None,
    *,
    target_metadata=None,
    regex_columns_enabled: bool = True,
) -> ScopeLineageResult:
    """Build scope tree for INSERT statements."""
    insert = tree if isinstance(tree, exp.Insert) else tree.find(exp.Insert)
    target_table = _insert_target_name(insert)
    partition_spec, partition_columns, partition_mode = _target_partition_facts_from_insert(insert)

    is_overwrite = bool(insert.args.get("overwrite"))
    stmt_kind = "INSERT_OVERWRITE" if is_overwrite else "INSERT"

    result = ScopeLineageResult(
        task_id=task_name,
        target_table=target_table,
        stmt_kind=stmt_kind,
        target_partition_spec=partition_spec,
        target_partition_columns=partition_columns,
        target_partition_mode=partition_mode,
        diagnostics=Diagnostics(),
    )

    if insert.expression is None:
        return result

    src_expr = _build_source_expression(insert, target_metadata=target_metadata)
    qualified, qualify_ok = _qualify_ast(src_expr)

    if not qualify_ok:
        result.diagnostics.fallback_used = True

    _build_result_from_scope(
        qualified,
        result,
        target_table,
        schema,
        target_metadata=target_metadata,
        explicit_target_columns=_explicit_insert_target_columns(insert),
        insert_by_name=bool(insert.args.get("by_name")),
        regex_columns_enabled=regex_columns_enabled,
    )
    _drop_dangling_column_refs(result)
    result.diagnostics.stats = _compute_stats(result)
    detect_warnings(result)
    infer_roles(result)
    return result


def _insert_target_name(insert: exp.Insert) -> str:
    target = _unwrap_target(insert.this) if insert.this is not None else None
    if isinstance(target, exp.Table):
        return _qualified_table(target)
    if isinstance(target, exp.Directory):
        path_expr = target.this
        if isinstance(path_expr, exp.Literal):
            path = str(path_expr.this or "").strip()
        else:
            path = path_expr.sql(dialect=DIALECT).strip().strip("'\"") if path_expr is not None else ""
        return f"directory:{path}" if path else "directory:unknown"
    return ""


def _explicit_insert_target_columns(insert: exp.Insert) -> list[str]:
    target = insert.this
    if not isinstance(target, exp.Schema):
        return []
    return [
        str(column.name or "").strip().lower()
        for column in target.expressions
        if getattr(column, "name", None)
    ]



def _stmt_kind_for_tree(tree: exp.Expression) -> str:
    """The statement kind to record when the scope build raised.

    This chose between CTAS and INSERT only, so a MERGE that failed to build was labelled
    INSERT — a wrong fact on the artifact, even though everything around it (parse_status,
    empty scopes, the diagnostic) was honest (MERGE-001).
    """
    if _is_ctas(tree):
        return "CTAS"
    if isinstance(tree, exp.Merge) or (
        tree.find(exp.Merge) is not None and tree.find(exp.Insert) is None
    ):
        return "MERGE"
    return "INSERT"


def _merge_with_subquery_source(merge: exp.Merge) -> exp.Merge:
    """Rewrite `USING <table>` into `USING (SELECT * FROM <table>)`, preserving the alias.

    The scope machinery expects the USING side to be a query block, because that is what a
    subquery source produces. A plain table reference — valid Spark, just absent from this
    corpus — left it with no scope to walk and the build raised, so the whole statement came
    back `parse_status=failed` with no lineage at all (MERGE-001).

    Rewriting is preferable to adding a second code path: `SELECT * FROM t` is exactly what the
    table reference means, so column resolution, star expansion and every downstream fact keep
    working unchanged.
    """
    using = merge.args.get("using")
    table = using.this if isinstance(using, exp.Alias) else using
    if not isinstance(table, exp.Table):
        return merge
    alias = table.alias or (using.alias if isinstance(using, exp.Alias) else "") or table.name
    rewritten = merge.copy()
    source = exp.Table(this=table.this, db=table.args.get("db"), catalog=table.args.get("catalog"))
    rewritten.set(
        "using",
        exp.Subquery(this=exp.Select().select(exp.Star()).from_(source), alias=exp.TableAlias(this=exp.to_identifier(alias))),
    )
    return rewritten


def _build_merge_scope(
    tree: exp.Expression, task_name: str, schema: dict | None = None,
    *, regex_columns_enabled: bool = True, target_metadata=None,
) -> ScopeLineageResult:
    """Build scope tree for MERGE statements.

    build_scope on the full MERGE AST produces:
      ROOT (Subquery expression) -> child SUBQUERY scope (the USING Select)
    """
    merge = tree if isinstance(tree, exp.Merge) else tree.find(exp.Merge)
    target = _unwrap_target(merge.this) if merge.this is not None else None
    target_table = _qualified_table(target) if isinstance(target, exp.Table) else ""

    result = ScopeLineageResult(
        task_id=task_name,
        target_table=target_table,
        stmt_kind="MERGE",
        diagnostics=Diagnostics(),
    )

    using = merge.args.get("using")
    if using is None:
        return result
    merge = _merge_with_subquery_source(merge)

    protected = _protect_merge_correlated_target_refs(merge)
    qualified, _qualify_ok = _qualify_ast(merge)
    _restore_merge_correlated_target_refs(qualified, protected)
    # Only the column list, and only for the `*` branches. Deliberately not routed
    # through apply_target_field_binding: that pass runs before MERGE's ROOT columns
    # exist, so it would bind against an empty projection and report every MERGE as a
    # count-mismatch fallback.
    merge_metadata = (
        lookup_target_table_metadata(target_metadata, target_table)
        if target_metadata is not None
        else None
    )
    _build_result_from_scope(
        qualified, result, target_table, schema,
        regex_columns_enabled=regex_columns_enabled,
        merge_target_columns=(
            [c.name for c in merge_metadata.columns] if merge_metadata else None
        ),
    )
    _drop_dangling_column_refs(result)
    result.diagnostics.stats = _compute_stats(result)
    detect_warnings(result)
    infer_roles(result)
    return result


_MERGE_TARGET_REF_SENTINEL = "__scope_lineage_merge_target_ref_{}__"


def _merge_correlated_target_ref_regions(merge: exp.Merge) -> list[exp.Expression]:
    """The only two places a correlated MERGE-target reference can legally appear.

    Deliberately excludes ``using`` and ``with``: a ``target.x`` written there is not
    correlated, it is out of scope, and it must keep producing an ordinary unresolved
    diagnostic instead of being silently rewritten into something that resolves.
    """
    regions = [merge.args.get("on"), merge.args.get("whens")]
    return [region for region in regions if isinstance(region, exp.Expression)]


def _is_nested_target_ref(column: exp.Column, region: exp.Expression) -> bool:
    """True when ``column`` sits inside a query nested under ``region``.

    A direct ``target.id`` in ON, or an UPDATE left value, qualifies correctly on its
    own. Only references that sqlglot resolves against a *nested* query's local sources
    get misbound, so widening this predicate would rewrite references that were never
    broken.
    """
    node = column.parent
    while node is not None and node is not region:
        if isinstance(node, exp.Select):
            return True
        node = node.parent
    return False


def _protect_merge_correlated_target_refs(merge: exp.Expression) -> dict[str, exp.Column]:
    """Swap correlated MERGE-target references for inert sentinel literals.

    sqlglot 30.x rewrites ``target.id`` inside an action scalar subquery to
    ``lookup.target.id``, treating the explicit target alias as a struct field on the
    subquery's local table. The author's binding has to survive qualify.

    Pairing the pre- and post-qualify ``find_all(exp.Column)`` traversals by position
    cannot do that: qualify reorders the traversal (a leading WITH block moves behind
    the MERGE body) while keeping the count identical, so a count check passes and the
    positional pairing pastes the action's ``target.*`` onto unrelated CTE projections
    and neighbouring UPDATE assignments (MERGE-CTE-001).

    Tagging the node through ``meta`` does not survive either: the misqualified node is
    a new node rather than the original mutated in place, so the tag is gone exactly
    where it is needed. An inert string literal is the one marker qualify leaves alone.
    """
    if not isinstance(merge, exp.Merge):
        return {}
    target = _unwrap_target(merge.this)
    if not isinstance(target, exp.Table):
        return {}
    target_qualifiers = {target.alias_or_name, target.name}
    existing_literals = {
        literal.this for literal in merge.find_all(exp.Literal) if literal.is_string
    }
    protected: dict[str, exp.Column] = {}
    for region in _merge_correlated_target_ref_regions(merge):
        for column in list(region.find_all(exp.Column)):
            if len(column.parts or []) != 2 or column.table not in target_qualifiers:
                continue
            if not _is_nested_target_ref(column, region):
                continue
            # Sequential, so the same SQL always yields the same sentinels and the
            # same output bytes.
            token = _MERGE_TARGET_REF_SENTINEL.format(len(protected))
            if token in existing_literals:
                raise ValueError(
                    "MERGE target-reference sentinel collides with a literal in the SQL"
                )
            protected[token] = column.copy()
            column.replace(exp.Literal.string(token))
    return protected


def _restore_merge_correlated_target_refs(
    qualified_merge: exp.Expression,
    protected: dict[str, exp.Column],
) -> None:
    """Put every protected reference back, or fail rather than publish a guess.

    A sentinel that vanished or multiplied means the AST is no longer the one that was
    protected. Falling back to positional guessing is what produced MERGE-CTE-001, so
    this stops the statement instead; callers that model whole scripts already record
    that as ``parse_status="failed"``.
    """
    if not protected:
        return
    found: dict[str, list[exp.Literal]] = {}
    for literal in qualified_merge.find_all(exp.Literal):
        if literal.is_string and literal.this in protected:
            found.setdefault(literal.this, []).append(literal)
    for token, column in protected.items():
        hits = found.get(token, [])
        if len(hits) != 1:
            raise ValueError(
                "MERGE target reference could not be restored after qualify: "
                f"sentinel found {len(hits)} times, expected exactly 1"
            )
        hits[0].replace(column.copy())


def _looks_schema_expanded_from_physical(scope_data) -> bool:
    """True when this scope's columns came from expanding `SELECT *` over a physical table.

    A missing column on such a scope means the schema metadata does not list it — a metadata
    coverage gap, which is actionable ("supply the column"). Rewriting those refs to UNKNOWN
    would destroy that signal, so the sweep below leaves them alone; the audit still reports
    them, as `schema_incomplete_column_ref` rather than a structural break.
    """
    columns = scope_data.columns or []
    if not columns:
        return False
    physical_sources = set()
    direct_from_physical = 0
    for col in columns:
        for source in col.sources or []:
            scope_name = source.scope or ""
            if scope_name.startswith(("cte:", "subq:", "union:", "udtf:", "ROOT", "UNKNOWN")):
                continue
            physical_sources.add(scope_name)
            if (
                col.transform == "DIRECT"
                and col.name == source.column
                and col.expression in (None, "", col.name)
            ):
                direct_from_physical += 1
    if not physical_sources:
        return False
    return direct_from_physical >= max(3, len(columns) // 2)


def _drop_dangling_column_refs(result: ScopeLineageResult) -> None:
    """Rewrite refs to columns their target scope does not actually expose, to UNKNOWN.

    Column resolution runs per scope, so whether an upstream scope has materialized its own
    columns yet depends on traversal order. Every ordering-sensitive branch that guesses
    "this scope probably exposes the column" can be wrong, and a wrong guess is not a small
    error: a source ref that names a scope is read downstream as an established fact, and
    nothing re-checks it. Once every scope is built the column lists are authoritative, so
    this single order-independent sweep is what makes the guarantee hold — no matter which
    branch produced the ref (LINEAGE-001).

    UNKNOWN is the honest answer: the audit reports it, and it cannot be mistaken for lineage.
    """
    exposed = {
        scope_id: {col.name for col in (scope_data.columns or [])}
        for scope_id, scope_data in result.scopes.items()
    }
    exempt = {
        scope_id for scope_id, scope_data in result.scopes.items()
        if _looks_schema_expanded_from_physical(scope_data)
    }
    for scope_id, scope_data in result.scopes.items():
        for column in list(scope_data.columns or []) + list(getattr(scope_data, "outputs", None) or []):
            for source in column.sources or []:
                target = source.scope
                if (
                    target in exposed
                    and target not in exempt
                    and source.column not in exposed[target]
                    and source.column != "*"
                    and column.name != "*"
                ):
                    result.diagnostics.warnings.append(DiagnosticWarning(
                        type="dangling_column_ref_dropped",
                        scope=scope_id,
                        msg=f"Column '{column.name}' pointed at {target}.{source.column}, "
                            f"which that scope does not output; left unresolved",
                    ))
                    source.scope = "UNKNOWN"


def _build_result_from_scope(
    qualified_expr, result: ScopeLineageResult, target_table: str,
    schema: dict | None = None,
    regex_columns_enabled: bool = True,
    merge_target_columns: list[str] | None = None,
    *,
    target_metadata=None,
    explicit_target_columns: list[str] | None = None,
    insert_by_name: bool = False,
) -> None:
    """Common logic: assign IDs, create stubs, collect physical tables, resolve columns.

    Uses traverse_scope(qualified_expr) to build the scope list so that CTE scopes
    inside MERGE...WITH are not missed (build_scope().traverse() silently skips them
    for MERGE statements). MERGE's ROOT is synthetic: SQLGlot 30.16 emitted root
    Subquery wrappers for its query fragments, while 30.17 deliberately stopped doing so.
    """
    all_scopes = list(traverse_scope(qualified_expr))
    merge_node = qualified_expr if isinstance(qualified_expr, exp.Merge) else None
    using_node = merge_node.args.get("using") if merge_node is not None else None

    if merge_node is not None:
        # SQLGlot <=30.16 yielded an is_root Subquery wrapper for every value-position
        # query in a MERGE, including scalar queries inside WHEN actions. Those wrappers
        # disappeared in 30.17 and were never distinct contract scopes: keeping them made
        # a second scalar query surface as ROOT_2 on only the older version.
        all_scopes = [
            scope
            for scope in all_scopes
            if not (scope.is_root and isinstance(scope.expression, exp.Subquery))
        ]

    root_scope = next((s for s in reversed(all_scopes) if s.is_root), None)
    if merge_node is None and root_scope is None:
        raise ValueError("sqlglot produced no root scope for a non-MERGE write query")

    using_query = using_node.unnest() if isinstance(using_node, exp.Subquery) else using_node
    merge_using_scope = (
        next((scope for scope in all_scopes if scope.expression is using_query), None)
        if merge_node is not None
        else None
    )
    if merge_node is not None and merge_using_scope is None:
        raise ValueError("sqlglot produced no scope for the MERGE USING relation")

    if merge_node is not None:
        merge_target = _unwrap_target(merge_node.this)
        if isinstance(merge_target, exp.Table):
            target_alias = merge_target.alias_or_name
            for scope in all_scopes:
                # Scalar queries inside WHEN actions may correlate to the MERGE target,
                # but SQLGlot's optimizer scope has the DML node rather than a query scope
                # as its parent. Supply that explicit binding so target refs do not become
                # UNKNOWN or bind to the scalar query's local table.
                has_correlated_target_ref = any(
                    column.table == target_alias
                    and not _inside_nested_subquery(scope.expression, column)
                    for column in scope.expression.find_all(exp.Column)
                )
                if (
                    scope.expression.find_ancestor(exp.When) is not None
                    and target_alias not in scope.sources
                    and has_correlated_target_ref
                ):
                    scope.sources.setdefault(target_alias, merge_target)

    # Step 1: Assign scope_ids to every scope (children before parents — traverse_scope order)
    for sg_scope in all_scopes:
        scope_id = _compute_scope_id(sg_scope)
        setattr(sg_scope, _SCOPE_ID_ATTR, scope_id)

    # Step 1b: Deduplicate IDs — same alias at different nesting levels must not collide.
    # Process in all_scopes order (bottom-up): first occurrence keeps natural ID,
    # subsequent occurrences get _2, _3, etc.
    _seen_ids: dict[str, int] = {}
    for sg_scope in all_scopes:
        sid = getattr(sg_scope, _SCOPE_ID_ATTR)
        count = _seen_ids.get(sid, 0) + 1
        _seen_ids[sid] = count
        if count > 1:
            setattr(sg_scope, _SCOPE_ID_ATTR, f"{sid}_{count}")

    # Step 2: Create synthetic UNION scopes for any scope with Union expression + union_scopes
    if root_scope:
        _create_union_scopes_recursive(root_scope, result)
    for sg_scope in all_scopes:
        if (
            not sg_scope.is_union
            and isinstance(sg_scope.expression, exp.Union)
            and sg_scope.union_scopes
        ):
            union_scope_id = _union_scope_id_for_container(
                getattr(sg_scope, _SCOPE_ID_ATTR, None)
            )
            if union_scope_id not in result.scopes:
                _create_union_scope(sg_scope, result)

    # Step 3: Create ScopeData stubs for each scope
    # Skip all is_union scopes: they are handled entirely by _create_union_scope.
    # - Leaf branches got their real "union:xxx:bNN" IDs assigned in Step 2.
    # - Intermediate Union scopes still have "_union_tmp_*" placeholder IDs.
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if scope_id is None:
            continue

        # Skip all is_union scopes — handled by _create_union_scope
        if sg_scope.is_union:
            continue

        kind = _scope_kind(sg_scope)
        alias_in_parent = _find_alias_in_parent(sg_scope)

        if scope_id in result.scopes:
            # Already created by _create_union_scopes_recursive (e.g. union scope or branch)
            if alias_in_parent:
                result.scopes[scope_id].alias_in_parent = alias_in_parent
            result.scopes[scope_id].distinct = _scope_has_distinct(sg_scope)
            continue

        result.scopes[scope_id] = ScopeData(
            kind=kind,
            distinct=_scope_has_distinct(sg_scope),
            alias_in_parent=alias_in_parent,
        )

    # Ensure ROOT exists
    if "ROOT" not in result.scopes:
        result.scopes["ROOT"] = ScopeData(kind="root")
    result.scopes["ROOT"].writes_to = target_table
    if using_node is not None:
        result.scopes["ROOT"].raw_sql = using_node.sql(dialect=DIALECT)
        result.scopes["ROOT"].raw_sql_available = bool(result.scopes["ROOT"].raw_sql)

    # Step 4: Collect physical tables from each scope's resolved bindings. CTE names
    # are lexical: an inner ``WITH staging`` must not make a physical ``staging`` in a
    # sibling scope disappear. A single AST-wide name set cannot represent that rule.
    physical_tables = _physical_tables_from_scopes(all_scopes)

    result.source_tables = sorted(physical_tables)
    all_nodes = set(result.scopes.keys()) | physical_tables
    result.scope_graph.nodes = sorted(all_nodes)

    # The USING relation's scope ID, recorded once here because it is only knowable
    # while the sqlglot scopes are in hand. Task-level modelling needs it to resolve
    # MERGE condition aliases through proven scope facts instead of re-guessing which
    # table an alias meant (MERGE-CTE-002).
    if merge_using_scope is not None:
        result.merge_using_scope_id = getattr(merge_using_scope, _SCOPE_ID_ATTR, "") or ""
    if merge_node is not None:
        merge_target_relation = _unwrap_target(merge_node.this)
        if isinstance(merge_target_relation, exp.Table):
            result.merge_target_alias = (
                merge_target_relation.alias_or_name or merge_target_relation.name
            )
        merge_using_relation = merge_node.args.get("using")
        if isinstance(merge_using_relation, exp.Expression):
            result.merge_using_alias = merge_using_relation.alias_or_name or "source"

    # Step 5: Resolve columns for all scopes
    resolve_all(
        result,
        all_scopes,
        schema,
        target_metadata=target_metadata,
        explicit_target_columns=explicit_target_columns,
        insert_by_name=insert_by_name,
        merge_node=merge_node,
        merge_using_scope=merge_using_scope,
        regex_columns_enabled=regex_columns_enabled,
        merge_target_columns=merge_target_columns,
    )
    _populate_enhanced_scope_facts(result, all_scopes, schema)
    result.related_metadata = build_related_metadata(result, schema)


def _physical_tables_from_scopes(all_scopes: list[Scope]) -> set[str]:
    """Return physical inputs while preserving lexical CTE and duplicate-alias binding."""
    physical_tables: set[str] = set()
    for scope in all_scopes:
        # Scope.sources has already resolved local CTE references to Scope objects and
        # physical relations to Table objects. It is authoritative for lexical binding.
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name:
                physical_tables.add(_qualified_table(source))

        # The mapping is keyed by alias and therefore retains only one item when invalid
        # or recovered SQL repeats an alias. Walk direct FROM/JOIN items as a safety net;
        # _source_item_from_ast_node still resolves CTE nodes by AST identity.
        expression = scope.expression
        if not isinstance(expression, exp.Select):
            continue
        source_nodes: list[exp.Expression] = []
        from_ = expression.args.get("from_")
        if from_ is not None and from_.this is not None:
            source_nodes.append(from_.this)
        source_nodes.extend(
            join.this for join in expression.args.get("joins") or [] if join.this is not None
        )
        for source_node in source_nodes:
            item = _source_item_from_ast_node(source_node, scope)
            if item is None:
                continue
            _alias, source = item
            if isinstance(source, exp.Table) and source.name:
                physical_tables.add(_qualified_table(source))
    return physical_tables


def _prepare_schema(schema: dict | None) -> dict | None:
    if schema is None or isinstance(schema, SchemaMap):
        return schema
    return normalize_schema_map(schema)


def _build_source_expression(
    insert: exp.Insert,
    *,
    target_metadata=None,
) -> exp.Expression:
    """Extract source expression from INSERT, with WITH grafted and wrappers unwrapped."""
    src = insert.expression.copy()
    w = insert.args.get("with_")

    if isinstance(src, exp.Values):
        return _wrap_top_level_values_source(
            insert,
            src,
            target_metadata=target_metadata,
        )

    if isinstance(src, exp.Subquery):
        inner = src.this
        if isinstance(inner, (exp.Select, exp.Union)):
            src = inner.copy()
            sw = src.args.get("with_")
            if sw is None and w is not None:
                src.set("with_", w.copy())
            return src

    if isinstance(src, exp.Select):
        from_ = src.args.get("from_")
        if from_ is not None:
            from_this = getattr(from_, "this", None)
            if isinstance(from_this, exp.Subquery) and isinstance(from_this.this, exp.Union):
                all_passthrough = all(
                    (
                        isinstance(p, (exp.Column, exp.Star)) or
                        (isinstance(p, exp.Alias) and isinstance(p.this, (exp.Column, exp.Star)))
                    )
                    # A star carrying EXCEPT (...) does not pass everything through -- it
                    # drops columns. Unwrapping it loses the exclusion exactly the way the
                    # comment below warns other clauses are lost, and the excluded column
                    # comes back as a published output field.
                    and not _star_modifiers(p)[0]
                    for p in src.expressions
                )
                # Unwrap ONLY pure wrappers. If the outer select also joins, filters,
                # groups, etc., unwrapping would silently discard those clauses —
                # including entire joined tables (seen in production SQL:
                # SELECT ... FROM (a UNION ALL b) t1 JOIN ods.x c1 ON ...).
                has_other_clauses = any(
                    src.args.get(key)
                    for key in ("joins", "where", "group", "having", "qualify",
                                "laterals", "distinct", "limit", "order", "windows")
                )
                if all_passthrough and not has_other_clauses:
                    union = from_this.this.copy()
                    if w is not None and union.args.get("with_") is None:
                        union.set("with_", w.copy())
                    return union

    if w is not None and isinstance(src, (exp.Select, exp.Union)):
        src.set("with_", w.copy())
    return src


def _wrap_top_level_values_source(
    insert: exp.Insert,
    values: exp.Values,
    *,
    target_metadata=None,
) -> exp.Select:
    """Give a top-level VALUES write the query scope expected by the resolver.

    ``SELECT * FROM VALUES`` is already a supported Spark shape.  A bare
    ``INSERT ... VALUES`` has the same row source but no root query scope in
    sqlglot, so wrap it and name the value columns deterministically.  The
    complete VALUES text remains on the child scope while ordinary target-field
    binding supplies authoritative output names when metadata is available.
    """
    rows = list(values.expressions or [])
    first_row = rows[0] if rows else None
    arity = len(first_row.expressions) if isinstance(first_row, exp.Tuple) else 0
    names = _top_level_values_column_names(
        insert,
        arity,
        target_metadata=target_metadata,
    )
    values.set(
        "alias",
        exp.TableAlias(
            this=exp.to_identifier("values_source"),
            columns=[exp.to_identifier(name) for name in names],
        ),
    )
    return exp.select("*").from_(values)


def _top_level_values_column_names(
    insert: exp.Insert,
    arity: int,
    *,
    target_metadata=None,
) -> list[str]:
    explicit = _explicit_insert_target_columns(insert)
    if len(explicit) == arity:
        return explicit

    target_table = _insert_target_name(insert)
    metadata = lookup_target_table_metadata(target_metadata, target_table)
    if metadata is not None and metadata.usable:
        static_partitions = {
            name
            for name, value in _target_partition_facts_from_insert(insert)[0].items()
            if value is not None
        }
        metadata_names = [
            column.name
            for column in metadata.columns
            if column.name not in static_partitions
        ]
        if len(metadata_names) == arity:
            return metadata_names

    return [f"value_{index + 1}" for index in range(arity)]


def _compute_scope_id(sg_scope: Scope) -> str:
    """Compute a scope_id for a single sqlglot Scope."""
    if sg_scope.is_root:
        return "ROOT"

    if sg_scope.is_cte:
        # CTE name from expression.parent.alias
        cte_node = sg_scope.expression.parent
        if hasattr(cte_node, "alias") and cte_node.alias:
            return f"cte:{cte_node.alias}"
        # Fallback: find in parent sources
        if sg_scope.parent:
            for name, src in sg_scope.parent.sources.items():
                if src is sg_scope:
                    return f"cte:{name}"
        return "cte:unknown"

    if sg_scope.is_union:
        # Placeholder: _create_union_scope will assign the real ID after flattening.
        # We use a temporary ID based on object identity to avoid collisions.
        return f"_union_tmp_{id(sg_scope)}"

    if sg_scope.is_derived_table or sg_scope.is_subquery:
        # Find alias in parent sources first
        alias = _find_alias_in_parent(sg_scope)
        if alias:
            return f"subq:{alias}"
        # A MERGE USING query keeps its alias on the AST wrapper. SQLGlot 30.17 no longer
        # creates the parent wrapper Scope, so reading only sg_scope.parent silently renamed
        # `subq:source` to the generated `subq:derived_0`.
        enclosing = sg_scope.expression.parent
        if isinstance(enclosing, exp.Subquery) and enclosing.alias:
            return f"subq:{enclosing.alias}"
        # Retain the Scope-based fallback for older non-MERGE shapes whose expression is not
        # directly enclosed by the aliased Subquery.
        if sg_scope.parent and isinstance(sg_scope.parent.expression, exp.Subquery):
            sq_alias = sg_scope.parent.expression.alias
            if sq_alias:
                return f"subq:{sq_alias}"
        return "subq:derived_0"

    if sg_scope.is_udtf:
        alias = _find_alias_in_parent(sg_scope)
        if alias:
            return f"udtf:{alias}"
        return "udtf:unknown_0"

    return "scope:unknown"


def _create_union_scopes_recursive(sg_scope: Scope, result: ScopeLineageResult) -> None:
    """Walk the scope tree and create synthetic UNION scopes wherever a scope has
    Union expression + union_scopes children.

    Handles both top-level UNION and UNION inside CTEs.
    UNION chains (A UNION ALL B UNION ALL C) are flattened into a single union scope
    with N branches, not nested union scopes.
    """
    # Check if this scope has UNION children
    if sg_scope.union_scopes and isinstance(sg_scope.expression, exp.Union):
        _create_union_scope(sg_scope, result)

    # Recurse into child scopes
    for child in sg_scope.cte_scopes:
        _create_union_scopes_recursive(child, result)
    for child in sg_scope.derived_table_scopes:
        _create_union_scopes_recursive(child, result)
    for child in sg_scope.subquery_scopes:
        _create_union_scopes_recursive(child, result)
    # Note: do NOT recurse into union_scopes here — _create_union_scope already
    # flattened the chain and assigned IDs to all leaf branches. Recursing into
    # union_scopes would re-enter scopes that have already been handled.
    for child in sg_scope.udtf_scopes:
        _create_union_scopes_recursive(child, result)


def _flatten_union_branches(sg_scope: Scope) -> list[Scope]:
    """Flatten a left-deep Union tree into a flat list of leaf SELECT scopes.

    sqlglot parses `A UNION ALL B UNION ALL C` as Union(Union(A, B), C),
    producing a nested scope tree. We want a single flat union scope with 3 branches.
    A branch that is itself a Union scope is recursively flattened.
    """
    leaves = []
    for branch in sg_scope.union_scopes:
        if isinstance(branch.expression, exp.Union) and branch.union_scopes:
            # Nested union — flatten it
            leaves.extend(_flatten_union_branches(branch))
        else:
            leaves.append(branch)
    return leaves


def _create_union_scope(container_scope: Scope, result: ScopeLineageResult) -> None:
    """Create a synthetic UNION scope for a scope that has Union expression + union_scopes.

    Flattens UNION chains so A UNION ALL B UNION ALL C produces one union scope
    with 3 branches, not nested union scopes.
    """
    # Flatten the left-deep union tree into leaf branches
    flat_branches = _flatten_union_branches(container_scope)
    if not flat_branches:
        return

    container_id = getattr(container_scope, _SCOPE_ID_ATTR, None)
    union_scope_id = _union_scope_id_for_container(container_id)
    context = union_scope_id.split(":", 1)[1]

    # Assign branch IDs to the flattened leaf branches
    branch_ids = []
    for i, branch in enumerate(flat_branches):
        branch_id = f"union:{context}:b{i + 1:02d}"
        setattr(branch, _SCOPE_ID_ATTR, branch_id)
        branch_ids.append(branch_id)

    # Also fix the _lineage_scope_id on intermediate Union scopes that we
    # flattened away — they should NOT appear as separate scopes in the result.
    # Point them to the union scope so they're treated as aliases.
    for branch in container_scope.union_scopes:
        if isinstance(branch.expression, exp.Union) and branch.union_scopes:
            setattr(branch, _SCOPE_ID_ATTR, union_scope_id)

    # Determine set_op type
    union_expr = container_scope.expression
    set_op = "UNION_ALL"
    if hasattr(union_expr, "args"):
        kind = union_expr.args.get("kind", "")
        if kind and "ALL" not in str(kind).upper():
            set_op = "UNION"

    result.scopes[union_scope_id] = ScopeData(
        kind="union",
        set_op=set_op,
        branches=branch_ids,
    )

    # Create branch stubs
    for i, branch_id in enumerate(branch_ids):
        if branch_id not in result.scopes:
            result.scopes[branch_id] = ScopeData(
                kind="union_branch",
                branch_index=i,
            )

    # If container is ROOT, also create the ROOT stub
    if container_id == "ROOT":
        if "ROOT" not in result.scopes:
            result.scopes["ROOT"] = ScopeData(kind="root")


def _union_scope_id_for_container(container_id: str | None) -> str:
    """Return the synthetic union scope ID for a container scope ID."""
    if not container_id or container_id == "ROOT":
        context = "main"
    elif ":" in container_id:
        context = container_id.split(":", 1)[1]
    else:
        context = container_id
    return f"union:{context}"


def _scope_kind(sg_scope: Scope) -> str:
    """Map sqlglot ScopeType to our kind string."""
    if sg_scope.is_root:
        return "root"
    if sg_scope.is_cte:
        return "cte"
    if sg_scope.is_union:
        return "union_branch"
    if sg_scope.is_derived_table:
        return "subquery"
    if sg_scope.is_subquery:
        return "subquery"
    if sg_scope.is_udtf:
        return "subquery"
    return "unknown"


def _scope_has_distinct(sg_scope: Scope) -> bool:
    """Return whether a sqlglot SELECT scope uses DISTINCT."""
    return isinstance(sg_scope.expression, exp.Select) and bool(
        sg_scope.expression.args.get("distinct")
    )


def _compute_stats(result: "ScopeLineageResult") -> dict:
    """Compute diagnostics.stats from the fully-built result."""
    cte_count = sum(1 for s in result.scopes.values() if s.kind == "cte")
    subquery_count = sum(1 for s in result.scopes.values() if s.kind == "subquery")
    union_count = sum(1 for s in result.scopes.values() if s.kind == "union")
    union_branch_count = sum(1 for s in result.scopes.values() if s.kind == "union_branch")

    physical_ids: set = set()
    for scope in result.scopes.values():
        for col in scope.columns:
            for src in col.sources:
                if src.scope and src.scope not in result.scopes and src.scope not in ("UNKNOWN", ""):
                    physical_ids.add(src.scope)
        for j in scope.joins:
            for sid in (j.left_scope, j.right_scope):
                if sid and sid not in result.scopes and sid not in ("UNKNOWN", ""):
                    physical_ids.add(sid)

    agg_count = window_count = case_count = join_count = 0
    for scope in result.scopes.values():
        join_count += len(scope.joins)
        for col in scope.columns:
            if col.transform == "AGGREGATE":
                agg_count += 1
            elif col.transform == "WINDOW":
                window_count += 1
            elif col.transform == "CONDITIONAL":
                case_count += 1

    def _depth(scope_id: str, memo: dict, visiting: set) -> int:
        if scope_id in memo:
            return memo[scope_id]
        if scope_id in visiting:   # cycle detected — return 0 to break recursion
            return 0
        visiting.add(scope_id)
        scope = result.scopes.get(scope_id)
        if scope is None:
            visiting.discard(scope_id)
            memo[scope_id] = 0
            return 0
        d = 1 + max((_depth(dep, memo, visiting) for dep in scope.depends_on), default=0)
        visiting.discard(scope_id)
        memo[scope_id] = d
        return d

    memo: dict = {}
    max_depth = max((_depth(sid, memo, set()) for sid in result.scopes), default=0)
    scope_count = len(result.scopes) + len(physical_ids)

    return {
        "scope_count": scope_count,
        "physical_table_count": len(physical_ids),
        "cte_count": cte_count,
        "subquery_count": subquery_count,
        "union_count": union_count,
        "union_branch_count": union_branch_count,
        "max_depth": max_depth,
        "case_when_count": case_count,
        "window_function_count": window_count,
        "join_count": join_count,
        "aggregate_function_count": agg_count,
    }


_LOGICBLOCKENGINE_INSTANCE = None


# Delegation shims preserve the free-function surface used by callers/tests.


# Delegation shims preserve the free-function surface used by callers/tests.
