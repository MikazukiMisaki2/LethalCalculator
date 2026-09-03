"""Import and audit the structured catalog/rules from the SWB-RL project.

This is deliberately a read-only compatibility boundary.  It does not replace
the current ``CardCatalog`` or ``card_rules_v2`` artifacts.  The command reads
``SWB-RL/data/cards.sqlite3`` and ``SWB-RL/data/rules/*.json`` and writes three
deterministic artifacts:

* a compact normalized catalog projection;
* the original rule files with per-file hashes;
* a compatibility report against the current LethalCalculator catalog/rules.

The reference project has a richer typed RuleBook than our v2 JSON format.
Keeping the original files intact here lets a later adapter preserve target
bindings, listeners and dynamic expressions instead of flattening them during
import.

Example::

    python import_swb_rl.py --source D:/Github/SWB-RL

The default output directory is ``data/imported``.  It is generated data and
is ignored by this repository's ``.gitignore``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
DEFAULT_SOURCE = Path("..") / "SWB-RL"
DEFAULT_OUTPUT = Path("data") / "imported"
DEFAULT_CATALOG = Path("data/generated/card_catalog.json")
DEFAULT_RULES = Path("data/generated/card_rules_v2.json")
DEFAULT_SCHEMA = Path("schemas/card_rules_v2.schema.json")


def _json_bytes(value: Any) -> bytes:
    """Return a stable UTF-8 representation for hashes and comparisons."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _rows_by_card(connection: sqlite3.Connection, table: str) -> dict[int, list[dict[str, Any]]]:
    """Load a small normalized table keyed by ``card_id``.

    The table names are fixed internal values, never user-provided SQL.  The
    helper keeps the extraction code compact while still returning ordinary
    JSON-compatible dictionaries.
    """

    connection.row_factory = sqlite3.Row
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(f'SELECT * FROM "{table}"'):
        item = dict(row)
        card_id = _as_int(item.get("card_id"))
        if card_id is not None:
            result[card_id].append(item)
    return result


def _parse_json_column(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return copy.deepcopy(fallback)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return copy.deepcopy(fallback)


def _card_projection(
    row: Mapping[str, Any],
    *,
    names: Mapping[int, list[dict[str, Any]]],
    localizations: Mapping[int, list[dict[str, Any]]],
    skills: Mapping[int, list[dict[str, Any]]],
    skill_texts: Mapping[int, list[dict[str, Any]]],
    abilities: Mapping[int, list[dict[str, Any]]],
    references: Mapping[int, list[dict[str, Any]]],
    alt_modes: Mapping[int, list[dict[str, Any]]],
    textures: Mapping[int, list[dict[str, Any]]],
    flavor_texts: Mapping[int, list[dict[str, Any]]],
    extra_data: Mapping[int, list[dict[str, Any]]],
    collectible_by_set: Mapping[int, bool],
) -> dict[str, Any]:
    card_id = int(row["card_id"])
    name_rows = names.get(card_id, [])
    name = {
        "chs": next(
            (_as_text(item.get("name")) for item in name_rows if item.get("language") == "zh-CN"),
            "",
        ),
        "eng": next(
            (_as_text(item.get("name")) for item in name_rows if item.get("language") == "en"),
            "",
        ),
    }

    def normalize_rows(items: Iterable[Mapping[str, Any]], fields: Iterable[str]) -> list[dict[str, Any]]:
        return [
            {field: copy.deepcopy(item.get(field)) for field in fields}
            for item in sorted(items, key=lambda value: int(value.get("position", 0)))
        ]

    projection: dict[str, Any] = {
        "card_id": card_id,
        "base_card_id": int(row["base_card_id"]),
        "card_set_id": int(row["card_set_id"]),
        "is_collectible": bool(collectible_by_set.get(int(row["card_set_id"]), False)),
        "class_id": int(row["class_id"]),
        "rarity_id": int(row["rarity_id"]),
        "type_id": int(row["type_id"]),
        "cost": int(row["cost"]),
        "attack": _as_int(row.get("attack"), 0) or 0,
        "life": _as_int(row.get("life"), 0) or 0,
        "is_evolution": bool(row.get("is_evolution")),
        "evolves_to": _as_int(row.get("evolves_to")),
        "tribe_id": int(row.get("tribe_id") or 0),
        "tribe_name": _as_text(row.get("tribe_name")),
        "name": name,
        "localizations": normalize_rows(
            localizations.get(card_id, []),
            ("language", "class_name", "rarity_name", "type_name", "tribe_name"),
        ),
        "skills": normalize_rows(
            skills.get(card_id, []),
            ("position", "skill_id", "type", "subtype"),
        ),
        "skill_texts": normalize_rows(
            skill_texts.get(card_id, []),
            ("position", "text_key", "text", "text_chs", "text_eng"),
        ),
        "abilities": normalize_rows(
            abilities.get(card_id, []),
            ("ability_keyword", "raw_keyword"),
        ),
        "references": normalize_rows(
            references.get(card_id, []),
            ("position", "referenced_card_id", "referenced_name"),
        ),
        "alt_modes": normalize_rows(
            alt_modes.get(card_id, []),
            ("position", "mode_type", "cost", "text_chs", "text_eng"),
        ),
        "textures": normalize_rows(textures.get(card_id, []), ("variant", "path")),
        "flavor_texts": normalize_rows(
            flavor_texts.get(card_id, []),
            ("position", "text_key", "text_chs", "text_eng"),
        ),
    }

    extra = extra_data.get(card_id, [])
    if extra:
        first = extra[0]
        projection["extra_data"] = {
            "skin_names": _parse_json_column(first.get("skin_names"), {}),
            "voices": _parse_json_column(first.get("voices"), {}),
            "voice_variants": _parse_json_column(first.get("voice_variants"), {}),
        }
    else:
        projection["extra_data"] = {"skin_names": {}, "voices": {}, "voice_variants": {}}

    # ``raw_json`` is deliberately not copied into every output card.  The
    # normalized columns above are the import surface; this hash still makes a
    # source refresh visible without retaining the original multilingual blob.
    raw_json = _as_text(row.get("raw_json")).encode("utf-8")
    projection["raw_json_sha256"] = _sha256_bytes(raw_json)
    projection["source_hash"] = _sha256_bytes(_json_bytes(projection))
    return projection


def build_catalog_projection(database_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the normalized SQLite catalog and return projection plus metadata."""

    if not database_path.exists():
        raise FileNotFoundError(f"SWB-RL database not found: {database_path}")

    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")

        cards = [dict(row) for row in connection.execute("SELECT * FROM cards ORDER BY card_id")]
        source_imports = [
            dict(row)
            for row in connection.execute("SELECT * FROM source_imports ORDER BY id")
        ]
        collectible_by_set = {
            int(row["id"]): bool(row["is_collectible"])
            for row in connection.execute("SELECT id, is_collectible FROM card_sets")
        }
        names = _rows_by_card(connection, "card_names")
        localizations = _rows_by_card(connection, "card_localizations")
        skills = _rows_by_card(connection, "skills")
        skill_texts = _rows_by_card(connection, "skill_texts")
        abilities = _rows_by_card(connection, "card_abilities")
        references = _rows_by_card(connection, "card_references")
        alt_modes = _rows_by_card(connection, "alt_modes")
        textures = _rows_by_card(connection, "textures")
        flavor_texts = _rows_by_card(connection, "flavor_texts")
        extra_data = _rows_by_card(connection, "card_extra_data")
        rule_support = [dict(row) for row in connection.execute("SELECT * FROM rule_support ORDER BY card_id")]
    finally:
        connection.close()

    cards_by_id = {
        str(int(row["card_id"])): _card_projection(
            row,
            names=names,
            localizations=localizations,
            skills=skills,
            skill_texts=skill_texts,
            abilities=abilities,
            references=references,
            alt_modes=alt_modes,
            textures=textures,
            flavor_texts=flavor_texts,
            extra_data=extra_data,
            collectible_by_set=collectible_by_set,
        )
        for row in cards
    }
    database_hash = _sha256_file(database_path)
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "project": "SWB-RL",
            "database": "data/cards.sqlite3",
            "database_sha256": database_hash,
            "source_imports": source_imports,
        },
        "cards": cards_by_id,
    }
    metadata = {
        "database_sha256": database_hash,
        "database_integrity": "ok",
        "database_card_count": len(cards_by_id),
        "database_collectible_count": sum(
            1 for card in cards_by_id.values() if card["is_collectible"]
        ),
        "database_generated_count": sum(
            1 for card in cards_by_id.values() if not card["is_collectible"]
        ),
        "rule_support_counts": dict(
            sorted(Counter(str(row.get("support_level", "")) for row in rule_support).items())
        ),
        "table_counts": _table_counts(database_path),
    }
    return catalog, metadata


def _table_counts(database_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(str(database_path))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name <> 'sqlite_sequence' ORDER BY name"
            )
        ]
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    finally:
        connection.close()


def _rule_file_payloads(rules_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not rules_dir.exists():
        raise FileNotFoundError(f"SWB-RL rules directory not found: {rules_dir}")
    files: dict[str, Any] = {}
    manifest: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.json")):
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        relative = path.relative_to(rules_dir).as_posix()
        files[relative] = payload
        manifest.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(raw),
                "byte_count": len(raw),
                "top_level_keys": sorted(payload) if isinstance(payload, dict) else [],
            }
        )
    if not files:
        raise ValueError(f"No JSON rule files found in {rules_dir}")
    return files, manifest


def _reference_effect_kinds(source_root: Path, observed: Iterable[str]) -> set[str]:
    """Load the reference enum when possible, with a data-only fallback."""

    observed_set = {str(value) for value in observed if value}
    try:
        source_text = str(source_root.resolve())
        if source_text not in sys.path:
            sys.path.insert(0, source_text)
        from swb.engine.effects import EffectKind  # type: ignore

        return {item.value for item in EffectKind}
    except Exception:
        return observed_set


def _walk_operation_kinds(value: Any, known: set[str], counter: Counter[str]) -> None:
    if isinstance(value, Mapping):
        kind = value.get("kind")
        if isinstance(kind, str) and kind in known:
            counter[kind] += 1
        for child in value.values():
            _walk_operation_kinds(child, known, counter)
    elif isinstance(value, list):
        for child in value:
            _walk_operation_kinds(child, known, counter)


def _raw_kind_values(value: Any, result: set[str]) -> None:
    """Collect literal ``kind`` values before consulting the typed enum.

    The rule JSON contains nested effect dictionaries.  Keeping this small
    data-only pass means the report still has useful operation statistics when
    the reference Python package cannot be imported (for example, when a
    caller only copied ``data/`` out of SWB-RL).
    """

    if isinstance(value, Mapping):
        kind = value.get("kind")
        if isinstance(kind, str) and kind:
            result.add(kind)
        for child in value.values():
            _raw_kind_values(child, result)
    elif isinstance(value, list):
        for child in value:
            _raw_kind_values(child, result)


def _walk_ids(value: Any, keys: set[str], result: set[int]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in keys:
                if isinstance(child, int) and not isinstance(child, bool):
                    result.add(child)
                elif isinstance(child, list):
                    result.update(
                        item
                        for item in child
                        if isinstance(item, int) and not isinstance(item, bool)
                    )
            _walk_ids(child, keys, result)
    elif isinstance(value, list):
        for child in value:
            _walk_ids(child, keys, result)


def _section_entries(payload: Any, section: str) -> list[Any]:
    if not isinstance(payload, Mapping):
        return []
    value = payload.get(section, [])
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        return list(value.values())
    return []


def _load_audit_summaries(source_root: Path) -> dict[str, Any]:
    report_paths = {
        "rule_coverage": source_root / "data/reports/rule_coverage.json",
        "token_audit": source_root / "data/reports/token_audit.json",
        "ability_audit": source_root / "data/reports/ability_audit.json",
        "final_gate": source_root / "data/reports/card_bug_audit/final_gate.json",
        "runtime_coverage": source_root / "data/reports/card_bug_audit/runtime_coverage.json",
        "clause_matrix": source_root / "data/reports/card_bug_audit/card_clause_matrix.json",
    }
    result: dict[str, Any] = {}
    for name, path in report_paths.items():
        if not path.exists():
            continue
        payload = _load_json(path)
        if name == "rule_coverage":
            result[name] = payload.get("summary", payload)
        elif name in {"token_audit", "ability_audit", "final_gate", "runtime_coverage", "clause_matrix"}:
            result[name] = payload.get("summary", payload)
    return result


# The current v2 schema intentionally has broad operations.  This table is
# only an import report: every mapped operation still needs target/zone/binding
# conversion and runtime evidence before it may become a confirmed rule.
SEMANTIC_OPERATION_MAP: dict[str, tuple[str, str]] = {
    "damage_leader": ("damage", "leader target must be preserved"),
    "damage_unit": ("damage", "unit/leader target union must be preserved"),
    "distribute_damage": ("damage", "distribution order/allocation must be preserved"),
    "random_distribute": ("damage", "random allocation must remain stochastic"),
    "heal_leader": ("heal", "leader target"),
    "heal_unit": ("heal", "unit target"),
    "heal_unit_and_leader": ("heal", "two target scopes"),
    "buff_unit": ("buff", "board target/filter"),
    "buff_hand_card": ("buff", "hand entity and current cost"),
    "buff_deck_cards": ("buff", "deck zone selector"),
    "add_card": ("add_to_hand", "generated-card identity"),
    "add_card_to_deck": ("add_to_zone", "deck destination"),
    "copy_to_hand": ("copy", "copy mode and current entity state"),
    "copy_destroyed_followers_to_hand": ("copy", "destroyed-history selector"),
    "add_keyword": ("grant_keyword", "canonical keyword and target"),
    "add_random_keywords": ("grant_keyword", "random keyword branch"),
    "remove_all_abilities": ("remove_abilities", "printed/granted ability state"),
    "add_attack_restriction": ("gain_status", "restriction duration"),
    "remove_attack_restriction": ("gain_status", "restriction removal"),
    "add_targeting_restriction": ("gain_status", "targeting restriction"),
    "remove_targeting_restriction": ("gain_status", "targeting restriction removal"),
    "restore_mana": ("recover_pp", "PP resource"),
    "restore_evolution_points": ("modify_resource", "EP resource"),
    "restore_super_evolution_points": ("modify_resource", "SEP resource"),
    "change_max_mana": ("modify_resource", "max PP"),
    "add_union_burst_gauge": ("modify_resource", "Union Burst gauge"),
    "add_shadows": ("modify_resource", "cemetery/shadow resource"),
    "add_combo": ("modify_counter", "combo counter"),
    "add_earth_sigils": ("modify_resource", "earth sigil resource"),
    "earth_rite": ("consume_resource", "atomic earth rite payment"),
    "consume_faith": ("consume_resource", "Faith instance identity"),
    "necromancy": ("consume_resource", "cemetery payment"),
    "grant_faith_ability": ("grant_resource_ability", "dynamic Faith ability"),
    "grant_faith_mode_selection_bonus": ("grant_resource_ability", "Faith mode bonus"),
    "evolve_unit": ("auto_evolve", "normal evolution semantics"),
    "super_evolve_unit": ("auto_evolve", "super-evolution semantics"),
    "grant_attacks_per_turn": ("set_attacks", "attack capacity"),
    "set_stats": ("set_stat", "dimension-specific assignment"),
    "gain_emblem": ("gain_crest", "emblem/crest instance"),
    "add_emblem": ("gain_crest", "emblem/crest instance"),
    "remove_emblem": ("destroy_crest", "emblem/crest identity"),
    "remove_all_emblems": ("destroy_crest", "all emblem instances"),
    "redraw_hand": ("draw", "redraw semantics"),
    "return_from_graveyard_to_hand": ("return_to_hand", "graveyard selector"),
    "summon_from_graveyard": ("summon", "graveyard selector"),
    "banish_from_graveyard": ("banish", "graveyard selector"),
    "summon_copy": ("copy", "copy source and state"),
    "summon_exact_copy": ("copy", "exact entity snapshot"),
    "summon_hand_copy": ("copy", "hand entity source"),
    "summon_from_hand": ("summon", "hand source"),
    "summon_from_deck": ("summon", "deck selector"),
    "summon_destroyed_amulets": ("copy", "destroyed-history selector"),
    "replay_source_fanfare": ("replicate_ability", "trigger replay"),
    "target_exists": ("conditional", "target existence condition"),
    "choose_one": ("mode_choice", "player choice, not random choice"),
    "banish_deck_filtered": ("banish", "deck filter"),
    "banish_deck_duplicates": ("banish", "duplicate-name filter"),
    "banish_same_name": ("banish", "bound-name filter"),
    "change_cost": ("modify_cost", "cost mode and zone"),
    "change_deck_cost": ("modify_cost", "deck cost modifier"),
    "change_leader_max_health": ("set_stat", "leader max-health dimension"),
    "set_leader_max_health": ("set_stat", "leader max-health dimension"),
    "grant_turn_end_ability": ("gain_status", "delayed nested ability"),
    "grant_turn_end_destroy": ("gain_status", "delayed destruction"),
    "grant_turn_end_banish": ("gain_status", "delayed banish"),
    "grant_effect_destroy_immunity": ("gain_status", "effect immunity"),
    "add_leader_barrier": ("gain_status", "leader barrier"),
    "copy_leftmost_hand_to_hand": ("copy", "leftmost hand entity"),
    "copy_random_enemy_deck_to_hand": ("copy", "hidden/public deck selector"),
    "transform_hand_from_random_enemy_deck": ("transform", "hidden deck selector"),
    "transform_board_from_random_own_deck": ("transform", "deck selector"),
    "transform_deck_cards": ("transform", "deck zone selector"),
}


def _current_catalog_card(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _load_current_catalog(path: Path | None) -> dict[int, Mapping[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = _load_json(path)
    cards = payload.get("cards", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(cards, Mapping):
        return {}
    return {
        int(card_id): card
        for card_id, card in cards.items()
        if str(card_id).isdigit() and isinstance(card, Mapping)
    }


def _load_current_rules(path: Path | None) -> dict[str, Mapping[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = _load_json(path)
    rules = payload.get("rules", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(rules, Mapping):
        return {}
    return {str(card_id): card for card_id, card in rules.items() if isinstance(card, Mapping)}


def _load_current_ops(schema_path: Path | None) -> set[str]:
    if schema_path is None or not schema_path.exists():
        return set()
    payload = _load_json(schema_path)
    try:
        return set(payload["$defs"]["effect"]["properties"]["op"]["enum"])
    except (KeyError, TypeError):
        return set()


def _compare_catalogs(
    reference_cards: Mapping[str, Mapping[str, Any]],
    current_cards: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    ref_ids = {int(card_id) for card_id in reference_cards}
    current_ids = set(current_cards)
    overlap = sorted(ref_ids & current_ids)
    field_mismatches: list[dict[str, Any]] = []
    name_mismatches: list[dict[str, Any]] = []
    text_equal = 0
    text_different: list[dict[str, Any]] = []
    text_missing: list[int] = []

    for card_id in overlap:
        reference = reference_cards[str(card_id)]
        current = current_cards[card_id]
        current_stats = current.get("stats", {})
        current_tribes = current.get("tribes", [])
        current_tribe_id = current_tribes[0] if isinstance(current_tribes, list) and current_tribes else 0
        field_pairs = {
            "base_card_id": (current.get("base_card_id"), reference.get("base_card_id")),
            "class_id": (current.get("class_id"), reference.get("class_id")),
            "raw_type/type_id": (current.get("raw_type"), reference.get("type_id")),
            "cost": (current.get("cost"), reference.get("cost")),
            "evolves_to": (current.get("evolves_to") or 0, reference.get("evolves_to") or 0),
            "tribe_id": (current_tribe_id or 0, reference.get("tribe_id") or 0),
            "attack": ((current_stats.get("attack") or 0), reference.get("attack") or 0),
            "life": ((current_stats.get("life") or 0), reference.get("life") or 0),
        }
        for field, (left, right) in field_pairs.items():
            if left != right:
                field_mismatches.append(
                    {"card_id": card_id, "field": field, "current": left, "reference": right}
                )

        current_name = current.get("name", {}) if isinstance(current.get("name"), Mapping) else {}
        reference_name = reference.get("name", {}) if isinstance(reference.get("name"), Mapping) else {}
        if current_name.get("chs", "") != reference_name.get("chs", "") or current_name.get("eng", "") != reference_name.get("eng", ""):
            name_mismatches.append(
                {
                    "card_id": card_id,
                    "current": {"chs": current_name.get("chs", ""), "eng": current_name.get("eng", "")},
                    "reference": {"chs": reference_name.get("chs", ""), "eng": reference_name.get("eng", "")},
                }
            )

        reference_text = "\n".join(
            str(item.get("text_eng", ""))
            for item in reference.get("skill_texts", [])
            if isinstance(item, Mapping)
        )
        current_text_entries = current.get("text", {}).get("skill_texts", []) if isinstance(current.get("text"), Mapping) else []
        current_text = "\n".join(
            str(item.get("eng", ""))
            for item in current_text_entries
            if isinstance(item, Mapping)
        )
        if not reference_text:
            text_missing.append(card_id)
        elif reference_text == current_text:
            text_equal += 1
        else:
            text_different.append(
                {
                    "card_id": card_id,
                    "reference": reference_text,
                    "current": current_text,
                }
            )

    return {
        "reference_card_count": len(ref_ids),
        "current_card_count": len(current_ids),
        "overlap_count": len(overlap),
        "overlap_ids": overlap,
        "current_only_ids": sorted(current_ids - ref_ids),
        "reference_only_ids": sorted(ref_ids - current_ids),
        "core_field_mismatch_count": len(field_mismatches),
        "core_field_mismatches": field_mismatches,
        "name_mismatch_count": len(name_mismatches),
        "name_mismatches": name_mismatches,
        "english_text_equal_count": text_equal,
        "english_text_different_count": len(text_different),
        "english_text_differences": text_different,
        "english_text_missing_in_reference_count": len(text_missing),
        "english_text_missing_in_reference_ids": text_missing,
    }


def _rule_statistics(
    source_root: Path,
    files: Mapping[str, Any],
    reference_cards: Mapping[str, Mapping[str, Any]],
    current_ops: set[str],
) -> dict[str, Any]:
    observed: set[str] = set()
    for payload in files.values():
        _raw_kind_values(payload, observed)
    known = _reference_effect_kinds(source_root, observed)
    core_counter: Counter[str] = Counter()
    all_counter: Counter[str] = Counter()
    for payload in files.values():
        _walk_operation_kinds(payload, known, all_counter)
        for entry in _section_entries(payload, "rules"):
            _walk_operation_kinds(entry, known, core_counter)

    db_ids = {int(card_id) for card_id in reference_cards}
    rule_ids: set[int] = set()
    behavior_ids: set[int] = set()
    synthetic_rule_ids: set[int] = set()
    for payload in files.values():
        rules = _section_entries(payload, "rules")
        for entry in rules:
            if isinstance(entry, Mapping):
                card_id = _as_int(entry.get("card_id"))
                if card_id is not None:
                    rule_ids.add(card_id)
                    if card_id not in db_ids:
                        synthetic_rule_ids.add(card_id)
        for section in (
            "rules",
            "passives",
            "listeners",
            "emblems",
            "faiths",
            "fusions",
            "invocations",
            "activations",
            "union_bursts",
            "intrinsic_keywords",
            "vanilla_cards",
        ):
            for entry in _section_entries(payload, section):
                ids: set[int] = set()
                _walk_ids(entry, {"card_id", "source_card_id"}, ids)
                behavior_ids.update(ids)

    operation_compatibility: dict[str, dict[str, Any]] = {}
    for kind in sorted(all_counter):
        if kind in current_ops:
            operation_compatibility[kind] = {
                "status": "direct_name",
                "current_op": kind,
                "occurrences_all_definitions": all_counter[kind],
            }
        elif kind in SEMANTIC_OPERATION_MAP:
            target, note = SEMANTIC_OPERATION_MAP[kind]
            operation_compatibility[kind] = {
                "status": "adapter_required",
                "current_op": target,
                "note": note,
                "occurrences_all_definitions": all_counter[kind],
            }
        else:
            operation_compatibility[kind] = {
                "status": "schema_gap",
                "current_op": None,
                "occurrences_all_definitions": all_counter[kind],
            }

    return {
        "rule_file_count": len(files),
        "rule_entry_count": sum(len(_section_entries(payload, "rules")) for payload in files.values()),
        "unique_rule_card_id_count": len(rule_ids),
        "real_rule_card_id_count": len(rule_ids & db_ids),
        "synthetic_rule_card_id_count": len(synthetic_rule_ids),
        "synthetic_rule_card_ids": sorted(synthetic_rule_ids),
        "behavior_definition_card_id_count": len(behavior_ids & db_ids),
        "behavior_definition_card_ids_without_db": sorted(behavior_ids - db_ids),
        "effect_kind_enum_count": len(known),
        "effect_kinds_used_in_rules_count": len(core_counter),
        "effect_kinds_used_all_definitions_count": len(all_counter),
        "effect_occurrences_in_rules": sum(core_counter.values()),
        "effect_occurrences_all_definitions": sum(all_counter.values()),
        "effect_kinds_used_in_rules": dict(core_counter.most_common()),
        "effect_kinds_used_all_definitions": dict(all_counter.most_common()),
        "current_v2_operation_count": len(current_ops),
        "literal_operation_intersection": sorted(set(all_counter) & current_ops),
        "operation_compatibility": operation_compatibility,
    }


def build_import_artifacts(
    source_root: Path,
    *,
    current_catalog_path: Path | None = None,
    current_rules_path: Path | None = None,
    current_schema_path: Path | None = None,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    database_path = source_root / "data/cards.sqlite3"
    rules_dir = source_root / "data/rules"
    catalog, db_metadata = build_catalog_projection(database_path)
    rule_files, rule_manifest = _rule_file_payloads(rules_dir)
    reference_cards = catalog["cards"]

    current_cards = _load_current_catalog(current_catalog_path)
    current_rules = _load_current_rules(current_rules_path)
    current_ops = _load_current_ops(current_schema_path)
    catalog_comparison = _compare_catalogs(reference_cards, current_cards)
    rule_stats = _rule_statistics(source_root, rule_files, reference_cards, current_ops)
    audits = _load_audit_summaries(source_root)

    # The report remains stable across machines: source paths are relative and
    # timestamps are deliberately not included.
    rules_hash = _sha256_bytes(
        _json_bytes({path: rule_files[path] for path in sorted(rule_files)})
    )
    raw_rulebook = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "project": "SWB-RL",
            "rules_directory": "data/rules",
            "rules_sha256": rules_hash,
            "files": rule_manifest,
        },
        "files": {path: rule_files[path] for path in sorted(rule_files)},
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "project": "SWB-RL",
            "database": "data/cards.sqlite3",
            "database_sha256": db_metadata["database_sha256"],
            "rules_directory": "data/rules",
            "rules_sha256": rules_hash,
            "reference_rule_files": rule_manifest,
        },
        "current_inputs": {
            "catalog": current_catalog_path.name if current_catalog_path and current_catalog_path.exists() else None,
            "catalog_sha256": _sha256_file(current_catalog_path) if current_catalog_path and current_catalog_path.exists() else None,
            "rules": current_rules_path.name if current_rules_path and current_rules_path.exists() else None,
            "rules_sha256": _sha256_file(current_rules_path) if current_rules_path and current_rules_path.exists() else None,
            "schema": current_schema_path.name if current_schema_path and current_schema_path.exists() else None,
        },
        "summary": {
            **db_metadata,
            **catalog_comparison,
            **{
                key: value
                for key, value in rule_stats.items()
                if key not in {"effect_kinds_used_in_rules", "effect_kinds_used_all_definitions", "operation_compatibility"}
            },
            "current_rule_card_count": len(current_rules),
            "audit_summaries_available": sorted(audits),
        },
        "catalog_comparison": catalog_comparison,
        "rule_statistics": rule_stats,
        "audits": audits,
        "warnings": [
            "SWB-RL rule coverage is a static/audit source, not proof that every runtime boundary was sampled.",
            "Rule operations require an adapter before they can be executed by LethalCalculator.",
            "Current-only cards are intentionally not overwritten by this import.",
        ],
    }
    return {
        "catalog": catalog,
        "raw_rulebook": raw_rulebook,
        "report": report,
    }


def write_import_artifacts(artifacts: Mapping[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "catalog": output_dir / "swb_catalog_projection.json",
        "raw_rulebook": output_dir / "swb_rulebook_raw.json",
        "report": output_dir / "swb_compatibility_report.json",
    }
    for key, path in paths.items():
        path.write_text(
            json.dumps(artifacts[key], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    markdown_path = output_dir / "swb_compatibility_report.md"
    markdown_path.write_text(render_report_markdown(artifacts["report"]), encoding="utf-8")
    paths["markdown"] = markdown_path
    return paths


def render_report_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {})
    comparison = report.get("catalog_comparison", {})
    rules = report.get("rule_statistics", {})
    lines = [
        "# SWB-RL / LethalCalculator Compatibility Report",
        "",
        "This report is generated by `import_swb_rl.py`. It does not overwrite the current catalog or rules.",
        "",
        "## Catalog",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| SWB-RL cards | {summary.get('database_card_count', 0)} |",
        f"| SWB-RL collectible cards | {summary.get('database_collectible_count', 0)} |",
        f"| SWB-RL generated/token cards | {summary.get('database_generated_count', 0)} |",
        f"| Current catalog cards | {comparison.get('current_card_count', 0)} |",
        f"| ID overlap | {comparison.get('overlap_count', 0)} |",
        f"| Current-only IDs | {len(comparison.get('current_only_ids', []))} |",
        f"| Core field mismatches | {comparison.get('core_field_mismatch_count', 0)} |",
        f"| Name mismatches | {comparison.get('name_mismatch_count', 0)} |",
        f"| English text equal | {comparison.get('english_text_equal_count', 0)} |",
        f"| English text different | {comparison.get('english_text_different_count', 0)} |",
        f"| English text missing in reference | {comparison.get('english_text_missing_in_reference_count', 0)} |",
        "",
        "## RuleBook",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Rule JSON files | {rules.get('rule_file_count', 0)} |",
        f"| Rule entries | {rules.get('rule_entry_count', 0)} |",
        f"| Unique rule card IDs | {rules.get('unique_rule_card_id_count', 0)} |",
        f"| Real DB rule IDs | {rules.get('real_rule_card_id_count', 0)} |",
        f"| Synthetic/test rule IDs | {rules.get('synthetic_rule_card_id_count', 0)} |",
        f"| Effect kinds used in rules | {rules.get('effect_kinds_used_in_rules_count', 0)} |",
        f"| Effect kinds in all definitions | {rules.get('effect_kinds_used_all_definitions_count', 0)} |",
        f"| Current v2 operations | {rules.get('current_v2_operation_count', 0)} |",
        f"| Literal operation-name intersection | {len(rules.get('literal_operation_intersection', []))} |",
        "",
        "## Operation adapter status",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    status_counts = Counter(
        item.get("status")
        for item in rules.get("operation_compatibility", {}).values()
        if isinstance(item, Mapping)
    )
    for status in ("direct_name", "adapter_required", "schema_gap"):
        lines.append(f"| {status} | {status_counts.get(status, 0)} |")
    lines.extend(
        [
            "",
            "## Warnings",
            "",
        ]
    )
    for warning in report.get("warnings", []):
        lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _default_path(value: Path, *, root: Path) -> Path:
    return value if value.is_absolute() else root / value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="SWB-RL checkout containing data/cards.sqlite3 and data/rules",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    current_catalog = _default_path(args.catalog, root=root)
    current_rules = _default_path(args.rules, root=root)
    current_schema = _default_path(args.schema, root=root)
    output_dir = _default_path(args.output_dir, root=root)
    artifacts = build_import_artifacts(
        args.source,
        current_catalog_path=current_catalog,
        current_rules_path=current_rules,
        current_schema_path=current_schema,
    )
    paths = write_import_artifacts(artifacts, output_dir)
    summary = artifacts["report"]["summary"]
    print(
        "SWB-RL import complete: "
        f"{summary['database_card_count']} reference cards, "
        f"{summary['overlap_count']} ID overlap, "
        f"{len(summary['current_only_ids'])} current-only; "
        f"{summary['core_field_mismatch_count']} core field mismatches."
    )
    for path in paths.values():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
