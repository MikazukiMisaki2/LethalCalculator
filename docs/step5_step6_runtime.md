# Step 5/6 runtime contract

当前 `EventInterpreter` 与 `LethalEngine` 已接通 CardRules v2 的资源门槛和随机分支。

## Step 5 boundary checks

- `normal`、`enhance`、`accelerate`、`crystallize` 是互斥的单次 play mode；只有被选择的 mode 扣 PP 和执行能力。
- `rally`、`cemetery`/`necromancy`、`awakening`、`earth_sigil`/`earth_rite`、`skybound_art`、`super_skybound_art` 在条件判断中使用公开快照值。
- `consume_resource` 与负值 `modify_resource` 在 play 前进行累计预检；支付不足时卡留在手牌，后续伤害/召唤不会执行。
- Faith 是按来源实例保存的资源；Mode 选择和进化事件逐个分发给实例。Crest 保留实例 ID、倒计时和独立触发，倒计时在回合结束时推进。

## Step 6 stochastic contract

`play_branches`/`evolve_branches` 返回 `StochasticBranch(probability, state, unsupported_ops, warnings)`：

1. `repeat` 每次重新计算目标池，死亡目标立即移出。
2. 每个随机目标、随机选项、随机抽牌、随机牌堆召唤都按公开池展开并乘累计概率。
3. 每个效果后按 `LethalState.state_key()` 合并相同最终状态。
4. 已知牌堆按卡牌数量加权；未知牌堆/敌方手牌身份保留残余分支并标记 `draw_unknown` 或 `summon_unknown`。
5. 随机池为空时该次效果跳过，不制造伪目标。
6. 求解器将所有分支都能斩杀的结果标为 `CONFIRMED`，部分分支斩杀标为 `PROBABILISTIC`；含仍可能影响斩杀的未知/未实现分支标为 `INCOMPLETE`。

## Verification

```powershell
python -B -m unittest discover -s . -p 'test_*.py'
python validate_contract.py --catalog data/generated/card_catalog.json --rules data/generated/card_rules_v2.json --coverage data/generated/card_rules_coverage_report.json
```

当前夹具覆盖 146 个单元测试，包括独立暴力枚举 oracle、Cupitan 七次随机伤害 golden fixture、任意 `N random followers` 的连续激活、Night Song ordered split、Sophia/连妥丝随机召唤、随机抽牌/复制、Faith 多实例、Crest 多实例倒计时、Reanimate 重复条目概率和隐藏牌堆 `INCOMPLETE` 分支，以及 Step 7 的引用/词汇/覆盖门禁。

当前 904 张卡的生成统计为 `generated=885`、`partial=9`、`unsupported=10`。`generated` 只表示 DSL 结构完整；`reanimate`、`replicate_ability`、`spellboost`、`transform` 已进入支持矩阵的 `implemented` 子集，但公开信息不足的分支仍会保留明确的 `INCOMPLETE` 原因：

- Reanimate 从 Tracker 提供的 `destroyed_card_ids` 破坏池中选择随从；数组中的重复条目保留概率权重。复活不会移除池中条目，也不会扣减墓地/Shadow；后续 Reanimate 仍可再次选到同一条目。消失/放逐只影响当前场面，不会从已经记录的 `destroyed_card_ids` 历史池中删除条目；缺少该字段时返回 `reanimate_unknown_pool`。
- Cemetery/Shadow 是可消耗的计数器：只有 `consume_resource`（包括 Necromancy/Earth Rite 分支）会减少它；Reanimate 只读取破坏池，不消耗 Shadow。法术自身的 Shadow 在该法术效果解析完成后加入，因此不会为自己的 Necromancy 阈值提供资源。
- Transform 的静态目标和公开牌堆随机模板可以展开；隐藏牌堆、对手牌堆或无法解析的动态来源产生 `transform_unknown`。
- Replicate Ability 支持同卡单层 Fanfare→Evolve/Engage 复制；缺失来源、未知条件和递归嵌套会报告 `replicate_*`。
- Spellboost 会按手牌实体的 `spell_boost_count` 逐次递增并触发 `on_spellboost`；随机手牌选择按公开候选均匀分支。
