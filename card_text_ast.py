"""Build a conservative intermediate AST from normalized bilingual clauses."""
from __future__ import annotations

import re
from typing import Any


def _amount(text: str) -> int | str | None:
    m = re.search(r"(?:deal|damage|造成|伤害)\s*(\d+|x)|([0-9]+|x)\s*(?:damage|点伤害)", text, re.I)
    if not m:
        return None
    value = next(x for x in m.groups() if x is not None)
    return int(value) if value.isdigit() else "variable"


def _variable_source(text: str) -> str | None:
    value = text.lower()
    if re.search(r"(?:pixie|fairy) followers? in your hand|手牌中.*(?:妖精|精灵).*随从", text, re.I):
        tribe = "pixie" if re.search(r"pixie|妖精", text, re.I) else "fairy"
        return f"var:hand_tribe:{tribe}"
    if re.search(r"number of .*cards? in your hand|手牌中.*(?:张数|数量)", text, re.I):
        return "var:hand_count"
    if re.search(r"number of crests|纹章(?:数|数量)", text, re.I):
        return "var:crest_count"
    if re.search(r"number of shadows|墓地(?:数|数量)|shadows in your cemetery", text, re.I):
        return "var:cemetery"
    if re.search(r"this follower's attack|本随从的攻击力|该随从的攻击力", text, re.I):
        return "var:source_attack"
    if re.search(r"number of enemy followers|敌方.*随从.*(?:数|数量)", text, re.I):
        return "var:enemy_board_count"
    if re.search(r"your combo|自己的【?连击】?", text, re.I):
        return "var:play_count"
    return None


def split_mode_clauses(clause: dict[str, Any]) -> list[dict[str, Any]]:
    """Split inline Enhance/Accelerate/Crystallize sections.

    The normalized source keeps a whole ability sentence in one clause. This
    helper creates deterministic virtual clauses while retaining the original
    source key/index for bilingual pairing and auditability.
    """
    text = clause.get("plain", "")
    marker = re.compile(r"(?P<label>enhance|accelerate|crystallize)\s*\(\s*(?P<cost>\d+)\s*\)\s*:\s*|(?P<chs>爆能强化|加速|结晶)[_（(]?\s*(?P<chs_cost>\d+)?\s*[）)]?\s*】?\s*[:：]?", re.I)
    matches = list(marker.finditer(text))
    if not matches:
        return [clause]
    pieces: list[dict[str, Any]] = []
    first = matches[0]
    prefix = text[:first.start()].strip("【】 ")
    if prefix:
        item = dict(clause)
        item["plain"] = prefix
        item["mode_override"] = None
        pieces.append(item)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip("【】 ")
        label = (match.group("label") or match.group("chs") or "").lower()
        mode = {"enhance": "enhance", "accelerate": "accelerate", "crystallize": "crystallize", "爆能强化": "enhance", "加速": "accelerate", "结晶": "crystallize"}.get(label, label)
        item = dict(clause)
        item["plain"] = body
        item["mode_override"] = mode
        item["mode_cost"] = int(match.group("cost") or match.group("chs_cost") or 0)
        pieces.append(item)
    return pieces


def _target(text: str) -> dict[str, Any] | None:
    value = text.lower()
    random_enemy_count = re.search(r"(?:deal damage to\s+)?(\d+)\s+random enemy followers?", text, re.I)
    if random_enemy_count:
        return {"scope": "enemy_follower", "selection": "random", "count": int(random_enemy_count.group(1))}
    enemy_count = re.search(r"select\s+(\d+)\s+enemy followers?", text, re.I)
    if enemy_count:
        return {"scope": "enemy_follower", "selection": "chosen", "count": int(enemy_count.group(1))}
    allied_other_count = re.search(r"select\s+(\d+)\s+other allied followers?", text, re.I)
    if allied_other_count:
        return {"scope": "ally_follower", "selection": "chosen", "count": int(allied_other_count.group(1)), "filters": {"exclude_source": True}}
    hand_selected = re.search(r"select\s+(?:a|an|\d+)\s+(?:(\w+)\s+)?(?:card|follower|spell|amulet)\s+in your hand", text, re.I)
    if hand_selected:
        kind_match = re.search(r"select\s+(?:a|an|\d+)\s+(card|follower|spell|amulet)\s+in your hand", text, re.I)
        filters: dict[str, Any] = {"zone": "hand"}
        if kind_match and kind_match.group(1).lower() != "card":
            filters["card_type"] = kind_match.group(1).lower()
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": filters}
    # Explicit multi-card selection must be resolved before broad scope
    # fallbacks. `other cards` excludes the source permanent itself.
    count_match = re.search(r"(?:select|choose)\s+(\d+)\s+other\s+cards?|选择[^。；]*?(\d+)张其他卡牌", text, re.I)
    if count_match:
        count = int(next(item for item in count_match.groups() if item))
        return {"scope": "any", "selection": "chosen", "count": count, "filters": {"exclude_source": True, "card_type": "field_card"}}
    count_match = re.search(r"(?:select|choose)\s+(\d+)\s+cards?|选择[^。；]*?(\d+)张卡牌", text, re.I)
    if count_match:
        count = int(next(item for item in count_match.groups() if item))
        return {"scope": "any", "selection": "chosen", "count": count, "filters": {"card_type": "field_card"}}
    if "enemy leader" in value or re.search(r"(?:对手的|敌方的)主战者", text):
        return {"scope": "enemy_leader"}
    if "your leader" in value or re.search(r"(?:自己的|你的)主战者", text):
        return {"scope": "ally_leader"}
    if "主战者" in text:
        return {"scope": "enemy_leader"}
    if "all enemies" in value:
        return {"scope": "any", "selection": "all", "filters": {"side": "enemy"}}
    if "random ally or enemy" in value:
        return {"scope": "any", "selection": "random", "count": 1, "filters": {"card_type": "follower"}}
    if "random enemy follower" in value or ("随机" in text and "随从" in text):
        return {"scope": "enemy_follower", "selection": "random", "count": 1}
    if "split between all enemy followers" in value or "所有随从分配" in text:
        return {"scope": "enemy_follower", "selection": "all", "allocation": "ordered_split"}
    if "all enemy follower" in value or ("所有随从" in text and ("对手" in text or "敌方" in text)):
        return {"scope": "enemy_follower", "selection": "all"}
    allied_tribe = re.search(r"all (other )?allied\s+([a-z]+)\s+followers?", text, re.I)
    if allied_tribe:
        filters = {"tribe": allied_tribe.group(2).lower()}
        if allied_tribe.group(1):
            filters["exclude_source"] = True
        return {"scope": "ally_follower", "selection": "all", "filters": filters}
    if "all followers" in value or "all other followers" in value or "战场上的所有随从" in text:
        target = {"scope": "any", "selection": "all"}
        if "other follower" in value or "其他所有随从" in text:
            target["filters"] = {"exclude_source": True, "card_type": "follower"}
        return target
    if "opposing follower" in value or "交战对手" in text:
        return {"scope": "trigger_source"}
    if "both leaders" in value or "all leaders" in value or "所有主战者" in text:
        return {"scope": "any", "selection": "all", "filters": {"card_type": "leader"}}
    if "leaders with the lowest defense" in value or "生命值最小的所有主战者" in text:
        return {"scope": "any", "selection": "lowest_life", "filters": {"card_type": "leader"}}
    if "enemy follower" in value or ("对手" in text and "随从" in text):
        result: dict[str, Any] = {"scope": "enemy_follower", "selection": "chosen", "count": 1}
        max_life = re.search(r"with\s+(\d+)\s+defense or less", text, re.I)
        if max_life:
            result["filters"] = {"max_life": int(max_life.group(1))}
        return result
    if "enemy card" in value:
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": {"side": "enemy", "zone": "field", "card_type": "field_card"}}
    if "another allied card" in value:
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": {"side": "ally", "zone": "field", "exclude_source": True, "card_type": "field_card"}}
    if "another follower" in value:
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "field", "exclude_source": True, "card_type": "follower"}}
    if re.search(r"select a card on the field", text, re.I):
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "field", "card_type": "field_card"}}
    if re.search(r"\bthis (?:card|follower)\b|本卡牌|本随从", text, re.I):
        return {"scope": "self"}
    return None


def _damage_effects(text: str) -> list[dict[str, Any]]:
    """Extract each sentence-level damage operation independently."""
    effects: list[dict[str, Any]] = []
    whole_source = _variable_source(text)
    fragments = [part.strip(' \"“”') for part in re.split(r"(?<=[。.!?])\s*", text) if part.strip()]
    for fragment in fragments:
        amount = _amount(fragment)
        if amount == "variable":
            amount = _variable_source(fragment) or whole_source or "var:X"
        target = _target(fragment)
        if amount is None or target is None:
            continue
        item: dict[str, Any] = {"kind": "damage", "target": target, "amount": amount}
        count = _repeat(fragment)
        if count is not None:
            item = {"kind": "repeat", "count": count, "effects": [item]}
        effects.append(item)
    return effects


def _is_destroy_action(text: str) -> bool:
    """Recognize an instruction to destroy, not a historical/conditional mention."""
    value = text.strip()
    patterns = (
        r"\bdestroy\s+(?:this card|it|that card|the selected|the opposing follower|an? |all |\d+ )",
        r"\b(?:select|choose)\b[^.]*\b(?:and\s+)?destroy\b",
        r"(?:破坏本卡牌|将(?:其|它|这些卡牌|该卡牌|所选择的卡牌)破坏|选择[^。]*，?破坏)",
    )
    return any(re.search(pattern, value, re.I) for pattern in patterns)


def _effect_map(effects: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in effects:
        kind = str(item.get("kind", ""))
        result.setdefault(kind, set()).add(json_signature(item))
    return result


def comparison_signature(value: Any) -> str:
    """Language-neutral AST signature used only for bilingual pairing."""
    if isinstance(value, dict):
        value = {key: comparison_signature(item) if isinstance(item, (dict, list)) else item for key, item in value.items() if key != "source_card_name"}
    elif isinstance(value, list):
        value = [comparison_signature(item) if isinstance(item, (dict, list)) else item for item in value]
    return json_signature(value)


def _has_real_effect_conflict(chs: dict[str, Any], eng: dict[str, Any]) -> bool:
    """Only flag contradictions both parsers actually understood.

    Extra effects on one language are parser coverage gaps. A real conflict needs
    the same parsed effect kind on both sides with incompatible AST values.
    """
    def scalar_facts(effects: list[dict[str, Any]]) -> dict[str, list[Any]]:
        facts: dict[str, list[Any]] = {}
        for item in effects:
            kind = str(item.get("kind", ""))
            if kind == "repeat":
                facts.setdefault("repeat_count", []).append(item.get("count"))
                nested = scalar_facts(item.get("effects", []))
                for nested_kind, values in nested.items():
                    facts.setdefault(f"repeat.{nested_kind}", []).extend(values)
            elif "amount" in item:
                facts.setdefault(f"{kind}.amount", []).append(item.get("amount"))
            elif kind == "buff":
                facts.setdefault("buff.stats", []).append((item.get("attack"), item.get("life")))
        return facts

    left = scalar_facts(chs.get("effects", []))
    right = scalar_facts(eng.get("effects", []))
    if set(_effect_map(chs.get("effects", []))) != set(_effect_map(eng.get("effects", []))):
        return False
    damage_pattern = r"(?:deal\s+\d+\s+damage|\d+\s+damage|造成\s*\d+\s*点伤害|\d+\s*点伤害)"
    if max(len(re.findall(damage_pattern, str(chs.get("source_clause", "")), re.I)), len(re.findall(damage_pattern, str(eng.get("source_clause", "")), re.I))) > 1:
        return False
    # Target extraction is intentionally excluded here: until references such
    # as "it" are resolved, differing targets indicate parser asymmetry, not a
    # trustworthy source-text contradiction.
    return any(
        len(left[key]) == len(right[key])
        and left[key] != right[key]
        and not any(isinstance(value, str) for value in left[key] + right[key])
        for key in set(left) & set(right)
    )


def _repeat(text: str) -> int | str | None:
    m = re.search(r"do this\s+(\d+|x)\s+times|发动\s*(\d+|x)\s*次", text, re.I)
    if not m:
        return None
    value = next(x for x in m.groups() if x is not None)
    return int(value) if value.isdigit() else "variable"


def _mode(text: str) -> str | None:
    value = text.lower()
    if "select a mode" in value or ("模式" in text and re.search(r"选择|发动", text)):
        return "mode_selection"
    if "super skybound art" in value or "解放奥义" in text:
        return "super_skybound_art"
    if "skybound art" in value or "奥义" in text:
        return "skybound_art"
    if "enhance" in value or "爆能强化" in text:
        return "enhance"
    if "accelerate" in value or "加速" in text:
        return "accelerate"
    if "crystallize" in value or "结晶" in text:
        return "crystallize"
    return None


def _conditions(text: str) -> list[dict[str, Any]]:
    result = []
    for pattern, state in ((r"rally\s*\((\d+)\)|协作[_ ]?(\d+)", "rally"), (r"combo\s*\((\d+)\)|连击[_ ]?(\d+)", "play_count"), (r"necromancy\s*\((\d+)\)|唤灵[_ ]?(\d+)", "cemetery"), (r"earth rite\s*\((\d+)\)|土之秘术[_ ]?(\d+)", "earth_sigil")):
        match = re.search(pattern, text, re.I)
        if match:
            amount = int(next(value for value in match.groups() if value))
            result.append({"state": state, "cmp": "gte", "value": amount})
    if re.search(r"overflow|觉醒", text, re.I):
        result.append({"state": "awakening", "cmp": "eq", "value": True})
    for pattern, cmp_name in ((r"(?:at least|至少)\s*(\d+)", "gte"), (r"(?:at most|至多)\s*(\d+)", "lte")):
        match = re.search(pattern, text, re.I)
        if match and re.search(r"\bX\b|变量|数值", text, re.I):
            result.append({"state": "variable", "name": "X", "cmp": cmp_name, "value": int(match.group(1))})
    if re.search(r"this follower is unevolved|本随从未进化", text, re.I):
        result.append({"state": "evolved", "cmp": "eq", "value": False})
    if re.search(r"this follower is evolved|本随从已进化", text, re.I):
        result.append({"state": "evolved", "cmp": "eq", "value": True})
    evolved_match = re.search(r"allied followers have evolved at least\s*(\d+)\s*times this match|随从的进化次数为\s*(\d+)次或以上", text, re.I)
    if evolved_match:
        result.append({"state": "evolved_allies_this_match", "cmp": "gte", "value": int(next(item for item in evolved_match.groups() if item))})
    base_costs = re.search(r"played cards with base costs of\s*([\d,\sand]+)\s+this match", text, re.I)
    if base_costs:
        result.append({"state": "played_base_cost_set", "cmp": "contains_all", "value": [int(item) for item in re.findall(r"\d+", base_costs.group(1))]})
    source_life = re.search(r"this follower's defense is\s*(\d+)\s*or less|本随从的生命值为\s*(\d+)或以下", text, re.I)
    if source_life:
        result.append({"state": "source_life", "cmp": "lte", "value": int(next(item for item in source_life.groups() if item))})
    return result


def clause_to_ast(clause: dict[str, Any]) -> dict[str, Any]:
    text = clause.get("plain", "")
    structure = clause.get("structure", {})
    node: dict[str, Any] = {
        "language": clause.get("language", ""),
        "source_language": clause.get("language", ""),
        "source_key": clause.get("source_key", ""),
        "index": clause.get("index", 0),
        "section": clause.get("section", "normal"),
        "trigger": clause.get("trigger", "static"),
        "mode": clause.get("mode_override", _mode(text)),
        "mode_cost": clause.get("mode_cost"),
        "conditions": _conditions(text),
        "source_text": text,
        "source_clause": text,
        "effects": [],
        "variable_initializers": {},
        "static_keywords": [],
        "unparsed": [],
        "unparsed_clauses": [],
    }
    engage_cost = re.search(r"engage\s*\((\d+)\)|费用\s*(\d+)\s*【启动】", text, re.I)
    if engage_cost:
        node["mode_cost"] = int(next(item for item in engage_cost.groups() if item))
    countdown_header = re.search(r"countdown\s*\((\d+)\)|吟唱[_ ]?(\d+)", text, re.I)
    if countdown_header:
        node["countdown_initial"] = int(next(item for item in countdown_header.groups() if item))
    target = _target(text)
    # Some cards put an Enhance/Super-Evolve replacement in a separate
    # sentence. Preserve the relation explicitly instead of pretending it is
    # an independent effect or discarding its dependence on the base clause.
    instead_patterns = (
        (r"select\s+(\d+)\s+instead", "selection_count"),
        (r"add\s+(\d+)(?:\s+copies)?\s+instead", "count"),
        (r"deal\s+(\d+)\s+damage instead", "amount"),
        (r"deal damage to\s+(\d+)\s+random enemy followers? instead", "target_count"),
        (r"do it\s+(\d+)\s+times instead", "repeat_count"),
        (r"restore\s+(\d+)\s+defense instead", "amount"),
    )
    for pattern, field in instead_patterns:
        matched = re.search(pattern, text, re.I)
        if matched:
            node["effects"].append({"kind": "modify_previous_effect", "field": field, "value": int(matched.group(1))})
            break
    if re.search(r"deal damage to all enemy followers instead", text, re.I):
        node["effects"].append({"kind": "modify_previous_effect", "field": "target", "value": {"scope": "enemy_follower", "selection": "all"}})
    if re.search(r"activate an ability in sequence from the following|按顺序发动以下能力", text, re.I):
        options = list(re.finditer(r"(?:^|\s)(?:([1-9]\d*)[.)]|（([1-9]\d*)）)\s*", text))
        effects = []
        for index, match in enumerate(options):
            end = options[index + 1].start() if index + 1 < len(options) else len(text)
            body = text[match.end():end].strip(" 。；;:")
            child = clause_to_ast({**clause, "plain": body})
            if child.get("effects"):
                effects.append({"label": match.group(1) or match.group(2), "effects": child["effects"]})
        if effects:
            node["effects"].append({"kind": "progressive_sequence", "steps": effects})
            node["confidence"] = 1.0 if len(effects) == len(options) else 0.0
            return node
        node["unparsed"].append(text)
        node["unparsed_clauses"].append(text)
        node["confidence"] = 0.0
        return node
    # Numbered mode choices are explicit player choices, not random effects.
    option_matches = list(re.finditer(r"(?:^|\s)(?:([1-9]\d*)[.)]|（([1-9]\d*)）)\s*", text))
    is_mode_choice = _mode(text) == "mode_selection"
    if len(option_matches) >= 2 and is_mode_choice:
        choices = []
        for index, match in enumerate(option_matches):
            end = option_matches[index + 1].start() if index + 1 < len(option_matches) else len(text)
            body = text[match.end():end].strip(" 。；;:")
            sub = clause_to_ast({**clause, "plain": body, "mode_override": None})
            choices.append({"label": match.group(1) or match.group(2), "effects": sub.get("effects", [])})
        node["effects"].append({"kind": "mode_choice", "choices": choices})
        node["confidence"] = 1.0 if all(choice.get("effects") for choice in choices) else 0.0
        return node
    else:
        alternative = re.search(r"(?:combo|rally)\s*\((\d+)\)\s*[-:：]\s*(.+?)\s+instead\b", text, re.I)
        if not alternative:
            alternative = re.search(r"【?(?:连击|协作)[_ ]?(\d+)】?\s*改为\s*(.+)", text, re.I)
        if alternative:
            base_text = text[:alternative.start()].strip()
            branch_text = alternative.group(2).strip()
            branch_effects = _damage_effects(branch_text)
            base_effects = _damage_effects(base_text)
            if not branch_effects and base_effects and re.search(r"deal damage to all enemy followers|对对手的战场上的所有随从造成", branch_text, re.I):
                amount = base_effects[0].get("amount", 0)
                branch_effects = [{"kind": "damage", "target": {"scope": "enemy_follower", "selection": "all"}, "amount": amount}]
            if branch_effects:
                state_name = "play_count" if re.search(r"combo|连击", alternative.group(0), re.I) else "rally"
                node["effects"].append({"kind": "conditional", "condition": {"state": state_name, "cmp": "gte", "value": int(alternative.group(1))}, "effects": branch_effects, "else_effects": base_effects})
            else:
                node["effects"].extend(_damage_effects(text))
        else:
            node["effects"].extend(_damage_effects(text))
    for condition in node["conditions"]:
        if condition.get("state") in ("cemetery", "earth_sigil"):
            node["effects"].insert(0, {"kind": "consume_resource", "resource": condition["state"], "amount": condition.get("value", 0)})
    if re.search(r"evolve this follower|使本随从进化|本随从进化[。！]", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "self"}, "evolution_kind": "normal"})
    if re.search(r"evolve it|使其进化", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "trigger_source"}, "evolution_kind": "normal"})
    draw_match = re.search(r"draw\s+(a|an|\d+|x)\s+(cards?|spells?|followers?|amulets?)|抽[取]?(\d+|X)张?(法术|随从|护符|卡牌)", text, re.I)
    if draw_match:
        value = next((x for x in draw_match.groups() if x), "1")
        card_kind = draw_match.group(2) or draw_match.group(4) or ""
        draw_node = {"kind": "draw", "count": int(value) if value.isdigit() else (_variable_source(text) or (1 if value.lower() in ("a", "an") else "var:X"))}
        if re.search(r"spell|法术", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "spell"}}
        elif re.search(r"follower|随从", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "follower"}}
        elif re.search(r"amulet|护符", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "amulet"}}
        node["effects"].append(draw_node)
    summon_match = re.search(r"summon\s+(?:(\d+)\s+copies of\s+|an?\s+)?([^.;\"]+)|召唤\s*(\d+)?\s*(?:个|张)?『([^』]+)』", text, re.I)
    if summon_match:
        count_value = summon_match.group(1) or summon_match.group(3) or "1"
        card_name = (summon_match.group(2) or summon_match.group(4) or "").strip()
        node["effects"].append({"kind": "summon", "count": int(count_value), "source_card_name": card_name})
    if _is_destroy_action(text):
        node["effects"].append({"kind": "destroy", "target": target or {"scope": "any"}})
    if re.search(r"\bstorm\b|【疾驰】", text, re.I):
        node["effects"].append({"kind": "grant_keyword", "keyword": "storm", "target": {"scope": "self"}})
    if re.search(r"\brush\b|【突进】", text, re.I):
        node["effects"].append({"kind": "grant_keyword", "keyword": "rush", "target": {"scope": "self"}})
    if re.search(r"\bward\b|【守护】", text, re.I):
        node["effects"].append({"kind": "grant_keyword", "keyword": "ward", "target": {"scope": "self"}})
    if re.search(r"activate all of them instead|改为发动所有能力", text, re.I):
        node["effects"].append({"kind": "activate_all_mode_choices"})
    for keyword, pattern in (("bane", r"\bbane\b"), ("ambush", r"\bambush\b"), ("aura", r"\baura\b"), ("barrier", r"\bbarrier\b")):
        if re.search(pattern, text, re.I):
            if not any(effect.get("kind") == "grant_keyword" and effect.get("keyword") == keyword for effect in node["effects"]):
                node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
            if re.fullmatch(r"(?:ambush|bane|aura|barrier)(?:\s+(?:ambush|bane|aura|barrier))*", text.strip(" ."), re.I):
                node["static_keywords"].append(keyword)
    if re.search(r"can't be destroyed by abilities|cannot be destroyed by abilities|不会被能力破坏", text, re.I):
        node["effects"].append({"kind": "grant_keyword", "keyword": "effect_indestructible", "target": {"scope": "self"}})
    for keyword, pattern in (("aura", r"aura|灵气"), ("earth_sigil", r"earth sigil|土之印"), ("unplayable", r"can(?:not|'t) be played|无法使用")):
        if re.fullmatch(pattern, text.strip("【】 。."), re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
            node["static_keywords"].append(keyword)
    for keyword, pattern in (("ambush", r"ambush|潜行"), ("bane", r"bane|必杀|毁灭"), ("drain", r"drain|虹吸"), ("intimidate", r"intimidate|威慑")):
        if re.fullmatch(pattern, text.strip("【】 "), re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
        elif re.search(rf"\bgive\s+(?:it|them|this follower)\s+({pattern})\b", text, re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": target or {"scope": "self"}})
    for keyword, pattern in (("ward", r"ward|守护"), ("storm", r"storm|疾驰"), ("rush", r"rush|突进")):
        if re.match(rf"(?:【)?(?:{pattern})(?:】)?(?:\s|$)", text, re.I) and len(text.strip("【】 ")) > 3:
            node["static_keywords"].append(keyword)
    buff_match = re.search(r"give\s+(this follower|all (?:other )?allied followers?|an allied follower|another allied follower|it|all enemy followers?)[^+\-]*([+-](?:\d+|x))/([+-](?:\d+|x))", text, re.I)
    if buff_match:
        subject = buff_match.group(1).lower()
        scope = "self" if subject == "this follower" else ("enemy_follower" if "enemy" in subject else "ally_follower")
        selection = "all" if subject.startswith("all") else ("chosen" if scope != "self" else None)
        target_node = {"scope": scope}
        if selection:
            target_node["selection"] = selection
        if "other" in subject:
            target_node["filters"] = {"exclude_source": True}
        attack = int(buff_match.group(2)) if buff_match.group(2).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        life = int(buff_match.group(3)) if buff_match.group(3).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        node["effects"].append({"kind": "buff", "target": target_node, "attack": attack, "life": life})
    filtered_buff = re.search(r"give all (other )?allied\s+([a-z]+)\s+followers? on the field\s+([+-]\d+)/([+-]\d+)", text, re.I)
    if filtered_buff:
        filters: dict[str, Any] = {"tribe": filtered_buff.group(2).lower()}
        if filtered_buff.group(1):
            filters["exclude_source"] = True
        node["effects"].append({"kind": "buff", "target": {"scope": "ally_follower", "selection": "all", "filters": filters}, "attack": int(filtered_buff.group(3)), "life": int(filtered_buff.group(4))})
    chs_buff = re.search(r"(?:使|让)?(本随从|自己(?:的)?战场上的(?:所有其他|其他所有|所有)随从|对手(?:的)?战场上的所有随从|其|该随从)\s*([+-](?:\d+|X))/([+-](?:\d+|X))", text, re.I)
    if chs_buff:
        subject = chs_buff.group(1)
        scope = "self" if subject == "本随从" else ("enemy_follower" if "对手" in subject else "ally_follower")
        target_node = {"scope": scope}
        if scope != "self":
            target_node["selection"] = "all" if "所有" in subject else "chosen"
        if "其他" in subject:
            target_node["filters"] = {"exclude_source": True}
        attack = int(chs_buff.group(2)) if chs_buff.group(2).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        life = int(chs_buff.group(3)) if chs_buff.group(3).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        node["effects"].append({"kind": "buff", "target": target_node, "attack": attack, "life": life})
    random_ally_buff = re.search(r"give a random super-evolved allied follower[^+]*([+-]\d+)/([+-]\d+)", text, re.I)
    if random_ally_buff:
        node["effects"].append({"kind": "buff", "target": {"scope": "ally_follower", "selection": "random", "count": 1, "filters": {"super_evolved": True}}, "attack": int(random_ally_buff.group(1)), "life": int(random_ally_buff.group(2))})
    random_enemy_buff = re.search(r"give a random enemy follower[^+\-]*([+-]\d+)/([+-]\d+)", text, re.I)
    if random_enemy_buff:
        buff = {"kind": "buff", "target": {"scope": "enemy_follower", "selection": "random", "count": 1}, "attack": int(random_enemy_buff.group(1)), "life": int(random_enemy_buff.group(2))}
        repeat_count = _repeat(text)
        node["effects"].append({"kind": "repeat", "count": (_variable_source(text) or "var:X") if repeat_count == "variable" else repeat_count, "effects": [buff]} if repeat_count is not None else buff)
    hand_class_buff = re.search(r"give all\s+([a-z]+)\s+followers in your hand\s+([+-]\d+)/([+-]\d+)", text, re.I)
    if hand_class_buff:
        node["effects"].append({"kind": "buff", "target": {"scope": "ally_follower", "selection": "all", "filters": {"zone": "hand", "class": hand_class_buff.group(1).lower()}}, "attack": int(hand_class_buff.group(2)), "life": int(hand_class_buff.group(3))})
    pp_match = re.search(r"recover\s+(\d+)\s+play points?", text, re.I)
    if not pp_match:
        pp_match = re.search(r"回复自己(\d+)点(?:PP|能量点)", text, re.I)
    if pp_match:
        node["effects"].append({"kind": "recover_pp", "amount": int(pp_match.group(1))})
    variable_pp = re.search(r"recover\s+X\s+play points?", text, re.I)
    if variable_pp:
        node["effects"].append({"kind": "recover_pp", "amount": _variable_source(text) or "var:X"})
    max_pp = re.search(r"gain\s+(\d+)\s+max play points?", text, re.I)
    if max_pp:
        node["effects"].append({"kind": "modify_resource", "resource": "max_pp", "amount": int(max_pp.group(1))})
    ep_recovery = re.search(r"recover\s+(\d+)\s+(super-)?evolution points?", text, re.I)
    if ep_recovery:
        node["effects"].append({"kind": "modify_resource", "resource": "sep" if ep_recovery.group(2) else "ep", "amount": int(ep_recovery.group(1))})
    combo_gain = re.search(r"increase your combo by\s+(\d+)", text, re.I)
    if combo_gain:
        node["effects"].append({"kind": "modify_resource", "resource": "play_count", "amount": int(combo_gain.group(1))})
    skybound_gain = re.search(r"increase the skybound art gauges? of all cards in your hand by\s+(\d+)", text, re.I)
    if skybound_gain:
        node["effects"].append({"kind": "modify_resource", "resource": "skybound_art", "amount": int(skybound_gain.group(1)), "target": {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}})
    heal_match = re.search(r"restore\s+(\d+)\s+defense to your leader", text, re.I)
    if not heal_match:
        heal_match = re.search(r"回复自己(?:的)?主战者(\d+)点生命值", text, re.I)
    if heal_match:
        node["effects"].append({"kind": "heal", "target": {"scope": "ally_leader"}, "amount": int(heal_match.group(1))})
    heal_all = re.search(r"restore\s+(\d+)\s+defense to all allies", text, re.I)
    if heal_all:
        node["effects"].append({"kind": "heal", "target": {"scope": "any", "selection": "all", "filters": {"side": "ally"}}, "amount": int(heal_all.group(1))})
    set_defense = re.search(r"set (?:its|the enemy leader's) (?:max )?defense to\s+(\d+)", text, re.I)
    if set_defense:
        set_target = {"scope": "enemy_leader"} if "leader" in set_defense.group(0).lower() else (target or {"scope": "enemy_follower", "selection": "chosen", "count": 1})
        node["effects"].append({"kind": "set_stat", "stat": "max_life" if "max defense" in set_defense.group(0).lower() else "life", "target": set_target, "amount": int(set_defense.group(1))})
    set_all_attack = re.search(r"set the attack of all enemy followers on the field to\s+(\d+)", text, re.I)
    if set_all_attack:
        node["effects"].append({"kind": "set_stat", "stat": "attack", "target": {"scope": "enemy_follower", "selection": "all"}, "amount": int(set_all_attack.group(1))})
    if re.search(r"replicate the effects of this card's fanfare ability|发动与【?入场曲】?相同的能力", text, re.I):
        node["effects"].append({"kind": "replicate_ability", "trigger": "on_fanfare"})
    cost_match = re.search(r"reduce the cost of this card by\s+(\d+)|使本卡牌的费用-(\d+)", text, re.I)
    if cost_match:
        value = int(next(item for item in cost_match.groups() if item))
        node["effects"].append({"kind": "modify_cost", "target": {"scope": "self"}, "amount": -value})
    filtered_cost = re.search(r"reduce the cost of all\s+([a-z]+)\s+(spells?|followers?|amulets?|cards?)\s+in your hand by\s+(\d+)", text, re.I)
    if filtered_cost:
        filters: dict[str, Any] = {"zone": "hand", "tribe": filtered_cost.group(1).lower()}
        card_type = filtered_cost.group(2).lower().rstrip("s")
        if card_type != "card":
            filters["card_type"] = card_type
        node["effects"].append({"kind": "modify_cost", "target": {"scope": "any", "selection": "all", "filters": filters}, "amount": -int(filtered_cost.group(3))})
    selected_cost = re.search(r"select an?\s+(amulet|spell|follower|card)\s+in your hand and reduce its cost by\s+(\d+)", text, re.I)
    if selected_cost:
        filters = {"zone": "hand"}
        if selected_cost.group(1).lower() != "card":
            filters["card_type"] = selected_cost.group(1).lower()
        node["effects"].append({"kind": "modify_cost", "target": {"scope": "any", "selection": "chosen", "count": 1, "filters": filters}, "amount": -int(selected_cost.group(2))})
    set_cost_match = re.search(r"set the cost of this card to\s+(\d+)|使本卡牌的费用变为\s*(\d+)", text, re.I)
    if set_cost_match:
        node["effects"].append({"kind": "set_cost", "target": {"scope": "self"}, "amount": int(next(item for item in set_cost_match.groups() if item))})
    variable_init = re.search(r"\bX\s+starts at\s+(\d+)|X起始为\s*(\d+)", text, re.I)
    if variable_init:
        node["variable_initializers"]["X"] = int(next(item for item in variable_init.groups() if item))
    variable_increase = re.search(r"increase\s+X\s+by\s+(\d+)|本卡牌的X\s*\+\s*(\d+)", text, re.I)
    if variable_increase:
        node["effects"].append({"kind": "modify_counter", "field": "variable_x", "delta": int(next(item for item in variable_increase.groups() if item))})
    shadows_match = re.search(r"gain\s+(\d+)\s+shadows?|墓场\s*\+\s*(\d+)", text, re.I)
    if shadows_match:
        node["effects"].append({"kind": "modify_resource", "resource": "cemetery", "amount": int(next(item for item in shadows_match.groups() if item))})
    attacks_match = re.search(r"can attack\s+(\d+)\s+times per turn|1回合可以攻击\s*(\d+)\s*次", text, re.I)
    if attacks_match:
        node["effects"].append({"kind": "set_attacks", "target": {"scope": "self"}, "amount": int(next(item for item in attacks_match.groups() if item))})
    quoted_status = re.search(r"give\s+(?:this follower|it|them)\s+\"([^\"]+)\"(?:\s+until\s+(.+?))?(?:\.|$)", text, re.I)
    if quoted_status:
        status_target = target or {"scope": "self"}
        node["effects"].append({"kind": "grant_status", "status": quoted_status.group(1).strip(), "target": status_target, "duration": (quoted_status.group(2) or "permanent").strip(" .")})
    reanimate_values = re.findall(r"reanimate\s*\((\d+)\)|亡者召还[_ ]?(\d+)", text, re.I)
    for values in reanimate_values:
        node["effects"].append({"kind": "reanimate", "cost": int(next(item for item in values if item))})
    spellboost_match = re.search(r"spellboost your hand(?:\s+(\d+)\s+times)?|所有手牌发动\s*(\d+)\s*次魔力增幅", text, re.I)
    if spellboost_match:
        count = next((item for item in spellboost_match.groups() if item), "1")
        node["effects"].append({"kind": "spellboost", "target": {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}, "count": int(count)})
    selected_spellboost = re.search(r"select a card in your hand with on spellboost and spellboost it\s+(\d+)\s+times", text, re.I)
    if selected_spellboost:
        node["effects"].append({"kind": "spellboost", "target": {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "hand", "has_trigger": "on_spellboost"}}, "count": int(selected_spellboost.group(1))})
    earth_match = re.search(r"gain\s+(?:an?\s+|(?:(\d+)\s+))?earth sigils?|土之印\s*\+\s*(\d+)", text, re.I)
    if earth_match:
        amount = next((item for item in earth_match.groups() if item), "1")
        node["effects"].append({"kind": "modify_resource", "resource": "earth_sigil", "amount": int(amount)})
    if re.search(r"select a card in your hand and discard it|选择自己的1张手牌，?舍弃", text, re.I):
        node["effects"].append({"kind": "discard", "target": {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "hand"}}})
    if re.search(r"invoke this card|瞬念召唤.*本卡牌", text, re.I):
        node["effects"].append({"kind": "invoke", "target": {"scope": "self"}})
    banish_match = re.search(r"banish\s+(all|a|an)?\s*(random\s+)?(?:copies of\s+[^.]+?\s+from your deck|enemy followers?|enemy follower)|使.*消失", text, re.I)
    if banish_match:
        banish_target = target or {"scope": "any"}
        if "random" in text.lower() and banish_target.get("selection") is None:
            banish_target["selection"] = "random"
        node["effects"].append({"kind": "banish", "target": banish_target})
    crest_delay = re.search(r"delay the counts? of (all your crests|your crest:[^.]+?) by\s*(\d+)|所有纹章的倒计数\+(\d+)|纹章[^』]*』的倒计数\+(\d+)", text, re.I)
    if crest_delay:
        amount = int(next(item for item in crest_delay.groups()[1:] if item and item.isdigit()))
        node["effects"].append({"kind": "modify_crest", "selection": "all" if "all" in (crest_delay.group(1) or "").lower() or "所有纹章" in text else "named", "amount": amount})
    countdown = re.search(r"countdown\s*\((\d+)\)|吟唱[_ ]?(\d+)", text, re.I)
    if countdown:
        node["countdown_initial"] = int(next(item for item in countdown.groups() if item))
        node["effects"].append({"kind": "countdown_config"})
    delayed_card = re.search(r"delay the count of a random allied\s+(.+?)\s+on the field by\s*(\d+)", text, re.I)
    if delayed_card:
        node["effects"].append({"kind": "modify_counter", "field": "countdown", "delta": -int(delayed_card.group(2)), "target": {"scope": "any", "selection": "random", "count": 1, "filters": {"side": "ally", "zone": "field", "card_name": delayed_card.group(1).strip()}}})
    crest_name = re.search(r"destroy your crest\s*:\s*([^.]+)", text, re.I)
    if crest_name:
        node["effects"].append({"kind": "destroy_crest", "target": {"scope": "any", "selection": "chosen", "filters": {"zone": "crests", "card_name": crest_name.group(1).strip()}}})
    filtered_draw = re.search(r"draw an?\s+([a-z]+)\s+follower(?: that costs\s*(\d+)\s+or less)?", text, re.I)
    if filtered_draw:
        filters: dict[str, Any] = {"zone": "deck", "card_type": "follower", "class": filtered_draw.group(1).lower()}
        if filtered_draw.group(2):
            filters["max_cost"] = int(filtered_draw.group(2))
        node["effects"].append({"kind": "draw", "count": 1, "target": {"scope": "any", "filters": filters}})
    filtered_card_draw = re.search(r"draw an?\s+([a-z]+)\s+card", text, re.I)
    if filtered_card_draw:
        node["effects"].append({"kind": "draw", "count": 1, "target": {"scope": "any", "filters": {"zone": "deck", "class": filtered_card_draw.group(1).lower()}}})
    variable_cost_draw = re.search(r"draw an?\s+X-cost\s+(follower|spell|amulet|card)", text, re.I)
    if variable_cost_draw:
        filters = {"zone": "deck", "cost": _variable_source(text) or "var:X"}
        if variable_cost_draw.group(1).lower() != "card":
            filters["card_type"] = variable_cost_draw.group(1).lower()
        node["effects"].append({"kind": "draw", "count": 1, "target": {"scope": "any", "filters": filters}})
    crest_gain = re.search(r"gain crest\s*:\s*([^.]+)", text, re.I)
    if crest_gain:
        node["effects"].append({"kind": "gain_crest", "source_card_name": crest_gain.group(1).strip(), "target": {"scope": "enemy_leader" if re.search(r"give your opponent crest", text, re.I) else "ally_leader"}})
    if re.search(r"evolve all unevolved allied followers", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "ally_follower", "selection": "all", "filters": {"evolved": False}}, "evolution_kind": "normal"})
    named_allied_buff = re.search(r"give all allied copies of\s+(.+?)\s+on the field\s+([+-]\d+)/([+-]\d+)", text, re.I)
    if named_allied_buff:
        node["effects"].append({"kind": "buff", "target": {"scope": "ally_follower", "selection": "all", "filters": {"card_name": named_allied_buff.group(1).strip()}}, "attack": int(named_allied_buff.group(2)), "life": int(named_allied_buff.group(3))})
    if re.search(r"select a card in your hand and transform it into an exact copy of a random card in your opponent's deck", text, re.I):
        node["effects"].append({"kind": "transform", "target": {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "hand"}}, "source": {"side": "enemy", "zone": "deck", "selection": "random", "copy": "exact"}})
    named_transform = re.search(r"(?:select .+? and\s+)?transform (?:it|them|all other followers on the field|all [^.]+? in your hand(?: that cost \d+ or less)?) into (?:an? |copies of )?([^.]+?)(?:\.|$)", text, re.I)
    if named_transform and "random card" not in named_transform.group(1).lower():
        transform_target = target or {"scope": "any"}
        if "all other followers" in text.lower():
            transform_target = {"scope": "any", "selection": "all", "filters": {"zone": "field", "card_type": "follower", "exclude_source": True}}
        node["effects"].append({"kind": "transform", "target": transform_target, "source_card_name": named_transform.group(1).strip()})
    fusion_match = re.search(r"fuse\s*:\s*([a-z]+)\s+cards", text, re.I)
    if fusion_match:
        config: dict[str, Any] = {"filters": {"tribe": fusion_match.group(1).lower()}}
        if re.search(r"total cost of the cards fused", text, re.I):
            config["aggregate"] = "total_cost"
            outcomes = []
            for match in re.finditer(r"(\d+)(\s+or more)?\s*:\s*([^\d]+?)(?=\s+\d+(?:\s+or more)?\s*:|$)", text, re.I):
                outcomes.append({"min": int(match.group(1)), "max": None if match.group(2) else int(match.group(1)), "source_card_name": match.group(3).strip()})
            if outcomes:
                config["outcomes"] = outcomes
        node["effects"].append({"kind": "fusion_config", "config": config})
    generic_fusion = re.search(r"fuse\s*:\s*cards\b", text, re.I)
    if generic_fusion and not fusion_match:
        node["effects"].append({"kind": "fusion_config", "config": {"filters": {}}})
    if re.search(r"return it to (?:the )?deck|使其返回牌组", text, re.I):
        node["effects"].append({"kind": "return_to_deck", "target": {"scope": "any", "selection": "chosen", "count": 1}})
    if re.search(r"return (?:it|an enemy follower|another allied card) to hand", text, re.I):
        node["effects"].append({"kind": "return_to_hand", "target": target or {"scope": "any", "selection": "chosen", "count": 1}})
    counter_match = re.search(r"engage\s*(?:\((\d+)\))?\s*:\s*advance this amulet's count by\s*(\d+|x)", text, re.I)
    if not counter_match:
        counter_match = re.search(r"(?:费用|消耗)\s*(\d+)?\s*【?(?:启动|激活)】?[^。；]*?倒计(?:时|数)\s*[-－](\d+|x)", text, re.I)
    if not counter_match:
        counter_match = re.search(r"【?(?:启动|激活)】?[^。；]*?倒计(?:时|数)\s*[-－](x)", text, re.I)
    if counter_match:
        if len(counter_match.groups()) > 1 and counter_match.group(1) and counter_match.group(2) is not None:
            node["mode_cost"] = int(counter_match.group(1))
        delta = counter_match.group(2) if len(counter_match.groups()) > 1 and counter_match.group(2) is not None else counter_match.group(1)
        node["effects"].append({"kind": "modify_counter", "field": "countdown", "delta": int(delta) if delta.isdigit() else (_variable_source(text) or "var:X")})
    faith_match = re.search(r"reduce your faith(?:'s value)? by\s*(\d+)|信仰值\s*[-－]\s*(\d+)", text, re.I)
    if faith_match:
        node["effects"].append({"kind": "modify_resource", "resource": "faith", "amount": -int(next(value for value in faith_match.groups() if value))})
    multi_add = re.search(r"add\s+(?:an?\s+)?(.+?)\s+and\s+(?:an?\s+)?(.+?)\s+to your hand|将\s*(\d+)?\s*张?『([^』]+)』和\s*(\d+)?\s*张?『([^』]+)』加入手牌", text, re.I)
    if multi_add:
        if multi_add.group(1):
            pairs = ((1, multi_add.group(1)), (1, multi_add.group(2)))
        else:
            pairs = ((int(multi_add.group(3) or 1), multi_add.group(4)), (int(multi_add.group(5) or 1), multi_add.group(6)))
        for count_value, card_name in pairs:
            node["effects"].append({"kind": "add_to_hand", "count": count_value, "source_card_name": card_name.strip()})
    add_match = None if multi_add else re.search(r"add\s+(?:(\d+)\s+copies of\s+|an?\s+)?([^.;\"]+?)\s+to your hand|将\s*(\d+)?\s*张?『([^』]+)』加入手牌", text, re.I)
    if add_match:
        count_value = add_match.group(1) or add_match.group(3) or "1"
        card_name = (add_match.group(2) or add_match.group(4) or "").strip()
        node["effects"].append({"kind": "add_to_hand", "count": int(count_value), "source_card_name": card_name})
    if not node["effects"]:
        node["unparsed"].append(text)
        node["unparsed_clauses"].append(text)
    node["confidence"] = 1.0 if node["effects"] and not node["unparsed"] else 0.0
    return node


def card_to_ast(card: dict[str, Any], primary_language: str = "eng") -> dict[str, Any]:
    raw_clauses = [piece for clause in card.get("clauses", []) for piece in split_mode_clauses(clause)]
    clauses = [clause_to_ast(c) for c in raw_clauses]
    # Compare paired CHS/ENG clauses. A disagreement is retained and lowers
    # confidence instead of silently selecting one language.
    paired: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for clause in clauses:
        paired.setdefault((clause.get("source_key"), clause.get("index"), clause.get("section"), clause.get("mode")), []).append(clause)
    conflicts = []
    abilities = []
    for key, values in paired.items():
        by_language = {value.get("source_language"): value for value in values}
        chs = by_language.get("chs")
        eng = by_language.get("eng")
        audit_conflict = False
        if chs and eng:
            chs_effects = {comparison_signature(item) for item in chs.get("effects", [])}
            eng_effects = {comparison_signature(item) for item in eng.get("effects", [])}
            chs_sig = (chs.get("trigger"), chs.get("mode"), comparison_signature(chs.get("effects")))
            eng_sig = (eng.get("trigger"), eng.get("mode"), comparison_signature(eng.get("effects")))
            if chs_sig == eng_sig and (chs.get("effects") or eng.get("effects")):
                classification, confidence = "matched", 1.0
            elif chs.get("trigger") != eng.get("trigger") or chs.get("mode") != eng.get("mode"):
                nonstatic_triggers = {value for value in (chs.get("trigger"), eng.get("trigger")) if value != "static"}
                classification = "semantic_conflict" if len(nonstatic_triggers) > 1 else "parser_asymmetry"
                confidence = 0.5 if classification == "semantic_conflict" else 0.75
            elif not chs.get("effects") and not eng.get("effects"):
                classification, confidence = "unparsed", 0.0
            elif bool(chs.get("effects")) != bool(eng.get("effects")) or chs_effects <= eng_effects or eng_effects <= chs_effects:
                classification, confidence = "parser_asymmetry", 0.75
            elif _has_real_effect_conflict(chs, eng):
                classification, confidence = "semantic_conflict", 0.5
            else:
                classification, confidence = "parser_asymmetry", 0.75
        else:
            classification, confidence = "missing_translation", 0.5
        audit_conflict = classification == "semantic_conflict"
        # English is the authoritative rules language. Chinese remains in the
        # AST for names, provenance, and translation audits, but cannot make a
        # successfully parsed English ability partial.
        if primary_language == "eng":
            preferred = eng or chs or values[0]
            if eng:
                classification = "matched" if eng.get("effects") else "unparsed"
                confidence = 1.0 if eng.get("effects") else 0.0
            else:
                classification, confidence = "missing_translation", 0.5
        elif primary_language == "chs":
            preferred = chs or eng or values[0]
            if chs:
                classification = "matched" if chs.get("effects") else "unparsed"
                confidence = 1.0 if chs.get("effects") else 0.0
            else:
                classification, confidence = "missing_translation", 0.5
        elif chs and eng and classification == "parser_asymmetry":
            preferred = max((chs, eng), key=lambda item: len(item.get("effects", [])))
        else:
            preferred = eng or chs or values[0]
        ability = {
            "ability_id": f"{card.get('card_id')}:{key[0]}:{key[1]}:{key[2]}:{key[3] or 'normal'}",
            "section": key[2],
            "source_language": primary_language if (primary_language == "eng" and eng) or (primary_language == "chs" and chs) else preferred.get("source_language", ""),
            "source_clause": {"chs": chs.get("source_clause", "") if chs else "", "eng": eng.get("source_clause", "") if eng else ""},
            "trigger": preferred.get("trigger"),
            "mode": preferred.get("mode"),
            "mode_cost": preferred.get("mode_cost"),
            "static_keywords": sorted(set(preferred.get("static_keywords", []))),
            "variable_initializers": preferred.get("variable_initializers", {}),
            "countdown_initial": preferred.get("countdown_initial"),
            "conditions": preferred.get("conditions", []),
            "effects": preferred.get("effects", []),
            "confidence": confidence,
            "classification": classification,
            "unparsed_clauses": list(preferred.get("unparsed_clauses", [])),
        }
        abilities.append(ability)
        if audit_conflict:
            conflicts.append({"source_key": key[0], "index": key[1], "section": key[2], "chs": chs_sig, "eng": eng_sig})
            for value in values:
                value["translation_conflict"] = True
    return {"card_id": card.get("card_id"), "name": card.get("name", {}), "source_hash": card.get("source_hash"), "primary_language": primary_language, "support": "partial" if any(a["classification"] != "matched" for a in abilities) else "generated", "bilingual_conflicts": conflicts, "abilities": abilities, "clauses": clauses}


def json_signature(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["card_to_ast", "clause_to_ast"]
