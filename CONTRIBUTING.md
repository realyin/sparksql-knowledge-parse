# Contributing

Thanks for considering a contribution.

Scope Lineage is intentionally conservative: lineage output should be
explainable, auditable, and explicit about uncertainty. Parser changes should
come with tests that cover both the parsed structure and the diagnostic behavior
when the SQL is ambiguous.

## Development

```bash
python -m pip install -e ".[dev]" -c constraints-dev.txt   # pins sqlglot to a CI-matrix version
python -m pytest -q tests/core
python -m pytest tests/core/test_lineage_contract_baseline.py -q
git diff --check                       # whitespace/conflict check after edits
```

A change that claims to be behavior-neutral (refactors above all) must prove it against
more than the 12 golden fixtures -- they once missed three real output changes that a
corpus-wide comparison caught. Run the differential harness against the branch point:

```bash
python tests/architecture/differential_compare.py main
```

It runs both code versions over every example task, example SQL, and golden case
(currently 21 inputs, ~257k leaf key/value pairs) and reports each difference with its
exact JSON path; exit 0 means byte-identical documents.

Before landing any change that re-records a golden baseline, rebase on the latest `main` and
run the suite once per CI compat-matrix sqlglot version -- a baseline recorded against one
version can silently disagree with the others:

```bash
for v in 30.0.0 30.16.0 30.17.0; do
  pip install -q "sqlglot==$v" && python -m pytest -q || break
done
pip install -q -c constraints-dev.txt sqlglot   # restore the dev pin afterwards
```

Seven functions are 150 lines or longer (`parse_task_lineage`,
`_resolve_internal_scope_expression_resolution`, `_apply_projection_write`,
`_build_result_from_scope`, `cli.main`, `_resolve_merge_columns`, `_expand_star_into_columns`).
When a change touches one of them, extract the segment you modified into a named private
function as part of the same change -- they shrink opportunistically, never grow. Ruff's
`C901` (threshold 24) keeps new complexity out; the four legacy exemptions are marked
`noqa: C901 - legacy exemption (WI-11)` and each removal is welcome. Every blind
`except Exception` must carry a `noqa: BLE001 - <reason>` naming why the boundary is
allowed to be blind.

Keep Core domain-neutral. Warehouse layer names, business-domain rules, report builders, and
modeling recommendations belong in downstream projects rather than this package.

## Architecture rules (each enforced by a test)

- **Package dependency direction is locked** -- `cli -> contract -> (serialize, scope) ->
  metadata`; `render` consumes contract JSON dicts only. A new cross-package edge is an
  architecture decision: extend the allowed set or whitelist in
  `tests/architecture/test_dependency_direction.py` with a written reason, or do not
  create the edge.
- **No grab-bag modules.** Shared scope code goes into the themed module that owns its
  concept; a helper with one caller sinks into that caller; a revived `_shared.py` fails
  its tombstone test. Never export a public symbol from a `_`-prefixed module.
- **A new fact pass joins a named phase** of `_populate_enhanced_scope_facts`; the wiring
  guard (`tests/core/test_fact_pipeline_wiring.py`) fails on a pass that is defined but
  not reachable. The pipeline tail's exact truncation is load-bearing -- the
  internal-resolution pass is not idempotent (documented at
  `_should_rebuild_internal_expansion_from_expression`); the provenance sentinels stand
  on that ordering.
- **Pass payloads are typed** in `scope/fact_protocols.py`; a pass emitting an
  undeclared `expression_resolution` key fails the corpus-coverage test.

## Verification hierarchy

1. A green suite is necessary, not sufficient: every CLI capability needs at least one
   end-to-end test of its real chain, and a library capability is not done until the CLI
   that should expose it is wired and tested.
2. A behavior-neutral claim requires the differential harness (above), not an argument;
   equivalence claims must not outrun their evidence.
3. Establish premises by experiment, not by reading -- when a refactor rests on a claim
   about behavior, test the claim first.
4. CI must actually run what you wrote: the test job enumerates paths explicitly, so a
   new test directory must be added to `.github/workflows/ci.yml`, and a new guard is
   trusted only after you have watched it fail on an injected violation.

## Merge and release

- Before merging, fetch and confirm `main` has not advanced past your branch point; if
  it has, merge it in and re-run the full verification (suite, matrix, differential).
- Never enable auto-merge (without required checks it bypasses CI); wait for green, then
  squash-merge with subject `<type>: <summary> (#PR)`.
- Release order: bump `pyproject.toml` + retitle CHANGELOG -> PR -> CI green -> merge ->
  `gh release create vX.Y.Z --target main` (the workflow rejects a tag that does not
  match the package version) -> the release workflow publishes to PyPI. Breaking changes
  ship only in minor bumps, each with a **Breaking** CHANGELOG entry and a migration
  section in both READMEs.
- Before pushing, opening a PR, or writing release notes, scan every text that will
  become public -- commit messages and PR bodies included -- for private corpus names,
  sizes, and measurement figures; published sdists cannot be edited afterwards. Keep
  proportions, drop absolutes; cite local verification notes instead of restating
  numbers.

## Pull Request Checklist

- Add or update tests for parser behavior.
- Add diagnostics tests when the behavior is uncertain or lossy.
- When you change an actively used output's structure/field meaning/evidence
  path, update the matching contract document under `docs/` in the same change.
- Keep examples synthetic and free of private table names, emails, or paths.

## SQL Fixtures

Do not add private production SQL to the public repository. If a real failure
requires a regression test, reduce it to a synthetic SQL statement that preserves
the parser shape but removes business names and private identifiers.
