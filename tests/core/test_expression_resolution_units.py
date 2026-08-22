"""Unit coverage for the three expression-resolution passes in scope_builder.

Migrated from the integration repository, which held the only tests for these functions while
Core had none of its own. Core owns the behaviour, so Core owns the tests -- and the boundary
that lets the integration repo consume Core as a released wheel only works if Core's internals
are not something a consumer has to import to keep tested (see the facade ledger over there).

Fixtures are synthetic: the real table, column and catalog names the originals carried have been
replaced, and the assertions were re-verified against the renamed inputs rather than assumed to
survive the rename.

These are deliberately unit tests. Each pass is reached directly, because what they encode is how
a *partially* resolved expression is completed -- states that are hard to arrange end to end and
easy to lose silently in an integration assertion.
"""

from __future__ import annotations

# Imported from the modules that define them, not from scope_builder's re-exports: those
# re-exports exist only for the integration repository and are slated for removal once its
# ledger is clear, and a Core test must not be what keeps them alive.
from scope_lineage.scope.expression_expansion import _resolve_expression_resolution_from_output_sources
from scope_lineage.scope.column_expression_resolution import (
    _expression_resolution_for_scope_column,
)
from scope_lineage.scope.passthrough_resolution import (
    _propagate_passthrough_expression_resolution,
)
from scope_lineage.scope.scope_types import (
    ScopeColumn,
    ScopeData,
    ScopeLineageResult,
    ScopeOutputField,
    SourceRef,
)


def test_unqualified_case_expression_resolves_from_column_sources():
    scope_data = ScopeData(kind="root")
    column = ScopeColumn(
        name="status_fa",
        transform="CONDITIONAL",
        expression="CASE WHEN status = 0 THEN '审批中' ELSE '其他' END",
        sources=[SourceRef(scope="ods.coupon_log", column="status")],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["resolution_type"] == "unqualified_expression_from_sources"
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "ods.coupon_log", "field": "status"}
    ]
    assert output["expression_resolution"]["missing_reasons"] == []


def test_expression_resolution_adds_physical_field_from_resolved_qualifier_without_refs():
    scope_data = ScopeData(
        kind="root",
        input_source_refs=[
            {
                "source_id": "dm_opr.dmd_opr_lia_call_info_iceberg_df",
                "source_type": "physical_table",
                "alias": "dmd_opr_lia_call_info_iceberg_df",
                "physical_source_id": "dm_opr.dmd_opr_lia_call_info_iceberg_df",
                "physical_source_ids": ["dm_opr.dmd_opr_lia_call_info_iceberg_df"],
            }
        ],
    )
    column = ScopeColumn(
        name="node",
        transform="EXPRESSION",
        expression="REVERSE(SPLIT(`dmd_opr_lia_call_info_iceberg_df`.`ivr_node_desc`, '-'))[0]",
        sources=[],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "dm_opr.dmd_opr_lia_call_info_iceberg_df", "field": "ivr_node_desc"}
    ]
    assert (
        output["expanded_expression"]
        == "REVERSE(SPLIT(`dm_opr.dmd_opr_lia_call_info_iceberg_df`.`ivr_node_desc`, '-'))[0]"
    )


def test_expression_resolution_ignores_comment_when_resolving_qualified_field_without_refs():
    scope_data = ScopeData(
        kind="root",
        input_source_refs=[
            {
                "source_id": "ods.exempt_apply",
                "source_type": "physical_table",
                "alias": "a",
                "physical_source_id": "ods.exempt_apply",
                "physical_source_ids": ["ods.exempt_apply"],
            }
        ],
    )
    column = ScopeColumn(
        name="apply_amt",
        transform="DIRECT",
        expression="`a`.`apply_amt` /* 申请豁免金额 */",
        sources=[],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "ods.exempt_apply", "field": "apply_amt"}
    ]
    assert output["expanded_expression"] == "`ods.exempt_apply`.`apply_amt` /* 申请豁免金额 */"


def test_expression_resolution_ignores_field_refs_inside_comments():
    scope_data = ScopeData(
        kind="root",
        input_source_refs=[
            {
                "source_id": "ods.score_a",
                "source_type": "physical_table",
                "alias": "a",
                "physical_source_id": "ods.score_a",
                "physical_source_ids": ["ods.score_a"],
            },
            {
                "source_id": "ods.score_b",
                "source_type": "physical_table",
                "alias": "b",
                "physical_source_id": "ods.score_b",
                "physical_source_ids": ["ods.score_b"],
            },
        ],
    )
    column = ScopeColumn(
        name="score",
        transform="EXPRESSION",
        expression="COALESCE(`a`.`score_1`, `b`.`score_2`) /* c.score_3,b.score_4 */",
        sources=[
            SourceRef(scope="ods.score_a", column="score_1"),
            SourceRef(scope="ods.score_b", column="score_2"),
        ],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "ods.score_a", "field": "score_1"},
        {"table": "ods.score_b", "field": "score_2"},
    ]
    assert output["expression_resolution"]["missing_reasons"] == []


def test_expression_resolution_ignores_catalog_qualified_udf_as_alias():
    scope_data = ScopeData(
        kind="root",
        input_source_refs=[
            {
                "source_id": "dwd.chat_message_di",
                "source_type": "physical_table",
                "alias": "chat_message_di",
                "physical_source_id": "dwd.chat_message_di",
                "physical_source_ids": ["dwd.chat_message_di"],
            }
        ],
    )
    column = ScopeColumn(
        name="account_no_code",
        transform="EXPRESSION",
        expression=(
            "`spark_catalog`.`default`.mask_phone("
            "SUBSTRING(`chat_message_di`.`account_no`, "
            "LENGTH(`chat_message_di`.`account_no`) - 9, 10))"
        ),
        sources=[SourceRef(scope="dwd.chat_message_di", column="account_no")],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["missing_reasons"] == []
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "dwd.chat_message_di", "field": "account_no"}
    ]


def test_expression_resolution_ignores_nested_scalar_subquery_aliases():
    scope_data = ScopeData(
        kind="root",
        input_source_refs=[
            {
                "source_id": "report_csc_ana.feiyong_zhixing",
                "source_type": "physical_table",
                "alias": "t",
                "physical_source_id": "report_csc_ana.feiyong_zhixing",
                "physical_source_ids": ["report_csc_ana.feiyong_zhixing"],
            }
        ],
    )
    column = ScopeColumn(
        name="annual_withdraw_count",
        transform="CONDITIONAL",
        expression=(
            "CASE WHEN `t`.`is_integrated` = 'integrated' THEN "
            "(SELECT COUNT(*) FROM `warehouse`.`dwd`.`approval_detail_df` AS `w` "
            "WHERE `w`.`entry_point` = 'loan_intent' AND `w`.`dt` = '20260620') "
            "ELSE NULL END"
        ),
        sources=[SourceRef(scope="report_csc_ana.feiyong_zhixing", column="is_integrated")],
    )

    output = _expression_resolution_for_scope_column(scope_data, column)

    assert output["expression_resolution"]["status"] == "resolved"
    assert output["expression_resolution"]["missing_reasons"] == []
    assert output["expression_resolution"]["physical_source_fields"] == [
        {"table": "report_csc_ana.feiyong_zhixing", "field": "is_integrated"}
    ]


def test_bare_expression_output_propagates_unique_upstream_resolution():
    result = ScopeLineageResult(
        task_id="bare_expression_passthrough",
        target_table="mart.repeat_flags",
        stmt_kind="INSERT",
        scopes={
            "cte:t_tmp3": ScopeData(
                kind="cte",
                outputs=[
                    ScopeOutputField(
                        name="is_repeat_48h",
                        transform="CONDITIONAL",
                        expression="CASE WHEN repeat_cnt > 0 THEN 1 ELSE 0 END",
                        expanded_expression="CASE WHEN `ods.calls`.`repeat_cnt` > 0 THEN 1 ELSE 0 END",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "unqualified_expression_from_sources",
                            "physical_source_fields": [{"table": "ods.calls", "field": "repeat_cnt"}],
                            "generated_sources": [],
                            "source_kind": "physical",
                            "missing_reasons": [],
                        },
                    )
                ],
            ),
            "ROOT": ScopeData(
                kind="root",
                outputs=[
                    ScopeOutputField(
                        name="is_repeat_48h",
                        transform="EXPRESSION",
                        expression="is_repeat_48h",
                        expanded_expression="is_repeat_48h",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[SourceRef(scope="cte:t_tmp3", column="is_repeat_48h")],
                    )
                ],
            ),
        },
    )

    _propagate_passthrough_expression_resolution(result)
    output = result.scopes["ROOT"].outputs[0]

    assert output.expanded_expression == "CASE WHEN `ods.calls`.`repeat_cnt` > 0 THEN 1 ELSE 0 END"
    assert output.expression_resolution["status"] == "resolved"
    assert output.expression_resolution["resolution_type"] == "bare_identifier_from_unique_upstream_output"
    assert output.expression_resolution["physical_source_fields"] == [
        {"table": "ods.calls", "field": "repeat_cnt"}
    ]
    assert output.expression_resolution["missing_reasons"] == []


def test_union_output_propagates_rowset_sources():
    result = ScopeLineageResult(
        task_id="rowset_union_passthrough",
        target_table="mart.indicators",
        stmt_kind="INSERT",
        scopes={
            "union:t:b01": ScopeData(
                kind="union_branch",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="AGGREGATE",
                        expression="COUNT(1)",
                        expanded_expression="COUNT(1)",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "row_count_aggregate",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "rowset",
                            "missing_reasons": [],
                        },
                    )
                ],
            ),
            "union:t:b02": ScopeData(
                kind="union_branch",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="AGGREGATE",
                        expression="COUNT(1)",
                        expanded_expression="COUNT(1)",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "row_count_aggregate",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "rowset",
                            "missing_reasons": [],
                        },
                    )
                ],
            ),
            "union:t": ScopeData(
                kind="union",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="UNION",
                        expression="test_index",
                        expanded_expression="test_index",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[
                            SourceRef(scope="union:t:b01", column="test_index"),
                            SourceRef(scope="union:t:b02", column="test_index"),
                        ],
                    )
                ],
            ),
        },
    )

    _propagate_passthrough_expression_resolution(result)
    output = result.scopes["union:t"].outputs[0]

    assert output.expression_resolution["status"] == "resolved"
    assert output.expression_resolution["source_kind"] == "rowset"
    assert output.expression_resolution["rowset_sources"] == [
        {
            "source_type": "rowset",
            "scope": "union:t:b01",
            "field": "test_index",
            "expression": "COUNT(1)",
        },
        {
            "source_type": "rowset",
            "scope": "union:t:b02",
            "field": "test_index",
            "expression": "COUNT(1)",
        },
    ]


def test_union_output_propagates_mixed_rowset_and_physical_sources():
    result = ScopeLineageResult(
        task_id="mixed_rowset_union_passthrough",
        target_table="mart.indicators",
        stmt_kind="INSERT",
        scopes={
            "subq:t": ScopeData(
                kind="subquery",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="UNION",
                        expression="test_index",
                        expanded_expression="test_index",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "union_branch_alignment",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "rowset_sources": [
                                {
                                    "source_type": "rowset",
                                    "scope": "union:t:b01",
                                    "field": "test_index",
                                    "expression": "COUNT(1)",
                                }
                            ],
                            "source_kind": "rowset",
                            "missing_reasons": [],
                        },
                    )
                ],
            ),
            "union:main:b01": ScopeData(
                kind="union_branch",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="DIRECT",
                        expression="`t`.`test_index`",
                        expanded_expression="`t`.`test_index`",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "qualified_source_projection",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[SourceRef(scope="subq:t", column="test_index")],
                    )
                ],
            ),
            "union:main:b02": ScopeData(
                kind="union_branch",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="DIRECT",
                        expression="`hist`.`test_index`",
                        expanded_expression="`ads.hist`.`test_index`",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "qualified_source_projection",
                            "physical_source_fields": [{"table": "ads.hist", "field": "test_index"}],
                            "generated_sources": [],
                            "source_kind": "physical",
                            "missing_reasons": [],
                        },
                    )
                ],
            ),
            "union:main": ScopeData(
                kind="union",
                outputs=[
                    ScopeOutputField(
                        name="test_index",
                        transform="UNION",
                        expression="test_index",
                        expanded_expression="test_index",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[
                            SourceRef(scope="union:main:b01", column="test_index"),
                            SourceRef(scope="union:main:b02", column="test_index"),
                        ],
                    )
                ],
            ),
        },
    )

    _propagate_passthrough_expression_resolution(result)
    branch_output = result.scopes["union:main:b01"].outputs[0]
    union_output = result.scopes["union:main"].outputs[0]

    assert branch_output.expression_resolution["status"] == "resolved"
    assert branch_output.expression_resolution["source_kind"] == "rowset"
    assert union_output.expression_resolution["status"] == "resolved"
    assert union_output.expression_resolution["source_kind"] == "mixed"
    assert union_output.expression_resolution["physical_source_fields"] == [
        {"table": "ads.hist", "field": "test_index"}
    ]
    assert union_output.expression_resolution["rowset_sources"] == [
        {
            "source_type": "rowset",
            "scope": "union:t:b01",
            "field": "test_index",
            "expression": "COUNT(1)",
        }
    ]


def test_unqualified_expression_resolves_from_internal_source_refs():
    result = ScopeLineageResult(
        task_id="internal_source_ref_expression",
        target_table="mart.coupon_daily",
        stmt_kind="INSERT",
        scopes={
            "subq:c": ScopeData(
                kind="subquery",
                outputs=[
                    ScopeOutputField(
                        name="status",
                        transform="DIRECT",
                        expression="status",
                        expanded_expression="`ods.coupon_record`.`status`",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "single_source_projection",
                            "physical_source_fields": [{"table": "ods.coupon_record", "field": "status"}],
                            "generated_sources": [],
                            "source_kind": "physical",
                            "missing_reasons": [],
                        },
                        sources=[SourceRef(scope="ods.coupon_record", column="status")],
                    )
                ],
            ),
            "subq:t1": ScopeData(
                kind="subquery",
                outputs=[
                    ScopeOutputField(
                        name="status_fa",
                        transform="CONDITIONAL",
                        expression="CASE WHEN `status` = 0 THEN '审批中' ELSE '其他' END",
                        expanded_expression="CASE WHEN `status` = 0 THEN '审批中' ELSE '其他' END",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[SourceRef(scope="subq:c", column="status")],
                    )
                ],
            ),
        },
    )

    _resolve_expression_resolution_from_output_sources(result)
    output = result.scopes["subq:t1"].outputs[0]

    assert output.expression_resolution["status"] == "resolved"
    assert output.expression_resolution["resolution_type"] == "expression_sources_from_source_refs"
    assert output.expression_resolution["physical_source_fields"] == [
        {"table": "ods.coupon_record", "field": "status"}
    ]
    assert output.expression_resolution["missing_reasons"] == []


def test_nested_and_map_expressions_resolve_from_internal_source_refs():
    result = ScopeLineageResult(
        task_id="nested_internal_source_ref_expression",
        target_table="mart.events",
        stmt_kind="INSERT",
        scopes={
            "subq:a": ScopeData(
                kind="subquery",
                outputs=[
                    ScopeOutputField(
                        name="ext_info",
                        transform="DIRECT",
                        expression="ext_info",
                        expanded_expression="`dwd.events`.`ext_info`",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "single_source_projection",
                            "physical_source_fields": [{"table": "dwd.events", "field": "ext_info"}],
                            "generated_sources": [],
                            "source_kind": "physical",
                            "missing_reasons": [],
                        },
                        sources=[SourceRef(scope="dwd.events", column="ext_info")],
                    ),
                    ScopeOutputField(
                        name="max_group_id",
                        transform="DIRECT",
                        expression="max_group_id",
                        expanded_expression="`ods.unit`.`max_group_id`",
                        expression_resolution={
                            "status": "resolved",
                            "resolution_type": "single_source_projection",
                            "physical_source_fields": [{"table": "ods.unit", "field": "max_group_id"}],
                            "generated_sources": [],
                            "source_kind": "physical",
                            "missing_reasons": [],
                        },
                        sources=[SourceRef(scope="ods.unit", column="max_group_id")],
                    ),
                ],
            ),
            "ROOT": ScopeData(
                kind="root",
                outputs=[
                    ScopeOutputField(
                        name="build_no",
                        transform="EXPRESSION",
                        expression="`ext_info`['build_no']",
                        expanded_expression="`ext_info`['build_no']",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[SourceRef(scope="subq:a", column="ext_info")],
                    ),
                    ScopeOutputField(
                        name="is_ab_test",
                        transform="EXPRESSION",
                        expression="CAST(j.max_group_id.is_ab_test AS INT)",
                        expanded_expression="CAST(j.max_group_id.is_ab_test AS INT)",
                        expression_resolution={
                            "status": "unresolved",
                            "resolution_type": "raw_expression",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "source_kind": "unresolved",
                            "missing_reasons": ["no_physical_source_fields"],
                        },
                        sources=[SourceRef(scope="subq:a", column="max_group_id")],
                    ),
                ],
            ),
        },
    )

    _resolve_expression_resolution_from_output_sources(result)
    build_no, is_ab_test = result.scopes["ROOT"].outputs

    assert build_no.expression_resolution["status"] == "resolved"
    assert build_no.expression_resolution["physical_source_fields"] == [
        {"table": "dwd.events", "field": "ext_info"}
    ]
    assert {"table": "dwd.events", "field": "build_no"} not in build_no.expression_resolution["physical_source_fields"]
    assert is_ab_test.expression_resolution["status"] == "resolved"
    assert is_ab_test.expression_resolution["physical_source_fields"] == [
        {"table": "ods.unit", "field": "max_group_id"}
    ]
