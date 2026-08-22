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

Before landing any change that re-records a golden baseline, rebase on the latest `main` and
run the suite once per CI compat-matrix sqlglot version -- a baseline recorded against one
version can silently disagree with the others:

```bash
for v in 30.0.0 30.16.0 30.17.0; do
  pip install -q "sqlglot==$v" && python -m pytest -q || break
done
pip install -q -c constraints-dev.txt sqlglot   # restore the dev pin afterwards
```

Keep Core domain-neutral. Warehouse layer names, business-domain rules, report builders, and
modeling recommendations belong in downstream projects rather than this package.

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
