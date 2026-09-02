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
```

人工覆盖规则的使用规范见 `docs/manual_overrides.md`。

## 测试

项目自带单元测试（`test_*.py`），覆盖：斩杀引擎示例路线、事件解释器、快照适配、Tracker 合同与刷新会话、文本归一化、规则 schema 校验、Step 7 合同门禁等，共 211 项。
