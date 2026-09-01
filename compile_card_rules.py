"""Compile conservative text AST nodes into CardRules v2 draft rules."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def effect(node: dict[str, Any], name_index: dict[str, int] | None = None) -> dict[str, Any] | None:
    kind = node.get("kind")
    if kind == "damage":
        out = {"op": "damage", "target": node.get("target", {}), "amount": node.get("amount", 0)}
        return out
    if kind == "heal":
        return {"op": "heal", "target": node.get("target", {"scope": "ally_leader"}), "amount": node.get("amount", 0)}
    if kind == "repeat":
        children = [effect(x, name_index) for x in node.get("effects", []) if isinstance(x, dict)]
        children = [x for x in children if x is not None]
        return {"op": "repeat", "count": node.get("count", 0), "effects": children} if children else None
    if kind == "conditional":
        children = [effect(x, name_index) for x in node.get("effects", []) if isinstance(x, dict)]
        children = [x for x in children if x is not None]
        else_children = [effect(x, name_index) for x in node.get("else_effects", []) if isinstance(x, dict)]
        else_children = [x for x in else_children if x is not None]
        return {"op": "conditional", "condition": node.get("condition", {}), "effects": children, "else_effects": else_children} if children else None
    if kind == "mode_choice":
        choices = []
        for choice in node.get("choices", []):
            nested = [effect(item, name_index) for item in choice.get("effects", []) if isinstance(item, dict)]
            nested = [item for item in nested if item is not None]
            if nested:
                choices.append({"label": str(choice.get("label", "")), "effects": nested})
        return {"op": "mode_choice", "choices": choices} if choices else None
    if kind == "activate_all_mode_choices":
        return {"op": "activate_all_mode_choices"}
    if kind == "progressive_sequence":
        steps = []
        for step in node.get("steps", []):
            nested = [effect(item, name_index) for item in step.get("effects", []) if isinstance(item, dict)]
            nested = [item for item in nested if item is not None]
            if nested:
                steps.append({"label": str(step.get("label", "")), "effects": nested})
        return {"op": "progressive_sequence", "steps": steps} if steps else None
    if kind == "draw":
        out = {"op": "draw", "count": node.get("count", 1)}
        if node.get("target"):
            out["target"] = node["target"]
        return out
    if kind == "buff":
        return {"op": "buff", "target": node.get("target", {"scope": "self"}), "attack": node.get("attack", 0), "life": node.get("life", 0)}
    if kind == "recover_pp":
        return {"op": "recover_pp", "amount": node.get("amount", 0)}
    if kind == "modify_cost":
        return {"op": "modify_cost", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 0)}
    if kind == "set_cost":
        return {"op": "set_cost", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 0)}
    if kind == "set_attacks":
        return {"op": "set_attacks", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 1)}
    if kind == "reanimate":
        return {"op": "reanimate", "cost": node.get("cost", 0)}
    if kind == "spellboost":
        return {"op": "spellboost", "target": node.get("target", {"scope": "any"}), "count": node.get("count", 1)}
    if kind == "discard":
        return {"op": "discard", "target": node.get("target", {"scope": "any", "selection": "chosen"})}
    if kind == "invoke":
        return {"op": "invoke", "target": node.get("target", {"scope": "self"})}
    if kind == "gain_crest":
        source_name = str(node.get("source_card_name", "")).removeprefix("Crest:").strip().casefold()
        card_id = (name_index or {}).get(source_name)
        return {"op": "gain_crest", "card_id": card_id, "target": node.get("target", {"scope": "ally_leader"})} if card_id else None
    if kind == "banish":
        return {"op": "banish", "target": node.get("target", {"scope": "any"})}
    if kind == "modify_crest":
        return {"op": "modify_crest", "target": {"scope": "any", "selection": "all" if node.get("selection") == "all" else "chosen", "filters": {"zone": "crests"}}, "amount": node.get("amount", 0)}
    if kind == "destroy_crest":
        return {"op": "destroy_crest", "target": node.get("target", {"scope": "any", "filters": {"zone": "crests"}})}
    if kind == "transform":
        out = {"op": "transform", "target": node.get("target", {"scope": "any"})}
        if node.get("source_card_name"):
            card_id = (name_index or {}).get(str(node["source_card_name"]).strip().casefold())
            if not card_id:
                return None
            out["card_id"] = card_id
        else:
            out["resource_selector"] = node.get("source", {})
        return out
    if kind == "return_to_hand":
        return {"op": "return_to_hand", "target": node.get("target", {"scope": "any"})}
    if kind == "return_to_deck":
        return {"op": "return_to_deck", "target": node.get("target", {"scope": "any"})}
    if kind == "modify_resource":
        out = {"op": "modify_resource", "resource": node.get("resource", "faith"), "amount": node.get("amount", 0)}
        if node.get("target"):
            out["target"] = node["target"]
        return out
    if kind == "modify_counter":
        out = {"op": "modify_counter", "field": node.get("field", "countdown"), "delta": node.get("delta", 0)}
        if node.get("target"):
            out["target"] = node["target"]
        return out
    if kind == "consume_resource":
        return {"op": "consume_resource", "resource": node.get("resource", "cemetery"), "amount": node.get("amount", 0)}
    if kind in ("summon", "add_to_hand"):
        source_name = str(node.get("source_card_name", "")).strip().casefold()
        card_id = (name_index or {}).get(source_name)
        if not card_id:
            return None
        return {"op": kind, "card_id": card_id, "count": node.get("count", 1)}
    if kind == "destroy":
        return {"op": "destroy", "target": node.get("target", {"scope": "any"})}
    if kind == "auto_evolve":
        return {"op": "auto_evolve", "target": node.get("target", {"scope": "self"}), "evolution_kind": node.get("evolution_kind", "normal")}
    if kind == "replicate_ability":
        return {"op": "replicate_ability", "trigger": node.get("trigger", "on_fanfare")}
    if kind == "grant_keyword":
        return {"op": "grant_keyword", "keyword": node.get("keyword", ""), "target": node.get("target", {"scope": "self"})}
    if kind == "grant_status":
        return {"op": "grant_status", "status": node.get("status", ""), "duration": node.get("duration", "permanent"), "target": node.get("target", {"scope": "self"})}
    if kind == "set_stat":
        return {"op": "set_stat", "stat": node.get("stat", "life"), "amount": node.get("amount", 0), "target": node.get("target", {"scope": "self"})}
    if kind == "modify_previous_effect":
        return {"op": "modify_previous_effect", "field": node.get("field", "amount"), "value": node.get("value")}
    return None


def compile_card(card_id: str, ast_card: dict[str, Any], catalog_card: dict[str, Any], name_index: dict[str, int] | None = None) -> dict[str, Any]:
    abilities_by_mode: dict[str, list[dict[str, Any]]] = {"normal": [], "enhance": [], "accelerate": [], "crystallize": []}
    static_keywords = set()
    variables: dict[str, Any] = {}
    fusion_configs: list[dict[str, Any]] = []
    countdown_initial: int | None = None
    unparsed = []
    unsupported = False
    # Compile the paired bilingual ability once. The legacy per-language
    # clauses remain in the AST solely for audit/source tracing.
    for clause in ast_card.get("abilities", []):
        static_keywords.update(clause.get("static_keywords", []))
        variables.update(clause.get("variable_initializers", {}))
        if clause.get("countdown_initial") is not None:
            countdown_initial = int(clause["countdown_initial"])
        for node in clause.get("effects", []):
            if not isinstance(node, dict) or node.get("kind") != "fusion_config":
                continue
            config = dict(node.get("config", {}))
            resolved_outcomes = []
            for outcome in config.get("outcomes", []):
                card_id_value = (name_index or {}).get(str(outcome.get("source_card_name", "")).strip().casefold())
                if card_id_value:
                    resolved_outcomes.append({"min": outcome.get("min", 0), "max": outcome.get("max"), "card_id": card_id_value})
            if config.get("outcomes"):
                config["outcomes"] = resolved_outcomes
            fusion_configs.append(config)
        classification = clause.get("classification", "unparsed")
        if classification != "matched":
            unsupported = True
            sources = clause.get("source_clause", {})
            if isinstance(sources, dict):
                primary = ast_card.get("primary_language", "eng")
                source_text = sources.get(primary) or sources.get("eng") or sources.get("chs")
                if source_text:
                    unparsed.append(source_text)
            unparsed.extend(clause.get("unparsed_clauses", []))
        mode = clause.get("mode") or "normal"
        mode_selection = mode == "mode_selection"
        if mode_selection:
            # Mode selection is represented as an explicit planned effect in
            # the normal play ability; the interpreter will mark it
            # INCOMPLETE until UI selection semantics are implemented.
            mode = "normal"
        if mode not in abilities_by_mode:
            unsupported = True
            sources = clause.get("source_clause", {})
            if isinstance(sources, dict):
                unparsed.extend(text for text in sources.values() if text)
            continue
        if clause.get("trigger") == "static" and not mode_selection:
            non_keyword_nodes = [node for node in clause.get("effects", []) if node.get("kind") not in ("grant_keyword", "fusion_config", "countdown_config")]
            for node in clause.get("effects", []):
                if node.get("kind") == "grant_keyword" and node.get("keyword"):
                    static_keywords.add(node["keyword"])
            # Spell text without an explicit trigger resolves when played.
            # Do not apply this fallback to followers/amulets, where a static
            # clause may describe an aura or activation ability.
            if (catalog_card.get("type") != "spell" and mode == "normal") or not non_keyword_nodes:
                continue
            clause = dict(clause)
            clause["trigger"] = "on_play"
            clause["effects"] = non_keyword_nodes
        source_nodes = [node for node in clause.get("effects", []) if isinstance(node, dict) and node.get("kind") not in ("fusion_config", "countdown_config") and not (node.get("kind") == "grant_keyword" and node.get("keyword") in clause.get("static_keywords", []))]
        effects = [effect(node, name_index) for node in source_nodes]
        effects = [x for x in effects if x is not None]
        if len(effects) != len(source_nodes):
            unsupported = True
            unparsed.append(f"unresolved_card_reference:{clause.get('ability_id', card_id)}")
        if not effects and classification == "matched":
            continue
        if classification != "matched" or any(node.get("kind") not in {"damage", "heal", "repeat", "conditional", "mode_choice", "activate_all_mode_choices", "progressive_sequence", "draw", "buff", "recover_pp", "modify_cost", "set_cost", "set_attacks", "reanimate", "spellboost", "discard", "invoke", "gain_crest", "banish", "modify_crest", "destroy_crest", "transform", "fusion_config", "countdown_config", "return_to_hand", "return_to_deck", "modify_resource", "modify_counter", "consume_resource", "summon", "add_to_hand", "destroy", "auto_evolve", "replicate_ability", "grant_keyword", "grant_status", "set_stat", "modify_previous_effect"} for node in clause.get("effects", []) if isinstance(node, dict) and not (node.get("kind") == "grant_keyword" and node.get("keyword") in clause.get("static_keywords", []))):
            unsupported = True
        if effects:
            ability = {"trigger": ("on_play" if mode_selection else clause.get("trigger", "on_play")), "effects": effects}
            if clause.get("trigger") == "on_engage" and clause.get("mode_cost") is not None:
                ability["cost"] = clause["mode_cost"]
            conditions = clause.get("conditions", [])
            if len(conditions) == 1:
                ability["condition"] = conditions[0]
            elif len(conditions) > 1:
                ability["condition"] = {"all": conditions}
            abilities_by_mode[mode].append(ability)
    for mode_name, mode_abilities in abilities_by_mode.items():
        seen = set()
        deduped = []
        for ability in mode_abilities:
            marker = json.dumps(ability, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if marker not in seen:
                seen.add(marker)
                deduped.append(ability)
        abilities_by_mode[mode_name] = deduped
    has_abilities = any(abilities_by_mode.values())
    support = "partial" if unsupported or unparsed else ("generated" if has_abilities or static_keywords or fusion_configs or countdown_initial is not None else "unsupported")
    rule = {
        "card_id": int(card_id),
        "support": support,
        "source_hash": hashlib.sha256(json.dumps(ast_card, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "modes": [{"kind": mode, "cost": (catalog_card.get("cost", 0) if mode == "normal" else next((a.get("mode_cost") for a in ast_card.get("abilities", []) if a.get("mode") == mode and a.get("mode_cost") is not None), 0)), "abilities": abilities_by_mode[mode]} for mode in ("normal", "enhance", "accelerate", "crystallize") if abilities_by_mode[mode] or mode == "normal"],
    }
    if not rule["modes"][0]["abilities"]:
        rule["modes"][0]["abilities"] = [{"trigger": "on_play", "effects": [{"op": "sequence", "effects": []}]}]
    if static_keywords:
        rule["static_keywords"] = sorted(static_keywords)
    if variables:
        rule["variables"] = variables
    if countdown_initial is not None:
        rule["countdown"] = countdown_initial
    if fusion_configs:
        rule["fusion"] = fusion_configs
    if unparsed:
        rule["unparsed_clauses"] = sorted(set(unparsed))
    return rule


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("data/generated/card_catalog.json"))
    parser.add_argument("--ast", type=Path, default=Path("data/generated/card_text_ast.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_rules_generated.json"))
    parser.add_argument("--report", type=Path, default=Path("data/generated/card_rules_compile_report.json"))
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    ast = json.loads(args.ast.read_text(encoding="utf-8"))
    name_index = {}
    for cid, card in catalog.get("cards", {}).items():
        for name in card.get("name", {}).values():
            if isinstance(name, str) and name.strip():
                name_index.setdefault(name.strip().casefold(), int(cid))
    rules = {cid: compile_card(cid, ast_card, catalog.get("cards", {}).get(cid, {}), name_index) for cid, ast_card in ast.get("cards", {}).items()}
    counts = {status: sum(rule["support"] == status for rule in rules.values()) for status in ("verified", "generated", "partial", "unsupported")}
    output = {"schema_version": 2, "catalog_version": 1, "game_version": catalog.get("game_version", ""), "rules": rules}
    report = {"cards": len(rules), "support": counts, "source_catalog": str(args.catalog), "source_ast": str(args.ast)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(rules)} rules)")
    print(f"wrote {args.report} ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
