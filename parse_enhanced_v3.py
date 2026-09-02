# -*- coding: utf-8 -*-
"""Enhanced re-parse of unparsed clauses after parser enhancement (v3).

Workflow
--------
1. Read the enhanced pipeline outputs produced by the strengthened parser:
   ``card_text_ast_enhanced.json`` (parse) and ``card_rules_enhanced.json`` (compile).
2. Extract every remaining ``unparsed_clauses`` entry (text + unresolved markers).
3. Re-parse each text clause with the enhanced ``clause_to_ast`` + ``effect``;
   Chinese clauses are first mapped to their English description via the
   bilingual normalized text (``find_english``).
4. Re-check markers against the enhanced AST abilities.
5. Write ``data/generated/unparsed_clauses_parse_result_v3.json``.

The original ``card_rules_v2.json`` and all prior pipeline outputs are read-only.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from card_text_ast import clause_to_ast
from compile_card_rules import effect, _resolve_card_name

ENHANCED_RULES = Path("data/generated/card_rules_enhanced.json")
ENHANCED_AST = Path("data/generated/card_text_ast_enhanced.json")
NORMALIZED = Path("data/generated/card_text_normalized.json")
CATALOG = Path("data/generated/card_catalog.json")
PREV_RESULT = Path("data/generated/unparsed_clauses_parse_result_v2.json")
OUT = Path("data/generated/unparsed_clauses_parse_result_v3.json")

MARKER_RE = re.compile(r"^unresolved_card_reference:(.+)$")


def nspace(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def build_name_index(catalog: dict) -> dict[str, int]:
    index: dict[str, int] = {}
    for cid, card in catalog.get("cards", {}).items():
        for name in card.get("name", {}).values():
            if isinstance(name, str) and name.strip():
                index.setdefault(name.strip().casefold(), int(cid))
    return index


def build_pair_index(normalized: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for cid, card in normalized.get("cards", {}).items():
        pairs: dict[tuple, dict] = {}
        for clause in card.get("clauses", []):
            key = (clause.get("source_key"), clause.get("index"), clause.get("section"))
            pairs.setdefault(key, {})[clause.get("language")] = clause.get("plain", "")
        index[cid] = pairs
    return index


def find_english(cid: str, chs_text: str, pair_index: dict, ast_by_id: dict) -> str | None:
    target = nspace(chs_text)
    pairs = pair_index.get(cid, {})
    for key, langs in pairs.items():
        chs = langs.get("chs")
        if chs and nspace(chs) == target and langs.get("eng"):
            return langs["eng"]
    for key, langs in pairs.items():
        chs = langs.get("chs")
        if chs and langs.get("eng") and (nspace(chs) in target or target in nspace(chs)):
            return langs["eng"]
    ast_card = ast_by_id.get(cid, {})
    for ability in ast_card.get("abilities", []):
        sc = ability.get("source_clause", {})
        chs = sc.get("chs")
        if chs and nspace(chs) == target and sc.get("eng"):
            return sc["eng"]
    return None


def resolve_source_card_names(node: dict, name_index: dict) -> list[str]:
    """Missing source names using the enhanced fallback resolution."""
    missing = []
    kind = node.get("kind")
    if kind in ("summon", "add_to_hand", "transform", "gain_crest"):
        source = node.get("source_card_name")
        if source:
            key = str(source).removeprefix("Crest:").strip().casefold()
            if key and name_index.get(key) is None and _resolve_card_name(name_index, source) is None:
                missing.append(str(source))
    for child in node.get("effects", []) or []:
        if isinstance(child, dict):
            missing.extend(resolve_source_card_names(child, name_index))
    for step in node.get("steps", []) or []:
        for child in step.get("effects", []) or []:
            if isinstance(child, dict):
                missing.extend(resolve_source_card_names(child, name_index))
    for choice in node.get("choices", []) or []:
        for child in choice.get("effects", []) or []:
            if isinstance(child, dict):
                missing.extend(resolve_source_card_names(child, name_index))
    if node.get("else_effects"):
        for child in node.get("else_effects", []):
            if isinstance(child, dict):
                missing.extend(resolve_source_card_names(child, name_index))
    return missing


def parse_text_entry(cid: str, text: str, language: str, pair_index: dict, ast_by_id: dict, name_index: dict, summary: Counter) -> dict:
    clause = {"plain": text, "language": language, "source_key": "skill", "index": 0, "section": "normal"}
    entry = {"kind": "text", "text": text, "language": language}
    node = clause_to_ast(clause)
    effects = node.get("effects", [])
    # For Chinese clauses the enhanced parser may handle some patterns directly;
    # if not, fall back to the bilingual English description.
    if language == "chs" and not effects:
        eng_text = find_english(cid, text, pair_index, ast_by_id)
        entry["original_language"] = "chs"
        entry["original_text"] = text
        entry["english_replacement"] = eng_text
        summary["chs_converted"] += 1
        if not eng_text:
            entry["status"] = "no_english_found"
            return entry
        clause["plain"] = eng_text
        clause["language"] = "eng"
        entry["text"] = eng_text
        node = clause_to_ast(clause)
        effects = node.get("effects", [])
    missing = []
    for effect_node in effects:
        if isinstance(effect_node, dict):
            missing.extend(resolve_source_card_names(effect_node, name_index))
    unique_missing = sorted(set(missing))
    if effects and not unique_missing:
        entry["status"] = "parsed"
    elif effects and unique_missing:
        entry["status"] = "parsed_with_unresolved_refs"
    else:
        entry["status"] = "unparsed"
    summary[entry["status"]] += 1
    entry["trigger"] = node.get("trigger")
    entry["mode"] = node.get("mode")
    entry["effects"] = effects
    entry["unparsed"] = node.get("unparsed", [])
    entry["missing_card_references"] = unique_missing
    return entry


def main() -> int:
    rules = json.loads(ENHANCED_RULES.read_text(encoding="utf-8"))
    ast = json.loads(ENHANCED_AST.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    normalized = json.loads(NORMALIZED.read_text(encoding="utf-8"))

    name_index = build_name_index(catalog)
    ast_by_id = {str(card.get("card_id")): card for card in ast.get("cards", {}).values()}
    abilities_by_id: dict[str, dict] = {}
    for card in ast.get("cards", {}).values():
        for ability in card.get("abilities", []):
            abilities_by_id.setdefault(str(ability.get("ability_id")), ability)
    pair_index = build_pair_index(normalized)

    summary = Counter()
    cards_out: dict[str, dict] = {}

    for cid, rule in rules.get("rules", {}).items():
        raw = rule.get("unparsed_clauses") or []
        if not raw:
            continue
        parsed_entries = []
        for text in raw:
            marker = MARKER_RE.match(text)
            if marker:
                ability_id = marker.group(1)
                ability = abilities_by_id.get(ability_id)
                missing_refs: list[str] = []
                if ability:
                    for effect_node in ability.get("effects", []):
                        if isinstance(effect_node, dict):
                            missing_refs.extend(resolve_source_card_names(effect_node, name_index))
                unique_missing = sorted(set(missing_refs))
                if ability and not unique_missing:
                    compiled = [effect(n, name_index) for n in ability.get("effects", []) if isinstance(n, dict)]
                    compiled = [c for c in compiled if c is not None]
                    status = "marker_resolved" if compiled else "marker_still_unresolved"
                else:
                    status = "marker_still_unresolved"
                summary[status] += 1
                parsed_entries.append({
                    "kind": "marker",
                    "ability_id": ability_id,
                    "text": text,
                    "status": status,
                    "missing_card_references": unique_missing,
                })
            else:
                language = "eng" if re.search(r"[a-zA-Z]", text) else ("chs" if re.search(r"[\u4e00-\u9fff]", text) else "other")
                parsed_entries.append(parse_text_entry(cid, text, language, pair_index, ast_by_id, name_index, summary))
        cards_out[cid] = {
            "card_id": int(cid),
            "name": rule.get("name"),
            "support": rule.get("support"),
            "parsed_entries": parsed_entries,
        }

    document = {
        "schema_version": 3,
        "parser_version": "enhanced-1",
        "source_files": {
            "enhanced_rules": str(ENHANCED_RULES),
            "enhanced_ast": str(ENHANCED_AST),
            "catalog": str(CATALOG),
            "normalized": str(NORMALIZED),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": dict(summary),
        "cards": cards_out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    print("summary:", dict(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
