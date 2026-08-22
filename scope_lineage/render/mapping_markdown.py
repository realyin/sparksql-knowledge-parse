"""Render one lineage.json (+ diagnostics.json) into a mapping.md field-mapping document.

This module is a derived VIEW of the versioned contract, not a second source of lineage
truth. It consumes contract document dicts only (the shape produced by ``to_lineage_dict``
and written to ``lineage.json``) and never imports scope-internal dataclasses, so a document
read back from disk and one produced in memory render identically.

Machine readability is provided by the document itself, not by a second JSON artifact:
- a flat YAML front matter block whose values are JSON scalars;
- fixed line grammars (``STEP_LINE_PATTERN``) with every field id inside a code span and
  the expression always last on the line, so SQL literals containing ``；``/``→``/``|``
  cannot break parsing;
- contract ids (mapping_chain_id, logic_block_id, scope_id) as join keys back into
  lineage.json.

The layout grammar is versioned as ``DOC_FORMAT`` (documented in docs/zh-CN/mapping-doc.md);
changing any line grammar requires bumping it there and here together.
"""

from __future__ import annotations

import json
import re
from typing import Iterable


DOC_FORMAT = "mapping-md/1"

SUPPORTED_SCHEMA_VERSION = "1.0"
TASK_SCHEMA_VERSION = "2.0"

SECTION_ORDER = (
    "overview",
    "sources",
    "relations",
    "mapping",
    "steps",
    "logic",
    "graph",
    "deps",
    "gaps",
)

_SECTION_TITLES = {
    "overview": "概览",
    "sources": "来源表",
    "relations": "来源表关系",
    "mapping": "字段映射总表",
    "steps": "加工步骤明细",
    "logic": "加工逻辑汇总",
    "graph": "scope 结构图",
    "deps": "任务依赖",
    "gaps": "不确定性与缺口",
}

# One processing step per line. Every field id sits in a single-backtick code span and the
# expression is the last field on the line (label ``表达式：`` to end of line), so separator
# characters inside SQL literals cannot break the grammar.
STEP_LINE_PATTERN = re.compile(
    r"^- 步骤 (?P<no>\d+)/(?P<total>\d+)："
    r"(?P<inputs>`[^`]*`(?:、`[^`]*`)*) → "
    r"(?P<output>`[^`]*`)；"
    r"(?P<step_type>[a-z_]+)"
    r"(?:；粒度=(?P<grain>[a-z_]+))?"
    r"；表达式：(?P<expression>.*)$"
)

# Extracts the field ids back out of a rendered span list.
FIELD_ID_SPAN_PATTERN = re.compile(r"`([^`]*)`")

_DERIVED_SCOPE_PREFIXES = ("cte:", "subq:", "union:", "udtf:")

_DIRECTORY_TARGET_PREFIX = "directory:"


def render_mapping_markdown(
    lineage_document: dict,
    diagnostics_document: dict | None = None,
    *,
    fields: Iterable[str] | None = None,
    expanded: bool = False,
    sections: Iterable[str] | None = None,
) -> str:
    """Render the mapping document for one write statement.

    ``fields`` restricts the per-field step sections; ``sections`` restricts which numbered
    sections appear (names from ``SECTION_ORDER``); ``expanded`` adds the fully expanded
    expression under each step.
    """
    version = lineage_document.get("schema_version")
    if (
        version == TASK_SCHEMA_VERSION
        and lineage_document.get("artifact_kind") == "task_lineage"
    ):
        # A task document is an ordered set of statement documents, each of which is
        # exactly the v1 shape (the task contract reuses the same converter). Render
        # one section per statement, in script order.
        return _render_task_document(
            lineage_document,
            diagnostics_document,
            fields=fields,
            expanded=expanded,
            sections=sections,
        )
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"mapping renderer supports schema_version {SUPPORTED_SCHEMA_VERSION!r} "
            f"statement documents and {TASK_SCHEMA_VERSION!r} task documents; "
            f"document declares {version!r}"
        )
    selected = _selected_sections(sections)
    wanted_fields = set(fields) if fields is not None else None

    lines: list[str] = []
    lines.extend(_front_matter(lineage_document))
    lines.append("")
    lines.append(f"# 字段映射文档 {_target_display(lineage_document)}")

    renderers = {
        "overview": lambda: _render_overview(lineage_document),
        "sources": lambda: _render_sources(lineage_document),
        "relations": lambda: _render_relations(lineage_document),
        "mapping": lambda: _render_mapping_table(lineage_document),
        "steps": lambda: _render_steps(lineage_document, wanted_fields, expanded),
        "logic": lambda: _render_logic(lineage_document),
        "graph": lambda: _render_graph(lineage_document),
        "deps": lambda: _render_dependencies(lineage_document),
        "gaps": lambda: _render_gaps(lineage_document, diagnostics_document),
    }
    for index, name in enumerate(SECTION_ORDER, start=1):
        if name not in selected:
            continue
        lines.append("")
        lines.append(f"## {index}. {_SECTION_TITLES[name]}")
        lines.extend(renderers[name]())
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- helpers


def _selected_sections(sections: Iterable[str] | None) -> set[str]:
    if sections is None:
        return set(SECTION_ORDER)
    chosen = set(sections)
    unknown = chosen - set(SECTION_ORDER)
    if unknown:
        raise ValueError(
            f"unknown sections {sorted(unknown)}; valid names: {list(SECTION_ORDER)}"
        )
    return chosen


def _front_matter(document: dict) -> list[str]:
    entries = (
        ("doc_format", DOC_FORMAT),
        ("schema_version", document.get("schema_version")),
        # the contract's top-level `task_id` is a name-based statement identifier
        # (see docs/zh-CN/lineage-json.md), so the document calls it what it is
        ("task_name", document.get("task_id")),
        ("target_table", document.get("target_table")),
        ("stmt_kind", document.get("stmt_kind")),
    )
    lines = ["---"]
    for key, value in entries:
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return lines


def _normalize_inline(text: str) -> str:
    """One fact per line: real newlines inside rendered values become literal ``\\n``."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _field_span(field_id: str) -> str:
    return f"`{_normalize_inline(str(field_id)).replace('`', '')}`"


def _expr_span(expression: str) -> str:
    """Code span that survives backticks inside SQL (`` `t`.`c` ``) and newlines."""
    text = _normalize_inline(str(expression))
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest_run + 1)
    if longest_run:
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _cell(text: str) -> str:
    return _normalize_inline(str(text)).replace("|", "\\|")


def _is_directory_target(document: dict) -> bool:
    return str(document.get("target_table", "")).startswith(_DIRECTORY_TARGET_PREFIX)


def _target_display(document: dict) -> str:
    target = str(document.get("target_table", ""))
    if target.startswith(_DIRECTORY_TARGET_PREFIX):
        return f"（写入目录 {target[len(_DIRECTORY_TARGET_PREFIX):]}）"
    return target


def _is_derived_scope(scope_id: str) -> bool:
    return str(scope_id).startswith(_DERIVED_SCOPE_PREFIXES)


def _informative_physical_pairs(pair: dict) -> list[str]:
    """Physical key pairs that add information beyond the scope-level pair.

    When both join sides pierce to the same physical field (two CTEs reading the same
    table), the pierced pair degenerates to `t.c = t.c` — factually derivable but
    meaningless and misleading to a reader, so self-equal pairs are dropped.
    """
    rendered = []
    for left in pair.get("left_fields") or []:
        for right in pair.get("right_fields") or []:
            left_text = f"{left.get('table')}.{left.get('field')}"
            right_text = f"{right.get('table')}.{right.get('field')}"
            if left_text != right_text:
                rendered.append(f"{left_text} = {right_text}")
    return rendered


def _scope_key_text(pair: dict) -> str:
    """Scope-level key pair, shortened to one column name when both sides share it."""
    left_ref = pair.get("left") or {}
    right_ref = pair.get("right") or {}
    left_name = f"{left_ref.get('qualifier') or left_ref.get('scope')}.{left_ref.get('column')}"
    right_name = (
        f"{right_ref.get('qualifier') or right_ref.get('scope')}.{right_ref.get('column')}"
    )
    if left_ref.get("column") == right_ref.get("column"):
        return str(left_ref.get("column"))
    return f"{left_name} = {right_name}"


# --------------------------------------------------------------------------- sections


def _render_overview(document: dict) -> list[str]:
    lines = [""]
    lines.append(f"- 任务名：{document.get('task_id')}")
    lines.append(f"- 目标：{document.get('target_table')}")
    lines.append(f"- 语句类型：{document.get('stmt_kind')}")
    lines.append(
        f"- 解析状态：{document.get('parse_status')}；语法状态：{document.get('syntax_status')}"
    )
    errors = document.get("syntax_errors") or []
    if errors:
        lines.append(f"- 语法错误：{len(errors)} 条（详见 lineage.json 的 syntax_errors）")
    spec = document.get("target_partition_spec")
    mode = document.get("target_partition_mode")
    if mode == "none":
        mode = None
    columns = document.get("target_partition_columns") or []
    if spec or mode or columns:
        parts = []
        if spec:
            parts.append(f"spec={_expr_span(spec)}")
        if mode:
            parts.append(f"模式={mode}")
        if columns:
            parts.append(f"分区列={'、'.join(columns)}")
        lines.append(f"- 分区：{'；'.join(parts)}")
    binding = document.get("target_field_binding")
    if binding:
        summary = (
            f"- 目标绑定：{binding.get('status')}；方法={binding.get('method')}；"
            f"投影 {binding.get('projection_count')} → 目标列 {binding.get('target_column_count')}；"
            f"纠正 {binding.get('corrected_column_count')}"
        )
        lines.append(summary)
        for issue in binding.get("issues") or []:
            lines.append(f"  - ⚠ 绑定问题：{_normalize_inline(str(issue))}")
    else:
        reason = document.get("target_binding_absent_reason")
        gloss = _BINDING_ABSENT_GLOSSES.get(reason)
        if reason == "target_table_not_found":
            # the only absence with real risk: Spark INSERTs positionally
            lines.append(f"- ⚠ 目标绑定：未做（{gloss}）")
        elif gloss:
            lines.append(f"- 目标绑定：未做（{gloss}）")
        elif reason:
            lines.append(f"- 目标绑定：未做（target_binding_absent_reason={reason}）")
        else:
            lines.append("- 目标绑定：未做目标绑定（文档未给出原因）")
    return lines


# Chinese glosses for target_binding_absent_reason (contract 1.x, added in #92).
_BINDING_ABSENT_GLOSSES = {
    "statement_defines_its_own_columns": "CTAS 建表即定列，无需绑定",
    "binding_not_applicable_for_statement": "MERGE 在绑定机制之外解析目标列",
    "target_is_not_a_table": "写入文件路径，没有可绑定的目标表",
    "metadata_not_provided": "调用方未提供 --target-ddl-metadata",
    "target_table_not_found": "提供了目标 DDL 目录但缺少该表——INSERT 按位置写入，"
    "未绑定的投影可能落错列",
}


def _render_sources(document: dict) -> list[str]:
    lines = [""]
    tables = document.get("source_tables") or []
    if not tables:
        lines.append("- 无物理来源表")
        return lines
    metadata = (document.get("related_metadata") or {}).get("input_tables") or {}
    lines.append("| 表 | 列数（元数据） | 元数据完整 |")
    lines.append("| --- | --- | --- |")
    for table in tables:
        info = metadata.get(table) or {}
        details = info.get("column_details")
        count = str(len(details)) if details is not None else "—"
        complete = (
            "—" if "metadata_complete" not in info
            else ("是" if info.get("metadata_complete") else "否")
        )
        lines.append(f"| {_cell(table)} | {count} | {complete} |")
    return lines


def _join_blocks(document: dict) -> list[tuple[str, dict]]:
    blocks = []
    for scope_id, scope in (document.get("scopes") or {}).items():
        for block in scope.get("logic_blocks") or []:
            if block.get("logic_type") == "join":
                blocks.append((scope_id, block))
    blocks.sort(key=lambda item: str(item[1].get("logic_block_id")))
    return blocks


def _union_scopes(document: dict) -> list[tuple[str, dict]]:
    unions = []
    for scope_id, scope in (document.get("scopes") or {}).items():
        alignment = scope.get("union_branch_alignment")
        if alignment:
            unions.append((scope_id, alignment))
    unions.sort(key=lambda item: item[0])
    return unions


def _render_relations(document: dict) -> list[str]:
    lines = [""]
    joins = _join_blocks(document)
    unions = _union_scopes(document)
    if not joins and not unions:
        lines.append("- 无 JOIN/UNION 关系")
        return lines

    if joins:
        table_rows = _table_level_relation_rows(joins)
        if table_rows:
            lines.append("| 左表 | 关系 | 右表 | 连接键 | 出现 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for (left, join_type, right, keys), count in table_rows:
                lines.append(
                    "| "
                    + " | ".join(
                        (
                            _cell(left),
                            _cell(f"{join_type} JOIN"),
                            _cell(right),
                            _cell("、".join(keys)),
                            f"{count} 处",
                        )
                    )
                    + " |"
                )
        else:
            lines.append(
                "- 各 JOIN 均为中间结果（CTE/子查询）之间的连接，未产生新的表级关系；"
                "scope 级连接明细见第 6 节"
            )
    if unions:
        if joins:
            lines.append("")
        for scope_id, alignment in unions:
            branch_parts = []
            for branch in alignment.get("branches") or []:
                tables = "、".join(branch.get("source_tables") or []) or "（无物理表）"
                branch_parts.append(f"{branch.get('branch_id')} ← {tables}")
            lines.append(
                f"- UNION：{scope_id}（{alignment.get('set_op')}，"
                f"{alignment.get('branch_count')} 分支）；" + "；".join(branch_parts)
            )
    return lines


def _table_level_relation_rows(
    joins: list[tuple[str, dict]],
) -> list[tuple[tuple[str, str, str, tuple[str, ...]], int]]:
    """Aggregate the joins into physical table-pair relationships, deduplicated.

    The overview answers "how do the SOURCE TABLES relate", so:
    - key pairs are pierced to physical fields, grouped per (left table, right table),
      and rendered with short field names (the row already names both tables);
    - a CTE⋈CTE join whose keys pierce to nothing informative contributes no row —
      that relation lives at scope level in the detail;
    - a degraded join between two physical tables keeps its ⚠ 未拆分 marker;
    - identical rows repeated across scopes merge into one row with a count.
    """
    counted: dict[tuple[str, str, str, tuple[str, ...]], int] = {}
    order: list[tuple[str, str, str, tuple[str, ...]]] = []

    def add(row: tuple[str, str, str, tuple[str, ...]]) -> None:
        if row not in counted:
            counted[row] = 0
            order.append(row)
        counted[row] += 1

    for _, block in joins:
        detail = block.get("join_relation_detail") or {}
        join_type = str(detail.get("join_type"))
        left_input = str(detail.get("left_input") or "")
        right_input = str(detail.get("right_input") or "")
        pairs = detail.get("join_key_pairs") or []
        missing = detail.get("missing_reasons") or []
        if not pairs:
            if (
                "missing_join_key_pairs" in missing
                and not _is_derived_scope(left_input)
                and not _is_derived_scope(right_input)
            ):
                add((left_input, join_type, right_input, ("⚠ 未拆分",)))
            continue
        grouped: dict[tuple[str, str], list[str]] = {}
        for pair in pairs:
            for left in pair.get("left_fields") or []:
                for right in pair.get("right_fields") or []:
                    left_table = str(left.get("table"))
                    right_table = str(right.get("table"))
                    left_field = str(left.get("field"))
                    right_field = str(right.get("field"))
                    if left_table == right_table and left_field == right_field:
                        continue  # self-equal artifact of two same-source scopes
                    key = (
                        left_field
                        if left_field == right_field
                        else f"{left_field} = {right_field}"
                    )
                    bucket = grouped.setdefault((left_table, right_table), [])
                    if key not in bucket:
                        bucket.append(key)
        for (left_table, right_table), keys in grouped.items():
            add((left_table, join_type, right_table, tuple(keys)))
    return [(row, counted[row]) for row in order]


def _render_join_detail(scope_id: str, block: dict) -> list[str]:
    detail = block.get("join_relation_detail") or {}
    left = detail.get("left_input")
    right = detail.get("right_input")
    lines = [
        f"- {detail.get('join_type')} JOIN：{_field_span(left)} ⋈ {_field_span(right)}"
        f"（@ {scope_id}；logic_block_id={block.get('logic_block_id')}）"
    ]
    pairs = detail.get("join_key_pairs") or []
    missing = detail.get("missing_reasons") or []
    degraded = not pairs and "missing_join_key_pairs" in missing
    if degraded:
        # Only the degraded path keeps the verbatim ON — the split below it is incomplete,
        # and the display form would resolve a self join's two aliases to the same table.
        condition = detail.get("condition_expression")
        if condition:
            lines.append(f"  - ON：{_expr_span(condition)}")
        lines.append(
            "  - ⚠ missing_join_key_pairs：等值键与过滤条件未能区分"
            "（自连接、ON TRUE 等场景），以下按原文列出"
        )
        label = "连接条件（未拆分）"
    else:
        label = "附加条件"
    for pair in pairs:
        physical = _informative_physical_pairs(pair)
        physical_text = (
            "（物理：" + "、".join(_field_span(item) for item in physical) + "）"
            if physical
            else ""
        )
        lines.append(f"  - 等值键：{_scope_key_text(pair)}{physical_text}")
    for cond in detail.get("condition_filters") or []:
        lines.append(f"  - {label}：{_expr_span(cond.get('expression'))}")
    return lines


def _e2e_sort_key(entry: dict, index: int):
    ordinal = entry.get("target_column_ordinal")
    if ordinal is None:
        ordinal = entry.get("output_ordinal")
    if ordinal is None:
        ordinal = index
    return (ordinal, index)


def _render_mapping_table(document: dict) -> list[str]:
    lines = [""]
    entries = document.get("end_to_end_lineage") or []
    if not entries:
        lines.append("- 无端到端字段映射（end_to_end_lineage 为空）")
        return lines
    ordered = sorted(
        enumerate(entries), key=lambda pair: _e2e_sort_key(pair[1], pair[0])
    )
    rows = []
    any_generated = False
    for row_no, (_, entry) in enumerate(ordered, start=1):
        physical = "、".join(
            f"{ref.get('table')}.{ref.get('column')}"
            for ref in entry.get("physical_sources") or []
        )
        generated = "、".join(
            str(ref.get("value"))
            for ref in entry.get("generated_sources") or []
            if ref.get("value") is not None
        )
        any_generated = any_generated or bool(generated)
        status = "✓" if entry.get("trace_complete") else "⚠ trace_incomplete"
        rows.append(
            (
                str(row_no),
                _cell(entry.get("column")),
                _cell(entry.get("transform")),
                _cell(physical) if physical else "—",
                _cell(generated) if generated else "—",
                status,
            )
        )
    # the 生成来源 column only earns its place when at least one field is constant-fed
    if any_generated:
        header = ("#", "目标字段", "加工类型", "来源物理字段", "生成来源", "状态")
        keep = (0, 1, 2, 3, 4, 5)
    else:
        header = ("#", "目标字段", "加工类型", "来源物理字段", "状态")
        keep = (0, 1, 2, 3, 5)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row[i] for i in keep) + " |")
    return lines


def _chain_title(document: dict, chain: dict) -> str:
    field = chain.get("target_field")
    target = str(document.get("target_table", ""))
    if _is_directory_target(document):
        path = target[len(_DIRECTORY_TARGET_PREFIX):]
        return f"### 字段 {field}（写入目录 {path}）"
    if document.get("stmt_kind") == "MERGE":
        branch, when_index = _merge_branch_for_position(
            document, chain.get("target_position")
        )
        if branch is not None:
            return f"### 字段 {target}.{field}（merge:{branch} 分支 {when_index}）"
    return f"### 字段 {target}.{field}"


def _merge_branch_for_position(document: dict, position) -> tuple:
    for entry in document.get("end_to_end_lineage") or []:
        if entry.get("output_ordinal") == position and "merge_branch" in entry:
            return entry.get("merge_branch"), entry.get("merge_when_index")
    return None, None


def _display_expression_for_step(document: dict, step: dict) -> str:
    scope_id = str(step.get("scope_id"))
    output_field = str(step.get("output_field"))
    scope = (document.get("scopes") or {}).get(scope_id) or {}
    name = None
    if output_field.startswith(scope_id + "."):
        name = output_field[len(scope_id) + 1:]
    candidates = []
    for output in scope.get("outputs") or []:
        if name is not None and output.get("name") == name:
            candidates.append(output)
        elif name is None and output_field in (output.get("target_columns") or []):
            candidates.append(output)
    expression_sql = step.get("expression_sql")
    for output in candidates:
        if output.get("expression") == expression_sql and output.get("display_expression"):
            return output["display_expression"]
    for output in candidates:
        if output.get("display_expression"):
            return output["display_expression"]
    return str(expression_sql or "")


def _render_steps(
    document: dict, wanted_fields: set | None, expanded: bool
) -> list[str]:
    lines: list[str] = []
    chains = document.get("field_mapping_chains") or []
    rendered_any = False
    for chain in chains:
        if wanted_fields is not None and chain.get("target_field") not in wanted_fields:
            continue
        rendered_any = True
        lines.append("")
        lines.append(_chain_title(document, chain))
        lines.append("")
        physical_roots = []
        constant_roots = []
        for root in chain.get("root_source_fields") or []:
            root_text = str(root)
            if root_text.startswith("CONSTANT."):
                constant_roots.append(root_text[len("CONSTANT."):])
            else:
                physical_roots.append(root_text)
        if physical_roots:
            lines.append(
                "- 来源字段：" + "、".join(_field_span(item) for item in physical_roots)
            )
        else:
            lines.append("- 来源字段：无（生成/常量字段）")
        if constant_roots:
            lines.append(
                "- 常量来源：" + "、".join(_expr_span(item) for item in constant_roots)
            )
        steps = chain.get("ordered_steps") or []
        step_types: list[str] = []
        for step in steps:
            step_type = str(step.get("step_type"))
            if step_type not in step_types:
                step_types.append(step_type)
        lines.append(f"- 加工路径：{len(steps)} 步；{', '.join(step_types)}")
        status = chain.get("trace_status")
        if status and status != "complete":
            reasons = "、".join(chain.get("missing_reasons") or []) or "未给出原因"
            lines.append(f"- ⚠ trace_status={status}: {reasons}")
        total = len(steps)
        for step in steps:
            inputs = "、".join(
                _field_span(item) for item in step.get("input_fields") or []
            )
            grain = (
                "；粒度=changed" if step.get("grain_effect") == "changed" else ""
            )
            expression = _display_expression_for_step(document, step)
            lines.append(
                f"- 步骤 {step.get('step_no')}/{total}：{inputs} → "
                f"{_field_span(step.get('output_field'))}；{step.get('step_type')}"
                f"{grain}；表达式：{_expr_span(expression)}"
            )
            if expanded and step.get("expanded_expression"):
                lines.append(
                    f"  - 展开表达式：{_expr_span(step['expanded_expression'])}"
                )
        lines.append(
            f"- 证据：mapping_chain_id={chain.get('mapping_chain_id')}；"
            f"chain={chain.get('chain_id')}"
        )
    if not rendered_any:
        lines.append("")
        lines.append("- 无字段映射链（field_mapping_chains 为空或均被过滤）")
    return lines


def _joins_by_profile_scope(
    document: dict, profile_scope_ids: set,
) -> tuple[dict[str, list[tuple[str, dict]]], list[tuple[str, dict]]]:
    """Attach each join block to the profile step that should host its detail.

    Union-branch scopes (``union:x:bNN``) are folded out of scope_profile, so their
    joins fall back to the parent union scope; anything still unmatched is returned
    separately so no join fact is silently dropped.
    """
    attached: dict[str, list[tuple[str, dict]]] = {}
    leftovers: list[tuple[str, dict]] = []
    for scope_id, block in _join_blocks(document):
        host = None
        if scope_id in profile_scope_ids:
            host = scope_id
        elif scope_id.startswith("union:"):
            parent = scope_id.rsplit(":", 1)[0]
            if parent in profile_scope_ids:
                host = parent
        if host is None:
            leftovers.append((scope_id, block))
        else:
            attached.setdefault(host, []).append((scope_id, block))
    return attached, leftovers


def _render_logic(document: dict) -> list[str]:
    lines = [""]
    profile = document.get("scope_profile") or {}
    steps = profile.get("steps") or []
    profile_scope_ids = {step.get("scope_id") for step in steps}
    attached_joins, leftover_joins = _joins_by_profile_scope(document, profile_scope_ids)
    if not steps:
        if not leftover_joins:
            lines.append("- 无 scope_profile（文档未携带或为空）")
            return lines
        lines.append("- 无 scope_profile（文档未携带或为空）；连接明细如下")
        for scope_id, block in leftover_joins:
            lines.extend(_render_join_detail(scope_id, block))
        return lines
    first = True
    for step in steps:
        if not first:
            lines.append("")
        first = False
        lines.append(
            f"### scope {_field_span(step.get('scope_id'))}"
            f"（{step.get('kind')}，角色 {step.get('role')}）"
        )
        lines.append("")
        summary = step.get("business_summary")
        if summary:
            lines.append(f"- 概要：{_normalize_inline(str(summary))}")
        inputs = "、".join(step.get("direct_inputs") or []) or "—"
        physical = "、".join(step.get("physical_source_tables") or []) or "—"
        lines.append(f"- 输入：{inputs}；物理上游：{physical}")
        logic = step.get("logic") or {}
        stats = (
            f"- 逻辑：join {len(logic.get('joins') or [])}、"
            f"filter {len(logic.get('filters') or [])}、"
            f"聚合 {len(logic.get('aggregations') or [])}、"
            f"窗口 {len(logic.get('window_functions') or [])}、"
            f"union 分支 {logic.get('union_branches') or 0}、"
            f"distinct {'是' if logic.get('distinct') else '否'}"
        )
        lines.append(stats)
        for cond in logic.get("filters") or []:
            lines.append(f"  - 过滤：{_expr_span(cond)}")
        for scope_id, block in attached_joins.get(step.get("scope_id"), []):
            lines.extend(_render_join_detail(scope_id, block))
    if leftover_joins:
        lines.append("")
        lines.append("### 其他连接（所属 scope 未列入概览）")
        lines.append("")
        for scope_id, block in leftover_joins:
            lines.extend(_render_join_detail(scope_id, block))
    return lines


def _render_graph(document: dict) -> list[str]:
    lines = [""]
    graph = document.get("scope_graph") or {}
    nodes = sorted(graph.get("nodes") or [])
    if not nodes:
        lines.append("- 无 scope_graph")
        return lines
    node_ids = {node: f"n{index}" for index, node in enumerate(nodes)}
    physical = set(document.get("source_tables") or [])
    lines.append("```mermaid")
    lines.append("flowchart LR")
    for node in nodes:
        label = node.replace('"', "'")
        lines.append(f'    {node_ids[node]}["{label}"]')
    edges = sorted(
        graph.get("edges") or [],
        key=lambda edge: (str(edge.get("from")), str(edge.get("to"))),
    )
    for edge in edges:
        source = node_ids.get(edge.get("from"))
        target = node_ids.get(edge.get("to"))
        if source and target:
            lines.append(f"    {source} --> {target}")
    physical_ids = [node_ids[node] for node in nodes if node in physical]
    if physical_ids:
        lines.append("    classDef physical fill:#e8f0fe,stroke:#4a6fa5")
        lines.append(f"    class {','.join(physical_ids)} physical")
    lines.append("```")
    return lines


def _render_dependencies(document: dict) -> list[str]:
    lines = [""]
    deps = document.get("task_dependencies") or {}
    upstream = deps.get("upstream_tasks") or []
    downstream = deps.get("downstream_tasks") or []
    if not upstream and not downstream:
        lines.append("- 无声明的任务依赖")
        return lines
    for label, items in (("上游", upstream), ("下游", downstream)):
        if items:
            rendered = "、".join(
                f"{item.get('task_name')}（{item.get('task_id')}）" for item in items
            )
            lines.append(f"- {label}：{rendered}")
    return lines


def _render_gaps(document: dict, diagnostics: dict | None) -> list[str]:
    """Only facts that change how much the reader may trust the lineage live here.

    Parse-process warnings are informational; their full text goes to the sibling
    warnings.md, and this section keeps a counted pointer.
    """
    lines = [""]
    incomplete = [
        entry.get("column")
        for entry in document.get("end_to_end_lineage") or []
        if not entry.get("trace_complete")
    ]
    if incomplete:
        lines.append(
            "- ⚠ 追溯不完整字段：" + "、".join(str(item) for item in incomplete)
        )
    else:
        lines.append("- 字段追溯：全部完整")
    if diagnostics is None:
        lines.append("- ⚠ 无 diagnostics 文档（未随 lineage.json 提供，告警与缺口未知）")
        return lines
    gaps = diagnostics.get("lineage_fact_gaps") or []
    if not gaps:
        lines.append("- 缺口：无（diagnostics 未记录 lineage_fact_gaps）")
    else:
        for gap in gaps:
            scalars = "；".join(
                f"{key}={_normalize_inline(str(value))}"
                for key, value in sorted(gap.items())
                if isinstance(value, (str, int, float, bool))
            )
            lines.append(f"- ⚠ 缺口：{scalars}")
    warnings = diagnostics.get("warnings") or []
    if warnings:
        lines.append(
            f"- 解析警告：{len(warnings)} 条（提示类信息，见同目录 warnings.md）"
        )
    else:
        lines.append("- 解析警告：无")
    return lines


# Chinese one-line glosses for the known warning types, shown as the group subtitle in
# warnings.md. Unknown types render with the type name alone.
_WARNING_GLOSSES = {
    "magic_number": "表达式中出现未命名常量（魔法数字），不影响血缘，建议结合业务口径确认",
    "complex_aggregate_with_case": "聚合函数内嵌 CASE WHEN，口径较复杂，建议人工复核",
    "filter_in_join_on_clause": "JOIN ON 中混有过滤条件（非连接键），注意连接语义",
    "unresolved_unqualified_no_schema": "未限定列缺少 schema 元数据，来源无法证明",
    "unsupported_statement": "脚本中存在未建模的语句，该语句被跳过",
    "duplicate_alias": "同一查询块中别名重复",
    "output_alias_missing_or_ambiguous": "输出列缺少别名或别名歧义，目标列名按位置确定",
    "duplicate_table_in_union": "UNION 分支中出现重复表",
    "target_field_binding_fallback": "目标字段绑定未能使用 DDL 权威顺序，回退到投影别名",
    "star_not_expanded": "SELECT * 未能展开（缺少 schema 元数据）",
    "ambiguous_unqualified": "未限定列有多个可行来源，保持歧义未归属",
}

WARNINGS_DOC_FORMAT = "warnings-md/1"


def _render_task_document(
    task_document: dict,
    diagnostics_document: dict | None,
    *,
    fields: Iterable[str] | None,
    expanded: bool,
    sections: Iterable[str] | None,
) -> str:
    statement_lineage = task_document.get("statement_lineage") or {}
    ordered_ids = [
        str(statement.get("statement_id") or "")
        for statement in task_document.get("statement_sequence") or []
        if statement.get("statement_id") in statement_lineage
    ]
    # Entries no statement_sequence row points at still render, after the ordered ones.
    ordered_ids.extend(sid for sid in statement_lineage if sid not in set(ordered_ids))

    lines = [
        f"# 任务字段映射：{task_document.get('task_id') or ''}",
        "",
        f"共 {len(ordered_ids)} 条写入语句；每节为一条语句的完整映射文档。",
    ]
    for statement_id in ordered_ids:
        entry = statement_lineage.get(statement_id) or {}
        lines.append("")
        lines.append(f"## {statement_id}")
        lines.append("")
        lines.append(
            render_mapping_markdown(
                entry,
                diagnostics_document,
                fields=fields,
                expanded=expanded,
                sections=sections,
            )
        )
    return "\n".join(lines)


def render_warnings_markdown(
    diagnostics_document: dict | None,
    lineage_document: dict | None = None,
) -> str | None:
    """Render the parse warnings of one statement as a standalone warnings.md.

    Returns None when there is nothing to report, so callers skip writing a file.
    Grouped by warning type, each group carrying a one-line Chinese gloss; the
    verbatim messages stay inside code spans.
    """
    warnings = (diagnostics_document or {}).get("warnings") or []
    if not warnings:
        return None
    lineage_document = lineage_document or {}
    lines = ["---"]
    entries = (
        ("doc_format", WARNINGS_DOC_FORMAT),
        ("schema_version", (diagnostics_document or {}).get("schema_version")),
        ("task_name", lineage_document.get("task_id")),
        ("target_table", lineage_document.get("target_table")),
    )
    for key, value in entries:
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# 解析警告 {_target_display(lineage_document)}".rstrip())
    lines.append("")
    lines.append(
        f"共 {len(warnings)} 条。这些是解析过程的提示与降级说明，不改变 lineage.json "
        "已证明的事实；影响血缘结论的信息在 mapping.md 的「不确定性与缺口」一节。"
    )

    grouped: dict[str, list[dict]] = {}
    for warning in warnings:
        grouped.setdefault(str(warning.get("type")), []).append(warning)
    for warning_type in sorted(grouped):
        group = grouped[warning_type]
        lines.append("")
        lines.append(f"## {warning_type}（{len(group)} 条）")
        lines.append("")
        gloss = _WARNING_GLOSSES.get(warning_type)
        if gloss:
            lines.append(f"{gloss}。")
            lines.append("")
        for warning in group:
            scope = warning.get("scope")
            location = f"@ {scope}：" if scope else ""
            lines.append(f"- {location}{_expr_span(warning.get('msg', ''))}")
    lines.append("")
    return "\n".join(lines)
