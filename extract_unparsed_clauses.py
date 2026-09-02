# -*- coding: utf-8 -*-
"""Extract ``unparsed_clauses`` from card_rules_v2.json and attempt to re-parse them.

Phase 1 (extraction) reads card_rules_v2.json **read-only** and writes every
unparsed clause -- together with card context -- into
``data/generated/unparsed_clauses_extracted.json``.

Phase 2 (re-parse) reads the extracted file and tries to parse each clause
with the project's own parser (``card_text_ast.clause_to_ast``), resolving card
references against the catalog name index, then writes per-clause results into
``data/generated/unparsed_clauses_parse_result.json``.

The original card_rules_v2.json is never modified.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from card_text_ast import clause_to_ast
from compile_card_rules import effect

RULES_SRC = Path("data/generated/card_rules_v2.json")
AST_SRC = Path("data/generated/card_text_ast.json")
CATALOG_SRC = Path("data/generated/card_catalog.json")
EXTRACT_OUT = Path("data/generated/unparsed_clauses_extracted.json")
PARSE_OUT = Path("data/generated/unparsed_clauses_parse_result.json")

MARKER_RE = re.compile(r"^unresolved_card_reference:(.+)$")


def build_name_index(catalog: dict) -> dict[str, int]:
    index: dict[str, int] = {}
    for cid, card in catalog.get("cards", {}).items():
        for name in card.get("name", {}).values():
            if isinstance(name, str) and name.strip():
                index.setdefault(name.strip().casefold(), int(cid))
    return index


def resolve_source_card_names(node: dict, name_index: dict) -> list[str]:
    """Return the list of source_card_name values that cannot be resolved."""
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


def extract() -> dict:
    rules = json.loads(RULES_SRC.read_text(encoding="utf-8"))
    ast = json.loads(AST_SRC.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_SRC.read_text(encoding="utf-8"))
    name_index = build_name_index(catalog)
    ast_by_id = {str(card.get("card_id")): card for card in ast.get("cards", {}).values()}
    abilities_by_id: dict[str, dict] = {}
    for card in ast.get("cards", {}).values():
        for ability in card.get("abilities", []):
            abilities_by_id.setdefault(str(ability.get("ability_id")), ability)

    cards: dict[str, dict] = {}
    summary = {"cards": 0, "clauses": 0, "text_clauses": 0, "markers": 0}
    for cid, rule in rules.get("rules", {}).items():
        raw = rule.get("unparsed_clauses") or []
        if not raw:
            continue
        entries = []
        for text in raw:
            match = MARKER_RE.match(text)
            if match:
                ability_id = match.group(1)
                entries.append({
                    "kind": "marker",
                    "ability_id": ability_id,
                    "text": text,
                })
                summary["markers"] += 1
            else:
                entries.append({
                    "kind": "text",
                    "text": text,
                    "language": "eng" if re.search(r"[a-zA-Z]", text) else ("chs" if re.search(r"[\u4e00-\u9fff]", text) else "other"),
                })
                summary["text_clauses"] += 1
            summary["clauses"] += 1
        ast_card = ast_by_id.get(str(cid), {})
        cards[str(cid)] = {
            "card_id": int(cid),
            "name": rule.get("name") or ast_card.get("name"),
            "support": rule.get("support"),
            "modes": [
                {"kind": mode.get("kind"), "cost": mode.get("cost")}
                for mode in rule.get("modes", [])
            ],
            "unparsed_clauses": entries,
        }
        summary["cards"] += 1

    document = {
        "schema_version": 1,
        "source_file": str(RULES_SRC),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cards": cards,
    }
    EXTRACT_OUT.parent.mkdir(parents=True, exist_ok=True)
    EXTRACT_OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {EXTRACT_OUT} ({summary['cards']} cards, {summary['clauses']} clauses)")
    return {"document": document, "name_index": name_index, "abilities_by_id": abilities_by_id, "ast_by_id": ast_by_id}


def reparse(document: dict, name_index: dict, abilities_by_id: dict) -> dict:
    status_counter = Counter()
    trigger_counter = Counter()
    cards: dict[str, dict] = {}
    for cid, card in document["cards"].items():
        parsed_entries = []
        for entry in card["unparsed_clauses"]:
            if entry["kind"] == "marker":
                ability_id = entry["ability_id"]
                ability = abilities_by_id.get(ability_id)
                missing_refs: list[str] = []
                if ability:
                    for effect_node in ability.get("effects", []):
                        if isinstance(effect_node, dict):
                            missing_refs.extend(resolve_source_card_names(effect_node, name_index))
                unique_missing = sorted(set(missing_refs))
                if not missing_refs:
                    status = "marker_resolvable"
                    # re-compile the ability to confirm all effects survive
                    effects = [effect(n, name_index) for n in ability.get("effects", []) if isinstance(n, dict)]
                    effects = [e for e in effects if e is not None]
                    result = {"op": "sequence", "effects": effects} if effects else None
                    status = "marker_resolved" if result else "marker_still_unresolved"
                else:
                    status = "marker_still_unresolved"
                parsed_entries.append({
                    "kind": "marker",
                    "ability_id": ability_id,
                    "text": entry["text"],
                    "status": status,
                    "missing_card_references": unique_missing,
                })
                status_counter[status] += 1
                continue

            # Text clause: attempt to parse with the project parser.
            text = entry["text"]
            clause = {
                "plain": text,
                "language": entry.get("language", "eng"),
                "source_key": "skill",
                "index": 0,
                "section": "normal",
            }
            node = clause_to_ast(clause)
            effects = node.get("effects", [])
            missing_refs = []
            for effect_node in effects:
                if isinstance(effect_node, dict):
                    missing_refs.extend(resolve_source_card_names(effect_node, name_index))
            unique_missing = sorted(set(missing_refs))
            if effects and not missing_refs:
                status = "parsed"
            elif effects and missing_refs:
                status = "parsed_with_unresolved_refs"
            else:
                status = "unparsed"
            status_counter[status] += 1
            trigger_counter[node.get("trigger", "static")] += 1
            parsed_entries.append({
                "kind": "text",
                "text": text,
                "language": entry.get("language", "eng"),
                "status": status,
                "trigger": node.get("trigger"),
                "mode": node.get("mode"),
                "effects": effects,
                "unparsed": node.get("unparsed", []),
                "missing_card_references": unique_missing,
            })
        cards[cid] = {
            "card_id": card["card_id"],
            "name": card["name"],
            "support": card["support"],
            "parsed_entries": parsed_entries,
        }

    result = {
        "schema_version": 1,
        "source_file": str(EXTRACT_OUT),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "cards": len(cards),
            "statuses": dict(status_counter),
            "triggers": dict(trigger_counter),
        },
        "cards": cards,
    }
    PARSE_OUT.parent.mkdir(parents=True, exist_ok=True)
    PARSE_OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PARSE_OUT} ({len(cards)} cards)")
    print("status:", dict(status_counter))
    return result


if __name__ == "__main__":
    context = extract()
    reparse(context["document"], context["name_index"], context["abilities_by_id"])
