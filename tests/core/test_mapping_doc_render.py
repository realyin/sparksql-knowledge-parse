"""Behavioural tests for the mapping.md renderer (contract-derived view, mapping-md/1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scope_lineage.contract import to_lineage_dict

from .statement_document import write_statement_documents
from scope_lineage.render.mapping_markdown import (
    DOC_FORMAT,
    FIELD_ID_SPAN_PATTERN,
    STEP_LINE_PATTERN,
    render_mapping_markdown,
    render_warnings_markdown,
)
from scope_lineage.scope.scope_builder import parse_scope_lineage


UNION_CASE_SQL = """
INSERT OVERWRITE TABLE mart.channel_metrics
WITH normalized AS (
  SELECT pay_amount, pay_status, 'APP' AS channel FROM ods.app_order
  UNION ALL
  SELECT order_amount AS pay_amount, order_status AS pay_status, 'WEB' AS channel
  FROM ods.web_order
)
SELECT channel,
       SUM(CASE WHEN pay_status = 'PAID' THEN pay_amount ELSE 0 END) AS paid_amount
FROM normalized
GROUP BY channel
"""

UNION_CASE_SCHEMA = {
    "ods.app_order": ["pay_amount", "pay_status"],
    "ods.web_order": ["order_amount", "order_status"],
}

JOIN_CASE_SQL = """
INSERT INTO mart.customer_profile
WITH order_summary AS (
  SELECT customer_id, COUNT(1) AS order_count FROM dwd.order_detail GROUP BY customer_id
)
SELECT base.customer_id, summary.order_count
FROM ods.customer_base AS base
LEFT JOIN order_summary AS summary ON base.customer_id = summary.customer_id
"""

JOIN_CASE_SCHEMA = {
    "ods.customer_base": ["customer_id"],
    "dwd.order_detail": ["customer_id"],
}

SELF_JOIN_SQL = """
INSERT INTO mart.node_edges
SELECT a.id, b.id AS parent_id
FROM ods.nodes AS a
JOIN ods.nodes AS b ON a.parent_id = b.id AND a.batch_id = b.batch_id
"""

SELF_JOIN_SCHEMA = {"ods.nodes": ["id", "parent_id", "batch_id"]}


def _document(sql: str, task_id: str = "case_task", schema=None) -> dict:
    return to_lineage_dict(parse_scope_lineage(sql, task_id, schema=schema))


def _documents_with_diagnostics(sql: str, tmp_path: Path, schema=None) -> tuple[dict, dict]:
    result = parse_scope_lineage(sql, "case_task", schema=schema)
    write_statement_documents(result, tmp_path / "artifacts")
    lineage = json.loads((tmp_path / "artifacts" / "lineage.json").read_text(encoding="utf-8"))
    diagnostics = json.loads(
        (tmp_path / "artifacts" / "diagnostics.json").read_text(encoding="utf-8")
    )
    return lineage, diagnostics


def _front_matter(rendered: str) -> dict:
    lines = rendered.splitlines()
    assert lines[0] == "---"
    closing = lines[1:].index("---") + 1
    block = {}
    for line in lines[1:closing]:
        key, sep, raw = line.partition(": ")
        assert sep, f"front matter line is not `key: value`: {line!r}"
        block[key] = json.loads(raw)
    return block


# ---------------------------------------------------------------- entry contract


def test_rejects_unknown_schema_versions() -> None:
    document = _document("INSERT INTO mart.t SELECT id FROM ods.users")
    document["schema_version"] = "3.0"
    with pytest.raises(ValueError, match="3.0"):
        render_mapping_markdown(document)


def test_renders_a_v2_task_document_one_section_per_statement() -> None:
    import json
    from pathlib import Path

    task_doc = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "task_lineage_contract"
            / "merge_cte_source"
            / "lineage.json"
        ).read_text(encoding="utf-8")
    )
    assert task_doc["schema_version"] == "2.0"

    rendered = render_mapping_markdown(task_doc)

    for statement in task_doc["statement_sequence"]:
        statement_id = statement["statement_id"]
        assert statement_id in rendered, f"statement {statement_id} missing from render"
        entry = task_doc["statement_lineage"][statement_id]
        # Each statement section is the v1 render of its entry.
        assert render_mapping_markdown(entry) in rendered


def test_renders_twice_byte_identical() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    assert render_mapping_markdown(document) == render_mapping_markdown(document)


def test_front_matter_is_flat_json_scalars_without_versions_or_timestamps() -> None:
    document = _document(UNION_CASE_SQL, "channel_metrics", schema=UNION_CASE_SCHEMA)
    block = _front_matter(render_mapping_markdown(document))
    # the contract's top-level `task_id` holds a name-based statement identifier,
    # so the document labels it as the task name
    assert block == {
        "doc_format": DOC_FORMAT,
        "schema_version": "1.0",
        "task_name": "channel_metrics",
        "target_table": "mart.channel_metrics",
        "stmt_kind": document["stmt_kind"],
    }


def test_tolerates_a_document_stripped_to_required_keys() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    for optional_key in (
        "target_field_binding",
        "task_dependencies",
        "related_metadata",
        "target_partition_spec",
        "target_partition_columns",
        "target_partition_mode",
        "scope_profile",
    ):
        document.pop(optional_key, None)
    document.pop("target_binding_absent_reason", None)
    rendered = render_mapping_markdown(document)
    assert "未做目标绑定" in rendered


def test_binding_absence_reason_renders_gloss_and_flags_the_risky_case() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    assert document.get("target_binding_absent_reason") == "metadata_not_provided"
    rendered = render_mapping_markdown(document)
    assert "--target-ddl-metadata" in rendered

    document["target_binding_absent_reason"] = "target_table_not_found"
    risky = render_mapping_markdown(document)
    assert "⚠ 目标绑定" in risky


def test_missing_diagnostics_document_is_stated_not_silent() -> None:
    document = _document("INSERT INTO mart.t SELECT id FROM ods.users")
    rendered = render_mapping_markdown(document, None)
    assert "无 diagnostics 文档" in rendered


# ---------------------------------------------------------------- step line grammar


def test_step_lines_match_grammar_and_round_trip_chain_facts() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    parsed_steps = []
    for line in rendered.splitlines():
        match = STEP_LINE_PATTERN.match(line)
        if match:
            inputs = frozenset(FIELD_ID_SPAN_PATTERN.findall(match.group("inputs")))
            output = FIELD_ID_SPAN_PATTERN.findall(match.group("output"))[0]
            parsed_steps.append((inputs, output, match.group("step_type")))
    assert parsed_steps, "no step lines found"

    expected = set()
    for chain in document["field_mapping_chains"]:
        for step in chain["ordered_steps"]:
            expected.add(
                (
                    frozenset(step["input_fields"]),
                    step["output_field"],
                    step["step_type"],
                )
            )
    assert set(parsed_steps) == expected


def test_step_expression_prefers_display_expression() -> None:
    document = _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    root_outputs = {o["name"]: o for o in document["scopes"]["ROOT"]["outputs"]}
    display = root_outputs["customer_id"].get("display_expression")
    assert display, "premise: alias resolution must produce a display form"
    step_lines = [
        line
        for line in rendered.splitlines()
        if STEP_LINE_PATTERN.match(line) and "customer_id" in line and "ROOT" not in line
    ]
    assert any(display in line for line in rendered.splitlines() if "步骤" in line)
    assert step_lines is not None  # grammar itself checked in the round-trip test


def test_adversarial_literals_stay_inside_expression_span() -> None:
    sql = (
        "INSERT INTO mart.t SELECT CASE WHEN name = 'x；y → z|w' THEN 'a\nb' "
        "ELSE name END AS flagged FROM ods.users"
    )
    document = _document(sql, schema={"ods.users": ["name"]})
    rendered = render_mapping_markdown(document)

    matches = [
        STEP_LINE_PATTERN.match(line)
        for line in rendered.splitlines()
        if line.startswith("- 步骤")
    ]
    matches = [m for m in matches if m]
    assert matches, "adversarial step line failed the grammar"
    joined = "".join(m.group("expression") for m in matches)
    assert "x；y → z|w" in joined
    assert "\n" not in joined
    assert "a\\nb" in joined


def test_field_sections_carry_identity_evidence_and_sources() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    assert "### 字段 mart.channel_metrics.paid_amount" in rendered
    paid_chain = next(
        chain
        for chain in document["field_mapping_chains"]
        if chain["target_field"] == "paid_amount"
    )
    assert f"mapping_chain_id={paid_chain['mapping_chain_id']}" in rendered
    assert f"chain={paid_chain['chain_id']}" in rendered
    assert "来源字段：" in rendered
    assert "`ods.app_order.pay_amount`" in rendered
    # constants are separated from physical sources, not mixed in
    assert "常量来源：" in rendered
    assert "'APP'" in rendered


def test_merge_field_titles_are_disambiguated_per_branch(tmp_path: Path) -> None:
    merge_case = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "lineage_contract"
            / "merge"
            / "case.json"
        ).read_text(encoding="utf-8")
    )
    document = _document(merge_case["sql"], merge_case["task_id"], schema=merge_case.get("schema"))
    rendered = render_mapping_markdown(document)

    titles = [line for line in rendered.splitlines() if line.startswith("### 字段 ")]
    assert len(titles) == len(set(titles)), f"duplicate chunk titles: {titles}"
    assert any("merge:matched" in title for title in titles)
    assert any("merge:not_matched" in title for title in titles)


def test_directory_target_titles_do_not_fabricate_a_table_name() -> None:
    sql = (
        "INSERT OVERWRITE DIRECTORY '/warehouse/export/daily' "
        "SELECT id FROM ods.users"
    )
    document = _document(sql, schema={"ods.users": ["id"]})
    assert document["target_table"].startswith("directory:")
    rendered = render_mapping_markdown(document)
    assert "写入目录 /warehouse/export/daily" in rendered
    assert "### 字段 directory:" not in rendered


# ---------------------------------------------------------------- relations section


def test_join_relations_render_physical_key_pairs() -> None:
    document = _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    assert "LEFT_OUTER" in rendered
    assert "logic_block_id=" in rendered
    # the CTE-side key is pierced to its physical field in the detail
    assert "dwd.order_detail.customer_id" in rendered
    # a cleanly split join does not repeat the verbatim ON text
    assert "ON：" not in rendered


CTE_PAIR_JOIN_SQL = """
INSERT INTO mart.queue_stats
WITH dim_base AS (
  SELECT queuecode, operator FROM ods.tasks
),
repay AS (
  SELECT queuecode, COUNT(1) AS repay_cnt FROM ods.tasks GROUP BY queuecode
),
call_agg AS (
  SELECT queuecode, COUNT(1) AS call_cnt FROM ods.tasks GROUP BY queuecode
)
SELECT db.queuecode, db.operator, rp.repay_cnt, ca.call_cnt
FROM dim_base db
LEFT JOIN repay rp ON db.queuecode = rp.queuecode
LEFT JOIN call_agg ca ON db.queuecode = ca.queuecode
"""

CTE_PAIR_SCHEMA = {"ods.tasks": ["queuecode", "operator"]}


def test_cte_only_joins_stay_out_of_the_table_level_overview() -> None:
    document = _document(CTE_PAIR_JOIN_SQL, schema=CTE_PAIR_SCHEMA)
    rendered = render_mapping_markdown(document)

    # both CTEs read the same table: piercing yields no table-level relationship,
    # so the overview says so instead of listing meaningless self-pairs
    overview = rendered.split("## 3.")[1].split("## 4.")[0]
    assert "cte:repay" not in overview
    assert "中间结果" in overview
    assert "ods.tasks.queuecode = ods.tasks.queuecode" not in rendered
    # the CTE-level relations remain addressable in the logic section
    detail = rendered.split("## 6.")[1].split("## 7.")[0]
    assert "cte:repay" in detail and "cte:call_agg" in detail


def test_overview_lists_physical_table_pairs_with_short_keys() -> None:
    document = _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)
    overview = rendered.split("## 3.")[1].split("## 4.")[0]

    # the CTE side is pierced to its physical table in the overview row
    row = next(
        line for line in overview.splitlines() if line.startswith("| ods.customer_base")
    )
    assert "dwd.order_detail" in row
    assert "customer_id" in row
    # the key cell uses short field names; full table names live in 左表/右表
    assert "dwd.order_detail.customer_id" not in row


# Non-equi ON: genuinely unsplittable into key pairs, so the degraded rendering path
# stays exercised now that equality self-joins resolve properly (JOINALIAS-001).
REPEATED_DEGRADED_JOIN_SQL = """
INSERT INTO mart.agents
WITH a1 AS (
  SELECT x.guid FROM ods.user_df x LEFT JOIN ods.user_df y ON x.leader > y.guid
),
a2 AS (
  SELECT x.guid FROM ods.user_df x LEFT JOIN ods.user_df y ON x.leader > y.guid
)
SELECT a1.guid FROM a1 JOIN a2 ON a1.guid = a2.guid
"""

REPEATED_DEGRADED_JOIN_SCHEMA = {"ods.user_df": ["guid", "leader"]}


def test_identical_degraded_joins_merge_into_one_counted_row() -> None:
    document = _document(REPEATED_DEGRADED_JOIN_SQL, schema=REPEATED_DEGRADED_JOIN_SCHEMA)
    rendered = render_mapping_markdown(document)
    overview = rendered.split("## 3.")[1].split("## 4.")[0]

    degraded_rows = [
        line
        for line in overview.splitlines()
        if "⚠ 未拆分" in line and "ods.user_df" in line
    ]
    assert len(degraded_rows) == 1
    assert "2 处" in degraded_rows[0]


def test_equality_self_join_renders_key_pairs_not_the_degraded_label() -> None:
    # Pinned the degraded rendering while the core collapsed self-join aliases; the core
    # now keeps the sides apart (JOINALIAS-001), so this input renders as a normal join.
    document = _document(SELF_JOIN_SQL, schema=SELF_JOIN_SCHEMA)
    rendered = render_mapping_markdown(document)

    assert "连接条件（未拆分）" not in rendered
    assert "parent_id = id" in rendered


def test_a_non_equi_join_degrades_with_warning_and_neutral_label() -> None:
    document = _document(
        SELF_JOIN_SQL.replace(
            "ON a.parent_id = b.id AND a.batch_id = b.batch_id",
            "ON a.batch_id > b.batch_id",
        ),
        schema=SELF_JOIN_SCHEMA,
    )
    rendered = render_mapping_markdown(document)

    assert "⚠" in rendered
    assert "连接条件（未拆分）" in rendered
    assert "非等值" not in rendered
    # only the degraded path keeps the verbatim ON text (the split is incomplete there)
    assert "ON：" in rendered


def test_union_relations_list_branches_and_physical_tables() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    assert "UNION_ALL" in rendered
    assert "2 分支" in rendered
    for table in ("ods.app_order", "ods.web_order"):
        assert table in rendered


# ---------------------------------------------------------------- mapping table


def test_generated_sources_column_appears_only_when_populated() -> None:
    with_constants = render_mapping_markdown(
        _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    )
    assert "生成来源" in with_constants

    without_constants = render_mapping_markdown(
        _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    )
    assert "生成来源" not in without_constants


def test_mapping_table_sorts_by_ordinal_and_flags_incomplete() -> None:
    document = _document("INSERT INTO mart.t SELECT * FROM ods.raw_events")
    rendered = render_mapping_markdown(document)
    assert "⚠" in rendered

    ordered = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered_ordered = render_mapping_markdown(ordered)
    table_lines = [
        line for line in rendered_ordered.splitlines() if line.startswith("| ")
    ]
    channel_pos = next(i for i, l in enumerate(table_lines) if "channel" in l)
    paid_pos = next(i for i, l in enumerate(table_lines) if "paid_amount" in l)
    assert channel_pos < paid_pos  # output_ordinal fallback ordering


# ---------------------------------------------------------------- graph and diagnostics


def test_scope_level_join_detail_lives_in_logic_section_not_relations() -> None:
    document = _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)
    relations = rendered.split("## 3.")[1].split("## 4.")[0]
    logic_section = rendered.split("## 6.")[1].split("## 7.")[0]

    # section 3 is table-level only: no scope-level join detail lines there
    assert "logic_block_id=" not in relations
    # the detail (with its ids and pierced keys) sits under the owning scope in section 6
    assert "logic_block_id=" in logic_section
    assert "dwd.order_detail.customer_id" in logic_section
    assert '"on":' not in logic_section  # never the old json.dumps form

    filtered = _document(
        "INSERT INTO mart.t SELECT id FROM ods.users WHERE ds = '20260801'",
        schema={"ods.users": ["id", "ds"]},
    )
    logic_only = render_mapping_markdown(filtered).split("## 6.")[1].split("## 7.")[0]
    assert "过滤：" in logic_only  # filters render nowhere else, so they stay


def test_union_branch_joins_attach_to_their_union_scope_in_logic_section() -> None:
    sql = """
    INSERT INTO mart.mixed
    SELECT e.user_id FROM ods.events e JOIN ods.users u ON e.user_id = u.user_id
    UNION ALL
    SELECT user_id FROM ods.fallback_users
    """
    document = _document(
        sql,
        schema={
            "ods.events": ["user_id"],
            "ods.users": ["user_id"],
            "ods.fallback_users": ["user_id"],
        },
    )
    branch_scope = next(
        scope_id
        for scope_id, scope in document["scopes"].items()
        if any(b.get("logic_type") == "join" for b in scope.get("logic_blocks", []))
    )
    assert branch_scope.startswith("union:")
    rendered = render_mapping_markdown(document)
    logic_section = rendered.split("## 6.")[1].split("## 7.")[0]
    # the branch scope is folded out of scope_profile, but its join must not be lost
    assert f"@ {branch_scope}" in logic_section


def test_mermaid_nodes_use_mapped_ids_with_labels() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    mermaid = rendered.split("```mermaid")[1].split("```")[0]
    assert 'n0["' in mermaid
    for node in document["scope_graph"]["nodes"]:
        assert f'"{node}"' in mermaid
        assert not re.search(rf"^\s*{re.escape(node)}\s*-->", mermaid, re.MULTILINE)


def test_gaps_section_keeps_conclusions_and_defers_warnings_to_sibling_doc(
    tmp_path: Path,
) -> None:
    lineage, diagnostics = _documents_with_diagnostics(
        "INSERT INTO mart.t SELECT * FROM ods.raw_events", tmp_path
    )
    rendered = render_mapping_markdown(lineage, diagnostics)
    # warning bodies leave the mapping document; only a counted pointer remains
    assert "star_not_expanded" not in rendered
    assert "warnings.md" in rendered
    # the unexpanded star is a fact gap now (STARGAP-001), so the gaps section reports
    # it as a conclusion instead of claiming there is none
    assert "缺口：无" not in rendered
    assert "projection_wildcard_unexpanded" in rendered


def test_warnings_doc_groups_by_type_with_chinese_gloss(tmp_path: Path) -> None:
    lineage, diagnostics = _documents_with_diagnostics(
        "INSERT INTO mart.t SELECT * FROM ods.raw_events", tmp_path
    )
    rendered = render_warnings_markdown(diagnostics, lineage)
    assert rendered is not None
    assert rendered.splitlines()[0] == "---"
    assert 'doc_format: "warnings-md/1"' in rendered
    assert "task_name:" in rendered and "task_id:" not in rendered
    assert "## star_not_expanded" in rendered
    assert "SELECT *" in rendered  # the Chinese gloss for the type
    assert "@ ROOT" in rendered


def test_warnings_doc_is_none_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    lineage, diagnostics = _documents_with_diagnostics(
        "INSERT INTO mart.t SELECT id FROM ods.users",
        tmp_path,
        schema={"ods.users": ["id"]},
    )
    assert not diagnostics.get("warnings")
    assert render_warnings_markdown(diagnostics, lineage) is None


# ---------------------------------------------------------------- machine readability


def test_every_referenced_id_dereferences_into_the_source_document() -> None:
    document = _document(JOIN_CASE_SQL, schema=JOIN_CASE_SCHEMA)
    rendered = render_mapping_markdown(document)

    chain_ids = {c["mapping_chain_id"] for c in document["field_mapping_chains"]}
    for match in re.finditer(r"mapping_chain_id=(\S+?)；", rendered):
        assert match.group(1) in chain_ids

    block_ids = {
        block["logic_block_id"]
        for scope in document["scopes"].values()
        for block in scope.get("logic_blocks", [])
    }
    for match in re.finditer(r"logic_block_id=([^\s；|）]+)", rendered):
        assert match.group(1) in block_ids


def test_doc_format_constant_matches_front_matter() -> None:
    document = _document("INSERT INTO mart.t SELECT id FROM ods.users")
    block = _front_matter(render_mapping_markdown(document))
    assert block["doc_format"] == DOC_FORMAT == "mapping-md/1"


# ---------------------------------------------------------------- golden baseline

FIXTURES = Path(__file__).parent / "fixtures" / "lineage_contract"
GOLDEN_CASES = tuple(sorted(path.parent for path in FIXTURES.glob("*/case.json")))


@pytest.mark.parametrize("case_dir", GOLDEN_CASES, ids=lambda path: path.name)
def test_mapping_md_matches_golden_bytes(case_dir: Path) -> None:
    lineage = json.loads((case_dir / "lineage.json").read_text(encoding="utf-8"))
    diagnostics = json.loads(
        (case_dir / "diagnostics.json").read_text(encoding="utf-8")
    )
    first = render_mapping_markdown(lineage, diagnostics)
    second = render_mapping_markdown(lineage, diagnostics)
    expected = (case_dir / "mapping.md").read_text(encoding="utf-8")
    assert first == expected
    assert second == first

    warnings_doc = render_warnings_markdown(diagnostics, lineage)
    golden_warnings = case_dir / "warnings.md"
    if warnings_doc is None:
        assert not golden_warnings.exists()
    else:
        assert warnings_doc == golden_warnings.read_text(encoding="utf-8")


# ---------------------------------------------------------------- selection options


def test_fields_filter_limits_step_sections() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document, fields=["paid_amount"])
    assert "### 字段 mart.channel_metrics.paid_amount" in rendered
    assert "### 字段 mart.channel_metrics.channel" not in rendered


def test_sections_filter_drops_unlisted_sections() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    rendered = render_mapping_markdown(document, sections=["overview", "steps"])
    assert "## 1. 概览" in rendered
    assert "加工步骤" in rendered
    assert "```mermaid" not in rendered


def _write_artifacts(sql: str, out_dir: Path, schema=None) -> Path:
    result = parse_scope_lineage(sql, out_dir.name, schema=schema)
    write_statement_documents(result, out_dir)
    return out_dir


def test_cli_render_writes_mapping_next_to_lineage(tmp_path: Path) -> None:
    from scope_lineage.cli import main

    task_dir = _write_artifacts(
        "INSERT INTO mart.t SELECT id FROM ods.users",
        tmp_path / "task_a",
        schema={"ods.users": ["id"]},
    )
    assert main(["render", "--lineage", str(task_dir / "lineage.json")]) == 0
    rendered = (task_dir / "mapping.md").read_text(encoding="utf-8")
    assert rendered.startswith("---\n")
    assert "无 diagnostics 文档" not in rendered  # diagnostics read from the sibling file
    # this clean statement has no warnings, so no warnings.md is written
    assert not (task_dir / "warnings.md").exists()


def test_cli_render_writes_warnings_doc_for_warning_bearing_tasks(tmp_path: Path) -> None:
    from scope_lineage.cli import main

    task_dir = _write_artifacts(
        "INSERT INTO mart.t SELECT * FROM ods.raw_events", tmp_path / "task_star"
    )
    assert main(["render", "--lineage", str(task_dir / "lineage.json")]) == 0
    warnings_doc = (task_dir / "warnings.md").read_text(encoding="utf-8")
    assert "star_not_expanded" in warnings_doc


def test_cli_render_directory_recurses_and_skips_unknown_documents(tmp_path: Path, capsys) -> None:
    from scope_lineage.cli import main

    corpus = tmp_path / "corpus"
    _write_artifacts(
        "INSERT INTO mart.t SELECT id FROM ods.users",
        corpus / "nested" / "task_a",
        schema={"ods.users": ["id"]},
    )
    # A 2.0 file without artifact_kind is not a task document -- unknown, skipped.
    unknown_dir = corpus / "task_unknown"
    unknown_dir.mkdir(parents=True)
    (unknown_dir / "lineage.json").write_text(
        json.dumps({"schema_version": "2.0"}), encoding="utf-8"
    )

    assert main(["render", "--lineage", str(corpus)]) == 0
    assert (corpus / "nested" / "task_a" / "mapping.md").exists()
    assert not (unknown_dir / "mapping.md").exists()
    assert "skipped_unknown_version=1" in capsys.readouterr().out


def test_cli_render_out_mirrors_input_tree(tmp_path: Path) -> None:
    from scope_lineage.cli import main

    corpus = tmp_path / "corpus"
    _write_artifacts(
        "INSERT INTO mart.t SELECT id FROM ods.users",
        corpus / "nested" / "task_a",
        schema={"ods.users": ["id"]},
    )
    out = tmp_path / "docs"
    assert main(["render", "--lineage", str(corpus), "--out", str(out)]) == 0
    assert (out / "nested" / "task_a" / "mapping.md").exists()
    assert not (corpus / "nested" / "task_a" / "mapping.md").exists()


def test_cli_render_rejects_unknown_schema_versions(tmp_path: Path, capsys) -> None:
    from scope_lineage.cli import main

    unknown = tmp_path / "lineage.json"
    unknown.write_text(json.dumps({"schema_version": "3.0"}), encoding="utf-8")
    assert main(["render", "--lineage", str(unknown)]) == 1
    assert "3.0" in capsys.readouterr().err


def test_cli_render_field_and_sections_options(tmp_path: Path) -> None:
    from scope_lineage.cli import main

    task_dir = _write_artifacts(
        UNION_CASE_SQL, tmp_path / "task_union", schema=UNION_CASE_SCHEMA
    )
    assert (
        main(
            [
                "render",
                "--lineage",
                str(task_dir / "lineage.json"),
                "--field",
                "paid_amount",
                "--sections",
                "overview,steps",
            ]
        )
        == 0
    )
    rendered = (task_dir / "mapping.md").read_text(encoding="utf-8")
    assert "paid_amount" in rendered
    assert "### 字段 mart.channel_metrics.channel" not in rendered
    assert "```mermaid" not in rendered


def test_expanded_flag_adds_expanded_expression_lines() -> None:
    document = _document(UNION_CASE_SQL, schema=UNION_CASE_SCHEMA)
    plain = render_mapping_markdown(document)
    expanded = render_mapping_markdown(document, expanded=True)
    assert "展开表达式：" not in plain
    assert "展开表达式：" in expanded


def test_render_cli_renders_task_documents(tmp_path):
    """The parse default emits task documents; the render subcommand must render them
    (it silently skipped every non-1.0 file, which after the v1 retirement meant it
    rendered nothing at all -- caught by local end-to-end verification)."""
    from scope_lineage.cli import main

    sql_path = tmp_path / "demo.sql"
    sql_path.write_text("INSERT INTO mart.t SELECT id FROM ods.source", encoding="utf-8")
    out = tmp_path / "artifacts"
    assert main(["parse", "--sql-file", str(sql_path), "--out", str(out)]) == 0

    assert main(["render", "--lineage", str(out)]) == 0
    mapping = out / "demo" / "mapping.md"
    assert mapping.is_file(), "render skipped the task document"
    text = mapping.read_text(encoding="utf-8")
    assert "stmt:" in text and "ods.source" in text
