---
doc_format: "warnings-md/1"
schema_version: "1.0"
task_name: "golden_multi_statement_script"
target_table: "mart.first_target"
---

# 解析警告 mart.first_target

共 2 条。这些是解析过程的提示与降级说明，不改变 lineage.json 已证明的事实；影响血缘结论的信息在 mapping.md 的「不确定性与缺口」一节。

## additional_write_statements_not_modeled（1 条）

- @ ROOT：`parse_scope_lineage 只建模脚本中的第一条写表语句;以下写入未被本文档建模: mart.second_target。需要全部写入请使用 parse_all_scope_lineage(契约 1.0)或 parse_task_lineage(契约 2.0)`

## unsupported_statement（1 条）

脚本中存在未建模的语句，该语句被跳过。

- @ ROOT：`DELETE 语句未解析(not_a_table_write_from_select);支持的写表语句: INSERT / INSERT OVERWRITE / CTAS / MERGE`
