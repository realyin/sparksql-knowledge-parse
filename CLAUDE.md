# CLAUDE.md — engineering rules for scope-lineage

Distilled from the 2026-08 governance audit, its independent review, and the 0.2.0
release (see CHANGELOG 0.2.0). Every rule below was paid for by a real defect or a
near-miss, and every rule names the mechanism that enforces it — when you change the
rule, change its enforcement in the same PR, and vice versa.

Detailed procedures live in [CONTRIBUTING.md](CONTRIBUTING.md); user-facing contracts in
`docs/zh-CN/`. This file is the part a development session must not violate.

## Architecture

- **Package dependency direction is locked**: `cli → contract → (serialize, scope) →
  metadata`; `render` consumes contract JSON dicts only and imports nothing internal.
  A new cross-package edge is an architecture decision, not a convenience import — add
  it to the allowed set or whitelist in
  [tests/architecture/test_dependency_direction.py](tests/architecture/test_dependency_direction.py)
  with a written reason, or don't create it. One whitelisted edge exists
  (`scope/task_lineage.py → contract`), evaluated and kept at 0.3.0 with its reasoning
  recorded at both ends.
- **No grab-bag modules.** `scope/_shared.py` was 1,571 lines of five unrelated themes
  and hid two same-name-different-behavior helper pairs; it is gone and a tombstone test
  keeps it gone. New shared code goes into the themed module that owns its concept
  (`source_refs`, `expression_text`, `sqlglot_walk`, …); a helper with one caller sinks
  into that caller. Keep modules under ~500 lines.
- **The facade is the only supported import surface.** Deep imports
  (`scope_lineage.scope.<anything>`) are internal and may vanish without notice —
  including for tests in other repositories.

## The fact pipeline (`scope/scope_facts.py`)

- **A new fact pass joins one of the named phases** of
  `_populate_enhanced_scope_facts`; the wiring guard
  ([tests/core/test_fact_pipeline_wiring.py](tests/core/test_fact_pipeline_wiring.py))
  fails on any pass function defined but not reachable from the pipeline root. Never
  splice a pass call somewhere else "just for now" — that is exactly how the old 30-call
  hand-ordered sequence grew.
- **The tail truncation is load-bearing.** The internal-resolution pass is NOT
  idempotent (documented at `_should_rebuild_internal_expansion_from_expression`):
  running it after the final output-sources call rewrites settled resolutions and
  collapses provenance traces. Do not "clean up" the tail into a loop, do not reorder
  it, and do not add a guard to make it look idempotent — that was tried and reverted
  because the guard also changed mid-pipeline behavior. The sentinels in
  [tests/core/test_scope_output_trace_granularity.py](tests/core/test_scope_output_trace_granularity.py)
  stand on this ordering.
- **Pass payloads are typed.** `expression_resolution` and its nested shapes are
  declared in `scope/fact_protocols.py`; a pass that starts emitting an undeclared key
  fails the corpus-coverage test. Extend the TypedDict in the same change.

## Contracts and public API

- **The statement-document shape is the task contract's payload**, not a v1 leftover:
  every `statement_lineage` entry carries it, `lineage.schema.json` /
  `diagnostics.schema.json` are its schemas, and entries are validated against them.
  Deleting "old" schemas or converters requires proving nothing embeds them — the plan
  that said "delete the v1 schemas" was wrong on inspection.
- **Golden byte-comparison is the reliable defense.** Semantic sentinel tests missed a
  provenance-quality regression that only the byte diff caught. Never re-record a golden
  to make a red test green without understanding the diff; the re-record procedure
  (rebase on latest main + sqlglot matrix loop) is in CONTRIBUTING.
- **`PUBLIC_CORE_API` changes are contract changes.** The required-symbols fixture is a
  LOWER BOUND (symbols that must exist), never a removal list; removals take a
  deprecation cycle and a CHANGELOG **Breaking** entry. Never export a symbol from a
  `_`-prefixed module — promote it to a real module first.

## Verification hierarchy (the core lesson of the audit)

1. **A green suite is necessary, not sufficient.** The suite was green while the
   `render` CLI silently produced nothing: parse and render each had coverage, no test
   ran the chain. Every CLI capability needs at least one end-to-end test of its real
   chain, and a library capability is not done until the CLI that should expose it is
   wired and tested.
2. **A behavior-neutral claim requires differential proof.** Run
   `python tests/architecture/differential_compare.py main` (repo corpus; extend the
   corpus rather than the script). The golden fixtures alone once missed three real
   output changes that the differential caught. Claims must not outrun their evidence:
   "equivalent on 12 goldens" is not "equivalent".
3. **Establish premises by experiment, not by reading.** The pipeline's repeated passes
   looked like a fix-point iteration; an experiment showed the truncation was
   load-bearing. When a refactor rests on a claim about behavior, test the claim first.
4. **CI must actually run what you wrote.** The test job enumerates paths explicitly
   (`pytest -q tests/core tests/architecture`); a new test directory that is not added
   to [.github/workflows/ci.yml](.github/workflows/ci.yml) never executes. Verify a new
   guard can fail (inject a violation) before trusting it.

## Code gates

- Ruff runs `F`, `BLE` and `C901` (threshold 24). Every blind `except Exception` carries
  `# noqa: BLE001 - <reason>`; the four legacy complexity exemptions are marked
  `noqa: C901 - legacy exemption (WI-11)` and shrink when touched. The seven 150-line
  functions listed in CONTRIBUTING only ever get shorter.
- Decision comments state the why and cite the internal issue id (PERF-001 style); a
  comment that narrates what the next line does is noise.
- Python floor is 3.9: `TypedDict(total=False)` yes, `NotRequired` no; pyright runs in
  basic mode with `pythonVersion: "3.9"` on the typed slice (non-blocking CI job until
  the slice covers the package).
- Dev environments pin sqlglot to a CI-matrix version via `constraints-dev.txt`; matrix
  and pin move together.

## Merge and release

- **Check that main has not advanced before merging** (`git fetch` + merge-base); a
  governance branch once collided with a parallel contract-fix PR and needed a verified
  merge. Never enable auto-merge — without required checks it bypasses CI; wait for
  green, then squash-merge with subject `<type>: <summary> (#PR)`.
- Release order is fixed: bump `pyproject.toml` + retitle CHANGELOG → PR → CI green →
  merge → `gh release create vX.Y.Z --target main` (the workflow rejects a tag that does
  not match the package version) → the release workflow publishes to PyPI. Breaking
  changes ship only in minor bumps, each with a **Breaking** CHANGELOG entry and a
  migration section in both READMEs.

## Index

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, golden re-record procedure, differential
  command, long-function list, domain-neutrality rule.
- [tests/architecture/](tests/architecture/) — the executable rules: dependency
  direction, distribution boundary, differential harness.
- [CHANGELOG.md](CHANGELOG.md) — the 0.2.0 entry is the case law behind these rules.
- `docs/zh-CN/` — user-facing contract documentation.
