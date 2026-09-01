"""Semantic validation against the CardRules operation support matrix."""
from __future__ import annotations

from typing import Any, Mapping


def iter_effects(effects: Any):
    for item in effects if isinstance(effects, list) else ():
        if not isinstance(item, Mapping):
            continue
        yield item
        yield from iter_effects(item.get("effects", []))
        yield from iter_effects(item.get("else_effects", []))
        for branch in item.get("choices", []):
            if isinstance(branch, Mapping):
                yield from iter_effects(branch.get("effects", []))


def validate_support(instance: Mapping[str, Any], matrix: Mapping[str, Any]) -> list[str]:
    statuses = matrix.get("operations", {})
    errors = []
    for key, rule in (instance.get("rules", {}) or {}).items():
        if not isinstance(rule, Mapping):
            continue
        rule_support = rule.get("support")
        for mode in rule.get("modes", []) or []:
            for ability in mode.get("abilities", []) if isinstance(mode, Mapping) else ():
                for item in iter_effects(ability.get("effects", []) if isinstance(ability, Mapping) else []):
                    op = item.get("op")
                    status = statuses.get(op)
                    if status is None:
                        errors.append(f"rules.{key}: operation {op!r} is absent from support matrix")
                    elif rule_support == "verified" and status != "implemented":
                        errors.append(f"rules.{key}: verified rule uses {op!r} marked {status}")
    return errors
