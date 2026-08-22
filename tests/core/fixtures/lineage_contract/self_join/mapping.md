---
doc_format: "mapping-md/1"
schema_version: "1.0"
task_name: "golden_self_join"
target_table: "mart.node_edges"
stmt_kind: "INSERT"
---

# 字段映射文档 mart.node_edges

## 1. 概览

- 任务名：golden_self_join
- 目标：mart.node_edges
- 语句类型：INSERT
- 解析状态：ok；语法状态：strict_ok
- 目标绑定：未做（调用方未提供 --target-ddl-metadata）

## 2. 来源表

| 表 | 列数（元数据） | 元数据完整 |
| --- | --- | --- |
| ods.nodes | 3 | 是 |

## 3. 来源表关系

| 左表 | 关系 | 右表 | 连接键 | 出现 |
| --- | --- | --- | --- | --- |
| ods.nodes | INNER JOIN | ods.nodes | parent_id = id | 1 处 |

## 4. 字段映射总表

| # | 目标字段 | 加工类型 | 来源物理字段 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | id | DIRECT | ods.nodes.id | ✓ |
| 2 | parent_id | DIRECT | ods.nodes.id | ✓ |

## 5. 加工步骤明细

### 字段 mart.node_edges.id

- 来源字段：`ods.nodes.id`
- 加工路径：1 步；direct_projection
- 步骤 1/1：`ods.nodes.id` → `mart.node_edges.id`；direct_projection；表达式：`` `id` ``
- 证据：mapping_chain_id=mc:001；chain=chain:ROOT:id:position:0

### 字段 mart.node_edges.parent_id

- 来源字段：`ods.nodes.id`
- 加工路径：1 步；direct_projection
- 步骤 1/1：`ods.nodes.id` → `mart.node_edges.parent_id`；direct_projection；表达式：`` `id` ``
- 证据：mapping_chain_id=mc:002；chain=chain:ROOT:parent_id:position:1

## 6. 加工逻辑汇总

### scope `ROOT`（root，角色 join）

- 概要：读取 ods.nodes；关联 1 个上游
- 输入：ods.nodes；物理上游：ods.nodes
- 逻辑：join 1、filter 0、聚合 0、窗口 0、union 分支 0、distinct 否
- INNER JOIN：`ods.nodes` ⋈ `ods.nodes`（@ ROOT；logic_block_id=logic:ROOT:join:001）
  - 等值键：a.parent_id = b.id（物理：`ods.nodes.parent_id = ods.nodes.id`）
  - 等值键：batch_id

## 7. scope 结构图

```mermaid
flowchart LR
    n0["ROOT"]
    n1["ods.nodes"]
    n1 --> n0
    classDef physical fill:#e8f0fe,stroke:#4a6fa5
    class n1 physical
```

## 8. 任务依赖

- 无声明的任务依赖

## 9. 不确定性与缺口

- 字段追溯：全部完整
- 缺口：无（diagnostics 未记录 lineage_fact_gaps）
- 解析警告：无
