# Scope Lineage 文档导航

Scope Lineage 把 Spark/Hive SQL 转换成两类机器可消费的事实：

- `lineage.json`：SQL 已经证明的结构、逻辑和字段来源；
- `diagnostics.json`：解析过程中发现的不确定性、降级和事实缺口。

如果你第一次接触项目，建议按下面顺序阅读：

1. [项目 README](../../README.zh-CN.md)：先了解工具解决什么问题、产物有什么价值；
2. [安装与使用指南](getting-started.md)：完成安装、第一次解析，并了解常用 CLI 和 Python API；
3. [Scope Lineage：把复杂 SQL 还原成可验证的字段加工链](value-and-use-cases.md)：通过完整案例了解加工血缘、可验证血缘和实际使用价值；
4. [输入格式](input-formats.md)：了解 SQL、任务 JSON、Schema 和目标表元数据怎么传入；
5. [v1 还是 v2？按业务场景选契约](contract-selection.md)：字段血缘、加工步骤分析用默认 1.0；审计、事故排查、最终表状态用 2.0——先选对契约再读细节；
6. [`lineage.json` 输出契约](lineage-json.md)：逐层理解顶层字段、scope、逻辑块、字段映射链和端到端血缘；
7. [`diagnostics.json` 输出契约](diagnostics-json.md)：理解 warning、事实缺口以及什么结果不能当成已证明事实。
8. [Task Lineage 2.0](task-lineage-v2.md)：理解 DELETE/TRUNCATE/UPDATE、行集合影响和多语句最终表状态。
9. [mapping.md 字段映射文档](mapping-doc.md)：用 `scope-lineage render` 把契约渲染成对人和机器都可读的映射文档。

## 从问题找到字段

| 你要回答的问题 | 优先读取的位置 |
| --- | --- |
| 任务写入哪张表、使用什么写入方式？ | `target_table`、`stmt_kind`、`target_partition_*` |
| 任务读取了哪些物理表？ | `source_tables` |
| CTE、子查询和 UNION 如何连接？ | `scope_graph`、`scopes.<scope_id>.depends_on` |
| 某个查询块做了什么？ | `scopes.<scope_id>.logic_blocks[]`、`scope_profile.steps[]` |
| 某个输出字段的 SQL 表达式是什么？ | `scopes.ROOT.outputs[]` |
| 目标字段最终来自哪些物理字段？ | `end_to_end_lineage[]` |
| 字段经过了哪些查询块和变换步骤？ | `field_mapping_chains[].ordered_steps[]` |
| JOIN 的 key 和附加过滤是什么？ | `logic_blocks[].join_relation_detail` |
| 聚合的 group by、指标表达式是什么？ | `logic_blocks[].aggregation_detail` |
| 窗口函数如何分区、排序？ | `logic_blocks[].window_specification` |
| `SELECT *` 是否真正展开？ | `scopes.*.outputs[]`、`diagnostics.json.warnings[]` |
| 某条血缘是否完整可信？ | `trace_complete` / `trace_status`、`missing_reasons`、`ambiguities` |
| 为什么无法确定字段来源？ | `diagnostics.json.lineage_fact_gaps[]` |
| DELETE/TRUNCATE 如何影响最终表？ | v2 `statement_sequence[]`、`table_state_graph`、`final_table_states` |

## 事实层与上层知识的边界

Core 输出的是可追溯事实，不直接生成业务结论。例如：

- Core 可以证明 `paid_amount_30d` 使用 `SUM(CASE WHEN ...)`，来自 `dwd.order_detail.pay_amount`；
- 上层 Agent 可以基于该事实解释“近 30 天已支付金额”；
- Core 不会仅凭字段名猜测这是收入、风险或客户价值指标。

这种边界让 AI 的回答可以回到 SQL 表达式、scope、字段和诊断证据，而不是依赖一次不可复核的自然语言猜测。
