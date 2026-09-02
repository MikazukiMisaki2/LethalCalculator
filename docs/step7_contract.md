# Step 7：CardRules 发布合同

Step 7 的门禁由 `validate_contract.py` 统一执行：

```powershell
python validate_contract.py \
  --catalog data/generated/card_catalog.json \
  --rules data/generated/card_rules_v2.json \
  --coverage data/generated/card_rules_coverage_report.json
```

它按以下顺序检查：

1. `card_catalog.schema.json` 和 `card_rules_v2.schema.json` 的 Draft 2020-12 校验；
2. 规则中的 `card_id`、`source_card_id`、`crest_card_id` 以及 `*_card_ids` 引用必须存在于 Catalog，且规则键必须和 `card_id` 一致；
3. 触发器、资源、条件状态和关键词必须来自 Schema/支持矩阵词表；关键词还会按支持矩阵检查运行时状态（当前只有 Storm/Rush/Ward 可进入 `verified`）；
4. 所有操作必须出现在 `card_rules_v2_support.json`，`verified` 规则不得使用 `planned` 操作；
5. 覆盖报告中的逐卡支持状态、来源哈希、未解析子句数量和缺失/多余 ID 必须与最终合并规则逐字段一致。

## 覆盖报告

`merge_card_rules.py` 会同时生成 `data/generated/card_rules_coverage_report.json`。报告包含：

- `support`：四种状态的计数；
- `support_by_card`：每个卡牌 ID 的最终状态；
- `cards_by_support`：按状态反向索引；
- `source_hash_by_card`：用于检测 AST/文本变更后旧覆盖是否失效；
- `unparsed_clause_count_by_card`：未解析内容的可追踪数量；
- `missing_rule_ids` / `extra_rule_ids`：Catalog 与规则集合差异。

报告只写逻辑文件名，不写临时目录和时间戳，因此相同输入的连续构建应当字节一致。

当前基线为 904 张卡：`generated=885`、`partial=9`、`unsupported=10`、`verified=0`。`generated` 代表规则结构可编译，不等于人工确认或完整运行时覆盖；斩杀搜索仍应依据支持矩阵和解释器结果处理 `INCOMPLETE`。

## ID 边界

紧凑 Catalog 以基础卡牌实体为索引；`evolves_to` 是演化元数据，演化后的 `...11` 变体不重复物化为独立 Catalog 条目。Step 7 的 ID 门禁针对规则实际会读取/创建的显式卡牌和 Token 引用；演化元数据不被误当成可执行规则引用。
