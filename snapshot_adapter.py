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


def _has(raw: Mapping[str, Any], *keys: str) -> bool:
    """Return whether any alias is present (presence differs from false)."""
    return any(key in raw for key in keys)


def _get_alias(raw: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Read the first present spelling without treating a false value as absent."""
    for key in keys:
        if key in raw:
            return raw[key]
    return default


def _copy_json(value: Any) -> Any:
    """Copy/normalize Tracker dataclass output without losing rich fields."""
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_copy_json(item) for item in value]
    return value


def _full_buff(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the complete ``CardBuff``/``PlayerBuff`` payload if exposed.

    The Tracker currently emits a mapping under ``buff``.  A few historical
    snapshots used ``buffs`` as a list, so retain that data under ``entries``
    rather than silently discarding it.  The solver's current attack/life
    values remain authoritative; this payload is metadata for exact copies,
    UI inspection, and future effect semantics.
    """
    value = raw.get("buff")
    if value is None:
        value = raw.get("buffs")
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _copy_json(value)
    if isinstance(value, (list, tuple)):
        return {"entries": _copy_json(value)}
    return {"value": value}


def _number(value: Any, default: int = 0) -> int:
    """Read a numeric Tracker field without treating booleans as numbers."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _player_damage_modifier(raw: Mapping[str, Any]) -> int:
    """Project a leader's existing damage-cut/increase buff into the state.

    ``PlayerBuff`` is serialized by Tracker as ``damage_cut`` and
    ``increase_damage``.  The interpreter stores one net modifier which is
    added to each subsequent damage event, so a cut is represented as a
    negative value.  Unknown buff keys remain preserved by ``_full_buff`` but
    do not get guessed here.
    """
    buff = _full_buff(raw)
    if not isinstance(buff, Mapping):
        buff = raw
    increase = _number(
        buff.get("increase_damage", buff.get("damage_increase", buff.get("increase_damage_taken", 0)))
    )
    cut = _number(buff.get("damage_cut", buff.get("damage_reduction", buff.get("damage_cut_amount", 0))))
    # Tracker's PlayerBuff uses -1 as the idle/sentinel value for
    # ``damage_cut`` (and some older builds use it for the increase field as
    # well).  Treating that sentinel as a real negative cut incorrectly adds
    # one damage to every attack/spell, which was the source of the observed
    # ``1 HP -> -2`` route and false lethal lines.
    if increase < 0:
        increase = 0
    if cut < 0:
        cut = 0
    return increase - cut


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return ()


def _int_sequence(value: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in _sequence(value) if isinstance(item, int) and not isinstance(item, bool))


def _manual_evolutions_this_turn(raw: Mapping[str, Any], turn: int | None, fallback: int) -> int:
    """Count player-initiated evolutions without mistaking auto-evolve for one.

    ``PlayerState.is_evolved_this_turn`` is intentionally broad and becomes
    true for an automatic card effect as well as a manual EP/SEP action.  The
    Tracker service attaches ``_recent_actions`` (and older exports use
    ``recent_actions``), which carries the action kind and turn.  Prefer that
    stream; when it is absent, retain the conservative boolean projection so
    hand-built/legacy snapshots never gain an extra manual evolution for free.
    """
    actions = raw.get("_recent_actions", raw.get("recent_actions"))
    if not isinstance(actions, (list, tuple)) or turn is None:
        return max(0, int(fallback))
    count = 0
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        action_turn = action.get("turn", action.get("current_turn"))
        if not isinstance(action_turn, int) or action_turn != turn:
            continue
        kind = str(action.get("kind", action.get("type", ""))).casefold()
        if any(token in kind for token in ("手动进化", "手动超进化", "manual evolve", "manual_evolve", "manual super")):
            count += 1
    return count


def _normalize_target_map(value: Any) -> dict[int, tuple[Any, ...]]:
    """Normalize Tracker ``AttackTargets`` while preserving target identity."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[int, tuple[Any, ...]] = {}
    for raw_attacker, raw_targets in value.items():
        try:
            attacker = int(raw_attacker)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_targets, Mapping):
            raw_targets = raw_targets.get("targets", raw_targets.get("target_ids", ()))
        if isinstance(raw_targets, (list, tuple, set)):
            targets: list[Any] = []
            for target in raw_targets:
                if isinstance(target, bool):
                    continue
                if isinstance(target, (int, str)):
                    targets.append(target)
                elif isinstance(target, Mapping):
                    candidate = target.get("unique_id", target.get("target_id", target.get("id")))
                    if isinstance(candidate, (int, str)) and not isinstance(candidate, bool):
                        targets.append(candidate)
            result[attacker] = tuple(targets)
    return result


def _keywords(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Read keyword/status names exposed by Tracker entity snapshots.

    Tracker versions have used ``statuses``, ``keywords`` and individual
    ``has_*`` flags interchangeably.  Keep the adapter tolerant and normalize
    the small canonical vocabulary consumed by the runtime.
    """
    values: list[str] = []
    for key in ("statuses", "keywords", "keyword_list", "abilities"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            # Some Tracker builds serialize keyword state as
            # ``{"storm": true, "bane": false}`` instead of a list.  Do not
            # project a disabled key into the runtime status tuple; when the
            # mapping is a metadata object with no boolean values, retaining
            # its keys keeps compatibility with the older representation.
            boolean_values = [item for item in value.values() if isinstance(item, bool)]
            value = (
                [item for item, enabled in value.items() if enabled]
                if boolean_values
                else value.keys()
            )
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if isinstance(item, (str, int)))
    for key in ("status", "keyword"):
        if isinstance(raw.get(key), str):
            values.append(raw[key])
    aliases = {
        "必杀": "bane", "毁灭": "bane", "虹吸": "drain", "吸血": "drain",
        "潜行": "ambush", "突袭": "ambush", "疾驰": "storm", "突进": "rush",
        "守护": "ward", "bane": "bane", "drain": "drain", "ambush": "ambush",
        "storm": "storm", "rush": "rush", "ward": "ward",
    }
    normalized = {aliases.get(value.casefold(), aliases.get(value, value.casefold())) for value in values}
    # Accept both the historical ``has_*`` names and the aliases emitted by
    # newer Tracker snapshots.  A boolean flag is only a positive assertion;
    # an absent/false flag does not erase a keyword listed in ``statuses``.
    for key, keyword in (
        ("has_bane", "bane"), ("is_bane", "bane"),
        ("has_killer", "bane"),
        ("has_drain", "drain"), ("is_drain", "drain"),
        ("has_ambush", "ambush"), ("is_ambush", "ambush"), ("is_stealth", "ambush"), ("has_sneak", "ambush"),
        ("has_storm", "storm"), ("is_storm", "storm"),
        ("has_rush", "rush"), ("is_rush", "rush"),
        ("has_guard", "ward"), ("has_ward", "ward"), ("is_ward", "ward"),
        ("has_cant_be_attacked", "cant_be_attacked"),
        ("has_cant_select", "cant_select"),
        ("has_cant_attack", "cant_attack"),
        ("has_last_word", "last_words"),
        ("has_damage_cut", "damage_cut"),
        ("has_induction", "induction"),
        ("has_activation", "activation"),
        ("has_reduce_damage", "reduce_damage"),
        ("has_cant_destroy", "cant_destroy"),
        ("has_super_evolve_buff", "super_evolve_buff"),
        ("has_temp_shield", "temp_shield"),
    ):
        if _bool(raw.get(key)):
            normalized.add(keyword)
    return tuple(sorted(str(item) for item in normalized if str(item)))


def _disabled_keywords(raw: Mapping[str, Any]) -> set[str]:
    """Return explicitly disabled keyword names from boolean status maps."""
    aliases = {
        "必杀": "bane", "毁灭": "bane", "虹吸": "drain", "吸血": "drain",
        "潜行": "ambush", "突袭": "ambush", "疾驰": "storm", "突进": "rush",
        "守护": "ward",
    }
    disabled: set[str] = set()
    for key in ("statuses", "keywords", "keyword_list", "abilities"):
        value = raw.get(key)
        if not isinstance(value, Mapping):
            continue
        for item, enabled in value.items():
            if enabled is False:
                text = str(item).strip().casefold()
                disabled.add(aliases.get(text, text))
    return disabled


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


def _catalog_with_evolution_aliases(values: Mapping[Any, Any] | None) -> dict[int, dict[str, Any]]:
    """Index catalog entries by both their printed and evolution ids.

    ``cards.json``/the generated catalog intentionally stores one canonical
    entry per card (normally the id ending in ``0``), while Tracker entities
    keep the evolution variant id (normally ending in ``1``).  The variant is
    therefore an alias for lookup purposes; rules remain keyed by the base id
    and are normalized separately by :func:`_base_card_id`.
    """
    result = _map_by_id(values)
    for base_id, item in tuple(result.items()):
        evolution_id = item.get("evolves_to")
        if not isinstance(evolution_id, int) or isinstance(evolution_id, bool) or evolution_id <= 0:
            continue
        if evolution_id in result:
            continue
        variant = dict(item)
        variant["card_id"] = evolution_id
        variant["base_card_id"] = int(item.get("base_card_id", base_id) or base_id)
        result[evolution_id] = variant
    return result


def _base_card_id(card_id: int, catalog_cards: Mapping[int, Mapping[str, Any]]) -> int:
    """Resolve a Tracker entity/style id to the catalog/rule base id.

    Prefer explicit ``base_card_id`` metadata.  The arithmetic fallback is
    deliberately used only when that canonical base is actually present in
    the catalog, avoiding accidental normalization of unrelated token ids.
    """
    cid = int(card_id)
    meta = catalog_cards.get(cid)
    if isinstance(meta, Mapping):
        explicit = meta.get("base_card_id")
        if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0 and explicit in catalog_cards:
            return int(explicit)
    canonical = (cid // 10) * 10
    if canonical in catalog_cards:
        candidate = catalog_cards.get(canonical)
        if isinstance(candidate, Mapping):
            evolution_id = candidate.get("evolves_to")
            if evolution_id == cid:
                return canonical
    # A catalog entry may be supplied only under the variant id but still
    # carry an explicit base id that was not indexed in this small fixture.
    if isinstance(meta, Mapping):
        explicit = meta.get("base_card_id")
        if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
            return int(explicit)
    return cid


@dataclass(frozen=True)
class SnapshotAdapterResult:
    state: LethalState
    legal_actions: dict[str, Any] | None
    is_ally_turn: bool
    usable: bool
    unsupported_card_ids: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()
    # ``trusted`` is stricter than ``usable``: a complete, current-turn
    # snapshot may still be unusable when it is the opponent's turn.  The
    # engine/UI must never infer missing values for a non-trusted snapshot.
    trusted: bool = False
    trust_reasons: tuple[str, ...] = ()

    @property
    def trustworthy(self) -> bool:
        """Compatibility/readability alias used by UI callers."""
        return self.trusted

    @property
    def is_trusted(self) -> bool:
        return self.trusted

    @property
    def untrusted_reasons(self) -> tuple[str, ...]:
        return self.trust_reasons

    def targets_for(self, attacker_uid: int) -> tuple[Any, ...]:
        return tuple(self.state.legal_attack_targets.get(int(attacker_uid), ()))

    def modes_for(self, unique_id: int) -> tuple[str, ...]:
        return tuple(self.state.legal_modes.get(int(unique_id), ()))


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
        # TrackerService optionally writes JSONL records shaped as
        # ``{"timestamp": ..., "snapshot": {"root": ...}}``.  The live
        # callback passes the inner object directly, but accepting the
        # envelope here makes recorded snapshots usable as fixtures without
        # weakening the trust checks below.
        if isinstance(snapshot, Mapping) and "root" not in snapshot:
            nested = snapshot.get("snapshot")
            if isinstance(nested, Mapping):
                snapshot = nested
        root = snapshot.get("root") if isinstance(snapshot, Mapping) else None
        # Accept a bare ``BattleRoot.to_dict()`` as well as the public
        # ``read_battle_model`` envelope.  This is convenient for callers
        # that already extracted the root before invoking the adapter.
        if root is None and isinstance(snapshot, Mapping) and isinstance(snapshot.get("players"), (list, tuple)):
            root = snapshot
        players = root.get("players") if isinstance(root, Mapping) else None
        structural_reasons: list[str] = []
        if not isinstance(root, Mapping):
            structural_reasons.append("root missing or not an object")
            root = {}
        if not isinstance(players, (list, tuple)):
            structural_reasons.append("root.players missing or not an array")
            players = []
        if len(players) < 2:
            structural_reasons.append("root.players must contain ally and enemy")
        mine = players[0] if players and isinstance(players[0], Mapping) else {}
        enemy = players[1] if len(players) > 1 and isinstance(players[1], Mapping) else {}
        if len(players) >= 1 and not isinstance(players[0], Mapping):
            structural_reasons.append("root.players[0] is not an object")
        if len(players) >= 2 and not isinstance(players[1], Mapping):
            structural_reasons.append("root.players[1] is not an object")
        legal_raw = None
        if isinstance(snapshot, Mapping):
            candidate = snapshot.get("legal_actions")
            if not isinstance(candidate, Mapping):
                candidate = snapshot.get("LegalActions")
            if not isinstance(candidate, Mapping) and isinstance(root, Mapping):
                candidate = root.get("legal_actions", root.get("LegalActions"))
            legal_raw = candidate
        legal = dict(legal_raw) if isinstance(legal_raw, Mapping) else None
        is_ally_turn = _bool(_get_alias(root, "is_ally_turn", "isAllyTurn"))
        warnings: list[str] = []
        trust_reasons: list[str] = list(structural_reasons)
        if not _has(root, "is_ally_turn", "isAllyTurn"):
            trust_reasons.append("root.is_ally_turn missing")
        elif not isinstance(_get_alias(root, "is_ally_turn", "isAllyTurn"), bool):
            trust_reasons.append("root.is_ally_turn must be boolean")
        if legal is None:
            warnings.append("legal_actions unavailable; result must not be treated as confirmed")
            trust_reasons.append("legal_actions missing")
        if not is_ally_turn:
            warnings.append("snapshot is not the ally's turn")

        catalog_raw = (
            catalog.get("cards")
            if isinstance(catalog, Mapping) and isinstance(catalog.get("cards"), Mapping)
            else catalog
        )
        rules_raw = (
            rules.get("rules")
            if isinstance(rules, Mapping) and isinstance(rules.get("rules"), Mapping)
            else rules
        )
        # Keep evolution ids addressable for metadata lookup, then normalize
        # every entity to its base id before consulting CardRules.
        catalog_cards = _catalog_with_evolution_aliases(catalog_raw)
        rule_cards = _map_by_id(rules_raw)
        unsupported: set[int] = set()

        # Tracker's canonical public spelling is ``attack_targets``. Accept
        # the older ``AttackTargets``/root/player projections too; when more
        # than one is present, the legal-action map wins because it is the
        # authoritative current-turn projection.
        # Presence is separate from the normalized mapping: ``{"201": []}``
        # is an authoritative statement that the follower currently has no
        # legal targets, whereas an absent field means the client did not
        # expose the target projection at all.
        attack_targets_present = False
        raw_mine_field = _get_alias(mine, "field", "Field", default=())
        if isinstance(legal, Mapping):
            attack_targets_present = "attack_targets" in legal or "AttackTargets" in legal
        if not attack_targets_present:
            attack_targets_present = any(
                isinstance(card, Mapping) and ("attack_targets" in card or "AttackTargets" in card)
                for card in (raw_mine_field if isinstance(raw_mine_field, (list, tuple)) else ())
            )
        if not attack_targets_present:
            attack_targets_present = "attack_targets" in mine or "AttackTargets" in mine or "attack_targets" in root or "AttackTargets" in root
        # Keep source presence separate from map truthiness.  An explicit
        # ``attack_targets: {}`` is authoritative (for example when all ally
        # followers are exhausted); it must not be replaced by a stale
        # FieldCard projection below.
        legal_target_source_present = False
        if isinstance(legal, Mapping) and "attack_targets" in legal:
            legal_target_source_present = True
            legal_attack_targets = _normalize_target_map(legal.get("attack_targets"))
        elif isinstance(legal, Mapping) and "AttackTargets" in legal:
            legal_target_source_present = True
            legal_attack_targets = _normalize_target_map(legal.get("AttackTargets"))
        else:
            legal_attack_targets = {}
        if not legal_target_source_present:
            for source in (mine.get("attack_targets"), mine.get("AttackTargets"), root.get("attack_targets"), root.get("AttackTargets")):
                legal_attack_targets = _normalize_target_map(source)
                if legal_attack_targets:
                    break
        if not legal_target_source_present and not legal_attack_targets:
            # FieldCard carries the same projection in Tracker's public
            # ``read_battle_model`` output.  Build the attacker->targets map
            # directly when the separate LegalActions payload is unavailable.
            field_target_map: dict[int, tuple[Any, ...]] = {}
            for raw_field in (raw_mine_field if isinstance(raw_mine_field, (list, tuple)) else ()):
                if not isinstance(raw_field, Mapping):
                    continue
                raw_uid = raw_field.get("unique_id")
                if not isinstance(raw_uid, int) or isinstance(raw_uid, bool):
                    continue
                raw_targets = raw_field.get("attack_targets", raw_field.get("AttackTargets"))
                normalized = _normalize_target_map({raw_uid: raw_targets})
                if raw_uid in normalized:
                    field_target_map[raw_uid] = normalized[raw_uid]
            legal_attack_targets = field_target_map

        def legal_ids(*keys: str) -> tuple[int, ...]:
            if not isinstance(legal, Mapping):
                return ()
            values: Any = None
            for key in keys:
                if key in legal:
                    values = legal.get(key)
                    break
            return tuple(int(item) for item in _sequence(values) if isinstance(item, int) and not isinstance(item, bool))

        can_play_uids = tuple(dict.fromkeys(legal_ids("can_play_cards") + legal_ids("can_play_cards_with_extra_pp")))
        can_enhance_uids = legal_ids("can_enhance_play_cards")
        can_accelerate_uids = legal_ids("can_accelerate_play_cards")
        can_crystallize_uids = legal_ids("can_crystal_play_cards", "can_crystallize_play_cards")
        can_fusion_uids = legal_ids("can_fusion_cards")
        can_evolve_uids = legal_ids("can_evolve_cards")
        can_super_evolve_uids = tuple(dict.fromkeys(legal_ids("can_super_evolve_cards") + legal_ids("can_super_evolve_with_skill_cards")))
        attacked_uids = legal_ids("attacked_cards")
        # A card with no legal action is still represented with an empty mode
        # tuple. This prevents the engine from falling back to a printed mode
        # when the Tracker explicitly says the card cannot be played.
        legal_modes: dict[int, tuple[str, ...]] = {}
        if legal is not None:
            raw_hand_for_modes = _get_alias(mine, "hand", "Hand", default=())
            for card in raw_hand_for_modes if isinstance(raw_hand_for_modes, (list, tuple)) else ():
                if not isinstance(card, Mapping):
                    continue
                uid = _int(card.get("unique_id"))
                modes: list[str] = []
                if uid in can_play_uids:
                    modes.append("normal")
                if uid in can_enhance_uids:
                    modes.append("enhance")
                if uid in can_accelerate_uids:
                    modes.append("accelerate")
                if uid in can_crystallize_uids:
                    modes.append("crystallize")
                if uid in can_fusion_uids:
                    # Fusion is exposed for UI/legal-action inspection.  The
                    # current lethal interpreter intentionally does not
                    # execute fusion payloads yet, so it is not included in
                    # ``EventInterpreter.available_modes``.
                    modes.append("fusion")
                # Some Tracker integrations expose explicit mode names on the
                # hand entity; prefer them when the legal-action lists are not
                # available for that particular client build.
                raw_modes = card.get("available_modes", card.get("legal_modes"))
                if not modes and isinstance(raw_modes, (list, tuple, set)):
                    modes = [str(item.get("kind", item.get("mode", ""))).casefold() if isinstance(item, Mapping) else str(item).casefold() for item in raw_modes]
                    modes = [item for item in modes if item in {"normal", "enhance", "accelerate", "crystallize", "fusion", "activation"}]
                legal_modes[uid] = tuple(dict.fromkeys(modes))

        def hand_card(raw: Mapping[str, Any]) -> LethalHandCard:
            uid = _int(raw.get("unique_id"))
            raw_cid = _int(raw.get("card_id"))
            raw_base = _int(raw.get("base_card_id"))
            cid = _base_card_id(raw_base or raw_cid, catalog_cards)
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
                enhance_costs_tuple = _int_sequence(enhance_costs)
                accelerate_costs_tuple = _int_sequence(accelerate_costs)
                crystallize_costs_tuple = _int_sequence(crystallize_costs)
                raw_keywords = set(_keywords(raw))
                disabled_keywords = _disabled_keywords(raw)
                raw_keywords.difference_update(disabled_keywords)
                raw_tribes = raw.get("tribes")
                tribes = tuple(str(item) for item in _sequence(raw_tribes)) if isinstance(raw_tribes, (list, tuple, set)) else tuple(str(item) for item in meta_tribes)
                def first_cost(value: Any) -> int | None:
                    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
                        return value[0]
                    return value if isinstance(value, int) else None
                # The live hand payload is authoritative for temporary
                # abilities and Buffs.  Preserve an explicitly empty mapping
                # (for example after a silence/ability replacement) instead
                # of falling back to the generated CardRules metadata.
                status_values = set(card.statuses) | raw_keywords
                status_values.difference_update(disabled_keywords)
                for flag, keyword in (
                    ("has_storm", "storm"), ("has_rush", "rush"),
                    ("has_guard", "ward"), ("has_bane", "bane"),
                    ("has_drain", "drain"), ("has_sneak", "ambush"),
                    ("has_ambush", "ambush"),
                ):
                    if flag in raw and raw.get(flag) is False:
                        status_values.discard(keyword)
                raw_buff = _full_buff(raw)
                return replace(
                    card,
                    name=str((meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}).get("chs") or (meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}).get("eng") or card.name),
                    cost=_int(raw.get("cost"), card.cost),
                    type=resolved_type,
                    atk=_int(raw.get("attack"), card.atk),
                    life=_int(raw.get("life"), card.life),
                    tribes=tribes or card.tribes,
                    spell_boost_count=_int(raw.get("spell_boost_count"), card.spell_boost_count),
                    has_spell_boost=_bool(raw.get("has_spell_boost"), card.has_spell_boost),
                    variable_x=_int(raw.get("variable_x"), card.variable_x),
                    supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else card.supplement_info,
                    statuses=tuple(sorted(status_values)),
                    has_bane=_bool(raw.get("has_bane"), "bane" in status_values),
                    has_drain=_bool(raw.get("has_drain"), "drain" in status_values),
                    has_ambush=_bool(raw.get("has_ambush"), "ambush" in status_values),
                    enhance_cost=first_cost(enhance_costs) if first_cost(enhance_costs) is not None else card.enhance_cost,
                    accelerate_cost=first_cost(accelerate_costs) if first_cost(accelerate_costs) is not None else card.accelerate_cost,
                    crystallize_cost=first_cost(crystallize_costs) if first_cost(crystallize_costs) is not None else card.crystallize_cost,
                    buff=raw_buff if ("buff" in raw or "buffs" in raw) else card.buff,
                    enhance_costs=enhance_costs_tuple if "enhance_costs" in raw else card.enhance_costs,
                    accelerate_costs=accelerate_costs_tuple if "accelerate_costs" in raw else card.accelerate_costs,
                    crystallize_costs=crystallize_costs_tuple if any(key in raw for key in ("crystal_costs", "crystallize_costs")) else card.crystallize_costs,
                )
            if not info:
                unsupported.add(cid)
            stats = meta.get("stats", {}) if isinstance(meta.get("stats"), Mapping) else {}
            name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
            card_type = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}.get(meta.get("type"), _int(raw.get("card_type"), 1))
            raw_keywords = set(_keywords(raw))
            raw_keywords.difference_update(_disabled_keywords(raw))
            raw_tribes = raw.get("tribes")
            tribes = tuple(str(item) for item in _sequence(raw_tribes)) if isinstance(raw_tribes, (list, tuple, set)) else tuple(str(item) for item in (meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ()))
            return LethalHandCard(
                unique_id=uid,
                card_id=cid,
                name=str(name.get("chs") or name.get("eng") or cid),
                cost=_int(raw.get("cost"), _int(meta.get("cost"))),
                type=card_type,
                atk=_int(raw.get("attack"), _int(stats.get("attack"))),
                life=_int(raw.get("life"), _int(stats.get("life"))),
                tribes=tribes,
                spell_boost_count=_int(raw.get("spell_boost_count")),
                has_spell_boost=_bool(raw.get("has_spell_boost")),
                variable_x=_int(raw.get("variable_x")),
                supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else (),
                statuses=tuple(sorted(raw_keywords)),
                has_bane="bane" in raw_keywords,
                has_drain="drain" in raw_keywords,
                has_ambush="ambush" in raw_keywords,
                buff=_full_buff(raw),
                enhance_costs=_int_sequence(raw.get("enhance_costs", ())),
                accelerate_costs=_int_sequence(raw.get("accelerate_costs", ())),
                crystallize_costs=_int_sequence(raw.get("crystal_costs", raw.get("crystallize_costs", ()))),
            )

        def follower(raw: Mapping[str, Any], *, enemy_side: bool = False) -> LethalFollower:
            uid = _int(raw.get("unique_id"))
            raw_cid = _int(raw.get("card_id"))
            # Field entities expose the evolution variant in ``card_id``;
            # normalize it to the base rule while retaining the live stats,
            # evolution state, buffs and target projection from Tracker.
            cid = _base_card_id(raw_cid, catalog_cards)
            meta = catalog_cards.get(cid, {})
            rule = rule_cards.get(cid, {}) if isinstance(rule_cards.get(cid, {}), Mapping) else {}
            abilities_removed = _bool(raw.get("abilities_removed")) or _bool(raw.get("has_no_abilities"))
            static_keywords = (
                {str(item).casefold() for item in rule.get("static_keywords", ())}
                if not abilities_removed and isinstance(rule.get("static_keywords", ()), (list, tuple, set))
                else set()
            )
            name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
            attacked = _bool(raw.get("has_attacked"))
            attack_limit = _int(raw.get("attack_limit"))
            raw_attacks_left = _int(raw.get("attacks_left"), -1)
            attacks_left = raw_attacks_left if raw_attacks_left >= 0 else (max(0, attack_limit - int(attacked)) if attack_limit > 0 else int(not attacked))
            raw_target_value = raw.get("attack_targets", raw.get("AttackTargets"))
            raw_target_map = _normalize_target_map({uid: raw_target_value}) if ("attack_targets" in raw or "AttackTargets" in raw) else {}
            attack_targets = legal_attack_targets.get(uid, raw_target_map.get(uid, ()))
            legal_leader_ids = set(legal_ids("can_attack_leader_cards"))
            legal_field_ids = set(legal_ids("can_attack_field_cards"))
            explicit_target_map = uid in legal_attack_targets or uid in raw_target_map
            enemy_leader_uid = _int(enemy.get("unique_id"), 0)
            leader_markers = {"leader", "enemy_leader", "enemy_leader_uid"}
            if enemy_leader_uid:
                leader_markers.add(enemy_leader_uid)
                leader_markers.add(str(enemy_leader_uid).casefold())
            target_allows_leader = any(target in leader_markers or str(target).casefold() in leader_markers for target in attack_targets)
            # LegalActions/AttackTargets are authoritative whenever present.
            # Do not resurrect a forbidden attack merely because the printed
            # card has Storm/Rush.
            legal_storm = uid in legal_leader_ids or (explicit_target_map and target_allows_leader)
            legal_rush = uid in legal_field_ids or (explicit_target_map and any(target not in leader_markers and str(target).casefold() not in leader_markers for target in attack_targets))
            # A silence/remove-all-abilities marker clears both printed and
            # previously granted keywords.  LegalActions still remains the
            # authoritative source for whether the entity can attack now.
            runtime_keywords = set() if abilities_removed else (set(_keywords(raw)) | static_keywords)
            runtime_keywords.difference_update(_disabled_keywords(raw))
            # A present false flag is an authoritative removal (for example
            # a silenced Storm/Bane entity). Do not leave the catalog keyword
            # in ``statuses`` where the interpreter would re-enable it.
            for flag, keyword in (
                ("has_storm", "storm"), ("has_rush", "rush"),
                ("has_guard", "ward"), ("has_bane", "bane"),
                ("has_drain", "drain"), ("has_sneak", "ambush"),
                ("has_ambush", "ambush"),
            ):
                if flag in raw and raw.get(flag) is False:
                    runtime_keywords.discard(keyword)
            has_storm = _bool(raw.get("has_storm")) if "has_storm" in raw else ("storm" in runtime_keywords)
            has_rush = _bool(raw.get("has_rush")) if "has_rush" in raw else ("rush" in runtime_keywords or has_storm)
            if abilities_removed:
                has_storm = False
                has_rush = False
            if not enemy_side and ("can_attack_leader_cards" in (legal or {}) or explicit_target_map):
                can_attack_leader = legal_storm
            elif "can_attack_leader" in raw:
                can_attack_leader = _bool(raw.get("can_attack_leader"))
            else:
                can_attack_leader = has_storm
            if not enemy_side and ("can_attack_field_cards" in (legal or {}) or explicit_target_map):
                can_attack_field = legal_rush
            elif "can_attack_field" in raw:
                can_attack_field = _bool(raw.get("can_attack_field"))
            else:
                can_attack_field = has_rush
            if not enemy_side and legal is not None and uid in attacked_uids and raw_attacks_left < 0:
                # ``attacked_cards`` records that at least one attack already
                # happened; it does not mean a multi-attack follower is
                # exhausted.  Use the Tracker attack limit when available,
                # otherwise retain one remaining attack only when the live
                # legal lists/target map still expose one.
                if attack_limit > 0:
                    attacks_left = max(0, attack_limit - 1)
                elif uid in legal_leader_ids or uid in legal_field_ids or attack_targets:
                    # Some Tracker versions do not expose ``attack_limit``.
                    # In that case the authoritative legal lists/target map
                    # still tell us that this entity has another attack.  The
                    # old fallback ``int(not attacked)`` incorrectly turned
                    # a multi-attack follower into zero attacks as soon as
                    # ``attacked_cards`` contained its UID.
                    attacks_left = max(1, attacks_left)
                else:
                    attacks_left = 0
            is_ward = _bool(raw.get("has_guard")) if "has_guard" in raw else ("ward" in runtime_keywords)
            has_bane = _bool(raw.get("has_bane"), "bane" in runtime_keywords)
            has_drain = _bool(raw.get("has_drain"), "drain" in runtime_keywords)
            has_ambush = _bool(raw.get("has_ambush"), "ambush" in runtime_keywords)
            if abilities_removed:
                is_ward = has_bane = has_drain = has_ambush = False
            return LethalFollower(
                unique_id=uid,
                card_id=cid,
                name=str(name.get("chs") or name.get("eng") or cid),
                atk=_int(raw.get("attack")),
                hp=_int(raw.get("life")),
                has_storm=has_storm,
                has_rush=has_rush,
                is_ward=is_ward,
                is_evolved=_bool(raw.get("is_evolved")) or _int(raw.get("evolve_state")) > 0,
                is_super_evolved=_bool(raw.get("is_super_evolved")) or _int(raw.get("evolve_state")) >= 2,
                can_attack_leader=can_attack_leader,
                can_attack_field=can_attack_field,
                attacks_left=attacks_left,
                damage_cap=None,
                countdown=(_int(raw.get("countdown"), _int(raw.get("remaining_countdown"), _int(raw.get("count"), -1))) if any(key in raw for key in ("countdown", "remaining_countdown", "count")) else None),
                abilities_removed=abilities_removed,
                statuses=tuple(sorted(runtime_keywords)),
                has_bane=has_bane,
                has_drain=has_drain,
                has_ambush=has_ambush,
                last_words=tuple(dict(item) for item in (raw.get("last_words", ()) if isinstance(raw.get("last_words"), (list, tuple)) else ()) if isinstance(item, Mapping)),
                base_cost=_int(raw.get("base_cost"), _int(meta.get("cost"))),
                spell_boost_count=_int(raw.get("spell_boost_count")),
                has_spell_boost=_bool(raw.get("has_spell_boost")),
                variable_x=_int(raw.get("variable_x")),
                supplement_info=tuple(sorted((str(key), int(value)) for key, value in (raw.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(raw.get("supplement_info"), Mapping) else (),
                progressive_sequence_index=_int(raw.get("progressive_sequence_index"), _int(raw.get("sequence_index"))),
                attack_targets=tuple(attack_targets),
                buff=_full_buff(raw),
            )

        hand_raw = _get_alias(mine, "hand", "Hand", default=())
        field_raw = _get_alias(mine, "field", "Field", default=())
        enemy_field_raw = _get_alias(enemy, "field", "Field", default=())
        if not isinstance(hand_raw, (list, tuple)):
            hand_raw = ()
            trust_reasons.append("ally hand missing or not an array")
        if not isinstance(field_raw, (list, tuple)):
            field_raw = ()
            trust_reasons.append("ally field missing or not an array")
        if not isinstance(enemy_field_raw, (list, tuple)):
            enemy_field_raw = ()
            trust_reasons.append("enemy field missing or not an array")
        my_hand = [hand_card(card) for card in hand_raw if isinstance(card, Mapping)]
        my_board = [follower(card) for card in field_raw if isinstance(card, Mapping)]
        enemy_board = [follower(card, enemy_side=True) for card in enemy_field_raw if isinstance(card, Mapping)]
        observed_attacked_uids = list(attacked_uids)
        for alias in ("attacked_cards", "attacked_card_uids", "attacked_with_follower_uids"):
            observed_attacked_uids.extend(_int_sequence(mine.get(alias)))
        observed_attacked_uids.extend(
            _int(card.get("unique_id"))
            for card in field_raw
            if isinstance(card, Mapping) and _bool(card.get("has_attacked")) and isinstance(card.get("unique_id"), int)
        )
        observed_attacked_uids = list(dict.fromkeys(observed_attacked_uids))
        attacked_this_turn = _bool(
            mine.get("attacked_with_follower_this_turn", mine.get("has_attacked_follower_this_turn"))
        ) or bool(observed_attacked_uids)

        # These values are the minimum information needed for a confirmed
        # one-turn search.  Defaults below keep the state inspectable, but any
        # missing/malformed critical value downgrades the adapter result so a
        # caller cannot accidentally treat a guessed zero as game truth.
        def require_int(obj: Mapping[str, Any], key: str, label: str, *aliases: str) -> None:
            value = _get_alias(obj, key, *aliases)
            if not isinstance(value, int) or isinstance(value, bool):
                trust_reasons.append(f"{label}.{key} missing or not an integer")

        require_int(mine, "life", "ally", "Life")
        require_int(enemy, "life", "enemy", "Life")
        for key, aliases in (
            ("pp", ("PP",)),
            ("max_pp", ("MaxPP",)),
            ("evolve_points", ("EP",)),
            ("super_evolve_points", ("SEP",)),
        ):
            require_int(mine, key, "ally", *aliases)
        # If unlock metadata is exposed, it must be numeric; silently
        # coercing a malformed value would make a legal/illegal evolve
        # decision unknowable.  The fields remain optional for legacy
        # snapshots that predate Tracker's PlayerState projection.
        for key, aliases in (
            ("evolve_turn", ("EvolveTurn", "evolve_unlock_turn", "evolution_unlock_turn")),
            ("super_evolve_turn", ("SuperEvolveTurn", "super_evolve_unlock_turn", "super_evolution_unlock_turn")),
        ):
            if _has(mine, key, *aliases):
                value = _get_alias(mine, key, *aliases)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    trust_reasons.append(f"ally.{key} must be a non-negative integer")
        if not _has(mine, "hand", "Hand"):
            trust_reasons.append("ally.hand missing")
        if not _has(mine, "field", "Field"):
            trust_reasons.append("ally.field missing")
        if not _has(enemy, "field", "Field"):
            trust_reasons.append("enemy.field missing")
        for zone_name, values in (("hand", hand_raw), ("ally.field", field_raw), ("enemy.field", enemy_field_raw)):
            for index, item in enumerate(values):
                if not isinstance(item, Mapping):
                    trust_reasons.append(f"{zone_name}[{index}] is not an object")
                    continue
                for key in ("unique_id", "card_id"):
                    if not isinstance(item.get(key), int) or isinstance(item.get(key), bool):
                        trust_reasons.append(f"{zone_name}[{index}].{key} missing or not an integer")
                    elif int(item.get(key)) <= 0:
                        trust_reasons.append(f"{zone_name}[{index}].{key} must be positive")
                if zone_name == "hand" and (
                    not isinstance(item.get("cost"), int)
                    or isinstance(item.get("cost"), bool)
                    or item.get("cost") < 0
                ):
                    # The printed Catalog cost is not a safe substitute for
                    # Tracker's current hand cost: discounts, Spellboost and
                    # mode modifiers can all change it.
                    trust_reasons.append(f"{zone_name}[{index}].cost missing or not an integer")
                if zone_name != "hand":
                    for key in ("attack", "life"):
                        if not isinstance(item.get(key), int) or isinstance(item.get(key), bool):
                            trust_reasons.append(f"{zone_name}[{index}].{key} missing or not an integer")
                    for target_key in ("attack_targets", "AttackTargets"):
                        if target_key not in item:
                            continue
                        targets = item.get(target_key)
                        if not isinstance(targets, (list, tuple, set)):
                            trust_reasons.append(f"{zone_name}[{index}].{target_key} is not an array")
                        elif any(not isinstance(target, (int, str)) or isinstance(target, bool) for target in targets):
                            trust_reasons.append(f"{zone_name}[{index}].{target_key} contains an invalid target UID")
        for key, aliases in (
            ("cemetery_count", ("cemetery", "Cemetery")),
            ("rally", ("Rally", "rally_count")),
            ("play_count", ("PlayCount", "plays_this_turn")),
            ("is_awakening", ("awakening", "Awakening")),
        ):
            if key == "is_awakening" and not _has(mine, key, *aliases):
                remaining = _get_alias(mine, "remaining_pp_until_awakening")
                if isinstance(remaining, int) and not isinstance(remaining, bool):
                    continue
            if not _has(mine, key, *aliases):
                trust_reasons.append(f"ally.{key} missing")
        if legal is not None:
            for key in (
                "can_play_cards", "can_play_cards_with_extra_pp", "can_enhance_play_cards",
                "can_accelerate_play_cards", "can_crystal_play_cards", "can_attack_leader_cards",
                "can_attack_field_cards", "attacked_cards", "can_evolve_cards",
                "can_super_evolve_cards", "attack_targets",
            ):
                if key not in legal and not (key == "can_crystal_play_cards" and "can_crystallize_play_cards" in legal) and not (key == "attack_targets" and "AttackTargets" in legal):
                    trust_reasons.append(f"legal_actions.{key} missing")
            for key in (
                "can_play_cards", "can_play_cards_with_extra_pp", "can_enhance_play_cards",
                "can_accelerate_play_cards", "can_crystal_play_cards", "can_crystallize_play_cards",
                "can_attack_leader_cards", "can_attack_field_cards", "attacked_cards",
                "can_evolve_cards", "can_super_evolve_cards", "can_super_evolve_with_skill_cards",
            ):
                if key in legal and not isinstance(legal.get(key), (list, tuple, set)):
                    trust_reasons.append(f"legal_actions.{key} is not an array")
                elif key in legal:
                    for value in _sequence(legal.get(key)):
                        if not isinstance(value, int) or isinstance(value, bool):
                            trust_reasons.append(f"legal_actions.{key} contains a non-integer UID")
                            break
            if "attack_targets" in legal and not isinstance(legal.get("attack_targets"), Mapping):
                trust_reasons.append("legal_actions.attack_targets is not an object")
            if "AttackTargets" in legal and not isinstance(legal.get("AttackTargets"), Mapping):
                trust_reasons.append("legal_actions.AttackTargets is not an object")
            for target_key in ("attack_targets", "AttackTargets"):
                target_payload = legal.get(target_key)
                if not isinstance(target_payload, Mapping):
                    continue
                for raw_attacker, raw_targets in target_payload.items():
                    try:
                        int(raw_attacker)
                    except (TypeError, ValueError):
                        trust_reasons.append(f"legal_actions.{target_key} contains a non-integer attacker UID")
                    if not isinstance(raw_targets, (list, tuple, set)):
                        trust_reasons.append(f"legal_actions.{target_key} target list is not an array")
                        continue
                    if any(not isinstance(target, (int, str)) or isinstance(target, bool) for target in raw_targets):
                        trust_reasons.append(f"legal_actions.{target_key} contains an invalid target UID")
            if field_raw and not legal_attack_targets:
                # An explicitly present empty map is a valid projection when
                # no ally field card is currently allowed to attack.  Treat it
                # as missing only when the companion legal-action lists claim
                # that at least one attacker is available.
                listed_attackers = set(legal_ids("can_attack_leader_cards")) | set(legal_ids("can_attack_field_cards"))
                if not attack_targets_present or listed_attackers:
                    trust_reasons.append("legal_actions.attack_targets missing for ally field")
            elif legal_attack_targets:
                for item in field_raw:
                    if isinstance(item, Mapping) and isinstance(item.get("unique_id"), int) and int(item["unique_id"]) not in legal_attack_targets:
                        trust_reasons.append(f"legal_actions.attack_targets missing for follower {item['unique_id']}")
            hand_uids = {int(item.unique_id) for item in my_hand if item.unique_id}
            field_uids = {int(item.unique_id) for item in my_board if item.unique_id}
            enemy_uids = {int(item.unique_id) for item in enemy_board if item.unique_id}
            if isinstance(enemy.get("unique_id"), int):
                enemy_uids.add(int(enemy["unique_id"]))
            for key in ("can_play_cards", "can_play_cards_with_extra_pp", "can_enhance_play_cards", "can_accelerate_play_cards", "can_crystal_play_cards", "can_crystallize_play_cards"):
                for uid in legal_ids(key):
                    if uid not in hand_uids:
                        trust_reasons.append(f"legal_actions.{key} references unknown hand UID {uid}")
            for key in ("can_attack_leader_cards", "can_attack_field_cards", "attacked_cards", "can_evolve_cards", "can_super_evolve_cards", "can_super_evolve_with_skill_cards"):
                for uid in legal_ids(key):
                    if uid not in field_uids:
                        trust_reasons.append(f"legal_actions.{key} references unknown field UID {uid}")
            for attacker, targets in legal_attack_targets.items():
                if attacker not in field_uids:
                    trust_reasons.append(f"legal_actions.attack_targets references unknown attacker UID {attacker}")
                for target in targets:
                    if isinstance(target, int) and target not in enemy_uids:
                        trust_reasons.append(f"legal_actions.attack_targets references unknown target UID {target}")
        def parse_crests(owner: str, player: Mapping[str, Any]) -> tuple[list[int], list[dict[str, Any]]]:
            ids: list[int] = []
            instances: list[dict[str, Any]] = []
            raw_crests: list[Any] = []
            for source in (player.get("crests", ()), player.get("extra_crests", ())):
                if isinstance(source, Mapping):
                    source = [source] if "card_id" in source else list(source.values())
                if isinstance(source, (list, tuple)):
                    raw_crests.extend(source)
            for index, crest in enumerate(raw_crests):
                if isinstance(crest, int):
                    crest = {"card_id": crest}
                if not isinstance(crest, Mapping) or not isinstance(crest.get("card_id"), int):
                    continue
                card_id = int(crest["card_id"])
                ids.append(card_id)
                unique_id = _int(crest.get("unique_id"), 0)
                if unique_id == 0:
                    # A compact Tracker list may expose only card ids.  Keep
                    # each occurrence distinct so countdowns and state keys do
                    # not collapse repeated Crests.
                    unique_id = -(index + 1) if owner == "ally" else -(index + 1_000_001)
                instance = {
                    "card_id": card_id,
                    "unique_id": unique_id,
                    "owner": owner,
                    "style_id": _int(crest.get("style_id")),
                    "countdown": _int(crest.get("countdown"), -1),
                    "faith_value": _int(crest.get("faith_value")),
                    "variable_x": _int(crest.get("variable_x")),
                    "supplement_info": dict(crest.get("supplement_info", {})) if isinstance(crest.get("supplement_info"), Mapping) else {},
                }
                crest_buff = _full_buff(crest)
                if crest_buff is not None:
                    instance["buff"] = crest_buff
                if isinstance(crest.get("abilities"), (list, tuple)):
                    instance["abilities"] = [dict(item) for item in crest["abilities"] if isinstance(item, Mapping)]
                for key in (
                    "activated_random_once_indexes", "current_run_in_order_count",
                    "is_run_in_order_no_loop", "run_in_order_amount",
                ):
                    if key in crest:
                        value = crest.get(key)
                        instance[key] = _copy_json(value)
                instances.append(instance)
            return ids, instances

        crest_ids, crest_instances = parse_crests("ally", mine)
        enemy_crest_ids, enemy_crest_instances = parse_crests("enemy", enemy)
        raw_faith_instances = mine.get("faith_instances", mine.get("faith_resources", mine.get("faiths", ())))
        if isinstance(raw_faith_instances, Mapping):
            if "instances" in raw_faith_instances or "active" in raw_faith_instances:
                raw_faith_instances = raw_faith_instances.get("instances", raw_faith_instances.get("active", ()))
            elif any(key in raw_faith_instances for key in ("source_card_id", "card_id", "value", "faith_value")):
                raw_faith_instances = [raw_faith_instances]
            else:
                # A few recorder/export formats use an object keyed by the
                # source UID instead of an array.  Preserve those entries and
                # copy the key into ``unique_id`` when the payload itself does
                # not carry one, so duplicate Faith sources remain distinct.
                mapped_instances: list[dict[str, Any]] = []
                for raw_uid, raw_instance in raw_faith_instances.items():
                    if not isinstance(raw_instance, Mapping):
                        continue
                    instance = dict(raw_instance)
                    if "unique_id" not in instance:
                        try:
                            instance["unique_id"] = int(raw_uid)
                        except (TypeError, ValueError):
                            pass
                    mapped_instances.append(instance)
                raw_faith_instances = mapped_instances
        raw_faith_value = mine.get("faith", mine.get("faith_value", 0))
        if isinstance(raw_faith_value, Mapping):
            if not raw_faith_instances:
                raw_faith_instances = raw_faith_value.get("instances", raw_faith_value.get("active", ()))
            faith_scalar_value = raw_faith_value.get("value", raw_faith_value.get("total", 0))
        else:
            faith_scalar_value = raw_faith_value
        faith_instances = [dict(item) for item in raw_faith_instances if isinstance(item, Mapping)] if isinstance(raw_faith_instances, (list, tuple)) else []
        # Faith is an instance resource, not one global counter.  A compact
        # snapshot may omit instance IDs; synthesize stable negative IDs so
        # duplicate Faith sources remain distinguishable and can be updated
        # independently by the interpreter.
        for index, item in enumerate(faith_instances):
            if not isinstance(item.get("unique_id"), int) or isinstance(item.get("unique_id"), bool) or int(item.get("unique_id")) == 0:
                item["unique_id"] = -(index + 1)
            if "source_card_id" not in item and isinstance(item.get("card_id"), int):
                item["source_card_id"] = int(item["card_id"])
            if "value" not in item and "faith_value" in item:
                item["value"] = _int(item.get("faith_value"))
        faith_scalar = faith_scalar_value
        # If the snapshot exposes only per-instance Faith entries, the
        # default ``0`` from ``dict.get`` is not an aggregate value.  Sum the
        # instances in that case; an explicitly provided scalar remains
        # authoritative even when it is currently zero.
        aggregate_faith_present = "faith" in mine or "faith_value" in mine
        if not isinstance(faith_scalar, int) or (faith_instances and not aggregate_faith_present):
            faith_scalar = sum(_int(item.get("value"), _int(item.get("faith_value"))) for item in faith_instances)
        earth_sigil = _get_alias(mine, "earth_sigil", "EarthSigil", "earth_rite", "EarthRite")
        if not isinstance(earth_sigil, int):
            earth_sigil = sum(1 for raw in field_raw if isinstance(raw, Mapping) and _bool(raw.get("is_earth_sigil")))
        evolved_turn_raw = mine.get("evolved_allies_this_turn", mine.get("is_evolved_this_turn", 0))
        evolved_turn_count = int(evolved_turn_raw) if isinstance(evolved_turn_raw, bool) else _int(evolved_turn_raw)
        awakening_raw = _get_alias(mine, "is_awakening", "awakening", "Awakening")
        if isinstance(awakening_raw, bool):
            awakening_value = awakening_raw
        else:
            remaining_awakening = _get_alias(mine, "remaining_pp_until_awakening")
            awakening_value = isinstance(remaining_awakening, int) and remaining_awakening <= 0

        # Evolution unlock turns are public PlayerState fields in Tracker.
        # Keep ``None`` when an older/exported snapshot omits them; the live
        # LegalActions lists still govern the initial action, while a present
        # threshold remains authoritative for all hypothetical successors.
        turn_value = _get_alias(mine, "turn", "Turn")
        if not isinstance(turn_value, int) or isinstance(turn_value, bool):
            turn_value = _get_alias(snapshot, "current_turn", "CurrentTurn") if isinstance(snapshot, Mapping) else None
        turn_number_value = int(turn_value) if isinstance(turn_value, int) and not isinstance(turn_value, bool) else None
        evolve_turn_value = _get_alias(mine, "evolve_turn", "EvolveTurn", "evolve_unlock_turn", "evolution_unlock_turn")
        super_evolve_turn_value = _get_alias(mine, "super_evolve_turn", "SuperEvolveTurn", "super_evolve_unlock_turn", "super_evolution_unlock_turn")
        evolve_turn_value = int(evolve_turn_value) if isinstance(evolve_turn_value, int) and not isinstance(evolve_turn_value, bool) and evolve_turn_value >= 0 else None
        super_evolve_turn_value = int(super_evolve_turn_value) if isinstance(super_evolve_turn_value, int) and not isinstance(super_evolve_turn_value, bool) and super_evolve_turn_value >= 0 else None
        if (evolve_turn_value is not None or super_evolve_turn_value is not None) and turn_number_value is None:
            trust_reasons.append("ally.turn/current_turn required to validate evolution unlock turns")
        manual_evolved_turn_count = _manual_evolutions_this_turn(mine, turn_number_value, evolved_turn_count)

        state = LethalState(
            enemy_hp=_int(_get_alias(enemy, "life", "Life")),
            pp=_int(_get_alias(mine, "pp", "PP")),
            max_pp=_int(_get_alias(mine, "max_pp", "MaxPP")),
            ep=_int(_get_alias(mine, "evolve_points", "EP")),
            sep=_int(_get_alias(mine, "super_evolve_points", "SEP")),
            rally=_int(_get_alias(mine, "rally", "Rally", "rally_count")),
            cemetery=_int(_get_alias(mine, "cemetery_count", "cemetery", "Cemetery")),
            is_awakening=awakening_value,
            play_count=_int(_get_alias(mine, "play_count", "PlayCount", "plays_this_turn")),
            faith=_int(faith_scalar),
            evolved_allies_this_turn=evolved_turn_count,
            manual_evolutions_this_turn=manual_evolved_turn_count,
            evolved_allies_this_match=_int(mine.get("evolved_allies_this_match", mine.get("evolved_count", 0))),
            my_board=my_board,
            enemy_board=enemy_board,
            hand=my_hand,
            deck_distribution={int(key): _int(value) for key, value in (mine.get("deck_distribution", {}) or {}).items() if str(key).lstrip("-").isdigit() and _int(value) > 0} if isinstance(mine.get("deck_distribution"), Mapping) else {},
            total_deck_count=_int(_get_alias(mine, "deck_count", "DeckCount")),
            active_crests=crest_ids,
            ally_hp=_int(_get_alias(mine, "life", "Life")),
            ally_max_hp=_int(_get_alias(mine, "max_life", "MaxLife"), _int(_get_alias(mine, "life", "Life"))),
            extra_pp=_int(_get_alias(mine, "extra_pp", "ExtraPP"), _int(_get_alias(mine, "preparation_extra_pp", "PreparationExtraPP"))),
            earth_sigil=_int(earth_sigil),
            skybound_art=_int(_get_alias(mine, "skybound_art", "SkyboundArt", "skybound_art_gauge", default=0)),
            super_skybound_art=_int(_get_alias(mine, "super_skybound_art", "SuperSkyboundArt", "super_skybound_art_gauge", default=0)),
            # Existing leader damage-cut/increase buffs are part of the live
            # snapshot.  Preserve their full payload below and project the
            # known numeric fields into the interpreter's net modifier.
            enemy_damage_taken_modifier=_player_damage_modifier(enemy),
            played_base_costs=tuple(sorted({int(value) for value in (mine.get("played_base_costs", mine.get("played_card_costs", ())) if isinstance(mine.get("played_base_costs", mine.get("played_card_costs", ())), (list, tuple, set)) else ()) if isinstance(value, int)})),
            deck_replacement=(str(mine.get("deck_replacement", mine.get("deck_template", mine.get("deck_name", "")))) if mine.get("deck_replacement", mine.get("deck_template", mine.get("deck_name", ""))) else None),
            faith_instances=faith_instances,
            crest_instances=crest_instances,
            enemy_active_crests=enemy_crest_ids,
            enemy_crest_instances=enemy_crest_instances,
            legal_actions_known=legal is not None,
            legal_actions=dict(legal or {}),
            legal_attack_targets={int(uid): tuple(targets) for uid, targets in legal_attack_targets.items()},
            attack_targets_known=attack_targets_present,
            legal_modes=legal_modes,
            legal_modes_known=legal is not None,
            legal_play_uids=tuple(can_play_uids),
            legal_evolve_uids=tuple(can_evolve_uids),
            legal_super_evolve_uids=tuple(can_super_evolve_uids),
            attacked_card_uids=tuple(observed_attacked_uids),
            enemy_leader_uid=(int(enemy.get("unique_id")) if isinstance(enemy.get("unique_id"), int) and not isinstance(enemy.get("unique_id"), bool) else None),
            turn_number=turn_number_value,
            evolve_turn=evolve_turn_value,
            super_evolve_turn=super_evolve_turn_value,
            attacked_with_follower_this_turn=attacked_this_turn,
            ally_buff=_full_buff(mine),
            enemy_buff=_full_buff(enemy),
        )
        # ShadowverseTracker's authoritative public field is
        # ``destroyed_card_ids``: a sequence of ``(card_id, style_id)``
        # tuples.  Older snapshots used a richer mapping under one of the
        # aliases below.  Prefer the authoritative field and preserve every
        # occurrence (duplicate card ids are distinct reanimation slots).
        destroyed_field_present = any(key in mine for key in ("destroyed_card_ids", "destroyed_this_match", "destroyed_history", "destroyed_cards"))
        raw_destroyed = mine.get("destroyed_card_ids", mine.get("destroyed_this_match", mine.get("destroyed_history", mine.get("destroyed_cards", ()))))
        if isinstance(raw_destroyed, Mapping):
            if "card_id" in raw_destroyed:
                raw_destroyed = [raw_destroyed]
            elif all(
                str(key).lstrip("-").isdigit()
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for key, value in raw_destroyed.items()
            ):
                # Compact ledgers sometimes publish ``{card_id: count}``.
                # Expand counts into separate pool entries so Reanimate's
                # probability is weighted by the number of destroyed copies.
                expanded: list[int] = []
                for raw_card_id, count in raw_destroyed.items():
                    expanded.extend([int(raw_card_id)] * int(count))
                raw_destroyed = expanded
            else:
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
                if isinstance(item, int):
                    # Some Tracker builds expose only card ids (style ids are
                    # omitted for ordinary cards).  Keep each occurrence as a
                    # separate synthetic pool entry so Reanimate probability
                    # still follows multiplicity.
                    card_id = _base_card_id(int(item), catalog_cards)
                    style_id = 0
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
                        can_attack_leader=False,
                        can_attack_field=False,
                        attacks_left=0,
                        base_cost=_int(meta.get("cost")),
                        statuses=(f"historical_style:{style_id}",),
                    ))
                    continue
                if isinstance(item, (list, tuple)) and item:
                    card_id = _base_card_id(_int(item[0]), catalog_cards)
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
        # De-duplicate reasons while retaining deterministic order.  A
        # snapshot with any critical gap is inspectable but not trustworthy;
        # callers must display INCOMPLETE and wait for the next refresh.
        trust_reasons = list(dict.fromkeys(str(item) for item in trust_reasons if str(item)))
        if trust_reasons:
            warnings.extend(f"untrusted snapshot: {reason}" for reason in trust_reasons)
        trusted = not trust_reasons
        usable = bool(trusted and legal is not None and is_ally_turn)
        return SnapshotAdapterResult(
            state,
            legal,
            is_ally_turn,
            usable,
            tuple(sorted(unsupported)),
            tuple(dict.fromkeys(warnings)),
            trusted,
            tuple(trust_reasons),
        )


def adapt_snapshot(
    snapshot: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    rules: Mapping[str, Any] | None = None,
) -> SnapshotAdapterResult:
    """Functional convenience wrapper for Tracker callback integrations."""
    return SnapshotAdapter.adapt(snapshot, catalog=catalog, rules=rules)


__all__ = ["SnapshotAdapter", "SnapshotAdapterResult", "adapt_snapshot"]
