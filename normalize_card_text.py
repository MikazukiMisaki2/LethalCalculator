"""Normalize bilingual CardCatalog text into compiler-friendly clauses.

This is deliberately a lossless-adjacent preprocessing step: raw ``chs`` and
``eng`` text remain available, while ``plain_*`` fields remove presentation
markup and ``trigger``/``hints`` provide deterministic parser hints.
"""
from __future__ import annotations

import argparse
import json
import re
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

TAG_RE = re.compile(r"</?(?:b|i|color(?:=[^>]+)?|rub(?:=[^>]+)?|ridx(?:=[^>]+)?|size(?:=[^>]+)?|br)[^>]*>", re.I)
BOUNDARY_RE = re.compile(r"<(?P<open>ev|sev)>|</(?P<close>ev|sev)>|<hr\s*/?>", re.I)
UNKNOWN_TAG_RE = re.compile(r"</?([a-zA-Z][\w-]*)(?:\s[^>]*)?>")
TRIGGER_PATTERNS = [
    ("on_fanfare", re.compile(r"fanfare|入场曲|出击" , re.I)),
    ("on_ally_follower_super_evolve", re.compile(r"(?:whenever|when) an allied follower super-?evolves?|自己的随从超进化时", re.I)),
    ("on_enemy_follower_super_evolve", re.compile(r"(?:whenever|when) an enemy follower super-?evolves?|对手的随从超进化时", re.I)),
    ("on_super_evolve", re.compile(r"super-?evolve\s*:|when this follower super-?evolves?|本随从超进化时|超进化时", re.I)),
    ("on_evolve", re.compile(r"evolve\s*:|when\s+.*?evolves?|进化时", re.I)),
    ("on_last_word", re.compile(r"last words|谢幕曲", re.I)),
    ("on_turn_start", re.compile(r"at the start of your turn|自己的回合开始时", re.I)),
    ("on_opponent_turn_end", re.compile(r"^(?:at )?the end of your opponent's turn|^对手的回合结束时", re.I)),
    ("on_turn_end", re.compile(r"end of your turn|自己的回合结束时", re.I)),
    ("on_spellboost", re.compile(r"on spellboost|魔力增幅时|每当.*魔力增幅", re.I)),
    ("on_engage", re.compile(r"engage\s*(?:\(|:)|激奏|激活|启动", re.I)),
    ("on_clash", re.compile(r"clash\s*:|交战时", re.I)),
    ("on_survive_damage", re.compile(r"whenever this follower takes damage but isn't destroyed|受到伤害且没被破坏时", re.I)),
    ("on_ally_follower_summon", re.compile(r"(?:pixie|fairy) follower.*enters? the field|妖精.*随从进入战场", re.I)),
    ("on_summon", re.compile(r"enters? the field|进入战场", re.I)),
]


def plain(text: str) -> str:
    text = TAG_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([，。！？：；、,.!?;:])", r"\1", text)
    text = re.sub(r"([:;])\s+", r"\1 ", text)
    return text


def split_sections(text: str) -> list[tuple[str, str, str]]:
    """Split text while retaining semantic ev/sev/hr boundaries."""
    result = []
    section = "normal"
    start = 0
    for match in BOUNDARY_RE.finditer(text or ""):
        chunk = text[start:match.start()]
        if plain(chunk):
            result.append((section, chunk, match.group(0)))
        token = match.group(0).lower()
        if token.startswith("<ev"):
            section = "evolve"
        elif token.startswith("<sev"):
            section = "super_evolve"
        elif token.startswith("</"):
            section = "normal"
        start = match.end()
    tail = text[start:]
    if plain(tail):
        result.append((section, tail, ""))
    return result


def split_clauses(text: str) -> list[str]:
    return [plain(chunk) for _, chunk, _ in split_sections(text)]


def keyword_tokens(text: str) -> list[str]:
    return sorted(set(re.findall(r"<color=Keyword>(.*?)</color>", text or "", re.I | re.S)))


def unknown_tags(text: str) -> list[str]:
    known = {"b", "i", "color", "rub", "ridx", "size", "br", "ev", "sev", "hr"}
    return sorted(set(tag.lower() for tag in UNKNOWN_TAG_RE.findall(text or "") if tag.lower() not in known))


def trigger_for(clause: str) -> str:
    for trigger, pattern in TRIGGER_PATTERNS:
        if pattern.search(clause):
            return trigger
    return "static"


def hints(clause: str) -> list[str]:
    value = clause.lower()
    result = []
    if re.search(r"do this\s+\w+\s+times|发动\s*[x一二三四五六七八九十]+\s*次", value):
        result.append("repeat")
    if "random" in value or "随机" in clause:
        result.append("random_target")
    if "split between" in value or "分配" in clause:
        result.append("ordered_split")
    if re.search(r"select|选择", clause, re.I):
        result.append("chosen_target")
    if re.search(r"\b(?:x|x点|x damage)\b|为.*的.*数", clause, re.I):
        result.append("variable_amount")
    if re.search(r"fairy|pixie|妖精", clause, re.I):
        result.append("tribe_condition")
    for word, hint in (("summon", "summon"), ("draw", "draw"), ("destroy", "destroy"), ("banish", "banish"), ("restore", "heal"), ("give ", "buff")):
        if word in value:
            result.append(hint)
    return sorted(set(result))


def structure(clause: str) -> dict[str, Any]:
    value = clause.lower()
    amounts = [int(x) for x in re.findall(r"(?<![a-z])([0-9]+)\s*(?:点)?\s*(?:damage|伤害)", value)]
    repeat_match = re.search(r"do this\s+([0-9]+|x)\s+times|发动\s*([0-9]+|x)\s*次", value)
    target = "enemy_leader" if ("enemy leader" in value or "主战者" in clause) else None
    if "random enemy follower" in value or "随机" in clause and "随从" in clause:
        target = "enemy_follower_random"
    elif "all enemy follower" in value or "所有随从" in clause:
        target = "enemy_follower_ordered_split" if "split" in value or "分配" in clause else "enemy_follower_all"
    elif "enemy follower" in value or "对手" in clause and "随从" in clause:
        target = "enemy_follower_chosen"
    variables = sorted(set(re.findall(r"\bX\b|\b[A-Z][A-Za-z_]+\b", clause)))
    return {
        "amounts": amounts,
        "repeat": int(repeat_match.group(1) or repeat_match.group(2)) if repeat_match and (repeat_match.group(1) or repeat_match.group(2)).isdigit() else ("variable" if repeat_match else None),
        "target": target,
        "variables": variables,
    }


def normalize_card(card: dict[str, Any]) -> dict[str, Any]:
    entries = []
    seen = set()
    source_entries = card.get("text", {}).get("skill_texts", [])
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        for language in ("chs", "eng"):
            raw = entry.get(language, "")
            for index, (section, raw_clause, boundary) in enumerate(split_sections(raw)):
                clause = plain(raw_clause)
                key = (language, section, clause)
                if key in seen:
                    continue
                seen.add(key)
                entries.append({"language": language, "source_key": entry.get("key", ""), "index": index, "section": section, "boundary": boundary, "raw": raw_clause, "plain": clause, "text_chs": clause if language == "chs" else "", "text_eng": clause if language == "eng" else "", "keywords": keyword_tokens(raw_clause), "unknown_tags": unknown_tags(raw_clause), "trigger": trigger_for(clause), "hints": hints(clause), "structure": structure(clause)})
    source_hash = hashlib.sha256(json.dumps(source_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"card_id": card.get("card_id"), "name": card.get("name", {}), "source_hash": source_hash, "clauses": entries}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/generated/card_catalog.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_text_normalized.json"))
    parser.add_argument("--report", type=Path, default=Path("data/generated/card_text_normalization_report.json"))
    args = parser.parse_args()
    catalog = json.loads(args.input.read_text(encoding="utf-8"))
    cards = catalog.get("cards", {})
    normalized = {"schema_version": 1, "catalog_source": catalog.get("source", {}), "cards": {cid: normalize_card(card) for cid, card in cards.items()}}
    trigger_counts = Counter()
    hint_counts = Counter()
    for card in normalized["cards"].values():
        for clause in card["clauses"]:
            trigger_counts[clause["trigger"]] += 1
            hint_counts.update(clause["hints"])
    unknown = Counter(tag for card in normalized["cards"].values() for clause in card["clauses"] for tag in clause["unknown_tags"])
    report = {"cards": len(cards), "clauses": sum(trigger_counts.values()), "triggers": dict(trigger_counts), "hints": dict(hint_counts), "unknown_tags": dict(unknown)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({report['cards']} cards, {report['clauses']} clauses)")
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
