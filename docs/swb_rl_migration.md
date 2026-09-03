# SWB-RL 规则迁移门禁

SWB-RL 的 SQLite/RuleBook 是结构化参考实现，不是可以直接覆盖运行时的
`card_rules_v2.json`。迁移分为三层：

1. `import_swb_rl.py` 只读导入并比较目录、英文文本和 RuleBook 覆盖范围。
2. `adapt_swb_rules.py` 将 typed RuleBook 转成 CardRules v2 候选，无法无损表达的
   target binding、动态表达式、监听器范围和隐藏区域会进入 `unparsed_clauses`，卡牌标记
   为 `partial`/`unsupported`。
3. `promote_swb_rules.py` 只选择候选规则生成隔离 overlay 或 merged 试验包。

## 选择策略

默认省略 `--cards` 时，只选择 `support=generated` 且没有
`unparsed_clauses` 的卡牌。显式指定卡牌时仍然要求候选支持状态被允许；
`partial` 和 `unsupported` 分别需要 `--allow-partial`、`--allow-unsupported`。
如果当前规则已经是 `verified`，替换还需要 `--allow-verified-replace`。

这三个开关只改变审阅包，不会把结果改标为 `verified`，也不会写入
`data/generated/card_rules_v2.json`。

## 推荐命令

```text
python import_swb_rl.py --source D:/Github/SWB-RL
python adapt_swb_rules.py --source D:/Github/SWB-RL
python promote_swb_rules.py --cards 10753310,10413110,10413310 --mode overlay
python promote_swb_rules.py --cards 10753310,10413110,10413310 --mode merged
```

产物全部位于 `data/imported/`，其中：

- `swb_rules_selected_overlay.json` 只包含本次选择的卡牌；
- `swb_rules_selected_merged.json` 以当前 v2 副本为基础，仅替换选择的卡牌；
- `swb_rule_migration_report_{overlay,merged}.{json,md}` 记录源哈希、旧支持状态、
  新支持状态、操作集合和是否发生变更。

## 首批运行时夹具

`test_swb_runtime_migration.py` 目前覆盖：

- 夜之歌的演唱会：6 点按入场顺序分配伤害，消耗 6 墓地后再造成 2 点主战者伤害；
- 丘比丹：7 次随机伤害刷新存活目标池并保持概率质量为 1；
- 阿尔夫海姆：普通模式下只执行一次玩家选择，不能因超奥义替代分支而重复 Buff。

这些夹具通过后，卡牌仍需结合 Tracker 快照做一次人工回放，才能从
`generated` 提升为人工 `verified` override。
