"""Typed protocols for the dict payloads the fact passes trade between each other.

These are the contracts that used to live only in the passes' heads: every pass reads
and writes ``ScopeOutputField.expression_resolution`` as a plain dict, and a mistyped
key was invisible until a golden diff caught the symptom. TypedDicts cost nothing at
runtime; they exist so pyright can see the protocol. ``total=False`` throughout --
every key is optional and producers emit only what they know (Python 3.9-compatible:
no NotRequired).

The corpus-coverage test (test_fact_protocols.py) fails when the pipeline starts
emitting a key that is not declared here, so the protocol cannot silently drift
behind the implementation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class PhysicalSourceField(TypedDict, total=False):
    """A resolved physical column: the leaf a consumer can point a scanner at."""

    table: str
    field: str
    transform: str


class GeneratedSource(TypedDict, total=False):
    """A value born in the query (constant, system function), not read from a table."""

    source_type: str      # e.g. "CONSTANT", "SYSTEM"
    value: str
    transform: str
    expression: str


class RowsetSource(TypedDict, total=False):
    """A row-set-level dependency (COUNT(1), bare OVER ()): no column to name."""

    source_type: str      # "rowset"
    scope: str
    field: str
    expression: str


class ScopeOutputTraceStep(TypedDict, total=False):
    """One hop of the provenance chain scope_projection -> ... -> physical_expression."""

    step: int
    from_scope_id: str
    from_field: str
    to_scope_id: Optional[str]
    to_field: Optional[str]
    relation: str          # "scope_projection" | "physical_expression" | ...
    expression_sql: Optional[str]
    resolution_status: str
    physical_source_fields: List[PhysicalSourceField]


class UnionBranchMapping(TypedDict, total=False):
    """How one UNION branch's output aligns with the union scope's output field."""

    branch_scope_id: str
    branch_index: Optional[int]
    output_field: str
    aligned_output_name: str
    expected_output_name: str
    expected_position: Optional[int]
    expression_sql: Optional[str]
    resolution_status: str
    physical_source_fields: List[PhysicalSourceField]
    generated_sources: List[GeneratedSource]
    rowset_sources: List[RowsetSource]
    missing_reasons: List[str]


class ExpressionResolution(TypedDict, total=False):
    """The resolution fact attached to a ScopeOutputField.

    Producers: the convergence passes (passthrough propagation, internal-scope
    resolution, output-sources expansion), the detail refreshes, star passthrough,
    and _normalize_expression_resolution, which canonicalizes the invariants
    (a "resolved" status requires a source fact; unresolved carries reasons).
    """

    status: str                       # "resolved" | "partially_resolved" | "unresolved"
    source_kind: str                  # "physical" | "generated" | "rowset" | "mixed" | "unresolved"
    expanded_expression: str
    physical_source_fields: List[PhysicalSourceField]
    generated_sources: List[GeneratedSource]
    rowset_sources: List[RowsetSource]
    missing_reasons: List[str]
    resolution_type: str              # how the winning resolution was derived
    source_scope_id: str              # internal upstream scope this output projects from
    source_scope_ids: List[str]
    source_output_field: str
    scope_output_trace: List[ScopeOutputTraceStep]
    union_branch_mappings: List[UnionBranchMapping]
    candidate_source_refs: List[dict]
    base_physical_source_fields: List[PhysicalSourceField]
    field_path: List[str]             # struct-member access path on the source column
    field_resolution_required: bool
    unresolved_qualifiers: List[str]
    udtf_output_binding: Dict[str, object]
    scope: str
    field: str
    expression: str
