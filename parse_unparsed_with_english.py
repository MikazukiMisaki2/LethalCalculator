# -*- coding: utf-8 -*-
"""Re-parse previously-unparsed Chinese clauses using their English descriptions.

Workflow
--------
1. Read the previous re-parse result (``unparsed_clauses_parse_result.json``).
2. For every clause that was still ``unparsed`` and Chinese, look up the
   matching English description from the bilingual card text (normalized file
   derived from ``card_catalog.json``; falls back to ``shadowverse_cards_en.json`` /
   AST ``source_clause``), then run it through the project parser again.
3. Merge the newly-parsed clauses with the previously-``parsed`` ones and write
   ``data/generated/unparsed_clauses_parse_result_v2.json``.

The original ``card_rules_v2.json`` and the previous result file are read-only.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from card_text_ast import clause_to_ast
from compile_card_rules import effect

PREV_RESULT = Path("data/generated/unparsed_clauses_parse_result.json")
EXTRACT = Path("data/generated/unparsed_clauses_extracted.json")
NORMALIZED = Path("data/generated/card_text_normalized.json")
CATALOG = Path("data/generated/card_catalog.json")
AST = Path("data/generated/card_text_ast.json")
OUT = Path("data/generated/unparsed_clauses_parse_result_v2.json")

MARKER_RE = re.compile(r"^unresolved_card_reference:(.+)$")


def nspace(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def build_pair_index(normalized: dict) -> dict[str, dict]:
    """card_id -> {(source_key,index,section): {lang: plain}}"""
    index: dict[str, dict] = {}
    for cid, card in normalized.get("cards", {}).items():
        pairs: dict[tuple, dict] = {}
        for clause in card.get("clauses", []):
            key = (clause.get("source_key"), clause.get("index"), clause.get("section"))
            pairs.setdefault(key, {})[clause.get("language")] = clause.get("plain", "")
        index[cid] = pairs
    return index


def find_english(cid: str, chs_text: str, pair_index: dict, ast_by_id: dict, en_data: dict | None) -> str | None:
    """Return the English description for a Chinese clause, or None."""
    target = nspace(chs_text)
    pairs = pair_index.get(cid, {})
    # 1) exact normalized pairing (from card_catalog.json)
    for key, langs in pairs.items():
        chs = langs.get("chs")
        if chs and nspace(chs) == target and langs.get("eng"):
            return langs["eng"]
    # 2) substring-based fallback within the same card
    for key, langs in pairs.items():
        chs = langs.get("chs")
        if chs and langs.get("eng") and (nspace(chs) in target or target in nspace(chs)):
            return langs["eng"]
    # 3) AST source_clause pairing
    ast_card = ast_by_id.get(cid, {})
    for ability in ast_card.get("abilities", []):
        sc = ability.get("source_clause", {})
        chs = sc.get("chs")
        if chs and nspace(chs) == target and sc.get("eng"):
            return sc["eng"]
    return None


def resolve_source_card_names(node: dict, name_index: dict) -> list[str]:
    missing = []
    kind = node.get("kind")
    if kind in ("summon", "add_to_hand", "transform", "gain_crest"):
        source = node.get("source_card_name")
        if source:
            key = str(source).removeprefix("Crest:").strip().casefold()
            if key and name_index.get(key) is None:
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


def build_name_index(catalog: dict) -> dict[str, int]:
    index: dict[str, int] = {}
    for cid, card in catalog.get("cards", {}).items():
        for name in card.get("name", {}).values():
            if isinstance(name, str) and name.strip():
                index.setdefault(name.strip().casefold(), int(cid))
    return index


def main() -> int:
    prev = json.loads(PREV_RESULT.read_text(encoding="utf-8"))
    normalized = json.loads(NORMALIZED.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    ast = json.loads(AST.read_text(encoding="utf-8"))
    name_index = build_name_index(catalog)
    ast_by_id = {str(card.get("card_id")): card for card in ast.get("cards", {}).values()}

    en_data = None
    try:
        en_raw = Path("shadowverse_cards_en.json")
        if en_raw.exists():
            en_data = json.loads(en_raw.read_text(encoding="utf-8"))
    except Exception:
        en_data = None

    pair_index = build_pair_index(normalized)

    summary = Counter()
    cards_out: dict[str, dict] = {}

    for cid, card in prev.get("cards", {}).items():
        entries_out = []
        for entry in card.get("parsed_entries", []):
            status = entry.get("status")
            if status in ("parsed", "parsed_with_unresolved_refs"):
                # keep previously-parsed entries as-is
                entries_out.append(entry)
                summary["prev_parsed" if status == "parsed" else "prev_parsed_with_unresolved_refs"] += 1
                summary["total_parsed"] += 1
            elif status == "unparsed" and entry.get("language") == "chs":
                # Chinese clause -> find English -> re-parse
                chs_text = entry.get("text", "")
                eng_text = find_english(cid, chs_text, pair_index, ast_by_id, en_data)
                summary["chs_converted"] += 1
                new_entry = {
                    "kind": "text",
                    "original_language": "chs",
                    "original_text": chs_text,
                    "english_replacement": eng_text,
                }
                if not eng_text:
                    new_entry["status"] = "no_english_found"
                    summary["chs_no_english"] += 1
                else:
                    clause = {"plain": eng_text, "language": "eng", "source_key": "skill", "index": 0, "section": "normal"}
                    node = clause_to_ast(clause)
                    effects = node.get("effects", [])
                    missing = []
                    for effect_node in effects:
                        if isinstance(effect_node, dict):
                            missing.extend(resolve_source_card_names(effect_node, name_index))
                    unique_missing = sorted(set(missing))
                    if effects and not unique_missing:
                        new_entry["status"] = "parsed"
                        summary["newly_parsed"] += 1
                        summary["total_parsed"] += 1
                    elif effects and unique_missing:
                        new_entry["status"] = "parsed_with_unresolved_refs"
                        summary["newly_parsed_with_unresolved_refs"] += 1
                    else:
                        new_entry["status"] = "still_unparsed"
                        summary["chs_still_unparsed"] += 1
                    new_entry["trigger"] = node.get("trigger")
                    new_entry["mode"] = node.get("mode")
                    new_entry["effects"] = effects
                    new_entry["unparsed"] = node.get("unparsed", [])
                    new_entry["missing_card_references"] = unique_missing
                entries_out.append(new_entry)
            else:
                # unparsed english / markers: carry through as context, not in parsed set
                summary[status] += 1
                entries_out.append(entry)
        cards_out[cid] = {
            "card_id": card["card_id"],
            "name": card["name"],
            "support": card["support"],
            "parsed_entries": entries_out,
        }

    document = {
        "schema_version": 2,
        "source_files": {
            "previous_result": str(PREV_RESULT),
            "english_reference": [str(CATALOG), str(NORMALIZED), "shadowverse_cards_en.json"],
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
