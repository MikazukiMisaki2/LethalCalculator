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
    """Split inline cost/mode sections while retaining source provenance.

    The normalized source keeps a whole ability sentence in one clause. This
    helper creates deterministic virtual clauses while retaining the original
    source key/index for bilingual pairing and auditability.
    """
    text = clause.get("plain", "")
    # ``When this card is Invoked, ... Fanfare: ...`` contains two different
    # event triggers in one normalized sentence. Split the invocation part
    # before processing Enhance/Skybound markers so the runtime will not fire
    # the Invoke-only effect on an ordinary hand play. ``virtual_index`` is
    # separate from the source index: card_to_ast uses it to keep the two
    # virtual abilities distinct while retaining the original provenance.
    invoke_fanfare = re.search(
        r"(?i)(?:when\s+(?:this\s+)?(?:card|follower)\s+is\s+invoked|被?瞬念召唤)[^.;]*?[.;]?\s*"
        r"fanfare\s*[:：]",
        text,
    )
    if invoke_fanfare:
        boundary = re.search(r"(?i)fanfare\s*[:：]", text[invoke_fanfare.start():])
        if boundary:
            fanfare_start = invoke_fanfare.start() + boundary.start()
            invoke_text = text[:fanfare_start].strip(" \t\r\n.;；。")
            fanfare_text = text[fanfare_start:].strip()
            if invoke_text and fanfare_text:
                invoke_piece = dict(clause)
                invoke_piece["plain"] = invoke_text
                invoke_piece["trigger"] = "on_invoke"
                invoke_piece["mode_override"] = None
                invoke_piece["virtual_index"] = f"{clause.get('index', 0)}:invoke"
                fanfare_piece = dict(clause)
                fanfare_piece["plain"] = fanfare_text
                fanfare_piece["trigger"] = "on_fanfare"
                fanfare_piece["virtual_index"] = f"{clause.get('index', 0)}:fanfare"
                return [invoke_piece, *split_mode_clauses(fanfare_piece)]
    marker = re.compile(
        r"(?P<special>super[- ]?skybound\s+art|skybound\s+art)\s*[-:：]\s*"
        r"|(?P<label>enhance|accelerate|crystallize)\s*\(\s*(?P<cost>\d+)\s*\)\s*:\s*"
        r"|(?P<chs_special>解放奥义|超奥义|奥义)\s*[-:：]\s*"
        r"|(?P<chs>爆能强化|加速|结晶)[_（(]?\s*(?P<chs_cost>\d+)?\s*[）)]?\s*】?\s*[:：]?",
        re.I,
    )
    matches = list(marker.finditer(text))
    if not matches:
        return [clause]
    pieces: list[dict[str, Any]] = []
    first = matches[0]
    prefix = text[:first.start()].strip("【】 ")
    raw_prefix = prefix
    # A trigger label before the first special mode is not an independent
    # ability. Keep it only when it also contains a real base effect.
    if re.fullmatch(
        r"(?:fanfare|evolve|super[- ]?evolve|入场曲|进化时|超进化时|"
        r"select\s+a\s+mode\s+to\s+activate|选择(?:一个)?模式(?:来)?(?:发动|激活))"
        r"\s*[.。:：]?",
        prefix,
        re.I,
    ):
        prefix = ""
    # A common layout puts the mode header before a Super Skybound Art
    # replacement and the numbered choices after it:
    # ``Select a Mode ... Super Skybound Art - Activate all ... 1. ...``.
    # Split that into a normal mode-choice clause plus a short replacement
    # clause; otherwise every numbered choice is incorrectly attached to the
    # Super branch and the header becomes an unparsed standalone sentence.
    first_label = (first.group("special") or first.group("chs_special") or "").lower()
    first_body = text[first.end():].strip("【】 ")
    first_option = re.search(r"(?:^|\s)(?:([1-9]\d*)[.)]|（([1-9]\d*)）)\s*", first_body)
    mode_header = bool(re.search(r"select\s+a\s+mode\s+to\s+activate|选择(?:一个)?模式", raw_prefix, re.I))
    normal_mode_piece: dict[str, Any] | None = None
    if first_label in {
        "skybound art",
        "skybound-art",
        "super skybound art",
        "super-skybound art",
        "奥义",
        "解放奥义",
        "超奥义",
    } and mode_header and first_option:
        normal_mode_piece = dict(clause)
        normal_mode_piece["plain"] = f"{raw_prefix} {first_body[first_option.start():]}".strip()
        normal_mode_piece["mode_override"] = "mode_selection"
        pieces.append(normal_mode_piece)
        prefix = ""
    if prefix:
        item = dict(clause)
        item["plain"] = prefix
        item["mode_override"] = None
        pieces.append(item)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip("【】 ")
        if index == 0 and normal_mode_piece is not None and first_option is not None:
            # Keep only the replacement preamble on the Super branch; the
            # numbered choices are represented by ``normal_mode_piece``.
            body = first_body[:first_option.start()].strip("【】 ")
        label = (match.group("special") or match.group("chs_special") or match.group("label") or match.group("chs") or "").lower()
        mode = {
            "enhance": "enhance", "accelerate": "accelerate", "crystallize": "crystallize",
            "爆能强化": "enhance", "加速": "accelerate", "结晶": "crystallize",
            "skybound art": "skybound_art", "super skybound art": "super_skybound_art",
            "skybound-art": "skybound_art", "super-skybound art": "super_skybound_art",
            "奥义": "skybound_art", "超奥义": "super_skybound_art", "解放奥义": "super_skybound_art",
        }.get(label, label)
        item = dict(clause)
        item["plain"] = body
        item["mode_override"] = mode
        raw_cost = match.group("cost") or match.group("chs_cost")
        item["mode_cost"] = int(raw_cost) if raw_cost else None
        pieces.append(item)
    return pieces


def _target(text: str) -> dict[str, Any] | None:
    value = text.lower()
    # Resolve an explicitly addressed leader before looking at the rest of
    # the sentence.  Trigger clauses often mention both the triggering
    # follower and a follow-up leader effect (for example: "Whenever an
    # enemy follower enters ... deal 1 damage to the enemy leader").  The
    # broad follower-or-leader fallback below must not turn that into a
    # chosen field target.
    if re.search(r"deal\s+(?:\d+|x)\s+damage\s+to\s+(?:the\s+)?enemy\s+leader", text, re.I):
        return {"scope": "enemy_leader"}
    if re.search(r"restore\s+(?:\d+|x)\s+defense\s+to\s+(?:your|the allied)\s+leader", text, re.I):
        return {"scope": "ally_leader"}
    random_enemy_count = re.search(r"(?:deal damage to\s+)?(\d+)\s+random enemy followers?", text, re.I)
    if random_enemy_count:
        return {"scope": "enemy_follower", "selection": "random", "count": int(random_enemy_count.group(1))}
    enemy_count_chs = re.search(
        r"(?:选择|指定)(?:对手|敌方)(?:的)?(?:战场上|场上)?(?:的)?\s*(\d+|一|1)\s*(?:个|张)?\s*随从",
        text,
        re.I,
    )
    if enemy_count_chs:
        token = enemy_count_chs.group(1)
        return {"scope": "enemy_follower", "selection": "chosen", "count": 1 if token in {"一", "1"} else int(token)}
    enemy_count = re.search(r"select\s+(\d+)\s+enemy followers?", text, re.I)
    if enemy_count:
        return {"scope": "enemy_follower", "selection": "chosen", "count": int(enemy_count.group(1))}
    allied_other_count = re.search(r"select\s+(\d+)\s+other allied followers?", text, re.I)
    if allied_other_count:
        return {"scope": "ally_follower", "selection": "chosen", "count": int(allied_other_count.group(1)), "filters": {"exclude_source": True}}
    # Explicit allied field selections must win over the broad enemy/follower
    # fallbacks below.  Keep the selection as a first-class target so a later
    # copy/buff/status operation can refer to the chosen entity.
    allied_field_selected = re.search(
        r"select\s+(?:a|an|\d+)\s+(?:allied|your)\s+(follower|card|amulet|spell)\s+on\s+the\s+field",
        text,
        re.I,
    )
    if allied_field_selected:
        card_type = allied_field_selected.group(1).lower()
        filters: dict[str, Any] = {"zone": "field"}
        if card_type != "card":
            filters["card_type"] = card_type
        return {"scope": "ally_follower" if card_type == "follower" else "any", "selection": "chosen", "count": 1, "filters": filters}
    hand_selected = re.search(
        r"select\s+(?:a|an|\d+)\s+(?:(?P<qualifier>[A-Za-z][\w-]*)\s+)?"
        r"(?P<kind>card|follower|spell|amulet)\s+in your hand",
        text,
        re.I,
    )
    if hand_selected:
        filters: dict[str, Any] = {"zone": "hand"}
        kind = hand_selected.group("kind").lower()
        qualifier = (hand_selected.group("qualifier") or "").lower()
        if kind != "card":
            filters["card_type"] = kind
        # Artifact/Puppetry/etc. are represented as tribes in the catalog.
        # Avoid treating the article or a random-selection adjective as a
        # tribe when a future template adds one.
        if qualifier and qualifier not in {"random", "another", "allied", "your", "enemy"}:
            filters["tribe"] = qualifier
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
    if ("enemy leader" in value and "enemy follower" in value) or ("主战者" in text and "随从" in text and re.search(r"或|or", text, re.I)):
        return {"scope": "any", "selection": "chosen", "count": 1, "filters": {"side": "enemy", "card_type": ["follower", "leader"], "zone": "field"}}
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
    if "random enemy" in value:
        return {"scope": "any", "selection": "random", "count": 1, "filters": {"side": "enemy", "card_type": ["follower", "leader"], "zone": "field"}}
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
    # A plain ``all allied followers`` recipient is common in Mode options
    # such as “give all allied followers +1/+0 and Rush”.  Keep it as the
    # shared target for both the stat change and the trailing keyword; if it
    # falls through to ``self`` the keyword silently applies to the spell
    # source instead of the intended board.
    if re.search(r"all (?:other )?allied followers?", value):
        target = {"scope": "ally_follower", "selection": "all"}
        if "other allied follower" in value:
            target["filters"] = {"exclude_source": True}
        return target
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
        max_attack = re.search(r"with\s+(\d+)\s+attack or less", text, re.I)
        if max_attack:
            result.setdefault("filters", {})["max_attack"] = int(max_attack.group(1))
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
    # Damage inside a quoted Last Words/谢幕曲 body is a deferred ability, not
    # an immediate damage operation.  Parse that status separately in
    # ``clause_to_ast`` and keep it out of this immediate-effect pass.  Other
    # quoted effect text (for example a repeated damage instruction) remains
    # eligible for parsing.
    analysis_text = re.sub(r'(?i)["“]\s*(?:last\s+words|谢幕曲)\s*[:：][^"”\n]*["”]', "", text)
    # Card text commonly chains effects with commas rather than full stops:
    # ``give it ... until ..., deal 1 damage ..., and restore ...``.  Split
    # only when the comma starts another effect verb so card names and quoted
    # text remain intact.  This lets each damage clause get its own target.
    fragments = [
        part.strip(' \"“”')
        for part in re.split(
            r"(?<=[。.!?])\s*|,\s*(?=(?:and\s+)?(?:deal|restore|destroy|summon|give|draw|add|return|banish|evolve|do)\b)",
            analysis_text,
            flags=re.I,
        )
        if part.strip()
    ]
    previous_target: dict[str, Any] | None = None
    for fragment in fragments:
        amount = _amount(fragment)
        if amount == "variable":
            amount = _variable_source(fragment) or whole_source or "var:X"
        target = _target(fragment)
        # Chained English clauses routinely use a pronoun (``select X ...,
        # and deal it/them N damage``).  The comma splitter isolates the
        # second clause, so carry the immediately preceding explicit target
        # forward when the damage fragment has no standalone target phrase.
        if target is None and previous_target is not None and amount is not None:
            target = previous_target
        if target is not None:
            previous_target = target
        if amount is None or target is None:
            continue
        item: dict[str, Any] = {"kind": "damage", "target": target, "amount": amount}
        count = _repeat(fragment)
        if count is not None:
            item = {"kind": "repeat", "count": count, "effects": [item]}
        effects.append(item)
    return effects


# Resource keywords such as ``Necromancy (6) -`` and ``Earth Rite (1) -``
# introduce a conditional suffix, rather than gating the complete sentence.
# For example, ``Deal 6 damage. Necromancy (6) - Deal 2 damage`` means that
# the first six damage is unconditional and only the second clause consumes
# six Shadows.  Keep this grammar close to the AST boundary so every caller
# (including hand-authored fixtures) gets the same semantics.
_RESOURCE_GATE_RE = re.compile(
    r"(?:\b(?P<eng_resource>necromancy|earth\s+rite)\s*\(\s*(?P<eng_amount>\d+)\s*\)"
    r"|(?P<chs_resource>唤灵|土之秘术)\s*(?:[_＿]\s*|[（(]\s*)(?P<chs_amount>\d+)\s*(?:[）)]\s*)?)"
    r"\s*(?:[-–—:：]\s*)",
    re.I,
)


def _resource_gate_split(text: str) -> tuple[str, str, str, int] | None:
    """Return ``(prefix, suffix, resource, amount)`` for a resource suffix.

    Markers inside a quoted Last Words body belong to the deferred ability,
    not to the surrounding clause.  Those spans are skipped here and parsed
    recursively when the nested body is visited.
    """
    quoted_status_spans = [
        match.span()
        for match in re.finditer(
            r'["“「『]\s*(?:last\s+words|谢幕曲)\s*[:：][^"”」』]*["”」』]',
            text,
            re.I,
        )
    ]
    for match in _RESOURCE_GATE_RE.finditer(text):
        if any(start <= match.start() < end for start, end in quoted_status_spans):
            continue
        resource = match.group("eng_resource") or match.group("chs_resource") or ""
        resource = "earth_sigil" if "earth" in resource.casefold() or "土之秘术" in resource else "cemetery"
        amount_raw = match.group("eng_amount") or match.group("chs_amount")
        if not amount_raw:
            continue
        prefix = text[:match.start()].strip(" \t\r\n.;；。")
        if re.fullmatch(
            r"(?:fanfare|evolve|super[- ]?evolve|engage|on\s+play|on\s+evolve|"
            r"on\s+super[- ]?evolve|on\s+engage|入场曲|进化时|超进化时|激奏|激活|启动)"
            r"\s*[:：]?",
            prefix,
            re.I,
        ):
            prefix = ""
        suffix = text[match.end():].strip()
        if suffix:
            return prefix, suffix, resource, int(amount_raw)
    return None


def _resource_gated_clause_to_ast(clause: dict[str, Any], split: tuple[str, str, str, int], depth: int) -> dict[str, Any]:
    """Build an AST with an effect-level resource conditional.

    Parsing the two sides independently avoids trying to infer which already
    extracted effect belongs to the resource branch.  A depth guard keeps
    malformed text from recursing forever while still supporting multiple
    resource markers in one clause.
    """
    prefix, suffix, resource, amount = split
    nested_clause = dict(clause)
    nested_clause["_resource_split_depth"] = depth + 1

    if prefix:
        base_clause = dict(nested_clause)
        base_clause["plain"] = prefix
        base_clause["structure"] = {}
        base_node = clause_to_ast(base_clause)
    else:
        base_node = None

    branch_clause = dict(nested_clause)
    branch_clause["plain"] = suffix
    branch_clause["structure"] = {}
    branch_node = clause_to_ast(branch_clause)

    # If the suffix is not understood, retain the original clause so the
    # normal parser/reporting path can expose it as unparsed instead of
    # manufacturing an empty conditional.
    if not branch_node.get("effects"):
        fallback = dict(branch_node)
        fallback["source_text"] = clause.get("plain", "")
        fallback["source_clause"] = clause.get("plain", "")
        return fallback

    node = dict(base_node or branch_node)
    base_effects = list(base_node.get("effects", ())) if base_node else []
    branch_effects = list(branch_node.get("effects", ()))
    branch_effects.insert(0, {"kind": "consume_resource", "resource": resource, "amount": amount})
    node["effects"] = base_effects + [{
        "kind": "conditional",
        "condition": {"state": resource, "cmp": "gte", "value": amount},
        "effects": branch_effects,
    }]
    # A keyword in the gated suffix (e.g. Necromancy - give this follower
    # Storm) is not a static keyword on the card.  Only the unconditional
    # prefix contributes static metadata.
    node["static_keywords"] = sorted(set(base_node.get("static_keywords", ())) if base_node else set())
    node["conditions"] = list(base_node.get("conditions", ())) if base_node else []
    node["variable_initializers"] = dict(base_node.get("variable_initializers", {})) if base_node else {}
    if base_node and base_node.get("countdown_initial") is not None:
        node["countdown_initial"] = base_node.get("countdown_initial")
    else:
        node.pop("countdown_initial", None)
    node["unparsed"] = list(base_node.get("unparsed", ())) if base_node else []
    node["unparsed"].extend(branch_node.get("unparsed", ()))
    node["unparsed_clauses"] = list(base_node.get("unparsed_clauses", ())) if base_node else []
    node["unparsed_clauses"].extend(branch_node.get("unparsed_clauses", ()))
    node["language"] = clause.get("language", "")
    node["source_language"] = clause.get("language", "")
    node["source_key"] = clause.get("source_key", "")
    node["index"] = clause.get("index", 0)
    if clause.get("virtual_index") is not None:
        node["virtual_index"] = clause.get("virtual_index")
    node["section"] = clause.get("section", "normal")
    node["trigger"] = clause.get("trigger", "static")
    node["mode"] = clause.get("mode_override", _mode(clause.get("plain", "")))
    node["mode_cost"] = clause.get("mode_cost")
    node["source_text"] = clause.get("plain", "")
    node["source_clause"] = clause.get("plain", "")
    node["confidence"] = 1.0 if node["effects"] and not node["unparsed"] and not node["unparsed_clauses"] else 0.0
    return node


def _is_destroy_action(text: str) -> bool:
    """Recognize an instruction to destroy, not a historical/conditional mention."""
    value = text.strip()
    # “Can't be destroyed by abilities” is a protection keyword, not a
    # destroy instruction.  Matching the word ``destroyed`` here used to
    # create a spurious Clash-like destroy effect on Armes.
    if re.search(r"can't\s+be\s+destroyed|cannot\s+be\s+destroyed|不会被能力破坏", value, re.I):
        return False
    patterns = (
        r"\bdestroy\s+(?:this card|it|that card|the selected|the opposing follower|an? |all |\d+ )",
        r"\b(?:select|choose)\b[^.]*\b(?:and\s+)?destroy\b",
        r"(?:破坏本卡牌|将(?:其|它|这些卡牌|该卡牌|所选择的卡牌)破坏|选择[^。]*，?破坏)",
    )
    return any(re.search(pattern, value, re.I) for pattern in patterns)


_SUMMON_TRAILER_RE = re.compile(
    r"(?P<trailer>(?:\s+and\s+|\s*,\s*)(?:"
    r"give\s+(?:it|them|this\s+follower|the\s+exact\s+copy|the\s+exact\s+copies|the\s+copies)\b"
    r"|evolve\s+(?:it|them|this\s+follower)\b"
    r"|super-?evolve\s+(?:it|them|this\s+follower)\b"
    r"|remove\s+last\s+words\s+from\s+it\b"
    r"|return\s+this\s+card\s+to\s+hand\b"
    r"|add\s+[^.]*?\bto\s+(?:your\s+)?hand\b"
    r"|set\s+its\s+cost\b"
    r"|destroy\s+this\s+card\b"
    r"))",
    re.I,
)


def _split_card_names(name_str: str) -> tuple[list[str], str]:
    """Split 'A, B, and C' / 'A and B' into separate card names and separate a
    trailing 'and give it X' / 'and evolve it' style effect suffix so that the
    leading token is a real catalog card name."""
    raw = (name_str or "").strip()
    trailer = ""
    m = _SUMMON_TRAILER_RE.search(raw)
    if m and m.start() > 0:
        trailer = raw[m.start():]
        raw = raw[:m.start()]
    # A comma is part of many official English card names (for example
    # ``Lhynkal, Wandering Fool``). Only treat comma punctuation as a card
    # separator when the text explicitly has a list conjunction; otherwise
    # preserve the whole string for catalog-aware resolution in the compiler.
    # ``&`` is part of a number of official card names (notably
    # ``Zeta & Bea, Crimson and Blue``).  Treating it as a list delimiter
    # makes the resolver invent unrelated cards such as ``Bluerust``.  Only
    # split conjunctions when the source does not contain an ampersand.
    has_name_ampersand = re.search(r"\s&\s", raw) is not None
    if not has_name_ampersand and re.search(r",\s*(?:and\s+|&\s+)", raw, re.I):
        parts = [p.strip(" .,;") for p in re.split(r"\s+(?:and|&)\s+|,\s*(?:and\s+)?", raw) if p.strip(" .,;")]
    elif not has_name_ampersand and re.search(r"\s+(?:and|&)\s+", raw, re.I):
        parts = [p.strip(" .,;") for p in re.split(r"\s+(?:and|&)\s+", raw) if p.strip(" .,;")]
    else:
        parts = [raw.strip(" .,;")] if raw.strip(" .,;") else []
    return parts, trailer.strip()


_CLASS_ALIASES = {
    "forestcraft": "forestcraft",
    "swordcraft": "swordcraft",
    "runcraft": "runcraft",
    "dragoncraft": "dragoncraft",
    "shadowcraft": "shadowcraft",
    "bloodcraft": "bloodcraft",
    "havencraft": "havencraft",
    "portalcraft": "portalcraft",
    "neutral": "neutral",
}


def _source_selector(
    *,
    zone: str,
    side: str | None = None,
    selection: str = "random",
    count: int | str | None = None,
    filters: dict[str, Any] | None = None,
    distinct_by: str | None = None,
) -> dict[str, Any]:
    """Build the language-neutral selector used by dynamic effects.

    A selector is deliberately data-only: the catalog/runtime, rather than the
    text parser, resolves which concrete entity/card satisfies it at execution
    time.  ``distinct_by`` captures phrases such as "differently named".
    """
    result: dict[str, Any] = {"zone": zone, "selection": selection}
    if side:
        result["side"] = side
    if count is not None:
        result["count"] = count
    if filters:
        result["filters"] = filters
    if distinct_by:
        result["distinct_by"] = distinct_by
    return result


def _selector_for_random_deck_summon(text: str) -> dict[str, Any] | None:
    """Parse ``Summon N random <class> <type> ... from your deck``.

    This is intentionally narrower than the generic summon parser.  It only
    fires when the source is explicitly the deck, preventing a card name that
    happens to contain "random" from being interpreted as a selector.
    """
    match = re.search(
        r"\bsummon\s+(?:(?P<count>\d+)|an?)\s+"
        r"(?P<random>random)\s+(?P<distinct>differently\s+named\s+)?"
        r"(?:(?P<side>allied|enemy|your|opponent)\s+)?"
        r"(?:(?P<class>[A-Za-z][\w-]*)\s+)?"
        r"(?P<type>followers?|amulets?|spells?|cards?)"
        r"(?:\s+that\s+costs?\s+(?P<max_cost>\d+)\s+or\s+less)?\s+from\s+your\s+deck",
        text,
        re.I,
    )
    if not match:
        return None
    count = int(match.group("count") or 1)
    card_type = match.group("type").lower().rstrip("s")
    filters: dict[str, Any] = {"card_type": card_type}
    side_name = match.group("side")
    if side_name and side_name.casefold() in {"enemy", "opponent"}:
        filters["side"] = "enemy"
    class_name = match.group("class")
    if class_name and class_name.casefold() not in {"allied", "enemy", "your", "opponent"}:
        class_key = class_name.casefold()
        if class_key in _CLASS_ALIASES:
            filters["class"] = _CLASS_ALIASES[class_key]
        else:
            # Newer descriptors such as Abysscraft are tribes in the catalog,
            # not playable classes.  Keep them as a tribe filter instead of
            # inventing an unknown class namespace.
            filters["tribe"] = class_key
    if match.group("max_cost"):
        filters["max_cost"] = int(match.group("max_cost"))
    return _source_selector(
        zone="deck",
        side="enemy" if side_name and side_name.casefold() in {"enemy", "opponent"} else "ally",
        selection="random",
        count=count,
        filters=filters,
        distinct_by="card_id" if match.group("distinct") else None,
    )


def _selector_for_destroyed_amulet_copy(text: str) -> tuple[int, dict[str, Any]] | None:
    """Parse the historical Last Words amulet-copy summon template."""
    match = re.search(
        r"\bsummon\s+(?:an?\s+)?copy\s+each\s+of\s+(?P<count>\d+)\s+"
        r"random\s+differently\s+named\s+allied\s+amulets?\s+destroyed\s+this\s+match\s+"
        r"with\s+last\s+words\s+and\s+a\s+base\s+cost\s+of\s+(?P<max_cost>\d+)\s+or\s+less",
        text,
        re.I,
    )
    if not match:
        return None
    filters = {
        "side": "ally",
        "card_type": "amulet",
        "has_last_words": True,
        "max_base_cost": int(match.group("max_cost")),
    }
    selector = _source_selector(
        zone="destroyed_this_match",
        selection="random",
        count=int(match.group("count")),
        filters=filters,
        distinct_by="card_id",
    )
    return int(match.group("count")), selector


def _selector_for_historical_copy(text: str) -> tuple[int, dict[str, Any]] | None:
    """Parse a copy of cards destroyed earlier in this match.

    The selector is kept separate from the board target because historical
    cards may no longer have a live entity.  ``highest_base_cost`` is a
    deterministic selection hint; ordinary ``random`` copies retain the
    engine's probability semantics.
    """
    match = re.search(
        r"\bsummon\s+(?:an?\s+)?copy(?:\s+each)?\s+of\s+(?P<count>\d+\s+)?"
        r"(?:an?\s+)?random\s+(?:differently\s+named\s+)?allied\s+(?P<type>amulets?|followers?|cards?)\s+"
        r"destroyed\s+this\s+match(?P<tail>[^.;]*)",
        text,
        re.I,
    )
    if not match:
        return None
    count = int((match.group("count") or "1").strip() or 1)
    card_type = match.group("type").lower().rstrip("s")
    tail = match.group("tail") or ""
    filters: dict[str, Any] = {"side": "ally", "card_type": card_type}
    max_cost = re.search(r"base\s+cost\s+of\s+(\d+)\s+or\s+less", tail, re.I)
    if max_cost:
        filters["max_base_cost"] = int(max_cost.group(1))
    if re.search(r"last\s+words", tail, re.I):
        filters["has_last_words"] = True
    selection = "highest_base_cost" if re.search(r"highest\s+base\s+cost", tail, re.I) else "random"
    selector = _source_selector(
        zone="destroyed_this_match",
        selection=selection,
        count=count,
        filters=filters,
        distinct_by="card_id" if re.search(r"differently\s+named", match.group(0), re.I) else None,
    )
    return count, selector


def _selector_for_historical_hand_copy(
    text: str,
) -> tuple[int, dict[str, Any], str, bool] | None:
    """Parse copies of cards destroyed earlier in this match added to a zone.

    The summon form is handled separately because it creates an entity on the
    field.  This form keeps the source as a historical selector and records the
    destination/copy mode explicitly, e.g. ``Add a copy of a random allied
    Artifact follower destroyed this match to your hand``.
    """
    match = re.search(
        r"\badd\s+(?:(?:an?|the)\s+)?(?P<exact>exact\s+)?copy(?:\s+each)?\s+of\s+"
        r"(?:(?P<count>\d+)\s+)?(?:an?\s+)?random\s+"
        r"(?P<distinct>differently\s+named\s+)?"
        r"(?:(?P<side>allied|enemy|opponent)\s+)?"
        r"(?:(?P<qualifier>[A-Za-z][\w-]*)\s+)?"
        r"(?P<type>followers?|amulets?|spells?|cards?)\s+destroyed\s+this\s+match"
        r"(?P<tail>[^.;]*?)\s+to\s+(?:your\s+)?(?P<destination>hand|deck)",
        text,
        re.I,
    )
    if not match:
        return None
    count = int(match.group("count") or 1)
    card_type = match.group("type").lower().rstrip("s")
    side_name = (match.group("side") or "allied").casefold()
    filters: dict[str, Any] = {
        "side": "enemy" if side_name in {"enemy", "opponent"} else "ally",
        "card_type": card_type,
    }
    qualifier = (match.group("qualifier") or "").casefold()
    if qualifier:
        filters["tribe"] = qualifier
    tail = match.group("tail") or ""
    max_cost = re.search(r"base\s+cost\s+of\s+(\d+)\s+or\s+less", tail, re.I)
    if max_cost:
        filters["max_base_cost"] = int(max_cost.group(1))
    if re.search(r"last\s+words", tail, re.I):
        filters["has_last_words"] = True
    selection = "highest_base_cost" if re.search(r"highest\s+base\s+cost", tail, re.I) else "random"
    selector = _source_selector(
        zone="destroyed_this_match",
        side=filters.pop("side"),
        selection=selection,
        filters=filters,
        distinct_by="card_id" if match.group("distinct") else None,
    )
    return count, selector, match.group("destination").lower(), bool(match.group("exact"))


def _selector_for_random_deck_copy(
    text: str,
) -> tuple[int, dict[str, Any], str, bool] | None:
    """Parse an exact/card copy selected randomly from a player's deck."""
    match = re.search(
        r"\badd\s+(?:(?:an?|the)\s+)?(?P<exact>exact\s+)?copy(?:\s+each)?\s+of\s+"
        r"(?:(?P<count>\d+)\s+)?(?:an?\s+)?random\s+"
        r"(?P<distinct>differently\s+named\s+)?"
        r"(?:(?P<qualifier>[A-Za-z][\w-]*)\s+)?"
        r"(?P<type>followers?|amulets?|spells?|cards?)\s+in\s+"
        r"(?P<owner>your\s+opponent's|the\s+opponent's|your)\s+deck"
        r"\s+to\s+(?:your\s+)?(?P<destination>hand|deck)",
        text,
        re.I,
    )
    if not match:
        return None
    count = int(match.group("count") or 1)
    card_type = match.group("type").lower().rstrip("s")
    owner = match.group("owner").casefold()
    filters: dict[str, Any] = {"card_type": card_type}
    qualifier = (match.group("qualifier") or "").casefold()
    if qualifier:
        if qualifier in _CLASS_ALIASES:
            filters["class"] = _CLASS_ALIASES[qualifier]
        else:
            filters["tribe"] = qualifier
    selector = _source_selector(
        zone="deck",
        side="enemy" if "opponent" in owner else "ally",
        selection="random",
        filters=filters,
        distinct_by="card_id" if match.group("distinct") else None,
    )
    return count, selector, match.group("destination").lower(), bool(match.group("exact"))


def _selector_for_leftmost_hand_copy(text: str) -> tuple[int, dict[str, Any]] | None:
    """Parse ``add an exact copy each of the N leftmost cards in your hand``."""
    match = re.search(
        r"\badd\s+(?:an?\s+)?exact\s+copy(?:\s+each)?\s+of\s+the\s+"
        r"(?P<count>\d+)\s+leftmost\s+cards?\s+in\s+your\s+hand\s+to\s+your\s+hand",
        text,
        re.I,
    )
    if not match:
        return None
    count = int(match.group("count"))
    selector = _source_selector(
        zone="hand",
        side="ally",
        selection="leftmost",
        count=count,
        filters={"card_type": "card"},
    )
    return count, selector


def _selected_hand_selector(text: str) -> tuple[int, dict[str, Any]] | None:
    """Parse ``Select N <tribe> followers in your hand ...`` selectors."""
    match = re.search(
        r"select\s+(?P<count>\d+|an?|a)\s+(?:(?P<qualifier>[A-Za-z][\w-]*)\s+)?"
        r"(?P<type>followers?|amulets?|spells?|cards?)\s+in\s+your\s+hand"
        r"(?:\s+that\s+costs?\s+(?P<max_cost>\d+)\s+or\s+less)?",
        text,
        re.I,
    )
    if not match:
        return None
    count_token = match.group("count")
    count = int(count_token) if count_token.isdigit() else 1
    card_type = match.group("type").lower().rstrip("s")
    filters: dict[str, Any] = {"side": "ally", "card_type": card_type}
    qualifier = (match.group("qualifier") or "").casefold()
    if qualifier and qualifier not in {"allied", "your", "enemy", "random"}:
        filters["tribe"] = qualifier
    if match.group("max_cost"):
        filters["max_cost"] = int(match.group("max_cost"))
    return count, _source_selector(zone="hand", selection="chosen", count=count, filters=filters)


def _copy_source_scope(raw: str) -> dict[str, Any]:
    value = raw.casefold().strip()
    if value in {"this card", "this follower", "this amulet", "本卡牌", "本随从", "本护符"}:
        return {"scope": "self"}
    if value in {"itself", "themselves"}:
        return {"scope": "self"}
    if value in {"it", "them", "the selected card", "the selected follower", "the selected amulet", "其", "它"}:
        return {"scope": "previous_target"}
    return {"scope": "self", "card_name": raw.strip()}


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
    # Card text uses both ``Do this 1 time`` and ``Do it 2 times``.  Treat
    # the singular form as a real repeat as well; otherwise the base action
    # is emitted as a plain effect and a later ``instead`` relation cannot
    # safely change its repeat count.
    m = re.search(r"do\s+(?:this|it)\s+(\d+|x)\s+times?|发动\s*(\d+|x)\s*次", text, re.I)
    if not m:
        return None
    value = next(x for x in m.groups() if x is not None)
    return int(value) if value.isdigit() else "variable"


def _mode(text: str) -> str | None:
    value = text.lower()
    if re.search(r"select\s+(?:\d+|a|an?)\s+modes?\b", value) or "select a mode" in value or re.search(
        r"(?:^|[【\s])模式[】\s]*(?:选择|发动)|(?:选择|发动)\s*(?:\d+|一个|1个)?\s*(?:模式|能力)",
        text,
        re.I,
    ):
        return "mode_selection"
    if "super skybound art" in value or "解放奥义" in text:
        return "super_skybound_art"
    if "skybound art" in value or "奥义" in text:
        return "skybound_art"
    # Match the keyword as a standalone mode marker.  A substring check
    # incorrectly classified cards such as ``Enhanced Puppet`` as an
    # Enhance mode, which then broke bilingual pairing and produced a bogus
    # Chinese ``unparsed_clauses`` entry even though the Fanfare was parsed.
    if re.search(r"\benhance(?:ment)?\b", value) or "爆能强化" in text:
        return "enhance"
    if re.search(r"\baccelerate\b", value) or "加速" in text:
        return "accelerate"
    if re.search(r"\bcrystallize\b", value) or "结晶" in text:
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
    if re.search(r"this follower is super-?evolved|本随从已超进化", text, re.I):
        result.append({"state": "super_evolved", "cmp": "eq", "value": True})
    card_cost = re.search(
        r"this\s+card's\s+cost\s+(isn't|is\s+not|is\s+different\s+from)\s*(\d+)"
        r"|(?:this\s+card's|its|the\s+card's)\s+cost\s+(?:is|equals?)\s*(\d+)"
        r"|本卡牌的费用(?:不是|不为|为|是)\s*(\d+)",
        text,
        re.I,
    )
    if card_cost:
        # The first alternative is the legacy ``isn't`` predicate.  The
        # following alternatives model exact cost checks used by discard
        # chains such as ``If its cost is 7 ... If its cost is 5 ...``.
        if card_cost.group(1):
            result.append({"state": "card_cost", "cmp": "ne", "value": int(card_cost.group(2))})
        elif card_cost.group(3):
            result.append({"state": "card_cost", "cmp": "eq", "value": int(card_cost.group(3))})
        else:
            raw = card_cost.group(0)
            cmp_name = "ne" if re.search(r"不是|不为", raw) else "eq"
            result.append({"state": "card_cost", "cmp": cmp_name, "value": int(card_cost.group(4))})
    allied_amulets = re.search(r"(?:at least|至少)\s*(\d+)\s+allied amulets?|有\s*(\d+)\s*张?护符", text, re.I)
    if allied_amulets:
        result.append({"state": "ally_amulet_count", "cmp": "gte", "value": int(next(item for item in allied_amulets.groups() if item))})
    artifact_count = re.search(r"(?:at least|至少)\s*(\d+)\s+differently named allied artifact followers? have entered the field this match", text, re.I)
    if artifact_count:
        result.append({"state": "ally_artifact_count", "cmp": "gte", "value": int(artifact_count.group(1))})
    if re.search(r"allied follower(?:s)? attacked a leader on your last turn|自己的随从在上个回合攻击过主战者", text, re.I):
        result.append({"state": "attacked_with_follower_last_turn", "cmp": "eq", "value": True})
    selected_type = re.search(r"you selected (?:a|an)\s+(spell|follower|amulet|card)|选择(?:了)?(?:一张|1张)?\s*(法术|随从|护符|卡牌)", text, re.I)
    if selected_type:
        raw_type = next(item for item in selected_type.groups() if item)
        type_map = {"法术": "spell", "随从": "follower", "护符": "amulet", "卡牌": "card"}
        result.append({"state": "selected_card_type", "cmp": "eq", "value": type_map.get(raw_type, raw_type.lower())})
    present = re.search(r"there(?:'s| is) an? allied\s+(.+?)\s+on the field|场上有(?:一个|1个)?\s*(.+?)的己方卡牌", text, re.I)
    if present:
        card_name = next(item for item in present.groups() if item)
        result.append({"state": "card_present", "cmp": "eq", "value": card_name.strip(" .")})
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


def _normalize_last_words_effects(effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make entity references in a nested Last Words AST owner-relative.

    A nested body is parsed as a standalone clause, where ``this card`` would
    naturally resolve to ``self``.  Once the body is attached to a status, the
    status owner is the deferred trigger source.  Rewriting only copy sources
    keeps ordinary targets (for example ``enemy leader``) unchanged while
    preserving exact-copy state semantics.
    """
    normalized: list[dict[str, Any]] = []
    for raw in effects:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if item.get("kind") == "copy" and isinstance(item.get("source"), dict):
            source = dict(item["source"])
            if source.get("scope") in {"self", "previous_target", "trigger_source"}:
                source["scope"] = "trigger_source"
            item["source"] = source
        for nested_field in ("effects", "else_effects"):
            children = item.get(nested_field)
            if isinstance(children, list):
                item[nested_field] = _normalize_last_words_effects(children)
        if isinstance(item.get("ability"), dict):
            ability = dict(item["ability"])
            if isinstance(ability.get("effects"), list):
                ability["effects"] = _normalize_last_words_effects(ability["effects"])
            item["ability"] = ability
        normalized.append(item)
    return normalized


def clause_to_ast(clause: dict[str, Any]) -> dict[str, Any]:
    text = clause.get("plain", "")
    resource_split_depth = int(clause.get("_resource_split_depth", 0) or 0)
    # The Earth Rite listener is itself the resource-gated clause; splitting
    # it first would discard the event prefix and make it look partially
    # parsed.  Let the explicit listener template below handle the whole
    # sentence atomically.
    is_golem_listener_text = re.fullmatch(
        r"whenever\s+an?\s+allied\s+golem\s+follower\s+enters?\s+the\s+field\s*,?\s*"
        r"earth\s+rite\s*\(\s*1\s*\)\s*[-–—:]\s*evolve\s+it\.?",
        text.strip(),
        re.I,
    )
    if resource_split_depth < 8 and not is_golem_listener_text:
        resource_split = _resource_gate_split(text)
        if resource_split is not None:
            return _resource_gated_clause_to_ast(clause, resource_split, resource_split_depth)
    structure = clause.get("structure", {})
    node: dict[str, Any] = {
        "language": clause.get("language", ""),
        "source_language": clause.get("language", ""),
        "source_key": clause.get("source_key", ""),
        "index": clause.get("index", 0),
        **({"virtual_index": clause.get("virtual_index")} if clause.get("virtual_index") is not None else {}),
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
    # Global listener clauses can restrict the event entity (for example,
    # “Whenever an allied Golem follower enters the field”).  Keep the
    # filter alongside the trigger instead of mis-binding “it” to the
    # listener source itself.
    golem_summon_listener = re.fullmatch(
        r"whenever\s+an?\s+allied\s+golem\s+follower\s+enters?\s+the\s+field\s*,?\s*"
        r"earth\s+rite\s*\(\s*1\s*\)\s*[-–—:]\s*evolve\s+it\.?",
        text.strip(),
        re.I,
    )
    if golem_summon_listener:
        node["trigger"] = "on_ally_follower_summon"
        node["trigger_filter"] = {"card_type": "follower", "tribe": "golem"}
        node["effects"] = [{
            "kind": "conditional",
            "condition": {"state": "earth_sigil", "cmp": "gte", "value": 1},
            "effects": [
                {"kind": "consume_resource", "resource": "earth_sigil", "amount": 1},
                {"kind": "auto_evolve", "target": {"scope": "trigger_source"}, "evolution_kind": "normal"},
            ],
        }]
        node["confidence"] = 1.0
        return node
    engage_cost = re.search(r"engage\s*\((\d+)\)|费用\s*(\d+)\s*【启动】", text, re.I)
    if engage_cost:
        node["mode_cost"] = int(next(item for item in engage_cost.groups() if item))
    countdown_header = re.search(r"countdown\s*\((\d+)\)|吟唱[_ ]?(\d+)", text, re.I)
    if countdown_header:
        node["countdown_initial"] = int(next(item for item in countdown_header.groups() if item))
    evolved_restore_match = re.search(
        r"at\s+the\s+end\s+of\s+your\s+turn\s*,?\s*draw\s+a\s+card\s+and\s*,?\s*"
        r"if\s+there(?:'s|\s+is)\s+an?\s+evolved\s+allied\s+follower\s+on\s+the\s+field\s*,?\s*"
        r"restore\s+1\s+defense\s+to\s+your\s+leader\s*\.\s*"
        r"if\s+there(?:'s|\s+is)\s+an?\s+super-?evolved\s+allied\s+follower\s+on\s+the\s+field\s*,?\s*"
        r"restore\s+2\s+defense\s+instead\.?",
        text,
        re.I,
    )
    target = _target(text)
    # A small family of discard cards has a mutually exclusive cost chain:
    # when the discarded card is cost 7 it creates a cost-5 copy, otherwise
    # when it is cost 5 it creates a cost-3 copy.  Parse the chain as a nested
    # conditional so changing the first copy's cost cannot accidentally make
    # the second branch fire in the same event.
    discard_cost_chain = re.fullmatch(
        r"when\s+this\s+card\s+is\s+discarded\s*,\s*"
        r"if\s+(?:its|this\s+card's)\s+cost\s+is\s+(?P<first_cost>\d+)\s*,\s*"
        r"add\s+(?:an?\s+)?(?P<first_name>.+?)\s+to\s+your\s+hand\s+"
        r"and\s+set\s+its\s+cost\s+to\s+(?P<first_new_cost>\d+)\s*\.\s*"
        r"if\s+(?:its|this\s+card's)\s+cost\s+is\s+(?P<second_cost>\d+)\s*,\s*"
        r"add\s+(?:an?\s+)?(?P<second_name>.+?)\s+to\s+your\s+hand\s+"
        r"and\s+set\s+its\s+cost\s+to\s+(?P<second_new_cost>\d+)\s*\.?",
        text.strip(),
        re.I,
    )
    if discard_cost_chain:
        def _cost_branch(cost: str, card_name: str, new_cost: str) -> dict[str, Any]:
            return {
                "kind": "conditional",
                "condition": {"state": "card_cost", "cmp": "eq", "value": int(cost)},
                "effects": [
                    {"kind": "add_to_hand", "count": 1, "source_card_name": card_name.strip(" .")},
                    # ``set its cost`` refers to the card just created in
                    # hand, not to the discarded source card.
                    {"kind": "set_cost", "target": {"scope": "previous_add"}, "amount": int(new_cost)},
                ],
            }
        first_branch = _cost_branch(
            discard_cost_chain.group("first_cost"),
            discard_cost_chain.group("first_name"),
            discard_cost_chain.group("first_new_cost"),
        )
        second_branch = _cost_branch(
            discard_cost_chain.group("second_cost"),
            discard_cost_chain.group("second_name"),
            discard_cost_chain.group("second_new_cost"),
        )
        first_branch["else_effects"] = [second_branch]
        # The predicates belong to the nested branches, not to the outer
        # on-discard ability; otherwise a cost-5 card would be blocked by the
        # first cost-7 check before it can reach the else branch.
        node["conditions"] = []
        node["effects"].append(first_branch)
        node["confidence"] = 1.0
        return node
    # ``remove all abilities`` is a distinct operation, not a keyword grant.
    # Keep the selected target (including its count/filter) so a subsequent
    # damage effect applies to exactly the same entities.
    remove_abilities_text = re.sub(
        r'(?i)["“「『]\s*(?:last\s+words|谢幕曲)\s*[:：][^"”」』]*["”」』]',
        "",
        text,
    )
    remove_abilities_match = re.search(
        r"\bremove\s+all\s+(?:of\s+)?(?:its|their|the\s+)?\s*abilities\b"
        r"|\bremove\s+all\s+abilities\s+from\b"
        r"|失去所有能力",
        remove_abilities_text,
        re.I,
    )
    if remove_abilities_match:
        node["effects"].append({"kind": "remove_abilities", "target": target or {"scope": "any"}})
    # Explicit keyword removal is distinct from removing all abilities.  Keep
    # the previous-target relation for sentences such as “select a follower
    # and remove Ward from it”; the caller supplies that selected UID when the
    # rule is executed.
    remove_keyword_patterns = (
        ("storm", r"(?:remove|lose|strip)\s+(?:the\s+)?storm\s+from|失去疾驰|移除疾驰"),
        ("rush", r"(?:remove|lose|strip)\s+(?:the\s+)?rush\s+from|失去突进|移除突进"),
        ("ward", r"(?:remove|lose|strip)\s+(?:the\s+)?ward\s+from|失去守护|移除守护"),
        ("bane", r"(?:remove|lose|strip)\s+(?:the\s+)?bane\s+from|失去必杀|移除必杀"),
        ("drain", r"(?:remove|lose|strip)\s+(?:the\s+)?drain\s+from|失去虹吸|移除虹吸"),
        ("ambush", r"(?:remove|lose|strip)\s+(?:the\s+)?ambush\s+from|失去潜行|移除潜行"),
    )
    removed_keywords: set[str] = set()
    for keyword, pattern in remove_keyword_patterns:
        if not re.search(pattern, text, re.I):
            continue
        removed_keywords.add(keyword)
        if re.search(r"\bfrom\s+(?:it|them|that follower|the selected follower)\b|使其|将其", text, re.I):
            remove_target = {"scope": "previous_target", "selection": "chosen"}
        elif re.search(r"\bfrom\s+(?:this|the)\s+(?:follower|amulet|card)\b|本(?:随从|护符|卡牌)", text, re.I):
            remove_target = {"scope": "self"}
        else:
            remove_target = target or {"scope": "self"}
        node["effects"].append({"kind": "remove_keyword", "keyword": keyword, "target": remove_target})
    # Deferred keyword abilities are kept as status nodes rather than being
    # mistaken for immediate effects.  The prefix before the quote contains
    # the actual recipient (for example, all allied Puppetry followers).
    last_words_match = re.search(
        r"(?P<prefix>\bgive\s+.+?)(?:\"|“)[\s]*last\s+words\s*:\s*(?P<body>[^\"”]+)(?:\"|”)" ,
        text,
        re.I,
    )
    last_words_target = None
    if last_words_match:
        # For ``Select ... and give it Last Words ...`` resolve the selected
        # entity from the full prefix before the quoted body.  Looking only at
        # ``give it`` would otherwise fall back to ``self``.
        before_quote = text[:last_words_match.start()].strip()
        last_words_target = _target(before_quote) or _target(last_words_match.group("prefix"))
    if last_words_match:
        last_words_body = last_words_match.group("body").strip()
        status_effect: dict[str, Any] = {
            "kind": "grant_status",
            "status": f"Last Words: {last_words_body}",
            "target": last_words_target or {"scope": "self"},
        }
        # Parse the quoted body with the same conservative grammar as a
        # normal clause, but attach it to an explicit deferred trigger.  This
        # handles simple nested effects (exact-copy, damage, draw, summon,
        # and their combinations) without treating them as immediate effects
        # of the granting card.  Any genuinely unsupported remainder remains
        # visible as ``unsupported_nested`` below.
        nested = clause_to_ast({
            **clause,
            "plain": last_words_body,
            "trigger": "on_last_word",
            "mode_override": None,
        })
        nested_effects = _normalize_last_words_effects(nested.get("effects", []))
        nested_unparsed = list(nested.get("unparsed", [])) + list(nested.get("unparsed_clauses", []))
        if nested_effects:
            status_effect["ability"] = {"trigger": "on_last_word", "effects": nested_effects}
        node["effects"].append(status_effect)
        if not nested_effects or nested_unparsed:
            # Preserve the body exactly enough for a report while avoiding a
            # duplicate marker for a fully parsed nested ability.
            node["unparsed_clauses"].append(f"unsupported_nested:Last Words: {last_words_body}")
    # Bodies inside a quoted Last Words/谢幕曲 status are deferred until the
    # host follower dies.  Remove those quoted bodies from immediate-effect
    # matching so ``Summon a copy of this card`` is not emitted at play time.
    immediate_text = re.sub(
        r'(?i)["“「『]\s*(?:last\s+words|谢幕曲)\s*[:：][^"”」』]*["”」』]',
        "",
        text,
    )
    # Some cards put an Enhance/Super-Evolve replacement in a separate
    # sentence. Preserve the relation explicitly instead of pretending it is
    # an independent effect or discarding its dependence on the base clause.
    instead_patterns = (
        (r"select\s+(\d+)\s+instead", "selection_count"),
        (r"add\s+(\d+)(?:\s+copies)?\s+instead", "count"),
        (r"summon\s+(\d+)(?:\s+copies)?\s+instead", "count"),
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
        # Keep the number selected as part of the effect.  Previously the
        # parser emitted only the options, so the engine could not tell
        # “Select 2 Modes” apart from the ordinary one-choice form and would
        # either execute one option or flatten all options together.
        selection_match = re.search(r"select\s+(\d+)\s+modes?\b|选择\s*(\d+)\s*个?模式", text, re.I)
        selection_count = None
        if selection_match:
            raw_count = next((item for item in selection_match.groups() if item), None)
            if raw_count:
                selection_count = int(raw_count)
        mode_effect: dict[str, Any] = {"kind": "mode_choice", "choices": choices}
        if selection_count is not None and selection_count > 0:
            mode_effect["selection_count"] = selection_count
            node["mode_selection_count"] = selection_count
        node["effects"].append(mode_effect)
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
            repeat_instead = re.search(r"do\s+(?:this|it)\s+(\d+|x)\s+times?(?:\s+instead)?\s*$", branch_text, re.I)
            if repeat_instead and base_effects:
                repeat_value = next(item for item in repeat_instead.groups() if item)
                repeat_value = int(repeat_value) if repeat_value.isdigit() else "var:X"
                # ``base_effects`` may already be wrapped as ``repeat 1``;
                # unwrap that neutral wrapper before constructing the branch
                # so the requested count is represented exactly once.
                base_action = base_effects[0]
                if base_action.get("kind") == "repeat" and base_action.get("effects"):
                    base_action = base_action["effects"][0]
                branch_effects = [{"kind": "repeat", "count": repeat_value, "effects": [base_action]}]
            if not branch_effects:
                # Rally/Combo replacements such as “Add 2 copies instead”
                # carry the card identity in the base sentence.  Reparse the
                # base sentence once and change only the matching add/summon
                # count; the replacement must not invent a card name.
                count_instead = re.search(r"(?:add|summon)\s+(\d+)(?:\s+copies)?(?:\s+instead)?\s*$", branch_text, re.I)
                if count_instead:
                    base_node = clause_to_ast({**clause, "plain": base_text})
                    if not base_effects:
                        base_effects = list(base_node.get("effects", []))
                    base_effect = next((item for item in base_node.get("effects", []) if item.get("kind") in ("add_to_hand", "summon")), None)
                    if base_effect:
                        replacement = dict(base_effect)
                        replacement["count"] = int(count_instead.group(1))
                        branch_effects = [replacement]
            if not branch_effects and base_effects:
                base_effect = next((item for item in base_effects if item.get("kind") in ("damage", "heal", "repeat")), None)
                amount_instead = re.search(r"(?:deal|restore)\s+(\d+)\s+(?:damage|defense)", branch_text, re.I)
                target_count_instead = re.search(r"deal\s+damage\s+to\s+(\d+)\s+random enemy followers?", branch_text, re.I)
                if base_effect and amount_instead:
                    if base_effect.get("kind") == "repeat" and base_effect.get("effects"):
                        base_effect = base_effect["effects"][0]
                    replacement = dict(base_effect)
                    replacement["amount"] = int(amount_instead.group(1))
                    branch_effects = [replacement]
                elif base_effect and target_count_instead:
                    replacement = dict(base_effect)
                    replacement["target"] = dict(replacement.get("target", {}))
                    replacement["target"]["count"] = int(target_count_instead.group(1))
                    replacement["target"]["selection"] = "random"
                    branch_effects = [replacement]
            if not branch_effects and base_effects and re.search(r"deal damage to all enemy followers|对对手的战场上的所有随从造成", branch_text, re.I):
                amount = base_effects[0].get("amount", 0)
                branch_effects = [{"kind": "damage", "target": {"scope": "enemy_follower", "selection": "all"}, "amount": amount}]
            if branch_effects and base_effects:
                # The condition is owned by the explicit conditional node;
                # leaving it on the outer ability would incorrectly suppress
                # the base branch when the threshold is not met.  The
                # ``instead`` relation emitted before this block is likewise
                # redundant once both branches are materialized.
                node["conditions"] = [
                    condition for condition in node["conditions"]
                    if not (condition.get("state") == ("play_count" if re.search(r"combo|连击", alternative.group(0), re.I) else "rally") and condition.get("value") == int(alternative.group(1)))
                ]
                state_name = "play_count" if re.search(r"combo|连击", alternative.group(0), re.I) else "rally"
                condition = {"state": state_name, "cmp": "gte", "value": int(alternative.group(1))}
                affected_index = next((
                    index for index, item in enumerate(base_effects)
                    if item.get("kind") in ("damage", "heal", "repeat", "add_to_hand", "summon")
                ), 0)
                conditional = {"kind": "conditional", "condition": condition, "effects": branch_effects, "else_effects": [base_effects[affected_index]]}
                node["effects"] = list(base_effects)
                node["effects"][affected_index] = conditional
                node["_dedupe_top_level_effects"] = True
        else:
            # Generic “If ..., <replacement> instead” clauses.  Only
            # materialize them when the condition extractor understands the
            # predicate; unknown predicates stay partial rather than being
            # applied unconditionally by the compiler.
            generic_if = re.search(r"\s+if\s+(.+),\s*(.+?)\s+instead\.?$", text, re.I)
            if generic_if:
                base_text = text[:generic_if.start()].strip()
                branch_text = generic_if.group(2).strip()
                parsed_conditions = _conditions(generic_if.group(1))
                base_node = clause_to_ast({**clause, "plain": base_text}) if parsed_conditions else {}
                branch_node = clause_to_ast({**clause, "plain": branch_text}) if parsed_conditions else {}
                base_effects = base_node.get("effects", []) if isinstance(base_node, dict) else []
                branch_effects = branch_node.get("effects", []) if isinstance(branch_node, dict) else []
                if parsed_conditions and base_effects and not branch_effects:
                    amount_match = re.search(r"(?:deal\s+|restore\s+)(\d+)\s+(?:damage|defense)", branch_text, re.I)
                    repeat_match = re.search(r"do\s+(?:this|it)\s+(\d+|x)\s+times?", branch_text, re.I)
                    count_match = re.search(r"(?:add|summon)\s+(\d+)(?:\s+copies)?", branch_text, re.I)
                    if amount_match:
                        amount = int(amount_match.group(1))
                        base_effect = next((item for item in base_effects if item.get("kind") in ("damage", "heal")), None)
                        if base_effect:
                            replacement = dict(base_effect)
                            replacement["amount"] = amount
                            branch_effects = [replacement]
                    elif repeat_match:
                        repeat_value = repeat_match.group(1)
                        repeat_value = int(repeat_value) if repeat_value.isdigit() else "var:X"
                        base_effect = next((item for item in base_effects if item.get("kind") in ("damage", "heal", "repeat")), None)
                        if base_effect:
                            if base_effect.get("kind") == "repeat" and base_effect.get("effects"):
                                base_effect = base_effect["effects"][0]
                            branch_effects = [{"kind": "repeat", "count": repeat_value, "effects": [dict(base_effect)]}]
                    elif count_match:
                        base_effect = next((item for item in base_effects if item.get("kind") in ("add_to_hand", "summon")), None)
                        if base_effect:
                            replacement = dict(base_effect)
                            replacement["count"] = int(count_match.group(1))
                            branch_effects = [replacement]
                if parsed_conditions and base_effects and branch_effects:
                    node["conditions"] = []
                    condition = parsed_conditions[0] if len(parsed_conditions) == 1 else {"all": parsed_conditions}
                    affected_index = next((
                        index for index, item in enumerate(base_effects)
                        if item.get("kind") in ("damage", "heal", "repeat")
                    ), 0)
                    affected_effect = base_effects[affected_index]
                    conditional = {"kind": "conditional", "condition": condition, "effects": branch_effects, "else_effects": [affected_effect]}
                    # Effects before/after the replacement (for example the
                    # discard in “discard a card, then restore X”) remain
                    # unconditional and keep their original order.
                    if condition.get("state") == "selected_card_type" and any(item.get("kind") == "discard" for item in base_effects):
                        node["effects"] = [item for item in base_effects if item is not affected_effect] + [conditional]
                    else:
                        node["effects"] = list(base_effects)
                        node["effects"][affected_index] = conditional
                    node["_dedupe_top_level_effects"] = True
                else:
                    node["effects"].extend(_damage_effects(text))
            else:
                node["effects"].extend(_damage_effects(text))
    for condition in node["conditions"]:
        if condition.get("state") in ("cemetery", "earth_sigil"):
            node["effects"].insert(0, {"kind": "consume_resource", "resource": condition["state"], "amount": condition.get("value", 0)})
    if re.search(r"(?<!super-)evolve this follower|使本随从进化|本随从进化[。！]", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "self"}, "evolution_kind": "normal"})
    if re.search(r"super-?evolve this follower|超进化本随从", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "self"}, "evolution_kind": "super"})
    if re.search(r"super-?evolve them instead", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "ally_follower", "selection": "all", "filters": {"evolved": False}}, "evolution_kind": "super"})
    if re.search(r"evolve it|使其进化", text, re.I):
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "trigger_source"}, "evolution_kind": "normal"})
    # Dynamic selectors/copies must be recognized before the generic summon or
    # hand-add patterns below.  They intentionally remain data selectors: the
    # catalog/runtime resolves the concrete cards/entities at execution time.
    dynamic_summon = False
    dynamic_hand_copy = False
    destroyed_amulet_copy = _selector_for_destroyed_amulet_copy(immediate_text)
    if destroyed_amulet_copy:
        copy_count, selector = destroyed_amulet_copy
        selector.pop("count", None)
        node["effects"].append({
            "kind": "summon",
            "count": copy_count,
            "resource_selector": selector,
            "copy_mode": "exact",
            "preserve_state": True,
        })
        dynamic_summon = True
    random_deck_selector = _selector_for_random_deck_summon(immediate_text)
    if random_deck_selector:
        summon_count = int(random_deck_selector.pop("count", 1))
        node["effects"].append({
            "kind": "summon",
            "count": summon_count,
            "resource_selector": random_deck_selector,
        })
        dynamic_summon = True
    historical_copy = _selector_for_historical_copy(immediate_text)
    if historical_copy and not dynamic_summon:
        copy_count, selector = historical_copy
        selector.pop("count", None)
        node["effects"].append({
            "kind": "summon",
            "count": copy_count,
            "resource_selector": selector,
            "copy_mode": "exact",
            "preserve_state": True,
        })
        dynamic_summon = True
    enemy_named_summon = re.search(
        r"\bsummon\s+(?P<count>\d+|an?)\s+enemy\s+"
        r"(?:exact\s+)?copies?\s+of\s+(?P<name>[^.;\"]+)",
        immediate_text,
        re.I,
    )
    if enemy_named_summon:
        raw_count = enemy_named_summon.group("count")
        node["effects"].append({
            "kind": "summon",
            "count": int(raw_count) if raw_count.isdigit() else 1,
            "source_card_name": enemy_named_summon.group("name").strip(),
            "target": {"scope": "enemy_follower"},
        })
        dynamic_summon = True
    historical_hand_copy = _selector_for_historical_hand_copy(immediate_text)
    if historical_hand_copy:
        copy_count, selector, destination, is_exact = historical_hand_copy
        node["effects"].append({
            "kind": "copy",
            "source": selector,
            "destination": destination,
            "count": copy_count,
            "copy_mode": "exact" if is_exact else "card",
            "preserve_state": bool(is_exact),
            "reveal": False if re.search(r"without\s+revealing", immediate_text, re.I) else True,
        })
        dynamic_hand_copy = True
    random_deck_copy = _selector_for_random_deck_copy(immediate_text)
    if random_deck_copy:
        copy_count, selector, destination, is_exact = random_deck_copy
        node["effects"].append({
            "kind": "copy",
            "source": selector,
            "destination": destination,
            "count": copy_count,
            "copy_mode": "exact" if is_exact else "card",
            "preserve_state": bool(is_exact),
            "reveal": False if re.search(r"without\s+revealing", immediate_text, re.I) else True,
        })
        dynamic_hand_copy = True
    leftmost_hand_copy = _selector_for_leftmost_hand_copy(immediate_text)
    if leftmost_hand_copy:
        copy_count, selector = leftmost_hand_copy
        node["effects"].append({
            "kind": "copy",
            "source": selector,
            "destination": "hand",
            "count": copy_count,
            "copy_mode": "exact",
            "preserve_state": True,
            "reveal": False if re.search(r"without\s+revealing", immediate_text, re.I) else True,
        })
        dynamic_hand_copy = True
    selected_hand = _selected_hand_selector(immediate_text)
    selected_hand_copy = re.search(
        r"select\s+(?:\d+|an?|a)\s+[A-Za-z][\w-]*\s+(?:followers?|amulets?|spells?|cards?)\s+"
        r"in\s+your\s+hand(?:\s+that\s+costs?\s+\d+\s+or\s+less)?\s*,?\s*(?:and\s+)?"
        r"summon\s+(?:an?\s+)?exact\s+copy\s+of\s+(?:each|it|them)",
        immediate_text,
        re.I,
    )
    if selected_hand and selected_hand_copy and not dynamic_summon:
        copy_count, selector = selected_hand
        node["effects"].append({
            "kind": "copy",
            "source": selector,
            "destination": "field",
            "count": copy_count,
            "copy_mode": "exact",
            "preserve_state": True,
        })
        dynamic_summon = True
    selected_enemy_copy = re.search(
        r"select\s+(?:an?|\d+)\s+enemy\s+follower\s+on\s+the\s+field"
        r"(?:\s+with\s+(?P<limit>\d+)\s+(?P<stat>defense|attack)\s+or\s+less)?\s*,?\s*"
        r"(?:banish|destroy)\s+it\s*(?:,\s*)?(?:and\s+)?summon\s+(?:an?\s+)?exact\s+copy\s+of\s+it",
        immediate_text,
        re.I,
    )
    if selected_enemy_copy and not dynamic_summon:
        filters: dict[str, Any] = {"side": "enemy", "card_type": "follower"}
        if selected_enemy_copy.group("limit"):
            filters["max_life" if selected_enemy_copy.group("stat").lower() == "defense" else "max_attack"] = int(selected_enemy_copy.group("limit"))
        node["effects"].append({
            "kind": "copy",
            "source": _source_selector(zone="field", selection="chosen", filters=filters),
            "destination": "field",
            "count": 1,
            "copy_mode": "exact",
            "preserve_state": True,
        })
        dynamic_summon = True
    exact_copy_status = re.search(
        r"give\s+(?:the\s+)?exact\s+cop(?:y|ies)\s+\"(?P<body>[^\"]+)\"",
        immediate_text,
        re.I,
    )
    if exact_copy_status and any(item.get("kind") == "copy" for item in node["effects"]):
        status_body = exact_copy_status.group("body").strip()
        status_effect: dict[str, Any] = {
            "kind": "grant_status",
            "status": status_body,
            "target": {"scope": "previous_copy"},
        }
        end_turn_destroy = re.fullmatch(
            r"at\s+the\s+end\s+of\s+your\s+opponent's\s+turn\s*,?\s*destroy\s+this\s+card\.??",
            status_body,
            re.I,
        )
        if end_turn_destroy:
            status_effect["ability"] = {
                "trigger": "on_opponent_turn_end",
                "effects": [{"kind": "destroy", "target": {"scope": "trigger_source"}}],
            }
        node["effects"].append(status_effect)
        # Keep a remainder marker only when the quoted body did not map to an
        # executable nested ability.  The end-of-opponent-turn destroy body
        # above is fully represented and must not downgrade an otherwise
        # complete copy effect to ``partial``.
        if "ability" not in status_effect:
            node["unparsed_clauses"].append(f"unsupported_nested:status:{status_body}")
    if dynamic_summon and re.search(r"super-?evolve\s+(?:it|them)", immediate_text, re.I):
        node["effects"] = [
            item for item in node["effects"]
            if not (item.get("kind") == "auto_evolve" and isinstance(item.get("target"), dict) and item["target"].get("scope") == "trigger_source")
        ]
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "previous_summon"}, "evolution_kind": "super"})
    elif dynamic_summon and re.search(r"(?<!super-)evolve\s+(?:it|them)", immediate_text, re.I):
        node["effects"] = [
            item for item in node["effects"]
            if not (item.get("kind") == "auto_evolve" and isinstance(item.get("target"), dict) and item["target"].get("scope") == "trigger_source")
        ]
        node["effects"].append({"kind": "auto_evolve", "target": {"scope": "previous_summon"}, "evolution_kind": "normal"})
    # ``Summon (an) exact copy/copy of this card/it`` refers to an entity, not
    # a static catalog card.  Preserve its current attack/life, damage,
    # keywords, and evolution state when the runtime materializes the copy.
    exact_copy = re.search(
        r"\bsummon\s+(?:(?P<count>\d+)\s+)?(?:an?\s+)?(?:exact\s+)?cop(?:y|ies)\s+of\s+"
        r"(?P<source>this\s+card|this\s+follower|this\s+amulet|itself|themselves|it|them|the\s+selected\s+(?:card|follower|amulet))\b",
        immediate_text,
        re.I,
    )
    if exact_copy and not dynamic_summon:
        copy_source = _copy_source_scope(exact_copy.group("source"))
        if copy_source.get("scope") == "previous_target" and re.search(r"this\s+card's\s+cost|本卡牌的费用", immediate_text, re.I):
            copy_source = {"scope": "self"}
        if copy_source.get("scope") == "previous_target" and re.search(r"this\s+(?:card|follower)\s+enters?\s+the\s+field|进入战场", immediate_text, re.I):
            copy_source = {"scope": "trigger_source"}
        node["effects"].append({
            "kind": "copy",
            "source": copy_source,
            "destination": "field",
            "count": int(exact_copy.group("count") or 1),
            "copy_mode": "exact",
            "preserve_state": True,
        })
        dynamic_summon = True
    copy_buff = re.search(
        r"give\s+(?:the\s+)?exact\s+cop(?:y|ies)\s+([+-](?:\d+|x))/([+-](?:\d+|x))",
        immediate_text,
        re.I,
    )
    if copy_buff and any(item.get("kind") == "copy" for item in node["effects"]):
        attack = int(copy_buff.group(1)) if copy_buff.group(1).lstrip("+-").isdigit() else "var:X"
        life = int(copy_buff.group(2)) if copy_buff.group(2).lstrip("+-").isdigit() else "var:X"
        node["effects"].append({"kind": "buff", "target": {"scope": "previous_copy"}, "attack": attack, "life": life})
    # Copy a selected board follower into hand and apply a modifier to that
    # newly-created card.  The source selector carries the base-cost filter;
    # no concrete card id is guessed from the prose.
    selected_copy_to_hand = re.search(
        r"select\s+(?:an?|\d+)\s+(?:allied\s+)?follower\s+on\s+the\s+field"
        r"(?:\s+with\s+a\s+base\s+cost\s+of\s+(?P<min_cost>\d+)\s+or\s+more)?\s*,?\s*"
        r"add\s+(?:an?\s+)?copy\s+of\s+it\s+to\s+your\s+hand"
        r"(?:\s+without\s+revealing\s+it)?[\s,]*(?:and\s+reduce\s+the\s+cost\s+of\s+the\s+copy\s+by\s+(?P<delta>\d+))?",
        immediate_text,
        re.I,
    )
    if selected_copy_to_hand:
        filters: dict[str, Any] = {"side": "ally", "card_type": "follower"}
        if selected_copy_to_hand.group("min_cost"):
            filters["min_base_cost"] = int(selected_copy_to_hand.group("min_cost"))
        node["effects"].append({
            "kind": "copy",
            "source": _source_selector(zone="field", selection="chosen", filters=filters),
            "destination": "hand",
            "count": 1,
            "copy_mode": "card",
            "preserve_state": False,
            "reveal": False,
            **({"cost_delta": -int(selected_copy_to_hand.group("delta"))} if selected_copy_to_hand.group("delta") else {}),
        })
        dynamic_hand_copy = True
    selected_destroy_copy = re.search(
        r"select\s+(?:an?|\d+)\s+enemy\s+follower\s+on\s+the\s+field\s*,?\s*"
        r"destroy\s+it\s*(?:,\s*)?(?:and\s+)?add\s+(?:an?\s+)?copy\s+of\s+it\s+to\s+your\s+hand",
        immediate_text,
        re.I,
    )
    if selected_destroy_copy:
        node["effects"].append({
            "kind": "copy",
            "source": _source_selector(zone="field", side="enemy", selection="chosen", filters={"card_type": "follower"}),
            "destination": "hand",
            "count": 1,
            "copy_mode": "card",
            "preserve_state": False,
        })
        dynamic_hand_copy = True
    selected_enemy_card_copy = re.search(
        r"select\s+an?\s+enemy\s+card\s+on\s+the\s+field\s*,?\s*"
        r"(?P<remove>banish|destroy)\s+it\s*(?:,\s*)?(?:and\s+)?"
        r"add\s+(?:an?\s+)?copy\s+of\s+it\s+to\s+your\s+hand",
        immediate_text,
        re.I,
    )
    if selected_enemy_card_copy:
        node["effects"].append({
            "kind": "copy",
            "source": _source_selector(
                zone="field",
                side="enemy",
                selection="chosen",
                filters={"card_type": "field_card"},
            ),
            "destination": "hand",
            "count": 1,
            "copy_mode": "card",
            "preserve_state": False,
        })
        dynamic_hand_copy = True
    # Exact copy of a random card in the opponent's hand, followed by an
    # ordinary draw.  Keep the two effects in source order.
    opponent_hand_copy = re.search(
        r"add\s+an?\s+exact\s+copy\s+of\s+a\s+random\s+card\s+in\s+your\s+opponent's\s+hand"
        r"\s+to\s+your\s+hand\s+without\s+revealing\s+it(?:\s+and\s+reduce\s+its\s+cost\s+by\s+(?P<delta>\d+))?",
        immediate_text,
        re.I,
    )
    if opponent_hand_copy:
        node["effects"].append({
            "kind": "copy",
            "source": _source_selector(zone="hand", side="enemy", selection="random", filters={"card_type": "card"}),
            "destination": "hand",
            "count": 1,
            "copy_mode": "exact",
            "preserve_state": True,
            "reveal": False,
            **({"cost_delta": -int(opponent_hand_copy.group("delta"))} if opponent_hand_copy.group("delta") else {}),
        })
        dynamic_hand_copy = True
    draw_match = re.search(r"draw\s+(a|an|\d+|x)\s+(cards?|spells?|followers?|amulets?)|draw\s+(a|an)\s+(\d+)\s*-\s*cost\s+(spell|follower|amulet|card)|抽[取]?(\d+|X)张?(法术|随从|护符|卡牌)", immediate_text, re.I)
    if draw_match:
        groups = draw_match.groups()
        # The first form is “draw a follower”; the second is “draw a
        # 3-cost spell”; the final pair is the Chinese equivalent.
        value = groups[0] or groups[2] or groups[5] or "1"
        card_kind = groups[1] or groups[4] or groups[6] or ""
        draw_node = {"kind": "draw", "count": int(value) if value.isdigit() else (_variable_source(text) or (1 if value.lower() in ("a", "an") else "var:X"))}
        if re.search(r"spell|法术", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "spell"}}
        elif re.search(r"follower|随从", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "follower"}}
        elif re.search(r"amulet|护符", card_kind, re.I):
            draw_node["target"] = {"scope": "any", "filters": {"zone": "deck", "card_type": "amulet"}}
        if groups[3]:
            draw_node.setdefault("target", {"scope": "any", "filters": {"zone": "deck"}})
            draw_node["target"].setdefault("filters", {})["max_cost"] = int(groups[3])
        node["effects"].append(draw_node)
    summon_match = None if dynamic_summon else re.search(r"summon\s+(?:(\d+)\s+copies of\s+|an?\s+)?([^.;\"]+)|召唤\s*(\d+)?\s*(?:个|张)?『([^』]+)』", immediate_text, re.I)
    if summon_match:
        count_value = summon_match.group(1) or summon_match.group(3) or "1"
        raw_names = (summon_match.group(2) or summon_match.group(4) or "").strip()
        names, trailer = _split_card_names(raw_names)
        # ``Summon 2 instead`` is a replacement relation, not a summon of a
        # card literally named “2 instead”.  Leave it to the instead parser.
        if not (len(names) == 1 and re.fullmatch(r"\d+(?:\s+copies)?(?:\s+instead)?", names[0], re.I)):
            summon_count = int(count_value)
            for name in names:
                if name:
                    node["effects"].append({"kind": "summon", "count": summon_count, "source_card_name": name})
            if trailer:
                child = clause_to_ast({**clause, "plain": trailer})
                node["effects"].extend(child.get("effects", []))
    action_text = re.sub(r'["“「『][^"”」』]*["”」』]', "", immediate_text)
    if _is_destroy_action(action_text):
        destroy_target = {"scope": "self"} if re.search(r"destroy\s+this\s+card|破坏本卡牌", action_text, re.I) else (target or {"scope": "any"})
        node["effects"].append({"kind": "destroy", "target": destroy_target})
    if re.search(r"\bstorm\b|【疾驰】", text, re.I) and "storm" not in removed_keywords:
        storm_target = last_words_target or target or {"scope": "self"}
        node["effects"].append({"kind": "grant_keyword", "keyword": "storm", "target": storm_target})
    if re.search(r"\brush\b|【突进】", text, re.I) and "rush" not in removed_keywords:
        rush_target = last_words_target or target or {"scope": "self"}
        node["effects"].append({"kind": "grant_keyword", "keyword": "rush", "target": rush_target})
    if re.search(r"\bward\b|【守护】", text, re.I) and "ward" not in removed_keywords:
        # A trailing keyword in “give all allied followers ... and Ward”
        # shares the preceding recipient.  Do not reuse a leader target from
        # an unrelated damage/heal sentence; bare Ward still means self.
        candidate = target if isinstance(target, dict) and target.get("scope") not in ("enemy_leader", "ally_leader") else None
        ward_target = last_words_target or candidate or {"scope": "self"}
        node["effects"].append({"kind": "grant_keyword", "keyword": "ward", "target": ward_target})
    if re.search(r"activate all of them instead|改为发动所有能力", text, re.I):
        node["effects"].append({"kind": "activate_all_mode_choices"})
    for keyword, pattern in (
        ("bane", r"\bbane\b|必杀|毁灭"),
        ("drain", r"\bdrain\b|虹吸|吸血"),
        ("ambush", r"\bambush\b|潜行|突袭"),
        ("aura", r"\baura\b"),
        ("barrier", r"\bbarrier\b"),
    ):
        if re.search(pattern, text, re.I) and keyword not in removed_keywords:
            if not any(effect.get("kind") == "grant_keyword" and effect.get("keyword") == keyword for effect in node["effects"]):
                node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
            if re.fullmatch(r"(?:ambush|bane|drain|aura|barrier|必杀|毁灭|虹吸|吸血|潜行|突袭)(?:\s+(?:ambush|bane|drain|aura|barrier|必杀|毁灭|虹吸|吸血|潜行|突袭))*", text.strip(" ."), re.I):
                node["static_keywords"].append(keyword)
    if re.search(r"can't be destroyed by abilities|cannot be destroyed by abilities|不会被能力破坏", text, re.I):
        node["effects"].append({"kind": "grant_keyword", "keyword": "effect_indestructible", "target": {"scope": "self"}})
    for keyword, pattern in (("aura", r"aura|灵气"), ("earth_sigil", r"earth sigil|土之印"), ("unplayable", r"can(?:not|'t) be played|无法使用")):
        if re.fullmatch(pattern, text.strip("【】 。."), re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
            node["static_keywords"].append(keyword)
    for keyword, pattern in (("ambush", r"ambush|潜行"), ("bane", r"bane|必杀|毁灭"), ("drain", r"drain|虹吸"), ("intimidate", r"intimidate|威慑")):
        if keyword in removed_keywords:
            continue
        if re.fullmatch(pattern, text.strip("【】 "), re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": {"scope": "self"}})
        elif re.search(rf"\bgive\s+(?:it|them|this follower)\s+({pattern})\b", text, re.I):
            node["effects"].append({"kind": "grant_keyword", "keyword": keyword, "target": target or {"scope": "self"}})
    for keyword, pattern in (("ward", r"ward|守护"), ("storm", r"storm|疾驰"), ("rush", r"rush|突进")):
        if re.match(rf"(?:【)?(?:{pattern})(?:】)?(?:\s|$)", text, re.I) and len(text.strip("【】 ")) > 3:
            node["static_keywords"].append(keyword)
    # Field-wide wording without an allegiance qualifier ("all followers on
    # the field") affects both sides.  Keep it separate from the allied/enemy
    # shorthand below so the resulting ``any`` target remains explicit.
    all_field_buff = re.search(r"give\s+all\s+followers?\s+on\s+the\s+field\s+([+-](?:\d+|x))/([+-](?:\d+|x))", text, re.I)
    if all_field_buff:
        attack = int(all_field_buff.group(1)) if all_field_buff.group(1).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        life = int(all_field_buff.group(2)) if all_field_buff.group(2).lstrip("+-").isdigit() else (_variable_source(text) or "var:X")
        node["effects"].append({
            "kind": "buff",
            "target": {"scope": "any", "selection": "all", "filters": {"zone": "field", "card_type": "follower"}},
            "attack": attack,
            "life": life,
        })
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
    # Stop a temporary-status duration at the next chained effect.  The
    # previous non-greedy expression still consumed the whole remainder of a
    # comma-separated trigger, turning ``until ... turn, deal 1 damage`` into
    # one giant duration string and hiding the damage clause.
    quoted_status = re.search(
        r"give\s+(?:this follower|it|them)\s+\"([^\"]+)\""
        r"(?:\s+until\s+(.+?))?(?=,\s*(?:and\s+)?(?:deal|restore|destroy|summon|give|draw|add|return|banish|evolve|do)\b|[.;]|$)",
        text,
        re.I,
    )
    damage_taken_modifier = re.search(
        r"(?:give|grant)\s+(?:the\s+)?enemy\s+leader\s+"
        r"[\"“]\s*takes\s+(?P<amount>\d+)\s+more\s+damage\s*\.?\s*[\"”]",
        text,
        re.I,
    )
    if damage_taken_modifier:
        node["effects"].append({
            "kind": "modify_damage_taken",
            "target": {"scope": "enemy_leader"},
            "amount": int(damage_taken_modifier.group("amount")),
            "duration": "permanent",
        })
    faith_mode_ability = re.search(
        r"give\s+(?:it|your\s+faith|the\s+faith)\s+\"\s*increase\s+the\s+number\s+of\s+modes?\s+you\s+can\s+select\s+by\s+(\d+)\s*\.??\s*\"",
        text,
        re.I,
    )
    if faith_mode_ability:
        node["effects"].append({
            "kind": "grant_resource_ability",
            "resource": "faith",
            "ability": {
                "trigger": "on_mode_selected",
                # This is a persistent Mode-capacity bonus, not a Faith
                # value increment.  Keep the field explicit so selecting a
                # Mode updates the correct per-Faith-instance counter.
                "effects": [{"kind": "modify_resource", "resource": "faith", "field": "mode_limit", "amount": int(faith_mode_ability.group(1))}],
            },
        })
    if quoted_status and not last_words_match and not faith_mode_ability:
        # In a summon trigger, ``give it`` refers to the follower that caused
        # the trigger, not to an arbitrary enemy selected from the sentence.
        status_prefix = text[:quoted_status.start()]
        # A quoted status is granted to the entity named immediately before
        # the quote.  Reusing the broad target extracted from the full clause
        # would incorrectly attach Yidmetra's listener to every allied
        # follower because the quoted body itself mentions that board.
        if re.search(r"\b(?:give|grant)\s+(?:this\s+follower|it|them)\s+", status_prefix, re.I):
            status_target = {"scope": "trigger_source"}
        else:
            status_target = {"scope": "trigger_source"} if re.search(r"(?:whenever|when)\s+an?\s+enemy\s+follower\s+enters?\s+the\s+field", text, re.I) else (target or {"scope": "self"})
        duration = (quoted_status.group(2) or "permanent").strip(" .")
        if duration.casefold() in {"the end of your opponent's turn", "the end of your opponent’s turn"}:
            duration = "until_end_of_opponent_turn"
        status_text = quoted_status.group(1).strip()
        status_effect: dict[str, Any] = {"kind": "grant_status", "status": status_text, "target": status_target, "duration": duration}
        # The most common persistent quoted ability is a listener rather than
        # a keyword: “Whenever you play an Enhanced card, give all allied
        # followers ...”.  Parse its body once and attach an executable
        # ability so the runtime can dispatch it on the next card play.
        enhanced_listener = re.match(r"whenever\s+you\s+play\s+an?\s+enhanced\s+card\s*,?\s*(.+)", status_text, re.I)
        delayed_destroy = re.fullmatch(
            r"at\s+the\s+end\s+of\s+your\s+opponent's\s+turn\s*,?\s*destroy\s+this\s+card\.?",
            status_text,
            re.I,
        )
        if enhanced_listener:
            body = clause_to_ast({**clause, "plain": enhanced_listener.group(1).strip(), "trigger": "on_card_play", "mode_override": None})
            nested_effects = list(body.get("effects", []))
            if nested_effects and not body.get("unparsed") and not body.get("unparsed_clauses"):
                status_effect["ability"] = {
                    "trigger": "on_card_play",
                    "condition": {"state": "last_played_mode", "cmp": "eq", "value": "enhance"},
                    "effects": nested_effects,
                }
            else:
                node["unparsed_clauses"].append(f"unsupported_nested:status:{status_text}")
        elif delayed_destroy:
            status_effect["ability"] = {
                "trigger": "on_opponent_turn_end",
                "effects": [{"kind": "destroy", "target": {"scope": "trigger_source"}}],
            }
        elif re.fullmatch(r"can\s+attack\s+\d+\s+times\s+per\s+turn\.?", status_text, re.I):
            # This is represented by set_attacks below; retaining a textual
            # gain_status node would only create a duplicate unsupported
            # marker for the same executable capability.
            status_effect = None
        if status_effect is not None:
            node["effects"].append(status_effect)
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
    if re.search(r"\bdiscard\s+your\s+hand\b|舍弃自己的手牌", text, re.I):
        node["effects"].append({"kind": "discard", "target": {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}})
    if re.search(r"invoke this card|瞬念召唤.*本卡牌", text, re.I):
        node["effects"].append({"kind": "invoke", "target": {"scope": "self"}})
    # Explicit field-wide banish choices.  These are deliberately more
    # specific than the generic ``enemy follower`` pattern because Bahamut's
    # mode text affects *all* cards of a type on the field, not one chosen
    # enemy target.  Crests are a resource rather than a board card, so use
    # the canonical destroy_crest operation for that choice.
    specific_banish = False
    if re.search(r"banish\s+all\s+other\s+followers?\s+from\s+the\s+field|使战场上的其他所有随从消失", text, re.I):
        node["effects"].append({
            "kind": "banish",
            "target": {
                "scope": "any",
                "selection": "all",
                "filters": {"zone": "field", "card_type": "follower", "exclude_source": True},
            },
        })
        specific_banish = True
    if re.search(r"banish\s+all\s+amulets?\s+from\s+the\s+field|使战场上的所有护符消失", text, re.I):
        node["effects"].append({
            "kind": "banish",
            "target": {"scope": "any", "selection": "all", "filters": {"zone": "field", "card_type": "amulet"}},
        })
        specific_banish = True
    if re.search(r"banish\s+all\s+crests?|使所有纹章消失", text, re.I):
        node["effects"].append({
            "kind": "destroy_crest",
            "target": {"scope": "any", "selection": "all", "filters": {"zone": "crests"}},
        })
        specific_banish = True
    banish_match = None if specific_banish else re.search(r"banish\s+(all|a|an)?\s*(random\s+)?(?:copies of\s+[^.]+?\s+from your deck|enemy followers?|enemy follower)|使.*消失", text, re.I)
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
    crest_gain = re.search(r"gain crest\s*:\s*(.+?)(?=\s+and\s+(?:return|give|destroy)\b|\s+fanfare\s*:|[。；;\"」]|$)", text, re.I)
    if crest_gain:
        crest_name = crest_gain.group(1).strip().rstrip(".")
        node["effects"].append({"kind": "gain_crest", "source_card_name": crest_name, "target": {"scope": "enemy_leader" if re.search(r"give your opponent crest", text, re.I) else "ally_leader"}})
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
    if re.search(r"return (?:it|this card|an enemy follower|another allied card) to hand", text, re.I):
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
            for name in _split_card_names(card_name)[0]:
                if name:
                    node["effects"].append({"kind": "add_to_hand", "count": count_value, "source_card_name": name})
    add_match = None if (multi_add or dynamic_hand_copy) else re.search(r"add\s+(?:(\d+)\s+copies of\s+|an?\s+)?([^.;\"]+?)\s+to your hand|将\s*(\d+)?\s*张?『([^』]+)』加入手牌", immediate_text, re.I)
    if add_match:
        count_value = add_match.group(1) or add_match.group(3) or "1"
        raw_names = (add_match.group(2) or add_match.group(4) or "").strip()
        names, trailer = _split_card_names(raw_names)
        for name in names:
            if name:
                node["effects"].append({"kind": "add_to_hand", "count": int(count_value), "source_card_name": name})
        if trailer:
            child = clause_to_ast({**clause, "plain": trailer})
            node["effects"].extend(child.get("effects", []))
    # --- enhancement: patterns for previously-unparsed clauses ---
    # Select an enemy follower/card on the field (with N defense or less) and banish it
    banish_select = re.search(r"select\s+an?\s+enemy\s+(follower|card)\s+on\s+the\s+field(?:\s+with\s+(?P<limit>\d+)\s+(?P<stat>defense|attack) or less)?\s*(?:,\s*)?(?:and\s+)?banish\s+it\b", immediate_text, re.I)
    if banish_select:
        selected_type = banish_select.group(1).lower()
        banish_scope = {
            "scope": "enemy_follower" if selected_type == "follower" else "any",
            "selection": "chosen",
            "count": 1,
            "filters": {
                "side": "enemy",
                "zone": "field",
                "card_type": "follower" if selected_type == "follower" else "field_card",
            },
        }
        if banish_select.group("limit"):
            banish_scope["filters"]["max_life" if banish_select.group("stat").lower() == "defense" else "max_attack"] = int(banish_select.group("limit"))
        node["effects"].append({"kind": "banish", "target": banish_scope})
    banish_plural = re.search(r"select\s+(\d+)\s+enemy\s+followers?\s+on\s+the\s+field\s+and\s+banish\s+them\b", text, re.I)
    if banish_plural:
        node["effects"].append({"kind": "banish", "target": {"scope": "enemy_follower", "selection": "chosen", "count": int(banish_plural.group(1))}})
    if re.search(r"banish\s+all\s+duplicates\s+from\s+your\s+deck", text, re.I):
        node["effects"].append({"kind": "banish", "target": {"scope": "any", "selection": "all", "filters": {"zone": "deck", "duplicates_only": True}}})
    if re.search(r"banish\s+all\s+enemy\s+copies\s+of\s+it\s+from\s+the\s+field", text, re.I):
        node["effects"].append({"kind": "banish", "target": {"scope": "any", "selection": "all", "filters": {"zone": "field", "side": "enemy", "copies_of": {"ref": "previous_target"}}}})
    # Give your opponent Crest: X
    crest_opponent = re.search(r"give\s+(?:your\s+)?opponent\s+crest\s*:\s*([^。；;\"」]+)", text, re.I)
    if crest_opponent:
        node["effects"].append({"kind": "gain_crest", "source_card_name": crest_opponent.group(1).strip().rstrip("."), "target": {"scope": "enemy_leader"}})
    crest_both = re.search(r"give\s+yourself\s+and\s+your\s+opponent\s+crest\s*:\s*([^。；;\"」]+)", text, re.I)
    if crest_both:
        crest_name = crest_both.group(1).strip().rstrip(".")
        node["effects"].append({"kind": "gain_crest", "source_card_name": crest_name, "target": {"scope": "ally_leader"}})
        node["effects"].append({"kind": "gain_crest", "source_card_name": crest_name, "target": {"scope": "enemy_leader"}})
    # Advance the count of your Crest: X by N
    crest_advance = re.search(r"advance\s+the\s+count\s+of\s+your\s+crest\s*:\s*([^。；;\"」]+?)\s+by\s+(\d+)", text, re.I)
    if crest_advance:
        node["effects"].append({"kind": "modify_crest", "source_card_name": crest_advance.group(1).strip(), "amount": int(crest_advance.group(2))})
    # Draw N differently named M-cost spells/cards
    draw_diff = re.search(r"draw\s+(\d+)\s+differently\s+named\s+(\d+)-cost\s+(spells?|cards?)", text, re.I)
    if draw_diff:
        diff_filters = {"zone": "deck", "max_cost": int(draw_diff.group(2)), "distinct_names": True}
        if draw_diff.group(3).lower().startswith("spell"):
            diff_filters["card_type"] = "spell"
        node["effects"].append({"kind": "draw", "count": int(draw_diff.group(1)), "target": {"scope": "any", "filters": diff_filters}})
    # When you draw this card, set its cost to N until the end of the turn
    draw_set_cost = re.search(r"when\s+you\s+draw\s+this\s+card\s*,\s*set\s+its\s+cost\s+to\s+(\d+)\s+until\s+the\s+end\s+of\s+the\s+turn", text, re.I)
    if draw_set_cost:
        node["effects"].append({"kind": "set_cost", "target": {"scope": "self"}, "amount": int(draw_set_cost.group(1)), "duration": "until_end_of_turn", "trigger": "on_draw"})
    # When this card is discarded, give a random allied follower on the field +A/+D
    discard_buff = re.search(r"when\s+this\s+card\s+is\s+discarded\s*,\s*give\s+a\s+random\s+allied\s+follower\s+on\s+the\s+field\s+([+-]\d+)/([+-]\d+)", text, re.I)
    if discard_buff:
        node["effects"].append({"kind": "buff", "target": {"scope": "ally_follower", "selection": "random", "count": 1}, "attack": int(discard_buff.group(1)), "life": int(discard_buff.group(2)), "trigger": "on_discard"})
    # Pronoun buffs: give them +A/+D (refers to an earlier selected target)
    give_them_buff = re.search(r"give\s+them\s+([+-](?:\d+|x))/([+-](?:\d+|x))", text, re.I)
    if give_them_buff:
        node["effects"].append({"kind": "buff", "target": target or {"scope": "any", "selection": "chosen", "count": 1, "filters": {"zone": "field", "card_type": "follower"}}, "attack": give_them_buff.group(1), "life": give_them_buff.group(2)})
    # Destroy X random enemy followers. X is ...
    destroy_var = re.search(r"destroy\s+x\s+random\s+enemy\s+followers?\s*\.\s*x\s+is\s+(.+)", text, re.I)
    if destroy_var:
        node["effects"].append({"kind": "destroy", "target": {"scope": "enemy_follower", "selection": "random", "count": "var:X"}, "count_source": destroy_var.group(1).strip()})
    # Halve the cost of all cards in your deck
    if re.search(r"halve\s+the\s+cost\s+of\s+all\s+cards\s+in\s+your\s+deck", text, re.I):
        node["effects"].append({"kind": "modify_cost", "target": {"scope": "any", "selection": "all", "filters": {"zone": "deck"}}, "operation": "halve"})
    # Replace your deck with X
    replace_deck = re.search(r"replace\s+your\s+deck\s+with\s+(.+)", text, re.I)
    if replace_deck:
        node["effects"].append({"kind": "replace_deck", "replacement": replace_deck.group(1).strip().rstrip(" .。")})
    # Transform all allied followers on the field into exact copies of random followers in your deck
    if re.search(r"transform\s+all\s+allied\s+followers\s+on\s+the\s+field\s+into\s+exact\s+copies\s+of\s+random\s+followers?\s+in\s+your\s+deck", text, re.I):
        node["effects"].append({"kind": "transform", "target": {"scope": "ally_follower", "selection": "all", "filters": {"zone": "field"}}, "source": {"zone": "deck", "selection": "random", "copy": "exact"}})
    # Variable power buff: give it +X/+Y (X/Y is ...)
    variable_buff = re.search(r"give\s+it\s+([+-])x/([+-])y", text, re.I)
    if variable_buff:
        var_target = target or {"scope": "any", "selection": "chosen", "count": 1}
        var_item = {"kind": "buff", "target": var_target, "attack": "var:X", "life": "var:Y"}
        if variable_buff.group(1) == "-":
            var_item["attack"] = "-var:X"
        if variable_buff.group(2) == "-":
            var_item["life"] = "-var:Y"
        node["effects"].append(var_item)
    # Add N copies of X to your deck
    add_deck = re.search(r"add\s+(?:(\d+)\s+copies of\s+|an?\s+)?([^.;\"]+?)\s+to your deck", text, re.I)
    if add_deck and not dynamic_hand_copy:
        add_deck_names, add_deck_trailer = _split_card_names(add_deck.group(2) or "")
        for name in add_deck_names:
            if name:
                node["effects"].append({"kind": "add_to_hand", "count": int(add_deck.group(1) or 1), "source_card_name": name, "target_zone": "deck"})
        if add_deck_trailer:
            child = clause_to_ast({**clause, "plain": add_deck_trailer})
            node["effects"].extend(child.get("effects", []))
    # Reduce the cost of all followers/cards in your deck by N
    deck_cost = re.search(r"reduce the cost of all\s+([a-z]+)\s+(spells?|followers?|amulets?|cards?)\s+in your deck by\s+(\d+)", text, re.I)
    if deck_cost:
        deck_filters = {"zone": "deck", "tribe": deck_cost.group(1).lower()}
        deck_ct = deck_cost.group(2).lower().rstrip("s")
        if deck_ct != "card":
            deck_filters["card_type"] = deck_ct
        node["effects"].append({"kind": "modify_cost", "target": {"scope": "any", "selection": "all", "filters": deck_filters}, "amount": -int(deck_cost.group(3))})
    # Transform a random spell/card in your hand into X
    transform_hand_random = re.search(r"transform\s+a\s+random\s+(spell|card)\s+in\s+your\s+hand\s+into\s+(?:an?\s+|copies of\s+)?([^.;\"]+)", text, re.I)
    if transform_hand_random:
        thr_filters = {"zone": "hand"}
        if transform_hand_random.group(1).lower() != "card":
            thr_filters["card_type"] = transform_hand_random.group(1).lower()
        node["effects"].append({"kind": "transform", "target": {"scope": "any", "selection": "random", "count": 1, "filters": thr_filters}, "source_card_name": transform_hand_random.group(2).strip()})
    # Generic replacement parsing happens before the ordinary heal/damage
    # regexes above.  Those regexes can also see the base sentence and append
    # it once more; remove only an exact duplicate of a conditional's else
    # branch, leaving unrelated follow-up effects intact.
    conditional_else = {
        json_signature(item)
        for effect_item in node["effects"]
        if effect_item.get("kind") == "conditional"
        for item in effect_item.get("else_effects", [])
    }
    if conditional_else:
        node["effects"] = [
            effect_item for effect_item in node["effects"]
            if effect_item.get("kind") == "conditional" or json_signature(effect_item) not in conditional_else
        ]
    if node.pop("_dedupe_top_level_effects", False):
        seen_effects = set()
        deduped_effects = []
        for effect_item in node["effects"]:
            marker = json_signature(effect_item)
            if marker not in seen_effects:
                seen_effects.add(marker)
                deduped_effects.append(effect_item)
        node["effects"] = deduped_effects
    if evolved_restore_match:
        # ``super-evolved`` is a stronger form of ``evolved``.  Keep an
        # explicit nested conditional so the 2-defense replacement is not
        # flattened into an unconditional heal or a modify-previous marker.
        draw_effect = next((item for item in node["effects"] if item.get("kind") == "draw"), {"kind": "draw", "count": 1})
        heal_one = {"kind": "heal", "target": {"scope": "ally_leader"}, "amount": 1}
        heal_two = {"kind": "heal", "target": {"scope": "ally_leader"}, "amount": 2}
        node["effects"] = [draw_effect, {
            "kind": "conditional",
            "condition": {"state": "super_evolved", "cmp": "eq", "value": True},
            "effects": [heal_two],
            "else_effects": [{
                "kind": "conditional",
                "condition": {"state": "evolved", "cmp": "eq", "value": True},
                "effects": [heal_one],
            }],
        }]
    # Keyword/status extraction has several intentionally overlapping
    # templates (for example ``give it Bane and this follower Storm``).  Do
    # not emit the same idempotent grant twice; repeated damage/summon nodes
    # remain untouched because their multiplicity is meaningful.
    seen_idempotent: set[str] = set()
    deduped_idempotent: list[dict[str, Any]] = []
    for effect_item in node["effects"]:
        if effect_item.get("kind") in {"grant_keyword", "grant_status", "grant_resource_ability"}:
            marker = json_signature(effect_item)
            if marker in seen_idempotent:
                continue
            seen_idempotent.add(marker)
        deduped_idempotent.append(effect_item)
    node["effects"] = deduped_idempotent
    if faith_mode_ability:
        # Preserve the printed order ``reduce Faith, then grant its listener``
        # rather than letting the generic quoted-status pass place the
        # resource mutation after the ability grant.
        faith_reductions = [
            item for item in node["effects"]
            if item.get("kind") == "modify_resource" and item.get("resource") == "faith" and isinstance(item.get("amount"), (int, float)) and item.get("amount") < 0
        ]
        if faith_reductions:
            node["effects"] = faith_reductions + [item for item in node["effects"] if item not in faith_reductions]
    # Dynamic copy extraction runs before the generic discard templates.  If
    # the printed sentence discards first (``Discard your hand. Add ...``),
    # restore that ordering so a later event interpreter sees the same state
    # transition and does not copy cards that should already be gone.
    discard_position = re.search(r"\bdiscard\b|舍弃", text, re.I)
    copy_position = re.search(r"\b(?:add|summon)\s+(?:an?\s+)?(?:exact\s+)?copy", text, re.I)
    if discard_position and copy_position and discard_position.start() < copy_position.start():
        discard_effects = [item for item in node["effects"] if item.get("kind") == "discard"]
        if discard_effects:
            node["effects"] = discard_effects + [item for item in node["effects"] if item.get("kind") != "discard"]
    # Likewise, ``banish/destroy it, then add/summon a copy`` must remove the
    # selected entity before the copy operation.  The copy still carries a
    # source selector so an executor can snapshot the entity before removal.
    remove_position = re.search(r"\b(?:banish|destroy)\s+(?:it|this\s+card|the\s+selected)", text, re.I)
    if remove_position and copy_position and remove_position.start() < copy_position.start():
        removal_effects = [item for item in node["effects"] if item.get("kind") in {"banish", "destroy"}]
        if removal_effects:
            node["effects"] = removal_effects + [item for item in node["effects"] if item.get("kind") not in {"banish", "destroy"}]
    # Preserve the explicit sacrifice order in templates such as
    # ``Destroy this card. Select ... and summon an exact copy``.  Dynamic
    # selector recognition runs before the generic destroy pass, so move that
    # self-destroy back to the front after all effects are known.
    if re.search(r"destroy\s+this\s+card", action_text, re.I) and re.search(r"select\s+", action_text, re.I):
        self_destroy = next((item for item in node["effects"] if item.get("kind") == "destroy" and item.get("target", {}).get("scope") == "self"), None)
        if self_destroy is not None:
            node["effects"] = [self_destroy] + [item for item in node["effects"] if item is not self_destroy]
    # Preserve the source order for event-triggered pronouns: ``give it ...,
    # deal ..., and restore ...`` applies the status to the triggering object
    # before resolving the two leader effects.
    trigger_status = [
        item for item in node["effects"]
        if item.get("kind") == "grant_status"
        and isinstance(item.get("target"), dict)
        and item["target"].get("scope") == "trigger_source"
    ]
    if trigger_status:
        node["effects"] = trigger_status + [item for item in node["effects"] if item not in trigger_status]
    if not node["effects"]:
        node["unparsed"].append(text)
        node["unparsed_clauses"].append(text)
    node["confidence"] = 1.0 if node["effects"] and not node["unparsed"] and not node["unparsed_clauses"] else 0.0
    return node


def card_to_ast(card: dict[str, Any], primary_language: str = "eng") -> dict[str, Any]:
    raw_clauses = [piece for clause in card.get("clauses", []) for piece in split_mode_clauses(clause)]
    clauses = [clause_to_ast(c) for c in raw_clauses]
    # Compare paired CHS/ENG clauses. A disagreement is retained and lowers
    # confidence instead of silently selecting one language.
    paired: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for clause in clauses:
        paired.setdefault((clause.get("source_key"), clause.get("virtual_index", clause.get("index")), clause.get("section"), clause.get("mode")), []).append(clause)
    # A single source sentence can be split into virtual English abilities
    # (notably ``When ... Invoked ... Fanfare: ...``) while a translated CHS
    # sentence remains unsplit.  English is the authoritative executable
    # language; suppress the unsplit translation when the same original
    # source index already has an English clause, otherwise the Chinese
    # parser's broad fallback can duplicate or mis-trigger the ability.
    english_origins = {
        (clause.get("source_key"), clause.get("index"), clause.get("section"), clause.get("mode"))
        for clause in clauses
        if clause.get("source_language") == "eng"
    }
    conflicts = []
    abilities = []
    for key, values in paired.items():
        by_language = {value.get("source_language"): value for value in values}
        chs = by_language.get("chs")
        eng = by_language.get("eng")
        if primary_language == "eng" and eng is None and values:
            origin = (values[0].get("source_key"), values[0].get("index"), values[0].get("section"), values[0].get("mode"))
            if origin in english_origins:
                continue
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
                complete = bool(eng.get("effects")) and not eng.get("unparsed") and not eng.get("unparsed_clauses")
                classification = "matched" if complete else "unparsed"
                confidence = 1.0 if complete else 0.0
            else:
                classification, confidence = "missing_translation", 0.5
        elif primary_language == "chs":
            preferred = chs or eng or values[0]
            if chs:
                complete = bool(chs.get("effects")) and not chs.get("unparsed") and not chs.get("unparsed_clauses")
                classification = "matched" if complete else "unparsed"
                confidence = 1.0 if complete else 0.0
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
            **({"trigger_filter": preferred.get("trigger_filter")} if isinstance(preferred.get("trigger_filter"), dict) else {}),
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
