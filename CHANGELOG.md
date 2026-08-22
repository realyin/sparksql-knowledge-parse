# Changelog

## Unreleased
- **Breaking**: the task contract (2.0) is now the only output mode, and the standalone
  contract-1.0 artifact is removed in the same release -- the planned deprecation window
  collapsed when the sole downstream consumer confirmed its own retirement.
  `write_lineage` is gone from the API; the CLI accepts `--contract-version 2.0` only
  (the flag stays one release so a `1.0` request fails with a clear choices error). The
  statement-document SHAPE is not retired: every `statement_lineage` entry keeps it,
  `lineage.schema.json` / `diagnostics.schema.json` remain as its schemas, the converters
  (`to_lineage_dict` / `to_lineage_json` / `to_dict` / `to_json`) stay public, and the
  golden statement corpus now validates every embedded entry against that schema.
- **Breaking**: removed four facade exports with no remaining consumer
  (`build_end_to_end_lineage`, `build_scope_profile`, `materialize_schema`,
  `table_details_for_table`) after the downstream consumer's retirement was confirmed.
  Their implementations stay internal to the packages that own them.
- `render` / `render_mapping_markdown` accept contract-2.0 task documents and render one
  mapping section per statement, in `statement_sequence` order; a single statement
  document still renders as before. Unknown schema versions are rejected.
- **Breaking**: removed the v1-era result types `Column`, `ColumnRef`, `JoinKey`,
  `LineageResult`, and `Unresolved` from the public API. Nothing inside the package, the test
  suite, or the approved consumer surface referenced them; they predate `ScopeLineageResult`
  and had no producer. Consumers of the current parser entry points are unaffected.
- Fixed the internal-resolution pass rewriting settled outputs: a completed expansion
  (resolved, physical fields present) is no longer mistaken for a damaged one, so the
  fine-grained `scope_output_trace` provenance chain can never be collapsed by an extra
  resolution round. Golden artifacts are byte-identical.
- Internal restructuring, no artifact changes: the `_shared.py` grab-bag split into ten
  themed modules; the fact pipeline expressed as four named phases with convergence loops
  and a wiring guard test; a package dependency-direction architecture test now runs in CI;
  the `expression_resolution` payload is typed (`fact_protocols.py`) with a non-blocking
  pyright job. Deep imports of `scope_lineage.scope._shared`, `scope.types`, or
  `scope.sqlglot_config` (now `scope_lineage.sqlglot_config`) no longer resolve -- the
  supported surface remains the package facade.
- The task contract no longer strips column types and comments while handing its schema to
  per-statement parsing. `SchemaMap` carries them on an attribute, and a `dict()` copy kept
  the keys while silently dropping it, so every nested statement in a v2 artifact reported
  null type/comment for columns the v1 artifact of the same statement documented fully.
- **Contract:** v1 documents now carry the script-position join key to v2: an optional
  top-level `statement_id` (`stmt:NNN`, matching v2's `statement_sequence[].statement_id`
  for the same statement) and `statement_index`. `task_id` cannot serve as the key -- v1
  suffixes it by write ordinal, v2 by script position, so the same `demo#1` names different
  statements in the two contracts and matches silently. Absent when parsing starts from a
  caller-supplied tree, where the script position is unknown.
- `parse_scope_lineage` (the single-statement entry) no longer applies its
  "first write only" boundary silently. Writes beyond the first are recorded in
  `skipped_statements` with `category: additional_write_statement` (plus their
  `target_table`), an `additional_write_statements_not_modeled` warning names the targets,
  and the script's non-write statements are recorded the same way the plural entry always
  did. Previously a two-write script came back as the first write's document with nothing
  recorded -- undeclared data loss on a public API symbol.
- **Behavior change (CI exit codes):** an unexpanded `SELECT *` on ROOT is now a
  root-impact `projection_wildcard_unexpanded` fact gap in the statement document -- the
  same gap type the task level always emitted for the same condition. Pipelines gating on
  `--fail-on-root-gap` or `--quality-policy strict` that previously passed such artifacts
  now fail them; that is what those flags are for. The permissive default still exits zero.
- Self-joins keep their two sides apart in `join_relation_detail`: `left_alias` and
  `right_alias` no longer collapse onto the later alias, and equality conjuncts whose two
  refs resolve to the same table become `join_key_pairs` oriented by qualifier instead of
  falling into `condition_filters` as an apparent tautology.
- `write_task_lineage` now validates every nested v1 document in `statement_lineage`
  against the v1 schema before publishing; the v2 schema types them as bare objects, so
  the envelope validation never looked inside.
- A `directory:` target in a v2 task raises a `directory_targets_present` task-level
  warning: the entry lands in `final_table_states` like a table, and v1 already documents
  the exclusion rule on `target_table` while v2 said nothing.
- A trailing comment (`INSERT ...; -- done`) is recorded as `stmt_kind: COMMENT` instead
  of `SEMICOLON`, in both contracts; the category stays `empty_statement`.
- Docs: designated `statement_id` as the only cross-contract statement key; marked
  `metadata_coverage`/`analysis_status` guidance as contract-2.0-only (v1 diagnostics
  never carried those keys); stated that top-level v2 `end_to_end_lineage` is a
  final-state merged view not equivalent to v1's per-statement arrays (the nested
  documents are the equivalent surface); documented the `directory:` phantom-table
  exclusion in v2; and stated the consequence of writing both contracts into one
  directory (same file names, silent overwrite).

## 0.1.16
- MERGE assignment values now resolve in the scope their WHEN branch can actually see. Spark
  picks the name-resolution scope from the clause -- a MATCHED action sees target and source
  both, `WHEN NOT MATCHED` only the source, `WHEN NOT MATCHED BY SOURCE` only the target --
  while Core resolved every branch against the USING relation and took the branch label from
  the THEN action's type rather than from the clause. With two branch kinds those dimensions
  coincide; Spark has three. A `WHEN NOT MATCHED BY SOURCE` update published an edge to the
  *source* table, marked `trace_complete`, for a branch in which Spark cannot see the source at
  all. The same missing candidate set produced two further wrong answers under plain MATCHED,
  with no BY SOURCE anywhere: an unqualified name both relations expose was silently
  attributed to the source, though this project already publishes a rule against picking an
  arbitrary source, and a name only the *target* exposes was attributed to the source as well,
  so a column the source does not have appeared as its output with no diagnostic -- and writing
  the same statement with a subquery `USING` gave a different answer again. Unqualified names
  now go through the resolver the rest of the product uses, so ambiguity lands in the existing
  `ambiguous_unqualified` / `AMBIGUOUS` + candidates representation, an unknowable side in
  `unresolved_unqualified_no_schema`, and a source-qualified reference under BY SOURCE in
  `dangling_column_ref_dropped`. No new vocabulary was introduced. WHEN conditions get the same
  branch discipline.
- **Contract:** Spark has three WHEN clause kinds and the `merge_branch` enum names two. Rather
  than publish one of the two for a clause that is neither -- `not_matched` means "absent from
  the target, inserted from the source", the opposite of what a BY SOURCE clause writes --
  `merge_branch` is now omitted there and a new optional `merge_branch_qualifier` carries the
  kind, on every surface that already published `merge_branch`. A consumer keying on
  `merge_branch` drops such a write rather than misplacing it: missing beats wrong, but it is a
  silent omission, so a `merge_branch_not_representable` warning states why the label is
  absent, and `merge_when_index` is still emitted. Two surfaces published `merge_branch`
  without declaring it in the schema; both are declared now.
- `SELECT * EXCEPT (...)` no longer publishes the columns it excludes. The excluded column used
  to appear as a proven output field on every surface -- including `related_metadata`'s entry
  for the *target* table, where it invented a column the target does not have -- with no
  diagnostic. The exclusion is applied in one pass over the resolved scopes rather than at each
  expansion site, because a star is materialized in more than one place and which one runs
  depends on how the query was written rather than on what it means. Relatedly, a passthrough
  SELECT over a UNION is no longer unwrapped when its star carries an EXCEPT: such a star is
  not a passthrough, it drops columns, and unwrapping lost the exclusion the same way the
  surrounding code already warns other clauses are lost. Because this changes the projection
  count, positional target-DDL binding moves in both directions, and both are corrections:
  where the counts now match, binding applies where it previously bailed out; where they no
  longer match, the statement falls back instead of authoritatively binding a column the
  projection never produced. Spark's grammar allows exactly one star modifier; `REPLACE`,
  `RENAME` and `ILIKE` belong to other engines and are reported through
  `star_modifier_not_supported` rather than modelled, and `star_except_column_not_found` marks
  an exclusion naming a column the star does not produce.
- A deployment can declare the partition overwrite mode its clusters run with, via
  `--partition-overwrite-mode static|dynamic` (contract 2.0 only). `INSERT OVERWRITE TABLE t
  PARTITION(dt)` -- a partition spec with no value -- deletes either the whole table or only
  the partitions the write produces, and `spark.sql.sources.partitionOverwriteMode` decides
  which; the SQL cannot see it. Scripts rarely `SET` it, so v2 fell back to Spark's documented
  default of `static`. For a deployment whose clusters run `dynamic` that answer is not
  conservative but backwards: every daily overwrite of a partitioned table was reported as
  wiping the table's history, dropping the "came from this table's own prior state" edges such
  an overwrite in fact preserves. A `SET` inside the script still wins. **Contract:**
  `partition_overwrite_mode_source: "assumed_default"` used to imply "static was used"; it now
  means only "the script did not set it", and a new `partition_overwrite_mode_declared` carries
  the value actually applied, its *absence* meaning Spark's default. Consumers that only filter
  on the enum are unaffected. Separately, the rolling `SET` tracker no longer treats an
  unrecognised value as `static`, which had quietly converted the neighbouring Hive key's
  `nonstrict` into a real answer.
- The empty statement a `;;` leaves behind is recorded. sqlglot models the two empty shapes
  differently -- a bare `;` after a comment parses to a semicolon node, `;;` yields nothing --
  and v1 recorded the first while dropping the second. Statement indices count every position,
  so dropping the record left holes that no published field explained, in exactly the field the
  documentation points readers to for "what was ignored". The two shapes keep separate kinds,
  matching what the task document has published for both all along. Note that
  `skipped_statements` is a conditional key: a script with no skipped statements gains the key,
  while one that already had an entry gains an array element, so a consumer that counts entries
  sees a different number with no schema change to signal it.
- A MERGE `INSERT *` branch expands over the target's columns, not the source's, matching how
  Spark resolves a star action.
- A statement with no target binding says why, once, in both documents, so an absent binding is
  distinguishable from a binding that was never attempted.
- The session setting for quoted regex column selections is honoured: with it disabled, a
  backtick-quoted pattern is the literal column name it is in Spark, not an expansion over the
  columns it would have matched. The overwrite documentation was corrected in the same change.

- New `scope-lineage render` subcommand and `render_mapping_markdown` public API: render one
  statement's `lineage.json` (+ sibling `diagnostics.json`) into a `mapping.md` field-mapping
  document readable by people and parseable by machines. The document is a derived view of the
  contract, never a second source of truth: a flat front matter block, fixed line grammars
  (versioned as `mapping-md/1`) whose expressions always sit last on the line inside code
  spans -- so SQL literals containing separators cannot break parsing -- and contract ids
  (`mapping_chain_id`, `logic_block_id`, scope ids) as join keys back into `lineage.json`.
  Uncertainty stays explicit: incomplete traces, un-split self-join conditions, and a missing
  diagnostics file are all marked instead of being rendered as facts. Directory mode skips
  contract-2.0 documents with a count instead of failing. The `parse` subcommand still writes
  exactly the two contract files. Four golden cases were added alongside the existing baseline
  (directory target, self join, a non-empty lineage fact gap, separator-heavy literals); the
  rendered `mapping.md` for every case is byte-locked the same way the JSON contracts are.
  Parse-process warnings do not enter `mapping.md`: a statement that has any renders a sibling
  `warnings.md` (grouped by type, each type carrying a one-line gloss), and the mapping
  document keeps a counted pointer plus only the facts that change how much a reader may trust
  the lineage. The relations overview answers "how do the source TABLES relate": join keys
  are pierced to physical fields and aggregated per table pair with short key names and an
  occurrence count, identical patterns repeated across scopes merge into one counted row, and
  CTE-to-CTE joins whose pierced keys add nothing (both sides reading the same table used to
  render as repeated self-pair rows with `t.c = t.c` keys) stay out of the overview entirely
  -- the scope-level detail below remains the complete list, where pierced pairs appear only
  when informative and the verbatim ON survives only where the key/filter split is
  incomplete. The constant-source column of the mapping table appears only when some field is
  actually constant-fed. The overview consumes `target_binding_absent_reason`: a statement
  without a binding states the actual reason in Chinese, and only `target_table_not_found`
  -- the one absence that risks positionally misplaced columns -- carries the warning
  marker. The four golden cases added with the renderer are regenerated against the current
  contract output.

## 0.1.15
- A UNION branch projecting a row-count aggregate or a bare window function no longer reports a
  root-impact lineage gap for an expression nothing was missing from. The branch mappings are
  built one pass before expression resolutions are normalized, and it is normalization that
  synthesizes `rowset_sources` for a resolution already classified `rowset`; the mapping copied
  three empty source lists and the gap detector re-derived `unresolved` from them. `COUNT(1)`
  and a bare `OVER ()` in a union branch therefore failed the strict quality gate, which was the
  largest single source of root-impact gaps a run could report. A windowed function that
  references a column was never affected -- it picks that column up as a physical source. No
  contract change: `rowset_sources` is an existing field.
- A generator over a literal -- `LATERAL VIEW EXPLODE(ARRAY(...))`,
  `INLINE(ARRAY(STRUCT(...)))` -- now records the constant it reads. Its argument has no column
  references, so the output columns were minted with an empty source list, making the column a
  dead end that reported itself as fully traced: `end_to_end_lineage` rendered
  `source_kind: "unresolved"` while `trace_complete` stayed true, the one pair a consumer must
  never have to tell apart, while `field_mapping_chains` already answered `generated` for the
  same field. The VALUES / table-valued-function path already routed a source-free leaf
  correctly; this is its missing twin. Fixing it in the resolver rather than while rendering the
  trace also clears the dead end out of the scope document, which the chains and the MERGE
  condition path read directly. A column that branches on the generator's value now reports
  `mixed` rather than `physical` -- it genuinely depends on the literal array, so the previous
  answer was an omission.
- A quoted regex column selection that was expanded no longer keeps a warning saying the column
  does not exist. Column-reference resolution runs first and cannot know the name is a pattern,
  so it warns -- `column_not_found` bare, `column_not_in_table_schema` qualified -- and the
  expansion pass then replaces the pattern with the columns it matched. The retraction is gated
  on the match having happened, not on the name looking like a regex: that predicate is only a
  metacharacter test, so it is equally true of a genuinely missing column called `amount$usd`,
  of a pattern matching nothing, and of a pattern in a WHERE clause that is never expanded.
  Suppressing on it would trade a false alarm for a false silence.
- A `CREATE TABLE ... <query>` that omits the optional `AS` is now parsed when its query begins
  with `WITH`. Spark allows the omission and the parser accepts it before `SELECT` but not
  before a CTE, where the statement degraded to an opaque command and contributed no lineage at
  all. The repair works on the token stream, per statement: a text-level match cannot see
  comments, and a commented-out `create table` line above a live `WITH ... AS (` is a common
  shape whose rewrite would swallow the *following* statement's CTE while still parsing. Each
  statement is judged alone -- it must currently be a command and must become a create carrying
  a query -- and the rewrite is disclosed as `ctas_as_inserted_for_parse` rather than left
  silent. `syntax_status` is deliberately unchanged: it is script-scoped, and downgrading it
  would degrade every other statement in the same script.
- A statement the tool ignores by design is no longer called unsupported. The category function
  already separated a config statement and an empty one from the kinds genuinely not modelled,
  and the task document acted on that split; the statement document imported the same function
  and never used it, so config and empty statements were the largest source of warnings in a run
  while the name asserted something false about both. Dropping the warning alone would have
  traded a misleading signal for no signal -- this document's skip record carried no SQL, and
  `skipped_statements` is written to `lineage.json` only -- so the record now carries
  `normalized_sql`, matching the task document's. Warnings for row mutations and genuinely
  unmodelled kinds are unchanged, and the records themselves are untouched.
- Documented what `target_table` holds when the write goes to a path. `INSERT OVERWRITE
  DIRECTORY` is modelled like any other write but its destination is a filesystem path, reported
  as `directory:<path>`; the contract doc described the field as a table name only, so a consumer
  registering warehouse tables from it had nothing telling it to skip these. No behaviour change
  -- the regression tests the shape never had are added, including that it emits no diagnostic of
  its own, since the result is correct and the target is self-describing.

## 0.1.14
- Narrowed the `source_state_columns_unknown` gap to the one shape it exists for. It was keyed
  on `state.columns_known`, which is false for *any* missing reason, so relations whose columns
  were named in the producing projection and listed column by column in the document were
  reported as undescribed. It now asks whether the relation's own projection stayed a wildcard
  -- the case where its single row is keyed on `*` and no named column can ever be found in it,
  which is what leaves a consumer with nothing to fold. `COUNT(*)` is excluded: its star is the
  row, not an unknown column list.
- Added `fold_session_scoped(document)` to the public API: one implementation of resolving hops
  through relations that do not outlive the session, so consumers do not each write their own.
  It returns a copy, drops the rows and `final_table_states` entries for those relations, and
  where a hop cannot be resolved it keeps the original source and says why via
  `value_sources_folded` / `fold_incomplete_reasons` rather than returning a shorter answer. The
  four unresolvable cases are all real: a read of a state that was later replaced, a relation
  whose own columns were never resolved, a column with no sources, and a cycle. Constants
  survive the fold -- they name no relation to resolve.
- `value_sources[]` entries that read a session-scoped relation now carry `session_scoped: true`.
  The same fact was already on the producing statement, but the edges a consumer acts on are
  here, so acting on it meant collecting relation names from `statement_sequence` and
  intersecting them against every source. This is a new optional key beside `source_kind`, not a
  value of it: a filter that does not know the key keeps exactly the behaviour it had. Because
  Core marks the relation it resolved, the edge is marked even where a consumer matching by name
  could not -- a global temporary view is declared bare and read qualified.
- A `CREATE GLOBAL TEMPORARY VIEW` is recorded as `global_temp.<name>`, the name it can be read
  by. Spark puts these views in the `global_temp` database and the declared bare name does not
  resolve, so recording the bare name meant the statement reading it matched nothing: the read
  looked like an ordinary physical table, a consumer excluding session-scoped relations kept it,
  and metadata was reported missing for a table that does not exist. This is the identity half
  of the judgement whose persistence half was fixed alongside temporary tables.
- Incompleteness now crosses a script-local hop. A column read out of a relation whose own
  columns were never resolved -- a temporary relation built from an unexpanded `SELECT *`, which
  has a single row keyed on `*` -- reported `trace_complete: true`, resting on a relation nobody
  could describe. Such columns now report `false` with a `source_state_columns_unknown` reason
  and a matching `lineage_fact_gaps` entry. Incompleteness already propagated from the previous
  state of the table being written; this is the same question asked of the relations being read.
  **This moves rows out of "complete"**: consumers gating on `trace_complete` will see fewer
  complete rows, and the ones they lose were making a claim the document could not support. No
  row moves the other way.
- `value_sources[]` entries now carry `source_state` when the source table was written by a
  statement in the same script, naming which state of it the read saw. A table can hold more
  than one state in a script, so a source that named only the table left two reads of a
  redefined relation indistinguishable, and a consumer resolving that hop by name folded both
  to whichever definition was recorded last. `end_to_end_lineage` is a final-state view, so an
  intermediate state has no row and cannot have one without changing what the field means --
  naming the state is what makes that detectable instead of wrong: the consumer looks for the
  state, finds no row, and keeps the original edge. Absent for a table the script never wrote,
  where there is no second candidate.
- A relation re-created during a script now gets a new `state_id` instead of reusing the first
  one. A CTAS is deliberately given no previous state -- it replaces the relation, so its value
  sources must carry no prior-state passthrough -- but the state's ordinal was derived from that
  same "previous", so every CTAS was numbered 1. A script that redefined a temporary view
  produced two `table_state_graph` nodes both called `state:v:001` with different producing
  statements, and every `edges` / `final_table_states` / `input_states` reference to that id
  became ambiguous rather than invalid, which is why no check caught it. The ordinal now counts
  the states a table has had; inheritance is unchanged. Validation now rejects a duplicate node
  id outright.
- `CREATE TEMPORARY TABLE ... AS SELECT` is now marked `is_session_scoped_relation` like the
  other session-scoped forms. sqlglot reports it with the same `TemporaryProperty` but
  `kind=TABLE`, and the predicate required `kind=VIEW`, so it was silently missed -- the
  "which keyword produced it" mistake the predicate's own comment warns against. The
  judgement is now the property alone.

## 0.1.13
- Added a `session_scoped_relations_present` warning naming every relation in a script that
  only lives for the session. `is_session_scoped_relation` alone was not enough in the task
  document: the flag sits on `statement_sequence[]` while the entry that misleads is in
  `final_table_states`, and `analysis_status` stays `complete`, so a consumer who does not
  know to cross-reference the two reads a confident artifact naming tables that were never
  written to storage.

## 0.1.12
- Kept a statement's lineage when one of its columns is named after a SQL keyword. Spark
  accepts `not`, `like`, `out` and `using` as column names when quoted, and authors routinely
  leave them unquoted; the parser stopped at the first one and the whole projection list was
  discarded, costing a statement the sources for nearly every column it writes. The
  repair carries no reserved-word list -- the parser names the token it stopped on, that token
  is quoted, and the statement is parsed again, with the rewrite kept only if it makes the
  statement parse. Rewrites are reported as an `identifiers_quoted_for_parse` warning rather
  than applied silently. Clause keywords are never quoted: one real malformed statement parses
  once its `WHERE` is quoted, yielding an AST in which WHERE is a column name, and staying
  `recovered` is the honest answer for SQL that is simply broken. Verified against a corpus of
  production statements: fewer statements degrade to `recovered`, and none lost a traced column.
- Added `is_session_scoped_relation`, marking relations that never reach storage -- `TEMP VIEW`,
  `GLOBAL TEMP VIEW` and `CACHE [LAZY] TABLE`. `CREATE TABLE db.r AS SELECT` and
  `CREATE OR REPLACE TEMP VIEW r AS SELECT` previously produced byte-identical lineage: the AST
  holds the distinction and Core dropped it, so `final_table_states` gained an entry for every
  temp view and consumers reconciling it against the catalogue reported tables that do not
  exist. One predicate covers every spelling, decided on AST facts rather
  than naming patterns; a non-temporary `CREATE VIEW` is registered in the catalogue and
  outlives the session, so it is not marked. Purely additive: `source_kind` and `source_type`
  keep their value distributions, and `is_cached_relation` keeps its meaning as the
  CACHE-shaped subset of this field.
- Stopped a statement sqlglot can parse but not print from taking the caller down with it. An
  identifier its tokenizer claims as a keyword — `CAST(out AS DOUBLE)`, where `out` is a real
  column name — parses into a Cast whose target type is None, and the Spark generator
  dereferences it. `parse_scope_lineage` had no error boundary, so the AttributeError escaped
  the public API; the batch entry point has had one since 0.1.0, which is why only the
  single-statement path was affected. Guarding the boundary rather than each of the 55 render
  sites: rendering is not the only thing that can fail on a repaired tree — `output_name`
  derives its answer by rendering too — and a statement that cannot be printed still has usable
  lineage. `ValueError` and `NoSupportedWriteStatementError` still reach the caller unchanged:
  this package raises those deliberately to mean "refuse to emit lineage rather than emit
  something wrong". The degradation itself is unchanged — that statement is still `recovered` —
  and a production corpus are byte-identical

## 0.1.11 - 2026-08-20

- Named the columns a window grouped or ordered by, in a new optional
  `window_context_sources` beside the existing sources. `transform` cannot carry this: it
  records the strongest expression kind on a source's path, and `_trace_column` passes that down
  every branch, so a partition key and the value the window computes arrive labelled `WINDOW`
  alike. Nothing was wrong with the lineage — `value_sources` was complete — but a window
  partitioned by fifteen columns has now twice been filed as a P0 "the lineage was smeared across
  the whole table", both times against right answers. The keys sit in their own array the way
  `row_membership_sources` and `value_condition_sources` have since 0.1.0, and `value_sources` is
  unchanged to the edge: it stays the complete dependency set change-impact analysis needs. A
  column that both orders a window and feeds the computed value appears in both, which is why
  subtracting one from the other is not the recipe for "what computes this" — on the real
  slowly-changing-dimension column that prompted this, subtraction answers "nothing". Optional,
  omitted when empty, declared in both documents' schemas; across a production corpus the
  `value_sources` edges are unchanged and the context entries are additive

- Stopped reporting a `duplicate_table_in_union` for a table a branch only reads inside a filter
  subquery. The warning exists to catch a copy-pasted UNION branch whose source was never changed,
  and it read that off `depends_on` -- everything the scope reaches. Once a filter subquery's
  physical tables were restored to `depends_on`, the anti-join shape (`SELECT ... FROM a` UNION
  `SELECT ... FROM b WHERE NOT EXISTS (SELECT 1 FROM a ...)`) started warning on every occurrence,
  which is deliberate SQL and extremely common: on a production corpus it produced new warnings and
  demoted a statement that had nothing wrong with it. `ScopeInputEdge` already carries the fact the
  detector wants -- "a direct input edge from a FROM/JOIN source into a scope" -- so it now reads
  `input_edges`, counting each branch once because one branch can hold several edges to the same
  table. A table the branch pulls in by JOIN still counts; a branch whose FROM is a derived table
  over the shared table is still missed, as it was before, and widening that reach is a separate
  change (DUP-UNION-001)

- Limited an unreadable metadata file to that file, on the two paths where 0.1.6's rule had never
  actually taken effect. Source schema had the per-file guard but caught only `MetadataFileError`,
  while the JSON reader let a raw `JSONDecodeError` out — so the commonest kind of bad file walked
  straight past it. Target DDL metadata raised on the first unreadable file and abandoned the rest
  of the directory, the rule having never been applied there at all. Worse, a file-level rejection
  is recorded with no table name, and the serializer kept only conflicts whose table was among the
  referenced ones — so every one of them was recorded and then dropped, leaving an artifact that
  said nothing at all about the file it could not read. A couple of corrupted files took the
  loader from **no usable tables at all to all but those two**, with each file and its reason now in
  `metadata_conflicts`. A load that produced no table still raises, and still names every file it
  refused

- Stopped deciding which warehouse layers require a cross-task trace. Core stamped
  `expression_resolution.cross_task_trace_required` from a vocabulary written into it -- `app`,
  `app_*`, `dm*`, `ads*`, matched against the database segment alone. Warehouse layer naming is a
  deployment convention, which this project's own conventions place downstream, and a deployment
  naming its upper layers anything else got the flag on nothing at all with no way to find out.
  Core still publishes what the judgement rests on: `physical_source_fields`, the physical columns
  an expression resolved to. **The field was never declared in the JSON Schema and appears in no
  document, but it did reach the artifact and it did have a consumer** -- so its removal is a
  behaviour change even though it breaks no contract. On a production sample it appeared often
  and now appears none; every other signal is unchanged. A consumer that wants it back computes it
  from `physical_source_fields` with its own layer policy

- Dropped seven re-exports from `scope_builder` that existed only so a consuming repository could
  import Core internals through it. They were never in `PUBLIC_CORE_API`, so this changes no
  contract -- but anyone who had reached for `scope_lineage.scope.scope_builder._populate_lineage_fact_gaps`
  and friends will now get an ImportError instead of a symbol Core was free to move anyway. The
  functions themselves are unchanged, in the modules that define them. The consumer that needed
  them stopped: the tests that were reaching through now live here, where the behaviour does

## 0.1.10 - 2026-08-19

- Published `Diagnostics` and `DiagnosticWarning` on the public facade. A consumer already
  receives both through `ScopeLineageResult.diagnostics`, which is itself published, and their
  siblings `ScopeColumn`, `ScopeData`, `ScopeOutputField` and `SourceRef` were public — these two
  alone were not, so anyone naming the type they had just been handed had to import it from
  `scope_lineage.scope.scope_types`, a path Core is free to move. Reaching a type through a
  private module in order to describe a published one is a hole in the facade, not a use of it

- Documented that `value_sources[]` lists participation paths rather than a set of columns. The
  dedup key is `(table, column, transform)` and includes the transform deliberately, so one
  physical column appears once per way it participates — a derived column on a real slowly
  changing dimension carries duplicate entries that dedupe to the same columns its sibling
  carries as 17. Read as a column set, that looks like the lineage was smeared across the whole
  table, and it has been reported as pollution twice. The document now gives the dedupe recipe
  and warns off the filter that suggests itself — keeping only `DIRECT`/`EXPRESSION`/
  `CONDITIONAL` empties the lineage of every aggregate and window metric, because their value
  arguments carry `AGGREGATE` and `WINDOW` too

## 0.1.9 - 2026-08-19

- Stopped reporting a table qualified by its own name as an unexpanded alias. `qualify` names
  an unaliased table after itself, so `FROM ods.pay` yields references written `pay.uid` while
  the physical id stays `ods.pay`; the exemption for "the alias *is* the physical source"
  compared the two directly and never matched. A fully resolved direct physical source was
  therefore reported as `expanded_expression_contains_unexpanded_alias`, demoting the output to
  partially_resolved and the statement to `partial`. It needed the same table read both in the
  enclosing `FROM` and inside a projection subquery to surface, which is why it hid: the `FROM`
  registers the binding and the subquery puts that same qualifier into the expression text,
  which the textual check cannot tell apart. A genuine local alias is still reported — `s` in
  `FROM ods.source s` is neither the id nor its table name. An affected statement goes from several gaps and
  `partial` to none and `complete`, with its physical sources unchanged

- Recovered the physical sources of a scalar subquery used as a projection. Column references
  inside a nested query are skipped when the enclosing expression is resolved, and rightly so:
  they belong to the subquery's sources, and resolving them outward binds them to whatever the
  outer scope exposes under the same alias. But nothing picked them up afterwards — a scalar
  subquery is not a FROM-clause source, so it never became an input of the outer scope, and the
  projection fell through to the constant fallback with the whole `(SELECT …)` recorded as a
  CONSTANT value and its tables nowhere in the lineage. In the plain shape this was silent: no
  gap, `analysis_status` complete. They now resolve against the subquery's own scope, which
  sqlglot already builds, and a correlated reference still binds outward because alias lookup
  walks parent scopes. Across the statements that use the shape, physical source edges come
  back and 5 subqueries stop being reported as constants

- Stopped a dynamic-partition `INSERT OVERWRITE` from claiming the target's previous values
  survived it. The write effect was chosen from `target_partition_mode != "none"`, so
  `PARTITION(dt='20260101')` and `PARTITION(dt)` were treated alike and both carried the
  target's previous `value_sources` forward. Only the first deserves that: a valued spec
  replaces the partitions it names and the rest of the table stands, while a bare
  `PARTITION(dt)` depends on `spark.sql.sources.partitionOverwriteMode`, whose default is
  STATIC — every existing partition is dropped before the new data lands. Every column of such
  a target therefore came back with a `prior_table_state` edge from a state the overwrite had
  destroyed, which is what a consumer folding state-evolution edges reads as "this column was
  left alone". The setting is now read from the script when present and applies to the
  statements after it. A dynamic-partition overwrite now agrees with the unpartitioned one it
  has always resembled: a column the write does not supply gets no row rather than a false
  one. Affected statements lose those edges; gap counts, statuses and syntax results are
  unchanged

- Documented that a window field's sources carry three different roles under one
  `transform: "WINDOW"`: the aggregate's value argument, the `PARTITION BY` keys and the
  `ORDER BY` keys. A window partitioned by many columns therefore lists all of them as
  sources, which reads as "the whole table was smeared onto one field" if the roles are not
  separated — a reading that has already produced a false pollution report. The roles are on
  the column that *defines* the window (`columns[].window.partition_by` / `order_by`), not on
  the downstream field, and `end_to_end_lineage` flattens the chain without a back-pointer,
  so both documents now say where to look and how to tell a value source from grouping
  context. No behaviour change

## 0.1.8 - 2026-08-19

- Normalized schema column names the way table names already were. sqlglot's `qualify`
  lower-cases unquoted identifiers, so every column reference the resolver sees is
  lower-case; `normalize_table_name` lower-cases for exactly that reason and says so in its
  docstring, but column names were passed through verbatim. A metadata export that spells
  its columns in upper case therefore matched nothing. Nothing failed loudly: `SELECT *`
  expansion copies schema names into a scope's column list, so an inner scope advertised
  `V1` while the outer scope asked for `v1`, source chains broke to `scope:"UNKNOWN"`, and
  explicitly referenced columns were re-added as case-variant duplicates — while
  `metadata_coverage` still reported every table covered, because coverage only checks table
  names. A multi-branch MERGE went from thousands of lineage fact gaps and `partial` to none
  and `complete`; the same schema differing only in case is now the same lineage

- Stopped a MERGE's USING alias from being captured by an inner table of the same name.
  `USING (SELECT record_id AS biz_no, 'prod' AS etl_source FROM ods.src t1) t1` resolved
  every `t1.<col>` against the subquery's *internal* sources, where the inner table won — so
  a renamed projection was published as `ods.src.biz_no`, a column that table does not have,
  and the literal became `ods.src.etl_source`, a physical field. With `trace_complete` true
  and no warning: a confident wrong answer, and precisely what this project's README
  criticises other tools for. A column the subquery passes straight through still binds
  directly to the table, which is the lexical source an earlier fix preserves; only a
  derived column is redirected. The fabricated columns in an affected statement go to none

- Gave `syntax_errors[]` an order that holds across processes. sqlglot builds one message
  per entry of `Expression.required_args`, which is a `set`, and CPython randomises string
  hashing per process — so a statement missing two required keywords wrote the same entries
  in an order that changed between runs. `syntax_errors` is a required field of
  `lineage.json`, and this project treats byte-for-byte determinism as a contract invariant,
  so anyone diffing artifacts across runs saw a phantom change. Sorted by position first, so
  errors genuinely ordered by where they occur keep that order and the description only
  breaks ties

## 0.1.7 - 2026-08-19

- Said where the reader looks when a source table's columns were never supplied. The fact
  was already in `metadata_coverage`, but `analysis_status` said `partial` for
  `lineage_fact_gap` and the document carried thousands of records — every one of those
  words meaning "the parser could not handle this SQL". `blocking_reasons` now names
  `metadata_incomplete` ahead of `lineage_fact_gap`, and a warning lists the source tables
  that were missing. Sources only: a target without a schema entry is an ordinary shape and
  is never why a source-side reference failed

- Resolved `col.field` on a struct column written without a table alias. `alias.col.field`
  carries three parts and was handled; the two-part form had its first part looked up as a
  table alias, found nothing, and reported the column as an unbound alias. Whether the alias
  is there is not the author's choice alone — qualify adds it when it knows the column set
  and cannot when the input is a `SELECT *` — so the same SQL resolved or did not depending
  on how deep it sat. A name more than one input exposes stays unresolved

- Modelled a PIVOT's output columns. `PIVOT (max(amt) FOR k IN ('A', 'B'))` turns the values
  of `k` into columns named A and B whose values come from the aggregate, and neither the
  names nor that lineage existed: a `SELECT *` over a pivoted relation saw the pivoted
  subquery's own columns instead, so every downstream reference to a pivoted name was a gap —
  many in an affected statement. The pivot's alias now becomes an input edge when it has one, and a
  star over a pivoted source expands to the IN list. A non-literal IN list still reports a
  gap rather than guessing names

- Stopped qualifying every statement twice. `qualify` mutates the tree it is given and
  returns that same object, so the `qualified is src_expr` comparison that guarded the "did
  qualify fail?" branch was true either way, and the branch re-ran qualify on every
  statement to learn what the first call already knew. `_qualify_ast` now reports success
  directly. Cost only — no output changes, both baselines untouched

- Stopped reading an unexpanded `a.*` as a regex pattern. A qualified star cannot always be
  expanded when its projection is first read — a CTE backed by a UNION only gets its columns
  in a later pass — so it is parked as a placeholder for the fixpoint expansion to finish.
  Spark's regex column selection, added in 0.1.6, then matched that placeholder as a pattern,
  and `a.*` is a valid one: a 63-column star collapsed into the single column whose name
  began with "a", and the placeholder was gone before the pass that would have expanded it
  properly ever ran. The affected statements go from many gaps to none

- Let a bare column bind through a regex column selection. Spark's `` `(rk)?+.+` `` names
  the columns a source exposes by pattern, and the match runs after column resolution — but
  a scope projecting one was read as already materialized, with a single concrete column
  literally called `(rk)?+.+`. Every other name was then judged absent from it, so a bare
  reference with two inputs lost the only input that could supply it. A pattern means "not
  yet knowable", which the resolver already models and already keeps in play. Of the 36 real
  tasks a regex projection can reach, 1 improves and 35 are unchanged

- Marked the fact gaps that a repaired parse produces, with a new optional
  `derived_from_recovered_syntax` on each. When sqlglot cannot place a token it drops the
  rest, and a statement that said `FROM` becomes one with no source at all — so the gaps
  that follow describe the truncation, not the query. They sat in the same list as gaps
  about genuinely missing metadata, and counting the two together turned one syntax problem
  into hundreds of apparent capability gaps in a single statement. `syntax_status` already said the
  parse was repaired; the marker means a consumer no longer has to correlate two documents
  to know which gaps to exclude. Statement lineage needed its own answer, since a truncation
  is invisible once the tree is rendered back out

- Backquoted reserved-word column names in a table's DDL before parsing it. sqlglot's Spark
  dialect does not terminate on `CREATE TABLE db.t (a DOUBLE, not DOUBLE)` — the same 51
  characters hang 30.0.0, 30.16.0 and 30.17.0 alike — so a table whose export happened to
  name a column `not` did not make a task's answer worse, it made the task never finish, and
  no caller could put a timeout around it. Three more tables were being rejected outright by
  the milder version of the same problem, losing their columns wholesale. Quoting is an
  equivalent rewrite, and nearly every DDL it touches yields facts identical to before

## 0.1.6 - 2026-08-18

- Roughly halved lineage resolution time on wide statements by remembering answers that
  depend only on their inputs: compiled patterns built from identifier names, the field
  references of an expression, and whether an expression reaches into a struct. A large
  task that previously exceeded two minutes and returned `partial` now completes in 67
  seconds with no gaps
- Expanded Spark's quoted regex column selection. `` `(dt)?+.+` `` selects every column
  whose name matches the pattern — its possessive quantifier making it the idiom for "every
  column except dt" — and reading it as a literal name produced a column no table has, which
  took every downstream reference to that scope down with it
- Resolved a reference to a LATERAL VIEW's output column when the qualifier is the column
  rather than the view's alias, so `arr.field` binds to the view that exposes `arr`. Two
  views exposing the same name stay a gap rather than being resolved by writing order
- Modelled Spark's `CACHE [LAZY] TABLE ... AS SELECT` as the relation-from-a-SELECT it is.
  It was skipped as an unsupported statement, so the relation it builds was read back as an
  external table nobody has metadata for and every reference to it became a gap — hundreds in a
  a single statement. It reports `stmt_kind: "CTAS"` with a new optional `is_cached_relation`
  flag, since the relation lives only for the session
- Made a table's DDL authoritative over its exported column array rather than validating one
  against the other. A partition column declared only in `PARTITIONED BY` is an ordinary
  export shape, not a contradiction, and rejecting it discarded usable metadata
- Limited an unusable metadata file to the table it describes. The loader raised, so two
  a couple of malformed files left every table without columns; rejected tables are now
  reported through `metadata_conflicts` and only a load that produced no table at all raises

## 0.1.4 - 2026-08-17

- Declared a MERGE's target relation as a ROOT input carrying its alias, so `target.x` can
  be mapped back to the relation it names, while holding it out of `alias_source_bindings`
  so the correlated reference a MERGE action preserves is not read as a failed expansion

## 0.1.3 - 2026-08-17

- Stopped re-parsing each statement from generated SQL during task-level modelling. sqlglot
  does not round-trip a WITH carried by an individual UNION branch, so the clauses merged,
  same-named CTEs shadowed each other, and the whole statement degraded to an unqualified
  parse; the AST parsed from the original script is now used directly
- Report `normalized_sql_not_equivalent` when the rendered statement loses a CTE to
  shadowing, so a consumer is not handed SQL that looks runnable and is not
- Stopped reading `COUNT(*)`'s dependency on the whole row as an unexpanded projection
  wildcard; only a source that is actually an unexpanded `SELECT *` reports one now
- Declared the USING relation as an input of a MERGE's ROOT scope. That scope is synthetic,
  so the pass that walks SQLGlot scopes never reached it and the scope reported no inputs at
  all, leaving `source` unbindable for expressions that resolve a qualifier by alias
- Expanded physical-table references in expressions that also reference a query block; the
  alias-expansion helper skipped physical sources entirely, so the alias stayed in the text
  and its field never reached the physical source list
- Report `column_not_in_table_schema` when a qualifier names a table whose schema proves
  the column does not exist; the qualified path previously took a qualifier as proof and
  published the reference as a physical field
- Resolve statements against tables the same script creates, so a `CREATE ... AS SELECT`
  feeding a later statement no longer leaves that statement's columns unexpandable and no
  longer reports the script-local table as missing warehouse metadata
- Finish expression expansion when substitution reintroduces a qualifier belonging to the
  consuming scope, recovering the physical field behind a LATERAL VIEW over a query block
- Fixed MERGE lineage corruption when the statement is preceded by a CTE: qualify
  reorders the column traversal, so pairing pre- and post-qualify columns by position
  pasted a MERGE action's target references onto unrelated CTE projections and
  neighbouring UPDATE assignments. Correlated target references are now protected across
  qualify by identity, and an unrestorable reference fails the statement instead of
  publishing a positional guess
- Resolved MERGE `row_membership_sources` through the built USING scope, so a CTE- or
  subquery-backed USING reports its physical root fields instead of the query block's
  name, a UNION reports every branch instead of the literal `UNKNOWN`, and a condition
  the USING relation does not expose reports a new `merge_condition_source_unresolved`
  fact gap instead of a fabricated column

## 0.1.2 - 2026-08-16

- Documentation and packaging only; no library changes

## 0.1.1 - 2026-08-15

- Added opt-in task-level `schema_version: "2.0"` output with ordered statements and
  table-state transitions for write and mutation operations
- Made rich JSON table schema metadata authoritative over CSV fallbacks, preserving column order,
  DDL, and other structured metadata while reporting conflicts
- Added installation, CLI usage, input-format, schema-precedence, and release documentation

## 0.1.0 - 2026-08-14

Initial public release preparation:

- Added opt-in task-level `schema_version: "2.0"` contracts that preserve statement order and
  model table-state transitions across INSERT, overwrite, CTAS, MERGE, DELETE, UPDATE, and
  TRUNCATE, including partition-scoped replacement/reset behavior
- Separated final-field value provenance, value-condition provenance, and row-membership
  provenance so DELETE predicates are not misrepresented as field value sources
- Added schema fallback merging with conflict reporting, metadata coverage diagnostics, target
  binding reason codes, compact JSON output, and configurable CLI quality gates
- Added a SQLGlot compatibility CI matrix for the oldest, previous, and latest supported releases
- Adapted MERGE scope handling for SQLGlot 30.17 and constrained the verified range to
  `sqlglot>=30,<30.18`; MERGE now uses an explicit USING scope instead of SQLGlot's removed
  root Subquery wrapper
- Fixed MERGE action scalar-subquery lineage so nested predicates are not emitted as target
  assignments, correlated target references remain physical self-sources, and scalar outputs bind
  through their own scopes instead of the USING scope
- Resolve CTE references lexically when collecting physical inputs, preserving an unqualified
  physical table that shares a name with a CTE in a different query block
- Versioned `lineage.json` and `diagnostics.json` 1.0 contracts with mandatory validation
- Pure Core writer API for emitting only Lineage and Diagnostics artifacts
- `scope-lineage parse` CLI for SQL files, exported task JSON, and recursive task directories;
  it still emits only Core artifacts
- Explicit `--catalog-prefixes` / `SCOPE_LINEAGE_CATALOG_PREFIXES` normalization policy; full
  catalog-qualified table identities are preserved by default
- Explicit `PUBLIC_CORE_API` facade for downstream Python consumers
- Wheel and source-distribution manifests contain only Lineage Core
- CI verifies Python 3.9–3.12, archive contents, and a repository-external installation
- Scope-aware column lineage parser for Spark/Hive SQL
- schema metadata loading for `SELECT *` expansion
- target DDL/Schema metadata loading for positional INSERT binding
- production-shaped synthetic examples for task wrappers, task dependencies, complex Spark SQL,
  Schema CSV/JSON, and target-table DDL metadata
- public documentation centered on AI-ready SQL task knowledge bases
- field-level documentation for every major Lineage and Diagnostics object, including scope values,
  logic blocks, mapping chains, end-to-end trace semantics, fact gaps, and safe AI consumption
