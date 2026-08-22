"""Public import surface and minimal Core CLI behavior.

The required-symbols fixture is a LOWER BOUND: every listed symbol must stay exported;
a symbol absent from the list is not thereby removable -- it may simply predate the
guard. Removing anything from PUBLIC_CORE_API takes a deprecation cycle and downstream
confirmation. The four symbols once pending confirmation (build_end_to_end_lineage,
build_scope_profile, materialize_schema, table_details_for_table) were removed with the
downstream's retirement -- their implementations remain internal to the packages that
own them. (Governance plan WI-13, closed.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import scope_lineage
from scope_lineage.cli import main


REQUIRED_SYMBOLS = (
    Path(__file__).parent / "fixtures" / "public-api-required-symbols.json"
)


def test_public_exports_match_the_declared_core_api() -> None:
    assert set(scope_lineage.__all__) == scope_lineage.PUBLIC_CORE_API


def test_public_api_covers_the_approved_consumer_surface() -> None:
    required = set(json.loads(REQUIRED_SYMBOLS.read_text(encoding="utf-8")))
    missing = required - scope_lineage.PUBLIC_CORE_API

    assert not missing, f"Public Core API is missing approved symbols: {sorted(missing)}"
    assert all(hasattr(scope_lineage, name) for name in required)


def test_core_cli_writes_only_lineage_and_diagnostics(tmp_path) -> None:
    sql_path = tmp_path / "demo.sql"
    sql_path.write_text(
        "INSERT INTO mart.t SELECT id FROM ods.source",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--out",
        str(output),
    ]) == 0

    task_dir = output / "demo"
    assert {path.name for path in task_dir.iterdir()} == {
        "lineage.json",
        "diagnostics.json",
    }
    assert json.loads((task_dir / "lineage.json").read_text())["schema_version"] == "2.0"


def test_core_cli_accepts_exported_task_json_and_keeps_dependencies(tmp_path) -> None:
    task_path = tmp_path / "daily_customer.json"
    task_path.write_text(
        json.dumps(
            {
                "meta": {
                    "task_id": "task-002",
                    "task_name": "daily_customer",
                    "upstream_tasks": [
                        {"task_id": "task-001", "task_name": "clean_customer"}
                    ],
                    "downstream_tasks": [],
                    "sql": "INSERT INTO mart.customer SELECT id FROM ods.customer",
                },
                "query_time": "2026-08-01 10:00:00",
                "data_source": "scheduler_api",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--task-file",
        str(task_path),
        "--out",
        str(output),
    ]) == 0

    lineage = json.loads(
        (output / "daily_customer" / "lineage.json").read_text(encoding="utf-8")
    )
    dependencies = lineage["task_dependencies"]
    assert dependencies["source_summary"] == {
        "source_format": "task_info_meta",
        "upstream_count": 1,
        "downstream_count": 0,
        "has_declared_task_dependencies": True,
    }
    assert dependencies["upstream_tasks"][0]["task_name"] == "clean_customer"


def test_core_cli_parses_task_directory_recursively(tmp_path) -> None:
    input_dir = tmp_path / "tasks"
    nested_dir = input_dir / "customer_domain"
    nested_dir.mkdir(parents=True)
    (input_dir / "orders.json").write_text(
        json.dumps(
            {
                "task_name": "orders",
                "sql": "INSERT INTO mart.orders SELECT id FROM ods.orders",
            }
        ),
        encoding="utf-8",
    )
    (nested_dir / "customers.json").write_text(
        json.dumps(
            {
                "meta": {
                    "task_name": "customers",
                    "sql": "INSERT INTO mart.customers SELECT id FROM ods.customers",
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--input-dir",
        str(input_dir),
        "--out",
        str(output),
    ]) == 0

    assert (output / "orders" / "lineage.json").is_file()
    assert (output / "customer_domain" / "customers" / "lineage.json").is_file()


def test_core_cli_rejects_empty_task_sql(tmp_path) -> None:
    task_path = tmp_path / "empty.json"
    task_path.write_text(
        json.dumps({"meta": {"task_name": "empty", "sql": ""}}),
        encoding="utf-8",
    )

    assert main([
        "parse",
        "--task-file",
        str(task_path),
        "--out",
        str(tmp_path / "output"),
    ]) == 1


def test_core_cli_preserves_catalog_by_default_and_strips_configured_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("SCOPE_LINEAGE_CATALOG_PREFIXES", raising=False)
    sql_path = tmp_path / "catalog.sql"
    sql_path.write_text(
        "INSERT INTO mart.orders SELECT id FROM warehouse_catalog.ods.orders",
        encoding="utf-8",
    )

    default_output = tmp_path / "default"
    # Pinned to 1.0 (deprecation-window coverage): the assertions read the v1
    # statement-document shape; catalog handling itself is contract-independent.
    assert main([
        "parse",
        "--contract-version",
        "1.0",
        "--sql-file",
        str(sql_path),
        "--out",
        str(default_output),
    ]) == 0
    default_lineage = json.loads(
        (default_output / "catalog" / "lineage.json").read_text(encoding="utf-8")
    )
    assert default_lineage["source_tables"] == ["warehouse_catalog.ods.orders"]

    configured_output = tmp_path / "configured"
    assert main([
        "parse",
        "--contract-version",
        "1.0",
        "--sql-file",
        str(sql_path),
        "--catalog-prefixes",
        "warehouse_catalog",
        "--out",
        str(configured_output),
    ]) == 0
    configured_lineage = json.loads(
        (configured_output / "catalog" / "lineage.json").read_text(
            encoding="utf-8"
        )
    )
    assert configured_lineage["source_tables"] == ["ods.orders"]
    assert configured_lineage["end_to_end_lineage"][0]["physical_sources"] == [
        {"table": "ods.orders", "column": "id", "transform": "DIRECT"}
    ]
    assert "SCOPE_LINEAGE_CATALOG_PREFIXES" not in os.environ


def test_core_cli_catalog_prefixes_override_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCOPE_LINEAGE_CATALOG_PREFIXES", "environment_catalog")
    sql_path = tmp_path / "catalog.sql"
    sql_path.write_text(
        "INSERT INTO mart.t SELECT id FROM cli_catalog.ods.source",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--contract-version",
        "1.0",
        "--sql-file",
        str(sql_path),
        "--catalog-prefixes",
        "cli_catalog",
        "--out",
        str(output),
    ]) == 0

    lineage = json.loads(
        (output / "catalog" / "lineage.json").read_text(encoding="utf-8")
    )
    assert lineage["source_tables"] == ["ods.source"]
    assert os.environ["SCOPE_LINEAGE_CATALOG_PREFIXES"] == "environment_catalog"


def test_documented_example_corpus_is_executable(tmp_path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "output"

    # Pinned to 1.0 (deprecation-window coverage): the corpus assertions document the
    # v1 statement artifacts; the task contract's corpus lives in the golden fixtures.
    assert main([
        "parse",
        "--contract-version",
        "1.0",
        "--input-dir",
        str(project_root / "examples" / "tasks"),
        "--schema",
        str(project_root / "examples" / "metadata" / "schema_info.json"),
        "--schema-fallback",
        str(
            project_root
            / "examples"
            / "metadata"
            / "subscription_account_snapshot"
            / "source_tables"
        ),
        "--target-ddl-metadata",
        str(project_root / "examples" / "metadata" / "target_tables"),
        "--out",
        str(output),
    ]) == 0

    lineage_files = sorted(output.rglob("lineage.json"))
    assert len(lineage_files) == 6
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["parse_status"] == "ok"
        for path in lineage_files
    )
    complex_example = json.loads(
        (
            output
            / "subscription"
            / "subscription_account_snapshot"
            / "lineage.json"
        ).read_text(encoding="utf-8")
    )
    assert len(complex_example["source_tables"]) == 19
    assert len(complex_example["end_to_end_lineage"]) == 112
    assert complex_example["target_field_binding"]["status"] == "applied"
    assert complex_example["diagnostics"]["lineage_fact_gap_count"] == 0


def test_rich_metadata_uses_ddl_order_for_source_and_target(tmp_path) -> None:
    metadata_path = tmp_path / "mart.demo_metadata.json"
    metadata_path.write_text(
        json.dumps({
            "table_name": "mart.demo",
            "schema": [
                {"columnName": "amount", "columnIndex": 1},
                {"columnName": "customer_id", "columnIndex": 0},
            ],
            "ddl": "CREATE TABLE mart.demo (customer_id BIGINT, amount DECIMAL(18,2))",
        }),
        encoding="utf-8",
    )

    source_schema = scope_lineage.load_schema(metadata_path)
    source_schema_from_directory = scope_lineage.load_schema(tmp_path)
    metadata = scope_lineage.load_target_table_metadata(metadata_path)
    table = metadata["mart.demo"]

    assert source_schema["mart.demo"] == ["customer_id", "amount"]
    assert source_schema_from_directory == source_schema
    assert table.usable
    assert table.structure_source == "ddl"
    assert [(column.ordinal, column.name) for column in table.columns] == [
        (0, "customer_id"),
        (1, "amount"),
    ]


def test_rich_source_schema_uses_column_index_without_ddl(tmp_path) -> None:
    metadata_path = tmp_path / "ods.demo_metadata.json"
    metadata_path.write_text(
        json.dumps({
            "table_name": "ods.demo",
            "schema": [
                {"columnName": "amount", "columnIndex": 1},
                {"columnName": "customer_id", "columnIndex": 0},
            ],
        }),
        encoding="utf-8",
    )

    schema = scope_lineage.load_schema(metadata_path)

    assert schema["ods.demo"] == ["customer_id", "amount"]


def test_rich_source_schema_rejects_non_contiguous_column_index(tmp_path) -> None:
    metadata_path = tmp_path / "ods.invalid_metadata.json"
    metadata_path.write_text(
        json.dumps({
            "table_name": "ods.invalid",
            "schema": [
                {"columnName": "customer_id", "columnIndex": 0},
                {"columnName": "amount", "columnIndex": 2},
            ],
        }),
        encoding="utf-8",
    )

    with pytest.raises(
        scope_lineage.MetadataFileError,
        match="schema_column_indices_not_contiguous",
    ):
        scope_lineage.load_schema(metadata_path)


def test_schema_sources_keep_rich_authority_and_fill_missing_tables(
    tmp_path,
) -> None:
    rich = tmp_path / "rich.json"
    rich.write_text(
        json.dumps({
            "table_name": "ods.primary",
            "schema": [
                {"columnName": "id", "columnIndex": 0},
                {"columnName": "amount", "columnIndex": 1},
            ],
            "ddl": "CREATE TABLE ods.primary (id BIGINT, amount DECIMAL(18,2))",
        }),
        encoding="utf-8",
    )
    fallback = tmp_path / "fallback.csv"
    fallback.write_text(
        "table_name,column_name\n"
        "ods.primary,id\n"
        "ods.primary,different_column\n"
        "ods.fallback,id\n",
        encoding="utf-8",
    )

    schema = scope_lineage.load_schema_sources([rich, fallback])

    assert schema["ods.primary"] == ["id", "amount"]
    assert schema["ods.fallback"] == ["id"]
    assert schema.metadata_source_count == 2
    assert schema.metadata_conflicts == [{
        "table": "ods.primary",
        "authoritative_columns": ["id", "amount"],
        "fallback_columns": ["id", "different_column"],
        "fallback_source_index": 1,
        "resolution": "kept_authoritative",
    }]


def test_cli_schema_fallback_expands_table_missing_from_primary(tmp_path) -> None:
    primary = tmp_path / "primary.json"
    primary.write_text(
        json.dumps({"ods.other": ["x"]}),
        encoding="utf-8",
    )
    fallback = tmp_path / "fallback.csv"
    fallback.write_text(
        "table_name,column_name\nods.source,id\nods.source,amount\n",
        encoding="utf-8",
    )
    sql = tmp_path / "star.sql"
    sql.write_text(
        "INSERT INTO mart.t SELECT * FROM ods.source",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--sql-file",
        str(sql),
        "--schema",
        str(primary),
        "--schema-fallback",
        str(fallback),
        "--out",
        str(output),
    ]) == 0
    lineage = json.loads(
        (output / "star" / "lineage.json").read_text(encoding="utf-8")
    )
    assert [item["column"] for item in lineage["end_to_end_lineage"]] == [
        "id",
        "amount",
    ]


def test_select_star_example_expands_and_uses_target_binding(tmp_path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "output"

    assert main([
        "parse",
        "--contract-version",
        "1.0",
        "--sql-file",
        str(project_root / "examples" / "sql" / "select_star_with_schema.sql"),
        "--schema",
        str(project_root / "examples" / "metadata" / "schema_info.json"),
        "--target-ddl-metadata",
        str(project_root / "examples" / "metadata" / "target_tables"),
        "--out",
        str(output),
    ]) == 0

    lineage = json.loads(
        (output / "select_star_with_schema" / "lineage.json").read_text(
            encoding="utf-8"
        )
    )
    assert lineage["target_field_binding"]["status"] == "applied"
    assert [item["column"] for item in lineage["end_to_end_lineage"]] == [
        "customer_id",
        "customer_name",
        "country_code",
        "registered_at",
        "dt",
    ]
    assert lineage["diagnostics"]["warning_count"] == 0


def test_core_cli_help_exposes_only_parse(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "parse" in help_text
    for upper_command in ("insight", "governance", "refactor-candidates"):
        assert upper_command not in help_text


def test_core_parse_help_explains_catalog_configuration(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["parse", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--catalog-prefixes" in help_text
    assert "SCOPE_LINEAGE_CATALOG_PREFIXES" in help_text
    assert "by default catalogs are preserved" in " ".join(help_text.split())


def test_public_qualified_field_extractor() -> None:
    assert scope_lineage.extract_qualified_field_refs(
        "a.id + `b`.`amount` + named_struct('x', c.value).x"
    ) == [("b", "amount"), ("a", "id"), ("c", "value")]


def test_compact_v1_writer_preserves_document_semantics(tmp_path) -> None:
    result = scope_lineage.parse_scope_lineage(
        "INSERT INTO mart.t SELECT id FROM ods.source",
        task_name="compact_v1",
        schema={"ods.source": ["id"]},
    )
    pretty = scope_lineage.write_lineage(result, tmp_path / "pretty")
    compact = scope_lineage.write_lineage(
        result,
        tmp_path / "compact",
        compact=True,
    )

    for name in ("lineage.json", "diagnostics.json"):
        assert json.loads((pretty / name).read_text(encoding="utf-8")) == json.loads(
            (compact / name).read_text(encoding="utf-8")
        )
        assert (compact / name).stat().st_size < (pretty / name).stat().st_size


def test_cli_v2_writes_one_ordered_task_artifact_with_dependencies(
    tmp_path,
    capsys,
) -> None:
    task_path = tmp_path / "state_task.json"
    task_path.write_text(
        json.dumps({
            "meta": {
                "task_name": "state_task",
                "upstream_tasks": [
                    {"task_id": "upstream", "task_name": "upstream_task"}
                ],
                "downstream_tasks": [],
                "sql": (
                    "TRUNCATE TABLE mart.t; "
                    "INSERT INTO mart.t SELECT id FROM ods.source"
                ),
            }
        }),
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--task-file",
        str(task_path),
        "--contract-version",
        "2.0",
        "--compact-json",
        "--schema",
        str(Path(__file__).parents[2] / "examples" / "metadata" / "schema_info.csv"),
        "--out",
        str(output),
    ]) == 0

    assert sorted(path.name for path in output.iterdir()) == ["state_task"]
    lineage = json.loads(
        (output / "state_task" / "lineage.json").read_text(encoding="utf-8")
    )
    assert lineage["schema_version"] == "2.0"
    assert [item["stmt_kind"] for item in lineage["statement_sequence"]] == [
        "TRUNCATETABLE",
        "INSERT",
    ]
    assert lineage["task_dependencies"]["source_summary"]["upstream_count"] == 1
    assert "using contract 2.0" in capsys.readouterr().out


def test_cli_v2_models_standalone_delete_instead_of_rejecting_input(
    tmp_path,
) -> None:
    sql_path = tmp_path / "delete.sql"
    sql_path.write_text(
        "DELETE FROM mart.t WHERE id IN (SELECT id FROM ods.source)",
        encoding="utf-8",
    )
    output = tmp_path / "output"

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--contract-version",
        "2.0",
        "--schema",
        str(Path(__file__).parents[2] / "examples" / "metadata" / "schema_info.csv"),
        "--out",
        str(output),
    ]) == 0
    lineage = json.loads(
        (output / "delete" / "lineage.json").read_text(encoding="utf-8")
    )
    assert lineage["statement_sequence"][0]["model_status"] == "modeled"
