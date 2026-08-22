"""Unit coverage for lineage fact gaps and UNION output branch mappings.

Migrated from the integration repository, which held the only tests for both functions while Core
had none. Core owns the behaviour, so Core owns the tests -- and the re-exports that repository
was importing through are meant to go once its ledger clears, so these import from the modules
that define them.

Both passes decide what the artifact *says about itself*: whether a partially resolved output
becomes a `lineage_fact_gap`, and which branch of a UNION an output is attributed to. Those are
states an end-to-end assertion tends to average away, which is why they are unit tests.

The fixtures arrived synthetic and were left that way.
"""

from __future__ import annotations

from scope_lineage.scope.star_passthrough import _populate_union_output_branch_mappings
from scope_lineage.scope.lineage_fact_gaps import _populate_lineage_fact_gaps
from scope_lineage.scope.scope_types import (
    Diagnostics,
    ScopeData,
    ScopeLineageResult,
    ScopeOutputField,
)


def test_partially_resolved_output_creates_lineage_fact_gap():
    result = ScopeLineageResult(
        task_id="partial_gap",
        target_table="dwd.partial_gap",
        stmt_kind="INSERT_OVERWRITE",
    )
    result.scopes["ROOT"] = ScopeData(
        kind="root",
        outputs=[
            ScopeOutputField(
                name="user_id",
                transform="DIRECT",
                expression="`a`.`user_id`",
                expression_resolution={
                    "status": "partially_resolved",
                    "resolution_type": "qualified_expression",
                    "physical_source_fields": [
                        {"table": "ods.ods_user", "field": "user_id"}
                    ],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": ["expanded_expression_contains_unexpanded_alias:a"],
                    "expanded_expression": "`a`.`user_id`",
                },
                final_target_columns=["dwd.partial_gap.user_id"],
            )
        ],
    )

    _populate_lineage_fact_gaps(result)

    assert result.diagnostics.lineage_fact_gaps
    gap = result.diagnostics.lineage_fact_gaps[0]
    assert gap["object_type"] == "output"
    assert gap["object_name"] == "user_id"
    assert gap["expression_resolution_status"] == "partially_resolved"
    assert "expanded_expression_contains_unexpanded_alias:a" in gap["missing_reasons"]


def test_union_branch_mapping_gap_is_reported_when_branch_unresolved():
    result = ScopeLineageResult(
        task_id="union_branch_gap",
        target_table="dwd.union_branch_gap",
        stmt_kind="INSERT_OVERWRITE",
    )
    result.scopes["union:main"] = ScopeData(
        kind="union",
        outputs=[
            ScopeOutputField(
                name="user_id",
                transform="UNION",
                expression="user_id",
                expression_resolution={
                    "status": "resolved",
                    "resolution_type": "union_branch_alignment",
                    "physical_source_fields": [
                        {"table": "ods.ods_user_a", "field": "user_id"}
                    ],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": [],
                    "union_branch_mappings": [
                        {
                            "branch_scope_id": "union:main:b01",
                            "branch_index": 0,
                            "output_field": "user_id",
                            "expression_sql": "`a`.`user_id`",
                            "physical_source_fields": [
                                {"table": "ods.ods_user_a", "field": "user_id"}
                            ],
                            "generated_sources": [],
                            "rowset_sources": [],
                            "resolution_status": "resolved",
                        },
                        {
                            "branch_scope_id": "union:main:b02",
                            "branch_index": 1,
                            "output_field": "user_id",
                            "expression_sql": "`b`.`user_id`",
                            "physical_source_fields": [],
                            "generated_sources": [],
                            "rowset_sources": [],
                            "resolution_status": "unresolved",
                            "missing_reasons": ["alias_not_bound_to_input_source:b"],
                        },
                    ],
                },
                final_target_columns=["dwd.union_branch_gap.user_id"],
            )
        ],
    )

    _populate_lineage_fact_gaps(result)

    gaps = result.diagnostics.lineage_fact_gaps
    assert len(gaps) == 1
    assert gaps[0]["object_type"] == "output.union_branch_mapping"
    assert gaps[0]["scope_id"] == "union:main"
    assert gaps[0]["object_name"] == "user_id@union:main:b02"
    assert gaps[0]["expression_resolution_status"] == "unresolved"
    assert gaps[0]["evidence_path"] == (
        "lineage.scopes.union:main.outputs[0]"
        ".expression_resolution.union_branch_mappings[1]"
    )


def test_union_branch_mapping_prefers_alignment_position_over_stale_name_match():
    result = ScopeLineageResult(
        task_id="union_position_mapping_names_differ",
        target_table="mart.target",
        stmt_kind="INSERT_OVERWRITE",
    )
    result.scopes["union:main:b01"] = ScopeData(
        kind="union_branch",
        outputs=[
            ScopeOutputField(
                name="event_time",
                transform="DIRECT",
                expression="event_time",
                expression_resolution={
                    "status": "resolved",
                    "resolution_type": "qualified_source_projection",
                    "physical_source_fields": [{"table": "ods.online_event", "field": "event_time"}],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": [],
                },
            )
        ],
    )
    result.scopes["union:main:b02"] = ScopeData(
        kind="union_branch",
        outputs=[
            ScopeOutputField(
                name="event_time",
                transform="DIRECT",
                expression="event_time",
                expression_resolution={
                    "status": "unresolved",
                    "resolution_type": "stale_name_match",
                    "physical_source_fields": [],
                    "generated_sources": [],
                    "source_kind": "unresolved",
                    "missing_reasons": ["stale_name_match_should_not_win"],
                },
            ),
            ScopeOutputField(
                name="send_time",
                transform="DIRECT",
                expression="send_time",
                expression_resolution={
                    "status": "resolved",
                    "resolution_type": "qualified_source_projection",
                    "physical_source_fields": [{"table": "ods.email_event", "field": "send_time"}],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": [],
                },
            ),
        ],
    )
    result.scopes["union:main"] = ScopeData(
        kind="union",
        union_branch_alignment={
            "field_alignment": [
                {
                    "aligned_output_name": "event_time",
                    "branch_items": [
                        {
                            "branch_id": "union:main:b01",
                            "branch_index": 0,
                            "position": 1,
                            "output_name": "event_time",
                        },
                        {
                            "branch_id": "union:main:b02",
                            "branch_index": 1,
                            "position": 2,
                            "output_name": "event_time",
                        },
                    ],
                }
            ]
        },
        outputs=[
            ScopeOutputField(
                name="event_time",
                transform="UNION",
                expression="event_time",
                expression_resolution={
                    "status": "resolved",
                    "resolution_type": "union_branch_alignment",
                    "physical_source_fields": [],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": [],
                },
            )
        ],
    )

    _populate_union_output_branch_mappings(result)

    mappings = result.scopes["union:main"].outputs[0].expression_resolution["union_branch_mappings"]
    assert mappings[1]["output_field"] == "send_time"
    assert mappings[1]["resolution_status"] == "resolved"
    assert mappings[1]["physical_source_fields"] == [
        {"table": "ods.email_event", "field": "send_time"}
    ]
    assert mappings[1]["candidate_rejection_reasons"] == [
        {
            "candidate_output": "event_time",
            "reason": "name_match_not_at_alignment_position",
        }
    ]


def test_union_output_status_downgrades_when_branch_mapping_unresolved():
    result = ScopeLineageResult(
        task_id="union_branch_partial",
        target_table="dwd.union_branch_partial",
        stmt_kind="INSERT_OVERWRITE",
    )
    result.scopes["union:main"] = ScopeData(
        kind="union",
        union_branch_alignment={
            "field_alignment": [
                {
                    "aligned_output_name": "user_id",
                    "branch_items": [
                        {
                            "branch_id": "union:main:b01",
                            "branch_index": 0,
                            "position": 1,
                            "output_name": "user_id",
                            "expression_sql": "`a`.`user_id`",
                            "expression_resolution": {
                                "status": "resolved",
                                "physical_source_fields": [
                                    {"table": "ods.ods_user_a", "field": "user_id"}
                                ],
                                "generated_sources": [],
                                "source_kind": "physical",
                                "missing_reasons": [],
                            },
                        },
                        {
                            "branch_id": "union:main:b02",
                            "branch_index": 1,
                            "position": 1,
                            "output_name": "user_id",
                            "expression_sql": None,
                            "expression_resolution": {
                                "status": "unresolved",
                                "physical_source_fields": [],
                                "generated_sources": [],
                                "source_kind": "unresolved",
                                "missing_reasons": [],
                            },
                        },
                    ],
                }
            ]
        },
        outputs=[
            ScopeOutputField(
                name="user_id",
                transform="UNION",
                expression="user_id",
                expression_resolution={
                    "status": "resolved",
                    "physical_source_fields": [
                        {"table": "ods.ods_user_a", "field": "user_id"}
                    ],
                    "generated_sources": [],
                    "source_kind": "physical",
                    "missing_reasons": [],
                },
            )
        ],
    )

    _populate_union_output_branch_mappings(result)

    output = result.scopes["union:main"].outputs[0]
    assert output.expression_resolution["status"] == "partially_resolved"
    assert "union_branch_mapping_unresolved" in output.expression_resolution["missing_reasons"]


def test_unexpanded_alias_in_expression_becomes_lineage_fact_gap():
    result = ScopeLineageResult(task_id="gap_alias", target_table="mart.t", stmt_kind="INSERT")
    result.scopes["ROOT"] = ScopeData(
        kind="root",
        outputs=[
            ScopeOutputField(
                name="update_flag",
                transform="EXPRESSION",
                expression="CASE WHEN u.unique_id IS NULL THEN 'inactive' ELSE 'active' END",
                expanded_expression="CASE WHEN `u`.`unique_id` IS NULL THEN 'inactive' ELSE 'active' END",
                expression_resolution={
                    "status": "partially_resolved",
                    "resolution_type": "qualified_expression",
                    "physical_source_fields": [],
                    "generated_sources": [
                        {
                            "source_type": "CONSTANT",
                            "value": "inactive",
                            "transform": "CASE_WHEN",
                        }
                    ],
                    "source_kind": "generated",
                    "missing_reasons": ["expanded_expression_contains_unexpanded_alias:u"],
                    "unresolved_qualifiers": ["u"],
                },
            )
        ],
        alias_source_bindings=[{"alias": "u", "source_type": "scope", "source_id": "cte:u"}],
    )
    result.diagnostics = Diagnostics()

    _populate_lineage_fact_gaps(result)

    assert result.diagnostics.lineage_fact_gaps
    gap = result.diagnostics.lineage_fact_gaps[0]
    assert gap["gap_type"] == "expression_resolution_incomplete"
    assert "expanded_expression_contains_unexpanded_alias:u" in gap["missing_reasons"]
    assert gap["owner_hint"] == "parser_internal_fact_backfill"


def test_union_branch_missing_output_gap_includes_branch_position_and_available_outputs():
    result = ScopeLineageResult(task_id="union_missing_evidence", target_table="mart.t", stmt_kind="INSERT")
    result.scopes["union:main"] = ScopeData(
        kind="union",
        outputs=[
            ScopeOutputField(
                name="event_time",
                transform="UNION",
                expression="event_time",
                expression_resolution={
                    "status": "partially_resolved",
                    "resolution_type": "union_branch_alignment",
                    "physical_source_fields": [],
                    "generated_sources": [],
                    "source_kind": "unresolved",
                    "missing_reasons": ["union_branch_mapping_unresolved"],
                    "union_branch_mappings": [
                        {
                            "branch_scope_id": "union:main:b02",
                            "branch_index": 1,
                            "output_field": "event_time",
                            "expected_position": 1,
                            "available_branch_outputs": ["user_id"],
                            "resolution_status": "unresolved",
                            "missing_reasons": ["union_branch_output_missing"],
                        }
                    ],
                },
            )
        ],
    )

    _populate_lineage_fact_gaps(result)

    gap = result.diagnostics.lineage_fact_gaps[0]
    evidence = gap["evidence_summary"]
    assert evidence["union_branch_mappings"][0]["branch_scope_id"] == "union:main:b02"
    assert evidence["union_branch_mappings"][0]["expected_position"] == 1
    assert evidence["union_branch_mappings"][0]["available_branch_outputs"] == ["user_id"]
