"""Small, dependency-free boundary types for the catalog/ruleset contract.

The JSON Schema files in ``schemas/`` are the authoritative format.  These
TypedDicts intentionally describe only the boundary; the solver should not
depend on raw cards.json or on Tracker's internal dataclasses.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


CardType = Literal["follower", "amulet", "countdown_amulet", "spell", "unknown"]
RuleSupport = Literal["verified", "generated", "partial", "unsupported"]
ResourceName = Literal["faith"]
AltModeKind = Literal["faith", "crest", "crystallize", "accelerate", "mode", "unknown"]


class CardCatalog(TypedDict, total=False):
    schema_version: Literal[1]
    game_version: str
    source: dict[str, str]
    type_map: dict[str, int]
    cards: dict[str, dict[str, Any]]


class CardRulesV2(TypedDict, total=False):
    schema_version: Literal[2]
    catalog_version: int
    game_version: str
    rules: dict[str, dict[str, Any]]
    resources: dict[str, Any]


__all__ = ["CardCatalog", "CardRulesV2", "CardType", "RuleSupport", "ResourceName", "AltModeKind"]
