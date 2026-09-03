# LethalCalculator

《影之诗 World's Beyond》斩杀计算模拟器 —— 给定一回合的场面、手牌与 PP，搜索能在一回合内击败对方主战者的斩杀（Lethal）路线。

A one-turn-kill (lethal) solver for *Shadowverse Worlds Beyond*. Given a board / hand / PP state, it searches for sequences that kill the enemy leader this turn, handling evolved followers, enhance (爆能强化), Storm / Rush, Ward, random-damage probability and draws from deck.

## 功能

- **斩杀搜索**：带记忆化的深度受限搜索，找出最优斩杀序列（`CONFIRMED` / `PROBABILISTIC` / `NO_LETHAL` / `INCOMPLETE`）
- **随机伤害概率**：对随机伤害/抽牌路线进行精确概率计算
- **事件解释器**：将卡牌效果解释为对场面的操作（疾驰/突进、守护、进化、超进化、爆能强化、资源增减等）
- **卡牌文本管线**：双语卡牌文本解析 → 归一化 → 规则编译 → 人工规则覆盖合并
- **桌面模拟器**：基于 tkinter 的 GUI，可视化摆场面并一键求解

## 模块结构

| 模块 | 说明 |
| --- | --- |
| `card_text_ast.py` | 卡牌文本解析为结构化 AST |
| `normalize_card_text.py` | 中英双语卡牌文本归一化 |
| `compile_card_rules.py` | 将文本 AST 编译为可执行卡牌规则 |
| `merge_card_rules.py` | 合并生成规则与人工覆盖（`card_rules_overrides.json`） |
| `validate_contract.py` | Step 7 发布合同：Schema、引用词汇和逐卡覆盖门禁 |
| `rule_coverage.py` | 生成/校验确定性的逐卡支持覆盖报告 |
| `event_interpreter.py` | 解释卡牌事件对局面的影响 |
| `stochastic_calculator.py` | 随机伤害/抽牌的精确概率计算 |
| `lethal_engine.py` | 斩杀搜索求解器（记忆化深度优先） |
| `lethal_models.py` | 核心数据结构（随从/手牌/状态/结果） |
| `lethal_ui_simulator.py` | tkinter 桌面斩杀模拟器 |
| `snapshot_adapter.py` | 将外部快照（Tracker）映射为求解器状态 |
| `tracker_integration.py` | Tracker 实时刷新、合法行动/目标投影与状态视图 |
| `import_swb_rl.py` | 只读导入 SWB-RL SQLite/RuleBook，生成兼容性报告 |
| `adapt_swb_rules.py` | 将 SWB-RL typed RuleBook 转成隔离的 CardRules v2 候选与缺口报告 |
| `test_swb_rule_adapter.py` | 候选规则的 Schema、引用、确定性和代表性语义回归测试 |
| `promote_swb_rules.py` | 按显式 allowlist 选择候选规则，生成隔离 overlay/merged 迁移包 |
| `test_swb_rule_promotion.py` | 迁移选择、verified 保护、Schema 和确定性回归测试 |
| `swb_rl_backend.py` | 可选原生后端：直接在 SWB-RL GameEngine 上按合法命令搜索，不依赖 CardRules v2 |
| `test_swb_rl_backend.py` | 原生后端的无依赖 focused tests |
| `shadow_state_adapter.py` | 将 Tracker 的公开快照保守地水合为 SWB-RL 影子引擎 |

## 快速开始

要求：Python 3.10+。可选依赖 `jsonschema`（用于规则 schema 校验，见 `requirements-dev.txt`）。

```bash
# 启动桌面模拟器
python lethal_ui_simulator.py

# 运行全部单元测试
python -m unittest discover -s . -p "test_*.py"
```

## 数据管线

`cards.json` / `shadowverse_cards_en.json` 为上游卡牌数据，`data/generated/` 下的目录（卡牌目录、规则、报告等）由脚本生成，不入库：

```bash
python clean_cards.py          # 生成 data/generated/card_catalog.json
python compile_card_rules.py   # 生成 data/generated/card_rules_generated.json
python merge_card_rules.py     # 合并人工覆盖 -> data/generated/card_rules_v2.json + coverage report
python validate_contract.py    # Step 7：Schema、引用词汇、逐卡覆盖完整门禁
python validate_schemas.py --tracker fixtures/tracker_snapshots/complete.json  # Step 8 快照 Schema
# 只读导入 SWB-RL 的 SQLite/RuleBook，并生成兼容性审计（不会覆盖上面的数据）
python import_swb_rl.py --source D:/Github/SWB-RL
# 将 RuleBook 适配为隔离候选（不会覆盖 data/generated/card_rules_v2.json）
python adapt_swb_rules.py --source D:/Github/SWB-RL
# 只选择无缺口候选，输出隔离 overlay（不触碰运行时规则）
python promote_swb_rules.py --cards 10753310,10413110,10413310
# 在当前规则上生成隔离 merged 试验包；已有 verified 规则默认不会被替换
python promote_swb_rules.py --mode merged --cards 10753310,10413110,10413310
```

若要直接使用 SWB-RL 的 RuleBook 做实验，不需要把它转换为 v2：

```python
from swb_rl_backend import SwbRlBackend

result = SwbRlBackend(max_depth=16, node_limit=50_000).solve(
    simulator, player_index=simulator.human_player
)
print(result.status, result.max_damage, result.sequence)
```

该后端只接受完整的 SWB-RL `GameEngine`/`MatchSimulator`，不把 Tracker 的
不完整公开快照猜测成引擎状态；详情见 `docs/swb_rl_backend.md`。随机事件在
单一 seed 下只是探测结果，会标记为 `INCOMPLETE`，不会错误显示为确定斩杀。

Tracker 还可以显式启用影子后端：

```powershell
$env:SHADOWVERSE_LETHAL_BACKEND = "swb_rl_shadow"
$env:SHADOWVERSE_SWB_RL_ROOT = "D:\Github\SWB-RL"
python D:\Github\ShadowverseTracker\run_tracker.py
```

影子适配会把 Tracker 当前可见的手牌、场面、资源、攻击目标和合法操作注入
SWB-RL 的 `GameState`，再使用其原生 `RuleBook` 搜索。Tracker 快照没有完整的
隐藏对手手牌、牌库顺序和随机状态，因此影子结果始终显示为 `INCOMPLETE`（并在
提示中列出缺口）；它适合验证规则/路线，不应当作为确定斩杀证明。未设置该环境变量
时仍使用 CardRules v2 后端。

`import_swb_rl.py` 的产物位于 `data/imported/`：`swb_catalog_projection.json`
保存带引用、模式和英文文本的规范化目录，`swb_rulebook_raw.json` 保留 RuleBook
原始文件及哈希，`swb_compatibility_report.{json,md}` 对照当前 v2 目录/Schema
统计字段差异和需要适配的效果。该目录是可重复生成的只读审计输入，不会自动把
SWB-RL 规则写入斩杀运行时。

在审计之后，`adapt_swb_rules.py` 会读取同一份 SQLite/RuleBook，输出
`data/imported/swb_card_rules_v2_candidate.json`、`swb_rule_adapter_report.{json,md}`。
适配器保留可表达的目标、条件、模式、随机/分配、资源和复制语义；无法安全映射的
typed expression、target binding、listener 限制或隐藏对手区域会写入
`unparsed_clauses`，并把该卡标记为 `partial`/`unsupported`。候选文件只用于审阅和
逐卡迁移，不能直接替代运行时规则；迁移前必须通过 Schema、引用合同和人工回归测试。

`promote_swb_rules.py` 是候选到运行时之间的安全闸门。显式指定卡牌时，它默认只接受
`support=generated` 且没有 `unparsed_clauses` 的候选；`partial`、`unsupported` 以及替换
现有 `verified` 规则都需要命令行显式确认。省略 `--cards` 只会选择全部 adapter-clean 的
`generated` 卡，但仍然只写到 `data/imported/`。`--mode overlay` 输出选中卡牌，
`--mode merged` 在当前规则副本上替换选中卡牌；两种产物都通过 Schema 和跨目录引用合同，
不会自动写入 `data/generated/card_rules_v2.json`。

人工覆盖规则的使用规范见 `docs/manual_overrides.md`；SWB-RL 选择性迁移门禁见
`docs/swb_rl_migration.md`。

## 测试

项目自带单元测试（`test_*.py`），覆盖：斩杀引擎示例路线、事件解释器、快照适配、Tracker 合同与刷新会话、文本归一化、规则 schema 校验、Step 7 合同门禁、SWB-RL 兼容性导入、RuleBook 适配、选择性迁移、原生后端和 Tracker 影子状态 focused tests 等，共 250 项（以本地 `unittest` 运行为准）。
