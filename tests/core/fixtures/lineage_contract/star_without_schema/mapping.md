---
doc_format: "mapping-md/1"
schema_version: "1.0"
task_name: "golden_star_without_schema"
target_table: "mart.raw_copy"
stmt_kind: "INSERT"
---

# 字段映射文档 mart.raw_copy

## 1. 概览

- 任务名：golden_star_without_schema
- 目标：mart.raw_copy
- 语句类型：INSERT
- 解析状态：ok；语法状态：strict_ok
- 目标绑定：未做（调用方未提供 --target-ddl-metadata）

## 2. 来源表

| 表 | 列数（元数据） | 元数据完整 |
| --- | --- | --- |
| ods.raw_events | 1 | 否 |

## 3. 来源表关系

- 无 JOIN/UNION 关系

## 4. 字段映射总表

| # | 目标字段 | 加工类型 | 来源物理字段 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | * | EXPAND_ALL | ods.raw_events.* | ⚠ trace_incomplete |

## 5. 加工步骤明细

### 字段 mart.raw_copy.*

- 来源字段：`ods.raw_events.*`
- 加工路径：1 步；expression
- 步骤 1/1：`ods.raw_events.*` → `mart.raw_copy.*`；expression；表达式：`*`
- 证据：mapping_chain_id=mc:001；chain=chain:ROOT:*:position:0

## 6. 加工逻辑汇总

### scope `ROOT`（root，角色 transform）

- 概要：读取 ods.raw_events
- 输入：ods.raw_events；物理上游：ods.raw_events
- 逻辑：join 0、filter 0、聚合 0、窗口 0、union 分支 0、distinct 否

## 7. scope 结构图

```mermaid
flowchart LR
    n0["ROOT"]
    n1["ods.raw_events"]
    n1 --> n0
    classDef physical fill:#e8f0fe,stroke:#4a6fa5
    class n1 physical
```

## 8. 任务依赖

- 无声明的任务依赖

## 9. 不确定性与缺口

- ⚠ 追溯不完整字段：*
- ⚠ 缺口：evidence_path=lineage.scopes.ROOT.outputs[0]；expression_sql=*；gap_bucket=wildcard_projection；gap_id=lineage_gap:0001；gap_type=projection_wildcard_unexpanded；needed_fact=source schema for wildcard expansion；object_name=*；object_type=output；owner_hint=metadata_provider；root_impact=True；scope_id=ROOT
- 解析警告：1 条（提示类信息，见同目录 warnings.md）
