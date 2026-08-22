"""A dynamic-partition INSERT OVERWRITE replaces the whole table under Spark's default.

`INSERT OVERWRITE TABLE t PARTITION(dt)` names the partition column without a value. What
that does depends on `spark.sql.sources.partitionOverwriteMode`, whose default is STATIC: all
existing partitions are dropped before the new data lands. Only when the mode is explicitly
DYNAMIC do untouched partitions survive.

The write effect was chosen from `target_partition_mode != "none"`, so a valued spec
(`PARTITION(dt='20260101')`) and a dynamic one were treated alike -- both kept the target's
previous `value_sources`, and every column of the target came back carrying a
`prior_table_state` edge from a state the overwrite had in fact destroyed (PARTOVR-001).

That edge is not merely redundant. It asserts that the new value may be the old one, which is
what a consumer folding state-evolution edges relies on to decide a column was left alone.
A valued spec keeps the edge because only the named partitions are replaced; the rest of the
table genuinely survives, which is why the two shapes must not be collapsed.
"""

from __future__ import annotations

import pytest

from scope_lineage.scope.task_lineage import parse_task_lineage

SCHEMA = {"ods.a": ["id", "v", "dt"], "mart.t": ["id", "v", "dt"]}


def _prior_state_columns(sql: str, table: str = "mart.t") -> set[str]:
    """Target columns that claim a value carried over from the table's previous state."""
    result = parse_task_lineage(sql, task_name="t", schema=SCHEMA)
    return {
        str(item.get("column"))
        for item in result.end_to_end_lineage
        if item.get("table") == table
        for source in item.get("value_sources") or []
        if source.get("source_kind") == "prior_table_state"
    }


def _seed(sql: str) -> str:
    """Give the target a previous state, so prior_table_state edges are possible at all."""
    return "INSERT INTO mart.t SELECT id, v, dt FROM ods.a;\n" + sql


def test_dynamic_partition_overwrite_drops_the_previous_state():
    sql = _seed("INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a")

    assert _prior_state_columns(sql) == set()


def test_valued_partition_overwrite_keeps_the_previous_state():
    """Only the named partition is replaced; the rest of the table survives."""
    sql = _seed("INSERT OVERWRITE TABLE mart.t PARTITION(dt='20260101') SELECT id, v FROM ods.a")

    assert _prior_state_columns(sql)


def test_mixed_partition_overwrite_keeps_the_previous_state():
    """A static prefix bounds the blast radius, so other prefixes survive."""
    schema = {"ods.a": ["id", "v", "dt", "region"], "mart.t": ["id", "v", "dt", "region"]}
    sql = (
        "INSERT INTO mart.t SELECT id, v, dt, region FROM ods.a;\n"
        "INSERT OVERWRITE TABLE mart.t PARTITION(region='mx', dt) SELECT id, v, dt FROM ods.a"
    )
    result = parse_task_lineage(sql, task_name="t", schema=schema)
    prior = {
        str(item.get("column"))
        for item in result.end_to_end_lineage
        if item.get("table") == "mart.t"
        for source in item.get("value_sources") or []
        if source.get("source_kind") == "prior_table_state"
    }

    assert prior


def test_an_explicit_dynamic_mode_keeps_the_previous_state():
    """With the mode set to DYNAMIC, untouched partitions really do survive."""
    sql = _seed(
        "set spark.sql.sources.partitionOverwriteMode=dynamic;\n"
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a"
    )

    assert _prior_state_columns(sql)


@pytest.mark.parametrize("setting", ["static", "STATIC"])
def test_an_explicit_static_mode_matches_the_default(setting):
    sql = _seed(
        f"set spark.sql.sources.partitionOverwriteMode={setting};\n"
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a"
    )

    assert _prior_state_columns(sql) == set()


def test_the_setting_only_applies_to_statements_after_it():
    """A mode set after the write cannot change what that write did."""
    sql = _seed(
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a;\n"
        "set spark.sql.sources.partitionOverwriteMode=dynamic"
    )

    assert _prior_state_columns(sql) == set()


def test_an_unpartitioned_overwrite_is_unchanged():
    """Already a full replace; this fix must not touch it."""
    sql = _seed("INSERT OVERWRITE TABLE mart.t SELECT id, v, dt FROM ods.a")

    assert _prior_state_columns(sql) == set()


def test_an_append_is_unchanged():
    """INSERT INTO keeps the previous state and must stay that way."""
    sql = _seed("INSERT INTO mart.t SELECT id, v, dt FROM ods.a")

    assert _prior_state_columns(sql)


def test_a_merge_target_is_unchanged():
    """MERGE genuinely retains unmatched rows; this fix must not reach it."""
    sql = (
        "INSERT INTO mart.t SELECT id, v, dt FROM ods.a;\n"
        "MERGE INTO mart.t t USING (SELECT id, v, dt FROM ods.a) s ON t.id = s.id\n"
        "WHEN NOT MATCHED THEN INSERT (id, v, dt) VALUES (s.id, s.v, s.dt)"
    )

    assert _prior_state_columns(sql)


def test_a_column_the_write_does_not_supply_matches_an_unpartitioned_overwrite():
    """A full replace says nothing about a column it never writes, however it is spelled.

    The target column `extra` is not in the SELECT. After a full replace its old values are
    gone, so no row is the honest answer -- and that is already what an unpartitioned
    INSERT OVERWRITE produces. A dynamic-partition overwrite is the same kind of write and
    must not disagree with it, while a valued spec keeps the row because those partitions
    really do survive.
    """
    schema = {"ods.a": ["id", "v", "dt"], "mart.t": ["id", "v", "dt", "extra"]}
    seed = "INSERT INTO mart.t SELECT id, v, dt, 'x' FROM ods.a;\n"

    def extra_sources(write: str):
        result = parse_task_lineage(seed + write, task_name="t", schema=schema)
        row = next(
            (i for i in result.end_to_end_lineage
             if i.get("table") == "mart.t" and i.get("column") == "extra"),
            None,
        )
        return None if row is None else sorted(
            {s.get("source_kind") for s in row.get("value_sources") or []}
        )

    unpartitioned = extra_sources("INSERT OVERWRITE TABLE mart.t SELECT id, v, dt FROM ods.a")
    dynamic = extra_sources("INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a")
    valued = extra_sources(
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt='20260101') SELECT id, v FROM ods.a"
    )

    assert unpartitioned is None
    assert dynamic == unpartitioned
    assert valued == ["prior_table_state"]


# --- the setting is an assumption unless the script states it -----------------------

def _rowset_effects(sql: str, schema=None, target_metadata=None) -> list[dict]:
    result = parse_task_lineage(
        sql, task_name="t", schema=schema if schema is not None else SCHEMA,
        target_metadata=target_metadata,
    )
    return [
        statement["effect"]["rowset_effect"]
        for statement in result.statements or []
        if statement.get("effect")
    ]


def test_a_dynamic_spec_without_a_set_says_the_mode_was_assumed():
    """The whole-table REPLACE hinges on Spark's default, not on anything in the script.

    It decides whether the target's own prior state survives, so a consumer folding
    state-evolution edges is entitled to know the answer came from an assumption.
    """
    effects = _rowset_effects(
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a"
    )
    assert effects[0]["operation"] == "REPLACE"
    assert effects[0]["partition_overwrite_mode_source"] == "assumed_default"


def test_an_observed_set_is_recorded_as_observed():
    effects = _rowset_effects(
        "SET spark.sql.sources.partitionOverwriteMode=static;\n"
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a"
    )
    assert effects[0]["operation"] == "REPLACE"
    assert effects[0]["partition_overwrite_mode_source"] == "observed"


def test_an_unqualified_overwrite_of_a_partitioned_table_is_also_assumed():
    """No PARTITION clause on a partitioned target is still a dynamic-partition insert.

    Without this the absent field would be a positive claim of independence, and here
    that claim is false.
    """
    from scope_lineage.metadata.target_table_metadata import (
        TargetColumnMetadata, TargetMetadataMap, TargetTableMetadata,
    )
    metadata = TargetMetadataMap()
    metadata["mart.t"] = TargetTableMetadata(
        table_name="t", full_table_name="mart.t",
        columns=[TargetColumnMetadata(name=n, data_type="string", ordinal=i,
                                      is_partition=(n == "dt"), comment="")
                 for i, n in enumerate(["id", "v", "dt"])],
        partition_columns=["dt"], ddl="", source_file="x", validation_issues=[],
        query_time=None, ddl_update_time=None, data_source="test", structure_source="ddl",
    )
    effects = _rowset_effects(
        "INSERT OVERWRITE TABLE mart.t SELECT id, v, dt FROM ods.a",
        target_metadata=metadata,
    )
    assert effects[0]["partition_overwrite_mode_source"] == "assumed_default"


# --- guards: must pass before AND after --------------------------------------------

def test_a_partitioned_ctas_is_not_marked():
    """The guard that bites: it satisfies REPLACE and mode=dynamic, and only the
    statement kind tells it apart. A plain CTAS would pass this vacuously."""
    effects = _rowset_effects(
        "CREATE TABLE mart.c PARTITIONED BY (dt) AS SELECT id, v, dt FROM ods.a"
    )
    assert effects[0]["operation"] == "REPLACE"
    assert "partition_overwrite_mode_source" not in effects[0]


def test_an_unqualified_overwrite_of_an_unpartitioned_table_is_not_marked():
    effects = _rowset_effects(
        "INSERT OVERWRITE TABLE mart.t SELECT id, v, dt FROM ods.a"
    )
    assert "partition_overwrite_mode_source" not in effects[0]


def test_a_valued_spec_is_not_marked():
    effects = _rowset_effects(
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt='20260101') SELECT id, v FROM ods.a"
    )
    assert effects[0]["operation"] == "REPLACE_PARTITION"
    assert "partition_overwrite_mode_source" not in effects[0]


def test_a_plain_insert_is_not_marked():
    effects = _rowset_effects("INSERT INTO mart.t SELECT id, v, dt FROM ods.a")
    assert "partition_overwrite_mode_source" not in effects[0]


def test_the_absent_shape_carries_no_extra_keys():
    effects = _rowset_effects("INSERT INTO mart.t SELECT id, v, dt FROM ods.a")
    assert "partition_overwrite_mode_source" not in effects[0]
    assert set(effects[0]) <= {"operation", "membership_sources", "row_filter_sources"}


# --- a deployment can declare the mode its cluster runs with ------------------------

def _declared(sql: str, mode: str | None, target_metadata=None) -> list[dict]:
    result = parse_task_lineage(
        sql, task_name="t", schema=SCHEMA, target_metadata=target_metadata,
        partition_overwrite_mode=mode,
    )
    return [s["effect"]["rowset_effect"] for s in result.statements or [] if s.get("effect")]


DYNAMIC_SPEC = "INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT id, v, dt FROM ods.a"


def test_a_declared_dynamic_mode_bounds_the_overwrite():
    effects = _declared(DYNAMIC_SPEC, "dynamic")
    assert effects[0]["operation"] == "REPLACE_PARTITION"


def test_the_declared_value_is_recorded_and_the_source_stays_assumed():
    """The script still said nothing, so the source is not `observed`. What the
    deployment declared is a separate fact, and it carries the value rather than a
    flag: without it a `none`-spec statement's artifact is identical either way."""
    effects = _declared(DYNAMIC_SPEC, "dynamic")
    assert effects[0]["partition_overwrite_mode_source"] == "assumed_default"
    assert effects[0]["partition_overwrite_mode_declared"] == "dynamic"


def test_a_declared_static_mode_is_also_recorded():
    effects = _declared(DYNAMIC_SPEC, "static")
    assert effects[0]["operation"] == "REPLACE"
    assert effects[0]["partition_overwrite_mode_declared"] == "static"


def test_a_script_setting_overrides_the_declared_value():
    """The guard that bites the knob: a deployment value is present, and must lose."""
    effects = _declared(
        "SET spark.sql.sources.partitionOverwriteMode=static;\n" + DYNAMIC_SPEC,
        "dynamic",
    )
    assert effects[0]["operation"] == "REPLACE"
    assert effects[0]["partition_overwrite_mode_source"] == "observed"
    assert "partition_overwrite_mode_declared" not in effects[0]


def test_an_unrecognised_setting_value_does_not_override_the_declaration():
    """`nonstrict` is the neighbouring Hive key's value and a predictable mix-up. It
    used to read as "observed static", which under a declared dynamic would discard the
    declaration and stamp the wrong answer with the contract's most authoritative label."""
    effects = _declared(
        "SET spark.sql.sources.partitionOverwriteMode=nonstrict;\n" + DYNAMIC_SPEC,
        "dynamic",
    )
    assert effects[0]["operation"] == "REPLACE_PARTITION"
    assert effects[0]["partition_overwrite_mode_source"] == "assumed_default"


def test_an_unpartitioned_target_is_not_bounded_by_a_declaration():
    """The guard that bites the `none`-class correction: an implementation that honours
    the declared mode without checking the target has partition columns reports a
    whole-table overwrite as partition-scoped, and passes every other test."""
    from scope_lineage.metadata.target_table_metadata import (
        TargetColumnMetadata, TargetMetadataMap, TargetTableMetadata,
    )
    metadata = TargetMetadataMap()
    metadata["mart.t"] = TargetTableMetadata(
        table_name="t", full_table_name="mart.t",
        columns=[TargetColumnMetadata(name=n, data_type="string", ordinal=i,
                                      is_partition=False, comment="")
                 for i, n in enumerate(["id", "v", "dt"])],
        partition_columns=[], ddl="", source_file="x", validation_issues=[],
        query_time=None, ddl_update_time=None, data_source="test",
        structure_source="ddl",
    )
    effects = _declared(
        "INSERT OVERWRITE TABLE mart.t SELECT id, v, dt FROM ods.a",
        "dynamic", target_metadata=metadata,
    )
    assert effects[0]["operation"] == "REPLACE"


def test_no_declaration_is_unchanged():
    effects = _declared(DYNAMIC_SPEC, None)
    assert effects[0]["operation"] == "REPLACE"
    assert effects[0]["partition_overwrite_mode_source"] == "assumed_default"
    assert "partition_overwrite_mode_declared" not in effects[0]


def test_a_valued_spec_ignores_the_declaration():
    effects = _declared(
        "INSERT OVERWRITE TABLE mart.t PARTITION(dt='20260101') SELECT id, v FROM ods.a",
        "dynamic",
    )
    assert effects[0]["operation"] == "REPLACE_PARTITION"
    assert "partition_overwrite_mode_declared" not in effects[0]


def test_requesting_the_removed_contract_1_0_fails_loudly(tmp_path, capsys):
    """The flag outlives the removed contract by one release so a 1.0 request gets a
    clear choices error instead of an unknown-argument error."""
    import pytest

    from scope_lineage.cli import main

    sql = tmp_path / "t.sql"
    sql.write_text("INSERT INTO mart.t SELECT 1")
    with pytest.raises(SystemExit):
        main(["parse", "--contract-version", "1.0", "--sql-file", str(sql),
              "--out", str(tmp_path / "o")])
    assert "invalid choice: '1.0'" in capsys.readouterr().err


def test_the_cli_rejects_an_unusable_value_once(tmp_path, capsys):
    """`nonstrict` belongs to the neighbouring Hive key. One error, before any input."""
    import pytest

    from scope_lineage.cli import main

    sql = tmp_path / "t.sql"
    sql.write_text("INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT 1, 2, 3")
    with pytest.raises(SystemExit):
        main(["parse", "--sql-file", str(sql), "--out", str(tmp_path / "o"),
              "--contract-version", "2.0", "--partition-overwrite-mode", "nonstrict"])
    assert "must be static or dynamic" in capsys.readouterr().err


def test_the_cli_accepts_upper_case(tmp_path):
    """spark-defaults.conf spells it DYNAMIC."""
    from scope_lineage.cli import main

    sql = tmp_path / "t.sql"
    sql.write_text("INSERT OVERWRITE TABLE mart.t PARTITION(dt) SELECT 1 AS id, 2 AS v, 3 AS dt")
    assert main(["parse", "--sql-file", str(sql), "--out", str(tmp_path / "o"),
                 "--contract-version", "2.0",
                 "--partition-overwrite-mode", "DYNAMIC"]) == 0
