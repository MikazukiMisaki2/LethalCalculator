"""Semantic validation for the CardRules executable contract.

JSON Schema can validate the shape of a rule, but it cannot check references
against the current card catalog or keep the vocabulary used by generated
rules in sync with the interpreter.  The helpers in this module are the
semantic half of the Step 7 gate.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_DEFAULT_TRIGGERS = frozenset(
    {
        "on_play",
        "on_fanfare",
        "on_invoke",
        "on_evolve",
        "on_super_evolve",
        "on_ally_follower_evolve",
        "on_ally_follower_super_evolve",
        "on_enemy_follower_super_evolve",
        "on_attack",
        "on_clash",
        "on_survive_damage",
        "on_follower_attack",
        "on_leader_attack",
        "on_damage",
        "on_summon",
        "on_ally_follower_summon",
        "on_destroy",
        "on_ally_amulet_destroy",
        "on_last_word",
        "on_turn_start",
        "on_turn_end",
        "on_opponent_turn_end",
        "on_mode_selected",
        "on_card_play",
        "on_spell_play",
        "on_spellboost",
        "on_engage",
        "on_draw",
        "on_discard",
        "on_crest_gain",
        "on_crest_countdown",
        "on_crest",
    }
)
_DEFAULT_RESOURCES = frozenset(
    {
        "faith",
        "cemetery",
        "earth_sigil",
        "rally",
        "play_count",
        "pp",
        "max_pp",
        "extra_pp",
        "ep",
        "sep",
        "skybound_art",
        "super_skybound_art",
    }
)
_DEFAULT_KEYWORDS = frozenset(
    {
        "ambush",
        "aura",
        "bane",
        "barrier",
        "drain",
        "earth_sigil",
        "effect_indestructible",
        "intimidate",
        "rush",
        "storm",
        "unplayable",
        "ward",
    }
)
_CARD_REFERENCE_KEYS = frozenset(
    {
        "card_id",
        "card_ids",
        "crest_card_id",
        "crest_card_ids",
        "source_card_id",
        "source_card_ids",
        "target_card_id",
        "target_card_ids",
        "replacement_card_id",
        "replacement_card_ids",
    }
)


def iter_effects(effects: Any):
    for item in effects if isinstance(effects, list) else ():
        if not isinstance(item, Mapping):
            continue
        yield item
        yield from iter_effects(item.get("effects", []))
        yield from iter_effects(item.get("else_effects", []))
        ability = item.get("ability")
        if isinstance(ability, Mapping):
            yield from iter_effects(ability.get("effects", []))
        for branch in item.get("choices", []):
            if isinstance(branch, Mapping):
                yield from iter_effects(branch.get("effects", []))
        for step in item.get("steps", []):
            if isinstance(step, Mapping):
                yield from iter_effects(step.get("effects", []))


def _iter_abilities(instance: Mapping[str, Any]):
    """Yield abilities from rules and resource instances exactly once."""
    for rule in (instance.get("rules", {}) or {}).values():
        if not isinstance(rule, Mapping):
            continue
        for mode in rule.get("modes", []) or []:
            if not isinstance(mode, Mapping):
                continue
            for ability in mode.get("abilities", []) or []:
                if isinstance(ability, Mapping):
                    yield ability
    resources = instance.get("resources", {})
    if isinstance(resources, Mapping):
        for resource in resources.values():
            if not isinstance(resource, Mapping):
                continue
            for ability in resource.get("abilities", []) or []:
                if isinstance(ability, Mapping):
                    yield ability
            for resource_instance in resource.get("instances", []) or []:
                if not isinstance(resource_instance, Mapping):
                    continue
                for ability in resource_instance.get("abilities", []) or []:
                    if isinstance(ability, Mapping):
                        yield ability


def validate_support(instance: Mapping[str, Any], matrix: Mapping[str, Any]) -> list[str]:
    statuses = matrix.get("operations", {})
    keyword_statuses = matrix.get("keywords", {})
    errors = []
    for key, rule in (instance.get("rules", {}) or {}).items():
        if not isinstance(rule, Mapping):
            continue
        rule_support = rule.get("support")
        if rule_support == "verified":
            for keyword in rule.get("static_keywords", []) or []:
                status = keyword_statuses.get(keyword) if isinstance(keyword_statuses, Mapping) else None
                if status is None:
                    errors.append(f"rules.{key}: keyword {keyword!r} is absent from support matrix")
                elif status != "implemented":
                    errors.append(f"rules.{key}: verified rule uses keyword {keyword!r} marked {status}")
        for ability in _iter_rule_abilities(rule):
            for item in iter_effects(ability.get("effects", [])):
                op = item.get("op")
                status = statuses.get(op)
                if status is None:
                    errors.append(f"rules.{key}: operation {op!r} is absent from support matrix")
                elif rule_support == "verified" and status != "implemented":
                    errors.append(f"rules.{key}: verified rule uses {op!r} marked {status}")
                keyword = item.get("keyword")
                if rule_support == "verified" and keyword is not None:
                    keyword_status = keyword_statuses.get(keyword) if isinstance(keyword_statuses, Mapping) else None
                    if keyword_status is None:
                        errors.append(f"rules.{key}: keyword {keyword!r} is absent from support matrix")
                    elif keyword_status != "implemented":
                        errors.append(f"rules.{key}: verified rule uses keyword {keyword!r} marked {keyword_status}")
    # Resource listeners are part of the same executable contract even though
    # they live outside ``rules.<card>.modes`` in the v2 document.
    for ability in _iter_resource_abilities(instance):
        for item in iter_effects(ability.get("effects", [])):
            op = item.get("op")
            status = statuses.get(op)
            if status is None:
                errors.append(f"resources: operation {op!r} is absent from support matrix")
    return errors


def _iter_rule_abilities(rule: Mapping[str, Any]):
    for mode in rule.get("modes", []) or []:
        if not isinstance(mode, Mapping):
            continue
        for ability in mode.get("abilities", []) or []:
            if isinstance(ability, Mapping):
                yield ability


def _iter_resource_abilities(instance: Mapping[str, Any]):
    resources = instance.get("resources", {})
    if not isinstance(resources, Mapping):
        return
    for resource in resources.values():
        if not isinstance(resource, Mapping):
            continue
        for ability in resource.get("abilities", []) or []:
            if isinstance(ability, Mapping):
                yield ability
        for resource_instance in resource.get("instances", []) or []:
            if not isinstance(resource_instance, Mapping):
                continue
            for ability in resource_instance.get("abilities", []) or []:
                if isinstance(ability, Mapping):
                    yield ability


def _schema_enum(schema: Mapping[str, Any], *path: str) -> set[str] | None:
    current: Any = schema
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if isinstance(current, list) and all(isinstance(item, str) for item in current):
        return set(current)
    return None


def _contract_vocab(
    schema: Mapping[str, Any] | None,
    matrix: Mapping[str, Any] | None,
) -> tuple[set[str], set[str], set[str], set[str]]:
    schema = schema or {}
    matrix = matrix or {}
    triggers = _schema_enum(schema, "$defs", "trigger", "enum") or set(_DEFAULT_TRIGGERS)
    resources = _schema_enum(schema, "$defs", "resource_name", "enum") or set(_DEFAULT_RESOURCES)
    keywords = set((matrix.get("keywords") or {}).keys()) if isinstance(matrix.get("keywords"), Mapping) else set()
    keywords = keywords or _schema_enum(schema, "$defs", "keyword", "enum") or set(_DEFAULT_KEYWORDS)
    # ``condition`` is a oneOf; its state enum is in the fourth branch.  Keep
    # the extraction defensive so a future schema revision can still use the
    # fallback set without crashing the validator.
    state_values: set[str] = set()
    condition_branches = (schema.get("$defs", {}) or {}).get("condition", {}).get("oneOf", []) if isinstance(schema, Mapping) else []
    for branch in condition_branches if isinstance(condition_branches, list) else []:
        candidate = branch.get("properties", {}).get("state", {}).get("enum") if isinstance(branch, Mapping) else None
        if isinstance(candidate, list):
            state_values.update(value for value in candidate if isinstance(value, str))
    if not state_values:
        state_values = {"variable"}
    return triggers, resources, keywords, state_values


def _path_join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _iter_id_values(value: Any, path: str):
    if isinstance(value, int) and not isinstance(value, bool):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_id_values(item, _path_join(path, index))


def validate_contract(
    instance: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
    matrix: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate executable references and vocabularies against the catalog.

    This intentionally runs after JSON Schema validation.  Structural errors
    are therefore reported by jsonschema, while this function focuses on
    cross-document invariants that JSON Schema cannot express.
    """
    triggers, resources, keywords, states = _contract_vocab(schema, matrix)
    catalog_cards = catalog.get("cards", {}) if isinstance(catalog, Mapping) else {}
    catalog_ids = {str(key) for key in catalog_cards} if isinstance(catalog_cards, Mapping) else set()
    errors: list[str] = []
    rules = instance.get("rules", {}) if isinstance(instance, Mapping) else {}
    if not isinstance(rules, Mapping):
        return ["rules must be an object"]

    if schema is not None and matrix is not None:
        schema_operations = _schema_enum(schema, "$defs", "effect", "properties", "op", "enum") or set()
        matrix_operations = matrix.get("operations", {})
        if isinstance(matrix_operations, Mapping):
            missing_operations = sorted(schema_operations - set(matrix_operations))
            if missing_operations:
                errors.append(f"support matrix is missing schema operations: {', '.join(missing_operations)}")
            invalid_statuses = sorted(
                f"{name}={status}"
                for name, status in matrix_operations.items()
                if status not in set(matrix.get("status_values", ()))
            )
            if invalid_statuses:
                errors.append(f"support matrix has invalid operation statuses: {', '.join(invalid_statuses)}")
        schema_keywords = _schema_enum(schema, "$defs", "keyword", "enum") or set()
        matrix_keywords = matrix.get("keywords", {})
        if isinstance(matrix_keywords, Mapping):
            missing_keywords = sorted(schema_keywords - set(matrix_keywords))
            if missing_keywords:
                errors.append(f"support matrix is missing schema keywords: {', '.join(missing_keywords)}")
            invalid_keyword_statuses = sorted(
                f"{name}={status}"
                for name, status in matrix_keywords.items()
                if status not in set(matrix.get("status_values", ()))
            )
            if invalid_keyword_statuses:
                errors.append(f"support matrix has invalid keyword statuses: {', '.join(invalid_keyword_statuses)}")

    for rule_key, rule in rules.items():
        rule_path = f"rules.{rule_key}"
        if not isinstance(rule, Mapping):
            continue
        card_id = rule.get("card_id")
        if isinstance(card_id, int) and not isinstance(card_id, bool):
            if str(card_id) != str(rule_key):
                errors.append(f"{rule_path}.card_id={card_id} does not match rule key")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = _path_join(path, key)
                if key == "trigger" and isinstance(child, str) and child not in triggers:
                    errors.append(f"{child_path}: unknown trigger {child!r}")
                elif key == "resource" and isinstance(child, str) and child not in resources:
                    errors.append(f"{child_path}: unknown resource {child!r}")
                elif key == "keyword" and isinstance(child, str) and child not in keywords:
                    errors.append(f"{child_path}: unknown keyword {child!r}")
                elif key == "static_keywords" and isinstance(child, list):
                    for index, keyword in enumerate(child):
                        if isinstance(keyword, str) and keyword not in keywords:
                            errors.append(f"{_path_join(child_path, index)}: unknown keyword {keyword!r}")
                elif key == "state" and isinstance(child, str) and child not in states:
                    errors.append(f"{child_path}: unknown condition state {child!r}")
                if isinstance(key, str) and (key in _CARD_REFERENCE_KEYS or key.endswith("_card_id") or key.endswith("_card_ids")):
                    for id_path, referenced_id in _iter_id_values(child, child_path):
                        if str(referenced_id) not in catalog_ids:
                            errors.append(f"{id_path}: card id {referenced_id} is absent from Catalog")
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, _path_join(path, index))

    walk(instance, "")
    if matrix is not None:
        errors.extend(validate_support(instance, matrix))
    return errors


__all__ = ["iter_effects", "validate_support", "validate_contract"]
