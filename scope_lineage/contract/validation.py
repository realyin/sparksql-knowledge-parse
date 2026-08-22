"""Schema and referential validation for Lineage contract documents."""

from __future__ import annotations

import json
from importlib import resources

from ..scope.scope_types import CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID


_schema_cache: dict[str, dict] = {}


def _load_schema() -> dict:
    return _load_packaged_schema("lineage.schema.json")


def _load_packaged_schema(name: str) -> dict:
    if name not in _schema_cache:
        schema_resource = resources.files("scope_lineage.schemas").joinpath(name)
        _schema_cache[name] = json.loads(schema_resource.read_text(encoding="utf-8"))
    # Validation libraries do not mutate schemas, and callers that need a modified test copy
    # already use copy.deepcopy. Returning a JSON round-trip keeps the old isolation contract.
    return json.loads(json.dumps(_schema_cache[name]))


def validate_lineage_document(document: dict) -> dict:
    """Validate an already-built Lineage document and return it unchanged.

    The "1.0" schema outlived the retired standalone v1 artifact on purpose: it is the
    schema of the STATEMENT document, which is exactly what a task document embeds per
    statement_lineage entry (and what the mapping renderer consumes per section).
    """
    import jsonschema

    schema_name = {
        "1.0": "lineage.schema.json",
        "2.0": "lineage-v2.schema.json",
    }.get(document.get("schema_version"), "lineage.schema.json")
    jsonschema.validate(document, _load_packaged_schema(schema_name))
    return document


def validate_diagnostics_document(document: dict) -> dict:
    """Validate an already-built diagnostics companion document."""
    import jsonschema

    schema_name = {
        "1.0": "diagnostics.schema.json",
        "2.0": "diagnostics-v2.schema.json",
    }.get(document.get("schema_version"), "diagnostics.schema.json")
    jsonschema.validate(document, _load_packaged_schema(schema_name))
    return document


def validate_cross_references(data: dict) -> list[str]:
    """Return references to scope IDs that do not exist in the document graph."""
    if data.get("schema_version") == "2.0":
        return _validate_task_cross_references(data)
    errors: list[str] = []
    known_scopes: set[str] = set(data.get("scopes", {}).keys())
    all_nodes: set[str] = set(data.get("scope_graph", {}).get("nodes", []))
    valid_ids = known_scopes | all_nodes | {CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID}

    for edge in data.get("scope_graph", {}).get("edges", []):
        for key in ("from", "to"):
            scope_id = edge.get(key)
            if scope_id and scope_id not in valid_ids:
                errors.append(
                    f"scope_graph edge {key}={scope_id!r} not in known scopes/nodes"
                )

    for scope_id, scope_data in data.get("scopes", {}).items():
        for column in scope_data.get("columns", []):
            for source in column.get("sources", []):
                source_id = source.get("scope")
                if source_id and source_id not in valid_ids and source_id != "UNKNOWN":
                    errors.append(
                        f"scope={scope_id!r} col={column.get('name')!r} "
                        f"source scope={source_id!r} not in known scopes/nodes"
                    )
    return errors


def _validate_task_cross_references(data: dict) -> list[str]:
    errors: list[str] = []
    graph = data.get("table_state_graph") or {}
    # A duplicate id makes every reference to it ambiguous rather than invalid, so each of the
    # checks below still passes while the graph has stopped being readable. That is how two
    # nodes called `state:v:001` shipped (STATE-ID-001); nothing resolved to nothing.
    seen_state_ids: set[str] = set()
    state_ids: set[str] = set()
    for node in graph.get("nodes", []):
        state_id = node.get("state_id")
        if not state_id:
            continue
        if state_id in seen_state_ids:
            errors.append(
                f"table_state_graph has a duplicate node state_id={state_id!r}; "
                "a state id must name exactly one state of one table"
            )
        seen_state_ids.add(state_id)
        state_ids.add(state_id)
    statement_ids = {
        item.get("statement_id")
        for item in data.get("statement_sequence", [])
        if item.get("statement_id")
    }
    for edge in graph.get("edges", []):
        for key in ("from", "to"):
            state_id = edge.get(key)
            if state_id not in state_ids:
                errors.append(
                    f"table_state_graph edge {key}={state_id!r} not in nodes"
                )
        if edge.get("statement_id") not in statement_ids:
            errors.append(
                "table_state_graph edge statement_id="
                f"{edge.get('statement_id')!r} not in statement_sequence"
            )
    for node in graph.get("nodes", []):
        producer = node.get("producer_statement_id")
        if producer is not None and producer not in statement_ids:
            errors.append(
                f"table state {node.get('state_id')!r} producer_statement_id="
                f"{producer!r} not in statement_sequence"
            )
    for table, state_id in (data.get("final_table_states") or {}).items():
        if state_id not in state_ids:
            errors.append(
                f"final_table_states[{table!r}]={state_id!r} not in nodes"
            )
    for statement in data.get("statement_sequence", []):
        for state_id in statement.get("input_states", []):
            if state_id not in state_ids:
                errors.append(
                    f"statement {statement.get('statement_id')!r} "
                    f"input state {state_id!r} not in nodes"
                )
        output_state = statement.get("output_state")
        if output_state and output_state not in state_ids:
            errors.append(
                f"statement {statement.get('statement_id')!r} "
                f"output state {output_state!r} not in nodes"
            )
    for statement_id in (data.get("statement_lineage") or {}):
        if statement_id not in statement_ids:
            errors.append(
                f"statement_lineage key {statement_id!r} not in statement_sequence"
            )
    for item in data.get("end_to_end_lineage", []):
        target_state = item.get("target_state")
        if target_state not in state_ids:
            errors.append(
                f"end_to_end_lineage target_state={target_state!r} not in nodes"
            )
        for source in item.get("value_sources", []):
            state_id = source.get("state_id")
            if (
                source.get("source_kind") == "prior_table_state"
                and state_id not in state_ids
            ):
                errors.append(
                    "end_to_end_lineage prior source state_id="
                    f"{state_id!r} not in nodes"
                )
    return errors
