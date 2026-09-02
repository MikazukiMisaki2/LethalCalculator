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
3. 触发器、资源、条件状态和关键词必须来自 Schema/支持矩阵词表；关键词还会按支持矩阵检查运行时状态（当前 Storm/Rush/Ward/Bane/Drain/Ambush 可进入 `verified`）；
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

当前基线为 904 张卡：`generated=904`、`partial=0`、`unsupported=0`、`verified=0`。这表示当前英文主文本都能生成结构化 CardRules v2 载荷；`generated` 仍不等于人工确认或完整运行时覆盖。公开信息不足的随机池、隐藏/替换牌堆和仍计划中的关键词（Aura、Barrier、Effect Indestructible、Intimidate、Unplayable 等），斩杀搜索仍必须依据支持矩阵和解释器结果返回 `INCOMPLETE`。

构建时还会单独保留文本解析审计：中文缺失翻译和无法解析的中文展示句不会把已由英文完整解析的规则降级为 `partial`，但会继续出现在 `card_text_parse_report.json` 的统计中。只有英文主能力本身存在未解析残留时，才会进入规则的 `unparsed_clauses` 并阻止 generated 状态。

## ID 边界

紧凑 Catalog 以基础卡牌实体为索引；`evolves_to` 是演化元数据，演化后的 `...11` 变体不重复物化为独立 Catalog 条目。Step 7 的 ID 门禁针对规则实际会读取/创建的显式卡牌和 Token 引用；演化元数据不被误当成可执行规则引用。
