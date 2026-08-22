"""Differential output comparison between two versions of this package.

Answers one question with evidence instead of argument: *does the code at HEAD produce
byte-identical documents to the code at BASE_REF for every input in the corpus?* This is
the harness that caught three real output changes the golden corpus missed (2026-08-23):
the goldens cover 12 fixed cases, while a refactor's equivalence claim is about every
input. Run it whenever a change is supposed to be behavior-neutral:

    python tests/architecture/differential_compare.py main
    python tests/architecture/differential_compare.py 625d1c4 --keep

How it works, and why each piece is shaped this way:

- The input corpus (every ``examples/tasks`` JSON, every ``examples/sql`` file, every
  golden ``case.json``) is collected ONCE, from the current tree, into a manifest with
  schemas already resolved to plain dicts -- both engines must judge exactly the same
  inputs, so nothing (not even schema loading) may come from the other checkout.
- BASE_REF is materialized with ``git worktree`` (cleaned up afterwards; ``--keep``
  preserves it for debugging).
- Each engine runs in its OWN subprocess (this same file, ``dump`` mode) with the target
  tree prepended to ``sys.path``: two versions of one package cannot share an
  interpreter.
- Comparison is a recursive walk that reports the exact JSON path of every difference --
  a bare "files differ" is useless for deciding whether a change is a regression, a
  cosmetic drift, or an intended fix.

Exit codes: 0 identical, 1 differences found (or an engine failed on some input),
2 usage/environment error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_REPORTED_DIFFS = 40


# ---------------------------------------------------------------- corpus manifest


def build_manifest(repo: Path) -> dict[str, dict]:
    """Collect every corpus input as {key: {task_name, sql, schema}}.

    Schemas are resolved to plain dicts HERE, with the current tree's loader, so both
    engines receive identical inputs and the comparison isolates parsing behavior.
    """
    sys.path.insert(0, str(repo))
    try:
        from scope_lineage import load_schema
    finally:
        sys.path.pop(0)

    default_schema = dict(load_schema(str(repo / "examples" / "metadata" / "schema_info.json")))

    manifest: dict[str, dict] = {}
    for path in sorted((repo / "examples" / "tasks").rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        meta = document.get("meta") or {}
        sql = document.get("sql") or meta.get("sql") or document.get("task_sql")
        name = document.get("task_name") or meta.get("task_name") or path.stem
        if isinstance(sql, str) and sql.strip():
            manifest[f"task:{path.relative_to(repo).as_posix()}"] = {
                "task_name": name,
                "sql": sql,
                "schema": default_schema,
            }
    for path in sorted((repo / "examples" / "sql").glob("*.sql")):
        manifest[f"sql:{path.name}"] = {
            "task_name": path.stem,
            "sql": path.read_text(encoding="utf-8"),
            "schema": default_schema,
        }
    for path in sorted(repo.glob("tests/core/fixtures/lineage_contract/*/case.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        manifest[f"golden:{path.parent.name}"] = {
            "task_name": case["task_id"],
            "sql": case["sql"],
            "schema": case.get("schema"),
        }
    return manifest


# ---------------------------------------------------------------- dump mode (subprocess)


def dump(tree: Path, manifest_path: Path, out_path: Path) -> None:
    """Run one engine over the manifest; executed in a dedicated subprocess."""
    sys.path.insert(0, str(tree))
    from scope_lineage import parse_all_scope_lineage, parse_task_lineage
    from scope_lineage.contract import to_lineage_dict, to_task_lineage_dict

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}
    for key, item in sorted(manifest.items()):
        entry: dict[str, object] = {}
        try:
            task = parse_task_lineage(item["sql"], task_name=item["task_name"], schema=item["schema"])
            entry["task_doc"] = to_task_lineage_dict(task)
        except Exception as exc:  # noqa: BLE001 - an engine crash IS a finding; recorded, compared, reported
            entry["task_doc_error"] = f"{type(exc).__name__}: {exc}"
        try:
            statements = parse_all_scope_lineage(
                item["sql"], task_name=item["task_name"], schema=item["schema"]
            )
            entry["statement_docs"] = [to_lineage_dict(r) for r in statements]
        except Exception as exc:  # noqa: BLE001 - same: comparison must see the failure, not die on it
            entry["statement_docs_error"] = f"{type(exc).__name__}: {exc}"
        result[key] = entry
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- comparison


def deep_diff(old: object, new: object, path: str = "", limit: int = MAX_REPORTED_DIFFS) -> list[str]:
    """Return one human-readable line per difference, each anchored to its JSON path."""
    diffs: list[str] = []

    def walk(a: object, b: object, at: str) -> None:
        if len(diffs) > limit:
            return
        if type(a) is not type(b):
            diffs.append(f"{at}: type {type(a).__name__} -> {type(b).__name__}")
            return
        if isinstance(a, dict):
            for key in sorted(a.keys() | b.keys()):
                child = f"{at}.{key}" if at else str(key)
                if key not in a:
                    diffs.append(f"{child}: key added in new")
                elif key not in b:
                    diffs.append(f"{child}: key removed in new")
                else:
                    walk(a[key], b[key], child)
        elif isinstance(a, list):
            if len(a) != len(b):
                diffs.append(f"{at}: list length {len(a)} -> {len(b)}")
                return
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{at}[{index}]")
        elif a != b:
            diffs.append(f"{at}: {a!r} -> {b!r}"[:200])

    walk(old, new, path)
    return diffs


def count_leaves(value: object) -> int:
    if isinstance(value, dict):
        return sum(count_leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_leaves(v) for v in value)
    return 1


# ---------------------------------------------------------------- orchestration


def compare(repo: Path, base_ref: str, *, keep: bool) -> int:
    dumps: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="scope-lineage-diff-") as scratch_str:
        scratch = Path(scratch_str)
        base_tree = scratch / "base"
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(base_tree), base_ref],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            print(f"cannot materialize {base_ref!r}: {added.stderr.strip()}", file=sys.stderr)
            return 2
        try:
            manifest_path = scratch / "manifest.json"
            manifest = build_manifest(repo)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            for label, tree in (("base", base_tree), ("head", repo)):
                out = scratch / f"{label}.json"
                run = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "dump",
                     "--tree", str(tree), "--manifest", str(manifest_path), "--out", str(out)],
                    capture_output=True,
                    text=True,
                )
                if run.returncode != 0:
                    print(f"{label} engine failed:\n{run.stderr}", file=sys.stderr)
                    return 2
                dumps[label] = out

            old = json.loads(dumps["base"].read_text(encoding="utf-8"))
            new = json.loads(dumps["head"].read_text(encoding="utf-8"))
            diffs = deep_diff(old, new)
            leaves = count_leaves(old)
            statements = sum(len(entry.get("statement_docs") or []) for entry in old.values())
            engine_errors = [
                f"{key}.{field}: {entry[field]}"
                for docs in (old, new)
                for key, entry in docs.items()
                for field in ("task_doc_error", "statement_docs_error")
                if field in entry
            ]

            print(
                f"compared {len(old)} inputs "
                f"({statements} statement docs + task docs, {leaves:,} leaf key/value pairs) "
                f"against {base_ref}"
            )
            for error in engine_errors:
                print(f"engine error: {error}")
            if diffs:
                shown = diffs[:MAX_REPORTED_DIFFS]
                print(f"DIFFERS: {len(diffs)}{'+' if len(diffs) > len(shown) else ''} difference(s):")
                for line in shown:
                    print(f"  {line}")
                return 1
            if engine_errors:
                print("IDENTICAL documents, but engine errors above need reading")
                return 1
            print("IDENTICAL")
            return 0
        finally:
            if keep:
                kept = Path(tempfile.mkdtemp(prefix="scope-lineage-diff-kept-"))
                print(f"--keep: base worktree preserved under {base_tree} until this "
                      f"process's tempdir vanishes; copies of dumps in {kept}", file=sys.stderr)
                for label, out in dumps.items():
                    (kept / f"{label}.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(base_tree)],
                cwd=repo,
                capture_output=True,
            )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "dump":
        dump_parser = argparse.ArgumentParser(prog="differential_compare.py dump")
        dump_parser.add_argument("--tree", required=True)
        dump_parser.add_argument("--manifest", required=True)
        dump_parser.add_argument("--out", required=True)
        args = dump_parser.parse_args(argv[1:])
        dump(Path(args.tree), Path(args.manifest), Path(args.out))
        return 0

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_ref", help="git ref to compare HEAD's outputs against")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--keep", action="store_true", help="preserve dumps for debugging")
    args = parser.parse_args(argv)
    return compare(Path(args.repo).resolve(), args.base_ref, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
