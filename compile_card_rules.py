"""Compile conservative text AST nodes into CardRules v2 draft rules."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from rule_coverage import build_coverage_report


_CARD_NAME_CUTTERS = re.compile(
    r"\s+and\s+|\s*,\s*|\s+to\s+(?:your\s+)?hand\b|\s+that\s+costs?\b|\s+from\s+(?:your\s+)?deck\b|\s+with\s+last\s+words\b|\s+in\s+(?:your\s+)?hand\b",
    re.I,
)


def _resolve_card_name(name_index: dict[str, int] | None, raw: Any) -> int | None:
    """Resolve a possibly-contaminated card-name string to a catalog id.

    Falls back from an exact match to cutting off trailing effect phrases
    ('and give it Ward', 'to your hand', ...), then to a prefix match (which
    recovers names truncated at punctuation such as 'Istyndet vs')."""
    if not name_index:
        return None
    source = str(raw or "").strip().casefold()
    if not source:
        return None
    direct = name_index.get(source)
    if direct:
        return direct
    for cut in _CARD_NAME_CUTTERS.split(source):
        cut = cut.strip(" .,")
        if not cut:
            continue
        hit = name_index.get(cut)
        if hit:
            return hit
    candidates = {cid for name, cid in name_index.items() if name.startswith(source) or source.startswith(name)}
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _apply_previous_modifier(base_effects: list[dict[str, Any]], modifier: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Apply a mode/replacement modifier to the preceding base effect.

    ``instead`` text is emitted by the parser as a relation rather than an
    invented standalone action. It is only resolved when the same source
    ability has an earlier base effect; otherwise the relation remains in the
    rule as planned ``modify_previous_effect`` and the solver reports
    INCOMPLETE.
    """
    if not base_effects:
        return None
    result = copy.deepcopy(base_effects)
    field = modifier.get("field")
    value = modifier.get("value")
    if value is None:
        return None

    # A clause can contain more than one operation (for example, damage plus
    # a token summon).  ``instead`` modifies the first compatible operation,
    # rather than whichever operation happened to be serialized last.
    priorities = {
        "amount": ("damage", "heal", "buff"),
        "target": ("damage", "heal", "destroy", "banish", "buff"),
        "selection_count": ("damage", "destroy", "banish", "summon", "add_to_hand", "draw"),
        "target_count": ("damage", "destroy", "banish", "summon", "add_to_hand", "draw"),
        "count": ("add_to_hand", "summon", "draw", "repeat", "destroy", "banish"),
        "repeat_count": ("repeat", "damage", "heal", "buff", "destroy", "banish"),
    }
    index = None
    for op_name in priorities.get(field, ()):
        index = next((i for i, item in enumerate(result) if isinstance(item, dict) and item.get("op") == op_name), None)
        if index is not None:
            break
    if index is None:
        return None
    target = result[index].get("target") if isinstance(result[index], dict) else None
    if field == "amount":
        result[index]["amount"] = value
    elif field == "count":
        result[index]["count"] = value
    elif field in ("selection_count", "target_count"):
        if not isinstance(target, dict):
            return None
        target["count"] = value
    elif field == "target":
        if not isinstance(value, dict) or "scope" not in value:
            return None
        result[index]["target"] = value
    elif field == "repeat_count":
        if result[index].get("op") == "repeat":
            result[index]["count"] = value
        else:
            # The source often says “do this N times” while the base parser
            # has a single damage node.  Wrap only that action; adjacent
            # effects such as a keyword grant must not be repeated.
            result[index] = {"op": "repeat", "count": value, "effects": [result[index]]}
    else:
        return None
    return result


def _ability_group_key(clause: dict[str, Any]) -> str:
    """Return the stable source group shared by mode virtual clauses."""
    ability_id = str(clause.get("ability_id", ""))
    parts = ability_id.split(":")
    if len(parts) >= 5:
        return ":".join(parts[:-1])
    return ":".join(str(clause.get(key, "")) for key in ("source_key", "index", "section"))


def _base_effects(effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in effects if isinstance(item, dict) and item.get("op") != "modify_previous_effect"]


def _is_redundant_translation_source(
    clause: dict[str, Any],
    source_text: str | None,
    source_nodes: list[dict[str, Any]],
    effects: list[dict[str, Any]],
) -> bool:
    """Return whether a non-primary Chinese clause is fully represented.

    The AST keeps both language clauses for provenance.  When the primary
    English text is absent, ``card_to_ast`` necessarily labels a successfully
    parsed Chinese clause ``missing_translation``; the old compiler then
    copied that same Chinese sentence into ``unparsed_clauses`` even though
    every effect had compiled.  Suppress only this narrow, safe case: the
    source must be the Chinese side, the clause must contain effects, every
    source node must resolve, and the parser must not report a remainder other
    than the source sentence itself.  Dynamic copies/unknown operations
    therefore remain visible.
    """
    sources = clause.get("source_clause")
    if not isinstance(sources, dict) or not source_text:
        return False
    if not sources.get("chs") or source_text != sources.get("chs"):
        return False
    if not clause.get("effects"):
        return False
    # The parser may put the source sentence itself in this list when a
    # language is missing.  That exact marker is the duplicate we are
    # cleaning; any additional remainder still makes the clause genuinely
    # incomplete and must be retained.
    residual_markers = [marker for marker in clause.get("unparsed_clauses", []) if marker != source_text]
    if residual_markers:
        return False
    return bool(source_nodes) and len(source_nodes) == len(effects)


def _find_modifier_base(
    clause: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Find the base operation for a virtual Enhance/Super-Evolve clause."""
    group = _ability_group_key(clause)
    mode = clause.get("mode")
    trigger = clause.get("trigger")
    ranked: list[tuple[int, list[dict[str, Any]]]] = []
    for candidate in candidates:
        if candidate.get("clause") is clause:
            continue
        effects = _base_effects(candidate.get("effects", []))
        if not effects:
            continue
        other = candidate.get("clause", {})
        other_mode = other.get("mode")
        other_trigger = other.get("trigger")
        score = 0
        if _ability_group_key(other) == group:
            # Enhance/Accelerate/Crystallize/Skybound virtual clauses replace
            # the normal operation from the same source ability.
            score = 100 if other_mode in (None, "normal") else 80
        if trigger == "on_super_evolve" and other_trigger == "on_evolve":
            score = max(score, 90)
        if mode in ("enhance", "accelerate", "crystallize", "skybound_art", "super_skybound_art") and other_trigger == trigger:
            score = max(score, 85)
        if score:
            ranked.append((score, effects))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _resolve_previous_modifiers(
    clause: dict[str, Any],
    effects: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Resolve safe replacement relations and flag unresolved ones.

    The parser keeps ``instead`` as an explicit relation.  We materialize it
    only when a base operation is unambiguous (same source mode, or an
    on_super_evolve replacement of on_evolve).  Conditional branches already
    contain their own base/alternative and therefore simply discard the
    parser's redundant relation node.
    """
    modifiers = [item for item in effects if item.get("op") == "modify_previous_effect"]
    if not modifiers:
        return effects, False
    non_modifiers = _base_effects(effects)
    source_text = " ".join(str(value) for value in (clause.get("source_clause") or {}).values())
    if non_modifiers and any(item.get("op") == "conditional" for item in non_modifiers) and re.search(r"instead|combo|rally|连击|协作", source_text, re.I):
        return non_modifiers, False
    # A modifier and a base action in the same raw clause is ambiguous when
    # it says “if ... instead”; applying it unconditionally would be worse
    # than retaining a planned relation for the runtime to report.
    if non_modifiers and re.search(r"\binstead\b|改为", source_text, re.I):
        # A virtual Enhance/Skybound clause may also carry a follow-up
        # keyword (for example “Summon 3 instead and give them Ward”).  The
        # follow-up is local, while the replaced summon/damage comes from the
        # normal-mode candidate.
        if clause.get("mode") in ("enhance", "accelerate", "crystallize", "skybound_art", "super_skybound_art"):
            base = _find_modifier_base(clause, candidates)
            if base:
                working = base
                unresolved = False
                for modifier in modifiers:
                    applied = _apply_previous_modifier(working, modifier)
                    if applied is None:
                        unresolved = True
                    else:
                        working = applied
                if not unresolved:
                    return working + non_modifiers, False
        return effects, True
    base = non_modifiers or _find_modifier_base(clause, candidates)
    if not base:
        return effects, True
    working = base
    unresolved = False
    for modifier in modifiers:
        applied = _apply_previous_modifier(working, modifier)
        if applied is None:
            unresolved = True
        else:
            working = applied
    if unresolved:
        # Preserve the relation node so schema consumers can see exactly what
        # remains unsupported; never silently drop it.
        working = working + [item for item in modifiers if _apply_previous_modifier(base, item) is None]
    return working, unresolved


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
        out = {"op": "modify_cost", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 0)}
        if node.get("operation"):
            out["operation"] = node["operation"]
        if node.get("duration"):
            out["duration"] = node["duration"]
        return out
    if kind == "set_cost":
        out = {"op": "set_cost", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 0)}
        if node.get("duration"):
            out["duration"] = node["duration"]
        return out
    if kind == "set_attacks":
        return {"op": "set_attacks", "target": node.get("target", {"scope": "self"}), "amount": node.get("amount", 1)}
    if kind == "reanimate":
        out = {"op": "reanimate", "cost": node.get("cost", 0)}
        if node.get("count") is not None:
            out["count"] = node["count"]
        if isinstance(node.get("filters"), dict):
            out["filters"] = dict(node["filters"])
        return out
    if kind == "spellboost":
        return {"op": "spellboost", "target": node.get("target", {"scope": "any"}), "count": node.get("count", 1)}
    if kind == "discard":
        return {"op": "discard", "target": node.get("target", {"scope": "any", "selection": "chosen"})}
    if kind == "invoke":
        return {"op": "invoke", "target": node.get("target", {"scope": "self"})}
    if kind == "gain_crest":
        card_id = _resolve_card_name(name_index, str(node.get("source_card_name", "")).removeprefix("Crest:"))
        if not card_id:
            return None
        target = node.get("target", {})
        player = "enemy" if isinstance(target, dict) and target.get("scope") == "enemy_leader" else "ally"
        return {"op": "gain_crest", "card_id": card_id, "player": player}
    if kind == "banish":
        return {"op": "banish", "target": node.get("target", {"scope": "any"})}
    if kind == "modify_crest":
        out = {"op": "modify_crest", "target": {"scope": "any", "selection": "all" if node.get("selection") == "all" else "chosen", "filters": {"zone": "crests"}}, "amount": node.get("amount", 0)}
        if node.get("source_card_name"):
            card_id = _resolve_card_name(name_index, str(node["source_card_name"]).removeprefix("Crest:"))
            if not card_id:
                return None
            out["crest_card_id"] = card_id
        return out
    if kind == "destroy_crest":
        return {"op": "destroy_crest", "target": node.get("target", {"scope": "any", "filters": {"zone": "crests"}})}
    if kind == "transform":
        out = {"op": "transform", "target": node.get("target", {"scope": "any"})}
        if node.get("source_card_name"):
            card_id = _resolve_card_name(name_index, node.get("source_card_name"))
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
        if node.get("field"):
            out["field"] = node["field"]
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
    if kind == "grant_resource_ability":
        nested = node.get("ability")
        if not isinstance(nested, dict) or not nested.get("trigger"):
            return None
        nested_effects = [effect(item, name_index) for item in nested.get("effects", []) if isinstance(item, dict)]
        nested_effects = [item for item in nested_effects if item is not None]
        if not nested_effects:
            return None
        return {
            "op": "grant_resource_ability",
            "resource": node.get("resource", "faith"),
            "ability": {"trigger": nested["trigger"], "effects": nested_effects},
        }
    if kind in ("summon", "add_to_hand"):
        card_id = _resolve_card_name(name_index, node.get("source_card_name"))
        selector = node.get("resource_selector") or node.get("source")
        # Dynamic selectors (random cards from a zone, or a historical
        # entity-copy pool) intentionally have no single catalog card id.
        if not card_id and not isinstance(selector, dict):
            return None
        destination = node.get("target_zone") or node.get("destination")
        if isinstance(selector, dict):
            out = {"op": kind, "count": node.get("count", 1), "resource_selector": selector}
            if destination:
                out["destination"] = destination
            for field in ("copy_mode", "preserve_state", "reveal", "cost_delta"):
                if field in node:
                    out[field] = node[field]
            return out
        if kind == "add_to_hand" and destination and destination != "hand":
            return {"op": "add_to_zone", "card_id": card_id, "count": node.get("count", 1), "destination": destination}
        out = {"op": kind, "card_id": card_id, "count": node.get("count", 1)}
        if node.get("target"):
            out["target"] = node["target"]
        return out
    if kind == "copy":
        source = node.get("source") or node.get("resource_selector")
        if not isinstance(source, dict):
            return None
        out = {
            "op": "copy",
            "source": source,
            "destination": node.get("destination", "field"),
            "count": node.get("count", 1),
        }
        for field in ("copy_mode", "preserve_state", "reveal", "cost_delta"):
            if field in node:
                out[field] = node[field]
        return out
    if kind == "destroy":
        out = {"op": "destroy", "target": node.get("target", {"scope": "any"})}
        source = str(node.get("count_source", "")).lower()
        if source:
            # Keep dynamic X as a value expression rather than an opaque
            # parser annotation. The runtime can then decide whether it is
            # implemented; it must never silently treat it as zero.
            if "minus" in source and "enemy" in source and "allied" in source:
                out["count"] = {"op": "sub", "args": ["var:enemy_board_count", "var:ally_board_count"]}
            elif "enemy" in source and "number" in source:
                out["count"] = "var:enemy_board_count"
            else:
                out["count"] = "var:X"
        return out
    if kind == "auto_evolve":
        return {"op": "auto_evolve", "target": node.get("target", {"scope": "self"}), "evolution_kind": node.get("evolution_kind", "normal")}
    if kind == "replicate_ability":
        return {"op": "replicate_ability", "trigger": node.get("trigger", "on_fanfare")}
    if kind == "grant_keyword":
        return {"op": "grant_keyword", "keyword": node.get("keyword", ""), "target": node.get("target", {"scope": "self"})}
    if kind == "remove_abilities":
        return {"op": "remove_abilities", "target": node.get("target", {"scope": "any"})}
    if kind == "modify_damage_taken":
        out = {
            "op": "modify_damage_taken",
            "target": node.get("target", {"scope": "enemy_leader"}),
            "amount": node.get("amount", 0),
        }
        if node.get("duration"):
            out["duration"] = node["duration"]
        return out
    if kind == "grant_status":
        out = {"op": "gain_status", "status": node.get("status", ""), "duration": node.get("duration", "permanent"), "target": node.get("target", {"scope": "self"})}
        nested = node.get("ability")
        if isinstance(nested, dict) and nested.get("trigger"):
            nested_effects = [effect(item, name_index) for item in nested.get("effects", []) if isinstance(item, dict)]
            nested_effects = [item for item in nested_effects if item is not None]
            if nested_effects:
                out["ability"] = {"trigger": nested["trigger"], "effects": nested_effects}
        return out
    if kind == "set_stat":
        return {"op": "set_stat", "stat": node.get("stat", "life"), "amount": node.get("amount", 0), "target": node.get("target", {"scope": "self"})}
    if kind == "modify_previous_effect":
        return {"op": "modify_previous_effect", "field": node.get("field", "amount"), "value": node.get("value")}
    if kind == "replace_deck":
        return {"op": "replace_deck", "replacement": node.get("replacement", "")}
    return None


def compile_card(card_id: str, ast_card: dict[str, Any], catalog_card: dict[str, Any], name_index: dict[str, int] | None = None) -> dict[str, Any]:
    abilities_by_mode: dict[str, list[dict[str, Any]]] = {"normal": [], "enhance": [], "accelerate": [], "crystallize": []}
    static_keywords = set()
    variables: dict[str, Any] = {}
    fusion_configs: list[dict[str, Any]] = []
    countdown_initial: int | None = None
    unparsed = []
    unsupported = False
    clauses = list(ast_card.get("abilities", []))
    # Compile a lightweight index first.  Mode-specific clauses are often
    # serialized before their base clause (and Super-Evolve uses a different
    # skill index), so a one-pass “previous effect” lookup is insufficient.
    # The index is only used to resolve explicit replacement relations; the
    # normal loop below still owns classification, conditions, and output.
    candidate_records: list[dict[str, Any]] = []
    for candidate_clause in clauses:
        candidate_nodes = [
            node for node in candidate_clause.get("effects", [])
            if isinstance(node, dict)
            and node.get("kind") not in ("fusion_config", "countdown_config")
            and not (node.get("kind") == "grant_keyword" and node.get("keyword") in candidate_clause.get("static_keywords", []))
        ]
        candidate_effects = [effect(node, name_index) for node in candidate_nodes]
        candidate_records.append({"clause": candidate_clause, "effects": [item for item in candidate_effects if item is not None]})
    # Compile the paired bilingual ability once. The legacy per-language
    # clauses remain in the AST solely for audit/source tracing.
    for clause_index, clause in enumerate(clauses):
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
        classification_needs_audit = classification != "matched"
        if classification != "matched":
            unsupported = True
        mode = clause.get("mode") or "normal"
        special_condition = None
        if mode == "skybound_art":
            special_condition = {"state": "skybound_art", "cmp": "gte", "value": 1}
            mode = "normal"
        elif mode == "super_skybound_art":
            special_condition = {"state": "super_skybound_art", "cmp": "gte", "value": 1}
            mode = "normal"
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
                if classification_needs_audit:
                    sources = clause.get("source_clause", {})
                    if isinstance(sources, dict):
                        primary = ast_card.get("primary_language", "eng")
                        source_text = sources.get(primary) or sources.get("eng") or sources.get("chs")
                        if source_text:
                            unparsed.append(source_text)
                    unparsed.extend(clause.get("unparsed_clauses", []))
                continue
            clause = dict(clause)
            clause["trigger"] = "on_play"
            clause["effects"] = non_keyword_nodes
        source_nodes = [node for node in clause.get("effects", []) if isinstance(node, dict) and node.get("kind") not in ("fusion_config", "countdown_config") and not (node.get("kind") == "grant_keyword" and node.get("keyword") in clause.get("static_keywords", []))]
        raw_effects = [effect(node, name_index) for node in source_nodes]
        effects = [x for x in raw_effects if x is not None]
        if classification_needs_audit:
            sources = clause.get("source_clause", {})
            source_text = None
            if isinstance(sources, dict):
                primary = ast_card.get("primary_language", "eng")
                source_text = sources.get(primary) or sources.get("eng") or sources.get("chs")
            redundant_translation = _is_redundant_translation_source(clause, source_text, source_nodes, effects)
            if source_text and not redundant_translation:
                unparsed.append(source_text)
            # A clause-level marker is meaningful unless it is exactly the
            # redundant Chinese source sentence covered by all compiled nodes.
            for marker in clause.get("unparsed_clauses", []):
                if not (redundant_translation and marker == source_text):
                    unparsed.append(marker)
        elif clause.get("unparsed_clauses"):
            # A parser can understand the immediate portion of a clause but
            # still retain an explicit nested/deferred remainder (for
            # example, a Last Words body).  Matched bilingual effects must
            # not erase that marker; keep the compiled immediate effects but
            # downgrade the rule to partial at the contract boundary.
            unsupported = True
            unparsed.extend(clause.get("unparsed_clauses", []))
        if len(effects) != len(source_nodes):
            unsupported = True
            unparsed.append(f"unresolved_card_reference:{clause.get('ability_id', card_id)}")
        effects, unresolved_modifier = _resolve_previous_modifiers(clause, effects, candidate_records)
        if unresolved_modifier:
            unsupported = True
            unparsed.append(f"unresolved_previous_effect:{clause.get('ability_id', card_id)}")
        if not effects and classification == "matched":
            continue
        if classification != "matched" or any(node.get("kind") not in {"damage", "heal", "repeat", "conditional", "mode_choice", "activate_all_mode_choices", "progressive_sequence", "draw", "buff", "recover_pp", "modify_cost", "set_cost", "set_attacks", "reanimate", "spellboost", "discard", "invoke", "gain_crest", "banish", "modify_crest", "destroy_crest", "transform", "fusion_config", "countdown_config", "return_to_hand", "return_to_deck", "modify_resource", "modify_counter", "consume_resource", "grant_resource_ability", "summon", "add_to_hand", "copy", "destroy", "auto_evolve", "replicate_ability", "grant_keyword", "grant_status", "remove_abilities", "modify_damage_taken", "set_stat", "modify_previous_effect", "replace_deck"} for node in clause.get("effects", []) if isinstance(node, dict) and not (node.get("kind") == "grant_keyword" and node.get("keyword") in clause.get("static_keywords", []))):
            unsupported = True
        if effects:
            ability = {"trigger": ("on_play" if mode_selection else clause.get("trigger", "on_play")), "effects": effects}
            if clause.get("trigger") == "on_engage" and clause.get("mode_cost") is not None:
                ability["cost"] = clause["mode_cost"]
            conditions = list(clause.get("conditions", []))
            if special_condition:
                conditions.append(special_condition)
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
    output = {"schema_version": 2, "catalog_version": 1, "game_version": catalog.get("game_version", ""), "rules": rules}
    # Reports are build artifacts too: record logical filenames instead of
    # absolute checkout/temp paths so repeated builds remain byte-identical.
    report = build_coverage_report(
        catalog,
        output,
        source_catalog=args.catalog.name,
        source_rules=args.output.name,
        phase="compile",
    )
    report["source_ast"] = args.ast.name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(rules)} rules)")
    print(f"wrote {args.report} ({report['support']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
