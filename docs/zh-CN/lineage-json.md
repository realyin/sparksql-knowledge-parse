# `lineage.json` 输出契约与字段说明

本文说明默认 `schema_version: "1.0"` 的单条写语句契约。显式使用
`--contract-version 2.0` 时，文件改为任务级有序状态契约，字段定义见
[Task Lineage 2.0](task-lineage-v2.md)，权威 Schema 为
`scope_lineage/schemas/lineage-v2.schema.json`。
不确定自己的场景该用哪份契约，先读[按业务场景选契约](contract-selection.md)：
字段血缘、加工步骤分析用本契约即可；涉及语句顺序、DELETE/TRUNCATE、最终表状态才需要 2.0。

## 1. 它到底输出什么

`lineage.json` 不是一组简单的“输入表 → 输出表”边，而是一份 SQL 任务的结构化事实文档。它同时回答五类问题：

1. **任务事实**：写入哪张表、使用什么语句、分区方式是什么；
2. **结构事实**：SQL 被拆成哪些 CTE、子查询、UNION 分支和 ROOT 查询块；
3. **逻辑事实**：每个查询块执行了哪些 JOIN、过滤、聚合、窗口和字段表达式；
4. **字段事实**：每个目标字段经过哪些 scope，最终来自哪些物理字段或生成值；
5. **可信度事实**：血缘是否完整、哪里存在歧义、还缺什么证据。

权威 JSON Schema 位于：

```text
scope_lineage/schemas/lineage.schema.json
```

文档用于解释字段语义和消费方法，Schema 用于判断结构是否合法。两者冲突时，以当前版本 Schema 和实际序列化代码为准。

## 2. 从 SQL 到事实：一个最小例子

输入：

```sql
INSERT OVERWRITE TABLE mart.customer_summary PARTITION (dt='${bizdate}')
SELECT
  c.customer_id,
  COUNT(DISTINCT o.order_id) AS order_count
FROM ods.customer c
LEFT JOIN dwd.order_detail o
  ON c.customer_id = o.customer_id
WHERE c.dt = '${bizdate}'
GROUP BY c.customer_id;
```

`lineage.json` 会表达为：

```json
{
  "schema_version": "1.0",
  "task_id": "customer_summary",
  "target_table": "mart.customer_summary",
  "stmt_kind": "INSERT_OVERWRITE",
  "parse_status": "ok",
  "syntax_status": "strict_ok",
  "target_partition_spec": {"dt": "${bizdate}"},
  "target_partition_columns": ["dt"],
  "target_partition_mode": "static",
  "source_tables": ["dwd.order_detail", "ods.customer"],
  "scope_graph": {
    "nodes": ["ROOT", "dwd.order_detail", "ods.customer"],
    "edges": [
      {"from": "ods.customer", "to": "ROOT"},
      {"from": "dwd.order_detail", "to": "ROOT"}
    ]
  },
  "end_to_end_lineage": [
    {
      "column": "order_count",
      "transform": "AGGREGATE",
      "trace_complete": true,
      "physical_sources": [
        {"table": "dwd.order_detail", "column": "order_id", "transform": "AGGREGATE"}
      ]
    }
  ]
}
```

这个片段省略了完整 `scopes` 和 `field_mapping_chains`。真实输出还会保留 JOIN 条件、过滤字段、GROUP BY、原始表达式、字段变换步骤和诊断摘要。

## 3. 顶层对象：key 和 value

### 3.1 完整顶层字段表

| Key | Value 类型 | 必填 | 含义与使用方式 |
| --- | --- | --- | --- |
| `schema_version` | string | 是 | 本文所述默认契约固定为 `1.0`。消费者先检查 major 版本。 |
| `task_id` | string | 是 | 本条写表语句的任务标识；批量输入和多语句任务可能基于输入名生成独立标识。**不要用它关联 v1 与 v2 产物**：多写入脚本下 v1 按写入序号加后缀（`task#0`、`task#1`），v2 按脚本位置（`task#1`、`task#3`），同一个 `task#1` 在两份产物里指向不同语句。关联键是下面的 `statement_id`。 |
| `statement_id` | string | 条件输出 | 脚本位置形式 `stmt:NNN`（如 `stmt:002`），与 v2 `statement_sequence[].statement_id` **对同一条语句取值相同**——这是 v1 与 v2 产物之间**唯一被指定的关联键**。经脚本文本解析（CLI、`parse_all_scope_lineage`、`parse_scope_lineage` 传 SQL）时输出；调用方直接传入已解析 AST（`tree=`）时不输出——那时脚本位置不可知，猜一个会静默匹配到错误的语句。 |
| `statement_index` | integer | 条件输出 | 零基脚本位置，计入脚本中**全部**语句（含 SET、DELETE 等未建模语句），与 v2 `statement_sequence[].statement_index` 同口径。与 `statement_id` 同出现、同缺席。 |
| `target_table` | string | 是 | SQL 实际写入的目标，如 `mart.customer_summary`。`INSERT OVERWRITE DIRECTORY` 写的是文件路径而不是表，此时取值形如 `directory:/warehouse/export/daily`，带 `directory:` 前缀。**消费者登记仓库表时应先排除这类取值**；这类语句的血缘照常产出，且因为目标不是表，`target_field_binding` 不会出现。 |
| `stmt_kind` | enum string | 是 | `INSERT_OVERWRITE`、`INSERT`、`CTAS`、`MERGE` 或 `UNKNOWN`。注意字段名不是 `statement_type`。 |
| `is_session_scoped_relation` | boolean | 否 | 仅在为 `true` 时出现。该语句产出的关系只存活于会话、不落存储：`TEMP VIEW`、`GLOBAL TEMP VIEW`、`CACHE [LAZY] TABLE` 都属此列。**消费者不应据此登记仓库中新增了一张表**，统计表级覆盖时也应先排除。判据取自 AST 事实而非命名模式：不带 `TEMPORARY` 的 `CREATE VIEW` 会注册进 catalog 并跨会话存活，因此**不**带此标记。`is_cached_relation` 是本字段在 CACHE 语法上的既有子集，含义不变。 |
| `parse_status` | enum string | 是 | `ok` 表示形成了可校验 Lineage 文档；`failed` 表示解析失败，不能消费正常血缘。 |
| `syntax_status` | enum string | 是 | `strict_ok`、`recovered` 或 `failed`。`recovered` 表示解析器经过恢复，必须同时读诊断。 |
| `syntax_errors` | array<object> | 是 | 语法错误或恢复证据。元素可含 `description`、`line`、`col` 和上下文片段。 |
| `skipped_statements` | array<object> | 条件输出 | v1 未作为投影写入建模的顶层语句。包含稳定的 `statement_id`、零基 `statement_index`、`statement_kind`、`category`、`model_status`、`reason`、`normalized_sql` 和支持范围说明；行变更应改用 v2 建模。**`category` 为 `control_statement`（如 `SET`）或 `empty_statement` 的语句是设计上忽略的，只记录、不再发 `unsupported_statement` 告警**——要知道被忽略了什么，读本字段（它只出现在 `lineage.json`，不在 `diagnostics.json` 里）。<br>单语句 API `parse_scope_lineage` 只建模脚本中的**第一条**写表语句：之后的写语句以 `category: additional_write_statement`、`model_status: not_modeled` 记录在此（附 `target_table`），并发一条 `additional_write_statements_not_modeled` 告警列出未建模的目标。这些语句本身是受支持的——要全部建模，用 `parse_all_scope_lineage`（CLI 即此口径）或契约 2.0。 |
| `target_partition_spec` | object | 是 | 分区名到分区值的映射。动态分区的 value 可以为 `null`。 |
| `target_partition_columns` | array<string> | 是 | 目标表分区列名。 |
| `target_partition_mode` | enum string | 是 | `none`、`static`、`dynamic` 或 `mixed`，描述的是 **`PARTITION(...)` 子句的写法**：给了值是 `static`、没给值是 `dynamic`、没有该子句是 `none`。**它与会话配置 `spark.sql.sources.partitionOverwriteMode` 无关**，也不表示这次覆写会删掉多少数据——两者名字相近但含义不同。覆写的实际影响范围由 v2 的 `effect.rowset_effect` 表达，见 task-lineage-v2.md。 |
| `target_field_binding` | object | 条件输出 | 提供目标表 DDL/Schema 时输出，说明目标字段是否按权威顺序绑定。 |
| `target_binding_absent_reason` | enum string | 条件输出 | **仅在没有 `target_field_binding` 时出现**，说明是四种情形中的哪一种。`statement_defines_its_own_columns`（CTAS：建表即定列）、`binding_not_applicable_for_statement`（MERGE：在绑定之外解析目标列）、`target_is_not_a_table`（写文件路径）、`metadata_not_provided`（调用方未传 `--target-ddl-metadata`）、**`target_table_not_found`（传了目录但缺这张表——只有这一种有风险**：Spark 的 `INSERT ... SELECT` 按位置写入，未绑定的投影可能落到错的列）。<br>两处**不会出现**该键：解析失败的语句（`parse_status: "failed"`），以及少数在解析早期返回、未走到绑定环节的语句——消费者不能假定该集合对产物封闭。<br>MERGE 的注意点：传了目标 DDL 时 `*` 分支的列名取自该 DDL、按目标顺序；未传时回落到源列名。两者都归为 `binding_not_applicable_for_statement`，产物中不区分。 |
| `task_dependencies` | object | 是 | 从任务 JSON 保留的上游、下游任务声明，以及依赖来源摘要。 |
| `source_tables` | array<string> | 是 | 解析得到的全部物理输入表去重列表。适合表级检索和初步影响分析。 |
| `related_metadata` | object | 是 | 输入表、输出表的字段类型、注释及元数据完整性观察结果。 |
| `partition_columns` | array<string> | 否 | 兼容性字段；记录解析过程中识别的分区列。新消费者优先使用 `target_partition_columns`。 |
| `scopes` | object/map | 是 | key 是 scope ID，value 是该查询块的完整事实。它是最详细的 SQL 结构层。 |
| `scope_graph` | object | 是 | `nodes[]` 是 scope/物理表节点，`edges[]` 表示数据从 `from` 流向 `to`。 |
| `field_mapping_chains` | array<object> | 是 | 每个目标字段的有序变换链，解释字段如何跨 scope 到达最终目标。 |
| `scope_profile` | object | 是 | 从 scope 事实生成的确定性简表，适合索引、检索和给 AI 提供低成本上下文。 |
| `end_to_end_lineage` | array<object> | 是 | 每个最终目标字段到物理字段、常量或行集来源的汇总血缘。 |
| `diagnostics` | object | 是 | 完整诊断的摘要：warning 数、缺口数、类型分布、样本和统计。 |

### 3.2 为什么同时有 `scopes`、`field_mapping_chains` 和 `end_to_end_lineage`

三者不是重复字段：

| 层级 | 回答的问题 | 典型消费者 |
| --- | --- | --- |
| `scopes` | 这段 SQL 在每个查询块里具体做了什么？ | SQL 解释、逻辑审查、代码导航 |
| `field_mapping_chains` | 某个字段按什么顺序穿过多个查询块？ | 证据展示、字段变换解释、调试 |
| `end_to_end_lineage` | 最终字段确定来自哪些物理字段？ | 影响分析、搜索索引、知识图谱边 |

只读取 `end_to_end_lineage` 可以快速建图；需要解释“为什么”时，再沿 `field_mapping_chains` 和 `scopes` 回看证据。

## 4. 标识符和引用规则

### 4.1 Scope ID

常见 ID：

| 形式 | 含义 |
| --- | --- |
| `ROOT` | 最终写入目标表的顶层 SELECT/MERGE 投影。 |
| `cte:<name>` | CTE 查询块，如 `cte:order_summary`。 |
| `subq:<name>` | 子查询查询块。 |
| `union:<name>` | UNION 组合查询块。 |
| `union:<name>:b01` | UNION 的第一个分支。 |
| `<database>.<table>` | scope graph 中的物理表节点。 |

`scopes` 是一个 JSON object，不是数组：

```json
{
  "scopes": {
    "cte:order_summary": {"kind": "cte", "depends_on": ["dwd.order_detail"]},
    "ROOT": {"kind": "root", "depends_on": ["cte:order_summary", "ods.customer"]}
  }
}
```

这里 key `cte:order_summary` 是稳定引用，value 是该查询块的事实。`scope_graph.edges[].from/to`、字段来源中的 `scope`、逻辑 ID 中的 scope 片段都会引用这些 ID。

### 4.2 其他稳定引用

| ID | 示例 | 用途 |
| --- | --- | --- |
| `input_ref_id` | `input:ROOT:001` | 区分同一 scope 中每一次 FROM/JOIN 输入，避免同表多次引用混淆。 |
| `logic_block_id` | `logic:ROOT:join:001` | 定位 JOIN、filter、aggregate、window 等逻辑块。 |
| `mapping_chain_id` | `mc:001` | 文档内简短映射链 ID。 |
| `chain_id` | `chain:ROOT:customer_id:position:0` | 带目标 scope、字段和位置的语义 ID。 |
| `gap_id` | `lineage_gap:0001` | `diagnostics.json` 中事实缺口 ID。 |

## 5. `scope_graph`：查询块依赖图

结构：

```json
{
  "scope_graph": {
    "nodes": ["ROOT", "cte:order_summary", "dwd.order_detail"],
    "edges": [
      {"from": "dwd.order_detail", "to": "cte:order_summary"},
      {"from": "cte:order_summary", "to": "ROOT"}
    ]
  }
}
```

- `nodes[]`：物理表和逻辑 scope 的全集；
- `edges[].from`：数据提供方；
- `edges[].to`：消费该数据的查询块。

这张图保留了中间结构。简单表血缘只会得到 `dwd.order_detail → mart.customer_summary`，scope 图则能表达 `dwd.order_detail → cte:order_summary → ROOT`，让 AI 知道聚合发生在哪一层。

## 6. `scopes.<scope_id>`：每个查询块的详细事实

### 6.1 Scope value 的主要字段

| Key | Value | 含义 |
| --- | --- | --- |
| `kind` | enum string | `physical_table`、`cte`、`subquery`、`union`、`union_branch` 或 `root`。 |
| `role` | string | 确定性结构角色，如 `aggregate`、`dedup`、`join`；这是 SQL 结构摘要，不是业务域判断。 |
| `depends_on` | array<string> | 直接依赖的物理表或其他 scope ID。 |
| `alias_in_parent` | string | 该 scope 在父查询中的别名。 |
| `writes_to` | string | ROOT scope 写入的目标表。 |
| `raw_sql` | string/null | 规范化后的当前查询块 SQL。 |
| `raw_sql_available` | boolean | 当前 scope 是否有可复用 SQL 文本。 |
| `raw_sql_quality` | object | SQL 文本是否干净、是否含占位符或恢复证据。 |
| `source_coverage` | object | `raw_sql` 中实际来源是否覆盖声明来源，是否存在缺失或额外表。 |
| `input_edges[]` | array<object> | FROM/JOIN/lateral view 输入的简明边。 |
| `input_source_refs[]` | array<object> | 每次输入的稳定身份、物理来源解析和绑定轨迹。 |
| `alias_source_bindings[]` | array<object> | SQL alias 到输入引用、scope 或物理表的绑定。 |
| `expression_source_bindings[]` | array<object> | 表达式中的 qualifier/字段如何绑定到来源。 |
| `logic_blocks[]` | array<object> | JOIN、过滤、聚合、窗口等可引用逻辑单元。 |
| `outputs[]` | array<object> | 当前 scope 的详细输出字段事实。 |
| `field_usage[]` | array<object> | 每个输入来源有哪些字段被逻辑块或输出字段使用。 |
| `columns[]` | array<object> | 兼容性投影视图；新消费者优先读取信息更完整的 `outputs[]`。 |
| `union_branch_alignment` | object | UNION 分支、字段位置和对齐状态。仅相关 scope 输出。 |
| `joins[]` | array<object> | 兼容性 JOIN 结构，保留左右输入、类型、ON 表达式和字段。精确关系优先读 `logic_blocks[].join_relation_detail`。 |
| `filters[]` | array<object> | 兼容性 WHERE 条件和引用字段。精确条件拆分优先读 `filter_predicate_detail`。 |
| `group_by[]` / `having[]` | array | 分组和聚合后过滤的结构化子句事实。 |
| `order_by[]` | array<object> | 排序表达式、字段和方向。窗口内部排序同时位于 `window_specification`。 |
| `distinct` | boolean | 当前 SELECT 是否使用 DISTINCT。 |
| `lateral_views[]` | array<object> | LATERAL VIEW/UDTF 的结构化事实。 |

MERGE 的 `ROOT` 是 Core 合成的写入作用域，不对应 SQLGlot 的单一查询 scope。契约 1.0
中，`scopes.ROOT.raw_sql` 保存规范化后的 `USING` 行集 SQL，以便在不同 SQLGlot 版本间
保持稳定；它不表示完整 MERGE 语句。各个 `WHEN MATCHED` / `WHEN NOT MATCHED` 分支的
写入表达式应读取 `ROOT.columns[]` / `ROOT.outputs[]` 中的 `merge_branch` 和
`merge_when_index`。

Spark 有**三种** WHEN 子句，而 `merge_branch` 的枚举只命名其中两种。第三种
`WHEN NOT MATCHED BY SOURCE` **不发 `merge_branch`**，改由 `merge_branch_qualifier`
承载（取值 `not_matched_by_source`），同时发一条 `merge_branch_not_representable` 告警
说明缺席的原因。`merge_when_index` 照常给出。发一个枚举里现有的名字会让消费者按错误的
行集语义计算：`not_matched` 指的是「目标中不存在、从源插入」，而该子句写的恰恰是
「目标中存在、源中没有对应行」。

分支还决定**赋值右侧的名字解析域**，这与 Spark 一致：`MATCHED` 同时可见目标与源
（两边同名且未限定 → `ambiguous_unqualified`，不任选来源）；`NOT MATCHED` 只见源；
`NOT MATCHED BY SOURCE` 只见目标，其中出现源别名限定的引用在 Spark 中无法解析，
Core 记 `dangling_column_ref_dropped` 而不产出来源边。如果写入值包含标量子查询，ROOT 字段先引用该子查询的稳定 scope
输出，再由 scope 链展开到物理字段；不会把子查询内部字段误绑定到 `USING` scope。
标量子查询中引用 MERGE 目标行的相关字段会作为目标表的物理自引用保留，并出现在
`source_tables` 中。

CTE 名按所在查询块的词法作用域绑定。例如，一个嵌套查询声明 `WITH staging AS (...)`
不会隐藏兄弟查询块中名为 `staging` 的无库名前缀物理表；后者仍会进入 `source_tables`。

### 6.2 `input_edges[]` 与 `input_source_refs[]`

`input_edges[]` 适合画结构图：

```json
{
  "source_id": "cte:order_summary",
  "source_type": "scope",
  "position": "join",
  "alias": "summary",
  "join_type": "LEFT_OUTER",
  "join_condition": "`base`.`customer_id` = `summary`.`customer_id`"
}
```

`input_source_refs[]` 适合精确绑定和追踪：

```json
{
  "input_ref_id": "input:ROOT:003",
  "source_id": "cte:order_summary",
  "source_type": "scope",
  "physical_source_ids": ["dwd.order_detail"],
  "source_resolution": {
    "status": "resolved",
    "cardinality": "single_source",
    "physical_source_tables": ["dwd.order_detail"]
  },
  "field_resolution_required": true,
  "binding_status": "resolved",
  "binding_trace": [],
  "trace_status": "complete"
}
```

同一物理表被 JOIN 两次时，不能只按表名绑定字段；应使用 `input_ref_id` 区分输入实例。

## 7. `logic_blocks[]`：SQL 处理逻辑

每个逻辑块至少有：

| Key | Value | 含义 |
| --- | --- | --- |
| `logic_block_id` | string | 可稳定引用的逻辑 ID。 |
| `logic_type` | string | 如 `join`、`filter`、`aggregate`、`group_by`、`window`。 |
| `raw_expression` | string/null | 接近原 SQL 的表达式。 |
| `normalized_expression` | string/null | 便于比较和检索的规范化表达式。 |
| `fingerprint` | string/null | 类型加规范表达式形成的去重指纹。 |
| `fields[]` | array<object> | 表达式直接引用的字段，元素至少含 `scope` 和 `column`。 |
| `output_fields[]` | array<string> | 该逻辑生成或影响的 scope 输出字段。 |
| `input_sources[]` | array<string> | 逻辑涉及的输入 scope/物理表。 |
| `field_usage[]` | array<object> | 字段被哪个逻辑块、哪个输出使用。 |
| `expression_features` | object | 函数、运算符及 CASE/CAST/window/aggregate/UDF 等布尔特征。 |
| `final_target_columns[]` | array<string> | 该逻辑最终影响的目标字段。 |

不同逻辑类型还会有专用 detail：

| Detail key | 内容 |
| --- | --- |
| `join_relation_detail` | `join_type`、`join_key_pairs[]`、`condition_filters[]`、`trace_status`、`missing_reasons[]`。区分真正的关联 key 与 ON 中附加过滤。 |
| `filter_predicate_detail` | WHERE/HAVING 条件拆分后的 `conjuncts[]`、字段解析、子查询依赖和分区过滤判断。 |
| `aggregation_detail` | `group_by_items[]`、`aggregate_items[]`、`having` 及每项的表达式来源。 |
| `window_specification` | 窗口函数、`partition_by[]`、`order_by[]`、窗口后过滤和 trace 状态。 |

例如，同样出现 `customer_id`，JOIN key、WHERE filter 和 SELECT 输出的用途不同；`logic_blocks` 会保留这种上下文，而不是把它们压成一个无语义字段集合。

## 8. `outputs[]`：scope 输出字段

一个输出字段的典型结构：

```json
{
  "name": "paid_amount_30d",
  "output_ordinal": 2,
  "transform": "AGGREGATE",
  "expression": "SUM(CASE WHEN pay_status = 'PAID' THEN pay_amount ELSE 0 END)",
  "expanded_expression": "SUM(CASE WHEN `dwd.order_detail`.`pay_status` = 'PAID' THEN `dwd.order_detail`.`pay_amount` ELSE 0 END)",
  "expression_type": "aggregate_expression",
  "expression_role": "metric_calculation",
  "grain_effect": "changed",
  "sources": [
    {"scope": "dwd.order_detail", "column": "pay_status"},
    {"scope": "dwd.order_detail", "column": "pay_amount"}
  ],
  "source_logic_blocks": ["logic:cte:order_summary:aggregate:002"],
  "downstream_fields": [{"scope": "ROOT", "column": "paid_amount_30d"}],
  "final_target_columns": ["mart.customer_profile_snapshot.paid_amount_30d"],
  "consumer_readiness": {"status": "ready", "blocked_reasons": []}
}
```

主要 key：

| Key | 含义 |
| --- | --- |
| `name` | 当前 scope 的输出名。 |
| `output_ordinal` | 从 0 开始的输出位置；重复字段名或 MERGE 多分支时不能只按 name 区分。 |
| `transform` | 粗粒度变换：`DIRECT`、`EXPRESSION`、`AGGREGATE`、`WINDOW`、`CONDITIONAL`、`CONSTANT`、`UNION`、`EXPAND_ALL`。 |
| `expression` | 当前 scope 中的 SQL 表达式。 |
| `expanded_expression` | 尽可能展开到物理来源限定名后的表达式。 |
| `expression_resolution` | 解析状态、物理/生成/行集来源、缺失原因和跨 scope trace。 |
| `expression_type` | 结构类型，如 direct、conditional、aggregate、window、arithmetic、constant、UDF。 |
| `expression_role` | 用途，如 direct projection、standardization、cleaning、metric calculation、record selection。 |
| `grain_effect` | `preserved`、`changed`、`may_change` 或 `unknown`。 |
| `sources[]` | 直接输入字段；可带 qualifier、binding scope、input ref。 |
| `source_logic_blocks[]` | 生成该字段的逻辑块 ID。 |
| `downstream_fields[]` | 消费该输出的后续 scope 字段。 |
| `target_columns[]` / `final_target_columns[]` | 当前目标和最终物理目标字段。 |
| `consumer_readiness` | 是否已具备安全下游消费所需事实；blocked 时列出原因。 |
| `merge_branch` / `merge_when_index` | MERGE 场景中字段属于哪个 WHEN 分支。`merge_branch` 在 `WHEN NOT MATCHED BY SOURCE` 上**缺席**（见 §7），`merge_when_index` 始终给出。 |
| `merge_branch_qualifier` | 枚举无法命名的 WHEN 子句种类，目前只有 `not_matched_by_source`。被枚举命名的两种分支上不出现。 |

### `SELECT * EXCEPT (...)`

星号的排除列表**会被应用**：被排除的列不出现在任何输出面（`columns[]` / `outputs[]` / `field_mapping_chains[]` / `end_to_end_lineage[]` / `related_metadata`）。

两点需要留意：

- 排除会改变投影**数量**，而目标 DDL 绑定是按位置做的。排除后投影数与目标列数相符时绑定会被启用；不符时整体降级为 fallback。两种变化都是修正——此前多出来的那一列会被当作真实输出列参与绑定。
- Spark 语法只允许 `EXCEPT` 一种星号修饰符。`REPLACE` / `RENAME` / `ILIKE` 是别的引擎的构造，工具不建模，只发 `star_modifier_not_supported` 告警。


### 8.1 聚合 STRUCT 成员投影

当上游输出通过 `MAX/MIN(STRUCT(...))` 或 `MAX/MIN(NAMED_STRUCT(...))` 选出一个
STRUCT，而下游继续访问其中一个成员时，`expanded_expression` 会保留聚合和成员投影，不能
压平为普通叶子字段。例如：

```sql
MAX(NAMED_STRUCT(
  'update_time', `ods.layer`.`update_time`,
  'layer_name', `ods.layer`.`layer_name`
)).layer_name
```

其输出需要同时保留：

- 完整的 `MAX(NAMED_STRUCT(...)).layer_name` 加工语义；
- `update_time` 这一选行/比较输入；
- `layer_name` 这一返回值和比较输入；
- 从外层输出回到上游聚合输出的 `scope_output_trace`。

普通非聚合 STRUCT 成员访问仍可展开为被选择的叶子字段。该区别防止消费者把“从聚合选中的
STRUCT 中取字段”误读成普通直接投影。

### 8.2 字段分类枚举

`transform` 是兼容的粗粒度分类：

| Value | 含义 |
| --- | --- |
| `DIRECT` | 直接字段投影。 |
| `EXPRESSION` | 普通表达式派生。 |
| `AGGREGATE` | 聚合表达式。 |
| `WINDOW` | 窗口表达式。 |
| `CONDITIONAL` | CASE WHEN / IF 条件表达式。 |
| `CONSTANT` | 常量或系统生成值。 |
| `UNION` | UNION 位置对齐后的输出。 |
| `EXPAND_ALL` | `SELECT *` 或 `alias.*` 未完全展开时的占位。 |

#### `WINDOW` 里混着三种角色，不要当作同一种依赖

一个窗口字段的来源，`transform` 都标成 `WINDOW`，但它们对结果值的作用完全不同：

| 角色 | `SUM(amt) OVER (PARTITION BY id ORDER BY dt)` 中 | 是否决定数值 |
| --- | --- | --- |
| 值参数 | `amt` | 是 |
| 分区键 | `id` | 否，只决定该行落在哪一组 |
| 排序键 | `dt` | 否，只决定组内次序 |

三列都会以 `transform: "WINDOW"` 出现在 `sources[]` 里。所以当一个窗口按很多列分区时，
这些列会同时成为来源——**这是如实记录**：换掉任一分区列，分组就变，窗口结果也可能变。

角色是落盘的，但它挂在**定义窗口的那一列**上，不在下游字段上。`columns[].window` 给出
`partition_by[]` 与 `order_by[]`，只有 `transform` 为 `WINDOW` 的那一列才有这个结构。

要判断某个下游字段的哪些来源属于窗口上下文，沿 `sources[]` 往上走到带 `window` 的那一列：

```
ROOT.begin_date        transform=EXPRESSION       ← 本层只有 1 个直接来源
  └ subq:s2.start_dt   DIRECT
     └ subq:s1.start_dt   EXPRESSION              ← date_add(dt, rn - 1)
        ├ subq:s0.dt   DIRECT                     ← 值来源
        └ subq:s0.rn   WINDOW   window={partition_by[15], order_by[1]}
```

`rn` 的 15 个 `partition_by` 列与 1 个 `order_by` 列是**上下文**；`start_dt` 真正的值来源是 `dt`。
注意 `begin_date` 在本层只有一个直接来源——那 16 个上下文列是 `end_to_end_lineage`
把整条链压平之后才出现的。

`row_number()`、`rank()` 这类窗口没有值参数，属于"值完全由分区与排序决定"。此时该下游字段的
值来源要到窗口之外的表达式里找——上例中就是 `date_add(...)` 里的 `dt`，它以 `DIRECT`/`EXPRESSION`
而非 `WINDOW` 出现。

这不是缺陷而是口径：分区键确实影响结果，Core 如实记录，由消费方区分"值从哪来"与
"分组/排序上下文是什么"。

`expression_type` 提供更适合新消费者的结构分类：

| Value | 含义 |
| --- | --- |
| `direct_projection` | 直接字段投影。 |
| `conditional_expression` | CASE WHEN / IF。 |
| `type_cast` | CAST 或类型转换。 |
| `function_expression` | COALESCE、TRIM、SUBSTR 等普通函数。 |
| `aggregate_expression` | SUM、COUNT、AVG、MIN、MAX 等聚合。 |
| `window_expression` | ROW_NUMBER、RANK 等窗口表达式。 |
| `arithmetic_expression` | 加减乘除等算术派生。 |
| `constant_expression` | 常量或系统值表达式。 |
| `udf_expression` | 非内置函数或 UDF。 |
| `unknown_expression` | 当前无法稳定分类。 |

`expression_role` 表示表达式在数据加工中的用途：

| Value | 含义 |
| --- | --- |
| `direct_projection` | 原样引用。 |
| `field_derivation` | 通用字段派生。 |
| `standardization` | 编码、状态或格式标准化。 |
| `cleaning` | 空值、异常值或文本清洗。 |
| `type_conversion` | 类型转换。 |
| `metric_calculation` | 指标计算。 |
| `record_selection` | 去重、排序取数等记录选择辅助。 |
| `constant_fill` | 常量填充。 |
| `unknown` | 无法从结构稳定判断用途。 |

`grain_effect` 表示表达式对行粒度的局部影响：

| Value | 含义 |
| --- | --- |
| `preserved` | 表达式本身不改变明细粒度。 |
| `changed` | 聚合等行为已经改变粒度。 |
| `may_change` | 窗口/去重等行为可能影响记录选择。 |
| `unknown` | 当前证据不足。 |

消费时不要只看 `transform`。字段解释优先组合 `expression_type`、`expression_features`、`expression_role` 和 `grain_effect`；判断整个模型粒度还必须查看 GROUP BY、窗口、DISTINCT 和 scope 上下文。

### 8.3 `field_usage[]`：输入字段被怎样使用

```json
{
  "source_id": "dwd.order_detail",
  "source_type": "physical_table",
  "used_fields": ["customer_id", "order_id", "pay_amount"],
  "used_field_details": [
    {"name": "pay_amount", "type": "decimal(18,2)", "comment": "Paid amount"}
  ],
  "used_by_logic_blocks": ["logic:cte:order_summary:aggregate:002"],
  "used_by_output_fields": ["paid_amount_30d"],
  "source_metadata": {}
}
```

| Key | 含义 |
| --- | --- |
| `source_id` / `source_type` | 直接输入的 scope/物理表身份和类型。 |
| `used_fields[]` | 当前 scope 实际使用的字段名。 |
| `used_field_details[]` | 可用时补充字段类型、注释等 Schema 详情。 |
| `used_by_logic_blocks[]` | 哪些逻辑块读取了这些字段。 |
| `used_by_output_fields[]` | 哪些 scope 输出字段直接使用了这些字段。 |
| `source_metadata` | Core 可用的通用来源元数据；没有输入时为空对象。 |

它适合回答“这张表的某个字段在当前查询块中是 JOIN key、过滤字段，还是输出表达式输入”。跨 scope 的最终目标影响仍应使用 mapping chain 或 end-to-end lineage。

### 8.4 `columns[]`：兼容性解析视图

`columns[]` 更接近解析器原始列模型，通常包含 `name`、`transform`、`expression` 和 `sources[]`，并可能保留特定变换附加值，如 `agg_function`、`case_branches`、`window` 或 UNION branches。

新消费者优先读取 `outputs[]`，因为它补齐了表达式解析、逻辑块引用、下游字段、最终目标和 consumer readiness。`columns[]` 主要用于：

- 兼容早期消费者；
- 调试 parser 的原始列解析；
- 查看某些 transform 特有的附加结构。

目标字段绑定成功时，ROOT `columns[]` 还可能包含 `parsed_name`、`target_column_ordinal`、`target_field_resolution`、`target_field_corrected` 和 `target_metadata_table`，语义与端到端字段中的同名审计 key 一致。

## 9. `field_mapping_chains[]`：字段逐步变换链

这是“为什么得到这条端到端血缘”的主要证据。

```json
{
  "mapping_chain_id": "mc:001",
  "chain_id": "chain:ROOT:customer_id:position:0",
  "chain_type": "field_mapping",
  "target_scope_id": "ROOT",
  "target_field": "customer_id",
  "target_position": 0,
  "chain_status": "resolved",
  "root_source_fields": ["ods.customer_base.customer_id"],
  "final_output_fields": ["mart.customer_profile_snapshot.customer_id"],
  "ordered_steps": [
    {
      "step_no": 1,
      "scope_id": "ROOT",
      "step_type": "direct_projection",
      "input_fields": ["ods.customer_base.customer_id"],
      "output_field": "mart.customer_profile_snapshot.customer_id",
      "expression_sql": "`base`.`customer_id`",
      "expanded_expression": "`ods.customer_base`.`customer_id`",
      "transform": "DIRECT",
      "grain_effect": "preserved"
    }
  ],
  "missing_reasons": [],
  "trace_status": "complete"
}
```

关键消费规则：

- `ordered_steps[]` 按 `step_no` 表示字段从上游到目标的变换顺序；
- `root_source_fields[]` 只包含已经证明的根物理字段；
- `trace_status=incomplete` 时查看 `missing_reasons[]`，不能把链当成完整证据；
- `target_position` 用于区分同名输出和保持 INSERT 位置语义。

## 10. `end_to_end_lineage[]`：最终字段血缘

每个元素对应一个最终输出位置：

| Key | Value | 含义 |
| --- | --- | --- |
| `column` | string | 绑定后的最终目标字段名。 |
| `parsed_column` | string | SQL 原始投影名；目标 DDL 纠正字段名时与 `column` 不同。 |
| `output_ordinal` / `target_column_ordinal` | integer | SQL 输出位置和目标字段位置。 |
| `target_field_resolution` | enum string | `ddl_position`、`schema_position` 或 `insert_column_list`。 |
| `target_field_corrected` | boolean | 是否根据目标元数据纠正了 SQL 投影名。 |
| `target_metadata_table` | string | 使用了哪张目标表元数据。 |
| `transform` | string | 最终字段的粗粒度变换类型。 |
| `expression` | string/null | 最终投影表达式。 |
| `trace_complete` | boolean | 是否已经追溯到确定来源。 |
| `trace_incomplete_reasons[]` | array<string> | 不完整原因。 |
| `physical_sources[]` | array<object> | 已证明的 `{table, column, transform}` 物理来源。 |
| `generated_sources[]` | array<object> | 常量、系统值等非物理字段来源。 |
| `rowset_sources[]` | array<object> | 窗口或行集级语义来源。 |
| `source_kind` | enum string | `physical`、`generated`、`mixed`、`rowset` 或 `unresolved`。 |
| `ambiguities[]` | array<object> | 无法唯一选择来源的位置和候选链。候选不是已证明来源。 |

### 10.1 物理来源、生成来源和行集来源

- `physical_sources`：真实表字段，例如 `ods.customer.customer_id`；
- `generated_sources`：常量、NULL、系统值等，例如固定字符串标签；
- `rowset_sources`：窗口函数等依赖行集合而非单一字段的来源；
- `mixed`：同时依赖多种来源。

### 10.2 歧义不能合并成事实

当 `trace_complete=false` 且有 `ambiguities[]` 时：

```json
{
  "column": "id",
  "trace_complete": false,
  "trace_incomplete_reasons": ["ambiguous_unqualified_column"],
  "physical_sources": [],
  "ambiguities": [
    {
      "scope": "ROOT",
      "column": "id",
      "candidate_count": 2,
      "candidates": [
        {"scope": "ods.customer", "column": "id", "trace_complete": true},
        {"scope": "ods.order", "column": "id", "trace_complete": true}
      ]
    }
  ]
}
```

下游不能把两个 candidate 都写入 `physical_sources`，也不能任意选一个。正确做法是保留歧义状态，并结合 `diagnostics.json` 请求 Schema、alias 或 SQL 修正。

## 11. `target_field_binding`：目标字段位置绑定

当提供 `--target-ddl-metadata` 时，SQL 的第 N 个投影可按目标 DDL/Schema 的第 N 个非静态分区字段绑定：

| Key | 含义 |
| --- | --- |
| `status` | `applied`、`fallback` 或 `not_applied`。 |
| `method` | `ddl_position`、`schema_position`、`insert_column_list` 或 `sql_projection`。 |
| `metadata_table` / `metadata_source_file` | 采用的目标元数据和来源文件。 |
| `projection_count` | SQL 投影数量。 |
| `target_column_count` | 可绑定目标字段数量。 |
| `corrected_column_count` | 被目标元数据纠正名称的字段数。 |
| `static_partition_columns[]` | SQL 已给定值的静态分区列，不占 SELECT 投影位置。 |
| `dynamic_partition_columns[]` | 需要由 SELECT 投影提供值的动态分区列。 |
| `issues[]` | 无法应用或降级时的原因。 |

它的价值是避免 `SELECT expr AS 临时别名` 被误认为最终目标字段名，并保留纠正证据。

## 12. `task_dependencies` 与 `related_metadata`

### 12.1 `task_dependencies`

```json
{
  "upstream_tasks": [
    {
      "dependency_id": "taskdep:upstream:001",
      "direction": "upstream",
      "task_id": "task-1001",
      "task_name": "order_detail_daily",
      "dependency_type": "declared",
      "dependency_table": null,
      "source": "task_info.meta.upstream_tasks",
      "source_file": "customer_profile_daily.json",
      "raw_record": {}
    }
  ],
  "downstream_tasks": [],
  "source_summary": {
    "source_format": "task_info_meta",
    "upstream_count": 1,
    "downstream_count": 0,
    "has_declared_task_dependencies": true
  }
}
```

这里的任务依赖来自输入任务 JSON 声明，不等同于解析 SQL 推导的表依赖。知识图谱可以分别建立“任务依赖边”和“表/字段血缘边”。

### 12.2 `related_metadata`

- `input_tables`：key 是输入表名，value 包含 `column_details[]`、字段类型/注释和 `metadata_complete`；
- `output_tables`：key 是目标表名，value 为对应目标元数据；
- `metadata_complete`：表示调用方提供的元数据是否足以覆盖已知字段，不表示真实 catalog 永远完整。

## 13. `scope_profile`：适合 AI 检索的确定性简表

`scope_profile.steps[]` 每项包含：

| Key | 含义 |
| --- | --- |
| `scope_id` / `name` / `kind` / `role` | 查询块身份和结构角色。 |
| `operations[]` | 该 scope 出现的操作，如 `join`、`filter`、`aggregate`、`window`。 |
| `direct_inputs[]` | 直接输入的 scope 或物理表。 |
| `direct_source_tables[]` | 当前 scope 直接读取的物理表。 |
| `physical_source_tables[]` | 穿透上游 scope 后涉及的全部物理表。 |
| `output_columns` | 输出字段数。 |
| `logic` | joins、filters、aggregations、window functions、CASE、DISTINCT、UNION 等简明结构。 |
| `business_summary` | 基于结构模板生成的确定性摘要；不应当作完整业务定义。 |

用于 RAG 时，可以先索引 `scope_profile` 和 `end_to_end_lineage`；只有命中任务后再加载完整 `scopes`，减少上下文体积。

## 14. `diagnostics`：Lineage 内的质量摘要

```json
{
  "fallback_used": false,
  "warning_count": 3,
  "warning_types": {"magic_number": 1, "filter_in_join_on_clause": 1},
  "lineage_fact_gap_count": 0,
  "lineage_fact_gap_types": {},
  "lineage_fact_gap_samples": [],
  "stats": {"scope_count": 6, "join_count": 2},
  "full_diagnostics_file": "diagnostics.json"
}
```

它只用于快速筛选。需要完整 warning 和所有事实缺口时，读取同目录 [`diagnostics.json`](diagnostics-json.md)。

## 15. 常见使用场景和读取路径

| 场景 | 建议读取 |
| --- | --- |
| 表级影响分析 | `source_tables`、`target_table` |
| 字段级影响分析 | `end_to_end_lineage[].physical_sources`、`column` |
| 解释指标计算方式 | ROOT `outputs[]` → `field_mapping_chains[]` → 相关 `logic_blocks[]` |
| 查询某字段在哪里过滤 | `scopes.*.logic_blocks[logic_type=filter].fields[]` |
| 找 JOIN key | `logic_blocks[].join_relation_detail.join_key_pairs[]` |
| 判断是否改变粒度 | `outputs[].grain_effect`、`aggregation_detail`、`window_specification` |
| 构建知识图谱 | scope graph + task dependencies + end-to-end physical source edges |
| 给 Agent 构建任务摘要 | `scope_profile` + `end_to_end_lineage` + diagnostics summary |
| 回答“为什么不确定” | `trace_incomplete_reasons`、`ambiguities`、`diagnostics.json.lineage_fact_gaps` |

## 16. 查询示例

列出目标字段及物理来源：

```bash
jq -r '.end_to_end_lineage[] |
  [.column, (.physical_sources | map(.table + "." + .column) | join(",")), (.trace_complete|tostring)] |
  @tsv' lineage.json
```

列出所有 scope 和直接依赖：

```bash
jq -r '.scopes | to_entries[] | [.key, .value.kind, (.value.depends_on|join(","))] | @tsv' lineage.json
```

列出所有过滤表达式：

```bash
jq -r '.scopes | to_entries[] as $scope |
  $scope.value.logic_blocks[]? |
  select(.logic_type == "filter") |
  [$scope.key, .logic_block_id, .raw_expression] | @tsv' lineage.json
```

Python 消费并校验：

```python
import json
from pathlib import Path

from scope_lineage import validate_lineage_document

document = json.loads(Path("lineage.json").read_text(encoding="utf-8"))
validate_lineage_document(document)

for field in document["end_to_end_lineage"]:
    if not field["trace_complete"]:
        continue
    sources = [f'{item["table"]}.{item["column"]}' for item in field["physical_sources"]]
    print(field["column"], sources)
```

## 17. 安全消费规则

1. 先检查 `schema_version`、`parse_status` 和 `syntax_status`；
2. `parse_status=failed` 时，空 `scopes` 不代表 SQL 没有血缘；
3. `syntax_status=recovered` 时必须展示恢复风险；
4. `trace_complete=false` 的字段不能进入“已证明血缘”集合；
5. `ambiguities[].candidates` 是候选，不是多来源事实；
6. `generated_sources` 不应伪装成物理表字段；
7. 缺 Schema 时，`SELECT *` 可能保留 `EXPAND_ALL`/`*` 降级表示；
8. 完整质量判断必须配对读取同次运行的 `diagnostics.json`；
9. 1.x 消费者必须容忍新增可选字段；删除、改名或语义改变需要升级 major。

写盘前 Core 会执行 JSON Schema 和交叉引用校验：悬空 scope、字段或图边不会被发布成成功产物。
