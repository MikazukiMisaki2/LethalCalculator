"""Adapt a ShadowverseTracker public snapshot to the solver boundary.

The adapter is deliberately one-way: the solver does not import Tracker's
memory reader.  ``legal_actions`` remains attached to the result so callers
can refuse to solve when the snapshot is stale, incomplete, or not our turn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from lethal_models import LethalFollower, LethalHandCard, LethalState


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _map_by_id(values: Mapping[Any, Any] | None) -> dict[int, dict[str, Any]]:
    if not isinstance(values, Mapping):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for key, value in values.items():
        if isinstance(value, Mapping):
            try:
                result[int(key)] = dict(value)
            except (TypeError, ValueError):
                continue
    return result


@dataclass(frozen=True)
class SnapshotAdapterResult:
    state: LethalState
    legal_actions: dict[str, Any] | None
    is_ally_turn: bool
    usable: bool
    unsupported_card_ids: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()


class SnapshotAdapter:
    """Convert a Tracker snapshot without importing Tracker internals."""

    @classmethod
    def adapt(
        cls,
        snapshot: Mapping[str, Any],
        *,
        catalog: Mapping[str, Any] | None = None,
        rules: Mapping[str, Any] | None = None,
    ) -> SnapshotAdapterResult:
        root = snapshot.get("root") if isinstance(snapshot, Mapping) else None
        players = root.get("players") if isinstance(root, Mapping) else None
        if not isinstance(players, (list, tuple)) or len(players) < 2:
            raise ValueError("Tracker snapshot does not contain two players")
        mine = players[0] if isinstance(players[0], Mapping) else {}
        enemy = players[1] if isinstance(players[1], Mapping) else {}
        legal_raw = snapshot.get("legal_actions")
        legal = dict(legal_raw) if isinstance(legal_raw, Mapping) else None
        is_ally_turn = _bool(root.get("is_ally_turn"))
        warnings: list[str] = []
        if legal is None:
            warnings.append("legal_actions unavailable; result must not be treated as confirmed")
        if not is_ally_turn:
            warnings.append("snapshot is not the ally's turn")

        catalog_cards = _map_by_id(catalog.get("cards") if isinstance(catalog, Mapping) else None)
        rule_cards = _map_by_id(rules.get("rules") if isinstance(rules, Mapping) else rules)
        unsupported: set[int] = set()

        def hand_card(raw: Mapping[str, Any]) -> LethalHandCard:
            uid = _int(raw.get("unique_id"))
            cid = _int(raw.get("card_id"))
            info = rule_cards.get(cid)
            meta = catalog_cards.get(cid, {})
            meta_type = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}.get(meta.get("type"))
            resolved_type = _int(raw.get("card_type"), meta_type if meta_type is not None else 1)
            if info is not None:
                from lethal_models import create_hand_card_from_rule
                card = create_hand_card_from_rule(cid, info, uid)
                meta_tribes = meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ()
                enhance_costs = raw.get("enhance_costs", ())
                accelerate_costs = raw.get("accelerate_costs", ())
                crystallize_costs = raw.get("crystal_costs", raw.get("crystallize_costs", ()))
                def first_cost(value: Any) -> int | None:
                    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
                        return value[0]
                    return value if isinstance(value, int) else None
                return replace(
                    card,
                    name=str((meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}).get("chs") or (meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}).get("eng") or card.name),
                    cost=_int(raw.get("cost"), card.cost),
                    type=resolved_type,
                    atk=_int(raw.get("attack"), card.atk),
                    life=_int(raw.get("life"), card.life),
                    tribes=tuple(str(item) for item in meta_tribes) or card.tribes,
                    spell_boost_count=_int(raw.get("spell_boost_count"), card.spell_boost_count),
                    has_spell_boost=_bool(raw.get("has_spell_boost"), card.has_spell_boost),
                    variable_x=_int(raw.get("variable_x"), card.variable_x),
                    supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else card.supplement_info,
                    enhance_cost=first_cost(enhance_costs) if first_cost(enhance_costs) is not None else card.enhance_cost,
                    accelerate_cost=first_cost(accelerate_costs) if first_cost(accelerate_costs) is not None else card.accelerate_cost,
                    crystallize_cost=first_cost(crystallize_costs) if first_cost(crystallize_costs) is not None else card.crystallize_cost,
                )
            if not info:
                unsupported.add(cid)
            stats = meta.get("stats", {}) if isinstance(meta.get("stats"), Mapping) else {}
            name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
            card_type = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}.get(meta.get("type"), _int(raw.get("card_type"), 1))
            return LethalHandCard(
                unique_id=uid,
                card_id=cid,
                name=str(name.get("chs") or name.get("eng") or cid),
                cost=_int(raw.get("cost"), _int(meta.get("cost"))),
                type=card_type,
                atk=_int(raw.get("attack"), _int(stats.get("attack"))),
                life=_int(raw.get("life"), _int(stats.get("life"))),
                tribes=tuple(str(item) for item in (meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ())),
                spell_boost_count=_int(raw.get("spell_boost_count")),
                has_spell_boost=_bool(raw.get("has_spell_boost")),
                variable_x=_int(raw.get("variable_x")),
                supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else (),
            )

        def follower(raw: Mapping[str, Any], *, enemy_side: bool = False) -> LethalFollower:
            uid = _int(raw.get("unique_id"))
            cid = _int(raw.get("card_id"))
            meta = catalog_cards.get(cid, {})
            rule = rule_cards.get(cid, {}) if isinstance(rule_cards.get(cid, {}), Mapping) else {}
            static_keywords = set(rule.get("static_keywords", ())) if isinstance(rule.get("static_keywords", ()), (list, tuple)) else set()
            name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
            attacked = _bool(raw.get("has_attacked"))
            attack_limit = _int(raw.get("attack_limit"))
            attacks_left = max(0, attack_limit - int(attacked)) if attack_limit > 0 else int(not attacked)
            legal_storm = uid in set(legal.get("can_attack_leader_cards", ())) if legal and not enemy_side else False
            legal_rush = uid in set(legal.get("can_attack_field_cards", ())) if legal and not enemy_side else False
            has_storm = _bool(raw.get("has_storm"), legal_storm or "storm" in static_keywords)
            has_rush = _bool(raw.get("has_rush"), legal_rush or "rush" in static_keywords or has_storm)
            can_attack_leader = _bool(raw.get("can_attack_leader"), legal_storm or has_storm)
            can_attack_field = _bool(raw.get("can_attack_field"), legal_rush or has_rush)
            return LethalFollower(
                unique_id=uid,
                card_id=cid,
                name=str(name.get("chs") or name.get("eng") or cid),
                atk=_int(raw.get("attack")),
                hp=_int(raw.get("life")),
                has_storm=has_storm,
                has_rush=has_rush,
                is_ward=_bool(raw.get("has_guard"), "ward" in static_keywords),
                is_evolved=_int(raw.get("evolve_state")) > 0,
                is_super_evolved=_bool(raw.get("is_super_evolved")) or _int(raw.get("evolve_state")) >= 2,
                can_attack_leader=can_attack_leader,
                can_attack_field=can_attack_field,
                attacks_left=attacks_left,
                damage_cap=None,
                countdown=(_int(raw.get("countdown"), _int(raw.get("remaining_countdown"), _int(raw.get("count"), -1))) if any(key in raw for key in ("countdown", "remaining_countdown", "count")) else None),
                abilities_removed=_bool(raw.get("abilities_removed")) or _bool(raw.get("has_no_abilities")),
                statuses=tuple(str(item) for item in (raw.get("statuses", ()) if isinstance(raw.get("statuses"), (list, tuple)) else ())),
                last_words=tuple(dict(item) for item in (raw.get("last_words", ()) if isinstance(raw.get("last_words"), (list, tuple)) else ()) if isinstance(item, Mapping)),
                base_cost=_int(raw.get("base_cost"), _int(meta.get("cost"))),
                spell_boost_count=_int(raw.get("spell_boost_count")),
                has_spell_boost=_bool(raw.get("has_spell_boost")),
                variable_x=_int(raw.get("variable_x")),
                supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else (),
            )

        my_hand = [hand_card(card) for card in mine.get("hand", ()) if isinstance(card, Mapping)]
        my_board = [follower(card) for card in mine.get("field", ()) if isinstance(card, Mapping)]
        enemy_board = [follower(card, enemy_side=True) for card in enemy.get("field", ()) if isinstance(card, Mapping)]
        crest_ids = []
        crest_instances: list[dict[str, Any]] = []
        raw_crests = []
        for source in (mine.get("crests", ()), mine.get("extra_crests", ())):
            if isinstance(source, Mapping):
                source = list(source.values())
            if isinstance(source, (list, tuple)):
                raw_crests.extend(source)
        for crest in raw_crests:
            if isinstance(crest, int):
                crest = {"card_id": crest}
            if isinstance(crest, Mapping) and isinstance(crest.get("card_id"), int):
                crest_ids.append(crest["card_id"])
                instance = {
                    "card_id": crest["card_id"],
                    "unique_id": _int(crest.get("unique_id")),
                    "countdown": _int(crest.get("countdown"), -1),
                    "faith_value": _int(crest.get("faith_value")),
                    "variable_x": _int(crest.get("variable_x")),
                    "supplement_info": dict(crest.get("supplement_info", {})) if isinstance(crest.get("supplement_info"), Mapping) else {},
                }
                if isinstance(crest.get("abilities"), (list, tuple)):
                    instance["abilities"] = [dict(item) for item in crest["abilities"] if isinstance(item, Mapping)]
                crest_instances.append(instance)
        raw_faith_instances = mine.get("faith_instances", mine.get("faith_resources", mine.get("faiths", ())))
        if isinstance(raw_faith_instances, Mapping):
            raw_faith_instances = raw_faith_instances.get("instances", raw_faith_instances.get("active", ()))
        raw_faith_value = mine.get("faith", mine.get("faith_value", 0))
        if isinstance(raw_faith_value, Mapping):
            if not raw_faith_instances:
                raw_faith_instances = raw_faith_value.get("instances", raw_faith_value.get("active", ()))
            faith_scalar_value = raw_faith_value.get("value", raw_faith_value.get("total", 0))
        else:
            faith_scalar_value = raw_faith_value
        faith_instances = [dict(item) for item in raw_faith_instances if isinstance(item, Mapping)] if isinstance(raw_faith_instances, (list, tuple)) else []
        for item in faith_instances:
            if "value" not in item and "faith_value" in item:
                item["value"] = _int(item.get("faith_value"))
        faith_scalar = faith_scalar_value
        if not isinstance(faith_scalar, int):
            faith_scalar = sum(_int(item.get("value"), _int(item.get("faith_value"))) for item in faith_instances)
        earth_sigil = mine.get("earth_sigil", mine.get("earth_rite"))
        if not isinstance(earth_sigil, int):
            earth_sigil = sum(1 for raw in mine.get("field", ()) if isinstance(raw, Mapping) and _bool(raw.get("is_earth_sigil")))

        state = LethalState(
            enemy_hp=_int(enemy.get("life")),
            pp=_int(mine.get("pp")),
            max_pp=_int(mine.get("max_pp")),
            ep=_int(mine.get("evolve_points")),
            sep=_int(mine.get("super_evolve_points")),
            rally=_int(mine.get("rally")),
            cemetery=_int(mine.get("cemetery_count")),
            is_awakening=_bool(mine.get("is_awakening", mine.get("awakening"))),
            play_count=_int(mine.get("play_count")),
            faith=_int(faith_scalar),
            evolved_allies_this_turn=_int(mine.get("evolved_allies_this_turn", 0)),
            evolved_allies_this_match=_int(mine.get("evolved_allies_this_match", mine.get("evolved_count", 0))),
            my_board=my_board,
            enemy_board=enemy_board,
            hand=my_hand,
            deck_distribution={int(key): _int(value) for key, value in (mine.get("deck_distribution", {}) or {}).items() if str(key).lstrip("-").isdigit() and _int(value) > 0} if isinstance(mine.get("deck_distribution"), Mapping) else {},
            total_deck_count=_int(mine.get("deck_count")),
            active_crests=crest_ids,
            ally_hp=_int(mine.get("life")),
            ally_max_hp=_int(mine.get("max_life"), _int(mine.get("life"))),
            extra_pp=_int(mine.get("extra_pp"), _int(mine.get("preparation_extra_pp"))),
            earth_sigil=_int(earth_sigil),
            skybound_art=_int(mine.get("skybound_art", mine.get("skybound_art_gauge", 0))),
            super_skybound_art=_int(mine.get("super_skybound_art", mine.get("super_skybound_art_gauge", 0))),
            faith_instances=faith_instances,
            crest_instances=crest_instances,
        )
        # ShadowverseTracker's authoritative public field is
        # ``destroyed_card_ids``: a sequence of ``(card_id, style_id)``
        # tuples.  Older snapshots used a richer mapping under one of the
        # aliases below.  Prefer the authoritative field and preserve every
        # occurrence (duplicate card ids are distinct reanimation slots).
        destroyed_field_present = any(key in mine for key in ("destroyed_card_ids", "destroyed_this_match", "destroyed_history", "destroyed_cards"))
        raw_destroyed = mine.get("destroyed_card_ids", mine.get("destroyed_this_match", mine.get("destroyed_history", mine.get("destroyed_cards", ()))))
        if isinstance(raw_destroyed, Mapping):
            raw_destroyed = list(raw_destroyed.values())
        if isinstance(raw_destroyed, (list, tuple)):
            historical: list[LethalFollower] = []
            # Synthetic negative ids avoid colliding with live Tracker entity
            # ids while keeping the sequence stable for memoisation.
            for index, item in enumerate(raw_destroyed):
                if isinstance(item, Mapping):
                    historical_item = follower(item)
                    if historical_item.unique_id == 0:
                        # Rich fixture/history mappings often carry only a
                        # card id.  Give each occurrence its own stable
                        # synthetic identity so duplicate deaths remain
                        # separate reanimation slots.
                        historical_item = replace(historical_item, unique_id=-(index + 1))
                    historical.append(historical_item)
                    continue
                if isinstance(item, (list, tuple)) and item:
                    card_id = _int(item[0])
                    style_id = _int(item[1]) if len(item) > 1 else 0
                    meta = catalog_cards.get(card_id, {})
                    stats = meta.get("stats", {}) if isinstance(meta.get("stats"), Mapping) else {}
                    name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
                    rule = rule_cards.get(card_id, {}) if isinstance(rule_cards.get(card_id, {}), Mapping) else {}
                    static = set(rule.get("static_keywords", ())) if isinstance(rule.get("static_keywords", ()), (list, tuple)) else set()
                    historical.append(LethalFollower(
                        unique_id=-(index + 1),
                        card_id=card_id,
                        name=str(name.get("chs") or name.get("eng") or card_id),
                        atk=_int(stats.get("attack")),
                        hp=_int(stats.get("life")),
                        has_storm="storm" in static,
                        has_rush="rush" in static or "storm" in static,
                        is_ward="ward" in static,
                        can_attack_leader="storm" in static,
                        can_attack_field="rush" in static or "storm" in static,
                        attacks_left=0,
                        base_cost=_int(meta.get("cost")),
                        statuses=(f"historical_style:{style_id}",),
                    ))
            state.destroyed_this_match = historical
        state.destroyed_pool_known = bool(destroyed_field_present)
        # ShadowverseTracker's ``destroyed_card_ids`` is the public destroyed
        # pool used by Reanimate.  Preserve multiplicity and style ids; a
        # Reanimate does not consume an entry, so the same pool remains
        # available to subsequent effects.  Keep the legacy field as a
        # compatibility projection for callers that still inspect it.
        state.destroyed_pool_exact = bool(destroyed_field_present)
        if state.destroyed_pool_known and not state.destroyed_this_match:
            # An explicitly empty Tracker array is meaningful: Reanimate is a
            # known no-op, not an unknown-information gap.
            state.destroyed_this_match = []
        if state.deck_distribution and not state.total_deck_count:
            state.total_deck_count = sum(state.deck_distribution.values())
        if len(my_board) > 5:
            warnings.append("ally field contains more than five cards")
        if unsupported:
            warnings.append(f"{len(unsupported)} hand card(s) have no executable rule")
        usable = bool(legal is not None and is_ally_turn)
        return SnapshotAdapterResult(state, legal, is_ally_turn, usable, tuple(sorted(unsupported)), tuple(warnings))


__all__ = ["SnapshotAdapter", "SnapshotAdapterResult"]
