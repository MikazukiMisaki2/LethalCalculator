"""Adapt the structured SWB-RL RuleBook into a CardRules v2 candidate.

The SWB-RL project has a richer, typed rule language than the compact JSON
contract used by the lethal calculator.  This module is intentionally an
*adapter*, not a replacement for the live ruleset: every lossy conversion is
recorded in ``unparsed_clauses`` and the generated rules are marked
``generated`` or ``partial`` (never ``verified``).

The adapter can consume a checkout directly or the read-only artifacts made by
``import_swb_rl.py``.  It never writes ``data/generated/card_rules_v2.json``;
the default output is an isolated candidate under ``data/imported``::

    python adapt_swb_rules.py --source D:/Github/SWB-RL

The output is useful for measuring semantic compatibility and for selecting
small, reviewed migrations.  It must not be loaded by the runtime wholesale.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from import_swb_rl import build_catalog_projection


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = Path(r"D:\Github\SWB-RL")
DEFAULT_OUTPUT = ROOT / "data" / "imported"
DEFAULT_SCHEMA = ROOT / "schemas" / "card_rules_v2.schema.json"
DEFAULT_MATRIX = ROOT / "schemas" / "card_rules_v2_support.json"

SCHEMA_VERSION = 2
RULESET_REVISION = 3

CARD_TYPE_BY_ID = {1: "follower", 2: "amulet", 3: "spell", 4: "spell"}
TRIBE_ALIASES = {
    2: "officer",
    3: "luminous",
    4: "levin",
    5: "pixie",
    6: "departed",
    8: "earth sigil",
    11: "mysteria",
    12: "golem",
    13: "shikigami",
    14: "artifact",
    15: "puppetry",
    17: "marine",
    18: "loot",
    19: "encroacher",
    20: "anathema",
}

CARD_TYPE_ALIASES = {
    "随从": "follower",
    "follower": "follower",
    "followers": "follower",
    "护符": "amulet",
    "魔法阵": "amulet",
    "amulet": "amulet",
    "amulets": "amulet",
    "倒数护符": "countdown_amulet",
    "countdown_amulet": "countdown_amulet",
    "法术": "spell",
    "spell": "spell",
    "spells": "spell",
    "主战者": "leader",
    "leader": "leader",
    "card": "field_card",
    "cards": "field_card",
    "卡牌": "field_card",
    "战场": "field_card",
    "field": "field_card",
}

KEYWORD_ALIASES = {
    "疾驰": "storm",
    "storm": "storm",
    "突进": "rush",
    "rush": "rush",
    "守护": "ward",
    "ward": "ward",
    "必杀": "bane",
    "毁灭": "bane",
    "bane": "bane",
    "吸血": "drain",
    "虹吸": "drain",
    "drain": "drain",
    "潜行": "ambush",
    "突袭": "ambush",
    "ambush": "ambush",
    "屏障": "barrier",
    "barrier": "barrier",
    "威吓": "intimidate",
    "威慑": "intimidate",
    "intimidate": "intimidate",
    "无法被效果破坏": "effect_indestructible",
    "效果破坏免疫": "effect_indestructible",
    "effect_indestructible": "effect_indestructible",
    "无法使用": "unplayable",
    "unplayable": "unplayable",
    "土之印": "earth_sigil",
    "earth sigil": "earth_sigil",
    "灵气": "aura",
    "aura": "aura",
}

# Keep this list in sync with EventInterpreter's confirmed combat keyword
# subset.  Schema-valid keywords such as Barrier, Aura and Earth Sigil are
# still emitted in the candidate for review, but they cannot be classified as
# generated until the runtime models their state transitions.
RUNTIME_KEYWORDS = frozenset({"storm", "rush", "ward", "bane", "drain", "ambush"})

TRIGGER_ALIASES = {
    "play": "on_play",
    "fanfare": "on_fanfare",
    "last_words": "on_last_word",
    "evolve": "on_evolve",
    "self_evolved": "on_evolve",
    "super_evolve": "on_super_evolve",
    "self_super_evolved": "on_super_evolve",
    "attack": "on_attack",
    "clash": "on_clash",
    "turn_start": "on_turn_start",
    "turn_end": "on_turn_end",
    "invoke": "on_invoke",
    "activate": "on_engage",
    "discarded": "on_discard",
}

EVENT_TRIGGER_ALIASES = {
    "follower_summoned": "on_ally_follower_summon",
    "follower_evolved": "on_ally_follower_evolve",
    "follower_super_evolved": "on_ally_follower_super_evolve",
    "card_played": "on_card_play",
    "spellboosted": "on_spellboost",
    "card_drawn": "on_draw",
    "follower_destroyed": "on_destroy",
    "follower_damaged_survived": "on_survive_damage",
    "turn_ended": "on_turn_end",
    "turn_end": "on_turn_end",
    "amulet_activated": "on_engage",
    "entity_left_play": "on_destroy",
}

FAITH_TRIGGER_ALIASES = {
    "follower_evolved": "on_ally_follower_evolve",
    "follower_super_evolved": "on_ally_follower_super_evolve",
    "amulet_destroyed": "on_ally_amulet_destroy",
    "card_enhanced": "on_card_play",
    "mode_selected": "on_mode_selected",
}

SOURCE_SECTIONS = (
    "rules",
    "passives",
    "listeners",
    "emblems",
    "activations",
    "union_bursts",
    "fusions",
    "faiths",
    "invocations",
    "intrinsic_keywords",
    "vanilla_cards",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _as_int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _canonical_card_type(value: Any) -> str | None:
    if isinstance(value, (list, tuple, set)):
        mapped = [_canonical_card_type(item) for item in value]
        return [item for item in mapped if item]  # type: ignore[return-value]
    if value is None:
        return None
    text = str(value).strip().casefold()
    return CARD_TYPE_ALIASES.get(text, CARD_TYPE_ALIASES.get(str(value).strip()))


def _canonical_keyword(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return KEYWORD_ALIASES.get(text)


def _runtime_keyword(value: Any, ctx: _AdapterContext, *, category: str = "keyword") -> str | None:
    """Canonicalize a keyword and mark schema-only keywords as partial."""

    canonical = _canonical_keyword(value)
    if canonical is None:
        return None
    if canonical not in RUNTIME_KEYWORDS:
        ctx.gap(category, f"runtime:{canonical}")
    return canonical


def _canonical_tribe(value: Any, tribe_id: Any = None) -> Any:
    ident = _as_int(tribe_id)
    if ident in TRIBE_ALIASES:
        return TRIBE_ALIASES[ident]
    return value


class _AdapterContext:
    """Per-build state, including loss reports keyed by card id."""

    def __init__(self, catalog: Mapping[str, Any]):
        cards = catalog.get("cards", {}) if isinstance(catalog, Mapping) else {}
        self.catalog = catalog
        self.cards: dict[str, Mapping[str, Any]] = {
            str(key): value for key, value in cards.items() if isinstance(value, Mapping)
        }
        self.card_ids = set(self.cards)
        self.card_gaps: dict[int, set[str]] = defaultdict(set)
        self.gaps: Counter[str] = Counter()
        self.mapped_operations: Counter[str] = Counter()
        self.mapped_triggers: Counter[str] = Counter()
        self.source_files: dict[int, set[str]] = defaultdict(set)
        self.current_card_id: int | None = None

    def gap(self, category: str, detail: str, card_id: int | None = None) -> None:
        marker = f"swb:{category}:{detail}"
        self.gaps[marker] += 1
        target = self.current_card_id if card_id is None else card_id
        if target is not None:
            self.card_gaps[int(target)].add(marker)

    def mapped(self, kind: str) -> None:
        self.mapped_operations[kind] += 1

    def has_card(self, card_id: Any) -> bool:
        ident = _as_int(card_id)
        return ident is not None and str(ident) in self.card_ids

    def card(self, card_id: Any) -> Mapping[str, Any]:
        return self.cards.get(str(_as_int(card_id) or 0), {})


def _load_rule_files(source_root: Path) -> dict[str, Any]:
    rules_dir = source_root / "data" / "rules"
    if not rules_dir.exists():
        imported = ROOT / "data" / "imported" / "swb_rulebook_raw.json"
        if imported.exists():
            payload = json.loads(imported.read_text(encoding="utf-8"))
            files = payload.get("files", {})
            if isinstance(files, Mapping):
                return {str(key): value for key, value in sorted(files.items())}
        raise FileNotFoundError(f"SWB-RL rules directory not found: {rules_dir}")
    output: dict[str, Any] = {}
    for path in sorted(rules_dir.glob("*.json")):
        output[path.name] = json.loads(path.read_text(encoding="utf-8"))
    if not output:
        raise ValueError(f"No rule JSON files found in {rules_dir}")
    return output


def _load_catalog(source_root: Path) -> dict[str, Any]:
    database = source_root / "data" / "cards.sqlite3"
    if database.exists():
        catalog, _metadata = build_catalog_projection(database)
        return catalog
    imported = ROOT / "data" / "imported" / "swb_catalog_projection.json"
    if imported.exists():
        return json.loads(imported.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"SWB-RL catalog database not found: {database}")


def _raw_entries(files: Mapping[str, Any], section: str) -> list[tuple[str, int, Mapping[str, Any]]]:
    entries: list[tuple[str, int, Mapping[str, Any]]] = []
    for filename in sorted(files):
        payload = files[filename]
        if not isinstance(payload, Mapping):
            continue
        values = payload.get(section, [])
        if isinstance(values, Mapping):
            values = list(values.values())
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if isinstance(item, Mapping):
                entries.append((filename, index, item))
    return entries


def _source_id(entry: Mapping[str, Any]) -> int | None:
    return _as_int(entry.get("card_id", entry.get("source_card_id")))


def _condition_value(raw: Any, ctx: _AdapterContext) -> Any:
    return _value(raw, ctx)


def _negate_value(value: Any, ctx: _AdapterContext) -> Any:
    """Return a v2-compatible negative value when it is statically known.

    The source RuleBook frequently represents a subtraction as a positive
    amount plus ``mode=\"subtract\"``.  CardRules v2 has no value-level
    ``negate`` operator that the runtime evaluates, so numeric values must be
    folded here.  A dynamic expression is retained as an explicit unsupported
    value and marks the card partial instead of silently turning subtraction
    into addition.
    """

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return -value
    ctx.gap("expression", "negate")
    return {"op": "swb_expr", "type": "negate", "args": [value]}


def _value(raw: Any, ctx: _AdapterContext) -> Any:
    """Convert SWB-RL ValueExpression JSON into v2 values.

    The v2 interpreter has a small set of stable ``var:`` names.  Other typed
    expressions are retained as an explicit ``swb_expr`` value.  This keeps
    the candidate schema-valid while the accompanying gap prevents it from
    being mistaken for a confirmed runtime rule.
    """

    if isinstance(raw, (int, float, bool, str)) or raw is None:
        return raw
    if not isinstance(raw, Mapping):
        return None
    typ = str(raw.get("type", ""))
    if typ == "constant":
        return _value(raw.get("value", 0), ctx)
    direct = {
        "source_attack": "var:source_attack",
        "controller_hand_count": "var:hand_count",
        "controller_shadows": "var:cemetery",
        "controller_earth_sigils": "var:earth_sigil",
        "controller_emblem_count": "var:crest_count",
        "source_spellboost_count": "var:spellboost_count",
        "distributed_value": "var:distributed_value",
        "source_cost": "var:source_cost",
        "source_health": "var:source_health",
        "source_missing_health": "var:source_missing_health",
        "target_attack": "var:target_attack",
        "target_health": "var:target_health",
        "bound_target_count": "var:target_count",
        "controller_board_count": "var:ally_board_count",
        "opponent_board_count": "var:enemy_board_count",
    }
    if typ in direct:
        # Only the variables already understood by the runtime are safe.  The
        # rest remain useful documentation but are explicitly partial.
        safe = {"source_attack", "controller_hand_count", "controller_shadows", "controller_earth_sigils", "controller_emblem_count"}
        if typ not in safe:
            ctx.gap("expression", typ)
        return direct[typ]
    args = raw.get("values", raw.get("args", []))
    converted = [_value(item, ctx) for item in args] if isinstance(args, list) else []
    ctx.gap("expression", typ or "unknown")
    result: dict[str, Any] = {"op": "swb_expr", "type": typ or "unknown"}
    if converted:
        result["args"] = converted
    if "value" in raw and typ != "constant":
        result["value"] = _value(raw.get("value"), ctx)
    if "binding_key" in raw:
        result["binding_key"] = str(raw.get("binding_key"))
    return result


def _filter_mapping(raw: Any, ctx: _AdapterContext, *, category: str = "filter") -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key, value in raw.items():
        key = str(key)
        if key in {"card_type", "type", "card_type_filter"}:
            mapped = _canonical_card_type(value)
            if mapped is not None:
                output["card_type"] = mapped
            else:
                ctx.gap(category, f"card_type:{value}")
        elif key in {"card_id", "card_id_filter", "deck_card_id_filter"}:
            ident = _as_int(value)
            if ident is not None and ctx.has_card(ident):
                output["card_id"] = ident
            else:
                ctx.gap(category, f"card_id:{value}")
        elif key in {"tribe_id", "tribe_id_filter"}:
            ident = _as_int(value)
            if ident is not None:
                output["tribe_id"] = ident
                output.setdefault("tribe", _canonical_tribe(None, ident))
        elif key in {"tribe_name", "tribe_name_filter"}:
            output["tribe"] = _canonical_tribe(value)
        elif key in {"keyword", "keyword_filter"}:
            mapped = _runtime_keyword(value, ctx, category=category)
            if mapped:
                output["keyword"] = mapped
            else:
                # These two printed keywords are predicates over hidden
                # abilities rather than runtime keyword flags.  Preserve the
                # predicate using the v2 filter vocabulary instead of
                # emitting an invalid `keyword` value.
                if str(value).strip().casefold() in {"谢幕曲", "last words"}:
                    output["has_last_words"] = True
                elif str(value).strip().casefold() in {"魔力增幅", "spellboost", "spell boost"}:
                    output["has_trigger"] = "on_spellboost"
                    ctx.gap(category, "spellboost_filter")
                else:
                    ctx.gap(category, f"keyword:{value}")
        elif key in {"cost_min", "min_cost", "target_cost_min"}:
            output["min_base_cost"] = _value(value, ctx)
        elif key in {"cost_max", "max_cost", "target_cost_max"}:
            output["max_base_cost"] = _value(value, ctx)
        elif key in {"class_id", "class_id_filter"}:
            output["class_id"] = _as_int(value, value)
        elif key in {"class_name", "class_name_filter"}:
            output["class"] = str(value)
        elif key in {"has_last_words", "damaged", "evolved", "super_evolved", "attacked_this_turn"}:
            output[key] = bool(value) if isinstance(value, bool) else value
        elif key in {"side", "zone", "card_name", "distinct_by"}:
            output[key] = value
        elif key.startswith("target_"):
            # Target-specific filters are normalized by _target before this
            # helper is called.  Retain a readable fallback for new fields.
            output[key.removeprefix("target_")] = value
        else:
            output[key] = copy.deepcopy(value)
    return output


def _operation_filters(operation: Mapping[str, Any], ctx: _AdapterContext) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for key, value in operation.items():
        if key.startswith("target_"):
            short = key.removeprefix("target_")
            filters.update(_filter_mapping({short: value}, ctx, category="target_filter"))
    for key in ("hand_filter", "deck_filter", "history_filter"):
        if isinstance(operation.get(key), Mapping):
            filters.update(_filter_mapping(operation[key], ctx, category=key))
    if operation.get("exclude_source"):
        filters["exclude_source"] = True
    if operation.get("exclude_self"):
        filters["exclude_source"] = True
    if operation.get("exclude_attack_target"):
        filters["exclude_attack_target"] = True
    return filters


def _target(raw: Any, ctx: _AdapterContext, *, operation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    operation = operation or {}
    source = str(raw or "self")
    lower = source.casefold()
    mapping: dict[str, tuple[str, str | None, dict[str, Any]]] = {
        "self": ("self", "chosen", {}),
        "event_source": ("trigger_source", "chosen", {}),
        "attack_target": ("trigger_source", "chosen", {}),
        "previous_target": ("previous_target", "chosen", {}),
        "previous_copy": ("previous_copy", "chosen", {}),
        "previous_summon": ("previous_summon", "chosen", {}),
        "previous_add": ("previous_add", "chosen", {}),
        "own_leader": ("ally_leader", "chosen", {}),
        "enemy_leader": ("enemy_leader", "chosen", {}),
        "own_unit": ("ally_follower", "chosen", {"side": "ally", "card_type": "follower"}),
        "enemy_unit": ("enemy_follower", "chosen", {"side": "enemy", "card_type": "follower"}),
        "any_unit": ("any", "chosen", {"card_type": "follower"}),
        "own_unit_or_leader": ("any", "chosen", {"side": "ally", "card_type": ["follower", "leader"]}),
        "enemy_unit_or_leader": ("any", "chosen", {"side": "enemy", "card_type": ["follower", "leader"]}),
        "any_unit_or_leader": ("any", "chosen", {"card_type": ["follower", "leader"]}),
        "own_board": ("any", "all", {"side": "ally", "card_type": ["follower", "amulet"]}),
        "enemy_board": ("any", "all", {"side": "enemy", "card_type": ["follower", "amulet"]}),
        "any_board": ("any", "all", {"card_type": ["follower", "amulet"]}),
        "all_own_board": ("any", "all", {"side": "ally", "card_type": ["follower", "amulet"]}),
        "all_enemy_board": ("any", "all", {"side": "enemy", "card_type": ["follower", "amulet"]}),
        "all_board": ("any", "all", {"card_type": ["follower", "amulet"]}),
        "all_own_units": ("ally_follower", "all", {"side": "ally", "card_type": "follower"}),
        "all_enemy_units": ("enemy_follower", "all", {"side": "enemy", "card_type": "follower"}),
        "all_units": ("any", "all", {"card_type": "follower"}),
        "all_enemy_units_and_leader": ("any", "all", {"side": "enemy", "card_type": ["follower", "leader"]}),
        "all_leaders": ("any", "all", {"card_type": "leader"}),
        # There is no dedicated ally_amulet scope in CardRules v2.  Use the
        # neutral field scope plus an explicit side/type filter; mapping an
        # amulet to ally_follower would make the runtime silently select
        # nothing because follower scopes are type-restricted.
        "all_own_amulets": ("any", "all", {"side": "ally", "card_type": "amulet"}),
        "all_own_emblems": ("any", "all", {"side": "ally", "card_type": "amulet"}),
        "random_own_unit": ("ally_follower", "random", {"side": "ally", "card_type": "follower"}),
        "random_enemy_unit": ("enemy_follower", "random", {"side": "enemy", "card_type": "follower"}),
        "random_any_unit": ("any", "random", {"card_type": "follower"}),
        "random_own_board": ("any", "random", {"side": "ally", "card_type": ["follower", "amulet"]}),
        "random_enemy_unit_or_leader": ("any", "random", {"side": "enemy", "card_type": ["follower", "leader"]}),
        "random_any_unit_or_leader": ("any", "random", {"card_type": ["follower", "leader"]}),
        "own_hand": ("ally_hand", "chosen", {"side": "ally", "zone": "hand"}),
        "all_own_hand": ("ally_hand", "all", {"side": "ally", "zone": "hand"}),
        "random_own_hand": ("ally_hand", "random", {"side": "ally", "zone": "hand"}),
        "enemy_hand": ("enemy_hand", "chosen", {"side": "enemy", "zone": "hand"}),
        "all_enemy_hand": ("enemy_hand", "all", {"side": "enemy", "zone": "hand"}),
        "random_enemy_hand": ("enemy_hand", "random", {"side": "enemy", "zone": "hand"}),
        "hand": ("hand", "chosen", {"zone": "hand"}),
        "own_graveyard_card": ("any", "chosen", {"side": "ally", "zone": "cemetery", "card_type": "follower"}),
        "random_own_graveyard_card": ("any", "random", {"side": "ally", "zone": "cemetery", "card_type": "follower"}),
        "all_own_graveyard_cards": ("any", "all", {"side": "ally", "zone": "cemetery", "card_type": "follower"}),
    }
    if lower.startswith("random_") and lower not in mapping:
        # A future target kind can still retain its random nature even if its
        # exact side/zone is not known by this adapter.
        ctx.gap("target", lower)
        scope, selection, defaults = "any", "random", {}
    else:
        if lower not in mapping:
            ctx.gap("target", lower)
        scope, selection, defaults = mapping.get(lower, ("any", "chosen", {}))
    target: dict[str, Any] = {"scope": scope}
    if selection:
        target["selection"] = selection
    filters = dict(defaults)
    filters.update(_operation_filters(operation, ctx))
    # Self-targeting hand effects (listeners and Spellboost reductions) must
    # carry a zone marker so the runtime resolves the hand entity rather than
    # looking for a board follower.
    if scope == "self" and operation.get("kind") in {"buff_hand_card", "change_cost", "spellboost_hand"}:
        filters.setdefault("zone", "hand")
    if filters:
        target["filters"] = filters
    if operation.get("requires_target") and selection not in {"random", "all"}:
        target["selection"] = "chosen"
    count = operation.get("target_count", operation.get("target_count_expr"))
    if count is not None:
        target["count"] = _value(count, ctx)
    if operation.get("target_key"):
        ctx.gap("target_binding", str(operation["target_key"]))
    extreme = operation.get("candidate_extreme")
    if extreme in {"lowest_health", "lowest_life", "lowest_hp"}:
        target["selection"] = "lowest_life"
    elif extreme in {"highest_attack", "highest_atk"}:
        target["selection"] = "highest_attack"
    elif extreme:
        ctx.gap("target_selection", str(extreme))
    return target


def _conditions(raw: Any, ctx: _AdapterContext) -> dict[str, Any] | None:
    values = _list(raw)
    if not values:
        return None
    converted: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            ctx.gap("condition", "malformed")
            continue
        converted_item = _condition(item, ctx)
        if converted_item is not None:
            converted.append(converted_item)
    if not converted:
        return None
    return converted[0] if len(converted) == 1 else {"all": converted}


def _condition(raw: Mapping[str, Any], ctx: _AdapterContext) -> dict[str, Any] | None:
    typ = str(raw.get("type", ""))
    if typ == "not":
        nested = _list(raw.get("conditions"))
        child = _conditions(nested, ctx)
        return {"not": child} if child else None
    if typ in {"all", "any"}:
        nested = [_condition(item, ctx) for item in _list(raw.get("conditions")) if isinstance(item, Mapping)]
        nested = [item for item in nested if item]
        return {typ: nested}
    value = _condition_value(raw.get("value", True), ctx)
    direct: dict[str, tuple[str, str]] = {
        "controller_board_count_at_least": ("ally_board_count", "gte"),
        "opponent_board_count_at_least": ("enemy_board_count", "gte"),
        "controller_max_mana_at_least": ("max_pp", "gte"),
        "controller_shadows_at_least": ("cemetery", "gte"),
        "controller_earth_sigils_at_least": ("earth_sigil", "gte"),
        "controller_evolutions_this_match_at_least": ("evolved_allies_this_match", "gte"),
        "source_evolved": ("evolved", "eq"),
        "source_super_evolved": ("super_evolved", "eq"),
        "source_cost_equals": ("card_cost", "eq"),
        "controller_follower_attacks_this_turn_at_most": ("attacked_with_follower_this_turn", "lte"),
        "controller_overflow": ("awakening", "eq"),
    }
    if typ in direct:
        state, cmp = direct[typ]
        if typ == "controller_overflow":
            value = True
        return {"state": state, "cmp": cmp, "value": value}
    if typ == "target_card_type_is":
        value = _canonical_card_type(raw.get("card_type")) or raw.get("card_type")
    elif typ == "source_card_type_is":
        value = _canonical_card_type(raw.get("card_type")) or raw.get("card_type")
    elif typ in {"source_has_keyword", "target_has_keyword"}:
        value = _canonical_keyword(raw.get("keyword")) or raw.get("keyword")
    # Preserve an unsupported typed condition as a runtime-false variable
    # gate.  It is safer than dropping the condition and overestimating lethal
    # damage, while still making the source semantics inspectable.
    ctx.gap("condition", typ or "unknown")
    return {"state": "variable", "name": typ or "unknown", "cmp": "eq", "value": value}


def _attach_condition(effect: dict[str, Any], operation: Mapping[str, Any], ctx: _AdapterContext) -> dict[str, Any]:
    condition = _conditions(operation.get("conditions"), ctx)
    if condition is not None:
        effect["condition"] = condition
    return effect


def _nested_operations(raw: Any, ctx: _AdapterContext) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in _list(raw):
        if not isinstance(item, Mapping):
            ctx.gap("operation", "malformed")
            continue
        converted = _operation(item, ctx)
        if converted is not None:
            output.append(converted)
    return output


def _resource_gate(
    resource: str,
    amount: Any,
    nested: list[dict[str, Any]],
) -> dict[str, Any]:
    """Encode a paid RuleBook clause as an executable ordered sequence.

    ``consume_resource.effects`` is legal in the broad v2 schema but the
    current interpreter treats ``consume_resource`` as an atomic payment and
    does not recurse into an optional ``effects`` property.  Put the payment
    first and the dependent payload after it so an insufficient resource
    stops the sequence without dropping the payload on successful payment.
    """

    payment: dict[str, Any] = {"op": "consume_resource", "resource": resource, "amount": amount}
    if not nested:
        return payment
    return {"op": "sequence", "effects": [payment, *nested]}


def _card_ref(raw: Any, ctx: _AdapterContext, *, category: str = "card_reference") -> int | None:
    ident = _as_int(raw)
    if ident is None or not ctx.has_card(ident):
        ctx.gap(category, str(raw))
        return None
    return ident


def _copy_source(raw_target: Any, ctx: _AdapterContext, *, operation: Mapping[str, Any]) -> dict[str, Any]:
    selector = _target(raw_target, ctx, operation=operation)
    source = dict(selector)
    scope = source.get("scope")
    filters = dict(source.get("filters", {})) if isinstance(source.get("filters"), Mapping) else {}
    if scope in {"ally_hand", "enemy_hand", "hand"}:
        source["zone"] = "hand"
    elif scope in {"previous_add"}:
        source["zone"] = "hand"
    elif scope in {"previous_copy", "previous_summon", "previous_target", "self", "trigger_source", "ally_follower", "enemy_follower", "any"}:
        source.setdefault("zone", "field")
    if filters.get("zone") == "cemetery":
        source["zone"] = "destroyed_this_match"
    return source


def _operation(raw: Mapping[str, Any], ctx: _AdapterContext) -> dict[str, Any] | None:
    kind = str(raw.get("kind", ""))
    target = _target(raw.get("target", "self"), ctx, operation=raw)
    amount = _value(raw.get("amount", 1), ctx)
    effect: dict[str, Any] | None = None

    if kind in {"damage_leader", "damage_unit"}:
        effect = {"op": "damage", "target": target, "amount": amount}
    elif kind == "distribute_damage":
        if raw.get("include_leader"):
            target = {"scope": "any", "selection": "all", "allocation": "ordered_split", "filters": {"side": "enemy", "card_type": ["follower", "leader"]}}
        else:
            target["selection"] = "all"
            target["allocation"] = "ordered_split"
        effect = {"op": "damage", "target": target, "amount": amount}
    elif kind in {"heal_leader", "heal_unit"}:
        effect = {"op": "heal", "target": target, "amount": amount}
    elif kind == "heal_unit_and_leader":
        effect = {"op": "sequence", "effects": [{"op": "heal", "target": target, "amount": amount}, {"op": "heal", "target": {"scope": "ally_leader"}, "amount": amount}]}
    elif kind in {"buff_unit", "buff_hand_card"}:
        effect = {"op": "buff", "target": target, "amount": _value(raw.get("amount", 0), ctx), "life": _value(raw.get("secondary_amount", 0), ctx)}
        if raw.get("duration"):
            effect["duration"] = str(raw["duration"])
    elif kind == "draw":
        effect = {"op": "draw", "target": target, "count": amount}
    elif kind == "draw_filtered":
        # v2 Draw accepts the same selector filters; keep the explicit gap so
        # future runtime changes can distinguish this from an unfiltered draw.
        target.setdefault("filters", {}).update(_filter_mapping(raw, ctx, category="draw_filter"))
        effect = {"op": "draw", "target": target, "count": amount}
        ctx.gap("operation", "draw_filtered")
    elif kind == "add_card":
        card_id = _card_ref(raw.get("card_id"), ctx)
        if card_id is not None:
            count = raw.get("amount", 1)
            effect = {"op": "add_to_hand", "card_id": card_id, "count": _value(count, ctx)}
    elif kind == "add_card_to_deck":
        card_id = _card_ref(raw.get("card_id"), ctx)
        if card_id is not None:
            effect = {"op": "add_to_zone", "card_id": card_id, "count": _value(raw.get("amount", 1), ctx), "destination": "deck"}
    elif kind in {"destroy", "banish", "discard", "return_to_hand", "return_to_deck"}:
        effect = {"op": kind, "target": target}
    elif kind == "restore_mana":
        effect = {"op": "recover_pp", "amount": amount}
    elif kind == "restore_evolution_points":
        effect = {"op": "modify_resource", "resource": "ep", "amount": amount}
    elif kind == "restore_super_evolution_points":
        effect = {"op": "modify_resource", "resource": "sep", "amount": amount}
    elif kind == "change_max_mana":
        effect = {"op": "modify_resource", "resource": "max_pp", "amount": amount}
    elif kind == "add_combo":
        effect = {"op": "modify_counter", "field": "count", "delta": amount}
        ctx.gap("operation", "add_combo")
    elif kind == "add_shadows":
        effect = {"op": "modify_resource", "resource": "cemetery", "amount": amount}
    elif kind == "add_earth_sigils":
        effect = {"op": "modify_resource", "resource": "earth_sigil", "amount": amount}
    elif kind == "add_union_burst_gauge":
        effect = {"op": "modify_resource", "resource": "skybound_art", "amount": amount}
    elif kind in {"earth_rite", "necromancy"}:
        effect = _resource_gate(
            "earth_sigil" if kind == "earth_rite" else "cemetery",
            amount,
            _nested_operations(raw.get("operations"), ctx),
        )
    elif kind == "consume_faith":
        effect = _resource_gate("faith", amount, _nested_operations(raw.get("operations"), ctx))
        if raw.get("faith_id"):
            effect["source"] = {"faith_id": str(raw["faith_id"])}
            ctx.gap("faith", "instance_selector")
    elif kind == "grant_faith_ability":
        nested = _nested_operations(raw.get("operations"), ctx)
        trigger = FAITH_TRIGGER_ALIASES.get(str(raw.get("faith_trigger", "")))
        if trigger and nested:
            effect = {"op": "grant_resource_ability", "resource": "faith", "ability": {"trigger": trigger, "effects": nested}}
            effect["source"] = {"faith_id": str(raw.get("faith_id", ""))}
        else:
            ctx.gap("faith", "ability_trigger")
    elif kind == "grant_faith_mode_selection_bonus":
        effect = {"op": "modify_resource", "resource": "faith", "field": "mode_limit", "amount": amount}
        effect["source"] = {"faith_id": str(raw.get("faith_id", ""))}
        ctx.gap("faith", "mode_limit_instance")
    elif kind in {"evolve_unit", "super_evolve_unit"}:
        effect = {"op": "auto_evolve", "target": target, "evolution_kind": "super" if kind == "super_evolve_unit" else "normal"}
    elif kind == "add_keyword":
        keyword = _runtime_keyword(raw.get("keyword"), ctx)
        if keyword:
            effect = {"op": "grant_keyword", "keyword": keyword, "target": target}
        else:
            ctx.gap("keyword", str(raw.get("keyword")))
    elif kind == "add_random_keywords":
        choices: list[dict[str, Any]] = []
        for keyword in _list(raw.get("keywords")):
            canonical = _runtime_keyword(keyword, ctx)
            if canonical:
                choices.append({"label": canonical, "effects": [{"op": "grant_keyword", "keyword": canonical, "target": target}]})
            else:
                ctx.gap("keyword", str(keyword))
        if choices:
            effect = {"op": "random_choice", "choices": choices, "selection_count": amount}
        ctx.gap("operation", "add_random_keywords")
    elif kind == "remove_keyword":
        keyword = _runtime_keyword(raw.get("keyword"), ctx)
        if keyword:
            effect = {"op": "remove_keyword", "keyword": keyword, "target": target}
        else:
            ctx.gap("keyword", str(raw.get("keyword")))
    elif kind == "remove_all_abilities":
        effect = {"op": "remove_abilities", "target": target}
    elif kind == "grant_last_words":
        nested = _nested_operations(raw.get("operations"), ctx)
        if nested:
            effect = {"op": "gain_status", "status": "last words", "duration": "permanent", "target": target, "ability": {"trigger": "on_last_word", "effects": nested}}
        else:
            ctx.gap("nested", "last_words")
    elif kind == "remove_last_words":
        effect = {"op": "gain_status", "status": "last_words_removed", "duration": "permanent", "target": target}
        ctx.gap("operation", "remove_last_words")
    elif kind == "grant_attacks_per_turn":
        effect = {"op": "set_attacks", "target": target, "amount": amount}
        if raw.get("target") not in {None, "self", "event_source"}:
            ctx.gap("operation", "grant_attacks_per_turn_target")
    elif kind == "grant_turn_end_ability":
        nested = _nested_operations(raw.get("operations"), ctx)
        trigger = "on_opponent_turn_end" if raw.get("turn_end_ability_timing") == "opponent_turn" else "on_turn_end"
        effect = {"op": "gain_status", "status": "turn_end_ability", "duration": "permanent", "target": target, "ability": {"trigger": trigger, "effects": nested}}
    elif kind in {"grant_turn_end_destroy", "grant_turn_end_banish"}:
        trigger = "on_opponent_turn_end" if raw.get(f"{kind}_timing") == "opponent_turn" else "on_turn_end"
        nested_op = "destroy" if kind.endswith("destroy") else "banish"
        effect = {"op": "gain_status", "status": kind, "duration": "permanent", "target": target, "ability": {"trigger": trigger, "effects": [{"op": nested_op, "target": {"scope": "self"}}]}}
        ctx.gap("operation", kind)
    elif kind == "grant_effect_destroy_immunity":
        effect = {"op": "gain_status", "status": "effect_indestructible", "duration": "permanent", "target": target}
        ctx.gap("operation", kind)
    elif kind == "add_attack_restriction":
        effect = {"op": "gain_status", "status": str(raw.get("restriction", "cannot_attack")), "duration": str(raw.get("duration", "permanent")), "target": target}
        ctx.gap("operation", kind)
    elif kind == "add_leader_barrier":
        effect = {"op": "gain_status", "status": "barrier", "duration": "permanent", "target": target}
        ctx.gap("operation", kind)
    elif kind == "add_leader_damage_modifier":
        effect = {"op": "modify_damage_taken", "target": target, "amount": amount}
    elif kind == "change_cost":
        mode = str(raw.get("mode", "add"))
        operation = "delta"
        converted_amount = amount
        if mode == "subtract":
            converted_amount = _negate_value(amount, ctx)
        elif mode == "set":
            operation = "set"
        elif mode in {"halve", "halve_round_up"}:
            operation = "halve"
            ctx.gap("operation", "change_cost_rounding")
        elif mode == "double":
            operation = "double"
        effect = {"op": "modify_cost", "target": target, "amount": converted_amount, "operation": operation}
        if raw.get("duration"):
            effect["duration"] = str(raw["duration"])
    elif kind == "change_deck_cost":
        deck_target = {"scope": "any", "selection": "all", "filters": {"side": "ally", "zone": "deck"}}
        mode = str(raw.get("mode", "subtract"))
        operation = "delta"
        converted_amount = amount
        if mode == "subtract":
            converted_amount = _negate_value(amount, ctx)
        elif mode in {"halve", "halve_round_up"}:
            operation = "halve"
        effect = {"op": "modify_cost", "target": deck_target, "amount": converted_amount, "operation": operation}
        ctx.gap("operation", "change_deck_cost")
    elif kind == "select_targets":
        # CardRules v2 expresses a target directly on the consuming effect;
        # there is no standalone “bind target” primitive.  Keep the binding
        # in the report and let following previous_target effects remain
        # partial rather than inventing a zero-damage mutation.
        ctx.gap("operation", "select_targets")
        return None
    elif kind == "copy_to_hand":
        source = _copy_source(raw.get("target", "self"), ctx, operation=raw)
        cost_delta = _value(raw.get("amount", 0), ctx)
        if raw.get("mode") == "subtract":
            cost_delta = _negate_value(cost_delta, ctx)
        effect = {"op": "copy", "source": source, "destination": "hand", "count": 1, "copy_mode": "card", "preserve_state": False, "cost_delta": cost_delta}
        # Tracker does not expose opponent-hand identities.  Keep the source
        # selector for documentation, but prevent this rule from being
        # classified as generated (and therefore from looking confirmed).
        if str(raw.get("target", "")).casefold() in {"enemy_hand", "random_enemy_hand", "all_enemy_hand"}:
            ctx.gap("operation", "hidden_enemy_hand_copy")
    elif kind == "copy_destroyed_followers_to_hand":
        source = {"scope": "any", "zone": "destroyed_this_match", "selection": "random", "filters": _filter_mapping(raw.get("history_filter", {}), ctx, category="history_filter")}
        effect = {"op": "copy", "source": source, "destination": "hand", "count": amount, "copy_mode": "exact", "preserve_state": True}
        ctx.gap("operation", kind)
    elif kind == "copy_leftmost_hand_to_hand":
        effect = {"op": "copy", "source": {"scope": "ally_hand", "zone": "hand", "selection": "chosen"}, "destination": "hand", "count": amount, "copy_mode": "card", "preserve_state": False}
        ctx.gap("target_selection", "leftmost_hand")
    elif kind == "copy_random_enemy_deck_to_hand":
        effect = {"op": "copy", "source": {"scope": "enemy_hand", "zone": "deck", "selection": "random"}, "destination": "hand", "count": amount, "copy_mode": "card", "preserve_state": False}
        ctx.gap("operation", kind)
    elif kind == "summon":
        card_id = _card_ref(raw.get("card_id"), ctx)
        if card_id is not None:
            effect = {"op": "summon", "card_id": card_id, "count": _value(raw.get("amount", 1), ctx)}
            if raw.get("target_key"):
                ctx.gap("target_binding", str(raw["target_key"]))
    elif kind == "summon_copy":
        effect = {"op": "copy", "source": _copy_source(raw.get("target", "self"), ctx, operation=raw), "destination": "field", "count": 1, "copy_mode": "card", "preserve_state": False}
        ctx.gap("operation", "summon_copy_state")
    elif kind == "summon_exact_copy":
        effect = {"op": "copy", "source": _copy_source(raw.get("target", "self"), ctx, operation=raw), "destination": "field", "count": 1, "copy_mode": "exact", "preserve_state": True}
    elif kind == "summon_hand_copy":
        source = _copy_source("own_hand", ctx, operation=raw)
        source["selection"] = "chosen"
        if isinstance(raw.get("hand_filter"), Mapping):
            source.setdefault("filters", {}).update(_filter_mapping(raw["hand_filter"], ctx, category="hand_filter"))
        effect = {"op": "copy", "source": source, "destination": "field", "count": _value(raw.get("target_count", 1), ctx), "copy_mode": "exact", "preserve_state": True}
        if raw.get("requires_full_target_count"):
            ctx.gap("operation", "requires_full_target_count")
    elif kind == "summon_from_hand":
        effect = {"op": "copy", "source": _copy_source("own_hand", ctx, operation=raw), "destination": "field", "count": _value(raw.get("amount", 1), ctx), "copy_mode": "exact", "preserve_state": True}
    elif kind == "summon_from_deck":
        selector = {"zone": "deck", "selection": "random", "filters": _filter_mapping(raw, ctx, category="deck_filter")}
        effect = {"op": "summon", "resource_selector": selector, "count": amount}
    elif kind == "summon_destroyed_amulets":
        selector = {"zone": "destroyed_this_match", "selection": "random", "filters": _filter_mapping(raw.get("history_filter", {}), ctx, category="history_filter")}
        if raw.get("distinct_card_names"):
            selector["distinct_by"] = "card_id"
        effect = {"op": "summon", "resource_selector": selector, "count": amount, "copy_mode": "exact", "preserve_state": True}
    elif kind == "reanimate":
        effect = {"op": "reanimate", "cost": amount}
    elif kind == "summon_from_graveyard":
        effect = {"op": "summon", "resource_selector": {"zone": "graveyard", "selection": "random", "filters": {"side": "ally", "card_type": "follower"}}, "count": 1, "copy_mode": "exact", "preserve_state": True}
    elif kind in {"return_from_graveyard_to_hand", "banish_from_graveyard"}:
        effect = {"op": "return_to_hand" if kind.startswith("return") else "banish", "target": {"scope": "any", "selection": "chosen", "filters": {"side": "ally", "zone": "cemetery", "card_type": "follower"}}}
        ctx.gap("operation", kind)
    elif kind in {"reduce_countdown", "increase_countdown"}:
        delta = amount if kind == "reduce_countdown" else _negate_value(amount, ctx)
        effect = {"op": "modify_counter", "field": "countdown", "delta": delta, "target": target}
        if str(raw.get("target")) == "all_own_emblems" or raw.get("emblem_id"):
            ctx.gap("target", "emblem_countdown")
    elif kind == "set_stats":
        stat_effects: list[dict[str, Any]] = []
        if "attack" in raw:
            stat_effects.append({"op": "set_stat", "stat": "attack", "target": target, "amount": _value(raw["attack"], ctx)})
        if "health" in raw:
            stat_effects.append({"op": "set_stat", "stat": "life", "target": target, "amount": _value(raw["health"], ctx)})
        if len(stat_effects) == 1:
            effect = stat_effects[0]
        elif stat_effects:
            effect = {"op": "sequence", "effects": stat_effects}
    elif kind == "spellboost_hand":
        target.setdefault("filters", {}).setdefault("zone", "hand")
        effect = {"op": "spellboost", "target": target, "count": amount}
    elif kind == "repeat":
        nested = _nested_operations(raw.get("operations"), ctx)
        if nested:
            effect = {"op": "repeat", "count": amount, "effects": nested}
    elif kind == "conditional":
        condition = _conditions(raw.get("conditions"), ctx)
        then_effects = _nested_operations(raw.get("then"), ctx)
        else_effects = _nested_operations(raw.get("else"), ctx)
        if condition and then_effects:
            effect = {"op": "conditional", "condition": condition, "effects": then_effects, "else_effects": else_effects}
    elif kind == "choose_one":
        choices: list[dict[str, Any]] = []
        for option in _list(raw.get("options")):
            if not isinstance(option, Mapping):
                continue
            nested = _nested_operations(option.get("operations"), ctx)
            if nested:
                choices.append({"label": str(option.get("id", option.get("label", "choice"))), "effects": nested})
        if choices:
            effect = {"op": "mode_choice", "choices": choices}
            if raw.get("choose_count") is not None:
                effect["selection_count"] = _value(raw["choose_count"], ctx)
    elif kind == "random_choice":
        choices: list[dict[str, Any]] = []
        for option in _list(raw.get("options")):
            if not isinstance(option, Mapping):
                continue
            nested = _nested_operations(option.get("operations"), ctx)
            if nested:
                item: dict[str, Any] = {"label": str(option.get("id", option.get("label", "choice"))), "effects": nested}
                if option.get("weight") is not None:
                    item["weight"] = _value(option["weight"], ctx)
                choices.append(item)
        if choices:
            effect = {"op": "random_choice", "choices": choices}
            if raw.get("amount") is not None:
                effect["selection_count"] = _value(raw["amount"], ctx)
    elif kind == "random_distribute":
        choices: list[dict[str, Any]] = []
        for index, bucket in enumerate(_list(raw.get("buckets"))):
            nested = _nested_operations(bucket, ctx)
            if nested:
                choices.append({"label": f"bucket_{index}", "effects": nested})
        if choices:
            effect = {"op": "random_choice", "choices": choices}
        ctx.gap("operation", kind)
    elif kind == "optional":
        ctx.gap("operation", kind)
    elif kind == "target_exists":
        nested = _nested_operations(raw.get("then"), ctx)
        if nested:
            effect = {"op": "conditional", "condition": {"state": "variable", "name": "target_exists", "cmp": "eq", "value": True}, "effects": nested}
        ctx.gap("condition", "target_exists")
    elif kind == "replay_source_fanfare":
        effect = {"op": "replicate_ability", "trigger": "on_fanfare"}
    elif kind == "replace_deck":
        card_ids = [_card_ref(value, ctx, category="replacement_card") for value in _list(raw.get("card_ids"))]
        valid = [str(value) for value in card_ids if value is not None]
        effect = {"op": "replace_deck", "replacement": "swb:" + ",".join(valid)}
        ctx.gap("operation", kind)
    elif kind == "transform":
        card_id = _card_ref(raw.get("card_id"), ctx)
        if card_id is not None:
            effect = {"op": "transform", "target": target, "card_id": card_id}
        if raw.get("mode") or raw.get("duration"):
            ctx.gap("operation", "transform_temporary")
    elif kind == "transform_board_from_random_own_deck":
        effect = {"op": "transform", "target": target, "resource_selector": {"zone": "deck", "selection": "random", "filters": _filter_mapping(raw, ctx, category="deck_filter")}}
        ctx.gap("operation", kind)
    elif kind == "transform_deck_cards":
        card_id = _card_ref(raw.get("card_id"), ctx)
        old_id = _card_ref(raw.get("card_id_filter"), ctx, category="card_reference")
        if card_id is not None:
            effect = {"op": "transform", "target": {"scope": "any", "selection": "all", "filters": {"side": "ally", "zone": "deck", "card_id": old_id} if old_id is not None else {"side": "ally", "zone": "deck"}}, "card_id": card_id}
        ctx.gap("operation", kind)
    elif kind == "transform_hand_from_random_enemy_deck":
        effect = {"op": "transform", "target": {"scope": "ally_hand", "selection": "chosen", "filters": {"side": "ally", "zone": "hand"}}, "resource_selector": {"zone": "deck", "side": "enemy", "selection": "random"}}
        ctx.gap("operation", kind)
    elif kind == "set_leader_max_health":
        effect = {"op": "set_stat", "stat": "max_life", "target": target, "amount": amount}
        ctx.gap("operation", kind)
    elif kind == "remove_all_emblems":
        effect = {"op": "destroy_crest", "target": target}
        ctx.gap("operation", kind)
    elif kind == "gain_emblem":
        emblem_id = str(raw.get("emblem_id", ""))
        emblem_card_id = getattr(ctx, "emblem_cards", {}).get(emblem_id)
        if emblem_card_id is not None:
            effect = {"op": "gain_crest", "card_id": emblem_card_id, "player": "ally"}
        else:
            ctx.gap("emblem", emblem_id or "unknown")
    elif kind == "remove_emblem":
        emblem_id = str(raw.get("emblem_id", ""))
        emblem_card_id = getattr(ctx, "emblem_cards", {}).get(emblem_id)
        if emblem_card_id is not None:
            effect = {"op": "destroy_crest", "crest_card_id": emblem_card_id, "target": {"scope": "any", "selection": "all", "filters": {"side": "ally"}}}
        else:
            ctx.gap("emblem", emblem_id or "unknown")
    elif kind == "banish_deck_filtered":
        nested = _nested_operations(raw.get("operations"), ctx)
        effect = {"op": "sequence", "effects": [{"op": "banish", "target": {"scope": "any", "selection": "all", "filters": {"side": "ally", "zone": "deck"}}}] + nested}
        ctx.gap("operation", kind)
    elif kind == "banish_deck_duplicates":
        effect = {"op": "banish", "target": {"scope": "any", "selection": "all", "filters": {"side": "ally", "zone": "deck"}}}
        ctx.gap("operation", kind)
    elif kind == "repeat" or kind == "copy":
        ctx.gap("operation", kind)
    else:
        ctx.gap("operation", kind or "unknown")
        return None

    if effect is None:
        ctx.gap("operation", kind or "unknown")
        return None
    ctx.mapped(kind)
    return _attach_condition(effect, raw, ctx)


def _mode_trigger(raw_trigger: Any, ctx: _AdapterContext) -> str | None:
    trigger = TRIGGER_ALIASES.get(str(raw_trigger))
    if trigger is None:
        ctx.gap("trigger", str(raw_trigger or "unknown"))
    return trigger


def _ability(entry: Mapping[str, Any], ctx: _AdapterContext, *, trigger: str | None = None, cost: int | None = None) -> dict[str, Any] | None:
    trigger = trigger or _mode_trigger(entry.get("trigger"), ctx)
    if trigger is None:
        return None
    effects = _nested_operations(entry.get("operations"), ctx)
    if not effects:
        return None
    result: dict[str, Any] = {"trigger": trigger, "effects": effects}
    if cost is not None:
        result["cost"] = max(0, int(cost))
    condition = _conditions(entry.get("conditions"), ctx)
    if condition is not None:
        result["condition"] = condition
    return result


def _listener_ability(entry: Mapping[str, Any], ctx: _AdapterContext) -> dict[str, Any] | None:
    event = str(entry.get("event", ""))
    trigger = EVENT_TRIGGER_ALIASES.get(event)
    if trigger is None:
        ctx.gap("listener", event or "unknown")
        return None
    ability = _ability({**entry, "trigger": trigger}, ctx, trigger=trigger)
    if ability is None:
        return None
    event_filter = entry.get("event_filter")
    if isinstance(event_filter, Mapping):
        filters = _filter_mapping(event_filter, ctx, category="listener_filter")
        # Ability trigger_filter is intentionally narrower than operation
        # filters.  Drop zone/card side fields that cannot be represented.
        allowed = {key: filters[key] for key in ("card_id", "card_type", "tribe", "side") if key in filters}
        if allowed:
            ability["trigger_filter"] = allowed
    for key in ("zone", "event_scope", "turn_scope", "source_relation"):
        if entry.get(key) not in (None, "", "board", "owner_event", "owner_turn", "other"):
            ctx.gap("listener", f"{key}:{entry.get(key)}")
    if entry.get("zone") not in (None, "board"):
        ctx.gap("listener", f"zone:{entry.get('zone')}")
    if entry.get("once_per_turn") or entry.get("max_activations") is not None:
        ctx.gap("listener", "activation_limit")
    return ability


def _static_keywords(card_id: int, entries: Iterable[Mapping[str, Any]], passives: Iterable[Mapping[str, Any]], ctx: _AdapterContext) -> list[str]:
    output: set[str] = set()
    for entry in entries:
        for keyword in _list(entry.get("keywords")):
            canonical = _runtime_keyword(keyword, ctx)
            if canonical:
                output.add(canonical)
            else:
                ctx.gap("keyword", str(keyword), card_id)
    for entry in passives:
        kind = str(entry.get("kind", ""))
        if kind == "non_intrinsic_keyword":
            canonical = _runtime_keyword(entry.get("keyword"), ctx)
            if canonical:
                output.add(canonical)
            else:
                ctx.gap("keyword", str(entry.get("keyword")), card_id)
        elif kind == "attacks_per_turn":
            # Handled by the caller as default_attacks.
            continue
        elif kind == "cannot_be_played":
            canonical = _runtime_keyword("unplayable", ctx)
            if canonical:
                output.add(canonical)
        elif kind == "cannot_be_destroyed_by_effects":
            canonical = _runtime_keyword("effect_indestructible", ctx)
            if canonical:
                output.add(canonical)
        elif kind:
            ctx.gap("passive", kind, card_id)
    return sorted(output)


def _fusions(entries: Iterable[Mapping[str, Any]], ctx: _AdapterContext) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for entry in entries:
        material = _filter_mapping(entry.get("material_filter", {}), ctx, category="fusion_filter")
        config: dict[str, Any] = {"filters": material}
        results: list[dict[str, Any]] = []
        aggregate = "count"
        for item in _list(entry.get("transform_results")):
            if not isinstance(item, Mapping):
                continue
            card_id = _card_ref(item.get("card_id"), ctx, category="fusion_result")
            if card_id is None:
                continue
            minimum = _as_int(item.get("min_total_material_cost"), 0) or 0
            maximum = _as_int(item.get("max_total_material_cost"))
            if minimum or maximum is not None:
                aggregate = "total_cost"
            results.append({"min": minimum, "max": maximum, "card_id": card_id})
            if item.get("preserve_fused_materials"):
                ctx.gap("fusion", "preserve_fused_materials")
        if results:
            config["aggregate"] = aggregate
            config["outcomes"] = results
        output.append(config)
    return output


def _union_condition(kind: str) -> dict[str, Any]:
    if kind == "super_skybound_art":
        return {"state": "super_skybound_art", "cmp": "gte", "value": 15}
    return {"state": "skybound_art", "cmp": "gte", "value": 10}


def _card_type(card: Mapping[str, Any]) -> str:
    value = card.get("type")
    if isinstance(value, str):
        return _canonical_card_type(value) or value
    return CARD_TYPE_BY_ID.get(_as_int(card.get("type_id"), 0) or 0, "unknown")


def _union_abilities(card_id: int, entries: Iterable[Mapping[str, Any]], base_effects: list[dict[str, Any]], ctx: _AdapterContext) -> list[dict[str, Any]]:
    unions = list(entries)
    if not unions:
        return []
    converted: list[tuple[Mapping[str, Any], list[dict[str, Any]]]] = []
    for entry in unions:
        effects = _nested_operations(entry.get("operations"), ctx)
        if effects:
            converted.append((entry, effects))
    if not converted:
        return []
    # Source uses replace_base_operations for an Enhance/Union payload.  A
    # single conditional preserves the mutual exclusion instead of appending
    # both burst and base effects (the historical generated-rule bug).
    replacements = [item for item in converted if item[0].get("replace_base_operations")]
    if replacements:
        branch: list[dict[str, Any]] = list(base_effects)
        for entry, effects in sorted(replacements, key=lambda item: 0 if item[0].get("kind") == "union_burst" else 1):
            branch = [{"op": "conditional", "condition": _union_condition(str(entry.get("kind"))), "effects": effects, "else_effects": branch}]
        return [{"trigger": "on_play" if _card_type(ctx.card(card_id)) == "spell" else "on_fanfare", "effects": branch[0:1]}]
    # Non-replacing burst effects remain separate abilities, but if both SA and
    # SSA are present the stronger threshold is nested above the lower one to
    # avoid double execution.
    if len(converted) > 1:
        converted.sort(key=lambda item: 0 if item[0].get("kind") == "super_skybound_art" else 1)
    return [
        {
            "trigger": "on_play" if _card_type(ctx.card(card_id)) == "spell" else "on_fanfare",
            "condition": _union_condition(str(entry.get("kind"))),
            "effects": effects,
        }
        for entry, effects in converted
    ]


def _build_card_rule(
    card_id: int,
    card: Mapping[str, Any],
    *,
    by_section: Mapping[str, list[tuple[str, int, Mapping[str, Any]]]],
    ctx: _AdapterContext,
) -> dict[str, Any]:
    ctx.current_card_id = card_id
    normal = {"kind": "normal", "cost": _as_int(card.get("cost"), 0) or 0, "abilities": []}
    modes: dict[str, dict[str, Any]] = {"normal": normal}
    source_present = False
    normal_effects: list[dict[str, Any]] = []
    rule_entries = [item for _fn, _index, item in by_section.get("rules", []) if _source_id(item) == card_id]
    for filename, _index, entry in by_section.get("rules", []):
        if _source_id(entry) != card_id:
            continue
        source_present = True
        ctx.source_files[card_id].add(filename)
        ability = _ability(entry, ctx)
        if ability:
            if ability["trigger"] in {"on_play", "on_fanfare", "on_summon", "on_invoke", "on_evolve", "on_super_evolve", "on_engage"}:
                normal["abilities"].append(ability)
            else:
                normal["abilities"].append(ability)
            normal_effects.extend(ability.get("effects", []))
        for play_mode in _list(entry.get("play_modes")):
            if not isinstance(play_mode, Mapping):
                continue
            mode_kind = str(play_mode.get("type", "normal"))
            if mode_kind not in {"enhance", "accelerate", "crystallize", "fusion", "activation"}:
                ctx.gap("mode", mode_kind)
                continue
            mode = modes.setdefault(mode_kind, {"kind": mode_kind, "cost": _as_int(play_mode.get("cost"), 0) or 0, "abilities": []})
            mode["cost"] = _as_int(play_mode.get("cost"), mode.get("cost", 0)) or 0
            mode_entry = dict(entry)
            mode_entry["operations"] = play_mode.get("operations", [])
            mode_ability = _ability(mode_entry, ctx)
            if mode_ability:
                mode["abilities"].append(mode_ability)
            # CardRules v2 models Enhance/Accelerate as mutually exclusive
            # modes, so SWB-RL's ``replace_base_operations`` flag is fully
            # represented by the mode boundary itself.  Do not mark this as
            # a semantic gap: doing so would downgrade otherwise executable
            # cards and prevent them from entering the migration allowlist.
            if play_mode.get("resulting_card_type") is not None or play_mode.get("countdown") is not None:
                ctx.gap("mode", "resulting_card_type_or_countdown")

    # Activation costs are metadata attached to the same card's Engage
    # ability.  The runtime reads the cost from the ability, not a synthetic
    # play mode.
    activation_costs = {
        _source_id(item): _as_int(item.get("cost"), 0) or 0
        for _fn, _index, item in by_section.get("activations", [])
        if _source_id(item) == card_id
    }
    if activation_costs:
        source_present = True
        for ability in normal["abilities"]:
            if ability.get("trigger") == "on_engage":
                ability["cost"] = activation_costs[card_id]

    # Listeners are persistent abilities of the card carrying the listener.
    for filename, _index, entry in by_section.get("listeners", []):
        if _source_id(entry) != card_id:
            continue
        source_present = True
        ctx.source_files[card_id].add(filename)
        ability = _listener_ability(entry, ctx)
        if ability:
            normal["abilities"].append(ability)

    # Automatic Invoke metadata creates a turn-start trigger that invokes the
    # card from the public deck.  The separate `trigger=invoke` rule above is
    # retained as the invoked card's Fanfare-equivalent payload.
    for filename, _index, entry in by_section.get("invocations", []):
        if _source_id(entry) != card_id:
            continue
        source_present = True
        ctx.source_files[card_id].add(filename)
        # `from_zone` is understood by the runtime but intentionally omitted
        # from the v2 effect property list; use the schema-approved selector
        # spelling instead.
        invoke_effect = {"op": "invoke", "card_id": card_id, "resource_selector": {"zone": "deck"}, "target": {"scope": "self"}}
        ability: dict[str, Any] = {"trigger": TRIGGER_ALIASES.get(str(entry.get("trigger")), "on_turn_start"), "effects": [invoke_effect]}
        condition = _conditions(entry.get("conditions"), ctx)
        if condition:
            ability["condition"] = condition
        normal["abilities"].append(ability)

    # Intrinsic keywords and passives.
    intrinsic = [item for _fn, _index, item in by_section.get("intrinsic_keywords", []) if _source_id(item) == card_id]
    passives = [item for _fn, _index, item in by_section.get("passives", []) if _source_id(item) == card_id]
    if intrinsic or passives:
        source_present = True
    static_keywords = _static_keywords(card_id, intrinsic, passives, ctx)
    default_attacks = next((_as_int(item.get("amount"), 1) for item in passives if item.get("kind") == "attacks_per_turn"), None)

    # Union/Skybound payloads are attached to the play event.  Pass a copy of
    # base effects so replace-base conditionals cannot mutate the normal mode.
    unions = [item for _fn, _index, item in by_section.get("union_bursts", []) if _source_id(item) == card_id]
    if unions:
        source_present = True
        union_abilities = _union_abilities(card_id, unions, list(normal_effects), ctx)
        if union_abilities:
            # ``replace_base_operations`` means that the burst is an
            # alternative to the card's ordinary play/fanfare payload.  The
            # base ability was already appended above while scanning the
            # RuleBook; retaining it here would execute the mode twice (once
            # from the old ability and once from the conditional's
            # ``else_effects``).  Remove only abilities on the same entry
            # trigger so unrelated evolve/last-words/listener abilities stay
            # intact.  The conditional carries both the burst and base
            # branches and is the single executable replacement.
            replacing = any(bool(item.get("replace_base_operations")) for item in unions)
            if replacing:
                play_trigger = "on_play" if _card_type(ctx.card(card_id)) == "spell" else "on_fanfare"
                normal["abilities"] = [
                    ability for ability in normal["abilities"] if ability.get("trigger") != play_trigger
                ]
            normal["abilities"].extend(union_abilities)

    fusion_entries = [item for _fn, _index, item in by_section.get("fusions", []) if _source_id(item) == card_id]
    fusion = _fusions(fusion_entries, ctx) if fusion_entries else []
    if fusion:
        source_present = True

    # Emblems are intentionally represented by the effect-level gain_crest
    # operation; their delayed/countdown definitions are captured as gaps in
    # the report because v2 has no top-level emblem registry.
    emblem_entries = [item for _fn, _index, item in by_section.get("emblems", []) if _source_id(item) == card_id]
    if emblem_entries:
        source_present = True
        for item in emblem_entries:
            if item.get("countdown") is not None or item.get("triggers"):
                ctx.gap("emblem", str(item.get("id", "definition")), card_id)

    gaps = sorted(ctx.card_gaps.get(card_id, set()))
    if not source_present:
        support = "unsupported"
    elif gaps:
        support = "partial"
    else:
        support = "generated"
    source_payload: dict[str, Any] = {"catalog_card": card}
    source_payload.update(
        {
            section: [item for _fn, _index, item in by_section.get(section, []) if _source_id(item) == card_id]
            for section in SOURCE_SECTIONS
        }
    )
    rule: dict[str, Any] = {
        "card_id": card_id,
        "support": support,
        # Include the compact catalog row as well as every RuleBook section.
        # Cards without a RuleBook entry otherwise all hash to the same
        # empty-section value, which makes stale migration reviews impossible
        # to detect.
        "source_hash": _sha256(source_payload),
        "modes": [modes[key] for key in ("normal", "enhance", "accelerate", "crystallize", "fusion", "activation") if key in modes],
    }
    if static_keywords:
        rule["static_keywords"] = static_keywords
    if default_attacks is not None:
        rule["default_attacks"] = max(1, int(default_attacks))
    if fusion:
        rule["fusion"] = fusion
    if gaps:
        rule["unparsed_clauses"] = gaps
    rule["notes"] = "SWB-RL RuleBook adapter candidate; structural import only, not runtime-verified."
    return rule


def _prepare_context(catalog: Mapping[str, Any], files: Mapping[str, Any]) -> tuple[_AdapterContext, dict[str, list[tuple[str, int, Mapping[str, Any]]]]]:
    ctx = _AdapterContext(catalog)
    by_section = {section: _raw_entries(files, section) for section in SOURCE_SECTIONS}
    ctx.emblem_cards = {}
    for _filename, _index, entry in by_section["emblems"]:
        emblem_id = entry.get("id")
        card_id = _as_int(entry.get("source_card_id"))
        if emblem_id and card_id is not None and ctx.has_card(card_id):
            ctx.emblem_cards[str(emblem_id)] = card_id
    return ctx, by_section


def build_swb_v2_candidate(
    source_root: Path | str = DEFAULT_SOURCE,
    *,
    catalog: Mapping[str, Any] | None = None,
    rule_files: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build ``(candidate_rules, adapter_report)`` deterministically."""

    source_root = Path(source_root)
    catalog = dict(catalog) if catalog is not None else _load_catalog(source_root)
    files = dict(rule_files) if rule_files is not None else _load_rule_files(source_root)
    ctx, by_section = _prepare_context(catalog, files)

    rules: dict[str, Any] = {}
    for key in sorted(ctx.cards, key=lambda value: int(value) if str(value).isdigit() else str(value)):
        card_id = _as_int(key)
        if card_id is None:
            continue
        rules[str(card_id)] = _build_card_rule(card_id, ctx.cards[key], by_section=by_section, ctx=ctx)

    support = Counter(str(rule.get("support")) for rule in rules.values())
    synthetic_ids = sorted(
        {
            int(_source_id(entry))
            for section in SOURCE_SECTIONS
            for _filename, _index, entry in by_section[section]
            if (_source_id(entry) is not None and not ctx.has_card(_source_id(entry)))
        }
    )
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "ruleset_revision": RULESET_REVISION,
        "catalog_version": int(catalog.get("schema_version", 1) or 1),
        "game_version": "SWB-RL candidate",
        "rules": rules,
    }
    report = {
        "schema_version": 1,
        "source": {
            "project": "SWB-RL",
            "source_root": str(source_root),
            "rule_files": sorted(files),
            "rule_file_count": len(files),
        },
        "summary": {
            "catalog_card_count": len(ctx.cards),
            "candidate_rule_count": len(rules),
            "support": dict(sorted(support.items())),
            "synthetic_source_card_id_count": len(synthetic_ids),
            "synthetic_source_card_ids": synthetic_ids,
            "mapped_operation_occurrences": sum(ctx.mapped_operations.values()),
            "gap_count": sum(ctx.gaps.values()),
            "card_with_gaps_count": sum(1 for rule in rules.values() if rule.get("unparsed_clauses")),
            "candidate_sha256": _sha256(candidate),
        },
        "mapped_operations": dict(sorted(ctx.mapped_operations.items())),
        "gaps": dict(sorted(ctx.gaps.items())),
        "source_files_by_card": {str(card_id): sorted(names) for card_id, names in sorted(ctx.source_files.items())},
        "cards": {
            str(card_id): {
                "support": rule.get("support"),
                "source_hash": rule.get("source_hash"),
                "gap_count": len(rule.get("unparsed_clauses", [])),
            }
            for card_id, rule in sorted(rules.items(), key=lambda item: int(item[0]))
        },
    }
    return candidate, report


# Short aliases make the adapter convenient to use from tests and notebooks.
build_candidate = build_swb_v2_candidate
adapt_swb_rules = build_swb_v2_candidate


def report_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report, Mapping) else {}
    lines = [
        "# SWB-RL → CardRules v2 adapter report",
        "",
        "This is an isolated structural candidate. It is not a replacement for `data/generated/card_rules_v2.json`.",
        "",
        f"- Catalog cards: {summary.get('catalog_card_count', 0)}",
        f"- Candidate rules: {summary.get('candidate_rule_count', 0)}",
        f"- Source synthetic IDs skipped: {summary.get('synthetic_source_card_id_count', 0)}",
        f"- Mapped operation occurrences: {summary.get('mapped_operation_occurrences', 0)}",
        f"- Cards with adapter gaps: {summary.get('card_with_gaps_count', 0)}",
        "",
        "## Support",
        "",
        "| status | cards |",
        "|---|---:|",
    ]
    for status, count in sorted((summary.get("support") or {}).items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## Largest gap categories", "", "| marker | occurrences |", "|---|---:|"])
    gaps = report.get("gaps", {}) if isinstance(report, Mapping) else {}
    for marker, count in sorted(gaps.items(), key=lambda item: (-int(item[1]), str(item[0])))[:40]:
        lines.append(f"| `{marker}` | {count} |")
    lines.append("")
    return "\n".join(lines)


def write_adapter_artifacts(
    candidate: Mapping[str, Any],
    report: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT,
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "candidate": output_dir / "swb_card_rules_v2_candidate.json",
        "report": output_dir / "swb_rule_adapter_report.json",
        "markdown": output_dir / "swb_rule_adapter_report.md",
    }
    paths["candidate"].write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["markdown"].write_text(report_markdown(report), encoding="utf-8")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="SWB-RL checkout")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidate, report = build_swb_v2_candidate(args.source)
    paths = write_adapter_artifacts(candidate, report, args.output_dir)
    print(f"candidate: {paths['candidate']}")
    print(f"report: {paths['report']}")
    print(f"markdown: {paths['markdown']}")
    print(json.dumps(report.get("summary", {}), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "adapt_swb_rules",
    "build_candidate",
    "build_swb_v2_candidate",
    "report_markdown",
    "write_adapter_artifacts",
]
