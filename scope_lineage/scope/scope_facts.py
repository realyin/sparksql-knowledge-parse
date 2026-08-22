"""Scope Facts — a subsystem extracted from a downstream builder.

Plain module-level functions (previously the ScopeFactsEngine class). The orchestrating
module imports and calls these; see its wrapper for the public entry point.
"""
from __future__ import annotations
import re
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope
from .parser import (
    _qualified_table,
)
from ..metadata.schema_metadata import column_details_for_table, table_details_for_table
from .scope_types import (
    ScopeData,
    ScopeColumn,
    ScopeFieldUsage,
    ScopeInputEdge,
    ScopeLineageResult,
    ScopeLogicBlock,
    ScopeOutputField,
    DiagnosticWarning,
    SourceRef,
)
from .expansion_budget import ExpansionBudget
from .sqlglot_walk import _pivot_of_source_node, _source_item_from_ast_node
from ._constants import DIALECT, PARSE_OPTS, _SCOPE_ID_ATTR
from .expression_expansion import _ordered_physical_fields_in_expression, _physical_fields_referenced_in_expression, _replace_struct_field_access_from_upstream, _resolve_expression_resolution_from_output_sources, _resolved_expression_fact_from_source_refs
from .expression_refs import _cached_pattern, _qualified_field_refs, _strip_sql_comments
from .expression_text import _function_names, _replace_qualified_ref_with_expression, _replace_unqualified_ref_with_expression, _unexpanded_bound_aliases_in_expression
from .function_catalog import _AGGREGATE_FUNCTIONS, _CLEANING_FUNCTIONS, _KNOWN_SCALAR_FUNCTIONS
from .sequences import _extend_unique, _unique_ordered
from .source_refs import _dedupe_generated_source_dicts, _dedupe_physical_field_dicts, _dedupe_rowset_source_dicts, _is_cross_join_type, _is_internal_scope_id, _normalize_expression_resolution, _physical_source_fields_for_refs, _physical_source_ids_for_input, _qualified_physical_field_sql, _rowset_sources_from_upstream_output, _source_kind_for_resolution, _source_ref_to_dict, _source_refs_from_detail_fields, _source_type_from_id
from .sqlglot_walk import _find_alias_in_parent
from .star_passthrough import _populate_union_output_branch_mappings, _star_passthrough_output_fact
from .column_expression_resolution import _expression_resolution_for_scope_column
from .lineage_fact_gaps import _populate_lineage_fact_gaps
from .passthrough_resolution import _propagate_passthrough_expression_resolution
from .logic_block import _populate_logic_blocks  # noqa: F401


# Both resolution segments used to be hand-unrolled (P I O, P I O, P I before the detail
# refreshes; P I, P I, P O after). Full rounds run to a fixed point were verified
# byte-identical on the whole golden corpus across the sqlglot compat matrix -- the tail
# unification only became safe once the internal pass turned idempotent on settled
# outputs. Floor of 3 keeps at least the unrolled coverage even if the fingerprint ever
# misses a mutated field; ceiling per the governance plan (WI-09).
_RESOLUTION_MIN_ROUNDS = 3
_RESOLUTION_MAX_ROUNDS = 6


def _resolution_fingerprint(result: ScopeLineageResult) -> int:
    """Cheap stability probe over every field the three resolution passes mutate."""
    return hash(tuple(
        (
            scope_id,
            output.name,
            output.expanded_expression,
            output.expansion_status,
            repr(output.expression_resolution),
            repr(output.unexpanded_refs),
            repr(output.sources),
        )
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    ))


def _run_resolution_rounds(result: ScopeLineageResult) -> None:
    fingerprint = _resolution_fingerprint(result)
    for round_index in range(_RESOLUTION_MAX_ROUNDS):
        _propagate_passthrough_expression_resolution(result)
        _resolve_internal_scope_expression_resolution(result)
        _resolve_expression_resolution_from_output_sources(result)
        next_fingerprint = _resolution_fingerprint(result)
        if round_index + 1 >= _RESOLUTION_MIN_ROUNDS and next_fingerprint == fingerprint:
            return
        fingerprint = next_fingerprint
    # Still changing at the ceiling: record it the way expansion_status records its budget --
    # the artifact stays valid, the consumer learns the resolution text may not be final.
    result.diagnostics.warnings.append(DiagnosticWarning(
        type="resolution_rounds_exhausted",
        scope="TASK",
        msg=(
            "expression resolution was still changing after "
            f"{_RESOLUTION_MAX_ROUNDS} rounds; resolutions may be incomplete"
        ),
    ))


def _populate_enhanced_scope_facts(
    result: ScopeLineageResult,
    all_scopes: list[Scope],
    schema: dict | None = None,
) -> None:
    """Populate additional per-scope facts for refactor-oriented analysis.

    Four phases, each a named function below. A new pass joins one of the phases -- the
    wiring guard test red-flags any pass function defined but not reachable from here.
    """
    _populate_structural_facts(result, all_scopes, schema)
    _run_resolution_rounds(result)
    _refresh_detail_resolutions(result)
    # A second convergence segment, not a repeat by accident: the refresh passes above
    # rewrite aggregate/window resolutions, and the rounds must settle again afterwards.
    # Safe as a loop only since the internal pass became idempotent on settled outputs
    # (see _should_rebuild_internal_expansion_from_expression).
    _run_resolution_rounds(result)
    _finalize_facts(result, schema)


def _populate_structural_facts(
    result: ScopeLineageResult,
    all_scopes: list[Scope],
    schema: dict | None,
) -> None:
    """Phase 1 -- structure. Writes scope raw_sql, input_edges, logic_blocks, bindings,
    union_branch_alignment, outputs, field_usage, and final target columns. No
    expression_resolution content yet."""
    for sg_scope in all_scopes:
        scope_id = getattr(sg_scope, _SCOPE_ID_ATTR, None)
        if not scope_id or scope_id not in result.scopes:
            continue
        scope_data = result.scopes[scope_id]
        _populate_scope_sql(scope_data, sg_scope)
        _populate_input_edges(scope_data, sg_scope)
        _populate_logic_blocks(scope_id, scope_data, sg_scope, result, schema)
    # A MERGE's ROOT has no sqlglot scope, so the loop above never reaches it.
    _populate_merge_root_input_edges(result)
    _populate_scope_input_source_refs(result)
    _populate_scope_raw_sql_quality_and_source_coverage(result)
    _populate_scope_alias_source_bindings(result)
    _populate_scope_expression_source_bindings(result)
    _populate_union_branch_alignment(result)
    _populate_scope_outputs(result)
    _populate_scope_field_usage(result, schema)
    _populate_final_targets(result)


def _refresh_detail_resolutions(result: ScopeLineageResult) -> None:
    """Phase 3 -- detail refresh. Rewrites aggregate and window outputs'
    expression_resolution with their detail facts; runs once between the two
    convergence segments."""
    _refresh_aggregation_detail_expression_resolution(result)
    _refresh_window_detail_expression_resolution(result)
    _refresh_window_output_expression_resolutions(result)


def _finalize_facts(result: ScopeLineageResult, schema: dict | None) -> None:
    """Phase 5 -- settlement. Reads the settled resolutions to finish reintroduced
    references, build union branch mappings and provenance traces, derive mapping
    chains, fact gaps, and logic-block features; prunes star warnings."""
    # Runs after the expansion passes have settled: it can only finish references those
    # passes reintroduced, so there is nothing to do until they stop changing the text.
    _finish_reintroduced_expansions(result)
    _restore_facts_behind_unexpanded_refs(result)
    _populate_union_output_branch_mappings(result)
    _normalize_scope_expression_resolutions(result)
    _refresh_join_relation_physical_fields(result)
    _populate_window_filter_links(result)
    _populate_field_mapping_chains(result)
    _populate_lineage_fact_gaps(result)
    _prune_resolved_star_warnings(result)
    _populate_logic_block_features(result)



def _restore_facts_behind_unexpanded_refs(result: ScopeLineageResult) -> None:
    """Guarantee that declining to inline text never drops a source fact (PERF-001).

    Several passes derive an output's physical and generated sources partly from its expanded
    text. That is sound only while the text is complete: once the expansion budget declines a
    substitution, the upstream field's name is no longer in the string even though the field is
    still a real source of the value. Rather than audit every pass for that assumption, this
    runs after all of them and restores the invariant directly — for each reference that was
    declined, the upstream output's own sources are unioned back in.

    Ordering, transform, status and the expanded text are untouched; only the source sets grow,
    and only back to what a full expansion would have produced.
    """
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_data in result.scopes.values():
        for output in scope_data.outputs:
            if not output.unexpanded_refs:
                continue
            resolution = output.expression_resolution
            if not resolution:
                continue
            physical = [dict(item) for item in resolution.get("physical_source_fields") or []
                        if isinstance(item, dict)]
            seen = {(str(i.get("table") or ""), str(i.get("field") or "")) for i in physical}
            generated = [dict(item) for item in resolution.get("generated_sources") or []
                         if isinstance(item, dict)]
            grew = False
            for ref in output.unexpanded_refs:
                upstream = output_lookup.get((str(ref.get("scope_id") or ""), str(ref.get("field") or "")))
                if upstream is None:
                    continue
                upstream_resolution = upstream.expression_resolution or {}
                for item in upstream_resolution.get("physical_source_fields") or []:
                    if not isinstance(item, dict):
                        continue
                    key = (str(item.get("table") or ""), str(item.get("field") or ""))
                    if not key[0] or not key[1] or key in seen:
                        continue
                    seen.add(key)
                    physical.append(dict(item))
                    grew = True
                before = len(generated)
                generated = _dedupe_generated_source_dicts([
                    *generated,
                    *[dict(i) for i in upstream_resolution.get("generated_sources") or []
                      if isinstance(i, dict)],
                ])
                grew = grew or len(generated) != before
            if not grew:
                continue
            resolution["physical_source_fields"] = physical
            resolution["generated_sources"] = generated
            resolution["source_kind"] = _source_kind_for_resolution(
                physical, generated,
                [i for i in resolution.get("rowset_sources") or [] if isinstance(i, dict)],
                rowset_dominates_generated=True,
            )


def _populate_scope_sql(scope_data: ScopeData, sg_scope: Scope) -> None:
    try:
        scope_data.raw_sql = sg_scope.expression.sql(dialect=DIALECT)
        scope_data.raw_sql_available = bool(scope_data.raw_sql)
    except Exception:  # noqa: BLE001 - raw_sql is evidence, not structure; absence is recorded as raw_sql_available=False
        scope_data.raw_sql = None
        scope_data.raw_sql_available = False


def _populate_input_edges(scope_data: ScopeData, sg_scope: Scope) -> None:
    if not isinstance(sg_scope.expression, exp.Select):
        return

    edges: list[ScopeInputEdge] = []
    from_ = sg_scope.expression.args.get("from_")
    if from_ is not None:
        source = getattr(from_, "this", None)
        item = _source_item_from_ast_node(source, sg_scope)
        if item:
            alias, src = item
            # A PIVOT is the relation downstream references, so its alias is the one that
            # has to appear here. sqlglot keeps the pivoted subquery's own alias in
            # scope.sources, and using that left `p.A` with no alias to bind (PIVOT-001).
            pivot = _pivot_of_source_node(source)
            pivot_alias = getattr(pivot, "alias", None) if pivot is not None else None
            edges.append(
                ScopeInputEdge(
                    source_id=_source_id_for_input(src),
                    source_type=_source_type_for_input(src),
                    alias=pivot_alias or alias,
                    position="from",
                )
            )

    for index, join_ast in enumerate(sg_scope.expression.args.get("joins") or []):
        item = _source_item_from_ast_node(join_ast.this, sg_scope)
        join_data = scope_data.joins[index] if index < len(scope_data.joins) else None
        if item:
            alias, src = item
            source_id = _source_id_for_input(src)
            source_type = _source_type_for_input(src)
        else:
            alias = getattr(join_ast.this, "alias", None)
            source_id = join_data.right_scope if join_data else "UNKNOWN"
            source_type = _source_type_from_id(source_id)
        edges.append(
            ScopeInputEdge(
                source_id=source_id,
                source_type=source_type,
                alias=alias,
                position="join",
                join_type=join_data.join_type if join_data else _join_type_from_ast(join_ast),
                join_condition=join_data.condition_expression if join_data else None,
                join_fields=list(join_data.condition_columns) if join_data else [],
            )
        )

    for udtf_scope in getattr(sg_scope, "udtf_scopes", []) or []:
        alias = _find_alias_in_parent(udtf_scope)
        source_id = _source_id_for_input(udtf_scope)
        edges.append(
            ScopeInputEdge(
                source_id=source_id,
                source_type=_source_type_for_input(udtf_scope),
                alias=alias or _alias_from_scope_id(source_id),
                position="lateral_view",
            )
        )

    scope_data.input_edges = edges


def _populate_merge_root_input_edges(result: ScopeLineageResult) -> None:
    """Declare the relations a MERGE reads on its ROOT scope.

    Input edges come from walking the sqlglot scopes, and a MERGE's ROOT is synthetic — it
    has no sqlglot scope, so that walk never reaches it and the scope declared no inputs at
    all. Everything downstream reads ``alias_source_bindings``, so ``source`` looked like an
    unknown alias: an expression that resolves a qualifier by alias, such as
    ``COALESCE(target.x, source.y)``, reported a root-impact gap for a binding column
    resolution had already made (MERGE-INPUT-001).

    Both relations carry their alias, so a consumer can map ``target.x`` or ``source.x``
    back to the relation it names. The target is then held out of ``alias_source_bindings``
    by ``_populate_scope_alias_source_bindings``: that table drives alias expansion, and the
    correlated ``target.id`` that MERGE action subqueries keep by design (see
    ``_protect_merge_correlated_target_refs``) lives inside a scalar subquery's text where
    no rewrite in this scope reaches it — binding the alias would make the unexpanded-alias
    check read a deliberate reference as an expansion that failed. Declared, not
    alias-expanded.

    Both use ``position="from"``: the contract constrains that field to a closed set, and
    both are relations this statement reads from. The target is appended rather than placed
    first — ``input_ref_id`` is positional, so leading with it would renumber the USING
    relation's reference that consumers already hold.

    A missing USING scope is left out rather than guessed at: that would be an internal
    invariant already broken upstream, and inventing an edge would hide it.
    """
    if result.stmt_kind != "MERGE":
        return
    root = result.scopes.get("ROOT")
    if root is None or root.input_edges:
        return
    edges: list[ScopeInputEdge] = []
    if result.merge_using_scope_id:
        edges.append(
            ScopeInputEdge(
                source_id=result.merge_using_scope_id,
                source_type=_source_type_from_id(result.merge_using_scope_id),
                alias=(
                    result.merge_using_alias
                    or _alias_from_scope_id(result.merge_using_scope_id)
                    or "source"
                ),
                # "from" and not a new enum value: the contract constrains position to
                # from/join/lateral_view, and the USING relation genuinely is the relation
                # this statement reads from.
                position="from",
            )
        )
    if result.target_table:
        edges.append(
            ScopeInputEdge(
                source_id=result.target_table,
                source_type="physical_table",
                alias=result.merge_target_alias or result.target_table,
                position="from",
            )
        )
    root.input_edges = edges


def _alias_from_scope_id(scope_id: str) -> str | None:
    if not scope_id or ":" not in scope_id:
        return None
    return scope_id.split(":", 1)[1] or None


def _source_id_for_input(src: Scope | exp.Table) -> str:
    if isinstance(src, Scope):
        return getattr(src, _SCOPE_ID_ATTR, None) or "UNKNOWN"
    if isinstance(src, exp.Table):
        return _qualified_table(src)
    return "UNKNOWN"


def _source_type_for_input(src: Scope | exp.Table) -> str:
    if isinstance(src, exp.Table):
        return "physical_table"
    if isinstance(src, Scope):
        return "scope"
    return "unknown"


def _populate_scope_input_source_refs(result: ScopeLineageResult) -> None:
    physical_source_memo: dict[str, list[str]] = {}
    for scope_id, scope_data in result.scopes.items():
        refs: list[dict] = []
        for index, edge in enumerate(scope_data.input_edges, start=1):
            physical_source_ids = _physical_source_ids_for_input(
                result,
                edge.source_id,
                memo=physical_source_memo,
            )
            source_resolution = _source_resolution_for_input(
                result,
                edge.source_id,
                edge.source_type,
                physical_source_memo=physical_source_memo,
            )
            trace_status = "complete" if source_resolution["status"] == "resolved" else "scope_only"
            source_scope_id = edge.source_id if _is_internal_scope_id(edge.source_id) else None
            refs.append(
                {
                    "input_ref_id": f"input:{scope_id}:{index:03d}",
                    "source_id": edge.source_id,
                    "source_type": edge.source_type,
                    **({"source_scope_id": source_scope_id} if source_scope_id else {}),
                    "alias": edge.alias,
                    "position": edge.position,
                    "relation_position": edge.position,
                    "join_type": edge.join_type,
                    "physical_source_id": physical_source_ids[0] if len(physical_source_ids) == 1 else None,
                    "physical_source_ids": physical_source_ids,
                    "source_resolution": source_resolution,
                    "field_resolution_required": source_resolution["field_resolution_required"],
                    "binding_status": source_resolution["status"],
                    "binding_trace": _binding_trace_for_input(edge.alias, edge.source_id, edge.source_type, physical_source_ids),
                    "trace_status": trace_status,
                }
            )
        scope_data.input_source_refs = refs


def _source_resolution_for_input(
    result: ScopeLineageResult,
    source_id: str,
    source_type: str,
    *,
    physical_source_memo: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    physical_source_ids = _physical_source_ids_for_input(
        result,
        source_id,
        memo=physical_source_memo,
    )
    missing_reasons: list[str] = []

    if source_type == "physical_table" and physical_source_ids:
        resolution_type = "direct_physical_source"
        cardinality = "single_source"
        field_resolution_required = False
    elif _is_internal_scope_id(source_id):
        field_resolution_required = True
        if len(physical_source_ids) == 1:
            resolution_type = "scope_single_physical_source"
            cardinality = "single_source"
        elif len(physical_source_ids) > 1:
            resolution_type = "scope_multi_physical_source"
            cardinality = "multi_source"
        else:
            resolution_type = "scope_physical_source_unresolved"
            cardinality = "unresolved"
            missing_reasons.append("physical_source_tables_unresolved")
    else:
        resolution_type = "unknown_source"
        cardinality = "unresolved"
        field_resolution_required = True
        missing_reasons.append("unknown_source")

    return {
        "status": "resolved" if physical_source_ids else "unresolved",
        "resolution_type": resolution_type,
        "cardinality": cardinality,
        "physical_source_tables": physical_source_ids,
        "field_resolution_required": field_resolution_required,
        "missing_reasons": missing_reasons,
    }


def _binding_trace_for_input(
    alias: str | None,
    source_id: str,
    source_type: str,
    physical_source_ids: list[str],
) -> list[dict[str, str]]:
    from_label = alias or source_id
    if source_type == "physical_table":
        return [
            {
                "from": from_label,
                "to": source_id,
                "relation": "alias_to_physical_source",
            }
        ]

    trace = [
        {
            "from": from_label,
            "to": source_id,
            "relation": "alias_to_scope",
        }
    ]
    if physical_source_ids:
        trace.extend(
            {
                "from": source_id,
                "to": physical_source,
                "relation": "scope_to_physical_source",
            }
            for physical_source in physical_source_ids
        )
    else:
        trace.append(
            {
                "from": source_id,
                "to": "UNKNOWN",
                "relation": "scope_to_physical_source_unresolved",
            }
        )
    return trace


def _populate_scope_raw_sql_quality_and_source_coverage(result: ScopeLineageResult) -> None:
    for _scope_id, scope_data in result.scopes.items():
        scope_data.raw_sql_quality = _raw_sql_quality(scope_data.raw_sql)
        scope_data.source_coverage = _source_coverage(scope_data)


def _raw_sql_quality(raw_sql: str | None) -> dict[str, object]:
    evidence = _raw_sql_placeholder_evidence(raw_sql or "")
    if not evidence:
        return {"status": "clean", "placeholder_types": [], "evidence": []}
    return {
        "status": "contains_placeholder",
        "placeholder_types": _unique_ordered([str(item["placeholder_type"]) for item in evidence]),
        "evidence": evidence,
    }


def _raw_sql_placeholder_evidence(raw_sql: str) -> list[dict[str, str]]:
    patterns = [
        ("todo_comment", r"\bTODO\b|<TODO>|\$\{TODO\}"),
        ("fixme_comment", r"\bFIXME\b"),
        ("pending_confirmation", r"待确认"),
        ("placeholder_text", r"占位"),
        ("xxx_placeholder", r"\bxxx\b"),
    ]
    evidence: list[dict[str, str]] = []
    for placeholder_type, pattern in patterns:
        for match in re.finditer(pattern, raw_sql, flags=re.I):
            start = max(0, match.start() - 80)
            end = min(len(raw_sql), match.end() + 80)
            item = {
                "placeholder_type": placeholder_type,
                "token": match.group(0),
                "snippet": raw_sql[start:end].strip(),
            }
            if item not in evidence:
                evidence.append(item)
    return evidence


def _source_coverage(scope_data: ScopeData) -> dict[str, object]:
    declared = _scope_declared_source_tables(scope_data)
    raw_sql_sources = _scope_raw_sql_source_tables(scope_data)
    missing = [source for source in declared if source not in set(raw_sql_sources)]
    extra = [source for source in raw_sql_sources if source not in set(declared)]
    status = "covered"
    if missing:
        status = "missing_declared_source_table"
    elif extra:
        status = "has_extra_source_table"
    return {
        "status": status,
        "declared_source_tables": declared,
        "raw_sql_source_tables": raw_sql_sources,
        "missing_declared_source_tables": missing,
        "extra_source_tables": extra,
    }


def _scope_declared_source_tables(scope_data: ScopeData) -> list[str]:
    tables: list[str] = []
    for ref in scope_data.input_source_refs:
        for source in ref.get("physical_source_ids") or []:
            if source not in tables:
                tables.append(str(source))
    return tables


def _scope_raw_sql_source_tables(scope_data: ScopeData) -> list[str]:
    return _scope_declared_source_tables(scope_data)


def _is_merge_target_input(
    result: ScopeLineageResult,
    scope_id: str,
    ref: dict,
) -> bool:
    """The MERGE target: declared as an input, held out of alias expansion.

    Alias bindings are what expression expansion resolves qualifiers through. A MERGE's
    correlated ``target.id`` is preserved on purpose inside an action subquery's text, and
    no rewrite in the ROOT scope reaches into it — so binding ``target`` here would make the
    unexpanded-alias check report that deliberate reference as an expansion that failed.
    The relation still appears in ``inputs`` with its alias, which is what a consumer needs
    to map ``target.x`` back to it (MERGE-INPUT-001).
    """
    return (
        result.stmt_kind == "MERGE"
        and scope_id == "ROOT"
        and bool(result.target_table)
        and ref.get("source_id") == result.target_table
    )


def _populate_scope_alias_source_bindings(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        bindings: list[dict] = []
        for ref in scope_data.input_source_refs:
            alias = ref.get("alias")
            if not alias or _is_merge_target_input(result, scope_id, ref):
                continue
            bindings.append(
                {
                    "alias": alias,
                    "source_id": ref.get("source_id"),
                    "source_type": ref.get("source_type"),
                    **({"source_scope_id": ref.get("source_scope_id")} if ref.get("source_scope_id") else {}),
                    "physical_source_id": ref.get("physical_source_id"),
                    "physical_source_ids": ref.get("physical_source_ids") or [],
                    "position": ref.get("position"),
                    "relation_position": ref.get("relation_position") or ref.get("position"),
                    "join_type": ref.get("join_type"),
                    "source_resolution": ref.get("source_resolution") or {},
                    "field_resolution_required": bool(ref.get("field_resolution_required")),
                    "binding_status": ref.get("binding_status")
                    or ("resolved" if ref.get("physical_source_id") or ref.get("physical_source_ids") else "unresolved"),
                    "binding_trace": ref.get("binding_trace") or [],
                    "input_ref_id": ref.get("input_ref_id"),
                }
            )
        scope_data.alias_source_bindings = bindings


def _populate_scope_expression_source_bindings(result: ScopeLineageResult) -> None:
    for _scope_id, scope_data in result.scopes.items():
        bindings: list[dict] = []
        seen: set[tuple[str, str | None]] = set()
        for block in scope_data.logic_blocks:
            for expression_sql, fields, evidence_path in _logic_block_binding_inputs(block):
                if not expression_sql:
                    continue
                key = (expression_sql, evidence_path)
                if key in seen:
                    continue
                seen.add(key)
                binding = _expression_source_binding(
                    expression_sql,
                    fields,
                    scope_data.alias_source_bindings,
                    evidence_path=evidence_path,
                    logic_block_id=block.logic_block_id,
                )
                if binding["referenced_qualifiers"]:
                    bindings.append(binding)
        scope_data.expression_source_bindings = bindings


def _logic_block_binding_inputs(block: ScopeLogicBlock) -> list[tuple[str | None, list[SourceRef], str | None]]:
    inputs: list[tuple[str | None, list[SourceRef], str | None]] = []
    if block.raw_expression:
        inputs.append((block.raw_expression, list(block.fields), f"logic_blocks.{block.logic_block_id}.raw_expression"))
    detail = block.join_relation_detail or {}
    for pair in detail.get("join_key_pairs") or []:
        fields = _source_refs_from_detail_fields([pair.get("left"), pair.get("right")])
        inputs.append((pair.get("expression"), fields, f"logic_blocks.{block.logic_block_id}.join_relation_detail.join_key_pairs"))
    for item in detail.get("condition_filters") or []:
        fields = _source_refs_from_detail_fields(item.get("fields") or [])
        inputs.append((item.get("expression"), fields, f"logic_blocks.{block.logic_block_id}.join_relation_detail.condition_filters"))
    filter_detail = block.filter_predicate_detail or {}
    for item in filter_detail.get("conjuncts") or []:
        fields = _source_refs_from_detail_fields(item.get("fields") or [])
        inputs.append((item.get("expression"), fields, f"logic_blocks.{block.logic_block_id}.filter_predicate_detail.conjuncts"))
    return inputs


def _expression_source_binding(
    expression_sql: str,
    fields: list[SourceRef],
    alias_bindings: list[dict],
    *,
    evidence_path: str | None,
    logic_block_id: str | None,
) -> dict:
    alias_by_name = {item.get("alias"): item for item in alias_bindings if item.get("alias")}
    refs_by_scope_column = {(ref.scope, ref.column): ref for ref in fields}
    resolved_refs: list[dict] = []
    unresolved: list[str] = []
    for qualifier, field in _qualified_field_refs(expression_sql):
        alias_binding = alias_by_name.get(qualifier)
        if not alias_binding:
            if qualifier not in unresolved:
                unresolved.append(qualifier)
            continue
        resolved_table = alias_binding.get("physical_source_id") or alias_binding.get("source_id")
        if (resolved_table, field) not in refs_by_scope_column and resolved_table not in {ref.scope for ref in fields}:
            status = "resolved_by_alias"
        else:
            status = "resolved"
        item = {
            "qualifier": qualifier,
            "resolved_table": resolved_table,
            "field": field,
            "status": status,
        }
        if item not in resolved_refs:
            resolved_refs.append(item)
    return {
        "expression_sql": expression_sql,
        "logic_block_id": logic_block_id,
        "referenced_qualifiers": sorted({qualifier for qualifier, _field in _qualified_field_refs(expression_sql)}),
        "resolved_source_refs": resolved_refs,
        "unresolved_qualifiers": sorted(unresolved),
        "binding_status": "complete" if not unresolved else "incomplete",
        "evidence_path": evidence_path,
    }


def _populate_union_branch_alignment(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        if scope_data.kind != "union":
            continue
        branch_ids = list(scope_data.branches or [])
        branches = [
            _union_branch_alignment_item(result, branch_id)
            for branch_id in branch_ids
            if branch_id in result.scopes
        ]
        field_alignment = _union_field_alignment(scope_data, branches)
        expected_width = len(scope_data.columns)
        trace_status = "complete"
        if len(branches) != len(branch_ids) or any(
            len(branch.get("select_items") or []) != expected_width
            for branch in branches
        ):
            trace_status = "incomplete"
        scope_data.union_branch_alignment = {
            "scope_id": scope_id,
            "set_op": scope_data.set_op,
            "branch_count": len(branch_ids),
            "branches": branches,
            "field_alignment": field_alignment,
            "trace_status": trace_status,
        }


def _union_branch_alignment_item(result: ScopeLineageResult, branch_id: str) -> dict[str, object]:
    branch = result.scopes[branch_id]
    source_tables: list[str] = []
    for input_ref in branch.input_source_refs:
        for physical_source_id in input_ref.get("physical_source_ids") or []:
            if physical_source_id not in source_tables:
                source_tables.append(physical_source_id)
    select_items = [
        _union_select_item(branch, column, position=index)
        for index, column in enumerate(branch.columns, start=1)
    ]
    return {
        "branch_id": branch_id,
        "branch_index": branch.branch_index,
        "source_tables": source_tables,
        "input_sources": _input_source_refs_for_alignment(branch.input_source_refs),
        "select_items": select_items,
    }


def _input_source_refs_for_alignment(input_source_refs: list[dict]) -> list[dict]:
    return [
        {
            "source_id": ref.get("source_id"),
            "source_type": ref.get("source_type"),
            "alias": ref.get("alias"),
            "position": ref.get("position"),
            "physical_source_ids": list(ref.get("physical_source_ids") or []),
        }
        for ref in input_source_refs
    ]


def _union_select_item(scope_data: ScopeData, column: ScopeColumn, *, position: int) -> dict[str, object]:
    expression_fact = _expression_resolution_for_scope_column(scope_data, column)
    return {
        "position": position,
        "output_name": column.name,
        "expression_sql": column.expression,
        "expanded_expression": expression_fact.get("expanded_expression"),
        "expression_resolution": expression_fact.get("expression_resolution") or {},
        "transform": column.transform,
        "source_fields": [_source_ref_to_dict(ref) for ref in column.sources],
    }


def _union_field_alignment(scope_data: ScopeData, branches: list[dict]) -> list[dict]:
    alignment: list[dict] = []
    for position, column in enumerate(scope_data.columns, start=1):
        branch_items: list[dict] = []
        for branch in branches:
            select_items = branch.get("select_items") or []
            if position > len(select_items):
                branch_items.append(
                    {
                        "branch_id": branch.get("branch_id"),
                        "branch_index": branch.get("branch_index"),
                        "position": position,
                        "missing": True,
                    }
                )
                continue
            item = dict(select_items[position - 1])
            item["branch_id"] = branch.get("branch_id")
            item["branch_index"] = branch.get("branch_index")
            branch_items.append(item)
        alignment.append(
            {
                "position": position,
                "aligned_output_name": column.name,
                "branch_items": branch_items,
            }
        )
    return alignment


def _populate_window_filter_links(result: ScopeLineageResult) -> None:
    filter_refs: dict[tuple[str, str], list[dict[str, object]]] = {}
    for scope_id, scope_data in result.scopes.items():
        for block in scope_data.logic_blocks:
            if block.logic_type not in {"filter", "having"}:
                continue
            fields = [_source_ref_to_dict(ref) for ref in block.fields]
            link = {
                "scope_id": scope_id,
                "logic_block_id": block.logic_block_id,
                "expression": block.raw_expression,
                "fields": fields,
            }
            for ref in block.fields:
                filter_refs.setdefault((ref.scope, ref.column), []).append(link)

    for scope_id, scope_data in result.scopes.items():
        for block in scope_data.logic_blocks:
            if block.logic_type != "window" or not block.output_fields:
                continue
            output_field = block.output_fields[0]
            links = filter_refs.get((scope_id, output_field), [])
            if links and block.window_specification:
                block.window_specification["filter_after_window"] = links


def _populate_field_mapping_chains(result: ScopeLineageResult) -> None:
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    target_outputs = [
        (scope_id, output)
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
        if output.target_columns
    ]
    chains: list[dict[str, object]] = []
    for index, (scope_id, output) in enumerate(target_outputs, start=1):
        steps: list[dict[str, object]] = []
        root_sources: list[str] = []
        trace_state: dict[str, object] = {
            "complete": True,
            "missing_reasons": [],
        }
        _collect_field_mapping_steps(
            result,
            scope_id,
            output.name,
            output_lookup,
            steps,
            root_sources,
            trace_state,
            seen=set(),
            current_output=output,
        )
        deduped_steps = _dedupe_mapping_steps(steps)
        final_output_fields = list(output.target_columns)
        for step_no, step in enumerate(deduped_steps, start=1):
            step["step_no"] = step_no
            step["final_output_fields"] = final_output_fields
        chain_resolution = _normalize_expression_resolution(
            output.expression_resolution or {},
            scope_id=scope_id,
            field=output.name,
            expression=output.expanded_expression or output.expression,
        )
        missing_reasons = _unique_ordered(
            [
                *[
                    str(reason)
                    for reason in chain_resolution.get("missing_reasons") or []
                    if reason
                ],
                *[
                    str(reason)
                    for reason in trace_state.get("missing_reasons") or []
                    if reason
                ],
            ]
        )
        trace_complete = bool(
            trace_state.get("complete")
            and chain_resolution.get("status") == "resolved"
            and not missing_reasons
        )
        chain_status = str(chain_resolution.get("status") or "unresolved")
        if not trace_complete and chain_status == "resolved":
            chain_status = "partially_resolved"
        chains.append(
            {
                "mapping_chain_id": f"mc:{index:03d}",
                "chain_id": (
                    f"chain:{scope_id}:{output.name}"
                    f":position:{output.output_ordinal}"
                ),
                "chain_type": "field_mapping",
                "target_scope_id": scope_id,
                "target_field": output.name,
                "target_position": output.output_ordinal,
                **(
                    {"merge_branch": output.merge_branch}
                    if output.merge_branch is not None
                    else {}
                ),
                **(
                    {"merge_branch_qualifier": output.merge_branch_qualifier}
                    if output.merge_branch_qualifier is not None
                    else {}
                ),
                **(
                    {"merge_when_index": output.merge_when_index}
                    if output.merge_when_index is not None
                    else {}
                ),
                "chain_status": chain_status,
                "source_kind": chain_resolution["source_kind"],
                "root_source_fields": root_sources,
                "final_output_fields": final_output_fields,
                "ordered_steps": deduped_steps,
                "expanded_expression": chain_resolution.get("expanded_expression") or output.expanded_expression,
                "missing_reasons": missing_reasons,
                "trace_status": "complete" if trace_complete else "incomplete",
            }
        )
    result.field_mapping_chains = chains


def _collect_field_mapping_steps(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
    steps: list[dict[str, object]],
    root_sources: list[str],
    trace_state: dict[str, bool],
    *,
    seen: set[tuple[str, str]],
    current_output: ScopeOutputField | None = None,
) -> None:
    key = (scope_id, column_name)
    if key in seen:
        _mark_mapping_trace_incomplete(
            trace_state,
            f"mapping_cycle_detected:{scope_id}.{column_name}",
        )
        return
    seen.add(key)
    output = current_output or output_lookup.get(key)
    if output is None:
        _mark_mapping_trace_incomplete(
            trace_state,
            f"upstream_output_not_found:{scope_id}.{column_name}",
        )
        _extend_unique(root_sources, [_scope_field_id(scope_id, column_name)])
        return

    for source in output.sources:
        source_key = (source.scope, source.column)
        if source_key in output_lookup:
            _collect_field_mapping_steps(
                result,
                source.scope,
                source.column,
                output_lookup,
                steps,
                root_sources,
                trace_state,
                seen=set(seen),
            )
        else:
            if (
                _is_internal_scope_id(source.scope)
                and not _is_resolved_rowset_terminal(output, source)
            ):
                _mark_mapping_trace_incomplete(
                    trace_state,
                    f"upstream_output_not_found:{source.scope}.{source.column}",
                )
            _extend_unique(root_sources, [_source_field_id(source)])

    steps.append(_field_mapping_step(scope_id, output))


def _is_resolved_rowset_terminal(
    output: ScopeOutputField,
    source: SourceRef,
) -> bool:
    """A ``scope.*`` aggregate input is a rowset fact, not a missing column output."""
    resolution = output.expression_resolution or {}
    return bool(
        source.column == "*"
        and resolution.get("status") == "resolved"
        and not (resolution.get("missing_reasons") or [])
        and resolution.get("source_kind") in {
            "rowset",
            "generated",
            "mixed",
        }
    )


def _mark_mapping_trace_incomplete(
    trace_state: dict[str, object],
    reason: str,
) -> None:
    trace_state["complete"] = False
    reasons = trace_state.setdefault("missing_reasons", [])
    if isinstance(reasons, list) and reason not in reasons:
        reasons.append(reason)


def _field_mapping_step(scope_id: str, output: ScopeOutputField) -> dict[str, object]:
    final_fields = list(output.final_target_columns or output.target_columns or [])
    return {
        "step_no": 0,
        "scope_id": scope_id,
        "step_type": _mapping_step_type(output),
        "input_fields": [_source_field_id(ref) for ref in output.sources],
        "output_field": _mapping_output_field(scope_id, output),
        "expression_sql": output.expression,
        "expanded_expression": output.expanded_expression,
        "expression_resolution": dict(output.expression_resolution or {}),
        "logic_ids": list(output.source_logic_blocks),
        "transform": output.transform,
        "grain_effect": output.grain_effect or "unknown",
        "final_output_fields": final_fields,
        **(
            {"target_position": output.output_ordinal}
            if scope_id == "ROOT" and output.output_ordinal is not None
            else {}
        ),
        **(
            {"merge_branch": output.merge_branch}
            if output.merge_branch is not None
            else {}
        ),
        **(
            {"merge_branch_qualifier": output.merge_branch_qualifier}
            if output.merge_branch_qualifier is not None
            else {}
        ),
        **(
            {"merge_when_index": output.merge_when_index}
            if output.merge_when_index is not None
            else {}
        ),
    }


def _mapping_step_type(output: ScopeOutputField) -> str:
    if output.transform == "DIRECT":
        return "direct_projection"
    if output.transform == "CONDITIONAL":
        return "case_when"
    if output.transform == "AGGREGATE":
        return "aggregate"
    if output.transform == "WINDOW":
        return "window"
    if output.transform == "CONSTANT":
        return "constant"
    if output.transform == "UNION":
        return "union"
    return "expression"


def _mapping_output_field(scope_id: str, output: ScopeOutputField) -> str:
    if output.target_columns:
        return output.target_columns[0]
    return _scope_field_id(scope_id, output.name)


def _scope_field_id(scope_id: str, column_name: str) -> str:
    return f"{scope_id}.{column_name}"


def _source_field_id(ref: SourceRef) -> str:
    return _scope_field_id(ref.scope, ref.column)


def _dedupe_mapping_steps(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for step in steps:
        key = (step.get("scope_id"), step.get("output_field"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(step)
    return deduped


def _populate_scope_outputs(result: ScopeLineageResult) -> None:
    downstream = _downstream_index(result)
    for scope_id, scope_data in result.scopes.items():
        logic_by_output: dict[str, list[str]] = {}
        for block in scope_data.logic_blocks:
            for output_field in block.output_fields:
                logic_by_output.setdefault(output_field, []).append(block.logic_block_id)

        outputs: list[ScopeOutputField] = []
        for output_ordinal, column in enumerate(scope_data.columns):
            target_columns = []
            if scope_data.writes_to and column.name and _is_reliable_target_output_name(column):
                target_columns.append(f"{scope_data.writes_to}.{column.name}")
            source_logic_blocks = list(logic_by_output.get(column.name, []))
            if column.parsed_name and column.parsed_name != column.name:
                for logic_block_id in logic_by_output.get(column.parsed_name, []):
                    if logic_block_id not in source_logic_blocks:
                        source_logic_blocks.append(logic_block_id)
            expression_resolution = _expression_resolution_for_scope_column(scope_data, column)
            outputs.append(
                ScopeOutputField(
                    name=column.name,
                    transform=column.transform,
                    expression=column.expression,
                    expanded_expression=expression_resolution.get("expanded_expression"),
                    expression_resolution=expression_resolution.get("expression_resolution") or {},
                    expression_type=_expression_type_for_column(column),
                    expression_features=_expression_features_for_column(column),
                    expression_role=_expression_role_for_column(column),
                    grain_effect=_grain_effect_for_column(column),
                    sources=list(column.sources),
                    source_logic_blocks=source_logic_blocks,
                    downstream_fields=downstream.get((scope_id, column.name), []),
                    target_columns=target_columns,
                    output_ordinal=output_ordinal,
                    merge_branch=column.merge_branch,
                    merge_branch_qualifier=column.merge_branch_qualifier,
                    merge_when_index=column.merge_when_index,
                )
            )
        scope_data.outputs = outputs


def _is_reliable_target_output_name(column) -> bool:
    """Return whether a scope output name is safe to treat as a physical target field."""
    if column.target_field_resolution:
        return True
    return not column.name_is_generated and not re.fullmatch(r"\d+", str(column.name or ""))


def _consumer_readiness_for_resolution(resolution: dict[str, object]) -> dict[str, object]:
    status = str(resolution.get("status") or "unresolved")
    missing_reasons = [
        str(reason)
        for reason in resolution.get("missing_reasons") or []
        if reason
    ]
    ready = status == "resolved" and not missing_reasons
    return {
        "status": "ready" if ready else "blocked",
        "blocked_reasons": [] if ready else _unique_ordered(
            missing_reasons or [f"expression_resolution_{status}"]
        ),
    }


def _normalize_scope_expression_resolutions(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        for output in scope_data.outputs:
            output.expression_resolution = _normalize_expression_resolution(
                output.expression_resolution or {},
                scope_id=scope_id,
                field=output.name,
                expression=output.expanded_expression or output.expression,
            )
            _attach_udtf_and_path_facts(scope_id, scope_data, output)
            _mark_unexpanded_bound_aliases(scope_data, output)
            output.consumer_readiness = _consumer_readiness_for_resolution(
                output.expression_resolution
            )
        for block in scope_data.logic_blocks:
            detail = block.aggregation_detail or {}
            for item_key in ("group_by_items", "aggregate_items", "filter_predicates"):
                for item in detail.get(item_key) or []:
                    if not isinstance(item, dict) or "expression_resolution" not in item:
                        continue
                    item["expression_resolution"] = _normalize_expression_resolution(
                        item.get("expression_resolution") or {},
                        scope_id=scope_id,
                        field=str(item.get("output_field") or item.get("expression_sql") or ""),
                        expression=str(item.get("expanded_expression") or item.get("expression_sql") or ""),
                    )
        for output in scope_data.outputs:
            _attach_window_output_resolution(scope_data, output)
            output.expression_resolution = _normalize_expression_resolution(
                output.expression_resolution or {},
                scope_id=scope_id,
                field=output.name,
                expression=output.expanded_expression or output.expression,
            )
            _mark_unexpanded_bound_aliases(scope_data, output)
            output.consumer_readiness = _consumer_readiness_for_resolution(
                output.expression_resolution
            )
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_id, scope_data in result.scopes.items():
        for output in scope_data.outputs:
            if not output.expression_resolution.get("scope_output_trace"):
                trace = _scope_output_trace_for_output(
                    result,
                    scope_id,
                    output,
                    output_lookup,
                    seen=set(),
                )
                if trace:
                    output.expression_resolution["scope_output_trace"] = trace


def _attach_window_output_resolution(scope_data: ScopeData, output: ScopeOutputField) -> None:
    resolution = dict(output.expression_resolution or {})
    if resolution.get("resolution_type") != "rowset_window_function":
        return
    window_spec = _window_specification_for_output(scope_data, output.name)
    if not window_spec:
        return
    dependency_items = [
        item
        for item in [
            *(window_spec.get("partition_by") or []),
            *(window_spec.get("order_by") or []),
        ]
        if isinstance(item, dict)
    ]
    if not dependency_items:
        return
    physical_fields: list[dict[str, object]] = []
    generated_sources: list[dict[str, object]] = []
    missing_reasons: list[str] = []
    expanded_expression = str(output.expanded_expression or output.expression or "")
    for item in dependency_items:
        item_resolution = item.get("expression_resolution") or {}
        physical_fields.extend(
            dict(field)
            for field in item_resolution.get("physical_source_fields") or []
            if isinstance(field, dict)
        )
        generated_sources.extend(
            dict(source)
            for source in item_resolution.get("generated_sources") or []
            if isinstance(source, dict)
        )
        if item_resolution.get("status") != "resolved":
            missing_reasons.extend(str(reason) for reason in item_resolution.get("missing_reasons") or [] if reason)
        item_expression = str(item.get("expression_sql") or "")
        item_expanded = str(item.get("expanded_expression") or item_resolution.get("expanded_expression") or item_expression)
        if item_expression and item_expanded and item_expression != item_expanded:
            expanded_expression = expanded_expression.replace(item_expression, item_expanded)
    physical_fields = _ordered_physical_fields_in_expression(
        expanded_expression,
        _dedupe_physical_field_dicts(physical_fields),
    )
    generated_sources = _dedupe_generated_source_dicts(generated_sources)
    existing_rowset_sources = [
        dict(source)
        for source in resolution.get("rowset_sources") or []
        if isinstance(source, dict)
    ]
    rowset_sources = _dedupe_rowset_source_dicts(
        existing_rowset_sources
        or [
            {
                "source_type": "rowset",
                "scope": "",
                "field": output.name,
                "expression": expanded_expression,
            }
        ]
    )
    missing_reasons = _unique_ordered(missing_reasons)
    status = "resolved"
    if missing_reasons:
        status = "partially_resolved" if physical_fields or generated_sources else "unresolved"
        missing_reasons = _unique_ordered(["window_dependency_unresolved", *missing_reasons])
    output.expanded_expression = expanded_expression
    output.expression_resolution = {
        **resolution,
        "status": status,
        "physical_source_fields": physical_fields,
        "generated_sources": generated_sources,
        "rowset_sources": rowset_sources,
        "source_kind": _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources),
        "missing_reasons": missing_reasons,
        "expanded_expression": expanded_expression,
    }


def _refresh_window_output_expression_resolutions(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        for output in scope_data.outputs:
            _attach_window_output_resolution(scope_data, output)


def _mark_unexpanded_bound_aliases(scope_data: ScopeData, output: ScopeOutputField) -> None:
    resolution = output.expression_resolution or {}
    if resolution.get("status") != "resolved":
        return
    expression = str(resolution.get("expanded_expression") or output.expanded_expression or output.expression or "")
    unresolved_aliases = _unexpanded_bound_aliases_in_expression(scope_data, expression)
    # A reference the expansion budget DECLINED is not an unresolved one: the upstream output
    # was found and its facts were taken, only its text was not inlined, and `unexpanded_refs`
    # says exactly which and why. Treating it as unresolved would demote the output to
    # partially_resolved, and every downstream output would then refuse to take its physical
    # sources — turning a size limit into lineage loss (PERF-001).
    if output.expansion_status != "full":
        declined = {str(ref.get("scope_id") or "").split(":", 1)[-1] for ref in output.unexpanded_refs}
        unresolved_aliases = [alias for alias in unresolved_aliases if alias not in declined]
    if not unresolved_aliases:
        return
    physical_fields = [
        dict(item)
        for item in resolution.get("physical_source_fields") or []
        if isinstance(item, dict)
    ]
    generated_sources = [
        dict(item)
        for item in resolution.get("generated_sources") or []
        if isinstance(item, dict)
    ]
    rowset_sources = [
        dict(item)
        for item in resolution.get("rowset_sources") or []
        if isinstance(item, dict)
    ]
    missing_reasons = _unique_ordered(
        [
            *[str(reason) for reason in resolution.get("missing_reasons") or [] if reason],
            *[
                f"expanded_expression_contains_unexpanded_alias:{alias}"
                for alias in sorted(set(unresolved_aliases))
            ],
        ]
    )
    output.expression_resolution = {
        **resolution,
        "status": "partially_resolved" if physical_fields or generated_sources else "unresolved",
        "missing_reasons": missing_reasons,
        "unresolved_qualifiers": _unique_ordered(
            [
                *[str(item) for item in resolution.get("unresolved_qualifiers") or [] if item],
                *unresolved_aliases,
            ]
        ),
        "physical_source_fields": physical_fields,
        "generated_sources": generated_sources,
        **({"rowset_sources": rowset_sources} if rowset_sources else {}),
    }


def _window_specification_for_output(scope_data: ScopeData, output_name: str) -> dict[str, object] | None:
    for block in scope_data.logic_blocks:
        if block.logic_type != "window":
            continue
        spec = block.window_specification or {}
        if spec.get("output_field") == output_name:
            return spec
    return None


def _scope_output_trace_step(
    *,
    step: int,
    from_scope_id: str,
    from_field: str,
    to_scope_id: str | None,
    to_field: str | None,
    relation: str,
    expression_sql: str | None,
    resolution_status: str,
    physical_source_fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "step": step,
        "from_scope_id": from_scope_id,
        "from_field": from_field,
        "to_scope_id": to_scope_id,
        "to_field": to_field,
        "relation": relation,
        "expression_sql": expression_sql,
        "resolution_status": resolution_status,
    }
    if physical_source_fields:
        item["physical_source_fields"] = physical_source_fields
    return item


def _scope_output_trace_for_output(
    result: ScopeLineageResult,
    scope_id: str,
    output: ScopeOutputField,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
    *,
    seen: set[tuple[str, str]],
) -> list[dict[str, object]]:
    key = (scope_id, output.name)
    if key in seen:
        return []
    seen.add(key)
    resolution = output.expression_resolution or {}
    status = str(resolution.get("status") or "unresolved")
    source_scope_id = str(resolution.get("source_scope_id") or "")
    source_output_field = str(resolution.get("source_output_field") or "")
    if source_scope_id and source_output_field and _is_internal_scope_id(source_scope_id):
        trace = [
            _scope_output_trace_step(
                step=1,
                from_scope_id=scope_id,
                from_field=output.name,
                to_scope_id=source_scope_id,
                to_field=source_output_field,
                relation="scope_projection",
                expression_sql=output.expression,
                resolution_status=status,
            )
        ]
        upstream = output_lookup.get((source_scope_id, source_output_field))
        if upstream is not None:
            upstream_trace = list((upstream.expression_resolution or {}).get("scope_output_trace") or [])
            if not upstream_trace:
                upstream_trace = _scope_output_trace_for_output(
                    result,
                    source_scope_id,
                    upstream,
                    output_lookup,
                    seen=set(seen),
                )
            trace.extend(_renumber_scope_output_trace(upstream_trace, start=len(trace) + 1))
        return trace
    physical_fields = [
        dict(item)
        for item in resolution.get("physical_source_fields") or []
        if isinstance(item, dict)
    ]
    if status == "resolved" and physical_fields:
        return [
            _scope_output_trace_step(
                step=1,
                from_scope_id=scope_id,
                from_field=output.name,
                to_scope_id=None,
                to_field=None,
                relation="physical_expression",
                expression_sql=output.expression,
                resolution_status=status,
                physical_source_fields=physical_fields,
            )
        ]
    return []


def _renumber_scope_output_trace(
    trace: list[dict[str, object]],
    *,
    start: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for offset, item in enumerate(trace):
        copied = dict(item)
        copied["step"] = start + offset
        result.append(copied)
    return result


def _attach_udtf_and_path_facts(
    scope_id: str,
    scope_data: ScopeData,
    output: ScopeOutputField,
) -> None:
    resolution = output.expression_resolution
    if scope_id.startswith("udtf:"):
        lateral = scope_data.lateral_views[0] if scope_data.lateral_views else {}
        resolution["udtf_output_binding"] = {
            "input_expression": lateral.get("expression") or output.expression,
            "output_alias": output.name,
            "source_expression": output.expanded_expression or output.expression,
            "function": lateral.get("function"),
        }
        resolution["field_path"] = [output.name]
        resolution["base_physical_source_fields"] = list(
            resolution.get("physical_source_fields") or []
        )
        return
    if any(source.scope.startswith("udtf:") for source in output.sources):
        field_path = _field_path_from_expression(output.expression)
        if field_path:
            resolution["field_path"] = field_path
            resolution["base_physical_source_fields"] = list(
                resolution.get("physical_source_fields") or []
            )


def _field_path_from_expression(expression: str | None) -> list[str]:
    if not expression:
        return []
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except sqlglot.errors.SqlglotError:
        return []
    for column in parsed.find_all(exp.Column):
        parts = [
            str(part.name or "")
            for part in column.parts
            if getattr(part, "name", None)
        ]
        if len(parts) >= 2:
            return parts
    return []


def _source_ref_from_dict(value: object) -> SourceRef | None:
    if not isinstance(value, dict):
        return None
    scope = value.get("scope")
    column = value.get("column")
    if not scope or not column:
        return None
    candidates = [
        {"scope": str(item["scope"]), "column": str(item["column"])}
        for item in value.get("candidates") or []
        if isinstance(item, dict) and item.get("scope") and item.get("column")
    ]
    return SourceRef(
        scope=str(scope),
        column=str(column),
        candidates=candidates,
        qualifier=(str(value["qualifier"]) if value.get("qualifier") else None),
        binding_scope_id=(
            str(value["binding_scope_id"])
            if value.get("binding_scope_id")
            else None
        ),
        input_ref_id=(
            str(value["input_ref_id"])
            if value.get("input_ref_id")
            else None
        ),
    )


def _refresh_join_relation_physical_fields(result: ScopeLineageResult) -> None:
    for scope_id, scope_data in result.scopes.items():
        for block in scope_data.logic_blocks:
            detail = block.join_relation_detail
            if not detail:
                continue
            for pair in detail.get("join_key_pairs") or []:
                if not isinstance(pair, dict):
                    continue
                left_ref = _source_ref_from_dict(pair.get("left"))
                right_ref = _source_ref_from_dict(pair.get("right"))
                if left_ref is not None:
                    pair["left_fields"] = _physical_source_fields_for_refs(result, [left_ref])
                if right_ref is not None:
                    pair["right_fields"] = _physical_source_fields_for_refs(result, [right_ref])
            for condition_filter in detail.get("condition_filters") or []:
                if not isinstance(condition_filter, dict):
                    continue
                refs = [
                    ref
                    for ref in (
                        _source_ref_from_dict(item)
                        for item in condition_filter.get("fields") or []
                    )
                    if ref is not None
                ]
                condition_filter["physical_fields"] = _physical_source_fields_for_refs(result, refs)

            missing_reasons = [
                reason
                for reason in detail.get("missing_reasons") or []
                if reason != "join_key_physical_fields_unresolved"
            ]
            if any(
                not pair.get("left_fields") or not pair.get("right_fields")
                for pair in detail.get("join_key_pairs") or []
                if isinstance(pair, dict)
            ):
                detail["trace_status"] = "partial"
                missing_reasons.append("join_key_physical_fields_unresolved")
            elif detail.get("join_key_pairs") or _is_cross_join_type(str(detail.get("join_type") or "")):
                detail["trace_status"] = "complete"
            else:
                detail["trace_status"] = "partial"
                if "missing_join_key_pairs" not in missing_reasons:
                    missing_reasons.append("missing_join_key_pairs")
            detail["missing_reasons"] = _unique_ordered(missing_reasons)


# Substitution can put a qualifier back into the text it just produced, so expansion is
# a fixed point, not a single step. The bound exists only so a pathological expression
# cannot loop; reaching it leaves the unexpanded-alias reason in place rather than
# declaring the expansion finished.
_EXPRESSION_EXPANSION_ROUNDS = 4


def _finish_reintroduced_expansions(result: ScopeLineageResult) -> None:
    """Expand references that expansion itself spliced back into the text.

    Expanding a reference inlines the upstream output's own expression, and that text can
    name an alias belonging to the *consuming* scope: a LATERAL VIEW is written in terms of
    the alias feeding it, so ``x.item`` becomes ``EXPLODE(t.items)``. The work list is built
    from the original expression, so ``t.items`` is never revisited — it stays in the text,
    and because the physical source list is filtered to fields the text actually mentions,
    ``ods.source.items`` is dropped. The output is then published as an incomplete fact
    while every one of its sources is in fact knowable.

    Only aliases bound in this scope are followed, and only upstream outputs that are
    themselves resolved contribute, so nothing here invents a source: it finishes work the
    first pass already proved was possible.
    """
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for _round in range(_EXPRESSION_EXPANSION_ROUNDS):
        changed = False
        for scope_data in result.scopes.values():
            alias_to_source = {
                str(binding.get("alias")): str(binding.get("source_id"))
                for binding in scope_data.alias_source_bindings
                if binding.get("alias") and binding.get("source_id")
            }
            if not alias_to_source:
                continue
            for output in scope_data.outputs:
                if _expand_reintroduced_refs(output, alias_to_source, output_lookup):
                    changed = True
        if not changed:
            return


def _expand_reintroduced_refs(
    output: ScopeOutputField,
    alias_to_source: dict[str, str],
    output_lookup: dict[tuple[str, str], ScopeOutputField],
) -> bool:
    resolution = output.expression_resolution or {}
    expanded = str(output.expanded_expression or "")
    original = str(output.expression or "")
    if not expanded or expanded == original or output.expansion_status != "full":
        return False
    original_refs = set(_qualified_field_refs(original))
    physical_fields = [
        dict(item)
        for item in resolution.get("physical_source_fields") or []
        if isinstance(item, dict)
    ]
    field_keys = {
        (str(item.get("table") or ""), str(item.get("field") or ""))
        for item in physical_fields
    }
    changed = False
    for qualifier, field in _qualified_field_refs(expanded):
        if (qualifier, field) in original_refs:
            continue
        source_id = alias_to_source.get(qualifier)
        if not source_id or not _is_internal_scope_id(source_id):
            continue
        upstream = output_lookup.get((source_id, field))
        upstream_resolution = (upstream.expression_resolution or {}) if upstream else {}
        replacement = str(
            upstream_resolution.get("expanded_expression")
            or (upstream.expanded_expression if upstream else "")
            or ""
        )
        if upstream_resolution.get("status") != "resolved" or not replacement:
            continue
        rewritten = _replace_qualified_ref_with_expression(
            expanded, qualifier, field, replacement
        )
        if rewritten == expanded:
            continue
        expanded = rewritten
        changed = True
        for physical_field in upstream_resolution.get("physical_source_fields") or []:
            if not isinstance(physical_field, dict):
                continue
            key = (
                str(physical_field.get("table") or ""),
                str(physical_field.get("field") or ""),
            )
            if not key[0] or not key[1] or key in field_keys:
                continue
            field_keys.add(key)
            physical_fields.append(dict(physical_field))
    if not changed:
        return False
    output.expanded_expression = expanded
    output.expression_resolution = {
        **resolution,
        "physical_source_fields": physical_fields,
        "expanded_expression": expanded,
    }
    return True


def _resolve_internal_scope_expression_resolution(result: ScopeLineageResult) -> None:  # noqa: C901 - legacy exemption (WI-11): shrink when next touched
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_id, scope_data in result.scopes.items():
        alias_to_source = {
            str(binding.get("alias")): str(binding.get("source_id"))
            for binding in scope_data.alias_source_bindings
            if binding.get("alias") and binding.get("source_id")
        }
        if not alias_to_source:
            continue
        for output in scope_data.outputs:
            current_resolution = output.expression_resolution or {}
            expression = output.expression or ""
            qualified_refs = _qualified_field_refs(expression)
            if not qualified_refs:
                continue
            has_struct_member_access = _has_qualified_struct_member_access(
                expression, qualified_refs
            )
            expanded_expression = output.expanded_expression or expression
            rebuild_internal_expansion = _should_rebuild_internal_expansion_from_expression(
                current_resolution,
                qualified_refs,
                alias_to_source,
                expanded_expression,
            )
            if (
                current_resolution.get("status") == "resolved"
                and (
                    current_resolution.get("physical_source_fields")
                    or current_resolution.get("generated_sources")
                    or current_resolution.get("rowset_sources")
                    or current_resolution.get("source_kind") == "rowset"
                )
                and current_resolution.get("source_scope_id")
                and not rebuild_internal_expansion
            ):
                continue
            if (
                current_resolution.get("status") == "resolved"
                and current_resolution.get("source_kind") == "rowset"
                and not rebuild_internal_expansion
            ):
                continue
            if rebuild_internal_expansion:
                expanded_expression = expression
            physical_fields: list[dict[str, str]] = [
                dict(item)
                for item in current_resolution.get("physical_source_fields") or []
                if isinstance(item, dict)
            ]
            physical_field_keys: set[tuple[str, str]] = {
                (str(item.get("table") or ""), str(item.get("field") or ""))
                for item in physical_fields
                if item.get("table") and item.get("field")
            }
            generated_sources = _dedupe_generated_source_dicts(
                [
                    dict(item)
                    for item in current_resolution.get("generated_sources") or []
                    if isinstance(item, dict)
                ]
            )
            rowset_sources: list[dict[str, str]] = [
                dict(item)
                for item in current_resolution.get("rowset_sources") or []
                if isinstance(item, dict)
            ]
            missing_reasons: list[str] = []
            # One budget per output: the expression must not exceed the limit no matter how
            # many upstream references contribute to it (PERF-001).
            budget = ExpansionBudget()
            resolved_internal_ref = False
            resolved_source_scope_ids: list[str] = []
            resolved_source_output_fields: list[str] = []
            resolved_upstream_transforms: list[str] = []
            scope_output_trace: list[dict[str, object]] = []
            for qualifier, field in qualified_refs:
                # ``alias_to_source`` necessarily collapses repeated SQL aliases to one
                # entry.  Column resolution has already retained the concrete input
                # occurrence on ``output.sources``; prefer that binding so expressions
                # referencing the first of two repeated LATERAL VIEW aliases do not get
                # rebound to the last occurrence.
                source_id = _udtf_source_id_from_output_sources(
                    output, qualifier, field, output_lookup
                )
                if not source_id:
                    source_id = alias_to_source.get(qualifier)
                if not source_id or not _is_internal_scope_id(source_id):
                    continue
                upstream = output_lookup.get((source_id, field))
                if upstream is None:
                    upstream_fact = _star_passthrough_output_fact(result, source_id, field, output_lookup)
                    if upstream_fact is None:
                        missing_reasons.append(f"upstream_output_not_found:{source_id}.{field}")
                        continue
                    upstream_fields = upstream_fact.get("physical_source_fields") or []
                    upstream_generated_sources = upstream_fact.get("generated_sources") or []
                    upstream_rowset_sources = upstream_fact.get("rowset_sources") or []
                    upstream_expanded_expression = str(upstream_fact.get("expanded_expression") or "")
                    upstream_transform = "DIRECT"
                else:
                    upstream_resolution = upstream.expression_resolution or {}
                    upstream_fields = upstream_resolution.get("physical_source_fields") or []
                    upstream_generated_sources = upstream_resolution.get("generated_sources") or []
                    upstream_rowset_sources = _rowset_sources_from_upstream_output(source_id, field, upstream)
                    upstream_source_kind = str(upstream_resolution.get("source_kind") or "")
                    if (
                        upstream_resolution.get("status") != "resolved"
                        or not (upstream_fields or upstream_generated_sources or upstream_rowset_sources or upstream_source_kind == "rowset")
                        or not upstream.expanded_expression
                    ):
                        upstream_fact = _star_passthrough_output_fact(result, source_id, field, output_lookup)
                        if upstream_fact is None:
                            missing_reasons.append(f"upstream_output_unresolved:{source_id}.{field}")
                            continue
                        upstream_fields = upstream_fact.get("physical_source_fields") or []
                        upstream_generated_sources = upstream_fact.get("generated_sources") or []
                        upstream_rowset_sources = upstream_fact.get("rowset_sources") or []
                        upstream_expanded_expression = str(upstream_fact.get("expanded_expression") or "")
                    else:
                        upstream_expanded_expression = str(upstream.expanded_expression)
                    upstream_transform = str(upstream.transform or "")
                expanded_expression = budget.substitute(
                    expanded_expression,
                    upstream_expanded_expression,
                    lambda expr, repl: _replace_struct_field_access_from_upstream(
                        expr, field, repl
                    ),
                    ref=field,
                    scope_id=source_id,
                    field=field,
                )
                expanded_expression = budget.substitute(
                    expanded_expression,
                    upstream_expanded_expression,
                    lambda expr, repl: _replace_qualified_ref_with_expression(
                        expr, qualifier, field, repl
                    ),
                    ref=f"{qualifier}.{field}",
                    scope_id=source_id,
                    field=field,
                )
                for physical_field in upstream_fields:
                    if not isinstance(physical_field, dict):
                        continue
                    key = (str(physical_field.get("table") or ""), str(physical_field.get("field") or ""))
                    if not key[0] or not key[1] or key in physical_field_keys:
                        continue
                    physical_field_keys.add(key)
                    physical_fields.append(dict(physical_field))
                generated_sources = _dedupe_generated_source_dicts(
                    [
                        *generated_sources,
                        *[
                            dict(item)
                            for item in upstream_generated_sources
                            if isinstance(item, dict)
                        ],
                    ]
                )
                rowset_sources = _dedupe_rowset_source_dicts(
                    [
                        *rowset_sources,
                        *[
                            dict(item)
                            for item in upstream_rowset_sources
                            if isinstance(item, dict)
                        ],
                    ]
                )
                if source_id not in resolved_source_scope_ids:
                    resolved_source_scope_ids.append(source_id)
                if field not in resolved_source_output_fields:
                    resolved_source_output_fields.append(field)
                if upstream_transform and upstream_transform not in resolved_upstream_transforms:
                    resolved_upstream_transforms.append(upstream_transform)
                scope_output_trace.append(
                    {
                        "from_scope_id": scope_id,
                        "from_field": output.name,
                        "to_scope_id": source_id,
                        "to_field": field,
                        "relation": (
                            "expanded_rowset_expression"
                            if upstream_rowset_sources
                            else "expanded_from_upstream_scope_expression"
                        ),
                        "expression_sql": f"`{qualifier}`.`{field}`",
                        "expanded_expression": upstream_expanded_expression,
                        "status": "resolved",
                    }
                )
                resolved_internal_ref = True
            if not resolved_internal_ref:
                continue
            physical_fields = (
                (
                    _physical_fields_referenced_in_expression(
                        expanded_expression, physical_fields
                    )
                    or _ordered_physical_fields_in_expression(
                        expanded_expression, physical_fields
                    )
                )
                if has_struct_member_access and not budget.stop_reason
                else _ordered_physical_fields_in_expression(
                    expanded_expression, physical_fields
                )
            )
            if not physical_fields and not generated_sources and not rowset_sources:
                continue
            output.expanded_expression = expanded_expression
            output.expansion_status = budget.status
            output.expansion_stop_reason = budget.stop_reason
            output.unexpanded_refs = list(budget.skipped_refs)
            status = "resolved" if not missing_reasons else "partially_resolved"
            source_anchor = {}
            if len(resolved_source_scope_ids) == 1:
                source_anchor["source_scope_id"] = resolved_source_scope_ids[0]
            if len(resolved_source_output_fields) == 1:
                source_anchor["source_output_field"] = resolved_source_output_fields[0]
            output.expression_resolution = {
                "status": status,
                "resolution_type": (
                    "expanded_from_upstream_scope"
                    if (
                        output.transform == "DIRECT"
                        and len(qualified_refs) == 1
                        and resolved_upstream_transforms == ["DIRECT"]
                    )
                    else "expanded_from_upstream_scope_expression"
                ),
                "physical_source_fields": physical_fields,
                "generated_sources": generated_sources,
                **({"rowset_sources": rowset_sources} if rowset_sources else {}),
                "source_kind": _source_kind_for_resolution(
                    physical_fields,
                    generated_sources,
                    rowset_sources,
                    rowset_dominates_generated=True,
                ),
                "missing_reasons": missing_reasons,
                **({"scope_output_trace": scope_output_trace} if scope_output_trace else {}),
                **source_anchor,
            }


# Asked once per reference per pass over the same expressions, and in a profiled run those
# repeated calls cost a fifth of the total. Depends only on its arguments (PERF-002).
_STRUCT_MEMBER_ACCESS_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], bool] = {}


def _has_qualified_struct_member_access(
    expression: str,
    qualified_refs: list[tuple[str, str]],
) -> bool:
    key = (expression, tuple(qualified_refs))
    cached = _STRUCT_MEMBER_ACCESS_CACHE.get(key)
    if cached is None:
        cached = _has_qualified_struct_member_access_uncached(expression, qualified_refs)
        _STRUCT_MEMBER_ACCESS_CACHE[key] = cached
    return cached


def _has_qualified_struct_member_access_uncached(
    expression: str,
    qualified_refs: list[tuple[str, str]],
) -> bool:
    for qualifier, field in qualified_refs:
        escaped_qualifier = re.escape(qualifier)
        escaped_field = re.escape(field)
        if _cached_pattern(
            rf"`{escaped_qualifier}`\.`{escaped_field}`\.`[^`]+`"
        ).search(expression):
            return True
        if _cached_pattern(
            rf"(?<![.`\w]){escaped_qualifier}\.{escaped_field}\."
            rf"[A-Za-z_][A-Za-z0-9_]*(?![`.\w])"
        ).search(expression):
            return True
    return False


def _refresh_aggregation_detail_expression_resolution(result: ScopeLineageResult) -> None:
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_data in result.scopes.values():
        alias_to_source = {
            str(binding.get("alias")): str(binding.get("source_id"))
            for binding in scope_data.alias_source_bindings
            if binding.get("alias") and binding.get("source_id")
        }
        if not alias_to_source:
            continue
        for block in scope_data.logic_blocks:
            detail = block.aggregation_detail
            if not detail:
                continue
            for item in [*(detail.get("group_by_items") or []), *(detail.get("aggregate_items") or [])]:
                if not isinstance(item, dict):
                    continue
                current_resolution = item.get("expression_resolution") or {}
                if current_resolution.get("status") == "resolved" and not current_resolution.get("missing_reasons"):
                    continue
                refreshed = _resolved_expression_fact_from_matching_scope_output(
                    scope_data,
                    str(item.get("expression_sql") or ""),
                )
                if not refreshed:
                    refreshed = _resolved_expression_fact_from_unqualified_scope_outputs(
                        scope_data,
                        str(item.get("expression_sql") or ""),
                    )
                if not refreshed:
                    refreshed = _resolved_scope_alias_expression_fact(
                        str(item.get("expression_sql") or ""),
                        alias_to_source,
                        output_lookup,
                    )
                if not refreshed:
                    refs = _source_refs_from_detail_fields(item.get("source_fields") or [])
                    refreshed = _resolved_expression_fact_from_source_refs(
                        result,
                        str(item.get("expression_sql") or ""),
                        refs,
                        output_lookup,
                    )
                if not refreshed:
                    continue
                item["expanded_expression"] = refreshed["expanded_expression"]
                item["expression_resolution"] = refreshed["expression_resolution"]
                item["physical_source_fields"] = refreshed["expression_resolution"]["physical_source_fields"]
                item["trace_status"] = "complete" if refreshed["expression_resolution"]["status"] == "resolved" else "partial"


def _refresh_window_detail_expression_resolution(result: ScopeLineageResult) -> None:
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_data in result.scopes.values():
        alias_to_source = {
            str(binding.get("alias")): str(binding.get("source_id"))
            for binding in scope_data.alias_source_bindings
            if binding.get("alias") and binding.get("source_id")
        }
        if not alias_to_source:
            continue
        for block in scope_data.logic_blocks:
            if block.logic_type != "window" or not block.window_specification:
                continue
            for item in [
                *(block.window_specification.get("partition_by") or []),
                *(block.window_specification.get("order_by") or []),
            ]:
                if not isinstance(item, dict):
                    continue
                current_resolution = item.get("expression_resolution") or {}
                if current_resolution.get("status") == "resolved" and not current_resolution.get("missing_reasons"):
                    continue
                expression_sql = str(item.get("expression_sql") or "")
                refreshed = _resolved_scope_alias_expression_fact(
                    expression_sql,
                    alias_to_source,
                    output_lookup,
                )
                if not refreshed:
                    refs = _source_refs_from_detail_fields(item.get("source_fields") or [])
                    refreshed = _resolved_expression_fact_from_source_refs(
                        result,
                        expression_sql,
                        refs,
                        output_lookup,
                    )
                if not refreshed:
                    continue
                item["expanded_expression"] = refreshed["expanded_expression"]
                item["expression_resolution"] = refreshed["expression_resolution"]
                item["physical_source_fields"] = refreshed["expression_resolution"]["physical_source_fields"]


def _resolved_expression_fact_from_matching_scope_output(
    scope_data: ScopeData,
    expression: str,
) -> dict[str, object] | None:
    expression_key = _normalize_expression_for_matching(expression)
    if not expression_key:
        return None
    for output in scope_data.outputs:
        if _normalize_expression_for_matching(output.expression) != expression_key:
            continue
        resolution = output.expression_resolution or {}
        physical_fields = resolution.get("physical_source_fields") or []
        generated_sources = resolution.get("generated_sources") or []
        rowset_sources = resolution.get("rowset_sources") or []
        if resolution.get("status") != "resolved" or not (
            physical_fields or generated_sources or rowset_sources or resolution.get("source_kind") == "rowset"
        ):
            continue
        return {
            "expanded_expression": output.expanded_expression or output.expression or expression,
            "expression_resolution": {
                "status": "resolved",
                "resolution_type": "matched_scope_output_expression",
                "physical_source_fields": [dict(item) for item in physical_fields if isinstance(item, dict)],
                "generated_sources": _dedupe_generated_source_dicts(
                    [dict(item) for item in generated_sources if isinstance(item, dict)]
                ),
                **({"rowset_sources": [dict(item) for item in rowset_sources if isinstance(item, dict)]} if rowset_sources else {}),
                "source_kind": str(resolution.get("source_kind") or _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources)),
                "missing_reasons": [],
            },
        }
    return None


def _resolved_expression_fact_from_unqualified_scope_outputs(
    scope_data: ScopeData,
    expression: str,
) -> dict[str, object] | None:
    fields = _unqualified_field_refs(expression)
    if not fields:
        return None
    outputs_by_name = {output.name: output for output in scope_data.outputs}
    expanded_expression = expression
    budget = ExpansionBudget()
    physical_fields: list[dict[str, str]] = []
    generated_sources: list[dict[str, str]] = []
    rowset_sources: list[dict[str, str]] = []
    resolved_any = False
    for field in fields:
        output = outputs_by_name.get(field)
        if output is None:
            continue
        resolution = output.expression_resolution or {}
        output_physical_fields = resolution.get("physical_source_fields") or []
        output_generated_sources = resolution.get("generated_sources") or []
        output_rowset_sources = resolution.get("rowset_sources") or []
        if resolution.get("status") != "resolved" or not (
            output_physical_fields
            or output_generated_sources
            or output_rowset_sources
            or resolution.get("source_kind") == "rowset"
        ):
            continue
        replacement = str(output.expanded_expression or output.expression or "")
        if replacement:
            expanded_expression = budget.substitute(
                expanded_expression,
                replacement,
                lambda expr, repl: _replace_unqualified_ref_with_expression(expr, field, repl),
                ref=field,
                field=field,
            )
        physical_fields.extend(
            dict(item)
            for item in output_physical_fields
            if isinstance(item, dict)
        )
        generated_sources = _dedupe_generated_source_dicts(
            [
                *generated_sources,
                *[
                    dict(item)
                    for item in output_generated_sources
                    if isinstance(item, dict)
                ],
            ]
        )
        rowset_sources = _dedupe_rowset_source_dicts(
            [
                *rowset_sources,
                *[
                    dict(item)
                    for item in output_rowset_sources
                    if isinstance(item, dict)
                ],
            ]
        )
        if str(resolution.get("source_kind") or "") == "rowset" and not output_rowset_sources:
            rowset_sources = _dedupe_rowset_source_dicts(
                [
                    *rowset_sources,
                    {
                        "source_type": "rowset",
                        "scope": "",
                        "field": field,
                        "expression": replacement,
                    },
                ]
            )
        resolved_any = True
    if not resolved_any:
        return None
    physical_fields = _ordered_physical_fields_in_expression(
        expanded_expression,
        _dedupe_physical_field_dicts(physical_fields),
    )
    generated_sources = _dedupe_generated_source_dicts(generated_sources)
    rowset_sources = _dedupe_rowset_source_dicts(rowset_sources)
    if not physical_fields and not generated_sources and not rowset_sources:
        return None
    return {
        "expanded_expression": expanded_expression,
        "expression_resolution": {
            "status": "resolved",
            "resolution_type": "expanded_from_scope_output_alias",
            "physical_source_fields": physical_fields,
            "generated_sources": generated_sources,
            **({"rowset_sources": rowset_sources} if rowset_sources else {}),
            "source_kind": _source_kind_for_resolution(
                physical_fields,
                generated_sources,
                rowset_sources,
                rowset_dominates_generated=True,
            ),
            "missing_reasons": [],
        },
    }


def _unqualified_field_refs(expression: str | None) -> list[str]:
    if not expression:
        return []
    try:
        parsed = sqlglot.parse_one(expression, dialect=DIALECT, **PARSE_OPTS)
    except sqlglot.errors.SqlglotError:
        return []
    fields: list[str] = []
    seen: set[str] = set()
    for column in parsed.find_all(exp.Column):
        if column.table:
            continue
        field = str(column.name or "")
        if not field or field in seen:
            continue
        seen.add(field)
        fields.append(field)
    return fields


def _normalize_expression_for_matching(expression: str | None) -> str:
    if not expression:
        return ""
    return re.sub(r"\s+", " ", _strip_sql_comments(str(expression)).strip()).lower()


def _resolved_scope_alias_expression_fact(
    expression: str,
    alias_to_source: dict[str, str],
    output_lookup: dict[tuple[str, str], ScopeOutputField],
) -> dict[str, object] | None:
    if not expression:
        return None
    qualified_refs = _qualified_field_refs(expression)
    if not qualified_refs:
        return None
    expanded_expression = expression
    budget = ExpansionBudget()
    physical_fields: list[dict[str, str]] = []
    generated_sources: list[dict[str, str]] = []
    rowset_sources: list[dict[str, str]] = []
    missing_reasons: list[str] = []
    resolved_internal_ref = False
    for qualifier, field in qualified_refs:
        source_id = alias_to_source.get(qualifier)
        if not source_id:
            continue
        if not _is_internal_scope_id(source_id):
            # A physical table has no upstream output to inline, but the reference is
            # already fully resolved: the alias names the table, and the table names the
            # field. Skipping it left the alias in the text and the field out of the
            # source list — and the list is filtered by the text afterwards, so the field
            # could not be recovered later either (MIXED-ALIAS-001). An expression built
            # only from physical references still returns None below, leaving it to the
            # candidate that handles that shape.
            rewritten = _replace_qualified_ref_with_expression(
                expanded_expression,
                qualifier,
                field,
                _qualified_physical_field_sql(source_id, field),
            )
            if rewritten == expanded_expression:
                continue
            expanded_expression = rewritten
            physical_fields.append({"table": source_id, "field": field})
            continue
        upstream = output_lookup.get((source_id, field))
        if upstream is None:
            missing_reasons.append(f"upstream_output_not_found:{source_id}.{field}")
            continue
        upstream_resolution = upstream.expression_resolution or {}
        upstream_fields = upstream_resolution.get("physical_source_fields") or []
        upstream_generated_sources = upstream_resolution.get("generated_sources") or []
        upstream_rowset_sources = _rowset_sources_from_upstream_output(source_id, field, upstream)
        upstream_source_kind = str(upstream_resolution.get("source_kind") or "")
        upstream_expanded_expression = str(
            upstream_resolution.get("expanded_expression")
            or upstream.expanded_expression
            or ""
        )
        if (
            upstream_resolution.get("status") != "resolved"
            or not (upstream_fields or upstream_generated_sources or upstream_rowset_sources or upstream_source_kind == "rowset")
            or not upstream_expanded_expression
        ):
            missing_reasons.append(f"upstream_output_unresolved:{source_id}.{field}")
            continue
        expanded_expression = budget.substitute(
            expanded_expression,
            upstream_expanded_expression,
            lambda expr, repl: _replace_qualified_ref_with_expression(expr, qualifier, field, repl),
            ref=f"{qualifier}.{field}",
            scope_id=source_id,
            field=field,
        )
        physical_fields.extend(dict(item) for item in upstream_fields if isinstance(item, dict))
        generated_sources = _dedupe_generated_source_dicts(
            [
                *generated_sources,
                *[
                    dict(item)
                    for item in upstream_generated_sources
                    if isinstance(item, dict)
                ],
            ]
        )
        rowset_sources = _dedupe_rowset_source_dicts(
            [
                *rowset_sources,
                *[
                    dict(item)
                    for item in upstream_rowset_sources
                    if isinstance(item, dict)
                ],
            ]
        )
        resolved_internal_ref = True
    if not resolved_internal_ref:
        return None
    physical_fields = _ordered_physical_fields_in_expression(
        expanded_expression,
        _dedupe_physical_field_dicts(physical_fields),
    )
    if not physical_fields and not generated_sources and not rowset_sources:
        return None
    return {
        "expanded_expression": expanded_expression,
        "expression_resolution": {
            "status": "resolved" if not missing_reasons else "partially_resolved",
            "resolution_type": "expanded_from_upstream_scope_expression",
            "physical_source_fields": physical_fields,
            "generated_sources": generated_sources,
            **({"rowset_sources": rowset_sources} if rowset_sources else {}),
            "source_kind": _source_kind_for_resolution(physical_fields, generated_sources, rowset_sources),
            "missing_reasons": missing_reasons,
        },
    }


def _udtf_source_id_from_output_sources(
    output: ScopeOutputField,
    qualifier: str,
    field: str,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
) -> str | None:
    for source in output.sources:
        if (
            source.column == field
            and source.qualifier == qualifier
            and source.scope.startswith("udtf:")
            and (source.scope, field) in output_lookup
        ):
            return source.scope
    # Older callers may not populate qualifier/input occurrence metadata.  Preserve
    # the historical single-alias fallback for those results.
    candidate = f"udtf:{qualifier}"
    if (candidate, field) in output_lookup:
        return candidate
    return None


def _should_rebuild_internal_expansion_from_expression(
    current_resolution: dict[str, object],
    qualified_refs: list[tuple[str, str]],
    alias_to_source: dict[str, str],
    expanded_expression: str,
) -> bool:
    internal_refs = [
        (qualifier, field)
        for qualifier, field in qualified_refs
        if _is_internal_scope_id(alias_to_source.get(qualifier) or "")
    ]
    if not internal_refs:
        return False
    if len(internal_refs) != len(qualified_refs):
        return False
    missing_reasons = [
        str(reason)
        for reason in current_resolution.get("missing_reasons") or []
        if reason
    ]
    has_unexpanded_alias_reason = any(
        reason.startswith("expanded_expression_contains_unexpanded_alias:")
        for reason in missing_reasons
    )
    lost_original_internal_refs = not any(
        _qualified_ref_present(expanded_expression, qualifier, field)
        for qualifier, field in internal_refs
    )
    # Absent internal refs are ambiguous: expansion may have mangled the text, or it may
    # have FINISHED -- replaced every internal ref with its physical fields. Rebuilding in
    # the second case is what made this pass non-idempotent: it clobbered source_scope_id
    # and the refined resolution, and the provenance trace built later collapsed to one
    # coarse record (WI-09 root cause; the idempotence test and the WI-08 sentinel both
    # stand on this line). A completed expansion is recognizable and exempt.
    expansion_completed = (
        str(current_resolution.get("status") or "") == "resolved"
        and bool(current_resolution.get("physical_source_fields"))
        and not has_unexpanded_alias_reason
    )
    return has_unexpanded_alias_reason or (lost_original_internal_refs and not expansion_completed)


def _qualified_ref_present(expression: str, qualifier: str, field: str) -> bool:
    if f"`{qualifier}`.`{field}`" in expression:
        return True
    return bool(
        re.search(
            rf"(?<![.`\w]){re.escape(qualifier)}\.{re.escape(field)}(?![`.\w])",
            expression,
        )
    )


def _prune_resolved_star_warnings(result: ScopeLineageResult) -> None:
    retained: list[DiagnosticWarning] = []
    for warning in result.diagnostics.warnings:
        if warning.type != "star_not_expanded":
            retained.append(warning)
            continue
        scope_data = result.scopes.get(warning.scope)
        if scope_data is None or not _scope_star_warning_resolved(scope_data):
            retained.append(warning)
    result.diagnostics.warnings = retained


def _scope_star_warning_resolved(scope_data: ScopeData) -> bool:
    if not scope_data.outputs:
        return False
    for output in scope_data.outputs:
        if output.transform == "EXPAND_ALL" or _scope_output_name_is_star(output.name):
            return False
    return True


def _scope_output_name_is_star(name: str) -> bool:
    return name == "*" or str(name or "").endswith(".*")


def _downstream_index(result: ScopeLineageResult) -> dict[tuple[str, str], list[SourceRef]]:
    downstream: dict[tuple[str, str], list[SourceRef]] = {}
    for scope_id, scope_data in result.scopes.items():
        for column in scope_data.columns:
            for source in column.sources:
                downstream.setdefault((source.scope, source.column), []).append(
                    SourceRef(scope=scope_id, column=column.name)
                )
    return downstream


def _populate_scope_field_usage(result: ScopeLineageResult, schema: dict | None = None) -> None:
    for scope_data in result.scopes.values():
        grouped: dict[str, ScopeFieldUsage] = {}
        for block in scope_data.logic_blocks:
            for usage in block.field_usage:
                _enrich_field_usage_metadata(usage, schema)
                target = grouped.setdefault(
                    usage.source_id,
                    ScopeFieldUsage(
                        source_id=usage.source_id,
                        source_type=usage.source_type,
                    ),
                )
                _extend_unique(target.used_fields, usage.used_fields)
                _merge_field_details(target, usage.used_field_details)
                _extend_unique(target.used_by_logic_blocks, usage.used_by_logic_blocks)
                _extend_unique(target.used_by_output_fields, usage.used_by_output_fields)

        for output in scope_data.outputs:
            for source in output.sources:
                target = grouped.setdefault(
                    source.scope,
                    ScopeFieldUsage(
                        source_id=source.scope,
                        source_type=_source_type_from_id(source.scope),
                    ),
                )
                _extend_unique(target.used_fields, [source.column])
                if output.source_logic_blocks:
                    _extend_unique(target.used_by_logic_blocks, output.source_logic_blocks)
                _extend_unique(target.used_by_output_fields, [output.name])

        for usage in grouped.values():
            _enrich_field_usage_metadata(usage, schema)
        scope_data.field_usage = list(grouped.values())


def _populate_final_targets(result: ScopeLineageResult) -> None:
    output_lookup = {
        (scope_id, output.name): output
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    for scope_id, scope_data in result.scopes.items():
        for output in scope_data.outputs:
            output.final_target_columns = _trace_final_target_columns(
                result,
                scope_id,
                output.name,
                output_lookup,
                seen=set(),
            )

    for scope_id, scope_data in result.scopes.items():
        output_targets = {output.name: output.final_target_columns for output in scope_data.outputs}
        for block in scope_data.logic_blocks:
            targets: list[str] = []
            for output_field in block.output_fields:
                _extend_unique(targets, output_targets.get(output_field, []))
            block.final_target_columns = targets


def _trace_final_target_columns(
    result: ScopeLineageResult,
    scope_id: str,
    column_name: str,
    output_lookup: dict[tuple[str, str], ScopeOutputField],
    *,
    seen: set[tuple[str, str]],
) -> list[str]:
    key = (scope_id, column_name)
    if key in seen:
        return []
    seen.add(key)
    output = output_lookup.get(key)
    if output is None:
        return []
    if output.target_columns:
        return list(output.target_columns)

    targets: list[str] = []
    for downstream in output.downstream_fields:
        _extend_unique(
            targets,
            _trace_final_target_columns(
                result,
                downstream.scope,
                downstream.column,
                output_lookup,
                seen=set(seen),
            ),
        )
    return targets


def _populate_logic_block_features(result: ScopeLineageResult) -> None:
    for scope_data in result.scopes.values():
        for block in scope_data.logic_blocks:
            block.expression_features = _expression_features(
                block.logic_type,
                block.raw_expression,
                block.fields,
                subtype=block.subtype,
            )


def _enrich_field_usage_metadata(usage: ScopeFieldUsage, schema: dict | None) -> None:
    if schema is None:
        return
    if usage.source_type == "physical_table":
        usage.source_metadata = table_details_for_table(schema, usage.source_id)
        details_by_name = {
            detail.get("name"): detail
            for detail in column_details_for_table(schema, usage.source_id)
            if detail.get("name")
        }
        usage.used_field_details = [
            dict(details_by_name.get(field) or {"name": field, "type": None, "comment": None})
            for field in usage.used_fields
        ]


def _merge_field_details(target: ScopeFieldUsage, details: list[dict]) -> None:
    existing = {item.get("name") for item in target.used_field_details if item.get("name")}
    for detail in details:
        name = detail.get("name")
        if name and name not in existing:
            target.used_field_details.append(dict(detail))
            existing.add(name)


def _expression_features(
    logic_type: str,
    expression: str | None,
    fields: list[SourceRef],
    *,
    subtype: str | None = None,
) -> dict[str, object]:
    text = expression or ""
    lower = text.lower()
    functions = _function_names(lower)
    operators = [op for op in ["<>", "!=", ">=", "<=", "=", ">", "<", "+", "-", "*", "/"] if op in text]
    field_names = {ref.column.lower() for ref in fields}
    return {
        "functions": functions,
        "operators": operators,
        "has_case_when": logic_type == "case_when" or "case when" in lower,
        "has_if": subtype == "if" or bool(re.search(r"\bif\s*\(", lower)),
        "has_cast": subtype == "cast" or bool(re.search(r"\bcast\s*\(", lower)),
        "has_window": logic_type == "window" or " over " in lower,
        "has_aggregate": logic_type == "aggregate" or _has_aggregate_function(lower),
        "has_udf": subtype == "udf" or _has_udf_function(functions),
        "has_constant_mapping": (
            logic_type == "constant"
            or (
                logic_type == "case_when"
                and bool(re.search(r"\bthen\s+['\"0-9]", lower))
            )
        ),
        "has_partition_filter": bool(field_names & {"dt", "ds", "pt", "bizdate", "biz_date"}),
    }


def _expression_type_for_column(column) -> str:
    transform = column.transform
    features = _expression_features_for_column(column)
    if transform == "DIRECT":
        return "direct_projection"
    if transform == "UNION":
        return "direct_projection"
    if transform == "CONSTANT":
        return "constant_expression"
    if transform == "WINDOW" or features["has_window"]:
        return "window_expression"
    if transform == "AGGREGATE" or features["has_aggregate"]:
        return "aggregate_expression"
    if transform == "CONDITIONAL" or features["has_case_when"] or features["has_if"]:
        return "conditional_expression"
    if features["has_cast"]:
        return "type_cast"
    if features["has_udf"]:
        return "udf_expression"
    if features["functions"]:
        return "function_expression"
    if set(features.get("operators") or []) & {"+", "-", "*", "/"}:
        return "arithmetic_expression"
    if transform == "EXPRESSION":
        return "unknown_expression"
    return "unknown_expression"


def _expression_features_for_column(column) -> dict[str, object]:
    logic_type = {
        "CONDITIONAL": "case_when",
        "WINDOW": "window",
        "AGGREGATE": "aggregate",
        "CONSTANT": "constant",
    }.get(column.transform, "expression")
    return _expression_features(
        logic_type,
        column.expression,
        list(column.sources),
        subtype=column.transform_subkind,
    )


def _expression_role_for_column(column) -> str:
    expression_type = _expression_type_for_column(column)
    features = _expression_features_for_column(column)
    functions = set(features.get("functions") or [])
    if expression_type == "direct_projection":
        return "direct_projection"
    if expression_type == "constant_expression":
        return "constant_fill"
    if expression_type == "type_cast":
        return "type_conversion"
    if expression_type == "aggregate_expression":
        return "metric_calculation"
    if expression_type == "window_expression":
        return "record_selection"
    if expression_type == "conditional_expression" and features.get("has_constant_mapping"):
        return "standardization"
    if expression_type == "function_expression" and functions & _CLEANING_FUNCTIONS:
        return "cleaning"
    if expression_type in {"function_expression", "conditional_expression", "udf_expression", "arithmetic_expression"}:
        return "field_derivation"
    return "unknown"


def _grain_effect_for_column(column) -> str:
    expression_type = _expression_type_for_column(column)
    if expression_type == "aggregate_expression":
        return "changed"
    if expression_type == "window_expression":
        return "preserved"
    if expression_type == "unknown_expression":
        return "unknown"
    return "preserved"


def _has_aggregate_function(expression: str) -> bool:
    return bool(re.search(r"\b(" + "|".join(sorted(_AGGREGATE_FUNCTIONS)) + r")\s*\(", expression))


def _has_udf_function(functions: list[str]) -> bool:
    return any(function not in _KNOWN_SCALAR_FUNCTIONS for function in functions)


def _join_type_from_ast(join: exp.Join) -> str:
    kind = (join.kind or "").upper()
    side = (join.side or "").upper()
    if kind == "CROSS":
        return "CROSS"
    if kind == "INNER":
        return "INNER"
    if kind == "OUTER" and side in ("LEFT", "RIGHT", "FULL"):
        return f"{side}_OUTER"
    if side in ("LEFT", "RIGHT", "FULL"):
        return f"{side}_OUTER"
    return kind or "INNER"
