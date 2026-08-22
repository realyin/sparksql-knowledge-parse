"""Task-level table-state lineage for ordered Spark SQL statements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

from ..metadata.schema_metadata import DictSchemaProvider
from ._shared import DIALECT, PARSE_OPTS, render_sql_or_none
from .end_to_end import _physical_fields_for_scope_column
from .scope_types import ScopeLineageResult
from .ctas_missing_as import repair_ctas_missing_as
from .keyword_identifiers import repair_keyword_identifiers
from .scope_builder import (
    _is_ctas,
    _schema_with_script_local_tables,
    script_local_schema,
    _normalize_directory_insert_sql,
    _qualified_table,
    _statement_category,
    _statement_kind_label,
    _stmt_kind_for_tree,
    _syntax_status,
    parse_scope_lineage,
)
from ..metadata.target_table_metadata import lookup_target_table_metadata
from .session_settings import (
    DEFAULT_QUOTED_REGEX_COLUMN_NAMES,
    quoted_regex_column_names_setting,
)


@dataclass
class TaskLineageResult:
    task_id: str
    parse_status: str
    syntax_status: str
    syntax_errors: list[dict] = field(default_factory=list)
    analysis_status: dict = field(default_factory=dict)
    statements: list[dict] = field(default_factory=list)
    table_state_graph: dict = field(default_factory=dict)
    final_table_states: dict[str, str] = field(default_factory=dict)
    statement_lineage: dict[str, object] = field(default_factory=dict)
    end_to_end_lineage: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    task_dependencies: dict = field(default_factory=dict)


@dataclass
class _State:
    state_id: str
    table: str
    ordinal: int
    known_empty: bool
    value_sources: dict[str, list[dict]]
    row_membership_sources: list[dict] = field(default_factory=list)
    value_condition_sources: dict[str, list[dict]] = field(default_factory=dict)
    # Per column: the keys a window grouped or ordered by. Parallel to value_sources,
    # which keeps every one of them as well -- this array only says which role they played.
    window_context_sources: dict[str, list[dict]] = field(default_factory=dict)
    columns_known: bool = True
    missing_reasons: list[str] = field(default_factory=list)
    # Whether the relation this state belongs to survives the session. Kept here so an edge
    # reading it can say so on itself, rather than making a consumer join against the
    # statement that produced it (TEMPVIEW-003).
    session_scoped: bool = False


class _StateBuilder:
    def __init__(self, schema: Mapping[str, Iterable[str]] | None):
        self.schema_provider = DictSchemaProvider(schema)
        self.states: dict[str, _State] = {}
        self.current_by_table: dict[str, _State] = {}
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def current(self, table: str) -> _State:
        current = self.current_by_table.get(table)
        if current is not None:
            return current
        columns = self.schema_provider.get_columns(table)
        state_id = _state_id(table, 0)
        value_sources = {
            column: [_prior_state_source(state_id, table, column)]
            for column in (columns or [])
        }
        current = _State(
            state_id=state_id,
            table=table,
            ordinal=0,
            known_empty=False,
            value_sources=value_sources,
            columns_known=columns is not None,
            missing_reasons=(
                []
                if columns is not None
                else ["schema_missing_for_state_passthrough"]
            ),
        )
        self._add(current, producer_statement_id=None)
        return current

    def transition(
        self,
        previous: _State | None,
        *,
        table: str,
        statement_id: str,
        effect: str,
        known_empty: bool,
        value_sources: dict[str, list[dict]],
        row_membership_sources: list[dict] | None = None,
        value_condition_sources: dict[str, list[dict]] | None = None,
        window_context_sources: dict[str, list[dict]] | None = None,
        columns_known: bool = True,
        missing_reasons: list[str] | None = None,
        session_scoped: bool = False,
    ) -> _State:
        # Two different questions, and they used to share one answer. `previous` says what
        # this state inherits: a CTAS is handed None because it replaces the relation, so its
        # value sources must carry no prior-state passthrough. The ordinal says which state of
        # this table it is, and that has to count every state the table has had, inherited
        # from or not. Deriving the ordinal from `previous` gave every CTAS ordinal 1, so a
        # script that redefined a relation produced two nodes with the same `state_id` and
        # different producers -- and nothing pointing at that id could say which it meant
        # (STATE-ID-001).
        latest = self.current_by_table.get(table)
        highest = max(
            previous.ordinal if previous is not None else 0,
            latest.ordinal if latest is not None else 0,
        )
        ordinal = highest + 1
        state = _State(
            state_id=_state_id(table, ordinal),
            table=table,
            ordinal=ordinal,
            session_scoped=session_scoped,
            known_empty=known_empty,
            value_sources=value_sources,
            row_membership_sources=list(row_membership_sources or []),
            window_context_sources={
                key: [dict(item) for item in value]
                for key, value in (window_context_sources or {}).items()
            },
            value_condition_sources={
                key: list(value)
                for key, value in (value_condition_sources or {}).items()
            },
            columns_known=columns_known,
            missing_reasons=list(missing_reasons or []),
        )
        self._add(state, producer_statement_id=statement_id)
        if previous is not None:
            self.edges.append({
                "from": previous.state_id,
                "to": state.state_id,
                "statement_id": statement_id,
                "effect": effect,
            })
        return state

    def _add(self, state: _State, producer_statement_id: str | None) -> None:
        self.states[state.state_id] = state
        self.current_by_table[state.table] = state
        self.nodes.append({
            "state_id": state.state_id,
            "table": state.table,
            "ordinal": state.ordinal,
            "known_empty": state.known_empty,
            "columns_known": state.columns_known,
            "producer_statement_id": producer_statement_id,
        })

    def graph(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "edges": list(self.edges),
            "nodes_by_id": {
                item["state_id"]: dict(item)
                for item in self.nodes
            },
        }

    def end_to_end(self) -> list[dict]:
        items: list[dict] = []
        for table in sorted(self.current_by_table):
            state = self.current_by_table[table]
            for column, sources in state.value_sources.items():
                items.append({
                    "target_state": state.state_id,
                    "table": table,
                    "column": column,
                    "value_sources": _dedupe_dicts(sources),
                    "row_membership_sources": _dedupe_dicts(
                        state.row_membership_sources
                    ),
                    "value_condition_sources": _dedupe_dicts(
                        state.value_condition_sources.get(column, [])
                    ),
                    "trace_complete": state.columns_known,
                    "missing_reasons": list(state.missing_reasons),
                })
                context = _dedupe_dicts(state.window_context_sources.get(column, []))
                if context:
                    # Optional and omitted when empty, so a row without a window keeps exactly
                    # the shape it had before this field existed.
                    items[-1]["window_context_sources"] = context
        return items


def _gaps_marked_for_recovered_syntax(
    gaps: list[dict],
    syntax_status: str,
) -> list[dict]:
    """Flag gaps that a repaired parse produced, so counting them stays honest.

    Script-level, matching how ``syntax_status`` itself is scoped: no single statement can
    be blamed, because the strict parse that detects the repair runs on the whole script.
    """
    if syntax_status == "strict_ok":
        return gaps
    for gap in gaps:
        if isinstance(gap, dict):
            gap["derived_from_recovered_syntax"] = True
    return gaps


def parse_task_lineage(
    sql: str,
    task_name: str,
    schema: Mapping[str, Iterable[str]] | None = None,
    target_metadata=None,
    task_dependencies: dict | None = None,
    partition_overwrite_mode: str | None = None,
) -> TaskLineageResult:
    """Parse an ordered SQL script into table-state and statement lineage."""
    ctas_repaired_sql, ctas_repairs = repair_ctas_missing_as(
        _normalize_directory_insert_sql(sql)
    )
    normalized, quoted_identifiers = repair_keyword_identifiers(ctas_repaired_sql)
    trees = sqlglot.parse(normalized, dialect=DIALECT, **PARSE_OPTS)
    syntax_status, syntax_errors = _syntax_status(sql)
    state_builder = _StateBuilder(schema)
    statements: list[dict] = []
    statement_lineage: dict[str, object] = {}
    warnings: list[dict] = []
    if ctas_repairs:
        # A rewritten statement must not be presented as if the author wrote it that way.
        # Same disclosure contract as identifiers_quoted_for_parse below; deliberately not
        # syntax_status: recovered, which is script-scoped and would degrade every other
        # statement in the same script along with this one.
        warnings.append({
            "type": "ctas_as_inserted_for_parse",
            "scope": "TASK",
            "msg": (
                "inserted the omitted AS to parse CREATE ... AS <query>: "
                + ", ".join(ctas_repairs)
            ),
        })
    if quoted_identifiers:
        warnings.append({
            "type": "identifiers_quoted_for_parse",
            "scope": "TASK",
            "msg": (
                "quoted keyword-colliding identifiers to parse the script: "
                + ", ".join(quoted_identifiers)
            ),
        })
    gaps: list[dict] = []
    # Columns proved by a CREATE ... AS SELECT earlier in this script, so the statements
    # that consume it are not modelled against a table nobody can describe.
    script_local: dict[str, list[str]] = {}
    parse_failed = False
    unsupported_data_changes = 0
    # Spark's own default for spark.sql.sources.partitionOverwriteMode. A script that never
    # sets it gets STATIC semantics, under which a dynamic-partition overwrite replaces the
    # whole table rather than only the partitions it writes.
    declared_overwrite_mode = (
        partition_overwrite_mode.strip().lower() if partition_overwrite_mode else None
    )
    if declared_overwrite_mode not in (None, "static", "dynamic"):
        raise ValueError(
            "partition_overwrite_mode must be 'static' or 'dynamic', got "
            f"{partition_overwrite_mode!r}"
        )
    dynamic_partition_overwrite = declared_overwrite_mode == "dynamic"
    # Tracked beside the value, not folded into it: "assumed static" and "observed static"
    # are the same bool but not the same fact, and only the second one is something the
    # script said. Kept local -- this setting is folded once and consumed in this module,
    # unlike quoted_regex_column_names_setting which earned its own module by being folded
    # in both entry points.
    partition_overwrite_mode_observed = False
    # Folded the same way, and handed to parse_scope_lineage rather than left to it: this
    # is the caller that holds the script, so the callee has nothing to guess from and its
    # default would silently disagree with the statement document (SESSION-001).
    regex_columns_enabled = DEFAULT_QUOTED_REGEX_COLUMN_NAMES

    for statement_index, tree in enumerate(trees):
        statement_id = f"stmt:{statement_index + 1:03d}"
        if tree is None:
            statements.append({
                "statement_id": statement_id,
                "statement_index": statement_index,
                "stmt_kind": "EMPTY",
                "category": "empty_statement",
                "model_status": "ignored",
                "normalized_sql": "",
            })
            continue
        statement = _statement_record(statement_id, statement_index, tree)
        if not _normalized_sql_is_equivalent(tree, statement["normalized_sql"]):
            warnings.append({
                "statement_id": statement_id,
                "type": "normalized_sql_not_equivalent",
                "scope": "TASK",
                "msg": (
                    "normalized_sql was generated from the AST and does not parse back to "
                    "the same query structure; it is a rendering of this statement, not a "
                    "runnable copy of it"
                ),
            })
        setting = _partition_overwrite_mode_setting(tree)
        if setting is not None:
            dynamic_partition_overwrite = setting
            partition_overwrite_mode_observed = True
        regex_setting = quoted_regex_column_names_setting(tree)
        if regex_setting is not None:
            regex_columns_enabled = regex_setting
        try:
            if _is_projection_write(tree):
                _apply_projection_write(
                    statement,
                    tree,
                    task_name,
                    _schema_with_script_local_tables(schema, script_local),
                    target_metadata,
                    state_builder,
                    statement_lineage,
                    gaps,
                    script_local,
                    dynamic_partition_overwrite=dynamic_partition_overwrite,
                    regex_columns_enabled=regex_columns_enabled,
                    partition_overwrite_mode_observed=partition_overwrite_mode_observed,
                    declared_overwrite_mode=declared_overwrite_mode,
                )
            elif isinstance(tree, exp.Delete):
                _apply_delete(statement, tree, state_builder, gaps)
            elif isinstance(tree, exp.TruncateTable):
                _apply_truncate(statement, tree, state_builder)
            elif isinstance(tree, exp.Update):
                _apply_update(statement, tree, state_builder, gaps)
            elif statement["category"] in {
                "control_statement",
                "empty_statement",
            }:
                statement["model_status"] = "ignored"
            else:
                unsupported_data_changes += 1
                warnings.append({
                    "statement_id": statement_id,
                    "type": "unsupported_statement",
                    "scope": "TASK",
                    "msg": (
                        f"{statement['stmt_kind']} is not modeled by task lineage"
                    ),
                })
        except Exception as exc:
            parse_failed = True
            statement["model_status"] = "failed"
            warnings.append({
                "statement_id": statement_id,
                "type": "LINEAGE_ERROR",
                "scope": "TASK",
                "msg": f"{type(exc).__name__}: {exc}",
            })
        statements.append(statement)

    gaps.extend(_statement_fact_gaps(statement_lineage))
    metadata_coverage = _metadata_coverage(
        state_builder,
        statement_lineage,
        statements,
        schema,
        script_local,
    )
    # A run that never received a table's columns produces gaps that read exactly like a
    # parser that could not handle the SQL. The fact was already recorded in
    # metadata_coverage, but nothing pointed at it from where a reader starts, and the same
    # run was read three times as a capability-gap report (METADATA-003).
    # Only source tables. A target without a schema entry is an ordinary shape — target DDL
    # is supplied separately — and it cannot be why a source-side reference failed to
    # resolve, so counting it would put a metadata explanation on gaps that have none.
    missing_sources = _missing_source_tables(metadata_coverage, statement_lineage)
    if missing_sources:
        warnings.append({
            "type": "metadata_incomplete",
            "scope": "TASK",
            "msg": (
                f"no column metadata for {len(missing_sources)} source table(s) "
                f"({', '.join(missing_sources[:5])}"
                f"{', ...' if len(missing_sources) > 5 else ''}). "
                "Field-level gaps in this document may follow from that rather than from "
                "the SQL, and should not be counted as parser capability gaps"
            ),
        })
    partial = bool(
        syntax_status != "strict_ok"
        or unsupported_data_changes
        or gaps
        or any(item["model_status"] == "failed" for item in statements)
    )
    analysis_status = {
        "status": "partial" if partial else "complete",
        "blocking_reasons": _analysis_blocking_reasons(
            syntax_status,
            unsupported_data_changes,
            gaps,
            statements,
            missing_sources,
        ),
    }
    # Said once for the whole script, after every statement is known. The per-statement flag
    # is on `statement_sequence[]`, while the entry that misleads is in `final_table_states`,
    # and `analysis_status` stays `complete` -- so a consumer who does not know to
    # cross-reference the two reads a confident artifact naming tables that were never
    # written to storage (TEMPVIEW-001). Naming the relations makes the warning actionable
    # rather than merely alarming.
    session_scoped = sorted({
        statement["target_table"]
        for statement in statements
        if statement.get("is_session_scoped_relation") and statement.get("target_table")
    })
    if session_scoped:
        warnings.append({
            "type": "session_scoped_relations_present",
            "scope": "TASK",
            "msg": (
                "these relations only live for the session and were never written to "
                "storage; exclude them from final_table_states and from table-level "
                "coverage before reconciling against a catalogue: "
                + ", ".join(session_scoped)
            ),
        })

    result = TaskLineageResult(
        task_id=task_name,
        parse_status="failed" if parse_failed else "ok",
        syntax_status=syntax_status,
        syntax_errors=syntax_errors,
        analysis_status=analysis_status,
        statements=statements,
        table_state_graph=state_builder.graph(),
        final_table_states={
            table: state.state_id
            for table, state in sorted(state_builder.current_by_table.items())
        },
        statement_lineage=statement_lineage,
        end_to_end_lineage=state_builder.end_to_end(),
        diagnostics={
            "warnings": warnings,
            # The per-statement lineage is built from `tree.sql()`, and a truncation is
            # invisible once the tree is rendered back out — the rendered statement parses
            # cleanly because the tokens sqlglot dropped are simply not in it. So the
            # script-level verdict is the only one that can say these gaps are shadows of a
            # repaired parse rather than facts about the query (PARSE-002).
            "lineage_fact_gaps": _gaps_marked_for_recovered_syntax(gaps, syntax_status),
                        "metadata_coverage": metadata_coverage,
            "stats": {
                "statement_count": len(statements),
                "modeled_statement_count": sum(
                    item["model_status"] == "modeled" for item in statements
                ),
                "ignored_statement_count": sum(
                    item["model_status"] == "ignored" for item in statements
                ),
                "failed_statement_count": sum(
                    item["model_status"] == "failed" for item in statements
                ),
            },
        },
        task_dependencies=dict(task_dependencies or {}),
    )
    return result


def _statement_record(
    statement_id: str,
    statement_index: int,
    tree: exp.Expression,
) -> dict:
    if _is_projection_write(tree):
        kind = _stmt_kind_for_tree(tree)
        category = (
            "conditional_write"
            if kind == "MERGE"
            else "projection_write"
        )
    else:
        kind = _statement_kind_label(tree)
        category = _statement_category(kind)
    return {
        "statement_id": statement_id,
        "statement_index": statement_index,
        "stmt_kind": kind,
        "category": category,
        "model_status": (
            "ignored"
            if category in {"control_statement", "empty_statement"}
            else "unsupported"
        ),
        # Best effort: a tree that cannot be printed still has usable lineage.
        "normalized_sql": render_sql_or_none(tree) or "",
    }


def _normalized_sql_is_equivalent(tree: exp.Expression, normalized_sql: str) -> bool:
    """Does the rendered statement still parse back to the same query structure?

    ``normalized_sql`` is generated from the AST, and generation is not always lossless: a
    WITH carried by an individual UNION branch is hoisted to statement level, so two
    same-named CTEs end up in one clause and the text will not run (ROUNDTRIP-001). The
    lineage is unaffected — it is built from the AST — but a consumer handed SQL that looks
    executable deserves to be told when it is not.

    The test is whether a single WITH clause ends up holding two CTEs of the same name that
    the original kept apart — the shadowing that makes the text unrunnable. Where the clause
    merely moves (a WITH written inside an INSERT is rendered ahead of it) nothing is lost
    and nothing is reported: an equivalence check that cries wolf on a re-rendering teaches
    consumers to ignore it.
    """
    # Only a statement that defines CTEs can lose one to shadowing. Skipping the rest also
    # keeps comment-only and control statements out of it: their rendering is not SQL, so
    # "does it parse back to the same structure" has no answer to give.
    if not normalized_sql or not any(True for _ in tree.find_all(exp.With)):
        return True
    try:
        reparsed = sqlglot.parse_one(normalized_sql, dialect=DIALECT, **PARSE_OPTS)
    except Exception:  # noqa: BLE001 - an unparseable rendering is itself the answer
        return False
    return not (_has_shadowed_cte(reparsed) and not _has_shadowed_cte(tree))


def _has_shadowed_cte(tree: exp.Expression) -> bool:
    for node in tree.find_all(exp.With):
        names = [cte.alias_or_name for cte in node.expressions]
        if len(names) != len(set(names)):
            return True
    return False


def _is_projection_write(tree: exp.Expression) -> bool:
    return bool(
        isinstance(tree, (exp.Insert, exp.Merge))
        or _is_ctas(tree)
        or tree.find(exp.Insert) is not None
        or tree.find(exp.Merge) is not None
    )


def _apply_projection_write(
    statement: dict,
    tree: exp.Expression,
    task_name: str,
    schema,
    target_metadata,
    states: _StateBuilder,
    statement_lineage: dict[str, object],
    gaps: list[dict],
    script_local: dict[str, list[str]] | None = None,
    *,
    regex_columns_enabled: bool = True,
    partition_overwrite_mode_observed: bool = False,
    declared_overwrite_mode: "str | None" = None,
    dynamic_partition_overwrite: bool = False,
) -> None:
    # Known inverted edge (scope -> contract), whitelisted by the dependency-direction
    # architecture test. statement_lineage entries are contract dicts that this module and
    # cli.py consume in four places, and TaskLineageResult's shape is public contract, so
    # untangling this belongs to the v1 retirement's converter re-homing (governance plan
    # WI-12, 0.3.0), not to a standalone move.
    from ..contract.lineage import to_lineage_dict

    result = parse_scope_lineage(
        tree.sql(dialect=DIALECT),
        task_name=f"{task_name}#{statement['statement_index']}",
        schema=dict(schema or {}),
        target_metadata=target_metadata,
        # Hand over the AST parsed from the original script. Re-parsing the generated SQL
        # loses a WITH carried by an individual UNION branch and degrades the whole
        # statement to an unqualified parse (ROUNDTRIP-001).
        tree=tree,
        regex_columns_enabled=regex_columns_enabled,
    )
    statement_id = statement["statement_id"]
    statement["model_status"] = "modeled"
    statement["target_table"] = result.target_table
    # Only true is recorded. `final_table_states` gains an entry for every relation a script
    # produces, so without this a consumer reconciling it against the catalogue reports temp
    # views as new warehouse tables (TEMPVIEW-001).
    if result.is_session_scoped_relation:
        statement["is_session_scoped_relation"] = True
    if script_local is not None:
        script_local_schema(schema, script_local, result)
    statement["target_field_binding"] = _target_binding_observation(
        result.target_field_binding,
        metadata_requested=target_metadata is not None,
        absence_reason=result.target_binding_absence,
    )
    previous = None if result.stmt_kind == "CTAS" else states.current(
        result.target_table
    )
    written_values = _write_value_sources(result, states)
    state_missing_reasons = _projection_state_missing_reasons(
        result,
        written_values,
    )
    if "projection_wildcard_unexpanded" in state_missing_reasons:
        gaps.append({
            "gap_type": "projection_wildcard_unexpanded",
            "statement_id": statement_id,
            "target_table": result.target_table,
            "root_impact": True,
            "needed_fact": "source schema for wildcard expansion",
        })
    undescribed = _undescribed_source_states(states, written_values)
    if undescribed:
        state_missing_reasons = list(dict.fromkeys([
            *state_missing_reasons,
            "source_state_columns_unknown",
        ]))
        gaps.append({
            "gap_type": "source_state_columns_unknown",
            "statement_id": statement_id,
            "target_table": result.target_table,
            "root_impact": True,
            "needed_fact": (
                "columns of the script-local relations read here: "
                + ", ".join(undescribed)
            ),
        })
    effect = _write_effect(
        result,
        dynamic_partition_overwrite=dynamic_partition_overwrite,
        target_metadata=target_metadata,
    )
    overwrite_mode_source = _partition_overwrite_mode_source(
        result,
        observed=partition_overwrite_mode_observed,
        target_metadata=target_metadata,
    )
    # Only where the source is reported, and only when the script did not say it itself:
    # a script SET is already `observed`, and adding what a deployment declared beside it
    # would say two things about one decision. Carries the value rather than a flag --
    # for a no-PARTITION write the artifact is otherwise identical either way, so a stale
    # declaration would leave no trace of what it claimed.
    declared_value = (
        declared_overwrite_mode
        if overwrite_mode_source == "assumed_default" and declared_overwrite_mode
        else None
    )
    if (
        effect in {"APPEND", "MERGE", "REPLACE_PARTITION"}
        and previous is not None
        and not previous.columns_known
    ):
        state_missing_reasons = list(dict.fromkeys([
            *state_missing_reasons,
            *previous.missing_reasons,
        ]))
    merge_conditions = (
        _merge_condition_sources(
            tree,
            target_table=result.target_table,
            result=result,
            statement_id=statement_id,
            gaps=gaps,
        )
        if isinstance(tree, exp.Merge)
        else []
    )
    if effect in {"APPEND", "REPLACE_PARTITION"} and previous is not None:
        value_sources = _merge_value_sources(
            _prior_values_for_written_columns(previous, written_values),
            written_values,
        )
    elif effect == "MERGE" and previous is not None:
        value_sources = _merge_value_sources(
            _prior_values_for_written_columns(previous, written_values),
            written_values,
        )
    else:
        value_sources = written_values
    columns_known = not state_missing_reasons and (
        bool(value_sources)
        or (previous.columns_known if previous is not None else False)
    )
    row_membership_sources = (
        list(previous.row_membership_sources)
        if effect in {"APPEND", "MERGE", "REPLACE_PARTITION"}
        and previous is not None
        else []
    )
    if effect == "MERGE":
        row_membership_sources = _dedupe_dicts([
            *row_membership_sources,
            *merge_conditions,
        ])
    value_condition_sources = (
        {
            column: list(sources)
            for column, sources in previous.value_condition_sources.items()
        }
        if effect in {"APPEND", "MERGE", "REPLACE_PARTITION"}
        and previous is not None
        else {}
    )
    if effect == "MERGE":
        for column in written_values:
            value_condition_sources[column] = _dedupe_dicts([
                *value_condition_sources.get(column, []),
                *merge_conditions,
            ])
    window_context_sources = _write_window_context_sources(result)
    state = states.transition(
        previous,
        table=result.target_table,
        statement_id=statement_id,
        effect=effect,
        session_scoped=result.is_session_scoped_relation,
        known_empty=False,
        value_sources=value_sources,
        row_membership_sources=row_membership_sources,
        value_condition_sources=value_condition_sources,
        window_context_sources=window_context_sources,
        columns_known=columns_known,
        missing_reasons=state_missing_reasons,
    )
    statement["input_states"] = (
        [previous.state_id] if previous is not None else []
    )
    statement["output_state"] = state.state_id
    statement["effect"] = {
        "rowset_effect": {
            "operation": effect,
            **(
                {"partition_overwrite_mode_source": overwrite_mode_source}
                if overwrite_mode_source
                else {}
            ),
            **(
                {"partition_overwrite_mode_declared": declared_value}
                if declared_value
                else {}
            ),
            **(
                {"membership_sources": merge_conditions}
                if effect == "MERGE"
                else {}
            ),
        },
        "column_effect": {"value_mode": "WRITE_PROJECTION"},
    }
    statement_lineage[statement_id] = to_lineage_dict(result)


def _apply_delete(
    statement: dict,
    tree: exp.Delete,
    states: _StateBuilder,
    gaps: list[dict],
) -> None:
    table = _table_name(tree.this)
    previous = states.current(table)
    where = tree.args.get("where")
    predicate = where.this if isinstance(where, exp.Where) else None
    membership_sources = _expression_field_sources(predicate, target_table=table)
    deletes_all_rows = predicate is None
    all_membership = _dedupe_dicts([
        *previous.row_membership_sources,
        *membership_sources,
    ])
    state = states.transition(
        previous,
        table=table,
        statement_id=statement["statement_id"],
        effect="RESET" if deletes_all_rows else "ANTI_FILTER",
        known_empty=deletes_all_rows or previous.known_empty,
        value_sources=(
            _empty_column_values(previous)
            if deletes_all_rows
            else {
                column: list(sources)
                for column, sources in previous.value_sources.items()
            }
        ),
        row_membership_sources=all_membership,
        value_condition_sources=previous.value_condition_sources,
        columns_known=previous.columns_known,
        missing_reasons=previous.missing_reasons,
    )
    statement.update({
        "model_status": "modeled",
        "target_table": table,
        "input_states": [previous.state_id],
        "output_state": state.state_id,
        "effect": {
            "rowset_effect": {
                "operation": (
                    "DELETE_ALL_ROWS"
                    if deletes_all_rows
                    else "DELETE_MATCHED_ROWS"
                ),
                "predicate_expression": (
                    predicate.sql(dialect=DIALECT) if predicate is not None else None
                ),
                "membership_sources": membership_sources,
            },
            "column_effect": {
                "value_mode": (
                    "NO_SURVIVING_ROWS"
                    if deletes_all_rows
                    else "PASSTHROUGH_SURVIVING_ROWS"
                ),
                "value_changed_columns": [],
                "row_membership_affected_columns": ["*"],
            },
        },
    })
    if not previous.columns_known:
        gaps.append(_schema_passthrough_gap(statement, table))


def _apply_truncate(
    statement: dict,
    tree: exp.TruncateTable,
    states: _StateBuilder,
) -> None:
    tables = list(tree.args.get("expressions") or [])
    table = _table_name(tables[0]) if tables else ""
    previous = states.current(table)
    partition = tree.args.get("partition")
    partition_only = isinstance(partition, exp.Partition)
    membership_sources = _expression_field_sources(
        partition if partition_only else None,
        target_table=table,
    )
    state = states.transition(
        previous,
        table=table,
        statement_id=statement["statement_id"],
        effect="RESET_PARTITION" if partition_only else "RESET",
        known_empty=previous.known_empty if partition_only else True,
        value_sources=(
            {
                column: list(sources)
                for column, sources in previous.value_sources.items()
            }
            if partition_only
            else _empty_column_values(previous)
        ),
        row_membership_sources=_dedupe_dicts([
            *previous.row_membership_sources,
            *membership_sources,
        ]),
        value_condition_sources=previous.value_condition_sources,
        columns_known=previous.columns_known,
        missing_reasons=previous.missing_reasons,
    )
    statement.update({
        "model_status": "modeled",
        "target_table": table,
        "input_states": [previous.state_id],
        "output_state": state.state_id,
        "effect": {
            "rowset_effect": {
                "operation": (
                    "RESET_PARTITION" if partition_only else "RESET_ALL_ROWS"
                ),
                "partition_expression": (
                    partition.sql(dialect=DIALECT) if partition_only else None
                ),
                "membership_sources": membership_sources,
            },
            "column_effect": {
                "schema_preserved": True,
                "value_mode": (
                    "PASSTHROUGH_UNAFFECTED_PARTITIONS"
                    if partition_only
                    else "NO_SURVIVING_ROWS"
                ),
                "row_membership_affected_columns": ["*"],
            },
        },
    })


def _apply_update(
    statement: dict,
    tree: exp.Update,
    states: _StateBuilder,
    gaps: list[dict],
) -> None:
    table = _table_name(tree.this)
    previous = states.current(table)
    where = tree.args.get("where")
    predicate = where.this if isinstance(where, exp.Where) else None
    condition_sources = _expression_field_sources(predicate, target_table=table)
    changed: list[str] = []
    assignments: list[dict] = []
    value_sources = {
        column: list(sources)
        for column, sources in previous.value_sources.items()
    }
    value_conditions = {
        column: list(sources)
        for column, sources in previous.value_condition_sources.items()
    }
    for assignment in tree.expressions:
        if not isinstance(assignment, exp.EQ) or not isinstance(
            assignment.this, exp.Column
        ):
            continue
        column = assignment.this.name
        changed.append(column)
        expression_sources = _expression_field_sources(
            assignment.expression,
            target_table=table,
        )
        value_sources[column] = _dedupe_dicts([
            *value_sources.get(
                column,
                [_prior_state_source(previous.state_id, table, column)],
            ),
            *[
                {
                    "source_kind": "physical_field",
                    **source,
                    "transform": "EXPRESSION",
                }
                for source in expression_sources
            ],
        ])
        value_conditions[column] = _dedupe_dicts([
            *value_conditions.get(column, []),
            *condition_sources,
        ])
        assignments.append({
            "column": column,
            "expression": assignment.expression.sql(dialect=DIALECT),
            "value_sources": expression_sources,
        })
    state = states.transition(
        previous,
        table=table,
        statement_id=statement["statement_id"],
        effect="CONDITIONAL_UPDATE",
        known_empty=previous.known_empty,
        value_sources=value_sources,
        row_membership_sources=previous.row_membership_sources,
        value_condition_sources=value_conditions,
        columns_known=previous.columns_known,
        missing_reasons=previous.missing_reasons,
    )
    statement.update({
        "model_status": "modeled",
        "target_table": table,
        "input_states": [previous.state_id],
        "output_state": state.state_id,
        "effect": {
            "rowset_effect": {"operation": "PRESERVE_ROWS"},
            "column_effect": {
                "value_mode": "CONDITIONAL_ASSIGNMENT",
                "value_changed_columns": changed,
                "value_passthrough_columns": [
                    column
                    for column in previous.value_sources
                    if column not in set(changed)
                ],
            },
            "predicate_expression": (
                predicate.sql(dialect=DIALECT) if predicate is not None else None
            ),
            "condition_sources": condition_sources,
            "assignments": assignments,
        },
    })
    if not previous.columns_known:
        gaps.append(_schema_passthrough_gap(statement, table))


def _partition_overwrite_mode_source(
    result,
    *,
    observed: bool,
    target_metadata=None,
) -> str | None:
    """Say whether this write's blast radius rests on an observed SET or on Spark's default.

    Only for a write whose answer actually turns on the setting. Keyed on `result.stmt_kind`
    rather than on the effect: a partitioned CTAS also yields REPLACE with a fully dynamic
    spec, and its answer does not depend on the setting at all (`statement_sequence` spells
    that kind `INSERT`, so the effect's own neighbours cannot be used for this).

    Two shapes qualify. A fully dynamic spec is the obvious one. An overwrite with no
    PARTITION clause at all is the other: on a partitioned table Spark treats it as a
    dynamic-partition insert too, and since an absent field here reads as "this answer does
    not depend on the setting", staying silent there would be a false claim rather than a
    neutral omission.
    """
    if result.stmt_kind != "INSERT_OVERWRITE":
        return None
    mode = result.target_partition_mode
    if mode == "dynamic":
        pass
    elif mode == "none":
        metadata = (
            lookup_target_table_metadata(target_metadata, result.target_table)
            if target_metadata is not None
            else None
        )
        if not (metadata and metadata.partition_columns):
            return None
    else:
        # A valued or mixed spec bounds the overwrite whatever the mode is.
        return None
    return "observed" if observed else "assumed_default"


def _write_effect(
    result,
    *,
    dynamic_partition_overwrite: bool = False,
    target_metadata=None,
) -> str:
    if result.stmt_kind == "INSERT":
        return "APPEND"
    if (
        result.stmt_kind == "INSERT_OVERWRITE"
        and _replaces_only_named_partitions(
            result.target_partition_mode,
            dynamic_partition_overwrite=dynamic_partition_overwrite,
            target_is_partitioned=_target_is_partitioned(result, target_metadata),
        )
    ):
        return "REPLACE_PARTITION"
    if result.stmt_kind in {"INSERT_OVERWRITE", "CTAS"}:
        return "REPLACE"
    if result.stmt_kind == "MERGE":
        return "MERGE"
    return "WRITE"


def _target_is_partitioned(result, target_metadata) -> bool:
    """Does the target's DDL declare partition columns? False when we were not told."""
    if target_metadata is None:
        return False
    metadata = lookup_target_table_metadata(target_metadata, result.target_table)
    return bool(metadata and metadata.partition_columns)


def _replaces_only_named_partitions(
    partition_mode: str,
    *,
    dynamic_partition_overwrite: bool,
    target_is_partitioned: bool = False,
) -> bool:
    """Does this INSERT OVERWRITE leave the rest of the table standing?

    A spec that names values (``PARTITION(dt='20260101')``, or a static prefix in a mixed
    spec) bounds the overwrite to those partitions, so the rest of the table survives whatever
    the overwrite mode is. A fully dynamic spec (``PARTITION(dt)``) does not: under Spark's
    default STATIC mode every existing partition is dropped first, and only an explicit
    DYNAMIC mode limits the replacement to the partitions actually written (PARTOVR-001).
    """
    if partition_mode in {"static", "mixed"}:
        return True
    if partition_mode == "dynamic":
        return dynamic_partition_overwrite
    if partition_mode == "none":
        # No PARTITION clause on a partitioned table is a dynamic-partition insert too,
        # so DYNAMIC bounds it the same way. Gated on the target actually being
        # partitioned: without that check a whole-table overwrite of an unpartitioned
        # table comes back partition-scoped, and every existing test still passes.
        return dynamic_partition_overwrite and target_is_partitioned
    return False


def _partition_overwrite_mode_setting(tree: exp.Expression) -> bool | None:
    """True/False when this statement sets partitionOverwriteMode, None when it does not."""
    if not isinstance(tree, exp.Set):
        return None
    for item in tree.args.get("expressions") or []:
        text = item.sql(dialect=DIALECT).replace("`", "").replace('"', "")
        key, _, value = text.partition("=")
        if key.strip().lower().endswith("partitionoverwritemode"):
            cleaned = value.strip().strip("'").lower()
            # Spark's SQLConf accepts only STATIC/DYNAMIC. Reading anything else as
            # "observed static" was harmless while static was the only default, but it
            # discards a declared deployment value and stamps the result `observed` --
            # the most authoritative label the contract has. `nonstrict` is the
            # neighbouring Hive key's value and a predictable mix-up. Same rule as
            # quoted_regex_column_names_setting: an unusable value leaves the setting
            # as it was.
            if cleaned in {"static", "dynamic"}:
                return cleaned == "dynamic"
            return None
    return None


def _target_binding_observation(
    binding: dict,
    *,
    metadata_requested: bool,
    absence_reason: str | None = None,
) -> dict:
    if not binding:
        # Read the classification rather than re-deriving it. `metadata_requested` cannot
        # tell a CTAS from a table missing from the DDL -- both arrive with no binding --
        # which is why this used to call every one of them target_table_not_found, the one
        # value that means the binding should have happened. The old derivation stays as a
        # fallback so the field remains unconditionally present for any caller that did
        # not run the classifier.
        return {
            "status": "absent",
            "reason_code": absence_reason or (
                "target_table_not_found"
                if metadata_requested
                else "metadata_not_provided"
            ),
        }
    issues = list(binding.get("issues") or [])
    reason_code = None
    if binding.get("status") == "fallback":
        reason_code = _binding_reason_code(issues)
    return {
        **dict(binding),
        **({"reason_code": reason_code} if reason_code else {}),
    }


def _binding_reason_code(issues: list[str]) -> str:
    if any(item.startswith("target_metadata_invalid:") for item in issues):
        return "metadata_unusable"
    if any(item.startswith("projection_target_count_mismatch:") for item in issues):
        return "column_count_mismatch"
    if any(item.startswith("insert_partition_not_in_target_metadata:") for item in issues):
        return "partition_alignment_mismatch"
    if "target_column_names_not_unique" in issues:
        return "ddl_schema_conflict"
    return "binding_not_applicable"


def _write_window_context_sources(result) -> dict[str, list[dict]]:
    from .end_to_end import build_end_to_end_lineage

    context: dict[str, list[dict]] = {}
    for item in build_end_to_end_lineage(result):
        entries = item.get("window_context_sources") or []
        if entries:
            context[item["column"]] = [dict(entry) for entry in entries]
    return context


def _undescribed_source_states(
    states: _StateBuilder,
    written_values: dict[str, list[dict]],
) -> list[str]:
    """Script-local relations these sources read whose own columns were never resolved.

    Incompleteness already propagates from the previous state of the table being written. It
    did not propagate from the relations being *read*, which is the same question asked of a
    different edge: a temporary relation built from an unexpanded `SELECT *` has one row keyed
    on `*`, and a statement selecting named columns out of it claimed a complete trace resting
    on a relation nobody could describe (TRACE-002).

    The test is whether the relation's own columns stayed a wildcard, and deliberately not
    `state.columns_known`. That flag is false for *any* missing reason: a valued-partition
    overwrite needs the table's prior state, and when the target is absent from the supplied
    schema that state carries `schema_missing_for_state_passthrough` -- so keying on it
    reported "the columns of this relation are unknown" about relations whose columns were
    named in the producing projection and listed in the document. The columns of a relation
    the script built are known from the script; external metadata has nothing to say about
    them, and waiting for it is the wrong question.

    That combination is worse than a wrong flag. A consumer folding the hop looks for the
    source column's row, finds only `*`, and gets an empty result reading as "no lineage" --
    with `trace_complete: true` sitting beside it, contradicting nothing.
    """
    undescribed: list[str] = []
    for sources in written_values.values():
        for source in sources:
            state_id = source.get("source_state")
            if not state_id:
                continue
            state = states.states.get(state_id)
            if state is None:
                continue
            # The one shape this is about: the relation's own projection stayed a wildcard, so
            # its single row is keyed on `*` and no named column can ever be found in it.
            if set(state.value_sources) != {"*"}:
                continue
            # Reading `*` from such a relation is `COUNT(*)`, whose star is the row rather than
            # an unknown column list -- the same distinction `_projection_state_missing_reasons`
            # warns about, approached from the other side.
            if source.get("column") == "*":
                continue
            undescribed.append(state_id)
    return sorted(dict.fromkeys(undescribed))


def _source_state(states: _StateBuilder | None, table: str) -> dict:
    """Which state of `table` this read sees, when the script itself produced one.

    A value source names a table, and after STATE-ID-001 a table can hold more than one state
    in a script. Two reads of a redefined relation were then indistinguishable, so a consumer
    resolving that hop by name folded both to whichever definition was recorded last
    (STATE-ID-002).

    `end_to_end_lineage` walks the state each table is in when the script *ends*, so the row
    for an earlier definition is not in the document and cannot be without changing what the
    field means. Naming the state is what makes that survivable rather than dangerous: the
    consumer looks for that state, finds no row, and knows the hop cannot be folded here --
    instead of folding it to the wrong answer.

    Nothing is stamped for a table the script never wrote. It is in the state it had before
    the script began, and there is no second candidate to confuse it with.
    """
    if states is None:
        return {}
    state = states.current_by_table.get(table)
    if state is None or state.ordinal < 1:
        return {}
    stamped = {"source_state": state.state_id}
    if state.session_scoped:
        # Said on the edge itself. The producing statement already carries
        # `is_session_scoped_relation`, but the edges a consumer acts on are here, and making
        # them join the two objects to find out is the whole complaint (TEMPVIEW-003). This is
        # a new optional key, not a `source_kind` value: a filter that does not know it keeps
        # exactly the behaviour it had.
        stamped["session_scoped"] = True
    return stamped


def _write_value_sources(result, states: _StateBuilder | None = None) -> dict[str, list[dict]]:
    from .end_to_end import build_end_to_end_lineage

    values: dict[str, list[dict]] = {}
    for item in build_end_to_end_lineage(result):
        sources = [
            {
                "source_kind": "physical_field",
                "table": source["table"],
                "column": source["column"],
                "transform": source.get("transform", item.get("transform", "DIRECT")),
                **_source_state(states, source["table"]),
            }
            for source in item.get("physical_sources", [])
        ]
        for generated in item.get("generated_sources", []):
            sources.append({
                "source_kind": "generated",
                **dict(generated),
            })
        values[item["column"]] = _dedupe_dicts(sources)
    return values


def _projection_state_missing_reasons(
    result,
    written_values: dict[str, list[dict]],
) -> list[str]:
    # ``*`` carries two opposite meanings here. A projection that stayed a wildcard names
    # its target column "*" and its source carries EXPAND_ALL — the columns are genuinely
    # unknown. COUNT(*) also records the source column as "*", but that star is the row
    # itself: the lineage is resolved, and the field's dependency on every column of the
    # table is the fact, not a hole in it. Reading the second as the first published fully
    # resolved statements as partial, asking for metadata that could never close the gap.
    if "*" in written_values or any(
        source.get("column") == "*" and source.get("transform") == "EXPAND_ALL"
        for sources in written_values.values()
        for source in sources
    ):
        return ["projection_wildcard_unexpanded"]
    if any(
        gap.get("root_impact")
        for gap in result.diagnostics.lineage_fact_gaps
        if isinstance(gap, dict)
    ):
        return ["projection_lineage_fact_gap"]
    if not written_values:
        return ["projection_has_no_resolved_target_fields"]
    return []


def _prior_values_for_written_columns(
    previous: _State,
    written: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    result = {
        column: list(sources)
        for column, sources in previous.value_sources.items()
    }
    for column in written:
        result.setdefault(
            column,
            [_prior_state_source(previous.state_id, previous.table, column)],
        )
    return result


def _merge_value_sources(*mappings) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for mapping in mappings:
        for column, sources in mapping.items():
            result[column] = _dedupe_dicts([
                *result.get(column, []),
                *sources,
            ])
    return result


def _empty_column_values(previous: _State) -> dict[str, list[dict]]:
    return {column: [] for column in previous.value_sources}


def _expression_field_sources(
    expression: exp.Expression | None,
    *,
    target_table: str,
) -> list[dict]:
    """Physical fields a predicate reads, resolved from the expression alone.

    There is deliberately no alias-map parameter: mapping a statement's aliases to
    tables here is guesswork, and MERGE — the one caller that needed it — now resolves
    its aliases through the built scopes instead (MERGE-CTE-002).
    """
    if expression is None:
        return []
    sources: list[dict] = []
    for column in expression.find_all(exp.Column):
        select = column.find_ancestor(exp.Select)
        if select is None:
            table = column.table if column.table else target_table
        else:
            direct_tables = [
                item
                for item in select.find_all(exp.Table)
                if item.find_ancestor(exp.Select) is select
            ]
            # Kept local: rebinding ``aliases`` here would leak this nested query's
            # alias map onto every later top-level column in the same expression.
            local_aliases = {
                (item.alias_or_name or "").lower(): _table_name(item)
                for item in direct_tables
            }
            if column.table:
                table = local_aliases.get(column.table.lower(), column.table)
            elif len(direct_tables) == 1:
                table = _table_name(direct_tables[0])
            else:
                table = "UNKNOWN"
        sources.append({"table": table, "column": column.name})
    return _dedupe_dicts(sources)


def _merge_condition_expressions(
    tree: exp.Merge,
) -> "list[tuple[exp.Expression | None, bool]]":
    """Each MERGE condition paired with whether its clause can see the source relation.

    A NOT MATCHED BY SOURCE clause is evaluated on target rows that no source row
    matched, so Spark resolves its condition against the target alone. Reading such a
    condition through the USING scope publishes a physical field the branch cannot
    reference -- the same fabricated edge this function's docstring already records for
    a different shape.
    """
    expressions: "list[tuple[exp.Expression | None, bool]]" = [(tree.args.get("on"), False)]
    whens = tree.args.get("whens")
    if whens is not None:
        expressions.extend(
            (
                when.args.get("condition"),
                not bool(when.args.get("matched")) and bool(when.args.get("source")),
            )
            for when in getattr(whens, "expressions", [])
        )
    return expressions


def _merge_condition_sources(
    tree: exp.Merge,
    *,
    target_table: str,
    result: ScopeLineageResult,
    statement_id: str,
    gaps: list[dict],
) -> list[dict]:
    """Row-membership sources for a MERGE, resolved through Core's scope facts.

    ``row_membership_sources`` claims a physical field decided whether a target row
    exists, so every entry has to be one. Deriving the USING alias's table from the raw
    AST cannot do that: a CTE-backed USING published the CTE name, a UNION published the
    literal string ``UNKNOWN``, and a condition naming a column the USING relation does
    not expose published that column anyway — all as if they were proven physical fields
    (MERGE-CTE-002). Core has already resolved the USING scope; trace through it, and
    where the trace cannot finish, emit a fact gap instead of a name.
    """
    target = tree.this
    target_alias = (
        (target.alias_or_name or target.name).lower()
        if isinstance(target, exp.Table)
        else ""
    )
    using = tree.args.get("using")
    using_alias = (
        (using.alias_or_name or "source").lower()
        if isinstance(using, exp.Expression)
        else ""
    )

    sources: list[dict] = []
    for expression, by_source in _merge_condition_expressions(tree):
        if expression is None:
            continue
        for column in expression.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not None:
                # A column inside a nested query belongs to that query's own sources,
                # not to either MERGE alias.
                sources.extend(
                    _expression_field_sources(column, target_table=target_table)
                )
                continue
            qualifier = column.table.lower()
            if not qualifier or qualifier == target_alias:
                sources.append({"table": target_table, "column": column.name})
                continue
            if qualifier != using_alias or by_source:
                # ``by_source``: the qualifier names the USING relation, but this clause
                # cannot see it, so the reference resolves to nothing in Spark. A gap is
                # the honest answer; a source field would be a claim we know is false.
                gaps.append(
                    _merge_condition_gap(statement_id, column.table, column.name)
                )
                continue
            physical_fields, incomplete_reasons = _merge_condition_physical_fields(
                result, column.name
            )
            if incomplete_reasons or not physical_fields:
                gaps.append(
                    _merge_condition_gap(statement_id, column.table, column.name)
                )
                continue
            sources.extend(physical_fields)
    return _dedupe_dicts(sources)


def _merge_condition_physical_fields(
    result: ScopeLineageResult,
    column_name: str,
) -> tuple[list[dict], list[str]]:
    if not result.merge_using_scope_id:
        return [], ["merge_using_scope_missing"]
    fields, incomplete_reasons = _physical_fields_for_scope_column(
        result, result.merge_using_scope_id, column_name
    )
    return (
        [{"table": field["table"], "column": field["column"]} for field in fields],
        incomplete_reasons,
    )


def _merge_condition_gap(statement_id: str, source_alias: str, column: str) -> dict:
    return {
        "gap_type": "merge_condition_source_unresolved",
        "statement_id": statement_id,
        "source_alias": source_alias,
        "column": column,
        "root_impact": True,
        "needed_fact": "MERGE condition source scope and physical field",
    }


def _table_name(table: exp.Expression | None) -> str:
    return _qualified_table(table) if isinstance(table, exp.Table) else ""


def _state_id(table: str, ordinal: int) -> str:
    return f"state:{table}:{ordinal:03d}"


def _prior_state_source(state_id: str, table: str, column: str) -> dict:
    return {
        "source_kind": "prior_table_state",
        "state_id": state_id,
        "table": table,
        "column": column,
    }


def _schema_passthrough_gap(statement: dict, table: str) -> dict:
    return {
        "gap_type": "schema_missing_for_state_passthrough",
        "statement_id": statement["statement_id"],
        "target_table": table,
        "root_impact": True,
        "needed_fact": "target table columns",
    }


def _missing_source_tables(
    metadata_coverage: Mapping[str, object],
    statement_lineage: Mapping[str, object],
) -> list[str]:
    """Source tables this run could not describe.

    Restricted to sources on purpose: a target with no schema entry is an ordinary shape,
    since target DDL is supplied through its own input, and it is never why a source-side
    reference failed to resolve.
    """
    missing = {str(table) for table in metadata_coverage.get("missing_tables") or []}
    if not missing:
        return []
    sources: set[str] = set()
    for lineage in statement_lineage.values():
        if isinstance(lineage, dict):
            sources.update(str(table) for table in lineage.get("source_tables") or [])
    return sorted(missing & sources)


def _analysis_blocking_reasons(
    syntax_status: str,
    unsupported_data_changes: int,
    gaps: list[dict],
    statements: list[dict],
    missing_source_tables: list[str] | None = None,
) -> list[str]:
    """Name the causes, and put a cause ahead of the symptom it explains.

    A reader takes the first reason as the headline. Gaps produced because a referenced
    table's columns were never supplied were headlined ``lineage_fact_gap``, which reads as
    a statement about the SQL — so the cause is listed first when it applies
    (METADATA-003).
    """
    reasons: list[str] = []
    if syntax_status != "strict_ok":
        reasons.append("syntax_recovered")
    if unsupported_data_changes:
        reasons.append("unsupported_data_change")
    if gaps:
        # Only alongside gaps: incomplete metadata with nothing unresolved blocks nothing,
        # and the warning already records it.
        if missing_source_tables:
            reasons.append("metadata_incomplete")
        reasons.append("lineage_fact_gap")
    if any(item["model_status"] == "failed" for item in statements):
        reasons.append("statement_failed")
    return reasons


def _statement_fact_gaps(statement_lineage: Mapping[str, object]) -> list[dict]:
    result: list[dict] = []
    for statement_id, lineage in statement_lineage.items():
        if not isinstance(lineage, dict):
            continue
        diagnostics = lineage.get("diagnostics") or {}
        for gap in diagnostics.get("lineage_fact_gaps") or []:
            if isinstance(gap, dict):
                result.append({"statement_id": statement_id, **dict(gap)})
    return result


def _metadata_coverage(
    states: _StateBuilder,
    statement_lineage: dict[str, object],
    statements: list[dict],
    schema,
    script_local: dict[str, list[str]] | None = None,
) -> dict:
    """Which referenced tables the analysis could describe, and which it could not.

    A table the script creates counts as covered rather than missing. Its columns are
    proved by the statement that built it, and it exists nowhere else — listing it as
    missing metadata asks the operator to supply a schema for a table the warehouse
    does not have.
    """
    referenced: set[str] = set(states.current_by_table)
    for lineage in statement_lineage.values():
        referenced.update(lineage.get("source_tables") or [])
        target = lineage.get("target_table")
        if target:
            referenced.add(target)
    for statement in statements:
        effect = statement.get("effect") or {}
        rowset = effect.get("rowset_effect") or {}
        for source in rowset.get("membership_sources") or []:
            table = source.get("table")
            if table and table != "UNKNOWN":
                referenced.add(table)
    script_local = script_local or {}
    covered = sorted(
        table
        for table in referenced
        if states.schema_provider.get_columns(table) is not None
        or table in script_local
    )
    missing = sorted(set(referenced) - set(covered))
    return {
        "referenced_table_count": len(referenced),
        "covered_table_count": len(covered),
        "missing_table_count": len(missing),
        "covered_tables": covered,
        "missing_tables": missing,
        "schema_source_count": getattr(schema, "metadata_source_count", 0)
        if schema is not None
        else 0,
        "metadata_conflicts": [
            dict(item)
            # A conflict about a table this task never reads is noise, so table-scoped ones are
            # filtered to the referenced set. A *file-level* rejection carries no table -- it is
            # recorded precisely because the file could not be read far enough to name one -- and
            # filtering on an empty table dropped every one of them, so a rejected metadata file
            # was recorded and then never shown (META-ISOLATION-001).
            for item in getattr(schema, "metadata_conflicts", [])
            if not item.get("table") or item.get("table") in referenced
        ],
    }


def _dedupe_dicts(items: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple] = set()
    for item in items:
        key = tuple(sorted((key, repr(value)) for key, value in item.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result
