# Step 8 · Tracker 状态契约

`ShadowverseTracker` 只负责读取公开对局状态；`LethalCalculator` 通过
`SnapshotAdapter` 将快照转换为 `LethalState`，再交给 `LethalEngine`。适配器不
会用 Catalog 基础值猜测缺失的实时数值：`life`、PP/最大 PP、EP/SEP、手牌/战场、
回合方向和 `legal_actions` 等关键字段缺失时，结果为 `trusted=false`，会话层显示
`INCOMPLETE`，等待下一次快照。

## 已映射字段

- 手牌/战场实体的 `card_id`、`unique_id`、当前费用、攻/生命、进化状态、关键字、
  完整 `buff`、`spell_boost_count`、`variable_x`、`supplement_info`；列表顺序即入场顺序。
  Catalog 只保留本体条目时，Tracker 的进化体 ID（如 `…21`）会通过
  `base_card_id/evolves_to` 归一到本体规则，同时保留实例的实时数值和进化标记，
  因而不会在 UI 或路线中回退成数字 ID。
- `LegalActions.attack_targets`（以及实体上的 `attack_targets`）限制攻击目标；目标
  UID 不在该集合时解释器拒绝攻击。即使 LegalActions 暂时不可用，只要实体明确给出
  `attack_targets`，也会保留这个限制，并把整体快照标为不可信。普通/强化/加速/结晶
  （以及可观察到的 Fusion）和进化/超进化列表同样作为当前回合的权威限制。
- `attacked_cards` 只记录本回合已经发生过攻击；若 Tracker 同时仍暴露合法目标，适配器
  不会把多次攻击随从误判为已耗尽。显式的空 `attack_targets` 映射表示当前没有可攻击目标，
  不会被陈旧的 FieldCard 列表覆盖。
- PP、ExtraPP、EP、SEP、Rally、PlayCount、Cemetery、Awakening、土之印、奥义/解放
  奥义计数、Faith 实例、Crest 实例与倒计时、当前回合已攻击 UID、毁坏池重复条目。
  `evolve_turn`/`super_evolve_turn` 作为持久的解锁门槛进入 `LethalState`；模拟动作使
  `LegalActions` 过期后仍会执行该门槛，避免在 T5/T7 之前自动进化或超进化。
  `is_evolved_this_turn` 可能也会被自动进化效果置为真；适配器优先从 Tracker 附带的
  `_recent_actions`/`recent_actions` 统计“手动进化/手动超进化”，写入
  `manual_evolutions_this_turn`。搜索器因此只禁止第二次玩家手动进化，不会错误禁止
  自动进化后再使用 EP/SEP。
  Tracker 的领袖 `PlayerBuff` 会原样保存；已知的 `increase_damage`/`damage_cut`
  会投影为后续直伤和攻击的净修正；Tracker 的空闲 `damage_cut=-1` 哨兵会按“无
  修正”处理，不会凭空给每次伤害增加 1 点。
- Schema 允许对手手牌使用 Tracker 的隐私占位 `{\"hidden\": true}`；适配器只要求
  我方手牌具备 UID、卡牌 ID 和当前费用，不会把未知的对手手牌身份猜成具体卡牌。

## 刷新与 UI

`tracker_integration.py` 中的 `TrackerLethalSession.on_snapshot` 可直接作为
Tracker 的 `on_snapshot` 回调。相同快照通过 SHA-256 指纹去重；新快照返回
`TrackerSolveView`，其中包含 `status`（`CONFIRMED`、`PROBABILISTIC`、
`INCOMPLETE`、`NO_LETHAL`）、概率、动作序列、合法动作、目标选项和警告。没有斩杀
时，会在相同 PP/EP/目标约束下运行 `LethalEngine.max_damage`，返回当前回合最高
理论伤害及对应路线；随机分支取可达的最大分支，并在路线中保留 `incomplete` 标记，
因此 UI 不会把理论上限误显示成确定斩杀。UI 可用 `select_target` 在当前
`attack_targets` 中选择目标，刷新后无效选择会自动清除。

Tracker 主界面的“斩杀计算（实时）”面板通过
`ShadowverseTracker/src/shadowverse_tracker/lethal_bridge.py` 自动加载同级
`LethalCalculator`。面板显示确认/概率/不完整状态、概率、斩杀或最高伤害路线，
并列出 PP/ExtraPP、EP/SEP、Rally、PlayCount、墓地、觉醒、Faith、Crest、奥义、
毁坏池、可用模式、AttackTargets 与合法操作。若目录或规则不可用，会显示原因并继续
正常运行记牌器；可用 `SHADOWVERSE_LETHAL_ROOT` 指定计算器目录。

TrackerService 写出的 JSONL 记录（外层含 `timestamp`/`snapshot`）也可直接交给适配器；
指纹只计算内层游戏状态，不会因记录时间变化而重复求解。

```text
Tracker callback
    └─> TrackerLethalSession.refresh/on_snapshot
          └─> SnapshotAdapter (trusted / legal target projection)
                └─> LethalEngine.solve (only when trusted and ally turn)
                      └─> TrackerSolveView.status + sequence + targets
```

验证示例：

```powershell
python -B validate_schemas.py `
  --tracker fixtures/tracker_snapshots/complete.json `
  --tracker fixtures/tracker_snapshots/missing_critical.json `
  --tracker fixtures/tracker_snapshots/target_restricted.json
```
