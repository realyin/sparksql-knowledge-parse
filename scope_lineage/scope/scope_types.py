"""Data model for scope-based column lineage.

Each CTE, subquery, UNION branch, and top-level SELECT is a "scope" node in a DAG.
Column sources reference scope_id + column_name (immediate upstream scope)
instead of physical table names.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .fact_protocols import ExpressionResolution


CONSTANT_SCOPE_ID = "CONSTANT"
SYSTEM_SCOPE_ID = "SYSTEM"
# An unqualified column that several sources could equally supply. Distinct from UNKNOWN,
# which means "no source found": here the sources ARE known, SQL just does not say which one
# is meant. Picking one made the answer depend on join order rather than on the query
# (LINEAGE-002), so the ref names the ambiguity and carries every candidate.
AMBIGUOUS_SCOPE_ID = "AMBIGUOUS"

# Scope ids that are NOT an upstream scope and NOT a physical table. Anything resolving here
# has no physical source to report; treating one as a table name would invent a table called
# "AMBIGUOUS". Kept as one set so a new terminal cannot be added to some checks and missed by
# others.
NON_PHYSICAL_SOURCE_SCOPES = frozenset({"UNKNOWN", CONSTANT_SCOPE_ID, SYSTEM_SCOPE_ID, AMBIGUOUS_SCOPE_ID})


@dataclass
class SourceRef:
    """A reference from a column to an upstream scope + column."""
    scope: str      # scope_id of the upstream scope
    column: str     # column name in the upstream scope
    # Only set when scope == AMBIGUOUS_SCOPE_ID: every source that could have supplied this
    # column, as {"scope": ..., "column": ...}. Consumers must not collapse this to one.
    candidates: List[dict] = field(default_factory=list)
    # Structural SQL input identity. ``scope + column`` is insufficient when one physical
    # table occurs more than once in a statement. These optional coordinates are provenance
    # only and intentionally do not participate in legacy SourceRef equality.
    qualifier: Optional[str] = field(default=None, compare=False)
    binding_scope_id: Optional[str] = field(default=None, compare=False)
    input_ref_id: Optional[str] = field(default=None, compare=False)


@dataclass
class ScopeColumn:
    """A column within a scope."""
    name: str
    transform: str   # DIRECT|EXPRESSION|AGGREGATE|WINDOW|CONDITIONAL|CONSTANT|UNION|EXPAND_ALL
    transform_subkind: Optional[str] = None
    expression: Optional[str] = None
    sources: List[SourceRef] = field(default_factory=list)
    # Optional fields by transform type:
    case_branches: Optional[List[dict]] = None      # CONDITIONAL
    window: Optional[dict] = None                     # WINDOW
    agg_function: Optional[str] = None                # AGGREGATE
    branches: Optional[List[dict]] = None             # UNION
    merge_branch: Optional[str] = None                # MERGE: "matched"|"not_matched"
    # Spark has three WHEN clause kinds; contract 1.0's ``merge_branch`` enum has two
    # names. Rather than publish one of the two for a clause that is neither -- which
    # would state a rowset semantics Spark does not apply -- ``merge_branch`` is left
    # unset and the clause kind is carried here. Absent for the two branches the enum
    # does name, so no existing artifact changes shape.
    merge_branch_qualifier: Optional[str] = None      # MERGE: "not_matched_by_source"
    # Zero-based WHEN identity; one MERGE may write the same field in several clauses.
    merge_when_index: Optional[int] = None
    # True only when sqlglot synthesized the projection name and SQL supplied no alias.
    # Internal resolution fact: serializers keep the established column contract while
    # outputs/warnings use it to avoid asserting a generated name as a physical target field.
    name_is_generated: bool = False
    # Optional target-binding audit facts. They are populated only when the caller supplies
    # authoritative target DDL/Schema metadata; without that optional input the serialized contract
    # remains unchanged.
    parsed_name: Optional[str] = None
    target_column_ordinal: Optional[int] = None
    target_field_resolution: Optional[str] = None
    target_field_corrected: Optional[bool] = None
    target_metadata_table: Optional[str] = None


@dataclass
class ScopeJoin:
    """A JOIN relationship within a scope."""
    join_type: str
    left_scope: str
    right_scope: str
    alias_in_parent: Optional[str] = None
    condition_expression: Optional[str] = None
    condition_columns: List[SourceRef] = field(default_factory=list)


@dataclass
class ScopeFilter:
    """A WHERE or HAVING filter within a scope."""
    expression: str
    columns: List[SourceRef] = field(default_factory=list)


@dataclass
class ScopeInputEdge:
    """A direct input edge from a FROM/JOIN source into a scope."""
    source_id: str
    source_type: str  # physical_table|scope|unknown
    alias: Optional[str] = None
    position: str = "from"  # from|join|lateral_view
    join_type: Optional[str] = None
    join_condition: Optional[str] = None
    join_fields: List[SourceRef] = field(default_factory=list)


@dataclass
class ScopeLogicBlock:
    """A normalized processing block within a scope."""
    logic_block_id: str
    logic_type: str
    raw_expression: Optional[str] = None
    normalized_expression: Optional[str] = None
    fingerprint: Optional[str] = None
    subtype: Optional[str] = None
    fields: List[SourceRef] = field(default_factory=list)
    output_fields: List[str] = field(default_factory=list)
    join_type: Optional[str] = None
    input_sources: List[str] = field(default_factory=list)
    field_usage: List["ScopeFieldUsage"] = field(default_factory=list)
    expression_features: Dict[str, object] = field(default_factory=dict)
    final_target_columns: List[str] = field(default_factory=list)
    left_input: Optional[str] = None
    right_input: Optional[str] = None
    join_keys: List[SourceRef] = field(default_factory=list)
    join_relation_detail: Dict[str, object] = field(default_factory=dict)
    filter_predicate_detail: Dict[str, object] = field(default_factory=dict)
    window_specification: Dict[str, object] = field(default_factory=dict)
    aggregation_detail: Dict[str, object] = field(default_factory=dict)


@dataclass
class ScopeFieldUsage:
    """Fields used from one direct or upstream input source within a scope."""
    source_id: str
    source_type: str
    used_fields: List[str] = field(default_factory=list)
    used_field_details: List[dict] = field(default_factory=list)
    used_by_logic_blocks: List[str] = field(default_factory=list)
    used_by_output_fields: List[str] = field(default_factory=list)
    source_metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class ScopeOutputField:
    """An output field produced by a scope, with impact anchors."""
    name: str
    transform: str
    expression: Optional[str] = None
    expanded_expression: Optional[str] = None
    # "full" | "bounded". `expanded_expression` inlines each referenced upstream field's own
    # expanded text, so an expression referenced N times is copied N times and every extra
    # scope layer multiplies again — a moderately sized statement produced a single enormous string
    # (PERF-001). When a limit is reached the reference is LEFT IN PLACE instead of the text
    # being cut: the expression stays valid SQL, and the untouched `a.field` is itself the
    # pointer to the upstream output that holds the rest. "bounded" therefore means
    # "incomplete but composable", never "damaged".
    expansion_status: str = "full"
    expansion_stop_reason: Optional[str] = None  # "max_chars" | "max_substitutions"
    # Which references were left unexpanded, as {scope_id, field, ref} — follow them to the
    # named scope's output to continue the expansion by hand.
    unexpanded_refs: List[dict] = field(default_factory=list)
    expression_resolution: ExpressionResolution = field(default_factory=lambda: ExpressionResolution())
    expression_type: Optional[str] = None
    expression_features: Dict[str, object] = field(default_factory=dict)
    expression_role: Optional[str] = None
    grain_effect: Optional[str] = None
    consumer_readiness: Dict[str, object] = field(default_factory=dict)
    sources: List[SourceRef] = field(default_factory=list)
    source_logic_blocks: List[str] = field(default_factory=list)
    downstream_fields: List[SourceRef] = field(default_factory=list)
    target_columns: List[str] = field(default_factory=list)
    final_target_columns: List[str] = field(default_factory=list)
    output_ordinal: Optional[int] = None
    merge_branch: Optional[str] = None
    merge_branch_qualifier: Optional[str] = None
    merge_when_index: Optional[int] = None


@dataclass
class ScopeData:
    """All data for a single scope (CTE, subquery, UNION branch, or ROOT)."""
    kind: str          # cte|subquery|union|union_branch|root
    role: Optional[str] = None
    distinct: bool = False
    depends_on: List[str] = field(default_factory=list)
    writes_to: Optional[str] = None
    alias_in_parent: Optional[str] = None
    raw_sql: Optional[str] = None
    raw_sql_available: bool = False
    # Physical tables whose schema was used to expand this scope's SELECT * / a.*.
    # Internal marker (not serialized): lets the resolver materialize columns that
    # downstream scopes reference but the (possibly incomplete) schema omitted.
    star_schema_sources: List[str] = field(default_factory=list)
    # Scope ids this scope's star was expanded from (internal, not serialized).
    # Lets reference-driven materialization walk back through star chains
    # (subq -> subq -> physical) when an enumeration may be incomplete.
    star_expanded_from: List[str] = field(default_factory=list)
    raw_sql_quality: Dict[str, object] = field(default_factory=dict)
    source_coverage: Dict[str, object] = field(default_factory=dict)
    input_edges: List[ScopeInputEdge] = field(default_factory=list)
    input_source_refs: List[dict] = field(default_factory=list)
    alias_source_bindings: List[dict] = field(default_factory=list)
    expression_source_bindings: List[dict] = field(default_factory=list)
    union_branch_alignment: Dict[str, object] = field(default_factory=dict)
    logic_blocks: List[ScopeLogicBlock] = field(default_factory=list)
    outputs: List[ScopeOutputField] = field(default_factory=list)
    field_usage: List[ScopeFieldUsage] = field(default_factory=list)
    columns: List[ScopeColumn] = field(default_factory=list)
    joins: List[ScopeJoin] = field(default_factory=list)
    filters: List[ScopeFilter] = field(default_factory=list)
    group_by: List[SourceRef] = field(default_factory=list)
    having: List[ScopeFilter] = field(default_factory=list)
    order_by: List[dict] = field(default_factory=list)
    lateral_views: List[dict] = field(default_factory=list)
    # Union-specific:
    set_op: Optional[str] = None
    branches: Optional[List[str]] = None
    branch_index: Optional[int] = None


@dataclass
class ScopeGraphEdge:
    """An edge in the scope dependency graph (from upstream to downstream)."""
    from_: str  # "from" is Python keyword; serialized as "from" in JSON
    to: str

    def to_dict(self) -> dict:
        return {"from": self.from_, "to": self.to}


@dataclass
class ScopeGraph:
    """Scope dependency graph: nodes + directed edges."""
    nodes: List[str] = field(default_factory=list)
    edges: List[ScopeGraphEdge] = field(default_factory=list)


@dataclass
class DiagnosticWarning:
    """A diagnostic warning about a SQL pattern or potential issue."""
    type: str
    scope: str
    msg: str


@dataclass
class Diagnostics:
    """Parsing diagnostics: fallback status, warnings, and statistics."""
    fallback_used: bool = False
    warnings: List[DiagnosticWarning] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    lineage_fact_gaps: List[dict] = field(default_factory=list)


@dataclass
class ScopeLineageResult:
    """The complete scope-based lineage result."""
    task_id: str
    target_table: str
    # INSERT_OVERWRITE | INSERT | CTAS | MERGE. Standalone UPDATE / DELETE are NOT parsed:
    # this tool models column lineage of statements that WRITE A TABLE FROM A SELECT, and a
    # row-level write has no such projection. They used to appear here and in the JSON Schema
    # while the entry point rejected them, so a consumer could read the contract and expect
    # artifacts that never existed (CONTRACT-001). UPDATE/DELETE branches inside MERGE are
    # still handled — that is the merge statement's own semantics.
    stmt_kind: str
    # Canonical statement AST captured before target-DDL positional binding.
    # It is an in-memory identity input and is intentionally not serialized as
    # a second copy of the SQL.
    statement_identity_sql: str = ""
    # Scope ID of a MERGE's USING relation, "" for every other statement kind. Like
    # statement_identity_sql this is an in-memory fact, not a contract field: it lets
    # task-level modelling resolve a MERGE condition alias through the scopes that were
    # actually built, instead of re-deriving the alias's table from the raw AST and
    # publishing a CTE name as though it were a physical table (MERGE-CTE-002).
    merge_using_scope_id: str = ""
    # Alias the MERGE author gave its USING relation, "" for every other statement kind.
    # A MERGE's ROOT is a synthetic scope with no sqlglot scope of its own, so the pass that
    # walks sqlglot scopes to build input edges never reaches it; this carries the fact that
    # pass would otherwise have read (MERGE-INPUT-001).
    merge_using_alias: str = ""
    # True when this CTAS is a Spark ``CACHE [LAZY] TABLE ... AS SELECT``. The relation is
    # defined from a SELECT exactly as CTAS is — hence the shared stmt_kind — but it lives
    # for the session, so a consumer registering data assets must not record it as a table
    # the warehouse now has (CACHE-001).
    is_cached_relation: bool = False
    is_session_scoped_relation: bool = False
    merge_target_alias: str = ""
    # "ok" | "failed". A statement whose scope build raised is still returned (so the failure
    # stays diagnosable and one bad statement cannot abort a batch), but it carries EMPTY
    # scopes — structurally indistinguishable from a successful parse unless it says so. Callers
    # must be able to tell the difference without inspecting diagnostics text (PARSE-001).
    parse_status: str = "ok"
    # "strict_ok" | "recovered" | "failed". Separate from parse_status, which is about whether
    # the scope build succeeded. The parser runs sqlglot with a lenient error level, so invalid
    # SQL is silently repaired — a dropped token can be a WHERE, a JOIN or a field expression,
    # and the lineage then describes a query that cannot run. "recovered" means the SQL did not
    # parse strictly and what follows is derived from a repaired AST (PARSE-002).
    syntax_status: str = "strict_ok"
    # Where strict parsing broke, verbatim from sqlglot: line/col plus the surrounding token
    # window, so the repaired region can be inspected rather than guessed at.
    syntax_errors: List[dict] = field(default_factory=list)
    # Statements in the same script that this tool does not model, as
    # {statement_kind, reason, supported}. Empty for the common single-statement case. Present
    # so a skipped statement cannot be mistaken for one that was never there (CONTRACT-001).
    skipped_statements: List[dict] = field(default_factory=list)
    # Script-position identity, shared with the v2 task contract: statement_index is the
    # zero-based position among ALL statements of the script, statement_id its stable
    # `stmt:NNN` form. v1 numbers artifacts by write ordinal (`task#0`, `task#1`) while v2
    # numbers by script position, so `task#1` named different statements in the two
    # contracts and nothing serialized related them (JOINKEY-001). None when the caller
    # handed over a pre-parsed tree: the script position is unknown there, and a guessed
    # key would silently match the wrong statement — the exact failure this field removes.
    statement_id: Optional[str] = None
    statement_index: Optional[int] = None
    target_partition_spec: Dict[str, Optional[str]] = field(default_factory=dict)
    target_partition_columns: List[str] = field(default_factory=list)
    target_partition_mode: str = "none"  # none|static|dynamic|mixed
    target_field_binding: Dict[str, object] = field(default_factory=dict)
    task_dependencies: Dict[str, object] = field(default_factory=dict)
    source_tables: List[str] = field(default_factory=list)
    related_metadata: Dict[str, dict] = field(default_factory=dict)
    scope_graph: ScopeGraph = field(default_factory=ScopeGraph)
    scopes: Dict[str, ScopeData] = field(default_factory=dict)
    field_mapping_chains: List[dict] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    # Internal, never serialized: whether spark.sql.parser.quotedRegexColumnNames was on
    # for this statement. Folded from the script's SET statements in order, because the
    # write trees are collected into a flat list that carries no script position.
    regex_columns_enabled: bool = True
    # Why this statement has no target_field_binding, or None when it has one. Computed
    # once, at the only place that sees the statement kind, the target name, the supplied
    # metadata and the lookup result together; both contract writers read it rather than
    # each re-deriving it (the task document used to, and got all four cases wrong).
    target_binding_absence: "str | None" = None
