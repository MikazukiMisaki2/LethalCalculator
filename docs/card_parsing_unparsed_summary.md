# 卡牌规则解析增强（unparsed\_clauses 处理）—— 任务总结

> 面向 Codex / 后续协作者的工程说明。目标：让接手者 5 分钟内理解「做了什么、生成了哪些文件、以什么规则生成、如何复现」，并能继续处理剩余问题。
> 项目：
>
> `D:\Github\LethalCalculator`
>
> （《影之诗 World's Beyond》斩杀求解器）
> 环境：Windows / PowerShell；Python 3.14；编码一律 UTF-8（PowerShell 读中文先 
>
> `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8`
>
> ）



***

## 1. 任务目标

原始文件 `data/generated/card_rules_v2.json`（827598 B，**只读不可改**，sha256 `8FBBEBC0D4BC37BC6410A80740443210780AE2732F5D529C9141B3953747A8AC`）中，有部分卡牌带 `unparsed_clauses`（无法编译成规则的子句）。

本次任务分 4 个阶段推进：



1. **提取**：把 `unparsed_clauses` 从 v2 抽到独立文件，不碰原文件。

2. **首轮重解析**：用项目解析器对提取出的子句重新解析。

3. **中文 → 英文替换**：对仍为中文的子句，用双语卡文本配对出英文描述后重解析，与已解析结果合并。

4. **解析器增强**：改核心解析器 `card_text_ast.py`（parse 层）与 `compile_card_rules.py`（compile 层），让更多子句能被原生解析，产出增强版完整规则 + 最终解析结果 v3。



***

## 2. 数据管线背景（前置知识）

卡牌文本 → 规则的全流程（本次未改动）：



```
cards.json / shadowverse\_cards\_en.json

&#x20; → clean\_cards.py                 → data/generated/card\_catalog.json

&#x20; → normalize\_card\_text.py         → data/generated/card\_text\_normalized.json   (双语成对 clause)

&#x20; → parse\_card\_text.py             → data/generated/card\_text\_ast.json          (AST：abilities + clauses)

&#x20; → compile\_card\_rules.py          → data/generated/card\_rules\_generated.json   (rules，含 unparsed\_clauses)
```

`card_rules_v2.json` 与 `card_rules_generated.json` 同源（v2 为基线快照）。

**关键模块入口**：



* `card_text_ast.clause_to_ast(clause)` — 单条子句 → AST 节点。`clause` 需含 `plain` / `language` / `source_key` / `index` / `section`。

* `compile_card_rules.effect(node, name_index)` — AST 效果节点 → 规则 `op`。`name_index` = catalog 卡名（中英文各自 casefold）→ `card_id`。

* `card_to_ast(card)` — 整卡：分句 → 每句 `clause_to_ast` → 双语配对对比（matched /semantic\_conflict/parser\_asymmetry /unparsed）→ 组 `abilities`。

**原始 unparsed 分布（v2 基线）**：168 张卡 / 195 条 = 80 条引用标记（`unresolved_card_reference:<ability_id>`，格式同 AST ability\_id，如 `10072120:skill:2:super_evolve:normal`）+ 115 条文本（87 英文 / 28 中文）。



***

## 3. 阶段工作流与生成规则

### 阶段 1 — 提取（脚本 `extract_unparsed_clauses.py`）

**产物**：`data/generated/unparsed_clauses_extracted.json`（93047 B）

**规则**：



* 遍历 `rules[*].unparsed_clauses`，按 `MARKER_RE = ^unresolved_card_reference:(.+)$` 分类：


  * 命中 → `kind="marker"`，记录 `ability_id`（cap 为 `<ability_id>`）

  * 未命中 → `kind="text"`，语言检测：含 ASCII 字母 → `eng`；含 CJK → `chs`；否则 `other`

* 每条带卡上下文：`card_id`、`name`（从 v2 rule 或 AST 补全）、`support`、`modes`

* summary：`cards=168, clauses=195, markers=80, text_clauses=115`

### 阶段 2 — 首轮重解析（`extract_unparsed_clauses.py` 的 `reparse()`）

**产物**：`data/generated/unparsed_clauses_parse_result.json`（138432 B）

**规则**（每条子句独立判状态）：



* **marker**：用 `ability_id` 反查 AST `abilities`；对其所有 `effects` 递归执行 `resolve_source_card_names(node, name_index)` 收集无法落成 card\_id 的 `source_card_name`（只查 `summon` / `add_to_hand` / `transform` / `gain_crest` 四类，递归进 effects/steps/choices/else\_effects）。无缺失且 `effect()` 能编译出非空结果 → `marker_resolved`；否则 `marker_still_unresolved`（带 `missing_card_references`）。

* **text**：构造 `{plain: text, language, source_key:"skill", index:0, section:"normal"}` → `clause_to_ast` → `resolve_source_card_names` 反查：


  * 有 effects 且无缺失引用 → `parsed`

  * 有 effects 但有缺失引用 → `parsed_with_unresolved_refs`

  * 无 effects → `unparsed`

**结果**：`parsed=83, unparsed=31, marker_still_unresolved=80, parsed_with_unresolved_refs=1`

### 阶段 3 — 中文→英文替换并合并（脚本 `parse_unparsed_with_english.py`）

**产物**：`data/generated/unparsed_clauses_parse_result_v2.json`（140691 B，schema\_version 2）

**规则**：



* 保留阶段 2 中 `parsed` 与 `parsed_with_unresolved_refs` 条目原样。

* 对 `unparsed` 且 `language=="chs"` 的子句，调 `find_english(cid, chs_text, pair_index, ast_by_id, en_data)` 取英文，三层兜底：

1. **normalized 精确配对**：`card_text_normalized.json` 按 `(source_key, index, section)` 分组，同组内 `nspace(chs)==nspace(目标中文)` 且组内有 `eng` → 返回 eng（去空白比较：`re.sub(r"\s+","",s)`）

2. **同卡子串**：同卡任意组，`nspace(chs)` 与目标互为子串

3. **AST source\_clause 兜底**：`abilities[].source_clause.{chs,eng}` 精确配对

* 拿到英文后重新 `clause_to_ast`，按阶段 2 同一套规则判状态；新条目带 `original_language="chs"`、`original_text`、`english_replacement`。

* 全部条目合并写 v2。

**结果**：`prev_parsed=83, total_parsed=90, unparsed=25, marker_still_unresolved=80, chs_converted=6, newly_parsed=6, prev_parsed_with_unresolved_refs=1`（6 条中文全部转英文解析成功，映射见文末附录 A）。

### 阶段 4 — 解析器增强 + v3（修改 `card_text_ast.py`、`compile_card_rules.py`；新脚本 `parse_enhanced_v3.py`）

**4a. parse 层增强（**`card_text_ast.py`**）** — 新增 `_SUMMON_TRAILER_RE` + `_split_card_names()`，并在 `clause_to_ast` 末尾（`if not node["effects"]:` 之前）追加一批新模式：



| 类别                      | 规则                                                                                                                                                                                                                                                                           |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 卡名切分                    | `_split_card_names`：`A, B, and C` / `A and B` 拆成多个卡名；`and give it X` / `, give it X` / `and evolve it` / `and remove Last Words from it` / `and return this card to hand` / `set its cost` / `destroy this card` / `add ... to your hand` 等后缀剥离，重新 `clause_to_ast` 递归解析成独立效果 |
| summon 块                | `summon_match` 结果改走 `_split_card_names`，逐个卡名生成 `{"kind":"summon","count":N,"source_card_name":name}`，后缀效果并入 `effects`                                                                                                                                                        |
| multi\_add / add\_match | 同样走 `_split_card_names` 拆分多卡                                                                                                                                                                                                                                                 |
| crest\_gain 修复          | 正则 `[^.]` 改 `[^。；;\"」]` + `rstrip(".")`，解决 `Istyndet vs. Mitilykket` 被句号截断                                                                                                                                                                                                   |
| banish                  | `Select an enemy (follower\|card) ... and banish it`（可选 `with N defense or less`）；`Banish all duplicates from your deck`；`Banish all enemy copies of it from the field`                                                                                                      |
| crest                   | `Give (your) opponent Crest: X` → `gain_crest(target=enemy_leader)`；`Advance the count of your Crest: X by N` → `modify_crest`                                                                                                                                               |
| draw                    | `Draw N differently named M-cost (spells\|cards)` → `draw` 带 `distinct_names` / `max_cost` 过滤                                                                                                                                                                                |
| 触发                      | `When you draw this card, set its cost to N ...` → `set_cost(duration=until_end_of_turn, trigger=on_draw)`；`When this card is discarded, give a random allied follower +A/+D` → `buff(trigger=on_discard)`                                                                   |
| 代词 buff                 | `Give them +A/+D` → `buff`（target 回退 `_target` 结果）                                                                                                                                                                                                                           |
| 变量                      | `Destroy X random enemy followers. X is ...` → `destroy(count="var:X", count_source=...)`；`give it +X/+Y` → `buff("var:X"/"var:Y")`                                                                                                                                          |
| 费用 / 牌组                 | `Halve the cost of all cards in your deck` → `modify_cost(operation=halve)`；`Add N copies of X to your deck` → `add_to_hand(target_zone=deck)`；`Reduce the cost of all <class> <type> in your deck by N` → `modify_cost`；`Replace your deck with X` → `replace_deck`         |
| transform               | `Transform all allied followers ... into exact copies of random followers in your deck` → `transform(source={zone:deck, selection:random})`；`Transform a random spell in your hand into X` → `transform(target hand random)`                                                 |

**4b. compile 层增强（**`compile_card_rules.py`**）**：



* 补 `import re`（新增代码依赖）。

* 新增 `_CARD_NAME_CUTTERS`（按 `and` / `, ` / ` to your hand` / ` that costs` / ` from your deck` / ` with last words` / ` in your hand` 切分）与 `_resolve_card_name(name_index, raw)`：

1. 整串 casefold 直查

2. 切分后逐段直查（回收 `Steelclad Knight and give it Ward` 里的 `Steelclad Knight`）

3. 前缀 / 后缀匹配（回收被句号截断的 `Istyndet vs`）

* `gain_crest` / `transform` / `summon` / `add_to_hand` 的卡名解析全部改用 `_resolve_card_name`。

**4c. v3 重解析（脚本&#x20;**`parse_enhanced_v3.py`**）**：



* 输入改读增强后管线：`card_text_ast_enhanced.json` + `card_rules_enhanced.json` + catalog + normalized。

* 从增强后 rules 提取**剩余** unparsed\_clauses，重走阶段 2/3 的判定；对中文子句**先直接&#x20;**`clause_to_ast`**&#x20;解析（增强后能解析部分中文模式），失败才&#x20;**`find_english`。

* `resolve_source_card_names` 同步升级：直查失败且 `_resolve_card_name` 也失败才算缺失。

**结果**：



* parse 报告 `unparsed_clauses`：251 → 180

* 增强后 rules：`data/generated/card_rules_enhanced.json`（904 卡：803 generated / 91 partial / 10 unsupported）

* v3 summary：`parsed=44（全部 text）, marker_still_unresolved=65, chs_converted=6`



***

## 4. 产物文件清单

### 4.1 本任务生成 / 修改的文件



| 文件                                                       | 类型     | 大小        | 说明                                              |
| -------------------------------------------------------- | ------ | --------- | ----------------------------------------------- |
| `docs/card_parsing_unparsed_summary.md`                  | 新增     | —         | 本文档                                             |
| `extract_unparsed_clauses.py`                            | 新增脚本   | —         | 阶段 1+2：提取 + 首轮重解析                               |
| `parse_unparsed_with_english.py`                         | 新增脚本   | —         | 阶段 3：中文→英文 + 合并 v2                              |
| `parse_enhanced_v3.py`                                   | 新增脚本   | —         | 阶段 4c：增强后 v3 重解析                                |
| `card_text_ast.py`                                       | **修改** | —         | parse 层增强（卡名切分 + 新模式）                           |
| `compile_card_rules.py`                                  | **修改** | —         | compile 层增强（`_resolve_card_name` + `import re`） |
| `data/generated/unparsed_clauses_extracted.json`         | 生成     | 93047 B   | 提取产物（168 卡 / 195 条）                             |
| `data/generated/unparsed_clauses_parse_result.json`      | 生成     | 138432 B  | 首轮重解析（parsed 83）                                |
| `data/generated/unparsed_clauses_parse_result_v2.json`   | 生成     | 140691 B  | 中文转英文合并（parsed 90）                              |
| `data/generated/card_text_ast_enhanced.json`             | 生成     | 4834455 B | 增强 parse 后 AST（904 卡）                           |
| `data/generated/card_text_parse_report_enhanced.json`    | 生成     | 1120 B    | 增强 parse 统计报告                                   |
| `data/generated/card_rules_enhanced.json`                | 生成     | 881103 B  | 增强 compile 后完整规则（904 卡）                         |
| `data/generated/card_rules_compile_report_enhanced.json` | 生成     | 254 B     | 增强 compile 统计报告                                 |
| `data/generated/unparsed_clauses_parse_result_v3.json`   | 生成     | 70815 B   | 最终增强解析结果（44 text 全 parsed + 65 marker）          |

### 4.2 各 JSON 的 schema 要点



* `unparsed_clauses_extracted.json`：`{schema_version:1, source_file, extracted_at, summary, cards:{card_id:{card_id,name,support,modes,unparsed_clauses:[{kind:marker|text, text, language?, ability_id?}]}}}`

* `unparsed_clauses_parse_result.json`**&#x20;/ v2 / v3**：`{schema_version, source_files?, created_at, summary, cards:{card_id:{card_id,name,support,parsed_entries:[...]}}}`


  * `parsed_entries[].status` 取值：`parsed` / `parsed_with_unresolved_refs` / `unparsed` / `marker_resolved` / `marker_still_unresolved` / `no_english_found`；v2/v3 中文条目另带 `original_language` / `original_text` / `english_replacement`。

* `card_rules_enhanced.json`：`{schema_version:2, catalog_version:1, game_version, rules:{card_id:{..., modes, unparsed_clauses?, ...}}}`（与 v2 同结构，仅内容为增强后）。



***

## 5. 验证与结果



* **单元测试**：`python -m unittest discover -s . -p "test_*.py"` → **51 项全部 OK**（增强前后一致，无回归）。

* **原文件完整性**：`card_rules_v2.json` sha256 `8FBBEBC0...` 全程未变。

* **增强净效果**（对比 v2 基线）：



| 指标            | 增强前 | 增强后                        |
| ------------- | --- | -------------------------- |
| 含 unparsed 的卡 | 168 | 91                         |
| 未解析文本子句       | 115 | 44（**剩余 44 条 v3 全部重解析成功**） |
| 未解析引用标记       | 80  | 65                         |



* 已抽查确认：被解决的 71 条文本子句**真实编译进** `card_rules_enhanced.json` 的 abilities（出现 `banish` / `gain_crest` / `modify_crest` / `destroy` / `draw` / `buff` / `transform` / `modify_cost` 等 op），并非仅从列表消失。



***

## 6. 复现命令（PowerShell）



```
cd D:\Github\LethalCalculator

\[Console]::OutputEncoding=\[System.Text.Encoding]::UTF8

\# 阶段 1+2

python extract\_unparsed\_clauses.py

\# 阶段 3

python parse\_unparsed\_with\_english.py

\# 阶段 4：增强 parse + compile（输出到 enhanced 文件，不覆盖 v2/generated）

python parse\_card\_text.py --input data/generated/card\_text\_normalized.json --output data/generated/card\_text\_ast\_enhanced.json --report data/generated/card\_text\_parse\_report\_enhanced.json

python compile\_card\_rules.py --catalog data/generated/card\_catalog.json --ast data/generated/card\_text\_ast\_enhanced.json --output data/generated/card\_rules\_enhanced.json --report data/generated/card\_rules\_compile\_report\_enhanced.json

\# 阶段 4c：v3

python parse\_enhanced\_v3.py

\# 回归

python -m unittest discover -s . -p "test\_\*.py"
```



***

## 7. 剩余问题（供后续处理）



1. **65 条&#x20;**`marker_still_unresolved`：均为深层语义，无法静态落成 card\_id：

* 代词回指：`copy of it` / `this card` / `exact copy of each` / `give the exact copy ...`

* 随机牌组检索：`random X from your deck` / `2 random differently named followers ... from your deck`

* 对局状态引用：`random allied follower destroyed this match` / `random card in your opponent's hand`

* 增强替换：`N instead` / `Summon 4 instead`

* 需在求解器运行时按状态动态解析（runtime resource selector），或在 `effect()` 里降级为 `resource_selector` 语义。

1. 未入库的卡名 token（如 `Ersatz Elimination`）若 catalog 缺失，`_resolve_card_name` 会返回 None → 仍标 marker；需要时可并入 `card_catalog.json`。



***

## 附录 A：阶段 3 的 6 条中文 → 英文映射



| card\_id | 卡名                             | 中文子句（节选）             | 英文替换                                                                                                                                           |
| -------- | ------------------------------ | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 10414110 | Ewiyar, Wind Personified       | 【入场曲】【奥义】回复自己 1 点进化点 | Fanfare: Skybound Art - Recover 1 evolution point.                                                                                             |
| 10471120 | Tsubasa, Blazing Gearcyclist   | 【入场曲】使所有手牌奥义槽 + 1    | Fanfare: Increase the Skybound Art gauges of all cards in your hand by 1.                                                                      |
| 10443110 | Meg, Girl Next Door            | 【入场曲】【奥义】本随从超进化      | Fanfare: Skybound Art - Super-evolve this follower.                                                                                            |
| 10424120 | Seofon, Leader of the Eternals | 使所有进化前随从进化… 改为超进化    | Fanfare: Skybound Art - Evolve all unevolved allied followers on the field. Super Skybound Art - Super-evolve them instead.                    |
| 10453310 | Corruption                     | 全体随从 - 2/-2… 破坏堕落纹章  | Give all followers on the field -2/-2. Give yourself and your opponent Crest: Corruption. Super Skybound Art - Destroy your Crest: Corruption. |
| 10454120 | Belial, Archangel of Cunning   | 对其它随从造成 10 点伤害… 获纹章  | Fanfare: Deal 10 damage to all other followers. Super Skybound Art - Gain Crest: Belial, Archangel of Cunning.                                 |

***

## 8. CardRules v2 契约修复（当前基线）

上面的历史章节描述的是旧增强快照；当前默认产物已由英文主解析管线重新生成，旧快照保存在 `data/generated/card_rules_v2_before_contract_fix.json`。本轮不再把无法确定语义的关系伪装成可执行效果：

* `instead` 在同源 Enhance、Skybound Art、Super-Evolve 或明确条件分支中会物化为完整替代效果；无法唯一定位基础效果时保留 `modify_previous_effect` 并标记 `partial`。
* 补齐 `on_draw`/`on_discard`、`gain_status`、`set_stat`、`add_to_zone`、`replace_deck` 等 v2 契约字段，并让 `merge_card_rules.py` 输出 `ruleset_revision: 2`。
* `ordered_split` 保持敌方战场顺序分配语义；随机目标和未实现操作继续由运行时返回 `INCOMPLETE`，不会静默当作确定结果。
* 当前默认英文主文本管线的 904 张卡编译统计为 `generated=904`、`partial=0`、`unsupported=0`；修正了 `Enhanced Puppet` 被误识别为 Enhance 模式的问题，清理了已完整编译的中文重复标记，并为多选 Mode、Golem 触发器和 Strike 等事件补齐了结构化输出。文本解析报告仍保留中文缺失翻译/展示句的审计计数；生成规则中若使用支持矩阵标为 `planned` 的操作，或运行时缺少公开随机池，解释器仍会显式返回 `INCOMPLETE`，不会伪装成确定斩杀。
* 10804110 的三个模式选项已分别编译为 follower/amulet/crest 分支；10224120 的触发能力已拆分为 `trigger_source` 状态、敌方主战者伤害和己方主战者回复，但其“召唤敌方 Knight 复制”仍保留未解析引用。
* 当前默认文件：`data/generated/card_rules_v2.json`；Schema 与支持矩阵分别为 `schemas/card_rules_v2.schema.json`、`schemas/card_rules_v2_support.json`。
