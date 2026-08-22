"""Minimal command line interface for the public Lineage Core."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contract import write_lineage, write_task_lineage
from .metadata.schema_metadata import load_schema, load_schema_sources
from .metadata.target_table_metadata import load_target_table_metadata
from .scope.scope_builder import parse_all_scope_lineage
from .scope.task_lineage import parse_task_lineage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-lineage")
    subcommands = parser.add_subparsers(dest="command", required=True)
    parse_cmd = subcommands.add_parser(
        "parse",
        help="Parse SQL or exported task JSON into Core artifacts",
    )
    input_group = parse_cmd.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--sql-file", help="Path to one SQL file")
    input_group.add_argument(
        "--task-file",
        help="Path to one task JSON (meta/sql wrapper or legacy task_name/sql object)",
    )
    input_group.add_argument(
        "--input-dir",
        help="Directory of task JSON files; files are discovered recursively",
    )
    parse_cmd.add_argument(
        "--task-name",
        help="Override the task name for --sql-file or --task-file",
    )
    parse_cmd.add_argument("--out", required=True, help="Output directory")
    parse_cmd.add_argument(
        "--schema",
        help="Optional source-table schema file or rich-JSON directory (JSON preferred; CSV fallback)",
    )
    parse_cmd.add_argument(
        "--schema-fallback",
        action="append",
        default=[],
        help=(
            "Additional CSV/JSON schema source used only for tables absent from "
            "the authoritative --schema; repeatable"
        ),
    )
    parse_cmd.add_argument(
        "--target-ddl-metadata",
        help="Optional authoritative target-table DDL/Schema JSON file or directory",
    )
    parse_cmd.add_argument(
        "--partition-overwrite-mode",
        help=(
            "The cluster's spark.sql.sources.partitionOverwriteMode (static or dynamic, "
            "case-insensitive). Spark's own default is static; declare what your "
            "deployment actually runs with. A SET in the script always wins. "
            "Requires --contract-version 2.0."
        ),
    )
    parse_cmd.add_argument(
        "--catalog-prefixes",
        help=(
            "Comma-separated leading catalog names to remove from table identities. "
            "Overrides SCOPE_LINEAGE_CATALOG_PREFIXES; by default catalogs are preserved."
        ),
    )
    parse_cmd.add_argument(
        "--sanitize-metadata-nul",
        action="store_true",
        help="Remove NUL bytes from metadata inputs and report provenance",
    )
    parse_cmd.add_argument(
        "--allow-partial",
        action="store_true",
        help="Return zero even when a statement produced parse_status=failed",
    )
    parse_cmd.add_argument(
        "--contract-version",
        choices=("1.0", "2.0"),
        default="1.0",
        help=(
            "Output contract: 1.0 keeps one artifact per projection write; "
            "2.0 emits one task-level ordered table-state artifact"
        ),
    )
    parse_cmd.add_argument(
        "--compact-json",
        action="store_true",
        help="Write the same JSON contract without pretty-print whitespace",
    )
    parse_cmd.add_argument(
        "--quality-policy",
        choices=("permissive", "balanced", "strict"),
        default="permissive",
        help=(
            "Quality gate: permissive preserves parse-only exit behavior; balanced "
            "rejects unsupported row mutations; strict also rejects recovered syntax, "
            "root-impact lineage gaps, and target-binding fallback"
        ),
    )
    parse_cmd.add_argument(
        "--fail-on-root-gap",
        action="store_true",
        help="Return non-zero when a lineage fact gap impacts a final target field",
    )
    parse_cmd.add_argument(
        "--fail-on-unsupported-mutation",
        action="store_true",
        help="Return non-zero when DELETE/UPDATE/TRUNCATE is not modeled",
    )
    parse_cmd.add_argument(
        "--fail-on-binding-fallback",
        action="store_true",
        help="Return non-zero when authoritative target-field binding falls back",
    )

    render_cmd = subcommands.add_parser(
        "render",
        help="Render mapping.md field-mapping documents from existing Core artifacts",
    )
    render_cmd.add_argument(
        "--lineage",
        required=True,
        help="One lineage.json file, or a directory searched recursively for lineage.json",
    )
    render_cmd.add_argument(
        "--out",
        help=(
            "Directory for the rendered mapping.md files, mirroring the input tree; "
            "default writes mapping.md next to each lineage.json"
        ),
    )
    render_cmd.add_argument(
        "--field",
        action="append",
        default=None,
        help="Restrict the per-field step sections to this target field; repeatable",
    )
    render_cmd.add_argument(
        "--expanded",
        action="store_true",
        help="Add the fully expanded physical-field expression under each step",
    )
    render_cmd.add_argument(
        "--sections",
        help="Comma-separated section names to render (default: all)",
    )

    args = parser.parse_args(argv)
    if args.command == "parse":
        if args.input_dir and args.task_name:
            parser.error("--task-name cannot be used with --input-dir")
        if getattr(args, "partition_overwrite_mode", None) is not None:
            # Validated here rather than per input: one bad value is one error, not one
            # per task. `nonstrict` is the neighbouring Hive key's value and the
            # predictable mistake.
            if args.partition_overwrite_mode.strip().lower() not in {"static", "dynamic"}:
                parser.error(
                    "--partition-overwrite-mode must be static or dynamic, got "
                    f"{args.partition_overwrite_mode!r}"
                )
            if args.contract_version != "2.0":
                # Contract 1.0 models no overwrite effect at all, so the value would be
                # silently inert. Erroring matches how the CLI rejects other
                # incompatible flag pairs.
                parser.error(
                    "--partition-overwrite-mode requires --contract-version 2.0"
                )
        with _catalog_prefix_override(args.catalog_prefixes):
            return _parse_inputs(args)
    if args.command == "render":
        return _render_inputs(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _render_inputs(args: argparse.Namespace) -> int:
    from .render.mapping_markdown import (
        SUPPORTED_SCHEMA_VERSION,
        render_mapping_markdown,
        render_warnings_markdown,
    )

    root = Path(args.lineage)
    if root.is_file():
        documents = [root]
        base = root.parent
    elif root.is_dir():
        documents = sorted(root.rglob("lineage.json"))
        base = root
    else:
        print(f"--lineage path does not exist: {root}", file=sys.stderr)
        return 2
    if not documents:
        print(f"no lineage.json found under {root}", file=sys.stderr)
        return 1

    sections = args.sections.split(",") if args.sections else None
    rendered = 0
    skipped_v2 = 0
    missing_diagnostics = 0
    for lineage_path in documents:
        document = json.loads(lineage_path.read_text(encoding="utf-8"))
        if document.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            if root.is_file():
                print(
                    "mapping renderer supports schema_version "
                    f"{SUPPORTED_SCHEMA_VERSION}; {lineage_path} declares "
                    f"{document.get('schema_version')!r}",
                    file=sys.stderr,
                )
                return 1
            skipped_v2 += 1
            continue
        diagnostics_path = lineage_path.parent / "diagnostics.json"
        diagnostics = None
        if diagnostics_path.is_file():
            diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        else:
            missing_diagnostics += 1
        try:
            markdown = render_mapping_markdown(
                document,
                diagnostics,
                fields=args.field,
                expanded=args.expanded,
                sections=sections,
            )
        except ValueError as error:
            print(f"{lineage_path}: {error}", file=sys.stderr)
            return 1
        if args.out:
            target_dir = Path(args.out) / lineage_path.parent.relative_to(base)
            target_dir.mkdir(parents=True, exist_ok=True)
        else:
            target_dir = lineage_path.parent
        (target_dir / "mapping.md").write_text(markdown, encoding="utf-8")
        warnings_markdown = render_warnings_markdown(diagnostics, document)
        if warnings_markdown is not None:
            (target_dir / "warnings.md").write_text(warnings_markdown, encoding="utf-8")
        rendered += 1

    print(
        f"Rendered {rendered} mapping document(s) "
        f"(skipped_v2={skipped_v2}, missing_diagnostics={missing_diagnostics})"
    )
    return 0


@dataclass(frozen=True)
class _TaskInput:
    source_path: Path
    relative_parent: Path
    task_name: str
    sql: str
    task_dependencies: dict


@contextmanager
def _catalog_prefix_override(value: str | None):
    """Apply a CLI-only catalog policy without leaking it to later in-process calls."""
    if value is None:
        yield
        return
    key = "SCOPE_LINEAGE_CATALOG_PREFIXES"
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _parse_inputs(args: argparse.Namespace) -> int:
    schema_paths = [
        path
        for path in [args.schema, *args.schema_fallback]
        if path
    ]
    schema = None
    if schema_paths:
        loader = load_schema_sources if len(schema_paths) > 1 else load_schema
        schema = loader(
            schema_paths if len(schema_paths) > 1 else schema_paths[0],
            sanitize_nul=args.sanitize_metadata_nul,
        )
    target_metadata = (
        load_target_table_metadata(
            args.target_ddl_metadata,
            sanitize_nul=args.sanitize_metadata_nul,
        )
        if args.target_ddl_metadata
        else None
    )
    out_root = Path(args.out)
    source_paths, input_root = _source_paths(args)
    if args.contract_version == "2.0":
        return _parse_task_inputs_v2(
            args,
            schema=schema,
            target_metadata=target_metadata,
            out_root=out_root,
            source_paths=source_paths,
            input_root=input_root,
        )
    result_count = 0
    failed_count = 0
    input_failed_count = 0
    unsupported_mutation_count = 0
    root_gap_result_count = 0
    binding_fallback_count = 0
    recovered_syntax_count = 0
    claimed_output_dirs: dict[Path, Path] = {}

    for source_path in source_paths:
        try:
            task = _load_task_input(source_path, input_root, args.task_name)
            results = parse_all_scope_lineage(
                task.sql,
                task_name=task.task_name,
                schema=schema,
                target_metadata=target_metadata,
            )
            if results:
                unsupported_mutation_count += sum(
                    1
                    for item in results[0].skipped_statements
                    if item.get("category") == "row_mutation"
                )
            for result in results:
                result.task_dependencies = task.task_dependencies
                task_out = (
                    out_root
                    / task.relative_parent
                    / result.task_id.replace("#", "_")
                )
                claimed_by = claimed_output_dirs.get(task_out)
                if claimed_by is not None and claimed_by != source_path:
                    raise ValueError(
                        f"output directory collision: {task_out} is already used by "
                        f"{claimed_by}"
                    )
                claimed_output_dirs[task_out] = source_path
                write_lineage(result, task_out, compact=args.compact_json)
                result_count += 1
                if result.parse_status == "failed":
                    failed_count += 1
                    _print_parse_failure(result)
                if any(
                    gap.get("root_impact")
                    for gap in result.diagnostics.lineage_fact_gaps
                    if isinstance(gap, dict)
                ):
                    root_gap_result_count += 1
                if result.target_field_binding.get("status") == "fallback":
                    binding_fallback_count += 1
                if result.syntax_status == "recovered":
                    recovered_syntax_count += 1
        except Exception as exc:  # noqa: BLE001 - batch boundary: one bad input must not kill the run; type+traceback go to stderr
            input_failed_count += 1
            print(f"  FAILED {source_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            # The traceback is what separates a Core bug from a bad input file.
            print(traceback.format_exc().rstrip(), file=sys.stderr)

    print(
        f"Parsed {result_count} statement(s) from {len(source_paths)} input(s) "
        f"into {out_root} "
        f"(ok={result_count - failed_count}, failed={failed_count}, "
        f"input_failed={input_failed_count}, "
        f"unsupported_mutations={unsupported_mutation_count}, "
        f"root_gap_results={root_gap_result_count}, "
        f"binding_fallbacks={binding_fallback_count}, "
        f"recovered_syntax={recovered_syntax_count})"
    )
    quality_failed = _quality_gate_failed(
        args,
        unsupported_mutation_count=unsupported_mutation_count,
        root_gap_result_count=root_gap_result_count,
        binding_fallback_count=binding_fallback_count,
        recovered_syntax_count=recovered_syntax_count,
    )
    if not failed_count and not input_failed_count and not quality_failed:
        return 0
    if quality_failed:
        return 1
    return 0 if args.allow_partial else 1


def _parse_task_inputs_v2(
    args: argparse.Namespace,
    *,
    schema,
    target_metadata,
    out_root: Path,
    source_paths: list[Path],
    input_root: Path | None,
) -> int:
    task_count = 0
    statement_count = 0
    modeled_count = 0
    failed_count = 0
    input_failed_count = 0
    partial_task_count = 0
    unsupported_mutation_count = 0
    root_gap_result_count = 0
    binding_fallback_count = 0
    recovered_syntax_count = 0
    claimed_output_dirs: dict[Path, Path] = {}

    for source_path in source_paths:
        try:
            task = _load_task_input(source_path, input_root, args.task_name)
            result = parse_task_lineage(
                task.sql,
                task_name=task.task_name,
                schema=schema,
                target_metadata=target_metadata,
                task_dependencies=task.task_dependencies,
                partition_overwrite_mode=getattr(args, "partition_overwrite_mode", None),
            )
            task_out = (
                out_root
                / task.relative_parent
                / result.task_id.replace("#", "_")
            )
            claimed_by = claimed_output_dirs.get(task_out)
            if claimed_by is not None and claimed_by != source_path:
                raise ValueError(
                    f"output directory collision: {task_out} is already used by "
                    f"{claimed_by}"
                )
            claimed_output_dirs[task_out] = source_path
            write_task_lineage(
                result,
                task_out,
                compact=args.compact_json,
            )
            task_count += 1
            statement_count += len(result.statements)
            modeled_count += sum(
                item.get("model_status") == "modeled"
                for item in result.statements
            )
            failed_count += sum(
                item.get("model_status") == "failed"
                for item in result.statements
            )
            partial_task_count += result.analysis_status.get("status") == "partial"
            unsupported_mutation_count += sum(
                item.get("category") == "row_mutation"
                and item.get("model_status") != "modeled"
                for item in result.statements
            )
            root_gap_result_count += any(
                gap.get("root_impact")
                for gap in result.diagnostics.get("lineage_fact_gaps", [])
                if isinstance(gap, dict)
            )
            binding_fallback_count += sum(
                (lineage.get("target_field_binding") or {}).get("status")
                == "fallback"
                for lineage in result.statement_lineage.values()
            )
            recovered_syntax_count += result.syntax_status == "recovered"
        except Exception as exc:  # noqa: BLE001 - batch boundary: one bad input must not kill the run; type+traceback go to stderr
            input_failed_count += 1
            print(
                f"  FAILED {source_path}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            # The traceback is what separates a Core bug from a bad input file.
            print(traceback.format_exc().rstrip(), file=sys.stderr)

    print(
        f"Parsed {statement_count} statement(s) from {len(source_paths)} input(s) "
        f"into {out_root} using contract 2.0 "
        f"(tasks={task_count}, modeled={modeled_count}, failed={failed_count}, "
        f"input_failed={input_failed_count}, partial_tasks={partial_task_count}, "
        f"unsupported_mutations={unsupported_mutation_count}, "
        f"root_gap_results={root_gap_result_count}, "
        f"binding_fallbacks={binding_fallback_count}, "
        f"recovered_syntax={recovered_syntax_count})"
    )
    quality_failed = _quality_gate_failed(
        args,
        unsupported_mutation_count=unsupported_mutation_count,
        root_gap_result_count=root_gap_result_count,
        binding_fallback_count=binding_fallback_count,
        recovered_syntax_count=recovered_syntax_count,
    )
    if not failed_count and not input_failed_count and not quality_failed:
        return 0
    if quality_failed:
        return 1
    return 0 if args.allow_partial else 1


def _quality_gate_failed(
    args: argparse.Namespace,
    *,
    unsupported_mutation_count: int,
    root_gap_result_count: int,
    binding_fallback_count: int,
    recovered_syntax_count: int,
) -> bool:
    balanced = args.quality_policy in {"balanced", "strict"}
    strict = args.quality_policy == "strict"
    return bool(
        (unsupported_mutation_count and (balanced or args.fail_on_unsupported_mutation))
        or (root_gap_result_count and (strict or args.fail_on_root_gap))
        or (binding_fallback_count and (strict or args.fail_on_binding_fallback))
        or (recovered_syntax_count and strict)
    )


def _source_paths(args: argparse.Namespace) -> tuple[list[Path], Path | None]:
    if args.sql_file:
        return [Path(args.sql_file)], None
    if args.task_file:
        return [Path(args.task_file)], None
    input_root = Path(args.input_dir)
    if not input_root.is_dir():
        raise ValueError(f"task input directory does not exist: {input_root}")
    paths = sorted(input_root.rglob("*.json"))
    if not paths:
        raise ValueError(f"task input directory contains no JSON files: {input_root}")
    return paths, input_root


def _load_task_input(
    source_path: Path,
    input_root: Path | None,
    task_name_override: str | None,
) -> _TaskInput:
    relative_parent = (
        source_path.parent.relative_to(input_root)
        if input_root is not None
        else Path()
    )
    if source_path.suffix.lower() == ".sql":
        return _TaskInput(
            source_path=source_path,
            relative_parent=relative_parent,
            task_name=task_name_override or source_path.stem,
            sql=source_path.read_text(encoding="utf-8"),
            task_dependencies=_empty_task_dependencies("sql_file"),
        )

    document = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("task JSON top level must be an object")
    meta = document.get("meta")
    payload = meta if isinstance(meta, dict) else document
    sql = payload.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("task JSON must contain a non-empty string at meta.sql or sql")
    task_name = (
        task_name_override
        or _clean_value(payload.get("task_name"))
        or _clean_value(payload.get("task_id"))
        or source_path.stem
    )
    return _TaskInput(
        source_path=source_path,
        relative_parent=relative_parent,
        task_name=task_name,
        sql=sql,
        task_dependencies=_task_dependencies(document, source_path),
    )


def _task_dependencies(document: dict, source_path: Path) -> dict:
    meta = document.get("meta")
    if not isinstance(meta, dict):
        return _empty_task_dependencies("task_json_legacy")
    upstream = _dependency_items(
        meta.get("upstream_tasks"), "upstream", source_path
    )
    downstream = _dependency_items(
        meta.get("downstream_tasks"), "downstream", source_path
    )
    return {
        "upstream_tasks": upstream,
        "downstream_tasks": downstream,
        "source_summary": {
            "source_format": "task_info_meta",
            "upstream_count": len(upstream),
            "downstream_count": len(downstream),
            "has_declared_task_dependencies": bool(upstream or downstream),
        },
    }


def _dependency_items(records, direction: str, source_path: Path) -> list[dict]:
    items = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        task_name = _clean_value(record.get("task_name") or record.get("task_id"))
        if not task_name:
            continue
        items.append(
            {
                "dependency_id": f"taskdep:{direction}:{len(items) + 1:03d}",
                "direction": direction,
                "task_id": _clean_value(record.get("task_id")),
                "task_name": task_name,
                "task_group": _clean_value(record.get("task_group")),
                "project_name": _clean_value(record.get("project_name")),
                "dependency_type": "declared",
                "dependency_table": _clean_value(
                    record.get("dependency_table") or record.get("table")
                ),
                "source": f"task_info.meta.{direction}_tasks",
                "source_file": source_path.as_posix(),
                "raw_record": record,
            }
        )
    return items


def _empty_task_dependencies(source_format: str) -> dict:
    return {
        "upstream_tasks": [],
        "downstream_tasks": [],
        "source_summary": {
            "source_format": source_format,
            "upstream_count": 0,
            "downstream_count": 0,
            "has_declared_task_dependencies": False,
        },
    }


def _clean_value(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _print_parse_failure(result) -> None:
    reasons = [
        warning.msg
        for warning in result.diagnostics.warnings
        if warning.type == "LINEAGE_ERROR"
    ]
    print(
        f"  FAILED {result.task_id}: "
        f"{reasons[0] if reasons else 'scope build failed'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
