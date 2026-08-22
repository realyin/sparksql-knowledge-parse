"""Task-level statement inventory and opt-in CLI quality gates."""

from __future__ import annotations

import json
from pathlib import Path

from scope_lineage.cli import main
from scope_lineage.scope.scope_builder import _collect_insert_trees


def _mixed_script(path: Path) -> None:
    path.write_text(
        """
        SET spark.sql.shuffle.partitions = 8;
        DELETE FROM mart.target WHERE expired = true;
        INSERT INTO mart.target SELECT id FROM ods.first_source;
        INSERT INTO mart.audit SELECT id FROM ods.second_source;
        """,
        encoding="utf-8",
    )


def test_skipped_statements_have_stable_task_level_identity_and_category() -> None:
    writes, skipped, _regex_flags, _write_indices = _collect_insert_trees(
        """
        SET spark.sql.shuffle.partitions = 8;
        DELETE FROM mart.target WHERE expired = true;
        INSERT INTO mart.target SELECT id FROM ods.source;
        """
    )

    assert len(writes) == 1
    assert [
        {
            key: item[key]
            for key in (
                "statement_id",
                "statement_index",
                "statement_kind",
                "category",
                "model_status",
            )
        }
        for item in skipped
    ] == [
        {
            "statement_id": "stmt:001",
            "statement_index": 0,
            "statement_kind": "SET",
            "category": "control_statement",
            "model_status": "ignored",
        },
        {
            "statement_id": "stmt:002",
            "statement_index": 1,
            "statement_kind": "DELETE",
            "category": "row_mutation",
            "model_status": "unsupported",
        },
    ]


def test_cli_models_the_mixed_scripts_delete_and_gates_on_the_root_gap(
    tmp_path: Path,
    capsys,
) -> None:
    """The task contract models DELETE, so the retired v1 unsupported-mutation count is
    zero here by design; what survives of the script's uncertainty is a root-impact gap,
    and that is what the strict policy gates on."""
    sql_path = tmp_path / "mixed.sql"
    _mixed_script(sql_path)

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--out",
        str(tmp_path / "permissive"),
    ]) == 0
    summary = capsys.readouterr().out
    assert "unsupported_mutations=0" in summary
    assert "root_gap_results=1" in summary

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--quality-policy",
        "strict",
        "--allow-partial",
        "--out",
        str(tmp_path / "strict"),
    ]) == 1


def test_explicit_root_gap_flag_gates_without_the_strict_policy(
    tmp_path: Path,
) -> None:
    sql_path = tmp_path / "mixed.sql"
    _mixed_script(sql_path)

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--fail-on-root-gap",
        "--allow-partial",
        "--out",
        str(tmp_path / "output"),
    ]) == 1

def test_strict_policy_rejects_root_fact_gaps_and_recovered_syntax(
    tmp_path: Path,
) -> None:
    ambiguous = tmp_path / "ambiguous.sql"
    ambiguous.write_text(
        "INSERT INTO mart.target SELECT id FROM ods.first_source a "
        "JOIN ods.second_source b ON a.id = b.id",
        encoding="utf-8",
    )
    schema = tmp_path / "schema.csv"
    schema.write_text(
        "table_name,column_name\n"
        "ods.first_source,id\n"
        "ods.second_source,id\n",
        encoding="utf-8",
    )
    recovered = tmp_path / "recovered.sql"
    recovered.write_text(
        "INSERT INTO mart.target SELECT id FROM ods.source WHERE WHERE id > 0",
        encoding="utf-8",
    )

    assert main([
        "parse",
        "--sql-file",
        str(ambiguous),
        "--schema",
        str(schema),
        "--quality-policy",
        "strict",
        "--out",
        str(tmp_path / "ambiguous-output"),
    ]) == 1
    assert main([
        "parse",
        "--sql-file",
        str(ambiguous),
        "--schema",
        str(schema),
        "--contract-version",
        "2.0",
        "--quality-policy",
        "strict",
        "--out",
        str(tmp_path / "ambiguous-v2-output"),
    ]) == 1
    assert main([
        "parse",
        "--sql-file",
        str(recovered),
        "--quality-policy",
        "strict",
        "--out",
        str(tmp_path / "recovered-output"),
    ]) == 1


def test_binding_fallback_gate_has_an_independent_exit_policy(tmp_path: Path) -> None:
    sql_path = tmp_path / "binding.sql"
    sql_path.write_text(
        "INSERT INTO mart.target SELECT id FROM ods.source",
        encoding="utf-8",
    )
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "target.json").write_text(
        json.dumps({
            "table_name": "mart.target",
            "schema": [
                {"columnName": "id", "columnIndex": 0, "isPartition": 0},
                {"columnName": "amount", "columnIndex": 1, "isPartition": 0},
            ],
        }),
        encoding="utf-8",
    )

    assert main([
        "parse",
        "--sql-file",
        str(sql_path),
        "--target-ddl-metadata",
        str(metadata_dir),
        "--fail-on-binding-fallback",
        "--out",
        str(tmp_path / "output"),
    ]) == 1


MERGE_CTE_SQL = """
WITH staged AS (
  SELECT e.id, e.event_type, a.account_key
  FROM ods.events e
  LEFT JOIN dim.accounts a ON e.account_id = a.account_id
)
MERGE INTO mart.event_target target
USING (SELECT id, event_type, account_key FROM staged) source
ON target.id = source.id
WHEN MATCHED THEN UPDATE SET
  target.id = source.id,
  target.event_type = source.event_type,
  target.account_key = source.account_key
WHEN NOT MATCHED THEN INSERT *
"""

MERGE_CTE_SCHEMA_CSV = (
    "table_name,column_name\n"
    "ods.events,id\n"
    "ods.events,event_type\n"
    "ods.events,account_id\n"
    "dim.accounts,account_id\n"
    "dim.accounts,account_key\n"
    "mart.event_target,id\n"
    "mart.event_target,event_type\n"
    "mart.event_target,account_key\n"
)


def _merge_inputs(tmp_path: Path, sql: str) -> tuple[Path, Path]:
    sql_file = tmp_path / "merge.sql"
    sql_file.write_text(sql, encoding="utf-8")
    schema = tmp_path / "schema.csv"
    schema.write_text(MERGE_CTE_SCHEMA_CSV, encoding="utf-8")
    return sql_file, schema


def test_strict_policy_accepts_a_fully_traced_merge_over_a_cte(tmp_path: Path) -> None:
    sql_file, schema = _merge_inputs(tmp_path, MERGE_CTE_SQL)

    for contract_args, out in (
        ([], "v1"),
        (["--contract-version", "2.0"], "v2"),
    ):
        assert main([
            "parse",
            "--sql-file",
            str(sql_file),
            "--schema",
            str(schema),
            *contract_args,
            "--quality-policy",
            "strict",
            "--out",
            str(tmp_path / out),
        ]) == 0


def test_strict_policy_rejects_a_merge_condition_that_cannot_be_traced(
    tmp_path: Path,
) -> None:
    sql_file, schema = _merge_inputs(
        tmp_path,
        """
        MERGE INTO mart.event_target target
        USING (SELECT id FROM ods.events) source
        ON target.id = source.missing_col
        WHEN MATCHED THEN UPDATE SET target.event_type = source.id
        """,
    )

    assert main([
        "parse",
        "--sql-file",
        str(sql_file),
        "--schema",
        str(schema),
        "--contract-version",
        "2.0",
        "--quality-policy",
        "strict",
        "--out",
        str(tmp_path / "untraceable"),
    ]) == 1
