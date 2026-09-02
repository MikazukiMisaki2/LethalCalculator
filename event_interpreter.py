"""Minimal event-driven rules interpreter for CardRules v2.

The runtime supports the deterministic lethal primitives (play, damage,
buffs, PP recovery, Storm/Rush/Ward, Bane/Drain/Ambush combat, evolve,
resources, invocation and delayed leader effects) plus a
probability-preserving branch API for known random targets, draws and deck
selectors. Unsupported effects are reported instead of being silently
ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Mapping

from lethal_models import LethalFollower, LethalHandCard, LethalState


# ``CardCatalog`` intentionally stores compact numeric tribe ids.  Text rules
# use the stable English keyword, so normalize the current game's public tribe
# table at the interpreter boundary (and retain the numeric spelling too).
_TRIBE_ALIASES: dict[int, str] = {
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


# Runtime-confirmed keyword subset.  The schema vocabulary is intentionally
# broader than this set; only keywords whose state transitions are modeled
# here may be used by a confirmed lethal line.
_IMPLEMENTED_KEYWORDS = frozenset({"storm", "rush", "ward", "bane", "drain", "ambush"})
_KEYWORD_ALIASES = {
    "必杀": "bane", "毁灭": "bane", "bane": "bane",
    "虹吸": "drain", "吸血": "drain", "drain": "drain",
    "潜行": "ambush", "突袭": "ambush", "ambush": "ambush",
    "疾驰": "storm", "storm": "storm", "突进": "rush", "rush": "rush",
    "守护": "ward", "ward": "ward",
}


def _normalize_keyword(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return _KEYWORD_ALIASES.get(text, text)


def _has_keyword(entity: LethalFollower | LethalHandCard, keyword: str) -> bool:
    wanted = _normalize_keyword(keyword)
    statuses = getattr(entity, "statuses", ())
    if isinstance(statuses, str):
        statuses = (statuses,)
    if any(_normalize_keyword(item) == wanted for item in statuses):
        return True
    # Storm is the stronger attack keyword and carries Rush semantics in the
    # combat model.  Preserve that implication for status-only fixtures too.
    if wanted == "rush" and any(_normalize_keyword(item) == "storm" for item in statuses):
        return True
    # Minimal/legacy fixtures often set only the original boolean fields.
    # Treat those fields as equivalent to their canonical status so Bane,
    # Drain and Ambush remain explicit while Storm/Rush/Ward compatibility is
    # preserved for snapshots created before ``statuses`` was added.
    flags = {
        "storm": bool(getattr(entity, "has_storm", False) or getattr(entity, "static_storm", False)),
        "rush": bool(getattr(entity, "has_rush", False) or getattr(entity, "static_rush", False)),
        "ward": bool(getattr(entity, "is_ward", False)),
        "bane": bool(getattr(entity, "has_bane", False)),
        "drain": bool(getattr(entity, "has_drain", False)),
        "ambush": bool(getattr(entity, "has_ambush", False)),
    }
    if wanted == "rush" and flags["storm"]:
        return True
    return bool(flags.get(wanted, False))


def _without_keyword(statuses: Any, keyword: str) -> tuple[str, ...]:
    wanted = _normalize_keyword(keyword)
    if isinstance(statuses, str):
        statuses = (statuses,)
    return tuple(sorted({str(item) for item in (statuses or ()) if _normalize_keyword(item) != wanted}))


@dataclass(frozen=True)
class InterpreterResult:
    state: LethalState
    unsupported_ops: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StochasticBranch:
    """One outcome of a random effect sequence."""

    probability: float
    state: LethalState
    unsupported_ops: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EventInterpreter:
    def __init__(self, rules: Mapping[str, Any] | None = None, *, catalog: Mapping[str, Any] | None = None, card_db: Mapping[int, LethalHandCard] | None = None) -> None:
        raw = rules.get("rules", {}) if isinstance(rules, Mapping) and isinstance(rules.get("rules"), Mapping) else rules
        self.rules: dict[int, dict[str, Any]] = {}
        for key, value in (raw or {}).items():
            if isinstance(value, Mapping):
                try:
                    self.rules[int(key)] = dict(value)
                except (TypeError, ValueError):
                    continue
        catalog_raw = catalog.get("cards", {}) if isinstance(catalog, Mapping) and isinstance(catalog.get("cards"), Mapping) else catalog
        self.catalog: dict[int, dict[str, Any]] = {}
        for key, value in (catalog_raw or {}).items():
            if isinstance(value, Mapping):
                try:
                    self.catalog[int(key)] = dict(value)
                except (TypeError, ValueError):
                    continue
        self.card_db: dict[int, LethalHandCard] = {int(key): value for key, value in (card_db or {}).items() if isinstance(value, LethalHandCard)}

    def _base_card_cost(self, card: LethalHandCard) -> int:
        """Return the printed/base cost, ignoring temporary hand discounts."""
        meta = self.catalog.get(int(card.card_id), {})
        raw = meta.get("cost") if isinstance(meta, Mapping) else None
        try:
            return int(raw) if raw is not None else int(card.cost)
        except (TypeError, ValueError):
            return int(card.cost)

    def _static_keywords(self, card_id: int, card: LethalHandCard | None = None) -> set[str]:
        rule = self.rules.get(int(card_id), {})
        raw = rule.get("static_keywords", ()) if isinstance(rule, Mapping) else ()
        values = {_normalize_keyword(item) for item in raw if str(item).strip()} if isinstance(raw, (list, tuple, set)) else set()
        if card is not None:
            values.update(_normalize_keyword(item) for item in getattr(card, "statuses", ()) if str(item).strip())
            if card.static_storm:
                values.add("storm")
            if card.static_rush:
                values.add("rush")
            for keyword in ("bane", "drain", "ambush"):
                if getattr(card, f"has_{keyword}", False):
                    values.add(keyword)
        return values

    def mode_cost(self, card: LethalHandCard, mode: str = "normal") -> int | None:
        """Return the cost for exactly one selected play mode.

        A missing alternate mode is rejected instead of silently falling back
        to normal.  This is the important contract that keeps a normal,
        Enhance, Accelerate, or Crystallize payload mutually exclusive.
        """
        if mode == "normal":
            return int(card.cost)
        if mode == "enhance" and card.enhance_cost is not None:
            return int(card.enhance_cost)
        if mode == "accelerate" and card.accelerate_cost is not None:
            return int(card.accelerate_cost)
        if mode == "crystallize" and card.crystallize_cost is not None:
            return int(card.crystallize_cost)
        for kind, cost in card.mode_costs:
            if kind == mode:
                return int(cost)
        rule = self.rules.get(card.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, Mapping) and m.get("kind") == mode), None)
        if selected is not None and isinstance(selected.get("cost"), (int, float)):
            return int(selected["cost"])
        return None

    def available_modes(self, card: LethalHandCard, state: LethalState | None = None) -> tuple[tuple[str, int], ...]:
        """Enumerate legal modes without combining their effects."""
        rule = self.rules.get(card.card_id, {})
        rule_modes = {str(m.get("kind")) for m in rule.get("modes", ()) if isinstance(m, Mapping)}
        candidates = ["normal", "enhance", "accelerate", "crystallize"]
        result: list[tuple[str, int]] = []
        for mode in candidates:
            cost = self.mode_cost(card, mode)
            if cost is None:
                continue
            has_explicit_card_mode = (
                (mode == "enhance" and card.enhance_cost is not None)
                or (mode == "accelerate" and card.accelerate_cost is not None)
                or (mode == "crystallize" and card.crystallize_cost is not None)
                or any(kind == mode for kind, _ in card.mode_costs)
            )
            if mode != "normal" and mode not in rule_modes and not has_explicit_card_mode:
                continue
            if state is not None and getattr(state, "legal_modes_known", False):
                # Tracker's mode lists are authoritative for this hand
                # entity. An explicitly empty tuple means no mode is legal;
                # never fall back to the printed card rule in that case.
                allowed = getattr(state, "legal_modes", {}).get(int(card.unique_id))
                if allowed is None or mode not in allowed:
                    continue
            if state is None or self._available_pp(state) >= cost:
                result.append((mode, cost))
        return tuple(result)

    @staticmethod
    def _available_pp(state: LethalState) -> int:
        return max(0, int(state.pp)) + max(0, int(getattr(state, "extra_pp", 0)))

    def _is_spell_card(self, card: LethalHandCard) -> bool:
        """Recognize v2 type=4 spells and legacy type=3 spell fixtures.

        Tracker/catalog snapshots use type=3 for countdown amulets and 4 for
        spells, while older hand-card fixtures used 3 as the spell marker.
        Catalog metadata disambiguates the former without breaking those
        legacy tests.
        """
        if card.type == 4:
            return True
        if card.type != 3:
            return False
        meta = self.catalog.get(card.card_id, {})
        return meta.get("type") not in ("amulet", "countdown_amulet")

    @staticmethod
    def _pay_pp(state: LethalState, cost: int) -> bool:
        if cost < 0 or EventInterpreter._available_pp(state) < cost:
            return False
        regular = min(max(0, int(state.pp)), cost)
        state.pp -= regular
        remaining = cost - regular
        if remaining:
            state.extra_pp = max(0, int(state.extra_pp) - remaining)
        return True

    def play(self, state: LethalState, unique_id: int, mode: str = "normal", target_uid: Any = None, choice: Any = None) -> InterpreterResult:
        index = next((i for i, card in enumerate(state.hand) if card.unique_id == unique_id), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"hand card {unique_id} not found",))
        card = state.hand[index]
        if getattr(state, "legal_modes_known", False):
            allowed_modes = state.legal_modes.get(int(unique_id))
            if allowed_modes is None or mode not in allowed_modes:
                return InterpreterResult(state, warnings=(f"Tracker forbids {mode} play for {unique_id}",))
        play_cost = self.mode_cost(card, mode)
        if play_cost is None:
            return InterpreterResult(state, warnings=(f"mode {mode} is not available for {card.name}",))
        if self._available_pp(state) < play_cost:
            return InterpreterResult(state, warnings=(f"insufficient PP for {card.name}",))
        # Accelerate/Crystallize replace a follower body with their alternate
        # spell/amulet payload, so only normal and Enhance occupy a follower
        # slot in this boundary model.
        places_follower = card.type == 1 and mode not in ("accelerate", "crystallize")
        if places_follower and len(state.my_board) >= 5:
            return InterpreterResult(state, warnings=("ally field is full",))
        resource_error = self._resource_preflight(state, self.rules.get(card.card_id, {}), mode, source_uid=card.unique_id, choice=choice)
        if resource_error is not None:
            resource, need, have = resource_error
            return InterpreterResult(
                state,
                unsupported_ops=(f"insufficient_resource:{resource}",),
                warnings=(f"insufficient {resource} (need {need}, have {have})",),
            )
        next_state = state.clone()
        self._invalidate_snapshot_legality(next_state)
        next_state.hand.pop(index)
        if not self._pay_pp(next_state, play_cost):
            return InterpreterResult(state, warnings=(f"insufficient PP for {card.name}",))
        next_state.play_count += 1
        next_state.last_played_card_cost = int(play_cost)
        next_state.last_played_mode = str(mode)
        next_state.last_played_card_type = card.type
        next_state.last_played_tribes = tuple(card.tribes)
        base_cost = self._base_card_cost(card)
        next_state.played_base_costs = tuple(sorted(set(next_state.played_base_costs) | {base_cost}))
        self._ensure_catalog_resources(next_state, card.card_id, card.unique_id)
        rule = self.rules.get(card.card_id, {})
        # A follower is already on the field when its Fanfare resolves, so
        # self-targeting buffs and status grants must be able to find it.
        enhanced = mode == "enhance"
        intrinsic_damage = card.enhance_face_damage if enhanced and card.enhance_face_damage else (card.face_damage if mode == "normal" else 0)
        intrinsic_recover = card.enhance_recover_pp if enhanced and card.enhance_recover_pp else (card.recover_pp if mode == "normal" else 0)
        intrinsic_buff = card.enhance_buff_atk if enhanced and card.enhance_buff_atk else (card.buff_atk if mode == "normal" else 0)
        board_uids_before = {f.unique_id for f in next_state.my_board}
        if places_follower:
            static = self._static_keywords(card.card_id, card)
            default_attacks = rule.get("default_attacks", 1) if isinstance(rule, Mapping) else 1
            try:
                default_attacks = max(1, int(default_attacks))
            except (TypeError, ValueError):
                default_attacks = 1
            storm = card.static_storm or (enhanced and card.enhance_gain_storm) or "storm" in static
            rush = card.static_rush or "rush" in static or storm
            next_state.my_board.append(LethalFollower(
                unique_id=card.unique_id, card_id=card.card_id, name=card.name,
                atk=card.atk + intrinsic_buff, hp=card.life, has_storm=storm, has_rush=rush,
                is_ward="ward" in static, can_attack_leader=storm, can_attack_field=rush, attacks_left=default_attacks,
                statuses=tuple(sorted(set(str(item) for item in static))),
                base_cost=card.cost,
                spell_boost_count=card.spell_boost_count,
                has_spell_boost=card.has_spell_boost or self._card_has_trigger(card.card_id, "on_spellboost"),
                variable_x=card.variable_x,
                supplement_info=card.supplement_info,
                has_bane="bane" in static,
                has_drain="drain" in static,
                has_ambush="ambush" in static,
                buff=card.buff,
            ))
            next_state.last_created_uid = card.unique_id
        next_state.selected_mode_choice = choice
        on_play = self._resolve_abilities(next_state, rule, mode, "on_play", source_uid=card.unique_id, target_uid=target_uid, choice=choice)
        # Selecting one numbered Mode is itself a public event observed by
        # Faith instances.  Super Skybound Art activates all choices instead
        # of selecting one, so it must not emit this event.
        if (
            choice is not None
            and self._mode_has_choice(rule, mode)
            and not (next_state.super_skybound_art > 0 and self._mode_has_activate_all(rule, mode))
        ):
            mode_event = self.select_mode(
                on_play.state,
                count=self._mode_selection_count(rule, mode, choice),
            )
            on_play = InterpreterResult(
                mode_event.state,
                tuple(sorted(set(on_play.unsupported_ops) | set(mode_event.unsupported_ops))),
                on_play.warnings + mode_event.warnings,
            )
        fanfare = self._resolve_abilities(on_play.state, rule, mode, "on_fanfare", source_uid=card.unique_id, target_uid=target_uid, choice=choice)
        summon = self._resolve_abilities(fanfare.state, rule, mode, "on_summon", source_uid=card.unique_id, target_uid=target_uid, choice=choice) if places_follower else InterpreterResult(fanfare.state)
        next_state = summon.state
        entered_uids = [f.unique_id for f in next_state.my_board if f.unique_id not in board_uids_before]
        ally_summon = self._resolve_board_trigger(next_state, "on_ally_follower_summon") if entered_uids else InterpreterResult(next_state)
        next_state = ally_summon.state
        filtered_unsupported: set[str] = set()
        filtered_warnings: list[str] = []
        for event_uid in entered_uids:
            filtered = self._resolve_board_trigger(next_state, "on_ally_follower_summon", event_uid=event_uid)
            next_state = filtered.state
            filtered_unsupported.update(filtered.unsupported_ops)
            filtered_warnings.extend(filtered.warnings)
        if filtered_unsupported or filtered_warnings:
            ally_summon = InterpreterResult(
                next_state,
                tuple(sorted(set(ally_summon.unsupported_ops) | filtered_unsupported)),
                ally_summon.warnings + tuple(filtered_warnings),
            )
        # Persistent listeners granted by cards such as Yidmetra observe the
        # completed play mode. ``last_played_mode`` is set before resolution,
        # so their condition can distinguish Enhance from a normal play.
        card_play = self._resolve_board_trigger(next_state, "on_card_play")
        next_state = card_play.state
        # Spellboost is a global event: playing a spell triggers each allied
        # follower that has an on_spellboost ability. Resolve in board order
        # so deterministic buffs and resource changes are reproducible.
        spellboost = InterpreterResult(next_state)
        if self._is_spell_card(card):
            spellboost = self._resolve_board_trigger(next_state, "on_spellboost")
            hand_spellboost = self._spellboost_effect(
                spellboost.state,
                {"op": "spellboost", "target": {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}, "count": 1},
                card.unique_id,
            )
            spellboost = InterpreterResult(
                hand_spellboost.state,
                tuple(sorted(set(spellboost.unsupported_ops) | set(hand_spellboost.unsupported_ops))),
                spellboost.warnings + hand_spellboost.warnings,
            )
            next_state = spellboost.state
        # Compatibility for hand cards created directly in tests or by the
        # legacy catalog: these are intrinsic effects, not engine transitions.
        if intrinsic_damage or intrinsic_recover:
            next_state = next_state.clone()
            next_state.enemy_hp -= intrinsic_damage
            next_state.pp = min(next_state.max_pp, next_state.pp + intrinsic_recover)
        # A spell enters the cemetery after its own resolution.  This keeps
        # the source spell from paying its own Necromancy/Earth Rite-style
        # gate while still contributing one shadow to the resulting state.
        if self._is_spell_card(card):
            next_state = next_state.clone()
            next_state.cemetery += 1
        unsupported = set(on_play.unsupported_ops) | set(fanfare.unsupported_ops) | set(summon.unsupported_ops) | set(ally_summon.unsupported_ops) | set(card_play.unsupported_ops) | set(spellboost.unsupported_ops)
        static_keywords = rule.get("static_keywords", ()) if isinstance(rule, Mapping) else ()
        unsupported.update(f"static_keyword:{keyword}" for keyword in static_keywords if _normalize_keyword(keyword) not in _IMPLEMENTED_KEYWORDS)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{card.card_id}")
        return InterpreterResult(next_state, tuple(sorted(unsupported)), on_play.warnings + fanfare.warnings + summon.warnings + ally_summon.warnings + card_play.warnings + spellboost.warnings)

    @staticmethod
    def _heal_leader_for_drain(state: LethalState, amount: int, unsupported: set[str], warnings: list[str]) -> LethalState:
        """Restore the allied leader's defense for a Drain combat event."""
        amount = max(0, int(amount))
        if amount <= 0:
            return state
        current = state.clone()
        if current.ally_hp or current.ally_max_hp:
            cap = current.ally_max_hp or current.ally_hp + amount
            current.ally_hp = min(cap, current.ally_hp + amount)
        else:
            # A minimal hand-built state may omit leader defense.  Preserve the
            # event as an explicit gap instead of claiming a healed value.
            unsupported.add("drain_target")
        return current

    def _destroy_ally_uid(self, state: LethalState, uid: int, unsupported: set[str], warnings: list[str]) -> LethalState:
        """Destroy one allied permanent and run its public death pipeline."""
        index = next((i for i, item in enumerate(state.my_board) if item.unique_id == uid), None)
        if index is None:
            return state
        current = state.clone()
        removed = current.my_board.pop(index)
        current.cemetery += 1
        self._record_destroyed(current, removed)
        if self._is_amulet(removed):
            faith_result = self._resolve_faith_trigger(current, "on_ally_amulet_destroy")
            current = faith_result.state
            unsupported.update(faith_result.unsupported_ops)
            warnings.extend(faith_result.warnings)
        last_words = self._resolve_last_words(current, removed)
        current = last_words.state
        unsupported.update(last_words.unsupported_ops)
        warnings.extend(last_words.warnings)
        return current

    @staticmethod
    def _destroy_enemy_uid(state: LethalState, uid: int) -> LethalState:
        current = state.clone()
        index = next((i for i, item in enumerate(current.enemy_board) if item.unique_id == uid), None)
        if index is not None:
            current.last_destroyed_snapshot = current.enemy_board.pop(index)
        return current

    @staticmethod
    def _snapshot_allows_attack(state: LethalState, attacker_uid: int, target_uid: Any = None, *, leader: bool = False) -> bool:
        """Honor Tracker LegalActions/AttackTargets when the snapshot is authoritative.

        Hand-built solver fixtures leave ``legal_actions_known`` false and
        continue to use the intrinsic Storm/Rush model.  A live Tracker
        snapshot sets it true and includes an entry for every ally follower;
        an absent entry or an empty target list therefore means that attack is
        currently illegal rather than an invitation to guess.
        """
        # A live Tracker snapshot may expose only ``FieldCard.attack_targets``
        # (for example when BattleViewServerData is unavailable).  Enforce
        # that projection independently of the broader LegalActions object;
        # hand-built legacy states leave both presence bits false and retain
        # the intrinsic Storm/Rush behavior.
        if not getattr(state, "legal_actions_known", False) and not getattr(state, "attack_targets_known", False):
            return True
        targets = getattr(state, "legal_attack_targets", {}).get(int(attacker_uid))
        if targets is None:
            return False
        if leader:
            leader_uid = getattr(state, "enemy_leader_uid", None)
            markers = {"leader", "enemy_leader", "enemy_leader_uid"}
            if leader_uid is not None:
                markers.add(leader_uid)
                markers.add(str(leader_uid).casefold())
            return any(target in markers or str(target).casefold() in markers for target in targets)
        if target_uid in targets:
            return True
        # JSON snapshots may stringify dictionary/list ids even though the
        # Tracker dataclass uses integers.  UID equality is numeric identity,
        # not a representation detail.
        return any(str(target_uid) == str(target) for target in targets)

    @staticmethod
    def _invalidate_snapshot_legality(state: LethalState) -> None:
        """Drop one-shot Tracker legality after a hypothetical state mutation.

        LegalActions describes the live board at refresh time. Once the solver
        plays/evolves/attacks in a branch, those UID lists are stale (summons,
        deaths and new modes may have changed them). Keep the projections for
        UI inspection but stop enforcing them in the simulated successor.
        """
        state.legal_actions_known = False
        state.attack_targets_known = False
        state.legal_modes_known = False

    @staticmethod
    def _evolution_unlocked(state: LethalState, *, super_evolve: bool) -> bool:
        """Return whether the Tracker-reported evolve turn has arrived.

        ``LegalActions`` is a point-in-time projection and is intentionally
        invalidated after a hypothetical action.  The unlock turn is stable
        for the match, so it remains a second, persistent legality guard.
        Hand-built legacy states leave the fields as ``None`` and retain the
        historical intrinsic EP/SEP behavior.
        """
        threshold = getattr(state, "super_evolve_turn" if super_evolve else "evolve_turn", None)
        if threshold is None:
            return True
        current = getattr(state, "turn_number", None)
        if current is None:
            # A threshold without a current turn cannot be proven legal.
            return False
        return int(current) >= int(threshold)

    @classmethod
    def _evolution_unlock_warning(cls, state: LethalState, *, super_evolve: bool) -> str:
        kind = "super-evolve" if super_evolve else "evolve"
        threshold = getattr(state, "super_evolve_turn" if super_evolve else "evolve_turn", None)
        current = getattr(state, "turn_number", None)
        if threshold is None:
            return f"{kind} unlock turn is unknown"
        if current is None:
            return f"{kind} unlock turn {threshold} cannot be checked without current turn"
        return f"{kind} is locked until turn {threshold} (current turn {current})"

    def attack_follower(self, state: LethalState, attacker_uid: int, target_uid: int) -> InterpreterResult:
        ai = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        ti = next((i for i, f in enumerate(state.enemy_board) if f.unique_id == target_uid), None)
        if ai is None or ti is None:
            return InterpreterResult(state, warnings=("combat target not found",))
        attacker = state.my_board[ai]
        target = state.enemy_board[ti]
        if not self._snapshot_allows_attack(state, attacker_uid, target_uid):
            return InterpreterResult(state, warnings=(f"Tracker forbids attack {attacker_uid}->{target_uid}",))
        if not (attacker.can_attack_field or _has_keyword(attacker, "rush") or attacker.is_evolved) or attacker.attacks_left <= 0:
            return InterpreterResult(state, warnings=(f"{attacker.name} cannot attack a follower",))
        if any(_has_keyword(f, "ward") for f in state.enemy_board) and not _has_keyword(target, "ward"):
            return InterpreterResult(state, warnings=("Ward must be attacked first",))
        # Ambush prevents enemy followers from selecting this permanent in
        # combat.  (Area effects may still hit it; target filtering below keeps
        # that distinction.)
        if _has_keyword(target, "ambush"):
            return InterpreterResult(state, warnings=(f"{target.name} is hidden by Ambush",))
        next_state = state.clone()
        self._invalidate_snapshot_legality(next_state)
        # The attack occurred even when the attacker dies in combat; Crest's
        # “didn't attack this turn” condition must therefore observe it before
        # Last Words resolve and remove the entity.
        next_state.attacked_with_follower_this_turn = True
        next_state.attacked_card_uids = tuple(dict.fromkeys((*next_state.attacked_card_uids, attacker_uid)))
        # An Ambush attacker becomes targetable as soon as it attacks.  Keep
        # the rest of its entity state intact while removing only that status.
        attacker_for_combat = (
            replace(
                attacker,
                # Ambush is a one-shot concealment state: the first attack
                # reveals the entity.  Clear both the extensible status and
                # the explicit projection so a later targeted effect cannot
                # still treat this attacker as hidden.
                statuses=_without_keyword(attacker.statuses, "ambush"),
                has_ambush=False,
            )
            if _has_keyword(attacker, "ambush")
            else attacker
        )
        next_state.enemy_board.pop(ti)
        dealt_to_target = min(max(0, attacker_for_combat.atk), max(0, target.hp))
        remaining_target_hp = target.hp - attacker_for_combat.atk
        if remaining_target_hp > 0:
            next_state.enemy_board.insert(ti, replace(target, hp=remaining_target_hp))
        else:
            # Preserve the complete enemy snapshot for a following
            # "copy/destroyed follower" effect.  The card remains in the
            # match's public destroyed pool only when it is an allied
            # permanent; an enemy snapshot is intentionally transient.
            next_state.last_destroyed_snapshot = target
        remaining_attacker_hp = attacker_for_combat.hp - target.atk
        next_state.my_board.pop(ai)
        combat_unsupported: set[str] = set()
        combat_warnings: list[str] = []
        if remaining_attacker_hp > 0:
            next_state.my_board.insert(ai, replace(attacker_for_combat, hp=remaining_attacker_hp, attacks_left=attacker.attacks_left - 1))
        else:
            next_state.cemetery += 1
            self._record_destroyed(next_state, attacker_for_combat)
            last_words = self._resolve_last_words(next_state, attacker_for_combat)
            next_state = last_words.state
            combat_unsupported.update(last_words.unsupported_ops)
            combat_warnings.extend(last_words.warnings)
        # Bane destroys a surviving combat target.  Resolve it after normal
        # combat damage so lethal damage and Bane are idempotent.
        if _has_keyword(attacker_for_combat, "bane") and remaining_target_hp > 0:
            next_state = self._destroy_enemy_uid(next_state, target.unique_id)
        if _has_keyword(target, "bane") and remaining_attacker_hp > 0:
            next_state = self._destroy_ally_uid(next_state, attacker_uid, combat_unsupported, combat_warnings)
        if _has_keyword(attacker_for_combat, "drain"):
            next_state = self._heal_leader_for_drain(next_state, dealt_to_target, combat_unsupported, combat_warnings)
        # Clash/Strike are emitted by the completed combat event.  Resolve
        # them after damage and Last Words so a surviving source sees its
        # updated entity state; a printed ability remains resolvable even if
        # the source died during the exchange.
        rule = self.rules.get(attacker.card_id, {})
        for trigger in ("on_clash", "on_follower_attack"):
            triggered = self._resolve_abilities(next_state, rule, "normal", trigger, source_uid=attacker.unique_id, target_uid=target_uid)
            next_state = triggered.state
            combat_unsupported.update(triggered.unsupported_ops)
            combat_warnings.extend(triggered.warnings)
        return InterpreterResult(next_state, tuple(sorted(combat_unsupported)), tuple(combat_warnings))

    def attack_leader(self, state: LethalState, attacker_uid: int) -> InterpreterResult:
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"attacker {attacker_uid} not found",))
        attacker = state.my_board[index]
        if not self._snapshot_allows_attack(state, attacker_uid, leader=True):
            return InterpreterResult(state, warnings=(f"Tracker forbids follower {attacker_uid} attacking leader",))
        if any(_has_keyword(f, "ward") for f in state.enemy_board):
            return InterpreterResult(state, warnings=("Ward blocks leader attack",))
        if not (attacker.can_attack_leader or _has_keyword(attacker, "storm")) or attacker.attacks_left <= 0:
            return InterpreterResult(state, warnings=(f"{attacker.name} cannot attack leader",))
        next_state = state.clone()
        self._invalidate_snapshot_legality(next_state)
        # Ambush is broken by the first attack, regardless of whether the
        # target is the leader or a follower.
        attacker_for_combat = (
            replace(
                attacker,
                statuses=_without_keyword(attacker.statuses, "ambush"),
                has_ambush=False,
            )
            if _has_keyword(attacker, "ambush")
            else attacker
        )
        dealt = max(0, attacker_for_combat.atk + int(next_state.enemy_damage_taken_modifier))
        next_state.enemy_hp -= dealt
        next_state.my_board[index] = replace(attacker_for_combat, attacks_left=attacker.attacks_left - 1)
        next_state.attacked_with_follower_this_turn = True
        next_state.attacked_card_uids = tuple(dict.fromkeys((*next_state.attacked_card_uids, attacker_uid)))
        rule = self.rules.get(attacker.card_id, {})
        triggered = self._resolve_abilities(next_state, rule, "normal", "on_leader_attack", source_uid=attacker_uid)
        unsupported = set(triggered.unsupported_ops)
        warnings = list(triggered.warnings)
        if _has_keyword(attacker_for_combat, "drain"):
            healed = self._heal_leader_for_drain(triggered.state, dealt, unsupported, warnings)
            return InterpreterResult(healed, tuple(sorted(unsupported)), tuple(warnings))
        return InterpreterResult(triggered.state, tuple(sorted(unsupported)), tuple(warnings))

    def evolve(self, state: LethalState, attacker_uid: int, *, super_evolve: bool = False, target_uid: Any = None) -> InterpreterResult:
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"follower {attacker_uid} not found",))
        follower = state.my_board[index]
        if getattr(state, "legal_actions_known", False):
            allowed = state.legal_super_evolve_uids if super_evolve else state.legal_evolve_uids
            if attacker_uid not in allowed:
                return InterpreterResult(state, warnings=(f"Tracker forbids evolving follower {attacker_uid}",))
        if not self._evolution_unlocked(state, super_evolve=super_evolve):
            return InterpreterResult(state, warnings=(self._evolution_unlock_warning(state, super_evolve=super_evolve),))
        # Shadowverse permits at most one player-initiated evolution per
        # turn.  ``evolved_allies_this_turn`` is populated from Tracker's
        # ``is_evolved_this_turn`` projection and survives hypothetical
        # actions.  This gate intentionally applies only to this manual
        # action; card effects use ``auto_evolve`` and may evolve additional
        # followers for free.
        if getattr(state, "manual_evolutions_this_turn", 0) > 0:
            return InterpreterResult(state, warnings=("only one manual evolution is allowed per turn",))
        if follower.is_evolved:
            return InterpreterResult(state, warnings=(f"{follower.name} already evolved",))
        points = state.sep if super_evolve else state.ep
        if points <= 0:
            return InterpreterResult(state, warnings=("no evolve points",))
        next_state = state.clone()
        self._invalidate_snapshot_legality(next_state)
        if super_evolve:
            next_state.sep -= 1
            delta = 3
            trigger = "on_super_evolve"
        else:
            next_state.ep -= 1
            delta = 2
            trigger = "on_evolve"
        next_state.my_board[index] = replace(
            follower,
            atk=follower.atk + delta,
            hp=follower.hp + delta,
            is_evolved=True,
            is_super_evolved=super_evolve,
            can_attack_leader=follower.has_storm or _has_keyword(follower, "storm"),
            can_attack_field=True,
        )
        next_state.evolved_allies_this_turn += 1
        next_state.manual_evolutions_this_turn += 1
        next_state.evolved_allies_this_match += 1
        rule = self.rules.get(follower.card_id, {})
        result = self._resolve_abilities(next_state, rule, "normal", trigger, source_uid=follower.unique_id, target_uid=target_uid)
        faith_result = self._resolve_faith_trigger(result.state, "on_ally_follower_evolve", source_uid=follower.unique_id)
        current = faith_result.state
        if super_evolve:
            super_faith = self._resolve_faith_trigger(current, "on_ally_follower_super_evolve", source_uid=follower.unique_id)
            current = super_faith.state
        else:
            super_faith = InterpreterResult(current)
        global_trigger = self._resolve_board_trigger(current, "on_ally_follower_super_evolve" if super_evolve else "on_ally_follower_evolve")
        current = global_trigger.state
        unsupported = set(result.unsupported_ops) | set(faith_result.unsupported_ops) | set(super_faith.unsupported_ops) | set(global_trigger.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{follower.card_id}")
        return InterpreterResult(current, tuple(sorted(unsupported)), result.warnings + faith_result.warnings + super_faith.warnings + global_trigger.warnings)

    def auto_evolve(self, state: LethalState, follower_uid: int, *, super_evolve: bool = False, target_uid: Any = None) -> InterpreterResult:
        """Evolve a follower without spending EP/SEP.

        Card text uses ``Evolve`` in two different contexts: a player action
        spends an evolution point, while an effect such as Earth Rite or a
        Fanfare says to evolve a follower directly.  The latter is a free
        state transition but still emits the ordinary evolve/Faith/listener
        events.  Keeping it separate prevents automatic evolutions from
        consuming the player's limited EP/SEP pool.
        """
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == follower_uid), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"follower {follower_uid} not found",))
        follower = state.my_board[index]
        if follower.is_evolved:
            return InterpreterResult(state, warnings=(f"{follower.name} already evolved",))
        next_state = state.clone()
        self._invalidate_snapshot_legality(next_state)
        delta = 3 if super_evolve else 2
        trigger = "on_super_evolve" if super_evolve else "on_evolve"
        next_state.my_board[index] = replace(
            follower,
            atk=follower.atk + delta,
            hp=follower.hp + delta,
            is_evolved=True,
            is_super_evolved=super_evolve,
            can_attack_leader=follower.has_storm or _has_keyword(follower, "storm"),
            can_attack_field=True,
        )
        next_state.evolved_allies_this_turn += 1
        next_state.evolved_allies_this_match += 1
        rule = self.rules.get(follower.card_id, {})
        result = self._resolve_abilities(next_state, rule, "normal", trigger, source_uid=follower_uid, target_uid=target_uid)
        faith_result = self._resolve_faith_trigger(result.state, "on_ally_follower_evolve", source_uid=follower_uid)
        current = faith_result.state
        if super_evolve:
            super_faith = self._resolve_faith_trigger(current, "on_ally_follower_super_evolve", source_uid=follower_uid)
            current = super_faith.state
        else:
            super_faith = InterpreterResult(current)
        global_trigger = self._resolve_board_trigger(
            current,
            "on_ally_follower_super_evolve" if super_evolve else "on_ally_follower_evolve",
        )
        current = global_trigger.state
        unsupported = set(result.unsupported_ops) | set(faith_result.unsupported_ops) | set(super_faith.unsupported_ops) | set(global_trigger.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{follower.card_id}")
        return InterpreterResult(
            current,
            tuple(sorted(unsupported)),
            result.warnings + faith_result.warnings + super_faith.warnings + global_trigger.warnings,
        )

    def auto_evolve_branches(
        self,
        state: LethalState,
        follower_uid: int,
        *,
        super_evolve: bool = False,
        target_uid: Any = None,
    ) -> list[StochasticBranch]:
        """Branching counterpart of :meth:`auto_evolve`."""
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == follower_uid), None)
        if index is None:
            return [StochasticBranch(1.0, state, warnings=(f"follower {follower_uid} not found",))]
        follower = state.my_board[index]
        if follower.is_evolved:
            return [StochasticBranch(1.0, state, warnings=(f"{follower.name} already evolved",))]
        current = state.clone()
        self._invalidate_snapshot_legality(current)
        delta = 3 if super_evolve else 2
        trigger = "on_super_evolve" if super_evolve else "on_evolve"
        current.my_board[index] = replace(
            follower,
            atk=follower.atk + delta,
            hp=follower.hp + delta,
            is_evolved=True,
            is_super_evolved=super_evolve,
            can_attack_leader=follower.has_storm,
            can_attack_field=True,
        )
        current.evolved_allies_this_turn += 1
        current.evolved_allies_this_match += 1
        rule = self.rules.get(follower.card_id, {})
        branches = self._resolve_abilities_branches(
            current,
            rule,
            "normal",
            trigger,
            source_uid=follower_uid,
            target_uid=target_uid,
        )
        branches = self._chain_faith_trigger_branches(branches, "on_ally_follower_evolve", source_uid=follower_uid)
        if super_evolve:
            branches = self._chain_faith_trigger_branches(branches, "on_ally_follower_super_evolve", source_uid=follower_uid)
        branches = self._chain_board_trigger_branches(
            branches,
            "on_ally_follower_super_evolve" if super_evolve else "on_ally_follower_evolve",
        )
        if rule.get("support") in ("partial", "unsupported"):
            branches = [
                StochasticBranch(
                    branch.probability,
                    branch.state,
                    tuple(sorted(set(branch.unsupported_ops) | {f"{rule.get('support')}_rule:{follower.card_id}"})),
                    branch.warnings,
                )
                for branch in branches
            ]
        return self._merge_stochastic(branches)

    def evolve_branches(self, state: LethalState, attacker_uid: int, *, super_evolve: bool = False, target_uid: Any = None) -> list[StochasticBranch]:
        """Evolve and resolve an evolve trigger with random outcomes kept."""
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        if index is None:
            return [StochasticBranch(1.0, state, warnings=(f"follower {attacker_uid} not found",))]
        follower = state.my_board[index]
        if getattr(state, "legal_actions_known", False):
            allowed = state.legal_super_evolve_uids if super_evolve else state.legal_evolve_uids
            if attacker_uid not in allowed:
                return [StochasticBranch(1.0, state, warnings=(f"Tracker forbids evolving follower {attacker_uid}",))]
        if not self._evolution_unlocked(state, super_evolve=super_evolve):
            return [StochasticBranch(1.0, state, warnings=(self._evolution_unlock_warning(state, super_evolve=super_evolve),))]
        if getattr(state, "manual_evolutions_this_turn", 0) > 0:
            return [StochasticBranch(1.0, state, warnings=("only one manual evolution is allowed per turn",))]
        if follower.is_evolved:
            return [StochasticBranch(1.0, state, warnings=(f"{follower.name} already evolved",))]
        points = state.sep if super_evolve else state.ep
        if points <= 0:
            return [StochasticBranch(1.0, state, warnings=("no evolve points",))]
        current = state.clone()
        self._invalidate_snapshot_legality(current)
        if super_evolve:
            current.sep -= 1
            delta = 3
            trigger = "on_super_evolve"
        else:
            current.ep -= 1
            delta = 2
            trigger = "on_evolve"
        current.my_board[index] = replace(follower, atk=follower.atk + delta, hp=follower.hp + delta, is_evolved=True, is_super_evolved=super_evolve, can_attack_leader=_has_keyword(follower, "storm"), can_attack_field=True)
        current.evolved_allies_this_turn += 1
        current.manual_evolutions_this_turn += 1
        current.evolved_allies_this_match += 1
        rule = self.rules.get(follower.card_id, {})
        branches = self._resolve_abilities_branches(current, rule, "normal", trigger, source_uid=follower.unique_id, target_uid=target_uid)
        branches = self._chain_faith_trigger_branches(branches, "on_ally_follower_evolve", source_uid=follower.unique_id)
        if super_evolve:
            branches = self._chain_faith_trigger_branches(branches, "on_ally_follower_super_evolve", source_uid=follower.unique_id)
        branches = self._chain_board_trigger_branches(branches, "on_ally_follower_super_evolve" if super_evolve else "on_ally_follower_evolve")
        if rule.get("support") in ("partial", "unsupported"):
            branches = [StochasticBranch(b.probability, b.state, tuple(sorted(set(b.unsupported_ops) | {f"{rule.get('support')}_rule:{follower.card_id}"})), b.warnings) for b in branches]
        return self._merge_stochastic(branches)

    def engage(self, state: LethalState, source_uid: int, target_uid: Any = None) -> InterpreterResult:
        """Activate an allied amulet's Engage ability.

        Amulets are currently carried by the unified board model; countdown
        changes are applied when the rule exposes a deterministic delta.
        """
        source = next((f for f in state.my_board if f.unique_id == source_uid), None)
        if source is None:
            return InterpreterResult(state, warnings=(f"engage source {source_uid} not found",))
        if getattr(state, "legal_actions_known", False):
            allowed = set(state.legal_actions.get("can_activation_field_cards", ())) | set(state.legal_actions.get("can_activation_field_cards_with_extra_pp", ()))
            if source_uid not in allowed:
                return InterpreterResult(state, warnings=(f"Tracker forbids Engage for {source_uid}",))
        rule = self.rules.get(source.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, Mapping) and m.get("kind") == "normal"), {})
        engage_cost = next((a.get("cost", 0) for a in selected.get("abilities", ()) if isinstance(a, Mapping) and a.get("trigger") == "on_engage"), 0)
        if state.pp < engage_cost:
            return InterpreterResult(state, warnings=(f"insufficient PP for Engage {source.name}",))
        working = state.clone()
        self._invalidate_snapshot_legality(working)
        working.pp -= int(engage_cost or 0)
        result = self._resolve_abilities(working, rule, "normal", "on_engage", source_uid=source_uid, target_uid=target_uid)
        unsupported = set(result.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{source.card_id}")
        return InterpreterResult(result.state, tuple(sorted(unsupported)), result.warnings)

    def engage_branches(self, state: LethalState, source_uid: int, target_uid: Any = None) -> list[StochasticBranch]:
        """Activate an amulet while preserving random Engage outcomes.

        Engage is a board action rather than a hand play, but its payload can
        still draw, transform from a random deck, or replicate another
        ability.  Keep the same branch contract as ``play_branches`` so the
        lethal searcher never collapses those outcomes into one guessed card.
        """
        source = next((f for f in state.my_board if f.unique_id == source_uid), None)
        if source is None:
            return [StochasticBranch(1.0, state, warnings=(f"engage source {source_uid} not found",))]
        if getattr(state, "legal_actions_known", False):
            allowed = set(state.legal_actions.get("can_activation_field_cards", ())) | set(state.legal_actions.get("can_activation_field_cards_with_extra_pp", ()))
            if source_uid not in allowed:
                return [StochasticBranch(1.0, state, warnings=(f"Tracker forbids Engage for {source_uid}",))]
        rule = self.rules.get(source.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, Mapping) and m.get("kind") == "normal"), {})
        engage_cost = next((a.get("cost", 0) for a in selected.get("abilities", ()) if isinstance(a, Mapping) and a.get("trigger") == "on_engage"), 0)
        if self._available_pp(state) < int(engage_cost or 0):
            return [StochasticBranch(1.0, state, warnings=(f"insufficient PP for Engage {source.name}",))]
        working = state.clone()
        self._invalidate_snapshot_legality(working)
        if not self._pay_pp(working, int(engage_cost or 0)):
            return [StochasticBranch(1.0, state, warnings=(f"insufficient PP for Engage {source.name}",))]
        branches = self._resolve_abilities_branches(working, rule, "normal", "on_engage", source_uid=source_uid, target_uid=target_uid)
        if rule.get("support") in ("partial", "unsupported"):
            branches = [StochasticBranch(
                branch.probability,
                branch.state,
                tuple(sorted(set(branch.unsupported_ops) | {f"{rule.get('support')}_rule:{source.card_id}"})),
                branch.warnings,
            ) for branch in branches]
        return self._merge_stochastic(branches)

    def end_turn(self, state: LethalState, *, opponent: bool = False) -> InterpreterResult:
        """Resolve end-of-turn triggers and advance Crest countdowns.

        This is intentionally explicit: callers deciding a lethal line must
        opt into a turn boundary; merely inspecting a state never advances a
        resource or countdown.  Crest instances are independent, so one
        reaching zero does not consume or reset its siblings.
        """
        trigger = "on_opponent_turn_end" if opponent else "on_turn_end"
        result = self._resolve_board_trigger(state, trigger)
        current = result.state.clone()
        self._invalidate_snapshot_legality(current)
        unsupported = set(result.unsupported_ops)
        warnings = list(result.warnings)
        crest_result = self._resolve_crest_trigger(current, trigger)
        current = crest_result.state
        unsupported.update(crest_result.unsupported_ops)
        warnings.extend(crest_result.warnings)
        if not opponent:
            invoked = self._resolve_deck_invocations(current, trigger)
            current = invoked.state
            unsupported.update(invoked.unsupported_ops)
            warnings.extend(invoked.warnings)
        expired: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for item in current.crest_instances:
            item = dict(item)
            countdown = item.get("countdown")
            if isinstance(countdown, int) and countdown >= 0:
                item["countdown"] = max(0, countdown - 1)
            if item.get("countdown") == 0:
                expired.append(item)
            else:
                remaining.append(item)
        current.crest_instances = remaining
        if current.crest_instances:
            current.active_crests = [int(item.get("card_id", 0)) for item in current.crest_instances]
        elif expired or current.active_crests:
            expired_ids = {int(item.get("card_id", 0)) for item in expired}
            current.active_crests = [cid for cid in current.active_crests if cid not in expired_ids]
        if opponent and current.enemy_crest_instances:
            enemy_remaining: list[dict[str, Any]] = []
            enemy_expired_ids: set[int] = set()
            for item in current.enemy_crest_instances:
                item = dict(item)
                countdown = item.get("countdown")
                if isinstance(countdown, int) and countdown >= 0:
                    item["countdown"] = max(0, countdown - 1)
                if item.get("countdown") == 0:
                    enemy_expired_ids.add(int(item.get("card_id", 0) or 0))
                else:
                    enemy_remaining.append(item)
            current.enemy_crest_instances = enemy_remaining
            if current.enemy_crest_instances:
                current.enemy_active_crests = [int(item.get("card_id", 0)) for item in current.enemy_crest_instances]
            elif enemy_expired_ids or current.enemy_active_crests:
                current.enemy_active_crests = [cid for cid in current.enemy_active_crests if cid not in enemy_expired_ids]
        # ``attacked_with_follower_this_turn`` is scoped to the turn boundary;
        # it is observable while resolving the boundary and then cleared for
        # the next turn.
        current.attacked_with_follower_this_turn = False
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def start_turn(self, state: LethalState) -> InterpreterResult:
        """Resolve start-of-turn listeners, including known deck Invokes."""
        result = self._resolve_board_trigger(state, "on_turn_start")
        invoked = self._resolve_deck_invocations(result.state, "on_turn_start")
        unsupported = tuple(sorted(set(result.unsupported_ops) | set(invoked.unsupported_ops)))
        return InterpreterResult(invoked.state, unsupported, result.warnings + invoked.warnings)

    # Friendly alias used by callers that model the game event name.
    advance_turn_end = end_turn

    def _resolve_crest_trigger(self, state: LethalState, trigger: str) -> InterpreterResult:
        """Resolve abilities attached to each Crest instance before countdown."""
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # A countdown event is emitted while the instance still exists, so a
        # Crest reaching one can react before it expires at this turn end.
        for instance in list(current.crest_instances):
            if not isinstance(instance, Mapping):
                continue
            abilities = instance.get("abilities", ())
            if not abilities:
                # Crest instances coming from Tracker may carry only their
                # identity/countdown.  Rehydrate the catalog-defined trigger
                # lazily so existing board Crests behave like newly gained
                # ones without requiring a second snapshot pass.
                abilities = self._catalog_crest_abilities(int(instance.get("card_id", 0) or 0))
            selected: list[Mapping[str, Any]] = []
            if isinstance(abilities, (list, tuple)):
                selected.extend(item for item in abilities if isinstance(item, Mapping) and item.get("trigger") in (trigger, "on_crest"))
                countdown = instance.get("countdown")
                if isinstance(countdown, int) and countdown <= 1:
                    selected.extend(item for item in abilities if isinstance(item, Mapping) and item.get("trigger") == "on_crest_countdown")
            for ability in selected:
                condition = ability.get("condition")
                if condition is not None:
                    verdict = self._condition_met(current, condition)
                    if verdict is None:
                        unsupported.add("conditional")
                        continue
                    if not verdict:
                        continue
                current, ops, warns = self._effects(current, ability.get("effects", ()), int(instance.get("unique_id", 0) or 0))
                unsupported.update(ops)
                warnings.extend(warns)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def select_mode(self, state: LethalState, count: int = 1) -> InterpreterResult:
        """Emit the public ``on_mode_selected`` event for every Faith instance.

        Faith instances are independent resource objects.  A granted ability
        is evaluated once per matching instance; an unscoped Faith mutation is
        scoped to that instance while it runs, preventing two Faiths from
        multiplying the same global delta accidentally.
        """
        current = state.clone()
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Work against the *current* state by index.  Resolving an ability
        # clones the state; iterating a stale list would otherwise discard
        # the mode-selection increment for every Faith after the first one.
        for index in range(len(current.faith_instances)):
            if index >= len(current.faith_instances):
                break
            instance = current.faith_instances[index]
            if not isinstance(instance, Mapping):
                continue
            instance["value"] = int(instance.get("value", 0) or 0) + int(count)
            for ability in instance.get("abilities", ()) if isinstance(instance.get("abilities"), (list, tuple)) else ():
                if not isinstance(ability, Mapping) or ability.get("trigger") != "on_mode_selected":
                    continue
                scoped_effects = []
                for effect in ability.get("effects", ()):
                    if isinstance(effect, Mapping):
                        effect_copy = dict(effect)
                        if effect_copy.get("resource") == "faith" and "source_card_id" not in effect_copy:
                            effect_copy["source_card_id"] = instance.get("source_card_id")
                        scoped_effects.append(effect_copy)
                current, ops, warns = self._effects(current, scoped_effects, int(instance.get("unique_id", 0) or 0))
                unsupported.update(ops)
                warnings.extend(warns)
        self._sync_faith_aggregate(current)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _resolve_faith_trigger(self, state: LethalState, trigger: str, *, source_uid: int = 0) -> InterpreterResult:
        """Run one public event against each independent Faith instance."""
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Use instance order as shown by Tracker.  An ability can itself
        # mutate Faith, so fetch the instance from the current state for each
        # iteration instead of retaining a stale dict reference.
        for index in range(len(current.faith_instances)):
            if index >= len(current.faith_instances):
                break
            instance = current.faith_instances[index]
            if not isinstance(instance, Mapping):
                continue
            abilities = instance.get("abilities", ())
            for ability in abilities if isinstance(abilities, (list, tuple)) else ():
                if not isinstance(ability, Mapping) or ability.get("trigger") != trigger:
                    continue
                scoped_effects: list[dict[str, Any]] = []
                for effect in ability.get("effects", ()):
                    if not isinstance(effect, Mapping):
                        continue
                    copy_effect = dict(effect)
                    if copy_effect.get("resource") == "faith" and "source_card_id" not in copy_effect:
                        copy_effect["source_card_id"] = instance.get("source_card_id")
                    scoped_effects.append(copy_effect)
                current, ops, warns = self._effects(current, scoped_effects, int(instance.get("unique_id", 0) or 0))
                unsupported.update(ops)
                warnings.extend(warns)
        self._sync_faith_aggregate(current)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _chain_faith_trigger_branches(self, branches: list[StochasticBranch], trigger: str, *, source_uid: int = 0) -> list[StochasticBranch]:
        """Probability-preserving Faith trigger dispatch (currently deterministic)."""
        output: list[StochasticBranch] = []
        for branch in branches:
            result = self._resolve_faith_trigger(branch.state, trigger, source_uid=source_uid)
            output.append(StochasticBranch(branch.probability, result.state, tuple(sorted(set(branch.unsupported_ops) | set(result.unsupported_ops))), tuple(branch.warnings) + tuple(result.warnings)))
        return self._merge_stochastic(output)

    def _resolve_abilities(self, state: LethalState, rule: Mapping[str, Any], mode: str, trigger: str, *, source_uid: int, target_uid: Any = None, choice: Any = None, event_uid: int | None = None) -> InterpreterResult:
        modes = rule.get("modes", ()) if isinstance(rule, Mapping) else ()
        selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if selected is None and mode == "normal":
            selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == "normal"), None)
        abilities = list(selected.get("abilities", ())) if isinstance(selected, Mapping) else []
        # A status granted to a particular entity is not part of that card's
        # printed rule.  Dispatch it alongside the printed abilities so
        # delayed copies and persistent listeners survive exact-copy state.
        source_follower = next((f for f in state.my_board if f.unique_id == source_uid), None)
        # ``remove_abilities`` is a stateful effect.  Once the source entity
        # has been stripped, its printed and granted listeners must not fire
        # on later evolve/attack/turn events even though the catalog rule is
        # still available by card id.
        if source_follower is not None and source_follower.abilities_removed:
            return InterpreterResult(state)
        if source_follower is not None:
            abilities.extend(item for item in source_follower.granted_abilities if isinstance(item, Mapping))
        unsupported: set[str] = set()
        warnings: list[str] = []
        current = state
        for ability in abilities:
            if not isinstance(ability, Mapping) or ability.get("trigger") != trigger:
                continue
            trigger_filter = ability.get("trigger_filter")
            if event_uid is None:
                # An unfiltered board event is dispatched once.  Filtered
                # listeners wait for the event-specific pass below.
                if isinstance(trigger_filter, Mapping):
                    continue
            elif not isinstance(trigger_filter, Mapping) or not self._trigger_filter_matches(current, trigger_filter, event_uid):
                continue
            if self._contains_op(ability.get("effects", ()), "mode_choice") and current.super_skybound_art > 0 and self._mode_has_activate_all(rule, mode):
                # Super Skybound Art replaces the ordinary “choose one”
                # branch; executing both would double-apply the selected
                # mode.
                continue
            condition = ability.get("condition")
            if condition is not None:
                condition_result = self._condition_met(current, condition)
                if condition_result is None:
                    unsupported.add("conditional")
                    continue
                if not condition_result:
                    continue
            effective_choice = self._choice_for_ability(rule, mode, ability, current, choice)
            expanded_effects, replicate_ops = self._expand_replicated_effects(
                current, rule, mode, ability, source_trigger=trigger
            )
            # For a filtered global listener, “it/trigger_source” denotes the
            # entity that entered the field, not the listener card carrying
            # the ability.  Keep ``source_uid`` for granted-ability lookup but
            # use the event entity as the effect source.
            effect_source_uid = event_uid if event_uid is not None else source_uid
            current, ops, warns = self._effects(current, expanded_effects, effect_source_uid, target_uid, choice=effective_choice)
            ops.update(replicate_ops)
            unsupported.update(ops)
            warnings.extend(warns)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _expand_replicated_effects(
        self,
        state: LethalState,
        rule: Mapping[str, Any],
        mode: str,
        ability: Mapping[str, Any],
        *,
        source_trigger: str,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Inline ``replicate_ability`` without re-dispatching the source.

        Card text commonly says “Replicate the effects of this card's
        Fanfare ability” on an Evolve or Engage line.  The compiler stores
        that as a small marker so that the same Fanfare payload can be used
        by both events.  Expanding it here keeps ordering (for example,
        Engage first destroys the amulet, then repeats its Fanfare) and avoids
        the infinite recursion that would result from calling the outer
        trigger again.  Nested replication is deliberately reported as a
        gap: there is no finite source event to infer safely.
        """
        modes = rule.get("modes", ()) if isinstance(rule, Mapping) else ()
        selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if selected is None and mode == "normal":
            selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == "normal"), None)
        abilities = selected.get("abilities", ()) if isinstance(selected, Mapping) else ()
        gaps: set[str] = set()

        def expand(items: Any, stack: tuple[int, ...] = ()) -> list[dict[str, Any]]:
            output: list[dict[str, Any]] = []
            for item in items if isinstance(items, (list, tuple)) else ():
                if not isinstance(item, Mapping):
                    continue
                if item.get("op") == "replicate_ability":
                    wanted_trigger = str(item.get("trigger") or "on_fanfare")
                    sources = [
                        candidate for candidate in abilities
                        if isinstance(candidate, Mapping)
                        and candidate is not ability
                        and candidate.get("trigger") == wanted_trigger
                    ]
                    if not sources:
                        gaps.add(f"replicate_source:{wanted_trigger}")
                        continue
                    for candidate in sources:
                        marker = id(candidate)
                        if marker in stack:
                            gaps.add("replicate_recursive")
                            continue
                        condition = candidate.get("condition")
                        if condition is not None:
                            verdict = self._condition_met(state, condition)
                            if verdict is None:
                                gaps.add("replicate_condition")
                                continue
                            if not verdict:
                                continue
                        output.extend(expand(candidate.get("effects", ()), stack + (marker,)))
                    continue
                copied = dict(item)
                if isinstance(item.get("effects"), (list, tuple)):
                    copied["effects"] = expand(item.get("effects", ()), stack)
                if isinstance(item.get("else_effects"), (list, tuple)):
                    copied["else_effects"] = expand(item.get("else_effects", ()), stack)
                if isinstance(item.get("choices"), (list, tuple)):
                    choices = []
                    for choice_item in item.get("choices", ()):
                        if not isinstance(choice_item, Mapping):
                            continue
                        choice_copy = dict(choice_item)
                        if isinstance(choice_item.get("effects"), (list, tuple)):
                            choice_copy["effects"] = expand(choice_item.get("effects", ()), stack)
                        choices.append(choice_copy)
                    copied["choices"] = choices
                output.append(copied)
            return output

        return expand(ability.get("effects", ())), gaps

    def _choice_for_ability(self, rule: Mapping[str, Any], mode: str, ability: Mapping[str, Any], state: LethalState, choice: Any) -> Any:
        """Resolve the special ``activate_all_mode_choices`` payload.

        The compiler emits the mode choices and the Super Skybound Art
        ability as separate effects.  At runtime the latter means *all*
        branches, not the label selected for the ordinary Mode action.  The
        helper expands that relationship without changing the public
        ``play(..., choice=...)`` API.
        """
        effects = ability.get("effects", ()) if isinstance(ability, Mapping) else ()
        if not self._contains_op(effects, "activate_all_mode_choices"):
            return choice
        choices = self._collect_mode_choices(rule, mode)
        return choices if choices else choice

    @staticmethod
    def _contains_op(effects: Any, op_name: str) -> bool:
        for effect in effects if isinstance(effects, (list, tuple)) else ():
            if not isinstance(effect, Mapping):
                continue
            if effect.get("op") == op_name:
                return True
            if EventInterpreter._contains_op(effect.get("effects", ()), op_name) or EventInterpreter._contains_op(effect.get("else_effects", ()), op_name):
                return True
            if any(EventInterpreter._contains_op(item.get("effects", ()), op_name) for item in effect.get("steps", ()) if isinstance(item, Mapping)):
                return True
            if any(EventInterpreter._contains_op(item.get("effects", ()), op_name) for item in effect.get("choices", ()) if isinstance(item, Mapping)):
                return True
        return False

    @staticmethod
    def _collect_mode_choices(rule: Mapping[str, Any], mode: str) -> list[dict[str, Any]]:
        selected = next((item for item in rule.get("modes", ()) if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if not isinstance(selected, Mapping):
            selected = next((item for item in rule.get("modes", ()) if isinstance(item, Mapping) and item.get("kind") == "normal"), None)
        def find(effects: Any) -> list[dict[str, Any]]:
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, Mapping):
                    continue
                if effect.get("op") == "mode_choice":
                    return [dict(item) for item in effect.get("choices", ()) if isinstance(item, Mapping)]
                found = find(effect.get("effects", ()))
                if found:
                    return found
            return []
        for ability in selected.get("abilities", ()):
            if isinstance(ability, Mapping):
                result = find(ability.get("effects", ()))
                if result:
                    return result
        return []

    @staticmethod
    def _mode_has_activate_all(rule: Mapping[str, Any], mode: str) -> bool:
        selected = next((item for item in rule.get("modes", ()) if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if not isinstance(selected, Mapping):
            return False
        return any(EventInterpreter._contains_op(ability.get("effects", ()), "activate_all_mode_choices") for ability in selected.get("abilities", ()) if isinstance(ability, Mapping))

    @staticmethod
    def _mode_has_choice(rule: Mapping[str, Any], mode: str) -> bool:
        """Whether the selected mode exposes a player-selectable Mode node."""
        selected = next((item for item in rule.get("modes", ()) if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if not isinstance(selected, Mapping):
            return False
        return any(EventInterpreter._contains_op(ability.get("effects", ()), "mode_choice") for ability in selected.get("abilities", ()) if isinstance(ability, Mapping))

    @staticmethod
    def _mode_selection_count(rule: Mapping[str, Any], mode: str, choice: Any = None) -> int:
        """Return how many Mode selections one play represents.

        ``mode_choice.selection_count`` is normally a small integer in the
        compiled rule.  A direct caller may pass a tuple for a multi-select
        action, so use its length as a conservative fallback while still
        honoring the rule-declared count when present.
        """
        selected = next(
            (item for item in rule.get("modes", ())
             if isinstance(item, Mapping) and item.get("kind") == mode),
            None,
        )

        def find(effects: Any) -> int | None:
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, Mapping):
                    continue
                if effect.get("op") == "mode_choice":
                    raw = effect.get("selection_count", 1)
                    try:
                        return max(1, int(raw))
                    except (TypeError, ValueError):
                        return max(1, len(choice) if isinstance(choice, (list, tuple)) else 1)
                nested = find(effect.get("effects", ()))
                if nested is not None:
                    return nested
                nested = find(effect.get("else_effects", ()))
                if nested is not None:
                    return nested
            return None

        if isinstance(selected, Mapping):
            for ability in selected.get("abilities", ()):
                if isinstance(ability, Mapping):
                    result = find(ability.get("effects", ()))
                    if result is not None:
                        return result
        return max(1, len(choice) if isinstance(choice, (list, tuple)) else 1)

    def _resource_preflight(self, state: LethalState, rule: Mapping[str, Any], mode: str, *, source_uid: int, choice: Any = None) -> tuple[str, int, int] | None:
        """Check unconditional/selected resource payments before a play.

        Effects are authored as a sequence, but a failed payment must not
        leave an earlier damage or summon behind.  We therefore validate the
        selected branch before removing the card or paying PP.  Unknown
        variable payments remain a normal interpreter gap instead of being
        guessed.  The source spell is added to the cemetery after its own
        abilities resolve, so its shadow cannot pay its own Necromancy/Earth
        Rite-style gate while the card is still resolving.
        """
        selected = next((item for item in rule.get("modes", ()) if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if not isinstance(selected, Mapping):
            return None
        # Keep the pre-play resource snapshot.  A spell's own cemetery entry
        # is committed only after its effects finish resolving.
        preflight_state = state
        def available(resource: Any, current: LethalState) -> int | None:
            return {
                "cemetery": current.cemetery,
                "necromancy": current.cemetery,
                "faith": current.faith,
                "earth_sigil": current.earth_sigil,
                "earth_rite": current.earth_sigil,
                "ep": current.ep,
                "sep": current.sep,
                "extra_pp": current.extra_pp,
                "rally": current.rally,
                "play_count": current.play_count,
                "pp": current.pp,
                "max_pp": current.max_pp,
                "skybound_art": current.skybound_art,
                "super_skybound_art": current.super_skybound_art,
            }.get(resource)

        def apply_shadow(current: LethalState, resource: Any, delta: int, effect: Mapping[str, Any] | None = None) -> LethalState:
            """Apply only resource state to the preflight shadow.

            The real effect resolver is intentionally not called here: it may
            damage, summon, or choose targets.  The shadow nevertheless needs
            cumulative resource changes so two payments in one ability are
            checked against the same pool and later conditions see the value
            after earlier payments.
            """
            updated = current.clone()
            if resource == "faith":
                updated.faith = max(0, updated.faith + delta)
                source_card_id = effect.get("source_card_id") if isinstance(effect, Mapping) else None
                matches = [item for item in updated.faith_instances if source_card_id is None or item.get("source_card_id") == source_card_id]
                if matches:
                    if delta < 0:
                        remaining = -delta
                        for item in matches:
                            used = min(int(item.get("value", 0) or 0), remaining)
                            item["value"] = int(item.get("value", 0) or 0) - used
                            remaining -= used
                            if remaining == 0:
                                break
                    else:
                        for item in matches:
                            item["value"] = max(0, int(item.get("value", 0) or 0) + delta)
                    self._sync_faith_aggregate(updated)
            elif resource in ("cemetery", "necromancy"):
                updated.cemetery = max(0, updated.cemetery + delta)
            elif resource in ("earth_sigil", "earth_rite"):
                updated.earth_sigil = max(0, updated.earth_sigil + delta)
            elif resource == "ep":
                updated.ep = max(0, updated.ep + delta)
            elif resource == "sep":
                updated.sep = max(0, updated.sep + delta)
            elif resource == "extra_pp":
                updated.extra_pp = max(0, updated.extra_pp + delta)
            elif resource == "rally":
                updated.rally = max(0, updated.rally + delta)
            elif resource == "play_count":
                updated.play_count = max(0, updated.play_count + delta)
            elif resource == "pp":
                updated.pp = max(0, min(updated.max_pp, updated.pp + delta))
            elif resource == "max_pp":
                updated.max_pp = max(0, updated.max_pp + delta)
                updated.pp = min(updated.max_pp, updated.pp)
            elif resource == "skybound_art":
                updated.skybound_art = max(0, updated.skybound_art + delta)
            elif resource == "super_skybound_art":
                updated.super_skybound_art = max(0, updated.super_skybound_art + delta)
            return updated

        def visit(effects: Any, current: LethalState) -> tuple[LethalState, tuple[str, int, int] | None]:
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, Mapping):
                    continue
                op = effect.get("op")
                if op == "consume_resource":
                    value = self._resolve_value(current, effect.get("amount"), source_uid)
                    if value is None:
                        continue
                    need = max(0, int(value))
                    have = available(effect.get("resource"), current)
                    if have is not None and have < need:
                        return current, (str(effect.get("resource")), need, int(have))
                    current = apply_shadow(current, effect.get("resource"), -need, effect)
                elif op == "modify_resource":
                    # Negative resource deltas in the card text are costs
                    # (for example, “reduce Faith by 10”).  Treat them as a
                    # pre-play gate so a preceding damage/summon cannot
                    # resolve when the payment is unavailable.
                    value = self._resolve_value(current, effect.get("amount"), source_uid)
                    resource = effect.get("resource")
                    if value is not None and int(value) < 0 and effect.get("field") not in ("mode_limit", "mode_limit_bonus"):
                        have = available(resource, current)
                        need = -int(value)
                        if have is not None and have < need:
                            return current, (str(resource), need, int(have))
                    if value is not None and effect.get("field") not in ("mode_limit", "mode_limit_bonus"):
                        current = apply_shadow(current, resource, int(value), effect)
                elif op == "conditional":
                    verdict = self._condition_met(current, effect.get("condition", {}))
                    if verdict is True:
                        current, failure = visit(effect.get("effects", ()), current)
                        if failure:
                            return current, failure
                    elif verdict is False:
                        current, failure = visit(effect.get("else_effects", ()), current)
                        if failure:
                            return current, failure
                elif op == "sequence":
                    current, failure = visit(effect.get("effects", ()), current)
                    if failure:
                        return current, failure
                elif op == "mode_choice":
                    choices = effect.get("choices", ())
                    if isinstance(choices, (list, tuple)) and choice is not None:
                        selected_values = list(choice) if isinstance(choice, (list, tuple)) and not all(isinstance(item, Mapping) for item in choice) else [choice]
                        selected_choices: list[Mapping[str, Any]] = []
                        for selected_value in selected_values:
                            selected_choice = None
                            if isinstance(selected_value, Mapping):
                                selected_choice = selected_value
                            elif isinstance(selected_value, int) and 0 <= selected_value < len(choices):
                                selected_choice = choices[selected_value]
                            else:
                                selected_choice = next((item for item in choices if isinstance(item, Mapping) and str(item.get("label")) == str(selected_value)), None)
                            if isinstance(selected_choice, Mapping):
                                selected_choices.append(selected_choice)
                        for selected_choice in selected_choices:
                            current, failure = visit(selected_choice.get("effects", ()), current)
                            if failure:
                                return current, failure
                elif op == "activate_all_mode_choices":
                    # Super Skybound Art executes every numbered Mode.  Use
                    # the explicit payload when supplied; for a direct play
                    # call derive the same choices from the selected rule so
                    # Faith/Earth/etc. costs are preflighted before mutation.
                    selected_choices = choice if isinstance(choice, (list, tuple)) else self._collect_mode_choices(rule, mode)
                    for selected_choice in selected_choices if isinstance(selected_choices, (list, tuple)) else ():
                        if isinstance(selected_choice, Mapping):
                            current, failure = visit(selected_choice.get("effects", ()), current)
                            if failure:
                                return current, failure
                elif op == "repeat":
                    count = self._resolve_value(current, effect.get("count"), source_uid)
                    if count is not None:
                        for _ in range(max(0, int(count))):
                            current, failure = visit(effect.get("effects", ()), current)
                            if failure:
                                return current, failure
            return current, None
        for ability in selected.get("abilities", ()):
            if not isinstance(ability, Mapping) or ability.get("trigger") not in ("on_play", "on_fanfare"):
                continue
            condition = ability.get("condition")
            if condition is not None and self._condition_met(preflight_state, condition) is not True:
                continue
            _, failure = visit(ability.get("effects", ()), preflight_state)
            if failure:
                return failure
        return None

    def _trigger_filter_matches(self, state: LethalState, trigger_filter: Mapping[str, Any], event_uid: int) -> bool:
        """Check a filtered board event against the entering entity."""
        follower = next((item for item in state.my_board if item.unique_id == event_uid), None)
        if follower is None:
            return False
        return self._matches_follower_filters(follower, "ally", trigger_filter)

    def _resolve_board_trigger(self, state: LethalState, trigger: str, *, event_uid: int | None = None) -> InterpreterResult:
        """Resolve a trigger for allied followers currently on board.

        With ``event_uid`` set, only abilities carrying a matching
        ``trigger_filter`` run.  The unfiltered pass remains the historical
        “one event observed by every listener” behavior.
        """
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Snapshot IDs because an effect may remove or add followers.
        source_ids = [f.unique_id for f in current.my_board]
        for source_uid in source_ids:
            if not any(f.unique_id == source_uid for f in current.my_board):
                continue
            follower = next(f for f in current.my_board if f.unique_id == source_uid)
            if follower.abilities_removed:
                continue
            rule = self.rules.get(follower.card_id, {})
            result = self._resolve_abilities(current, rule, "normal", trigger, source_uid=source_uid, event_uid=event_uid)
            current = result.state
            unsupported.update(result.unsupported_ops)
            warnings.extend(result.warnings)
            if rule.get("support") in ("partial", "unsupported"):
                unsupported.add(f"{rule.get('support')}_rule:{follower.card_id}")
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    @staticmethod
    def _condition_met(state: LethalState, condition: Any) -> bool | None:
        if not isinstance(condition, Mapping):
            return None
        if "all" in condition:
            values = [EventInterpreter._condition_met(state, item) for item in condition["all"]]
            return None if any(value is None for value in values) else all(values)
        if "any" in condition:
            values = [EventInterpreter._condition_met(state, item) for item in condition["any"]]
            return None if any(value is None for value in values) else any(values)
        if "not" in condition:
            value = EventInterpreter._condition_met(state, condition["not"])
            return None if value is None else not value
        crest_count = len(state.crest_instances) if state.crest_instances else len(state.active_crests)
        faith_instances = state.faith_instances
        evolved = any(f.is_evolved for f in state.my_board)
        super_evolved = any(f.is_super_evolved for f in state.my_board)
        state_name = condition.get("state")
        values = {
            "pp": state.pp,
            "max_pp": state.max_pp,
            "extra_pp": state.extra_pp,
            "ep": state.ep,
            "sep": state.sep,
            "rally": state.rally,
            "play_count": state.play_count,
            "cemetery": state.cemetery,
            "necromancy": state.cemetery,
            "earth_sigil": state.earth_sigil,
            "earth_rite": state.earth_sigil,
            # Awakening is exposed by Tracker, but deriving it from the
            # seven-PP threshold keeps hand-built fixtures faithful too.
            "awakening": bool(state.is_awakening or state.max_pp >= 7),
            "is_awakening": bool(state.is_awakening or state.max_pp >= 7),
            "ally_board_count": len(state.my_board),
            "enemy_board_count": len(state.enemy_board),
            "board_count": len(state.my_board) + len(state.enemy_board),
            "crest_count": crest_count,
            "crest.count": crest_count,
            "faith": state.faith,
            "faith.value": state.faith,
            "faith.instance_count": len(faith_instances),
            "skybound_art": state.skybound_art,
            "super_skybound_art": state.super_skybound_art,
            "evolved": evolved,
            "super_evolved": super_evolved,
            "evolved_allies_this_turn": state.evolved_allies_this_turn,
            "evolved_allies_this_match": state.evolved_allies_this_match,
            "attacked_with_follower_this_turn": state.attacked_with_follower_this_turn,
            "attacked_with_follower_last_turn": False,
            "card_cost": state.last_played_card_cost,
            "last_played_mode": state.last_played_mode,
            "selected_card_type": state.last_played_card_type,
            "card_type": state.last_played_card_type,
            "tribe": state.last_played_tribes,
        }
        if state_name == "card_present":
            wanted = condition.get("value")
            values["card_present"] = any(c.card_id == wanted for c in state.hand) or any(f.card_id == wanted for f in state.my_board)
        elif state_name == "ally_amulet_count":
            values["ally_amulet_count"] = sum(1 for f in state.my_board if f.card_id and f.atk == 0)
        elif state_name == "ally_artifact_count":
            values["ally_artifact_count"] = sum(1 for c in state.hand if any(str(t).casefold() == "artifact" for t in c.tribes))
        elif state_name == "played_base_cost_set":
            values["played_base_cost_set"] = tuple(sorted(set(int(item) for item in getattr(state, "played_base_costs", ()))))
        if state_name not in values:
            return None
        left, right, cmp = values[state_name], condition.get("value"), condition.get("cmp")
        if state_name == "tribe" and cmp in ("eq", "contains_all"):
            wanted_values = right if isinstance(right, (list, tuple, set)) else [right]
            left_values = {str(item).casefold() for item in (left if isinstance(left, (list, tuple, set)) else [left])}
            return all(str(item).casefold() in left_values for item in wanted_values)
        if state_name == "card_type" and isinstance(left, int) and isinstance(right, str):
            left = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(left, left)
        comparisons = {"eq": lambda: left == right, "ne": lambda: left != right, "gte": lambda: left >= right, "gt": lambda: left > right, "lte": lambda: left <= right, "lt": lambda: left < right, "contains_all": lambda: all(item in left for item in right)}
        try:
            return comparisons[cmp]() if cmp in comparisons else None
        except TypeError:
            return None

    def _progressive_effect(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None, *, choice: Any = None) -> tuple[LethalState, set[str], list[str]]:
        """Resolve one step of an entity-owned progressive sequence.

        A progressive sequence is not a loop: each trigger consumes exactly
        one step and the next step is stored on the source entity.  This is
        what lets two copies of a Countdown amulet advance independently.
        """
        steps = effect.get("steps") if isinstance(effect.get("steps"), (list, tuple)) else ()
        unsupported: set[str] = set()
        warnings: list[str] = []
        if not steps:
            unsupported.add("progressive_sequence")
            return state, unsupported, warnings
        source_side = None
        source_index = None
        for side, board in (("ally", state.my_board), ("enemy", state.enemy_board)):
            source_index = next((i for i, item in enumerate(board) if item.unique_id == source_uid), None)
            if source_index is not None:
                source_side = side
                break
        if source_side is None or source_index is None:
            unsupported.add("progressive_source")
            return state, unsupported, warnings
        source = (state.my_board if source_side == "ally" else state.enemy_board)[source_index]
        step_index = max(0, int(getattr(source, "progressive_sequence_index", 0)))
        if step_index >= len(steps):
            # Once the sequence is exhausted, later triggers are a legal no-op.
            return state, unsupported, warnings
        step = steps[step_index]
        nested = step.get("effects", ()) if isinstance(step, Mapping) else ()
        current = state.clone()
        board = current.my_board if source_side == "ally" else current.enemy_board
        board[source_index] = replace(source, progressive_sequence_index=step_index + 1)
        current, nested_ops, nested_warns = self._effects(current, nested, source_uid, target_uid, choice=choice)
        unsupported.update(nested_ops)
        warnings.extend(nested_warns)
        return current, unsupported, warnings

    def _progressive_effect_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None, *, choice: Any = None) -> list[StochasticBranch]:
        steps = effect.get("steps") if isinstance(effect.get("steps"), (list, tuple)) else ()
        if not steps:
            return [StochasticBranch(1.0, state, ("progressive_sequence",))]
        source_side = None
        source_index = None
        for side, board in (("ally", state.my_board), ("enemy", state.enemy_board)):
            source_index = next((i for i, item in enumerate(board) if item.unique_id == source_uid), None)
            if source_index is not None:
                source_side = side
                break
        if source_side is None or source_index is None:
            return [StochasticBranch(1.0, state, ("progressive_source",))]
        source = (state.my_board if source_side == "ally" else state.enemy_board)[source_index]
        step_index = max(0, int(getattr(source, "progressive_sequence_index", 0)))
        if step_index >= len(steps):
            return [StochasticBranch(1.0, state)]
        step = steps[step_index]
        nested = step.get("effects", ()) if isinstance(step, Mapping) else ()
        current = state.clone()
        board = current.my_board if source_side == "ally" else current.enemy_board
        board[source_index] = replace(source, progressive_sequence_index=step_index + 1)
        return self._effects_branches(current, nested, source_uid, target_uid, choice=choice)

    @staticmethod
    def _runtime_previous_modifier(base_effect: Mapping[str, Any], modifier: Mapping[str, Any]) -> dict[str, Any] | None:
        """Apply a raw ``modify_previous_effect`` relation at runtime.

        The compiler resolves ordinary ``instead`` text ahead of time.  This
        fallback keeps manually authored rules executable when the relation is
        adjacent to its base effect (and, importantly, does not apply both the
        original and replacement operation).
        """
        result = dict(base_effect)
        field = modifier.get("field")
        value = modifier.get("value")
        if value is None:
            return None
        if field in ("amount", "count"):
            result[str(field)] = value
            return result
        if field in ("selection_count", "target_count"):
            target = dict(result.get("target", {})) if isinstance(result.get("target"), Mapping) else None
            if target is None:
                return None
            target["count"] = value
            result["target"] = target
            return result
        if field == "target":
            if not isinstance(value, Mapping) or "scope" not in value:
                return None
            result["target"] = dict(value)
            return result
        if field == "repeat_count":
            if result.get("op") == "repeat":
                result["count"] = value
            else:
                result = {"op": "repeat", "count": value, "effects": [result]}
            return result
        return None

    def _invoke_effect(
        self,
        state: LethalState,
        effect: Mapping[str, Any],
        source_uid: int,
        target_uid: Any = None,
        *,
        choice: Any = None,
        _single: bool = False,
    ) -> InterpreterResult:
        """Invoke one follower from hand/deck without spending PP.

        Invocation is a summon event, not a normal play: no Play Count/PP is
        consumed.  A card-specific ``on_invoke`` listener is preferred; legacy
        catalog text that places the sentence under Fanfare falls back to that
        trigger, which matches the current generated catalog.
        """
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # ``count`` is useful for manually-authored rules such as “invoke two
        # copies”.  Keep the one-copy implementation below as the atomic unit
        # so every copy gets a fresh UID and its own Fanfare/summon pipeline.
        # The public deterministic API deliberately does not guess a random
        # target; callers that need a distribution use ``_invoke_effect_branches``.
        if not _single:
            count = self._resolve_value(state, effect.get("count", 1), source_uid)
            if count is None:
                return InterpreterResult(state, unsupported_ops=("invoke_count",))
            count = max(0, int(count))
            if count == 0:
                return InterpreterResult(state)
            working = state
            all_unsupported: set[str] = set()
            all_warnings: list[str] = []
            one = dict(effect)
            one["count"] = 1
            for _ in range(count):
                result = self._invoke_effect(working, one, source_uid, target_uid, choice=choice, _single=True)
                working = result.state
                all_unsupported.update(result.unsupported_ops)
                all_warnings.extend(result.warnings)
                # A failed/unknown invocation is not a reason to invent later
                # copies.  Continue only when the atomic operation actually
                # created a follower or consumed a hand/deck source.
                if any(str(item).startswith("invoke_") for item in result.unsupported_ops):
                    # ``result.state is state`` is not a reliable no-op test:
                    # invoke may clone a state before discovering an unknown
                    # pool.  Stop the copy loop whenever the atomic operation
                    # reported an invocation gap, otherwise a hand/deck
                    # shortage would be reported once per requested copy.
                    break
            return InterpreterResult(working, tuple(sorted(all_unsupported)), tuple(all_warnings))
        target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {"scope": "self"}
        selector = effect.get("resource_selector") if isinstance(effect.get("resource_selector"), Mapping) else {}
        from_zone = str(effect.get("from_zone") or selector.get("zone") or "").casefold()
        if from_zone in ("your_hand", "ally_hand"):
            from_zone = "hand"
        if from_zone in ("your_deck", "ally_deck"):
            from_zone = "deck"
        target_filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        selector_filters = selector.get("filters") if isinstance(selector.get("filters"), Mapping) else {}
        filters = dict(selector_filters)
        filters.update(target_filters)
        target_scope = str(target.get("scope") or "").casefold()
        target_selection = str(target.get("selection") or "chosen").casefold()
        source_hand_entity = next((card for card in current.hand if card.unique_id == source_uid), None)
        if not from_zone and source_hand_entity is not None and target_scope in ("self", "trigger_source"):
            # Hand listeners (On Draw/On Spellboost/temporary hand
            # abilities) can invoke their own card.  This is distinct from a
            # board follower's “Invoke this card” listener, which defaults to
            # a deck lookup below.
            from_zone = "hand"
        card_id = effect.get("card_id", effect.get("source_card_id"))
        try:
            card_id = int(card_id) if card_id is not None else None
        except (TypeError, ValueError):
            card_id = None
        # Resolve an explicitly selected hand entity before falling back to the
        # board source.  Without this ordering an effect on a board follower
        # that says “invoke a card in your hand” would incorrectly invoke the
        # source follower's own card from the deck.
        requested_uids: set[int] = set()
        if isinstance(target_uid, Mapping):
            requested_uids = {int(key) for key, value in target_uid.items() if value}
        elif isinstance(target_uid, (list, tuple, set)):
            requested_uids = {int(value) for value in target_uid}
        elif target_uid is not None:
            try:
                requested_uids = {int(target_uid)}
            except (TypeError, ValueError):
                requested_uids = set()

        def hand_matches(card: LethalHandCard) -> bool:
            if card_id is not None and card.card_id != card_id:
                return False
            wanted_type = filters.get("card_type")
            if wanted_type:
                values = wanted_type if isinstance(wanted_type, (list, tuple, set)) else [wanted_type]
                actual = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(card.type, card.type)
                if actual not in values and not (actual == "countdown_amulet" and "amulet" in values):
                    return False
            wanted_tribe = filters.get("tribe")
            if wanted_tribe:
                values = wanted_tribe if isinstance(wanted_tribe, (list, tuple, set)) else [wanted_tribe]
                if not any(str(value).casefold() in {str(item).casefold() for item in card.tribes} for value in values):
                    return False
            return True

        hand_index: int | None = None
        hand_target_requested = from_zone == "hand" or target_scope in ("hand", "ally_hand") or str(filters.get("zone", "")).casefold() in ("hand", "ally_hand")
        if requested_uids:
            hand_index = next((i for i, card in enumerate(current.hand) if card.unique_id in requested_uids and hand_matches(card)), None)
            if hand_index is not None:
                from_zone = "hand"
                card_id = current.hand[hand_index].card_id
        if hand_index is None and hand_target_requested:
            if source_uid and target_scope in ("self", "trigger_source"):
                hand_index = next((i for i, card in enumerate(current.hand) if card.unique_id == source_uid and hand_matches(card)), None)
            if hand_index is None:
                hand_index = next((i for i, card in enumerate(current.hand) if hand_matches(card)), None)
            if hand_index is not None:
                from_zone = "hand"
                card_id = current.hand[hand_index].card_id
            elif from_zone == "hand" or target_scope == "hand":
                unsupported.add("invoke_hand_target")
                return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

        board_source = next((item for item in current.my_board if item.unique_id == source_uid), None)
        if card_id is None and hand_index is not None:
            card_id = current.hand[hand_index].card_id
        if card_id is None and board_source is not None:
            card_id = board_source.card_id
        if card_id is None and filters.get("card_id") is not None:
            try:
                card_id = int(filters["card_id"])
            except (TypeError, ValueError):
                card_id = None
        if card_id is None:
            unsupported.add("invoke_card")
            return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))
        if target_selection == "random":
            unsupported.add("random_invoke")
        # If no zone was supplied, an explicit hand target selects hand;
        # otherwise a card-id/source invocation is a deck lookup.  Keeping the
        # default explicit also lets a board listener invoke a known card id
        # instead of silently treating the source follower as already invoked.
        if not from_zone:
            from_zone = "hand" if hand_index is not None else "deck"
        # A source already on the field has already been invoked.  This guard
        # prevents a delayed listener from recursively summoning itself.
        if board_source is not None and target_scope in ("self", "trigger_source") and from_zone not in ("deck", "hand"):
            return InterpreterResult(current)
        from_hand = hand_index is not None and from_zone != "deck"
        if len(current.my_board) >= 5:
            warnings.append("ally field is full; invoke skipped")
            return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))
        if from_hand:
            current = current.clone()
            current.hand.pop(hand_index)
        else:
            known_count = int(current.deck_distribution.get(card_id, 0) or 0)
            if known_count <= 0:
                if current.deck_replacement and current.total_deck_count > 0:
                    unsupported.add("invoke_replaced_deck")
                elif current.total_deck_count > 0:
                    unsupported.add("invoke_unknown_pool")
                else:
                    warnings.append(f"card {card_id} is not available to invoke")
                return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))
            current = current.clone()
            current.deck_distribution[card_id] = max(0, known_count - 1)
            current.total_deck_count = max(0, int(current.total_deck_count) - 1)
        uid = self._next_instance_uid(current)
        follower = self._follower_for_card(card_id, uid)
        if follower is None:
            unsupported.add(f"invoke_card:{card_id}")
            return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))
        current.my_board.append(follower)
        current.last_created_uid = uid
        rule = self.rules.get(card_id, {})
        has_invoke_ability = any(
            isinstance(ability, Mapping) and ability.get("trigger") == "on_invoke"
            for mode in rule.get("modes", ()) if isinstance(mode, Mapping)
            for ability in mode.get("abilities", ()) if isinstance(mode.get("abilities"), (list, tuple))
        )
        if has_invoke_ability:
            triggered = self._resolve_abilities(current, rule, "normal", "on_invoke", source_uid=uid, target_uid=target_uid, choice=choice)
        else:
            triggered = self._resolve_abilities(current, rule, "normal", "on_fanfare", source_uid=uid, target_uid=target_uid, choice=choice)
        current = triggered.state
        unsupported.update(triggered.unsupported_ops)
        warnings.extend(triggered.warnings)
        summoned = self._resolve_abilities(current, rule, "normal", "on_summon", source_uid=uid, target_uid=target_uid, choice=choice)
        current = summoned.state
        unsupported.update(summoned.unsupported_ops)
        warnings.extend(summoned.warnings)
        if any(item.unique_id == uid for item in current.my_board):
            board_event = self._resolve_board_trigger(current, "on_ally_follower_summon")
            current = board_event.state
            unsupported.update(board_event.unsupported_ops)
            warnings.extend(board_event.warnings)
            filtered = self._resolve_board_trigger(current, "on_ally_follower_summon", event_uid=uid)
            current = filtered.state
            unsupported.update(filtered.unsupported_ops)
            warnings.extend(filtered.warnings)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{card_id}")
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _invoke_card_branches(
        self,
        state: LethalState,
        effect: Mapping[str, Any],
        source_uid: int,
        target_uid: Any = None,
        *,
        choice: Any = None,
    ) -> list[StochasticBranch]:
        """Invoke one *known* card and preserve random Fanfare outcomes.

        ``_invoke_effect`` is the deterministic primitive used by direct
        callers.  The branch API must not route an invoked card's Fanfare
        through that primitive, because a Fanfare may itself contain random
        damage, draws, or deck selectors.  This helper performs the same
        hand/deck removal and summon boundary, then dispatches all nested
        abilities with ``_resolve_abilities_branches``.
        """
        target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {"scope": "self"}
        selector = effect.get("resource_selector") if isinstance(effect.get("resource_selector"), Mapping) else {}
        from_zone = str(effect.get("from_zone") or selector.get("zone") or "").casefold()
        if from_zone in ("your_hand", "ally_hand"):
            from_zone = "hand"
        if from_zone in ("your_deck", "ally_deck"):
            from_zone = "deck"
        if not from_zone:
            from_zone = "hand" if any(card.unique_id == source_uid for card in state.hand) else "deck"
        card_id = effect.get("card_id", effect.get("source_card_id"))
        try:
            card_id = int(card_id) if card_id is not None else None
        except (TypeError, ValueError):
            card_id = None
        if card_id is None and str(target.get("scope") or "").casefold() in ("self", "trigger_source"):
            source_entity = next((item for item in state.my_board if item.unique_id == source_uid), None)
            if source_entity is None:
                source_entity = next((item for item in state.enemy_board if item.unique_id == source_uid), None)
            if source_entity is not None:
                card_id = source_entity.card_id
        if card_id is None:
            return [StochasticBranch(1.0, state, ("invoke_card",))]

        unsupported: set[str] = set()
        warnings: list[str] = []
        current = state
        hand_index: int | None = None
        if from_zone == "hand":
            requested: set[int] = set()
            if isinstance(target_uid, Mapping):
                requested = {int(key) for key, value in target_uid.items() if value}
            elif isinstance(target_uid, (list, tuple, set)):
                requested = {int(value) for value in target_uid}
            elif target_uid is not None:
                try:
                    requested = {int(target_uid)}
                except (TypeError, ValueError):
                    requested = set()
            filters = {}
            if isinstance(selector.get("filters"), Mapping):
                filters.update(selector["filters"])
            if isinstance(target.get("filters"), Mapping):
                filters.update(target["filters"])
            candidates = [
                (index, card)
                for index, card in enumerate(current.hand)
                if (card_id is None or card.card_id == card_id)
                and (not requested or card.unique_id in requested)
                and self._hand_card_matches_filters(card, filters)
            ]
            if not candidates:
                return [StochasticBranch(1.0, state, ("invoke_hand_target",))]
            hand_index, selected_card = candidates[0]
            card_id = selected_card.card_id
        elif current.deck_replacement:
            return [StochasticBranch(1.0, state, ("invoke_replaced_deck",))]
        elif int(current.deck_distribution.get(card_id, 0) or 0) <= 0:
            if int(current.total_deck_count) > 0:
                return [StochasticBranch(1.0, state, ("invoke_unknown_pool",))]
            return [StochasticBranch(1.0, state, warnings=(f"card {card_id} is not available to invoke",))]

        if len(current.my_board) >= 5:
            return [StochasticBranch(1.0, state, warnings=("ally field is full; invoke skipped",))]
        current = current.clone()
        if hand_index is not None:
            current.hand.pop(hand_index)
        else:
            current.deck_distribution[card_id] = max(0, int(current.deck_distribution.get(card_id, 0) or 0) - 1)
            current.total_deck_count = max(0, int(current.total_deck_count) - 1)
        uid = self._next_instance_uid(current)
        follower = self._follower_for_card(card_id, uid)
        if follower is None:
            return [StochasticBranch(1.0, current, (f"invoke_card:{card_id}",))]
        before_uids = {item.unique_id for item in current.my_board}
        current.my_board.append(follower)
        current.last_created_uid = uid
        rule = self.rules.get(card_id, {})
        has_invoke_ability = any(
            isinstance(ability, Mapping) and ability.get("trigger") == "on_invoke"
            for mode in rule.get("modes", ()) if isinstance(mode, Mapping)
            for ability in mode.get("abilities", ()) if isinstance(mode.get("abilities"), (list, tuple))
        )
        # A hand entity's target UID was consumed to select the source card;
        # it must not leak into the invoked card's self-targeting Fanfare.
        ability_target_uid = None if hand_index is not None else target_uid
        trigger = "on_invoke" if has_invoke_ability else "on_fanfare"
        branches = self._resolve_abilities_branches(
            current,
            rule,
            "normal",
            trigger,
            source_uid=uid,
            target_uid=ability_target_uid,
            choice=choice,
        )
        output: list[StochasticBranch] = []
        for branch in branches:
            summoned = self._resolve_abilities_branches(
                branch.state,
                rule,
                "normal",
                "on_summon",
                source_uid=uid,
                target_uid=ability_target_uid,
                choice=choice,
                probability=branch.probability,
                inherited_unsupported=branch.unsupported_ops,
                inherited_warnings=branch.warnings,
            )
            for summoned_branch in summoned:
                if any(item.unique_id == uid for item in summoned_branch.state.my_board):
                    output.extend(
                        self._chain_board_trigger_branches(
                            [summoned_branch],
                            "on_ally_follower_summon",
                            entered_before=before_uids,
                        )
                    )
                else:
                    output.append(summoned_branch)
        # Include the rule's static keyword contract just as ``play_branches``
        # does.  The invoked follower is still a normal entity and an unknown
        # static keyword must never be treated as a confirmed lethal line.
        if any(_normalize_keyword(keyword) not in _IMPLEMENTED_KEYWORDS for keyword in rule.get("static_keywords", ()) if isinstance(rule, Mapping)):
            output = [
                StochasticBranch(
                    item.probability,
                    item.state,
                    tuple(sorted(set(item.unsupported_ops) | {f"static_keyword:{keyword}" for keyword in rule.get("static_keywords", ()) if _normalize_keyword(keyword) not in _IMPLEMENTED_KEYWORDS})),
                    item.warnings,
                )
                for item in output
            ]
        if rule.get("support") in ("partial", "unsupported"):
            output = [
                StochasticBranch(item.probability, item.state, tuple(sorted(set(item.unsupported_ops) | {f"{rule.get('support')}_rule:{card_id}"})), item.warnings)
                for item in output
            ]
        return self._merge_stochastic(output or [StochasticBranch(1.0, current, tuple(sorted(unsupported)), tuple(warnings))])

    def _invoke_effect_branches(
        self,
        state: LethalState,
        effect: Mapping[str, Any],
        source_uid: int,
        target_uid: Any = None,
        *,
        choice: Any = None,
    ) -> list[StochasticBranch]:
        """Expand random/selector based Invoke effects.

        ``invoke`` is often printed as “invoke this card”, which is
        deterministic once the card is known.  A few rules instead invoke a
        random card from a hand/deck selector.  The latter must use the same
        multiplicity-weighted public-pool contract as Draw/Summon; any hidden
        or replacement-deck mass remains an explicit incomplete branch.
        """
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        if count is None:
            return [StochasticBranch(1.0, state, ("invoke_count",))]
        count = max(0, int(count))
        if count == 0:
            return [StochasticBranch(1.0, state)]
        one = dict(effect)
        one["count"] = 1
        current = [StochasticBranch(1.0, state)]
        for _ in range(count):
            output: list[StochasticBranch] = []
            for branch in current:
                target = one.get("target") if isinstance(one.get("target"), Mapping) else {}
                selector = one.get("resource_selector") if isinstance(one.get("resource_selector"), Mapping) else {}
                target_filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
                selector_filters = selector.get("filters") if isinstance(selector.get("filters"), Mapping) else {}
                filters = dict(selector_filters)
                filters.update(target_filters)
                from_zone = str(one.get("from_zone") or selector.get("zone") or "").casefold()
                if from_zone in ("your_hand", "ally_hand"):
                    from_zone = "hand"
                if from_zone in ("your_deck", "ally_deck"):
                    from_zone = "deck"
                if not from_zone:
                    if target.get("scope") in ("hand", "ally_hand") or str(filters.get("zone", "")).casefold() in ("hand", "ally_hand"):
                        from_zone = "hand"
                    elif str(target.get("scope") or "").casefold() in ("self", "trigger_source") and any(card.unique_id == source_uid for card in branch.state.hand):
                        from_zone = "hand"
                    else:
                        from_zone = "deck"
                selection = str(target.get("selection") or selector.get("selection") or "chosen").casefold()
                card_id = one.get("card_id", one.get("source_card_id"))
                try:
                    card_id = int(card_id) if card_id is not None else None
                except (TypeError, ValueError):
                    card_id = None
                # Printed “Invoke this card” abilities carry no explicit
                # card_id in the generated DSL.  Resolve the source entity
                # before treating the operation as a random deck selector.
                if card_id is None and str(target.get("scope") or "").casefold() in ("self", "trigger_source"):
                    source_entity = next((item for item in branch.state.my_board if item.unique_id == source_uid), None)
                    if source_entity is None:
                        source_entity = next((item for item in branch.state.enemy_board if item.unique_id == source_uid), None)
                    if source_entity is not None:
                        card_id = source_entity.card_id

                # Hand invocations select an actual entity.  A chosen hand
                # target is passed through the deterministic primitive so its
                # source UID and exact removal semantics stay centralized.
                if from_zone == "hand":
                    requested: set[int] = set()
                    if isinstance(target_uid, Mapping):
                        requested = {int(key) for key, value in target_uid.items() if value}
                    elif isinstance(target_uid, (list, tuple, set)):
                        requested = {int(value) for value in target_uid}
                    elif target_uid is not None:
                        try:
                            requested = {int(target_uid)}
                        except (TypeError, ValueError):
                            requested = set()
                    candidates = [
                        card for card in branch.state.hand
                        if (card_id is None or card.card_id == card_id)
                        and (not requested or card.unique_id in requested)
                        and self._hand_card_matches_filters(card, filters)
                    ]
                    if not candidates:
                        if not branch.state.hand and selection in ("all", "each"):
                            output.append(branch)
                        else:
                            output.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"invoke_hand_target"})), branch.warnings))
                        continue
                    if selection == "random" and not requested:
                        candidates = list(candidates)
                        weight = 1.0 / len(candidates)
                        for card in candidates:
                            bound = dict(one)
                            bound["from_zone"] = "hand"
                            bound["card_id"] = card.card_id
                            bound["target"] = {"scope": "hand", "selection": "chosen"}
                            for result in self._invoke_card_branches(branch.state, bound, source_uid, card.unique_id, choice=choice):
                                output.append(StochasticBranch(branch.probability * weight * result.probability, result.state, tuple(sorted(set(branch.unsupported_ops) | set(result.unsupported_ops))), tuple(branch.warnings) + tuple(result.warnings)))
                    else:
                        card = candidates[0]
                        bound = dict(one)
                        bound["from_zone"] = "hand"
                        bound["card_id"] = card.card_id
                        bound["target"] = {"scope": "hand", "selection": "chosen"}
                        for result in self._invoke_card_branches(branch.state, bound, source_uid, card.unique_id, choice=choice):
                            output.append(StochasticBranch(branch.probability * result.probability, result.state, tuple(sorted(set(branch.unsupported_ops) | set(result.unsupported_ops))), tuple(branch.warnings) + tuple(result.warnings)))
                    continue

                if branch.state.deck_replacement:
                    replaced = branch.state.clone()
                    replaced.total_deck_count = max(0, replaced.total_deck_count - 1)
                    output.append(StochasticBranch(
                        branch.probability,
                        replaced,
                        tuple(sorted(set(branch.unsupported_ops) | {"invoke_replaced_deck"})),
                        branch.warnings,
                    ))
                    continue

                # An explicit card id (including the inferred source id for
                # “Invoke this card”) is not a random draw from the whole
                # deck.  If a known copy exists, invoke it with probability
                # one; otherwise retain one explicit hidden/unavailable
                # branch instead of assigning probability to unrelated cards.
                if card_id is not None:
                    known_count = int(branch.state.deck_distribution.get(card_id, 0) or 0)
                    if known_count > 0:
                        bound = dict(one)
                        bound["from_zone"] = "deck"
                        bound["card_id"] = card_id
                        bound["target"] = {"scope": "self"}
                        for result in self._invoke_card_branches(branch.state, bound, source_uid, choice=choice):
                            output.append(StochasticBranch(
                                branch.probability * result.probability,
                                result.state,
                                tuple(sorted(set(branch.unsupported_ops) | set(result.unsupported_ops))),
                                tuple(branch.warnings) + tuple(result.warnings),
                            ))
                    elif int(branch.state.total_deck_count) > 0:
                        unknown = branch.state.clone()
                        unknown.total_deck_count = max(0, unknown.total_deck_count - 1)
                        gap = "invoke_replaced_deck" if branch.state.deck_replacement else "invoke_unknown_pool"
                        output.append(StochasticBranch(
                            branch.probability,
                            unknown,
                            tuple(sorted(set(branch.unsupported_ops) | {gap})),
                            branch.warnings,
                        ))
                    else:
                        output.append(StochasticBranch(branch.probability, branch.state, branch.unsupported_ops, tuple(branch.warnings) + (f"card {card_id} is not available to invoke",)))
                    continue

                selector_for_deck: dict[str, Any] = {"filters": filters}
                selector_for_deck["side"] = selector.get("side", filters.get("side"))
                if card_id is not None:
                    known = [(card_id, int(branch.state.deck_distribution.get(card_id, 0) or 0))]
                    known = [(cid, amount) for cid, amount in known if amount > 0]
                else:
                    known = [
                        (int(cid), int(amount))
                        for cid, amount in branch.state.deck_distribution.items()
                        if int(amount) > 0 and self._card_matches_selector(int(cid), selector_for_deck)
                    ]
                visible_total = sum(max(0, int(amount)) for amount in branch.state.deck_distribution.values())
                selector_side = selector_for_deck.get("side")
                if selector_side not in (None, "ally", "your"):
                    known = []
                    hidden_total = max(1, int(branch.state.total_deck_count))
                else:
                    hidden_total = max(0, int(branch.state.total_deck_count) - visible_total)
                known_total = sum(amount for _, amount in known)
                total = known_total + hidden_total
                if not total:
                    # A known empty deck is a legal no-op; an effect with a
                    # concrete card id that is not in the known pool is still
                    # reported as an unavailable invocation.
                    if card_id is not None and int(branch.state.total_deck_count) > 0:
                        output.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"invoke_unknown_pool"})), branch.warnings))
                    else:
                        output.append(branch)
                    continue
                for candidate_id, amount in known:
                    bound = dict(one)
                    bound["from_zone"] = "deck"
                    bound["card_id"] = candidate_id
                    bound["target"] = {"scope": "self"}
                    for result in self._invoke_card_branches(branch.state, bound, source_uid, choice=choice):
                        output.append(StochasticBranch(
                            branch.probability * amount / total * result.probability,
                            result.state,
                            tuple(sorted(set(branch.unsupported_ops) | set(result.unsupported_ops))),
                            tuple(branch.warnings) + tuple(result.warnings),
                        ))
                if hidden_total:
                    unknown = branch.state.clone()
                    unknown.total_deck_count = max(0, unknown.total_deck_count - 1)
                    output.append(StochasticBranch(
                        branch.probability * hidden_total / total,
                        unknown,
                        tuple(sorted(set(branch.unsupported_ops) | {"invoke_unknown_pool"})),
                        branch.warnings,
                    ))
            current = self._merge_stochastic(output)
        return current

    @staticmethod
    def _hand_card_matches_filters(card: LethalHandCard, filters: Mapping[str, Any]) -> bool:
        wanted_id = filters.get("card_id")
        if wanted_id is not None:
            try:
                if card.card_id != int(wanted_id):
                    return False
            except (TypeError, ValueError):
                return False
        wanted_type = filters.get("card_type")
        if wanted_type:
            values = wanted_type if isinstance(wanted_type, (list, tuple, set)) else [wanted_type]
            actual = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(card.type, card.type)
            if actual not in values and not (actual == "countdown_amulet" and "amulet" in values):
                return False
        max_cost = filters.get("max_cost", filters.get("max_base_cost"))
        if isinstance(max_cost, (int, float)) and int(card.cost) > int(max_cost):
            return False
        wanted_tribe = filters.get("tribe")
        if wanted_tribe:
            values = wanted_tribe if isinstance(wanted_tribe, (list, tuple, set)) else [wanted_tribe]
            if not any(str(value).casefold() in {str(item).casefold() for item in card.tribes} for value in values):
                return False
        return True

    def _resolve_deck_invocations(self, state: LethalState, trigger: str) -> InterpreterResult:
        """Resolve known Invoke listeners on cards currently in the deck."""
        if trigger not in ("on_turn_start", "on_turn_end"):
            return InterpreterResult(state)
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Iterate over a snapshot: invoking one copy mutates the distribution,
        # while a Fanfare may add cards back to the deck/hand.
        for card_id, count in list(current.deck_distribution.items()):
            if int(count) <= 0:
                continue
            rule = self.rules.get(int(card_id), {})
            abilities = [
                ability for mode in rule.get("modes", ()) if isinstance(mode, Mapping)
                for ability in mode.get("abilities", ()) if isinstance(mode.get("abilities"), (list, tuple))
                if isinstance(ability, Mapping) and ability.get("trigger") == trigger and self._contains_op(ability.get("effects", ()), "invoke")
            ]
            if not abilities:
                continue
            # Each physical copy has its own Invoke listener.  Stop at the
            # board cap; the helper reports the resulting warning explicitly.
            for _ in range(max(0, int(count))):
                if len(current.my_board) >= 5:
                    warnings.append("ally field is full; invoke skipped")
                    break
                condition_failed = False
                for ability in abilities:
                    condition = ability.get("condition")
                    verdict = self._condition_met(current, condition) if condition is not None else True
                    if verdict is None:
                        unsupported.add("conditional")
                        condition_failed = True
                        break
                    if not verdict:
                        condition_failed = True
                        break
                if condition_failed:
                    break
                result = self._invoke_effect(current, {"op": "invoke", "card_id": int(card_id), "from_zone": "deck", "target": {"scope": "self"}}, 0)
                current = result.state
                unsupported.update(result.unsupported_ops)
                warnings.extend(result.warnings)
                if int(current.deck_distribution.get(int(card_id), 0) or 0) <= 0:
                    break
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _effects(self, state: LethalState, effects: Any, source_uid: int, target_uid: Any = None, *, choice: Any = None) -> tuple[LethalState, set[str], list[str]]:
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        previous_effect: Mapping[str, Any] | None = None
        previous_state: LethalState | None = None
        for effect in effects if isinstance(effects, (list, tuple)) else ():
            if not isinstance(effect, Mapping):
                continue
            # A failed textual payment gates the rest of the containing
            # sequence. ``play`` preflights card costs, while this guard also
            # covers delayed/nested abilities resolved outside card play.
            if any(str(item).startswith("insufficient_resource:") for item in unsupported):
                break
            op = effect.get("op")
            if op == "modify_previous_effect":
                if previous_effect is None or previous_state is None:
                    unsupported.add("modify_previous_effect_context")
                    continue
                modified = self._runtime_previous_modifier(previous_effect, effect)
                if modified is None:
                    unsupported.add(f"modify_previous_effect:{effect.get('field')}")
                    continue
                # Roll back the immediately preceding operation and execute
                # its replacement once.  This is the runtime equivalent of
                # compiler-side ``instead`` resolution.
                current = previous_state.clone()
                current, nested_ops, nested_warns = self._effects(current, [modified], source_uid, target_uid, choice=choice)
                unsupported.update(nested_ops)
                warnings.extend(nested_warns)
                previous_effect = modified
                continue
            previous_state = current.clone()
            previous_effect = dict(effect)
            amount = effect.get("amount", 0)
            if isinstance(amount, str):
                resolved = self._resolve_value(current, amount, source_uid)
                if resolved is None:
                    unsupported.add("variable_amount")
                else:
                    amount = resolved
            amount = amount if isinstance(amount, (int, float)) else 0
            target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
            scope = target.get("scope")
            selection = target.get("selection")
            filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
            # A few generated rules retain a board-like scope while carrying
            # ``filters.zone=hand`` (for example, "give all followers in your
            # hand +X/+X").  Keep hand and field resolution disjoint: sending
            # such a target through ``_target_indexes`` would mutate every
            # matching board follower as well as the intended hand cards.
            source_is_hand = any(card.unique_id == source_uid for card in current.hand)
            target_is_hand = (
                scope in ("hand", "ally_hand", "enemy_hand")
                or str(filters.get("zone", "")).casefold() in ("hand", "ally_hand")
                or (source_is_hand and scope in ("self", "trigger_source"))
            )
            if selection == "random":
                unsupported.add("random_target")
            if op == "damage" and scope == "enemy_leader":
                current = current.clone()
                current.enemy_hp -= max(0, int(amount) + int(current.enemy_damage_taken_modifier))
            elif op == "damage" and scope == "any" and selection not in ("all", "each"):
                # ``any`` targets with ``card_type: [follower, leader]`` may
                # bind to the enemy leader.  Leaders are not board indexes,
                # so resolve the explicit leader UID/marker before the field
                # target resolver rather than treating it as a missing
                # follower target.
                leader_uid = getattr(current, "enemy_leader_uid", None)
                leader_markers = {"leader", "enemy_leader", "enemy_leader_uid"}
                if leader_uid is not None:
                    leader_markers.update({leader_uid, str(leader_uid).casefold()})
                target_is_leader = target_uid in leader_markers or (
                    target_uid is not None and str(target_uid).casefold() in leader_markers
                )
                if target_is_leader and self._matches_target_filters(None, "enemy", filters, leader=True):
                    current = current.clone()
                    current.enemy_hp -= max(0, int(amount) + int(current.enemy_damage_taken_modifier))
                else:
                    indexes = self._target_indexes(current, target, target_uid, source_uid)
                    if not indexes:
                        warnings.append("damage target not found")
                        continue
                    current = current.clone()
                    for side, index in sorted(indexes, key=lambda item: (item[0], -item[1])):
                        board = current.my_board if side == "ally" else current.enemy_board
                        if not (0 <= index < len(board)):
                            continue
                        follower = board[index]
                        remaining_hp = follower.hp - int(amount)
                        if remaining_hp > 0:
                            board[index] = replace(follower, hp=remaining_hp)
                        else:
                            removed = board.pop(index)
                            if side == "ally":
                                current.cemetery += 1
                                self._record_destroyed(current, removed)
                                if self._is_amulet(removed):
                                    faith_result = self._resolve_faith_trigger(current, "on_ally_amulet_destroy")
                                    current = faith_result.state
                                    unsupported.update(faith_result.unsupported_ops)
                                    warnings.extend(faith_result.warnings)
                                last_words = self._resolve_last_words(current, removed)
                                current = last_words.state
                                unsupported.update(last_words.unsupported_ops)
                                warnings.extend(last_words.warnings)
                            else:
                                current.last_destroyed_snapshot = removed
            elif op == "heal" and scope == "ally_leader":
                current = current.clone()
                if current.ally_hp or current.ally_max_hp:
                    cap = current.ally_max_hp or current.ally_hp + int(amount)
                    current.ally_hp = min(cap, current.ally_hp + int(amount))
                else:
                    unsupported.add("heal_target")
            elif op == "heal" and scope in ("ally_follower", "any") and selection in ("all", "each"):
                # “Restore X defense to all allies” includes the leader and
                # every allied follower.  Follower max defense is not tracked
                # separately in the compact model, so their current HP is the
                # conservative mutable cap, matching existing buff semantics.
                current = current.clone()
                filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
                side_filter = filters.get("side")
                if scope == "ally_follower" or side_filter in (None, "ally"):
                    for i, follower in enumerate(current.my_board):
                        if self._matches_follower_filters(follower, "ally", filters):
                            current.my_board[i] = replace(follower, hp=follower.hp + int(amount))
                if scope == "any" and side_filter in (None, "ally") and not filters.get("card_type"):
                    if current.ally_hp or current.ally_max_hp:
                        cap = current.ally_max_hp or current.ally_hp + int(amount)
                        current.ally_hp = min(cap, current.ally_hp + int(amount))
            elif op == "damage" and scope == "enemy_follower":
                current = current.clone()
                if isinstance(target_uid, Mapping):
                    allocations = target_uid
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        damage = int(allocations.get(follower.unique_id, 0))
                        if damage <= 0:
                            continue
                        if selection not in ("all", "each") and self._source_is_ally(current, source_uid) and _has_keyword(follower, "ambush"):
                            warnings.append(f"{follower.name} is hidden by Ambush")
                            continue
                        remaining_hp = follower.hp - damage
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            removed = current.enemy_board.pop(i)
                            current.last_destroyed_snapshot = removed
                elif isinstance(target_uid, (list, tuple, set)):
                    selected_ids = {int(value) for value in target_uid}
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        if follower.unique_id not in selected_ids:
                            continue
                        if selection not in ("all", "each") and self._source_is_ally(current, source_uid) and _has_keyword(follower, "ambush"):
                            warnings.append(f"{follower.name} is hidden by Ambush")
                            continue
                        remaining_hp = follower.hp - int(amount)
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            removed = current.enemy_board.pop(i)
                            current.last_destroyed_snapshot = removed
                elif selection == "all" and target.get("allocation") in ("split", "ordered_split"):
                    remaining = max(0, int(amount))
                    to_remove: list[int] = []
                    for i, follower in enumerate(current.enemy_board):
                        if remaining <= 0:
                            break
                        assigned = min(remaining, max(0, follower.hp))
                        remaining -= assigned
                        remaining_hp = follower.hp - assigned
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            to_remove.append(i)
                    for i in reversed(to_remove):
                        removed = current.enemy_board.pop(i)
                        current.last_destroyed_snapshot = removed
                elif selection == "all":
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        remaining_hp = follower.hp - int(amount)
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            removed = current.enemy_board.pop(i)
                            current.last_destroyed_snapshot = removed
                elif target_uid is None:
                    warnings.append("damage enemy_follower requires target_uid")
                else:
                    for i, follower in enumerate(current.enemy_board):
                        if follower.unique_id == target_uid:
                            if selection not in ("all", "each") and self._source_is_ally(current, source_uid) and _has_keyword(follower, "ambush"):
                                warnings.append(f"{follower.name} is hidden by Ambush")
                                break
                            remaining_hp = follower.hp - int(amount)
                            if remaining_hp > 0:
                                current.enemy_board[i] = replace(follower, hp=remaining_hp)
                            else:
                                removed = current.enemy_board.pop(i)
                                current.last_destroyed_snapshot = removed
                            break
            elif op == "damage" and scope in ("ally_follower", "any") and selection not in ("all", "each"):
                indexes = self._target_indexes(current, target, target_uid, source_uid)
                if not indexes:
                    warnings.append("damage target not found")
                    continue
                current = current.clone()
                for side, index in sorted(indexes, key=lambda item: (item[0], -item[1])):
                    board = current.my_board if side == "ally" else current.enemy_board
                    if not (0 <= index < len(board)):
                        continue
                    follower = board[index]
                    remaining_hp = follower.hp - int(amount)
                    if remaining_hp > 0:
                        board[index] = replace(follower, hp=remaining_hp)
                    else:
                        removed = board.pop(index)
                        if side == "ally":
                            current.cemetery += 1
                            self._record_destroyed(current, removed)
                            if self._is_amulet(removed):
                                faith_result = self._resolve_faith_trigger(current, "on_ally_amulet_destroy")
                                current = faith_result.state
                                unsupported.update(faith_result.unsupported_ops)
                                warnings.extend(faith_result.warnings)
                            last_words = self._resolve_last_words(current, removed)
                            current = last_words.state
                            unsupported.update(last_words.unsupported_ops)
                            warnings.extend(last_words.warnings)
                        else:
                            current.last_destroyed_snapshot = removed
            elif op == "damage" and scope == "any" and selection in ("all", "each"):
                # ``all enemies`` is represented as a unified target in v2;
                # apply the same amount to every matching enemy permanent and
                # (when requested) the enemy leader.
                current = current.clone()
                filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
                card_types = filters.get("card_type")
                card_types = card_types if isinstance(card_types, (list, tuple)) else ([card_types] if card_types else [])
                include_leader = not card_types or "leader" in card_types
                include_followers = not card_types or "follower" in card_types or "field_card" in card_types
                if include_followers and target.get("allocation") in ("split", "ordered_split"):
                    remaining = max(0, int(amount))
                    to_remove: list[int] = []
                    for i, follower in enumerate(current.enemy_board):
                        if remaining <= 0:
                            break
                        assigned = min(remaining, max(0, follower.hp))
                        remaining -= assigned
                        remaining_hp = follower.hp - assigned
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            to_remove.append(i)
                    for i in reversed(to_remove):
                        removed = current.enemy_board.pop(i)
                        current.last_destroyed_snapshot = removed
                    # Some effects (notably Crest) split damage across all
                    # enemy permanents *and* the leader.  Apply only the
                    # unspent remainder to the leader; applying the original
                    # amount here would duplicate damage after followers.
                    if include_leader and remaining > 0:
                        current.enemy_hp -= max(0, remaining + int(current.enemy_damage_taken_modifier))
                elif include_followers:
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        remaining_hp = follower.hp - int(amount)
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            removed = current.enemy_board.pop(i)
                            current.last_destroyed_snapshot = removed
                if include_leader and target.get("allocation") not in ("split", "ordered_split"):
                    current.enemy_hp -= max(0, int(amount) + int(current.enemy_damage_taken_modifier))
            elif op == "recover_pp":
                current = current.clone()
                current.pp = min(current.max_pp, current.pp + int(amount))
            elif op in ("modify_cost", "set_cost"):
                indexes = self._hand_indexes(current, target, target_uid, source_uid)
                if not indexes:
                    # A cost effect can also address a just-created entity
                    # before it enters the hand.  Do not treat a missing
                    # chosen target as “all cards”; that would create false
                    # lethal lines.
                    warnings.append(f"{op} target not found")
                    continue
                current = current.clone()
                operation = "set" if op == "set_cost" else effect.get("operation", "delta")
                for index in indexes:
                    card = current.hand[index]
                    old_cost = int(card.cost)
                    if operation == "set":
                        new_cost = int(amount)
                    elif operation == "halve":
                        new_cost = old_cost // 2
                    elif operation == "double":
                        new_cost = old_cost * 2
                    else:
                        new_cost = old_cost + int(amount)
                    current.hand[index] = replace(card, cost=max(0, new_cost))
                if effect.get("duration") not in (None, "permanent", ""):
                    unsupported.add("temporary_cost")
            elif op == "draw":
                # Card draws are intrinsically random unless a caller uses
                # ``_draw_branches`` with a known deck distribution.  The
                # deterministic interpreter therefore reports the gap and
                # leaves the state untouched.
                unsupported.add("draw")
            elif op == "discard":
                indexes = self._hand_indexes(current, target, target_uid, source_uid)
                if not indexes:
                    if selection in ("all", "each") and current.hand:
                        indexes = list(range(len(current.hand)))
                    else:
                        warnings.append("discard target not found")
                        continue
                current = current.clone()
                for index in sorted(set(indexes), reverse=True):
                    if 0 <= index < len(current.hand):
                        current.hand.pop(index)
                        current.cemetery += 1
            elif op == "modify_counter":
                delta = int(effect.get("delta", amount) or 0)
                current = current.clone()
                field = str(effect.get("field", "countdown")).casefold()
                # Spellboost/printed X counters can live on a hand entity;
                # countdowns normally live on a board permanent.  Resolve by
                # source UID and preserve the entity identity in either zone.
                if field in ("variable_x", "x", "spellboost", "spell_boost_count"):
                    changed = False
                    for i, card in enumerate(current.hand):
                        if card.unique_id != source_uid:
                            continue
                        if field in ("spellboost", "spell_boost_count"):
                            current.hand[i] = replace(card, spell_boost_count=max(0, int(card.spell_boost_count) + delta), has_spell_boost=True)
                        else:
                            current.hand[i] = replace(card, variable_x=max(0, int(card.variable_x) + delta))
                        changed = True
                        break
                    if not changed:
                        for i, follower in enumerate(current.my_board):
                            if follower.unique_id != source_uid:
                                continue
                            if field in ("spellboost", "spell_boost_count"):
                                current.my_board[i] = replace(follower, spell_boost_count=max(0, int(follower.spell_boost_count) + delta), has_spell_boost=True)
                            else:
                                current.my_board[i] = replace(follower, variable_x=max(0, int(follower.variable_x) + delta))
                            changed = True
                            break
                    if not changed:
                        unsupported.add("modify_counter_state")
                    continue
                # Countdown counters are stored on the unified board follower
                # model.  ``self`` is the common Engage case, while global
                # listeners (for example Barbaros advancing every allied
                # pirate flag) may target several matching permanents.
                if scope in ("self", None):
                    target_indexes = [
                        ("ally", index)
                        for index, follower in enumerate(current.my_board)
                        if follower.unique_id == source_uid and follower.countdown is not None
                    ]
                else:
                    target_indexes = [
                        (side, index)
                        for side, index in self._target_indexes(current, target, target_uid, source_uid)
                        if side == "ally"
                    ]
                    if not target_indexes:
                        unsupported.add("modify_counter_target")
                        continue
                # Capture UIDs before mutations shift board indexes and run
                # countdown-expiry destruction through the normal Last Words
                # pipeline when a counter reaches zero.
                target_uids = [current.my_board[index].unique_id for _side, index in target_indexes if 0 <= index < len(current.my_board)]
                changed = False
                for target_uid_value in target_uids:
                    index = next((i for i, follower in enumerate(current.my_board) if follower.unique_id == target_uid_value), None)
                    if index is None:
                        continue
                    follower = current.my_board[index]
                    if follower.countdown is None:
                        continue
                    next_countdown = max(0, follower.countdown - delta)
                    current = current.clone()
                    current.my_board[index] = replace(follower, countdown=next_countdown)
                    changed = True
                    if next_countdown == 0:
                        current = self._destroy_ally_uid(current, target_uid_value, unsupported, warnings)
                if not changed:
                    unsupported.add("modify_counter_state")
            elif op == "modify_resource":
                resource = effect.get("resource")
                delta = int(amount)
                if resource == "faith" and effect.get("field") in ("mode_limit", "mode_limit_bonus"):
                    current = current.clone()
                    source_card_id = effect.get("source_card_id")
                    matches = [
                        item for item in current.faith_instances
                        if source_card_id is None or item.get("source_card_id") == source_card_id
                    ]
                    if not matches:
                        unsupported.add("faith_mode_limit_target")
                        continue
                    for item in matches:
                        item["mode_limit_bonus"] = int(item.get("mode_limit_bonus", 0) or 0) + delta
                    continue
                available = {
                    "cemetery": current.cemetery,
                    "necromancy": current.cemetery,
                    "faith": current.faith,
                    "earth_sigil": current.earth_sigil,
                    "earth_rite": current.earth_sigil,
                    "ep": current.ep,
                    "sep": current.sep,
                    "extra_pp": current.extra_pp,
                    "rally": current.rally,
                    "play_count": current.play_count,
                    "pp": current.pp,
                    "max_pp": current.max_pp,
                    "skybound_art": current.skybound_art,
                    "super_skybound_art": current.super_skybound_art,
                }.get(resource)
                if delta < 0 and available is not None and available < -delta:
                    # A negative ``modify_resource`` is a textual payment.
                    # Stop the containing sequence atomically when the
                    # resource is short instead of clamping to zero and
                    # allowing later effects to create a false lethal line.
                    unsupported.add(f"insufficient_resource:{resource}")
                    warnings.append(f"insufficient {resource} (need {-delta}, have {available})")
                    break
                current = current.clone()
                if resource == "faith":
                    current.faith = max(0, current.faith + delta)
                    source_card_id = effect.get("source_card_id")
                    matched = [item for item in current.faith_instances if source_card_id is None or item.get("source_card_id") == source_card_id]
                    if matched:
                        for item in matched:
                            item["value"] = max(0, int(item.get("value", 0) or 0) + delta)
                        self._sync_faith_aggregate(current)
                elif resource == "cemetery":
                    current.cemetery = max(0, current.cemetery + delta)
                elif resource in ("earth_sigil", "earth_rite"):
                    current.earth_sigil = max(0, current.earth_sigil + delta)
                elif resource == "ep":
                    current.ep = max(0, current.ep + delta)
                elif resource == "sep":
                    current.sep = max(0, current.sep + delta)
                elif resource == "rally":
                    current.rally = max(0, current.rally + delta)
                elif resource == "play_count":
                    current.play_count = max(0, current.play_count + delta)
                elif resource == "pp":
                    current.pp = max(0, min(current.max_pp, current.pp + delta))
                elif resource == "max_pp":
                    current.max_pp = max(0, current.max_pp + delta)
                    current.pp = min(current.max_pp, current.pp)
                elif resource == "extra_pp":
                    current.extra_pp = max(0, current.extra_pp + int(amount))
                elif resource == "skybound_art":
                    current.skybound_art = max(0, current.skybound_art + int(amount))
                elif resource == "super_skybound_art":
                    current.super_skybound_art = max(0, current.super_skybound_art + int(amount))
                else:
                    unsupported.add(f"resource:{resource}")
            elif op == "consume_resource":
                resource = effect.get("resource")
                amount_i = max(0, int(amount))
                available = {
                    "cemetery": current.cemetery,
                    "necromancy": current.cemetery,
                    "faith": current.faith,
                    "earth_sigil": current.earth_sigil,
                    "earth_rite": current.earth_sigil,
                    "ep": current.ep,
                    "sep": current.sep,
                    "extra_pp": current.extra_pp,
                }.get(resource)
                if available is None:
                    unsupported.add(f"resource:{resource}")
                elif available < amount_i:
                    # Consumption is atomic.  A failed resource payment must
                    # not partially mutate a branch or accidentally create a
                    # false lethal line.
                    warnings.append(f"insufficient {resource} (need {amount_i}, have {available})")
                    unsupported.add(f"insufficient_resource:{resource}")
                    # A resource payment is a gate for the containing effect;
                    # do not continue to later damage/buff nodes on a failed
                    # branch.
                    break
                else:
                    current = current.clone()
                    if resource in ("cemetery", "necromancy"):
                        current.cemetery -= amount_i
                    elif resource == "faith":
                        original_amount = amount_i
                        current.faith -= original_amount
                        for item in current.faith_instances:
                            if item.get("value", 0) > 0:
                                used = min(int(item.get("value", 0)), amount_i)
                                item["value"] = int(item.get("value", 0)) - used
                                amount_i -= used
                                if amount_i == 0:
                                    break
                        self._sync_faith_aggregate(current)
                    elif resource in ("earth_sigil", "earth_rite"):
                        current.earth_sigil -= amount_i
                    elif resource == "ep":
                        current.ep -= amount_i
                    elif resource == "sep":
                        current.sep -= amount_i
                    elif resource == "extra_pp":
                        current.extra_pp -= amount_i
            elif op == "reanimate":
                cost = self._resolve_value(current, effect.get("cost", effect.get("max_cost", 0)), source_uid)
                if cost is None:
                    unsupported.add("reanimate_cost")
                    continue
                candidates = self._reanimate_candidates(current, effect, int(cost), source_uid)
                if not candidates:
                    if not current.destroyed_pool_known:
                        unsupported.add("reanimate_unknown_pool")
                    # An explicitly empty cemetery is a legal no-op.
                    continue
                if len(candidates) > 1:
                    # The deterministic API is only a safe fallback for a
                    # singleton pool.  The searcher routes real random pools
                    # through ``play_branches``/``evolve_branches``.
                    unsupported.add("random_reanimate")
                candidate_index, candidate = candidates[0]
                current = current.clone()
                if candidate.base_cost is None:
                    unsupported.add("reanimate_unknown_cost")
                if len(current.my_board) >= 5:
                    warnings.append("ally field is full; reanimate skipped")
                    continue
                # Reanimate samples an entry from the destroyed pool but does
                # not consume that pool entry.  The tracker models the pool
                # as the public set of cards that have entered the cemetery;
                # only an explicit banish/disappear operation removes a
                # permanent from play.  Keeping the entry also preserves
                # duplicate-card multiplicity for later Reanimate effects.
                new_uid = self._next_instance_uid(current)
                follower = self._follower_for_card(candidate.card_id, new_uid)
                if follower is None:
                    unsupported.add(f"reanimate_card:{candidate.card_id}")
                    continue
                current.my_board.append(follower)
                current.last_created_uid = follower.unique_id
            elif op == "spellboost":
                result = self._spellboost_effect(current, effect, source_uid, target_uid)
                current = result.state
                unsupported.update(result.unsupported_ops)
                warnings.extend(result.warnings)
            elif op == "invoke":
                result = self._invoke_effect(current, effect, source_uid, target_uid, choice=choice)
                current = result.state
                unsupported.update(result.unsupported_ops)
                warnings.extend(result.warnings)
            elif op == "transform":
                if isinstance(effect.get("resource_selector"), Mapping):
                    unsupported.add("transform_unknown")
                    continue
                card_id = effect.get("card_id")
                if not isinstance(card_id, (int, float)):
                    unsupported.add("transform_card")
                    continue
                targets = self._transform_targets(current, target, target_uid, source_uid)
                if not targets:
                    if selection not in ("all", "each"):
                        warnings.append("transform target not found")
                    continue
                current = current.clone()
                for kind, index in sorted(targets, key=lambda item: (item[0], -item[1])):
                    if kind == "hand":
                        if not (0 <= index < len(current.hand)):
                            continue
                        old = current.hand[index]
                        transformed = self._hand_card_for_card(int(card_id), old.unique_id)
                        if transformed is None:
                            unsupported.add(f"transform_card:{int(card_id)}")
                            continue
                        current.hand[index] = transformed
                    else:
                        board = current.my_board if kind == "ally" else current.enemy_board
                        if not (0 <= index < len(board)):
                            continue
                        old = board[index]
                        transformed = self._follower_for_card(int(card_id), old.unique_id)
                        if transformed is None:
                            unsupported.add(f"transform_card:{int(card_id)}")
                            continue
                        # A transform replaces identity/stats but does not
                        # rewind the already-used attack slot.  Preserve the
                        # current attack allowance while discarding the old
                        # evolution/Last Words state.
                        transformed = replace(transformed, attacks_left=max(0, old.attacks_left))
                        board[index] = transformed
            elif op == "replicate_ability":
                # Replication is expanded at ability-dispatch time where the
                # containing rule and source trigger are available.  Seeing
                # a raw marker here means it was nested in an unsupported
                # context; report it rather than silently dropping it.
                unsupported.add("replicate_ability_context")
            elif op == "buff":
                current = current.clone()
                attack = self._resolve_value(current, effect.get("attack", amount), source_uid)
                life = self._resolve_value(current, effect.get("life", 0), source_uid)
                if attack is None or life is None:
                    unsupported.add("variable_amount")
                attack = int(attack or 0)
                life = int(life or 0)
                indexes = [] if target_is_hand else self._target_indexes(current, target, target_uid, source_uid)
                if indexes:
                    for side, i in indexes:
                        board = current.my_board if side == "ally" else current.enemy_board
                        follower = board[i]
                        new_hp = follower.hp + life
                        if new_hp <= 0:
                            board.pop(i)
                        else:
                            board[i] = replace(follower, atk=max(0, follower.atk + attack), hp=new_hp)
                hand_indexes = self._hand_indexes(current, target, target_uid, source_uid) if target_is_hand else []
                if hand_indexes:
                    for i in hand_indexes:
                        card = current.hand[i]
                        current.hand[i] = replace(card, atk=max(0, card.atk + attack), life=max(0, card.life + life))
                elif not indexes and scope not in ("ally_leader", "enemy_leader") and selection not in ("all", "each"):
                    unsupported.add("buff_target")
            elif op == "grant_keyword":
                current = current.clone()
                keyword = _normalize_keyword(effect.get("keyword"))
                if keyword not in _IMPLEMENTED_KEYWORDS:
                    unsupported.add(f"grant_keyword:{keyword}")
                    continue
                indexes = [] if target_is_hand else self._target_indexes(current, target, target_uid, source_uid)
                for side, i in indexes:
                    board = current.my_board if side == "ally" else current.enemy_board
                    follower = board[i]
                    board[i] = replace(
                        follower,
                        has_storm=follower.has_storm or keyword == "storm",
                        has_rush=follower.has_rush or keyword == "rush" or keyword == "storm",
                        is_ward=follower.is_ward or keyword == "ward",
                        has_bane=follower.has_bane or keyword == "bane",
                        has_drain=follower.has_drain or keyword == "drain",
                        has_ambush=follower.has_ambush or keyword == "ambush",
                        can_attack_leader=follower.can_attack_leader or keyword == "storm",
                        can_attack_field=follower.can_attack_field or keyword in ("storm", "rush"),
                        statuses=tuple(sorted(set(follower.statuses) | {keyword})),
                    )
                hand_indexes = self._hand_indexes(current, target, target_uid, source_uid) if target_is_hand else []
                for i in hand_indexes:
                    card = current.hand[i]
                    if keyword == "storm":
                        current.hand[i] = replace(card, static_storm=True, static_rush=True, statuses=tuple(sorted(set(card.statuses) | {keyword})))
                    elif keyword == "rush":
                        current.hand[i] = replace(card, static_rush=True, statuses=tuple(sorted(set(card.statuses) | {keyword})))
                    elif keyword == "bane":
                        current.hand[i] = replace(card, has_bane=True, statuses=tuple(sorted(set(card.statuses) | {keyword})))
                    elif keyword == "drain":
                        current.hand[i] = replace(card, has_drain=True, statuses=tuple(sorted(set(card.statuses) | {keyword})))
                    elif keyword == "ambush":
                        current.hand[i] = replace(card, has_ambush=True, statuses=tuple(sorted(set(card.statuses) | {keyword})))
                    else:
                        current.hand[i] = replace(card, statuses=tuple(sorted(set(card.statuses) | {keyword})))
            elif op == "remove_keyword":
                current = current.clone()
                keyword = _normalize_keyword(effect.get("keyword", effect.get("status")))
                if keyword not in _IMPLEMENTED_KEYWORDS:
                    unsupported.add(f"remove_keyword:{keyword}")
                    continue
                indexes = [] if target_is_hand else self._target_indexes(current, target, target_uid, source_uid)
                for side, i in indexes:
                    board = current.my_board if side == "ally" else current.enemy_board
                    if not (0 <= i < len(board)):
                        continue
                    follower = board[i]
                    follower_statuses = {_normalize_keyword(item) for item in follower.statuses}
                    # Storm grants Rush implicitly.  ``_has_keyword`` cannot
                    # distinguish that projection from an explicitly printed
                    # Rush on legacy snapshots, so prefer the canonical
                    # status set and fall back to a Rush flag only when Storm
                    # is not present.  This makes removing Storm from a
                    # Storm-only follower remove its implicit Rush as well,
                    # while preserving a separately granted Rush.
                    had_storm = bool(follower.has_storm or "storm" in follower_statuses)
                    explicit_rush = "rush" in follower_statuses or (follower.has_rush and not had_storm)
                    has_storm = had_storm and keyword != "storm"
                    has_rush = (explicit_rush or (had_storm and keyword != "storm")) and keyword != "rush"
                    is_ward = bool(follower.is_ward or "ward" in follower_statuses) and keyword != "ward"
                    has_bane = bool(follower.has_bane or "bane" in follower_statuses) and keyword != "bane"
                    has_drain = bool(follower.has_drain or "drain" in follower_statuses) and keyword != "drain"
                    has_ambush = bool(follower.has_ambush or "ambush" in follower_statuses) and keyword != "ambush"
                    # Storm implies the ability to attack followers, so a
                    # removed Rush remains available only while Storm persists.
                    can_attack_leader = follower.can_attack_leader and keyword != "storm"
                    can_attack_field = follower.can_attack_field and keyword not in ("storm", "rush")
                    if has_storm:
                        can_attack_leader = follower.can_attack_leader
                        can_attack_field = True
                    elif has_rush:
                        can_attack_field = True
                    board[i] = replace(
                        follower,
                        has_storm=has_storm,
                        has_rush=has_rush,
                        is_ward=is_ward,
                        has_bane=has_bane,
                        has_drain=has_drain,
                        has_ambush=has_ambush,
                        can_attack_leader=can_attack_leader,
                        can_attack_field=can_attack_field,
                        statuses=_without_keyword(follower.statuses, keyword),
                    )
                hand_indexes = self._hand_indexes(current, target, target_uid, source_uid) if target_is_hand else []
                for i in hand_indexes:
                    card = current.hand[i]
                    card_statuses = {_normalize_keyword(item) for item in card.statuses}
                    # Storm grants Rush implicitly, but a separately printed
                    # or granted Rush must survive removing Storm.  The
                    # legacy booleans cannot distinguish the two on their
                    # own, so prefer the explicit status set and only retain
                    # ``static_rush`` when it was not Storm-derived.
                    had_storm = bool(card.static_storm or "storm" in card_statuses)
                    explicit_rush = "rush" in card_statuses or (card.static_rush and not had_storm)
                    new_static_rush = card.static_rush
                    if keyword == "rush":
                        new_static_rush = False
                    elif keyword == "storm" and not explicit_rush:
                        new_static_rush = False
                    current.hand[i] = replace(
                        card,
                        static_storm=had_storm and keyword != "storm",
                        static_rush=new_static_rush,
                        has_bane=bool(card.has_bane or "bane" in card_statuses) and keyword != "bane",
                        has_drain=bool(card.has_drain or "drain" in card_statuses) and keyword != "drain",
                        has_ambush=bool(card.has_ambush or "ambush" in card_statuses) and keyword != "ambush",
                        statuses=_without_keyword(card.statuses, keyword),
                    )
            elif op == "remove_abilities":
                current = current.clone()
                indexes = self._target_indexes(current, target, target_uid, source_uid)
                if not indexes and scope not in ("self", "trigger_source"):
                    warnings.append("remove_abilities target not found")
                for side, index in indexes:
                    board = current.my_board if side == "ally" else current.enemy_board
                    follower = board[index]
                    board[index] = replace(
                        follower,
                        abilities_removed=True,
                        # Keyword abilities are mirrored by legacy booleans
                        # in ``LethalFollower``.  Clear all of them together
                        # with the status tuple; otherwise a removed Ward
                        # still blocks attacks and a removed Storm still
                        # grants leader attacks.  Evolution's ordinary
                        # follower-to-follower permission is state, not a
                        # printed keyword, so retain it for an evolved body.
                        has_storm=False,
                        has_rush=False,
                        is_ward=False,
                        can_attack_leader=False,
                        can_attack_field=bool(follower.is_evolved),
                        statuses=(),
                        last_words=(),
                        granted_abilities=(),
                    )
            elif op == "modify_damage_taken":
                # The current v2 operation is intentionally leader-facing;
                # keeping the modifier in state makes later damage events
                # observe it without rewriting every card rule.
                if scope == "enemy_leader":
                    current = current.clone()
                    current.enemy_damage_taken_modifier += int(amount)
                    if effect.get("duration") not in (None, "", "permanent"):
                        unsupported.add("temporary_damage_modifier")
                else:
                    unsupported.add("modify_damage_taken_target")
            elif op == "gain_status":
                status = _normalize_keyword(effect.get("status", ""))
                duration = str(effect.get("duration", "permanent")).casefold()
                nested_ability = effect.get("ability")
                if status.startswith("last words") and isinstance(nested_ability, Mapping):
                    current = current.clone()
                    indexes = self._target_indexes(current, target or {"scope": "self"}, target_uid, source_uid)
                    for side, i in indexes:
                        if side != "ally":
                            continue
                        follower = current.my_board[i]
                        current.my_board[i] = replace(
                            follower,
                            statuses=tuple(sorted(set(follower.statuses) | {"last_words"})),
                            last_words=tuple(follower.last_words) + (dict(nested_ability),),
                        )
                    continue
                # Quoted non-keyword abilities (for example “At the end of
                # your opponent's turn, destroy this card” or “Whenever you
                # play an Enhanced card, ...”) are stored on the selected
                # entity and dispatched through the normal board trigger
                # path.  This keeps the status owner-relative and lets exact
                # copies preserve the granted listener.
                if isinstance(nested_ability, Mapping) and nested_ability.get("trigger"):
                    current = current.clone()
                    indexes = self._target_indexes(current, target or {"scope": "self"}, target_uid, source_uid)
                    nested_copy = {
                        key: (list(value) if isinstance(value, (list, tuple)) else value)
                        for key, value in nested_ability.items()
                    }
                    for side, i in indexes:
                        if side != "ally":
                            continue
                        follower = current.my_board[i]
                        current.my_board[i] = replace(
                            follower,
                            statuses=tuple(sorted(set(follower.statuses) | {status or "granted_ability"})),
                            granted_abilities=tuple(follower.granted_abilities) + (nested_copy,),
                        )
                    continue
                # The attack-count sentence is an intrinsic status.  It is
                # represented by the same attack budget used by the static
                # ``Can attack N times per turn`` parser, rather than as an
                # opaque textual marker.
                attacks_match = re.fullmatch(r"can\s+attack\s+(\d+)\s+times\s+per\s+turn\.?", status, re.I)
                if attacks_match:
                    current = current.clone()
                    indexes = self._target_indexes(current, target or {"scope": "self"}, target_uid, source_uid)
                    for side, i in indexes:
                        if side == "ally":
                            follower = current.my_board[i]
                            current.my_board[i] = replace(follower, attacks_left=max(follower.attacks_left, int(attacks_match.group(1))), statuses=tuple(sorted(set(follower.statuses) | {"multi_attack"})))
                    continue
                # Only these immediate keyword-like statuses are represented
                # by LethalFollower. Textual restrictions and temporary
                # durations require a richer status model and must stay
                # incomplete instead of being treated as a no-op.
                if status not in _IMPLEMENTED_KEYWORDS or duration not in ("permanent", ""):
                    unsupported.add("gain_status")
                    continue
                current = current.clone()
                keyword = status
                indexes = self._target_indexes(current, target or {"scope": "self"}, target_uid, source_uid)
                for side, i in indexes:
                    board = current.my_board if side == "ally" else current.enemy_board
                    if not (0 <= i < len(board)):
                        continue
                    follower = board[i]
                    board[i] = replace(
                        follower,
                        has_storm=follower.has_storm or keyword == "storm",
                        has_rush=follower.has_rush or keyword in ("storm", "rush"),
                        is_ward=follower.is_ward or keyword == "ward",
                        has_bane=follower.has_bane or keyword == "bane",
                        has_drain=follower.has_drain or keyword == "drain",
                        has_ambush=follower.has_ambush or keyword == "ambush",
                        can_attack_leader=follower.can_attack_leader or keyword == "storm",
                        can_attack_field=follower.can_attack_field or keyword in ("storm", "rush"),
                        statuses=tuple(sorted(set(follower.statuses) | {keyword})),
                    )
            elif op in ("banish", "destroy") and scope in ("self", "trigger_source", "any", "ally_follower", "enemy_follower"):
                current = current.clone()
                indexes = self._target_indexes(current, target, target_uid, source_uid)
                if not indexes and selection not in ("all", "each"):
                    warnings.append(f"{op} target not found")
                for side in ("ally", "enemy"):
                    positions = sorted((index for item_side, index in indexes if item_side == side), reverse=True)
                    board = current.my_board if side == "ally" else current.enemy_board
                    for index in positions:
                        if 0 <= index < len(board):
                            removed = board.pop(index)
                            if op == "destroy" and side == "ally":
                                current.cemetery += 1
                                self._record_destroyed(current, removed)
                                if self._is_amulet(removed):
                                    faith_result = self._resolve_faith_trigger(current, "on_ally_amulet_destroy")
                                    current = faith_result.state
                                    unsupported.update(faith_result.unsupported_ops)
                                    warnings.extend(faith_result.warnings)
                                last_words = self._resolve_last_words(current, removed)
                                current = last_words.state
                                unsupported.update(last_words.unsupported_ops)
                                warnings.extend(last_words.warnings)
                            elif op == "destroy" and side == "enemy":
                                # Keep the most recently destroyed enemy for
                                # a following “copy that follower” effect.
                                current.last_destroyed_snapshot = removed
            elif op == "return_to_hand":
                current = current.clone()
                indexes = self._target_indexes(current, target, target_uid, source_uid)
                for side, index in sorted(indexes, key=lambda item: (item[0], -item[1])):
                    board = current.my_board if side == "ally" else current.enemy_board
                    if not (0 <= index < len(board)):
                        continue
                    follower = board.pop(index)
                    card = self._hand_card_for_card(follower.card_id, self._next_instance_uid(current))
                    if card is None:
                        card = LethalHandCard(follower.unique_id, follower.card_id, follower.name, 0, 1, follower.atk, follower.hp, tribes=())
                    current.hand.append(card)
            elif op == "return_to_deck":
                current = current.clone()
                indexes = self._target_indexes(current, target, target_uid, source_uid)
                removed = 0
                for side, index in sorted(indexes, key=lambda item: (item[0], -item[1])):
                    board = current.my_board if side == "ally" else current.enemy_board
                    if 0 <= index < len(board):
                        board.pop(index)
                        current.deck_distribution[0] = current.deck_distribution.get(0, 0) + 1
                        current.total_deck_count += 1
                        removed += 1
            elif op == "gain_crest":
                current = current.clone()
                card_id = int(effect.get("card_id", 0) or 0)
                # A crest is an instance, not a boolean.  Repeated gains keep
                # separate identities and countdowns.
                next_uid = self._next_instance_uid(current)
                owner = str(effect.get("player", "ally") or "ally").casefold()
                if owner not in ("ally", "enemy"):
                    unsupported.add(f"gain_crest_player:{owner}")
                    continue
                instance = {"card_id": card_id, "unique_id": next_uid, "owner": owner}
                if effect.get("countdown") is not None:
                    instance["countdown"] = int(effect["countdown"])
                abilities = [dict(item) for item in effect.get("abilities", ()) if isinstance(item, Mapping)] if isinstance(effect.get("abilities"), (list, tuple)) else []
                abilities.extend(self._catalog_crest_abilities(card_id))
                if abilities:
                    instance["abilities"] = abilities
                if owner == "ally":
                    current.crest_instances.append(instance)
                    current.active_crests.append(card_id)
                else:
                    # Opponent Crests are public state as well.  They must not
                    # inflate the ally ``crest_count`` condition, but keeping
                    # their identity/countdown lets a later opponent-turn
                    # boundary resolve or expire them without losing data.
                    current.enemy_crest_instances.append(instance)
                    current.enemy_active_crests.append(card_id)
            elif op == "summon" and "resource_selector" not in effect:
                current = current.clone()
                card_id = effect.get("card_id")
                count_value = self._resolve_value(current, effect.get("count", 1), source_uid)
                if card_id is None or count_value is None:
                    unsupported.add("summon")
                    continue
                for _ in range(max(0, int(count_value))):
                    if len(current.my_board) >= 5:
                        warnings.append("ally field is full; summon skipped")
                        break
                    follower = self._follower_for_card(int(card_id), self._next_instance_uid(current))
                    if follower is None:
                        unsupported.add(f"summon_card:{card_id}")
                        break
                    current.my_board.append(follower)
                    current.last_created_uid = follower.unique_id
            elif op in ("add_to_hand", "add_to_zone") and effect.get("destination", "hand") == "hand":
                current = current.clone()
                card_id = effect.get("card_id")
                count_value = self._resolve_value(current, effect.get("count", 1), source_uid)
                if card_id is None or count_value is None:
                    unsupported.add("add_to_hand")
                    continue
                for _ in range(max(0, int(count_value))):
                    card = self._hand_card_for_card(int(card_id), self._next_instance_uid(current))
                    if card is None:
                        unsupported.add(f"add_to_hand_card:{card_id}")
                        break
                    current.hand.append(card)
                    current.last_created_uid = card.unique_id
            elif op == "add_to_zone":
                destination = str(effect.get("destination", "")).casefold()
                current = current.clone()
                card_id = effect.get("card_id")
                count_value = self._resolve_value(current, effect.get("count", 1), source_uid)
                if card_id is None or count_value is None:
                    unsupported.add("add_to_zone")
                    continue
                for _ in range(max(0, int(count_value))):
                    if destination == "deck":
                        current.deck_distribution[int(card_id)] = current.deck_distribution.get(int(card_id), 0) + 1
                        current.total_deck_count += 1
                        continue
                    if destination == "cemetery":
                        current.cemetery += 1
                        continue
                    if destination == "field":
                        if len(current.my_board) >= 5:
                            warnings.append("ally field is full; add_to_zone skipped")
                            break
                        follower = self._follower_for_card(int(card_id), self._next_instance_uid(current))
                        if follower is None:
                            unsupported.add(f"add_to_zone_card:{card_id}")
                            break
                        current.my_board.append(follower)
                        current.last_created_uid = follower.unique_id
                        continue
                    if destination == "banished":
                        # The compact state has no public banished-card list.
                        # Keep the insertion explicit rather than pretending it
                        # entered the deck or cemetery.
                        unsupported.add("add_to_zone_banished")
                        continue
                    unsupported.add(f"add_to_zone_destination:{destination}")
            elif op == "modify_crest":
                current = current.clone()
                delta = int(effect.get("amount", amount) or 0)
                wanted_card = effect.get("crest_card_id")
                selected = selection in ("all", "each")
                changed = False
                for item in current.crest_instances:
                    if wanted_card is not None and item.get("card_id") != wanted_card:
                        continue
                    if not selected and changed:
                        continue
                    if item.get("countdown") is not None:
                        item["countdown"] = max(0, int(item.get("countdown", 0)) - delta)
                    else:
                        item["variable_x"] = int(item.get("variable_x", 0) or 0) + delta
                    changed = True
                if not current.crest_instances and current.active_crests:
                    # Legacy snapshots do not carry countdown objects.  Keep
                    # the projection usable by adjusting no identity state.
                    pass
            elif op == "destroy_crest":
                current = current.clone()
                wanted_card = effect.get("crest_card_id")
                if isinstance(target.get("filters"), Mapping):
                    wanted_card = wanted_card or target["filters"].get("card_id")
                if selection in ("all", "each"):
                    if wanted_card is None:
                        current.crest_instances = []
                        current.active_crests = []
                    else:
                        current.crest_instances = [item for item in current.crest_instances if item.get("card_id") != wanted_card]
                        current.active_crests = [cid for cid in current.active_crests if cid != wanted_card]
                elif wanted_card is not None:
                    for i, item in enumerate(current.crest_instances):
                        if item.get("card_id") == wanted_card:
                            current.crest_instances.pop(i)
                            break
                    try:
                        current.active_crests.remove(wanted_card)
                    except ValueError:
                        pass
            elif op == "copy":
                current = current.clone()
                source = effect.get("source") if isinstance(effect.get("source"), Mapping) else {}
                source_candidates = self._copy_source_candidates(current, source, target_uid, source_uid)
                if source.get("selection") == "random":
                    unsupported.add("random_copy")
                    continue
                source_entity = source_candidates[0] if source_candidates else None
                if source_entity is None:
                    unsupported.add("copy_source")
                    continue
                count_value = self._resolve_value(current, effect.get("count", 1), source_uid)
                if count_value is None:
                    unsupported.add("variable_amount")
                    continue
                destination = effect.get("destination", "field")
                for _ in range(max(0, int(count_value))):
                    new_uid = self._next_instance_uid(current)
                    if destination == "field":
                        if len(current.my_board) >= 5:
                            warnings.append("ally field is full; copy skipped")
                            break
                        if isinstance(source_entity, LethalFollower):
                            copied = replace(source_entity, unique_id=new_uid)
                        else:
                            copied = self._follower_for_card(source_entity.card_id, new_uid)
                        if copied is None:
                            unsupported.add("copy_source")
                            break
                        current.my_board.append(copied)
                        current.last_created_uid = new_uid
                    elif destination == "hand":
                        card_id = source_entity.card_id
                        copied_card = self._hand_card_for_card(card_id, new_uid)
                        if copied_card is None:
                            unsupported.add("copy_destination_hand")
                            break
                        cost_delta = effect.get("cost_delta", 0)
                        if isinstance(cost_delta, (int, float)):
                            copied_card = replace(copied_card, cost=max(0, copied_card.cost + int(cost_delta)))
                        if isinstance(source_entity, LethalFollower) and effect.get("copy_mode") == "exact" and effect.get("preserve_state"):
                            copied_card = replace(copied_card, atk=source_entity.atk, life=source_entity.hp)
                        current.hand.append(copied_card)
                        current.last_created_uid = new_uid
                    else:
                        unsupported.add(f"copy_destination:{destination}")
            elif op == "set_attacks":
                current = current.clone()
                for i, follower in enumerate(current.my_board):
                    if follower.unique_id == source_uid:
                        current.my_board[i] = replace(follower, attacks_left=int(amount))
            elif op == "set_stat":
                current = current.clone()
                stat = effect.get("stat")
                value = self._resolve_value(current, effect.get("amount", amount), source_uid)
                if value is None:
                    unsupported.add("variable_amount")
                    continue
                indexes = [] if target_is_hand else self._target_indexes(current, target, target_uid, source_uid)
                for side, i in indexes:
                    board = current.my_board if side == "ally" else current.enemy_board
                    follower = board[i]
                    if stat == "attack":
                        board[i] = replace(follower, atk=int(value))
                    elif stat in ("life", "max_life"):
                        board[i] = replace(follower, hp=int(value))
                    else:
                        unsupported.add(f"set_stat:{stat}")
                hand_indexes = self._hand_indexes(current, target, target_uid, source_uid) if target_is_hand else []
                for i in hand_indexes:
                    card = current.hand[i]
                    if stat == "attack":
                        current.hand[i] = replace(card, atk=int(value))
                    elif stat in ("life", "max_life"):
                        current.hand[i] = replace(card, life=int(value))
            elif op == "auto_evolve":
                evolution_kind = effect.get("evolution_kind", "normal")
                target_scope = target.get("scope")
                target_ids = [uid for side, i in self._target_indexes(current, target, target_uid, source_uid) for uid in [(current.my_board if side == "ally" else current.enemy_board)[i].unique_id]]
                if target_scope in ("self", "trigger_source") and not target_ids:
                    target_ids = [source_uid]
                if not target_ids:
                    warnings.append("auto_evolve target not found")
                else:
                    evolved_result = self.auto_evolve(current, target_ids[0], super_evolve=evolution_kind == "super")
                    current = evolved_result.state
                    unsupported.update(evolved_result.unsupported_ops)
                    warnings.extend(evolved_result.warnings)
            elif op == "grant_resource_ability":
                resource = effect.get("resource")
                if resource != "faith":
                    unsupported.add(f"resource:{resource}")
                    continue
                current = current.clone()
                ability = effect.get("ability")
                if not isinstance(ability, Mapping):
                    unsupported.add("grant_resource_ability")
                    continue
                source_card_id = effect.get("source_card_id")
                instances = current.faith_instances
                if source_card_id is not None:
                    matches = [item for item in instances if item.get("source_card_id") == source_card_id]
                else:
                    matches = instances
                if not matches:
                    matches = [{"source_card_id": source_card_id or 0, "value": current.faith, "abilities": []}]
                    instances.extend(matches)
                for item in matches:
                    item.setdefault("abilities", []).append(dict(ability))
            elif op == "repeat":
                count_value = self._resolve_value(current, effect.get("count", 0), source_uid)
                if count_value is None:
                    unsupported.add("variable_amount")
                    continue
                if self._effect_contains_random(effect.get("effects", ())):
                    unsupported.add("random_target")
                    continue
                for _ in range(max(0, int(count_value))):
                    current, nested_ops, nested_warns = self._effects(current, effect.get("effects", ()), source_uid, target_uid, choice=choice)
                    unsupported.update(nested_ops)
                    warnings.extend(nested_warns)
                    if any(str(item).startswith("insufficient_resource:") for item in nested_ops):
                        break
            elif op == "progressive_sequence":
                current, nested_ops, nested_warns = self._progressive_effect(current, effect, source_uid, target_uid, choice=choice)
                unsupported.update(nested_ops)
                warnings.extend(nested_warns)
            elif op == "sequence":
                current, nested_ops, nested_warns = self._effects(current, effect.get("effects", ()), source_uid, target_uid, choice=choice)
                unsupported.update(nested_ops)
                warnings.extend(nested_warns)
            elif op == "conditional":
                verdict = self._condition_met(current, effect.get("condition", {}))
                if verdict is None:
                    unsupported.add("conditional")
                else:
                    branch = effect.get("effects", ()) if verdict else effect.get("else_effects", ())
                    current, nested_ops, nested_warns = self._effects(current, branch, source_uid, target_uid, choice=choice)
                    unsupported.update(nested_ops)
                    warnings.extend(nested_warns)
            elif op == "mode_choice":
                choices = effect.get("choices", ())
                if not isinstance(choices, (list, tuple)) or not choices:
                    unsupported.add("mode_choice")
                    continue
                # ``selection_count`` allows cards such as “Select 2 Modes”
                # to carry more than one branch.  The engine passes a tuple
                # of labels for those actions; a direct interpreter caller
                # may still pass one index/label for the common one-choice
                # form.
                selected_values = list(choice) if isinstance(choice, (list, tuple)) and not all(isinstance(item, Mapping) for item in choice) else [choice]
                selected_choices: list[Mapping[str, Any]] = []
                for selected_value in selected_values:
                    selected_choice = None
                    if isinstance(selected_value, Mapping):
                        selected_choice = selected_value
                    elif isinstance(selected_value, int) and 0 <= selected_value < len(choices):
                        selected_choice = choices[selected_value]
                    else:
                        selected_choice = next((item for item in choices if isinstance(item, Mapping) and str(item.get("label")) == str(selected_value)), None)
                    if isinstance(selected_choice, Mapping):
                        selected_choices.append(selected_choice)
                if not selected_choices:
                    unsupported.add("mode_choice")
                    continue
                for selected_choice in selected_choices:
                    current, nested_ops, nested_warns = self._effects(current, selected_choice.get("effects", ()), source_uid, target_uid, choice=choice)
                    unsupported.update(nested_ops)
                    warnings.extend(nested_warns)
            elif op == "activate_all_mode_choices":
                # If the compiler emits this as a separate ability, the
                # selected choices are supplied through ``choice`` as a list
                # of labels/indices.  Without that explicit payload the
                # operation remains incomplete rather than guessing.
                if not isinstance(choice, (list, tuple)):
                    unsupported.add("mode_choice")
                    continue
                for selected_choice in choice:
                    if isinstance(selected_choice, Mapping):
                        nested = selected_choice.get("effects", ())
                    else:
                        nested = ()
                    current, nested_ops, nested_warns = self._effects(current, nested, source_uid, target_uid, choice=choice)
                    unsupported.update(nested_ops)
                    warnings.extend(nested_warns)
            elif op == "replace_deck":
                replacement = str(effect.get("replacement", "")).strip()
                if not replacement:
                    unsupported.add("replace_deck")
                    continue
                current = current.clone()
                current.deck_replacement = replacement
                # The replacement template has no publicly known card
                # identities.  Preserve the remaining count, but discard the
                # old identity distribution so future selectors cannot sample
                # cards from the previous deck.
                current.total_deck_count = max(
                    int(current.total_deck_count),
                    sum(max(0, int(value)) for value in current.deck_distribution.values()),
                )
                current.deck_distribution = {}
            else:
                unsupported.add(str(op))
        return current, unsupported, warnings

    @staticmethod
    def _effect_contains_random(effects: Any) -> bool:
        for effect in effects if isinstance(effects, (list, tuple)) else ():
            if not isinstance(effect, Mapping):
                continue
            target = effect.get("target")
            if isinstance(target, Mapping) and target.get("selection") == "random":
                return True
            if effect.get("op") in ("random_choice", "random_target", "reanimate"):
                return True
            if effect.get("op") == "transform" and isinstance(effect.get("resource_selector"), Mapping):
                return True
            if effect.get("op") == "spellboost" and isinstance(effect.get("target"), Mapping) and effect["target"].get("selection") == "random":
                return True
            if effect.get("op") == "copy" and isinstance(effect.get("source"), Mapping) and effect["source"].get("selection") == "random":
                return True
            if EventInterpreter._effect_contains_random(effect.get("effects", ())):
                return True
            if EventInterpreter._effect_contains_random(effect.get("else_effects", ())):
                return True
            if any(EventInterpreter._effect_contains_random(item.get("effects", ())) for item in effect.get("steps", ()) if isinstance(item, Mapping)):
                return True
            if any(EventInterpreter._effect_contains_random(item.get("effects", ())) for item in effect.get("choices", ()) if isinstance(item, Mapping)):
                return True
        return False

    # ------------------------------------------------------------------
    # Step 6: probability-preserving effect execution
    # ------------------------------------------------------------------
    def play_branches(self, state: LethalState, unique_id: int, mode: str = "normal", target_uid: Any = None, choice: Any = None) -> list[StochasticBranch]:
        """Play one card and retain every random outcome.

        The deterministic ``play`` API deliberately reports random selectors
        as incomplete.  The searcher uses this method when it sees a random
        node, so a hit is never chosen once and incorrectly treated as
        certain.  Branches are merged after each effect using ``state_key``.
        """
        index = next((i for i, card in enumerate(state.hand) if card.unique_id == unique_id), None)
        if index is None:
            return [StochasticBranch(1.0, state, warnings=(f"hand card {unique_id} not found",))]
        card = state.hand[index]
        if getattr(state, "legal_modes_known", False):
            allowed_modes = state.legal_modes.get(int(unique_id))
            if allowed_modes is None or mode not in allowed_modes:
                return [StochasticBranch(1.0, state, warnings=(f"Tracker forbids {mode} play for {unique_id}",))]
        play_cost = self.mode_cost(card, mode)
        if play_cost is None:
            return [StochasticBranch(1.0, state, warnings=(f"mode {mode} is not available for {card.name}",))]
        if self._available_pp(state) < play_cost:
            return [StochasticBranch(1.0, state, warnings=(f"insufficient PP for {card.name}",))]
        places_follower = card.type == 1 and mode not in ("accelerate", "crystallize")
        if places_follower and len(state.my_board) >= 5:
            return [StochasticBranch(1.0, state, warnings=("ally field is full",))]
        resource_error = self._resource_preflight(state, self.rules.get(card.card_id, {}), mode, source_uid=card.unique_id, choice=choice)
        if resource_error is not None:
            resource, need, have = resource_error
            return [StochasticBranch(1.0, state, (f"insufficient_resource:{resource}",), (f"insufficient {resource} (need {need}, have {have})",))]
        base = state.clone()
        self._invalidate_snapshot_legality(base)
        base.hand.pop(index)
        self._pay_pp(base, int(play_cost))
        base.play_count += 1
        base.last_played_card_cost = int(play_cost)
        base.last_played_mode = str(mode)
        base.last_played_card_type = card.type
        base.last_played_tribes = tuple(card.tribes)
        base_cost = self._base_card_cost(card)
        base.played_base_costs = tuple(sorted(set(base.played_base_costs) | {base_cost}))
        self._ensure_catalog_resources(base, card.card_id, card.unique_id)
        rule = self.rules.get(card.card_id, {})
        enhanced = mode == "enhance"
        intrinsic_damage = card.enhance_face_damage if enhanced and card.enhance_face_damage else (card.face_damage if mode == "normal" else 0)
        intrinsic_recover = card.enhance_recover_pp if enhanced and card.enhance_recover_pp else (card.recover_pp if mode == "normal" else 0)
        intrinsic_buff = card.enhance_buff_atk if enhanced and card.enhance_buff_atk else (card.buff_atk if mode == "normal" else 0)
        board_uids_before = {f.unique_id for f in base.my_board}
        if places_follower:
            static = self._static_keywords(card.card_id, card)
            default_attacks = rule.get("default_attacks", 1) if isinstance(rule, Mapping) else 1
            try:
                default_attacks = max(1, int(default_attacks))
            except (TypeError, ValueError):
                default_attacks = 1
            storm = card.static_storm or (enhanced and card.enhance_gain_storm) or "storm" in static
            rush = card.static_rush or "rush" in static or storm
            statuses = tuple(sorted(set(str(item) for item in static)))
            base.my_board.append(LethalFollower(
                unique_id=card.unique_id, card_id=card.card_id, name=card.name,
                atk=card.atk + intrinsic_buff, hp=card.life,
                has_storm=storm, has_rush=rush, is_ward="ward" in static,
                can_attack_leader=storm, can_attack_field=rush, attacks_left=default_attacks,
                statuses=statuses,
                base_cost=card.cost,
                spell_boost_count=card.spell_boost_count,
                has_spell_boost=card.has_spell_boost or self._card_has_trigger(card.card_id, "on_spellboost"),
                variable_x=card.variable_x,
                supplement_info=card.supplement_info,
                has_bane="bane" in static,
                has_drain="drain" in static,
                has_ambush="ambush" in static,
                buff=card.buff,
            ))
            base.last_created_uid = card.unique_id
        branches = self._resolve_abilities_branches(base, rule, mode, "on_play", source_uid=card.unique_id, target_uid=target_uid, choice=choice)
        if (
            choice is not None
            and self._mode_has_choice(rule, mode)
            and not (base.super_skybound_art > 0 and self._mode_has_activate_all(rule, mode))
        ):
            mode_branches: list[StochasticBranch] = []
            for branch in branches:
                mode_event = self.select_mode(
                    branch.state,
                    count=self._mode_selection_count(rule, mode, choice),
                )
                mode_branches.append(StochasticBranch(
                    branch.probability,
                    mode_event.state,
                    tuple(sorted(set(branch.unsupported_ops) | set(mode_event.unsupported_ops))),
                    tuple(branch.warnings) + tuple(mode_event.warnings),
                ))
            branches = self._merge_stochastic(mode_branches)
        branches = self._chain_ability_branches(branches, rule, mode, "on_fanfare", source_uid=card.unique_id, target_uid=target_uid, choice=choice)
        if places_follower:
            branches = self._chain_ability_branches(branches, rule, mode, "on_summon", source_uid=card.unique_id, target_uid=target_uid, choice=choice)
            branches = self._chain_board_trigger_branches(
                branches,
                "on_ally_follower_summon",
                entered_before=board_uids_before,
            )
        branches = self._chain_board_trigger_branches(branches, "on_card_play")
        if self._is_spell_card(card):
            branches = self._chain_board_trigger_branches(branches, "on_spellboost")
            branches = self._chain_spellboost_branches(branches, source_uid=card.unique_id)
        if intrinsic_damage or intrinsic_recover:
            updated: list[StochasticBranch] = []
            for branch in branches:
                current = branch.state.clone()
                current.enemy_hp -= max(0, int(intrinsic_damage) + int(current.enemy_damage_taken_modifier))
                current.pp = min(current.max_pp, current.pp + int(intrinsic_recover))
                updated.append(StochasticBranch(branch.probability, current, branch.unsupported_ops, branch.warnings))
            branches = updated
        if self._is_spell_card(card):
            branches = [
                StochasticBranch(
                    branch.probability,
                    replace(branch.state, cemetery=branch.state.cemetery + 1),
                    branch.unsupported_ops,
                    branch.warnings,
                )
                for branch in branches
            ]
        static_keywords = rule.get("static_keywords", ()) if isinstance(rule, Mapping) else ()
        if any(_normalize_keyword(keyword) not in _IMPLEMENTED_KEYWORDS for keyword in static_keywords):
            branches = [StochasticBranch(b.probability, b.state, tuple(sorted(set(b.unsupported_ops) | {f"static_keyword:{k}" for k in static_keywords if _normalize_keyword(k) not in _IMPLEMENTED_KEYWORDS})), b.warnings) for b in branches]
        if rule.get("support") in ("partial", "unsupported"):
            branches = [StochasticBranch(b.probability, b.state, tuple(sorted(set(b.unsupported_ops) | {f"{rule.get('support')}_rule:{card.card_id}"})), b.warnings) for b in branches]
        return self._merge_stochastic(branches)

    def _chain_ability_branches(self, branches: list[StochasticBranch], rule: Mapping[str, Any], mode: str, trigger: str, *, source_uid: int, target_uid: Any = None, choice: Any = None) -> list[StochasticBranch]:
        output: list[StochasticBranch] = []
        for branch in branches:
            output.extend(self._resolve_abilities_branches(branch.state, rule, mode, trigger, source_uid=source_uid, target_uid=target_uid, choice=choice, probability=branch.probability, inherited_unsupported=branch.unsupported_ops, inherited_warnings=branch.warnings))
        return self._merge_stochastic(output)

    def _resolve_abilities_branches(self, state: LethalState, rule: Mapping[str, Any], mode: str, trigger: str, *, source_uid: int, target_uid: Any = None, choice: Any = None, probability: float = 1.0, inherited_unsupported: tuple[str, ...] = (), inherited_warnings: tuple[str, ...] = (), event_uid: int | None = None) -> list[StochasticBranch]:
        modes = rule.get("modes", ()) if isinstance(rule, Mapping) else ()
        selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if selected is None and mode == "normal":
            selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == "normal"), None)
        current = [StochasticBranch(probability, state, tuple(inherited_unsupported), tuple(inherited_warnings))]
        abilities = list(selected.get("abilities", ())) if isinstance(selected, Mapping) else []
        source_follower = next((f for f in state.my_board if f.unique_id == source_uid), None)
        if source_follower is not None and source_follower.abilities_removed:
            return [StochasticBranch(probability, state, tuple(inherited_unsupported), tuple(inherited_warnings))]
        if source_follower is not None:
            abilities.extend(item for item in source_follower.granted_abilities if isinstance(item, Mapping))
        for ability in abilities:
            if not isinstance(ability, Mapping) or ability.get("trigger") != trigger:
                continue
            trigger_filter = ability.get("trigger_filter")
            if event_uid is None:
                if isinstance(trigger_filter, Mapping):
                    continue
            elif not isinstance(trigger_filter, Mapping) or not self._trigger_filter_matches(state, trigger_filter, event_uid):
                continue
            next_branches: list[StochasticBranch] = []
            for branch in current:
                if self._contains_op(ability.get("effects", ()), "mode_choice") and branch.state.super_skybound_art > 0 and self._mode_has_activate_all(rule, mode):
                    next_branches.append(branch)
                    continue
                condition = ability.get("condition")
                if condition is not None:
                    verdict = self._condition_met(branch.state, condition)
                    if verdict is None:
                        next_branches.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"conditional"})), branch.warnings))
                        continue
                    if not verdict:
                        next_branches.append(branch)
                        continue
                effective_choice = self._choice_for_ability(rule, mode, ability, branch.state, choice)
                expanded_effects, replicate_ops = self._expand_replicated_effects(
                    branch.state, rule, mode, ability, source_trigger=trigger
                )
                effect_source_uid = event_uid if event_uid is not None else source_uid
                effects = self._effects_branches(branch.state, expanded_effects, effect_source_uid, target_uid, choice=effective_choice)
                for effect_branch in effects:
                    next_branches.append(StochasticBranch(branch.probability * effect_branch.probability, effect_branch.state, tuple(sorted(set(branch.unsupported_ops) | set(effect_branch.unsupported_ops) | replicate_ops)), tuple(branch.warnings) + tuple(effect_branch.warnings)))
            current = self._merge_stochastic(next_branches)
        return current

    def _chain_board_trigger_branches(
        self,
        branches: list[StochasticBranch],
        trigger: str,
        *,
        entered_before: set[int] | None = None,
    ) -> list[StochasticBranch]:
        """Resolve a board event without replaying listeners on old entities.

        ``on_ally_follower_summon`` is emitted for the followers that entered
        during this play.  The old implementation iterated every board slot
        on every play, so a Golem listener could evolve an already-present
        Golem repeatedly and stochastic branches diverged from deterministic
        execution.  We retain one unfiltered pass for the public event and a
        second, event-specific pass for each newly entered UID.
        """
        result = branches

        def run_sources(
            current: list[StochasticBranch],
            source_slots: list[int],
            *,
            event_uid: int | None = None,
        ) -> list[StochasticBranch]:
            for source_uid in source_slots:
                next_result: list[StochasticBranch] = []
                for branch in current:
                    follower = next((f for f in branch.state.my_board if f.unique_id == source_uid), None)
                    if follower is None or follower.abilities_removed:
                        next_result.append(branch)
                        continue
                    rule = self.rules.get(follower.card_id, {})
                    next_result.extend(
                        self._resolve_abilities_branches(
                            branch.state,
                            rule,
                            "normal",
                            trigger,
                            source_uid=source_uid,
                            event_uid=event_uid,
                            probability=branch.probability,
                            inherited_unsupported=branch.unsupported_ops,
                            inherited_warnings=branch.warnings,
                        )
                    )
                current = self._merge_stochastic(next_result)
            return current

        # The unfiltered pass observes the event once for each board source,
        # matching ``_resolve_board_trigger``.  Filtered abilities are skipped
        # here and only considered in the event-specific pass below.
        source_slots: list[int] = []
        for branch in result:
            for follower in branch.state.my_board:
                if follower.unique_id not in source_slots:
                    source_slots.append(follower.unique_id)
        result = run_sources(result, source_slots)

        if entered_before is not None and trigger == "on_ally_follower_summon":
            entered_slots: list[int] = []
            # Recompute listeners after the unfiltered event: a Fanfare may
            # have summoned an additional listener before the filtered pass.
            listener_slots: list[int] = []
            for branch in result:
                for follower in branch.state.my_board:
                    if follower.unique_id not in listener_slots:
                        listener_slots.append(follower.unique_id)
                    if follower.unique_id not in entered_before and follower.unique_id not in entered_slots:
                        entered_slots.append(follower.unique_id)
            for event_uid in entered_slots:
                result = run_sources(result, listener_slots, event_uid=event_uid)
        return result

    def _chain_spellboost_branches(self, branches: list[StochasticBranch], *, source_uid: int) -> list[StochasticBranch]:
        """Apply the global Spellboost event to every eligible hand card."""
        output: list[StochasticBranch] = []
        for branch in branches:
            result = self._spellboost_effect_branches(
                branch.state,
                {"op": "spellboost", "target": {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}, "count": 1},
                source_uid,
            )
            output.extend(
                StochasticBranch(
                    branch.probability * item.probability,
                    item.state,
                    tuple(sorted(set(branch.unsupported_ops) | set(item.unsupported_ops))),
                    tuple(branch.warnings) + tuple(item.warnings),
                )
                for item in result
            )
        return self._merge_stochastic(output)

    def _effects_branches(self, state: LethalState, effects: Any, source_uid: int, target_uid: Any = None, *, choice: Any = None) -> list[StochasticBranch]:
        # Keep the immediately preceding operation and its pre-state along
        # every branch.  ``modify_previous_effect`` is a runtime fallback for
        # textual ``instead`` relations; when the preceding operation was
        # random, each branch still rolls back to that branch's own pre-state
        # rather than replaying a sibling outcome.
        records: list[tuple[StochasticBranch, Mapping[str, Any] | None, LethalState | None]] = [
            (StochasticBranch(1.0, state), None, None)
        ]

        def merge_records(items: list[tuple[StochasticBranch, Mapping[str, Any] | None, LethalState | None]]) -> list[tuple[StochasticBranch, Mapping[str, Any] | None, LethalState | None]]:
            merged: dict[tuple[Any, ...], tuple[StochasticBranch, Mapping[str, Any] | None, LethalState | None]] = {}
            for branch, previous, previous_state in items:
                if branch.probability <= 0:
                    continue
                key = (
                    branch.state.state_key(),
                    repr(previous) if previous is not None else None,
                    previous_state.state_key() if previous_state is not None else None,
                )
                old = merged.get(key)
                if old is None:
                    merged[key] = (branch, previous, previous_state)
                else:
                    old_branch = old[0]
                    merged[key] = (
                        StochasticBranch(
                            old_branch.probability + branch.probability,
                            old_branch.state,
                            tuple(sorted(set(old_branch.unsupported_ops) | set(branch.unsupported_ops))),
                            old_branch.warnings if len(old_branch.warnings) <= len(branch.warnings) else branch.warnings,
                        ),
                        old[1],
                        old[2],
                    )
            return list(merged.values())

        for effect in effects if isinstance(effects, (list, tuple)) else ():
            if not isinstance(effect, Mapping):
                continue
            next_records: list[tuple[StochasticBranch, Mapping[str, Any] | None, LethalState | None]] = []
            for branch, previous_effect, previous_state in records:
                # Failed resource payments are atomic gates for the containing
                # sequence.  Preserve the blocked branch for reporting but do
                # not let later effects resolve after the failed cost.
                if any(str(item).startswith("insufficient_resource:") for item in branch.unsupported_ops):
                    next_records.append((branch, previous_effect, previous_state))
                    continue
                if effect.get("op") == "modify_previous_effect":
                    if previous_effect is None or previous_state is None:
                        next_records.append((
                            StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"modify_previous_effect_context"})), branch.warnings),
                            previous_effect,
                            previous_state,
                        ))
                        continue
                    modified = self._runtime_previous_modifier(previous_effect, effect)
                    if modified is None:
                        next_records.append((
                            StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {f"modify_previous_effect:{effect.get('field')}"})), branch.warnings),
                            previous_effect,
                            previous_state,
                        ))
                        continue
                    outcomes = self._effects_branches(previous_state.clone(), [modified], source_uid, target_uid, choice=choice)
                    for outcome in outcomes:
                        next_records.append((
                            StochasticBranch(
                                branch.probability * outcome.probability,
                                outcome.state,
                                tuple(sorted(set(branch.unsupported_ops) | set(outcome.unsupported_ops))),
                                tuple(branch.warnings) + tuple(outcome.warnings),
                            ),
                            modified,
                            previous_state,
                        ))
                    continue
                before = branch.state.clone()
                for outcome in self._effect_branches(branch.state, effect, source_uid, target_uid, choice=choice):
                    next_records.append((
                        StochasticBranch(
                            branch.probability * outcome.probability,
                            outcome.state,
                            tuple(sorted(set(branch.unsupported_ops) | set(outcome.unsupported_ops))),
                            tuple(branch.warnings) + tuple(outcome.warnings),
                        ),
                        dict(effect),
                        before,
                    ))
            records = merge_records(next_records)
        return [item[0] for item in records]

    def _reanimate_candidates(self, state: LethalState, effect: Mapping[str, Any], cost: int, source_uid: int) -> list[tuple[int, LethalFollower]]:
        filters = effect.get("filters") if isinstance(effect.get("filters"), Mapping) else {}
        result: list[tuple[int, LethalFollower]] = []
        for index, follower in enumerate(state.destroyed_this_match):
            # Reanimate only selects followers.  Tracker's destroyed pool
            # contains card ids for every destroyed permanent, so filter out
            # amulets using catalog/card metadata before applying the cost
            # ceiling.
            if self._is_amulet(follower):
                continue
            base_cost = follower.base_cost
            if base_cost is None:
                meta = self.catalog.get(follower.card_id, {})
                base_cost = meta.get("cost")
                if base_cost is None and follower.card_id in self.card_db:
                    base_cost = self.card_db[follower.card_id].cost
            try:
                resolved_cost = int(base_cost) if base_cost is not None else None
            except (TypeError, ValueError):
                resolved_cost = None
            if filters and not self._matches_follower_filters(follower, "ally", filters):
                continue
            if resolved_cost is None:
                # Keep the unresolved entry as an explicitly incomplete
                # outcome instead of silently treating it as eligible for
                # Reanimate(N).  Normal Tracker snapshots resolve this from
                # CardCatalog; missing metadata must never create a false
                # lethal line.
                result.append((index, replace(follower, base_cost=None)))
                continue
            if resolved_cost > int(cost):
                continue
            if follower.base_cost != resolved_cost:
                follower = replace(follower, base_cost=resolved_cost)
            result.append((index, follower))
        return result

    def _reanimate_effect_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int) -> list[StochasticBranch]:
        """Resolve Reanimate as a random draw from the destroyed pool.

        Reanimate samples an entry without removing it from the destroyed
        pool.  A later Reanimate may therefore select the same card again;
        duplicate ``destroyed_card_ids`` entries retain their multiplicity and
        probability weight.  This mirrors the tracker's public destroyed
        pool, where a card remains in the cemetery after it is reanimated.
        """
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        if count is None:
            return [StochasticBranch(1.0, state, ("reanimate_count",))]
        current = [StochasticBranch(1.0, state)]
        for _ in range(max(0, int(count))):
            output: list[StochasticBranch] = []
            for branch in current:
                cost = self._resolve_value(branch.state, effect.get("cost", effect.get("max_cost", 0)), source_uid)
                if cost is None:
                    output.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"reanimate_cost"})), branch.warnings))
                    continue
                candidates = self._reanimate_candidates(branch.state, effect, int(cost), source_uid)
                if not candidates:
                    if branch.state.destroyed_pool_known:
                        output.append(branch)
                    else:
                        output.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"reanimate_unknown_pool"})), branch.warnings))
                    continue
                for candidate_index, candidate in candidates:
                    next_state = branch.state.clone()
                    candidate_ops = set(branch.unsupported_ops)
                    if candidate.base_cost is None:
                        candidate_ops.add("reanimate_unknown_cost")
                    if len(next_state.my_board) >= 5:
                        output.append(StochasticBranch(branch.probability / len(candidates), next_state, tuple(sorted(candidate_ops)), tuple(branch.warnings) + ("ally field is full; reanimate skipped",)))
                        continue
                    new_uid = self._next_instance_uid(next_state)
                    follower = self._follower_for_card(candidate.card_id, new_uid)
                    if follower is None:
                        output.append(StochasticBranch(branch.probability / len(candidates), branch.state, tuple(sorted(candidate_ops | {f"reanimate_card:{candidate.card_id}"})), branch.warnings))
                        continue
                    next_state.my_board.append(follower)
                    next_state.last_created_uid = follower.unique_id
                    output.append(StochasticBranch(
                        branch.probability / len(candidates),
                        next_state,
                        tuple(sorted(candidate_ops)),
                        branch.warnings,
                    ))
            current = self._merge_stochastic(output)
        return current

    def _transform_targets(self, state: LethalState, target: Mapping[str, Any], target_uid: Any, source_uid: int) -> list[tuple[str, int]]:
        """Resolve transform targets across hand and field zones."""
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        zone = str(filters.get("zone", "field")).casefold()
        scope = target.get("scope")
        if scope == "hand" or zone in ("hand", "ally_hand"):
            return [("hand", index) for index in self._hand_indexes(state, target, target_uid, source_uid)]
        if zone in ("deck", "graveyard", "destroyed", "destroyed_this_match"):
            return []
        indexes = self._target_indexes(state, target, target_uid, source_uid)
        return indexes

    def _transform_effect_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None, *, choice: Any = None) -> list[StochasticBranch]:
        """Branch random target/source selections for Transform.

        A known public deck distribution produces exact branches.  Any
        residual hidden deck mass is retained as an explicit incomplete
        branch.  The deck itself is not decremented: the card is sampled as a
        printed template, matching the game wording “copy of a random card in
        your deck.”
        """
        target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {"scope": "any"}
        selection = target.get("selection", "chosen")
        if selection == "random":
            candidates = self._transform_targets(state, {**target, "selection": "all"}, None, source_uid)
            if not candidates:
                return [StochasticBranch(1.0, state)]
            output: list[StochasticBranch] = []
            for kind, index in candidates:
                uid = state.hand[index].unique_id if kind == "hand" else (state.my_board if kind == "ally" else state.enemy_board)[index].unique_id
                bound_target = dict(target)
                bound_target["selection"] = "chosen"
                bound_target["scope"] = kind if kind == "hand" else ("ally_follower" if kind == "ally" else "enemy_follower")
                bound = dict(effect)
                bound["target"] = bound_target
                result = self._transform_effect_branches(state, bound, source_uid, uid, choice=choice)
                output.extend(StochasticBranch(item.probability / len(candidates), item.state, item.unsupported_ops, item.warnings) for item in result)
            return self._merge_stochastic(output)

        selector = effect.get("resource_selector") if isinstance(effect.get("resource_selector"), Mapping) else None
        if selector is None:
            # Static transforms are deterministic once their target is bound;
            # dispatch straight to the checked primitive to avoid recursively
            # re-entering this branch helper.
            next_state, unsupported, warnings = self._effects(state, [effect], source_uid, target_uid, choice=choice)
            return [StochasticBranch(1.0, next_state, tuple(sorted(unsupported)), tuple(warnings))]

        targets = self._transform_targets(state, target, target_uid, source_uid)
        if not targets:
            if selection not in ("all", "each"):
                return [StochasticBranch(1.0, state, warnings=("transform target not found",))]
            return [StochasticBranch(1.0, state)]

        def target_uid_for(kind: str, index: int, current: LethalState) -> int | None:
            if kind == "hand":
                return current.hand[index].unique_id if 0 <= index < len(current.hand) else None
            board = current.my_board if kind == "ally" else current.enemy_board
            return board[index].unique_id if 0 <= index < len(board) else None

        current = [StochasticBranch(1.0, state)]
        # Transform each selected entity independently.  Recompute its current
        # index by UID after every prior replacement so board order mutations
        # cannot direct a later sample at the wrong card.
        target_uids = [target_uid_for(kind, index, state) for kind, index in targets]
        target_uids = [uid for uid in target_uids if uid is not None]
        for uid in target_uids:
            next_branches: list[StochasticBranch] = []
            for branch in current:
                selector_side = selector.get("side")
                selector_filters = selector.get("filters") if isinstance(selector.get("filters"), Mapping) else {}
                if selector_side is None:
                    selector_side = selector_filters.get("side")
                if branch.state.deck_replacement:
                    hidden_total = max(1, int(branch.state.total_deck_count))
                    next_branches.append(StochasticBranch(
                        branch.probability,
                        branch.state,
                        tuple(sorted(set(branch.unsupported_ops) | {"transform_replaced_deck"})),
                        branch.warnings,
                    ))
                    current = self._merge_stochastic(next_branches)
                    continue
                known = [
                    (int(card_id), int(number))
                    for card_id, number in branch.state.deck_distribution.items()
                    if selector_side in (None, "ally", "your")
                    and int(number) > 0
                    and self._card_matches_selector(int(card_id), selector)
                ]
                known_total = sum(number for _, number in known)
                visible_total = sum(max(0, int(number)) for number in branch.state.deck_distribution.values())
                if selector_side not in (None, "ally", "your"):
                    # Opponent deck identities are never present in the
                    # ally-only ``deck_distribution``.  Keep one opaque
                    # residual outcome even when the local deck count is
                    # zero, rather than treating the selector as empty.
                    hidden_total = max(1, int(branch.state.total_deck_count))
                else:
                    hidden_total = max(0, int(branch.state.total_deck_count) - visible_total)
                total = known_total + hidden_total
                if not total:
                    next_branches.append(StochasticBranch(branch.probability, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"transform_empty_pool"})), branch.warnings))
                    continue
                for card_id, number in known:
                    bound = dict(effect)
                    bound.pop("resource_selector", None)
                    bound["card_id"] = card_id
                    bound_target = dict(target)
                    bound_target["selection"] = "chosen"
                    bound_target["scope"] = "hand" if any(item.unique_id == uid for item in branch.state.hand) else target.get("scope", "any")
                    bound["target"] = bound_target
                    transformed = self._transform_effect_branches(branch.state, bound, source_uid, uid, choice=choice)
                    for item in transformed:
                        next_branches.append(StochasticBranch(branch.probability * number / total * item.probability, item.state, tuple(sorted(set(branch.unsupported_ops) | set(item.unsupported_ops))), tuple(branch.warnings) + tuple(item.warnings)))
                if hidden_total:
                    next_branches.append(StochasticBranch(branch.probability * hidden_total / total, branch.state, tuple(sorted(set(branch.unsupported_ops) | {"transform_unknown"})), branch.warnings))
            current = self._merge_stochastic(next_branches)
        return current

    def _effect_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None, *, choice: Any = None) -> list[StochasticBranch]:
        op = effect.get("op")
        if op == "auto_evolve":
            target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
            target_scope = target.get("scope")
            target_ids = [
                uid
                for side, index in self._target_indexes(state, target, target_uid, source_uid)
                for uid in [
                    (state.my_board if side == "ally" else state.enemy_board)[index].unique_id
                ]
            ]
            if target_scope in ("self", "trigger_source") and not target_ids:
                target_ids = [source_uid]
            if not target_ids:
                return [StochasticBranch(1.0, state, warnings=("auto_evolve target not found",))]
            return self.auto_evolve_branches(
                state,
                target_ids[0],
                super_evolve=effect.get("evolution_kind", "normal") == "super",
            )
        if op == "reanimate":
            return self._reanimate_effect_branches(state, effect, source_uid)
        if op == "spellboost":
            return self._spellboost_effect_branches(state, effect, source_uid, target_uid)
        if op == "transform":
            return self._transform_effect_branches(state, effect, source_uid, target_uid, choice=choice)
        if op == "progressive_sequence":
            return self._progressive_effect_branches(state, effect, source_uid, target_uid, choice=choice)
        if op == "invoke":
            return self._invoke_effect_branches(state, effect, source_uid, target_uid, choice=choice)
        if op in ("remove_keyword", "modify_previous_effect", "replace_deck"):
            next_state, unsupported, warnings = self._effects(state, [effect], source_uid, target_uid, choice=choice)
            return [StochasticBranch(1.0, next_state, tuple(sorted(unsupported)), tuple(warnings))]
        if op == "conditional":
            verdict = self._condition_met(state, effect.get("condition", {}))
            if verdict is None:
                return [StochasticBranch(1.0, state, ("conditional",))]
            branch = effect.get("effects", ()) if verdict else effect.get("else_effects", ())
            return self._effects_branches(state, branch, source_uid, target_uid, choice=choice)
        if op == "sequence":
            return self._effects_branches(state, effect.get("effects", ()), source_uid, target_uid, choice=choice)
        if op == "repeat":
            count = self._resolve_value(state, effect.get("count", 0), source_uid)
            if count is None:
                return [StochasticBranch(1.0, state, ("variable_amount",))]
            current = [StochasticBranch(1.0, state)]
            for _ in range(max(0, int(count))):
                next_branches: list[StochasticBranch] = []
                for branch in current:
                    if any(str(item).startswith("insufficient_resource:") for item in branch.unsupported_ops):
                        next_branches.append(branch)
                        continue
                    for outcome in self._effects_branches(branch.state, effect.get("effects", ()), source_uid, target_uid, choice=choice):
                        next_branches.append(StochasticBranch(branch.probability * outcome.probability, outcome.state, tuple(sorted(set(branch.unsupported_ops) | set(outcome.unsupported_ops))), tuple(branch.warnings) + tuple(outcome.warnings)))
                current = self._merge_stochastic(next_branches)
                if current and all(any(str(item).startswith("insufficient_resource:") for item in branch.unsupported_ops) for branch in current):
                    break
            return current
        if op == "mode_choice":
            # Mode is a player decision, not a source of probability.  The
            # engine enumerates labels explicitly; a direct stochastic call
            # without a choice must remain incomplete rather than silently
            # inventing a random distribution.
            choices = effect.get("choices", ())
            if not isinstance(choices, (list, tuple)) or not choices or choice is None:
                return [StochasticBranch(1.0, state, ("mode_choice",))]
            selected_values = list(choice) if isinstance(choice, (list, tuple)) and not all(isinstance(item, Mapping) for item in choice) else [choice]
            selected_choices: list[Mapping[str, Any]] = []
            for selected_value in selected_values:
                selected_choice = None
                if isinstance(selected_value, Mapping):
                    selected_choice = selected_value
                elif isinstance(selected_value, int) and 0 <= selected_value < len(choices):
                    selected_choice = choices[selected_value]
                else:
                    selected_choice = next((item for item in choices if isinstance(item, Mapping) and str(item.get("label")) == str(selected_value)), None)
                if isinstance(selected_choice, Mapping):
                    selected_choices.append(selected_choice)
            if not selected_choices:
                return [StochasticBranch(1.0, state, ("mode_choice",))]
            current = [StochasticBranch(1.0, state)]
            for selected_choice in selected_choices:
                next_branches: list[StochasticBranch] = []
                for branch in current:
                    for outcome in self._effects_branches(branch.state, selected_choice.get("effects", ()), source_uid, target_uid, choice=choice):
                        next_branches.append(StochasticBranch(branch.probability * outcome.probability, outcome.state, tuple(sorted(set(branch.unsupported_ops) | set(outcome.unsupported_ops))), tuple(branch.warnings) + tuple(outcome.warnings)))
                current = self._merge_stochastic(next_branches)
            return current
        if op == "activate_all_mode_choices":
            # Super Skybound Art replaces a normal Mode choice with every
            # choice in sequence.  Resolve the nested effects through the
            # branching interpreter (rather than the deterministic helper)
            # so a choice containing draw/random effects keeps its complete
            # probability distribution.  The compiler/runtime passes the
            # concrete choice mappings via ``choice``.
            if not isinstance(choice, (list, tuple)):
                return [StochasticBranch(1.0, state, ("mode_choice",))]
            current = [StochasticBranch(1.0, state)]
            for selected_choice in choice:
                nested = selected_choice.get("effects", ()) if isinstance(selected_choice, Mapping) else ()
                next_branches: list[StochasticBranch] = []
                for branch in current:
                    for outcome in self._effects_branches(branch.state, nested, source_uid, target_uid, choice=choice):
                        next_branches.append(
                            StochasticBranch(
                                branch.probability * outcome.probability,
                                outcome.state,
                                tuple(sorted(set(branch.unsupported_ops) | set(outcome.unsupported_ops))),
                                tuple(branch.warnings) + tuple(outcome.warnings),
                            )
                        )
                current = self._merge_stochastic(next_branches)
            return current
        if op == "random_choice":
            choices = effect.get("choices", ())
            if not isinstance(choices, (list, tuple)) or not choices:
                return [StochasticBranch(1.0, state, ("random_choice",))]
            weights: list[float] = []
            for item in choices:
                if not isinstance(item, Mapping):
                    weights.append(0.0)
                    continue
                raw_weight = item.get("weight", 1.0)
                try:
                    weights.append(max(0.0, float(raw_weight if raw_weight is not None else 0.0)))
                except (TypeError, ValueError):
                    return [StochasticBranch(1.0, state, ("random_choice_weight",))]
            weight_sum = sum(weights)
            total = weight_sum or float(len(choices))
            output: list[StochasticBranch] = []
            for index, branch_choice in enumerate(choices):
                if not isinstance(branch_choice, Mapping):
                    continue
                p = (weights[index] if weight_sum else 1.0) / total
                for branch in self._effects_branches(state, branch_choice.get("effects", ()), source_uid, target_uid, choice=choice):
                    output.append(StochasticBranch(p * branch.probability, branch.state, branch.unsupported_ops, branch.warnings))
            return self._merge_stochastic(output)
        if op == "random_target" or self._target_is_random(effect):
            target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
            count = self._resolve_value(state, target.get("count", 1), source_uid)
            if count is None:
                return [StochasticBranch(1.0, state, ("variable_amount",))]
            # A textual “N random followers” effect is N random activations.
            # Recompute the candidate pool after every activation, allowing a
            # destroyed follower to disappear while a surviving follower may
            # be selected again.  This is also the semantics needed by
            # repeat/random cards such as Cupitan and the FAQ examples.
            current = [StochasticBranch(1.0, state)]
            for _ in range(max(0, int(count))):
                output: list[StochasticBranch] = []
                for branch in current:
                    candidates = self._random_target_candidates(branch.state, target, source_uid)
                    if not candidates:
                        # Random target with an empty pool is a skipped
                        # activation; preserve this branch with probability 1.
                        output.append(branch)
                        continue
                    for kind, uid in candidates:
                        bound = self._bind_target(effect, kind, uid)
                        nested = bound.get("effects") if op == "random_target" else None
                        if nested is not None:
                            nested_effects = [self._bind_target(item, kind, uid) if isinstance(item, Mapping) else item for item in nested]
                            outcomes = self._effects_branches(branch.state, nested_effects, source_uid, uid, choice=choice)
                        else:
                            outcomes = self._effects_branches(branch.state, [bound], source_uid, uid, choice=choice)
                        output.extend(StochasticBranch(branch.probability * outcome.probability / len(candidates), outcome.state, outcome.unsupported_ops, outcome.warnings) for outcome in outcomes)
                current = self._merge_stochastic(output)
            return current
        if op == "draw":
            return self._draw_branches(state, effect, source_uid)
        if op == "summon" and isinstance(effect.get("resource_selector"), Mapping):
            return self._summon_branches(state, effect, source_uid)
        if op == "copy" and isinstance(effect.get("source"), Mapping) and effect["source"].get("selection") == "random":
            return self._copy_branches(state, effect, source_uid, target_uid, choice=choice)
        # All non-random primitives use the same checked implementation as the
        # deterministic interpreter.  It may still report an explicitly
        # planned operation; such a branch is never treated as confirmed.
        next_state, unsupported, warnings = self._effects(state, [effect], source_uid, target_uid, choice=choice)
        return [StochasticBranch(1.0, next_state, tuple(sorted(unsupported)), tuple(warnings))]

    def _copy_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None, *, choice: Any = None) -> list[StochasticBranch]:
        source = effect.get("source") if isinstance(effect.get("source"), Mapping) else {}
        # Ask the source resolver for the complete pool.  It treats a random
        # selector as a set of equally likely entities and never exposes
        # hidden enemy-hand identities.
        pool_source = dict(source)
        pool_source["selection"] = "all"
        candidates = self._copy_source_candidates(state, pool_source, target_uid, source_uid)
        if not candidates:
            return [StochasticBranch(1.0, state, ("random_copy",))]
        output: list[StochasticBranch] = []
        for candidate in candidates:
            bound = dict(effect)
            bound_source = dict(source)
            bound_source["selection"] = "chosen"
            bound["source"] = bound_source
            candidate_uid = candidate.unique_id
            outcomes = self._effects_branches(state, [bound], source_uid, candidate_uid, choice=choice)
            for outcome in outcomes:
                output.append(StochasticBranch(outcome.probability / len(candidates), outcome.state, outcome.unsupported_ops, outcome.warnings))
        return self._merge_stochastic(output)

    @staticmethod
    def _target_is_random(effect: Mapping[str, Any]) -> bool:
        target = effect.get("target")
        return isinstance(target, Mapping) and target.get("selection") == "random"

    def _random_target_candidates(self, state: LethalState, target: Mapping[str, Any], source_uid: int) -> list[tuple[str, int | None]]:
        scope = target.get("scope")
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        output: list[tuple[str, int | None]] = []
        if scope in ("enemy_leader", "any") and self._matches_target_filters(None, "enemy", filters, leader=True):
            output.append(("leader", None))
        if scope in ("enemy_follower", "any"):
            for follower in state.enemy_board:
                # Ambush is untargetable by an enemy ability.  An explicit
                # all-enemy area selector is not a target choice and therefore
                # still includes the hidden follower.
                if target.get("selection") not in ("all", "each") and self._source_is_ally(state, source_uid) and _has_keyword(follower, "ambush"):
                    continue
                if self._matches_target_filters(follower, "enemy", filters) and not ((filters.get("exclude_source") or filters.get("exclude_self")) and follower.unique_id == source_uid):
                    output.append(("enemy_follower", follower.unique_id))
        if scope in ("ally_follower", "any"):
            for follower in state.my_board:
                if self._matches_target_filters(follower, "ally", filters) and not ((filters.get("exclude_source") or filters.get("exclude_self")) and follower.unique_id == source_uid):
                    output.append(("ally_follower", follower.unique_id))
        return output

    def _matches_target_filters(self, follower: LethalFollower | None, side: str, filters: Mapping[str, Any], *, leader: bool = False) -> bool:
        if filters.get("side") and filters.get("side") != side:
            return False
        card_types = filters.get("card_type")
        if leader:
            return not card_types or "leader" in (card_types if isinstance(card_types, (list, tuple)) else [card_types])
        if follower is None:
            return False
        if card_types:
            values = card_types if isinstance(card_types, (list, tuple)) else [card_types]
            actual = self.catalog.get(follower.card_id, {}).get("type")
            if actual is None and follower.card_id in self.card_db:
                actual = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(self.card_db[follower.card_id].type)
            if actual and actual not in values and not (actual == "countdown_amulet" and "amulet" in values):
                return False
        if not self._matches_follower_filters(follower, side, filters):
            return False
        if filters.get("exclude_source") and follower.unique_id == 0:
            return False
        return True

    def _matches_follower_filters(self, follower: LethalFollower, side: str, filters: Mapping[str, Any]) -> bool:
        """Apply catalog-aware filters shared by field and random selectors."""
        if filters.get("side") and filters.get("side") != side:
            return False
        meta = self.catalog.get(follower.card_id, {})
        card_type = meta.get("type")
        if card_type is None and follower.card_id in self.card_db:
            card_type = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(self.card_db[follower.card_id].type)
        wanted_type = filters.get("card_type")
        if wanted_type:
            values = wanted_type if isinstance(wanted_type, (list, tuple)) else [wanted_type]
            if card_type is not None and card_type not in values and not (card_type == "countdown_amulet" and "amulet" in values):
                return False
        card_id = filters.get("card_id")
        if card_id is not None and int(card_id) != follower.card_id:
            return False
        base_cost = follower.base_cost
        if base_cost is None:
            base_cost = meta.get("cost")
        for key, comparator in (("max_base_cost", lambda actual, wanted: actual <= wanted), ("max_cost", lambda actual, wanted: actual <= wanted), ("min_base_cost", lambda actual, wanted: actual >= wanted)):
            wanted_cost = filters.get(key)
            if wanted_cost is None:
                continue
            try:
                if base_cost is None or not comparator(int(base_cost), int(wanted_cost)):
                    return False
            except (TypeError, ValueError):
                return False
        names = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
        wanted_name = filters.get("card_name")
        if wanted_name and str(wanted_name).casefold() not in {str(names.get("chs", "")).casefold(), str(names.get("eng", "")).casefold()}:
            return False
        tribes = meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ()
        if not tribes and follower.card_id in self.card_db:
            tribes = self.card_db[follower.card_id].tribes
        wanted_tribe = filters.get("tribe")
        if wanted_tribe:
            values = wanted_tribe if isinstance(wanted_tribe, (list, tuple)) else [wanted_tribe]
            actual_tribes: set[str] = set()
            for item in tribes:
                actual_tribes.add(str(item).casefold())
                try:
                    alias = _TRIBE_ALIASES.get(int(item))
                except (TypeError, ValueError):
                    alias = None
                if alias:
                    actual_tribes.add(alias.casefold())
            if not any(str(value).casefold() in actual_tribes for value in values):
                return False
        wanted_class = filters.get("class", filters.get("class_id"))
        if wanted_class is not None:
            class_id = meta.get("class_id")
            class_names = {1: "swordcraft", 2: "dragoncraft", 3: "forestcraft", 4: "runecraft", 5: "shadowcraft", 6: "havencraft", 7: "bloodcraft", 8: "portalcraft", 0: "neutral"}
            wanted = {str(item).casefold() for item in (wanted_class if isinstance(wanted_class, (list, tuple)) else [wanted_class])}
            actual_values = {str(class_id).casefold()} if class_id is not None else set()
            if class_id in class_names:
                actual_values.add(class_names[class_id])
            if not actual_values & wanted:
                return False
        if "has_last_words" in filters:
            has_last_words = bool(follower.last_words) or "last_words" in follower.statuses
            if bool(filters.get("has_last_words")) != has_last_words:
                return False
        return True

    @staticmethod
    def _bind_target(effect: Mapping[str, Any], kind: str, uid: int | None) -> dict[str, Any]:
        bound = dict(effect)
        target = dict(effect.get("target", {})) if isinstance(effect.get("target"), Mapping) else {}
        target["selection"] = "chosen"
        target["scope"] = "enemy_leader" if kind == "leader" else kind
        bound["target"] = target
        return bound

    def _draw_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int) -> list[StochasticBranch]:
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        if count is None:
            return [StochasticBranch(1.0, state, ("variable_amount",))]
        current = [StochasticBranch(1.0, state)]
        for _ in range(max(0, int(count))):
            output: list[StochasticBranch] = []
            for branch in current:
                if branch.state.deck_replacement:
                    # The replacement deck is a named opaque template.  Its
                    # next card cannot be inferred from the old distribution,
                    # but drawing still consumes one slot from the public
                    # count and remains explicitly incomplete.
                    unknown_state = branch.state.clone()
                    unknown_state.total_deck_count = max(0, unknown_state.total_deck_count - 1)
                    output.append(StochasticBranch(
                        branch.probability,
                        unknown_state,
                        tuple(sorted(set(branch.unsupported_ops) | {"draw_replaced_deck"})),
                        branch.warnings,
                    ))
                    continue
                visible_total = sum(max(0, int(n)) for n in branch.state.deck_distribution.values())
                total = max(0, int(branch.state.total_deck_count), visible_total)
                selector = {"filters": (effect.get("target", {}).get("filters", {}) if isinstance(effect.get("target"), Mapping) else {})}
                candidates = [
                    (cid, n)
                    for cid, n in branch.state.deck_distribution.items()
                    if n > 0 and (cid in self.card_db or cid in self.catalog) and self._card_matches_selector(cid, selector)
                ]
                known = sum(n for _, n in candidates)
                if not total:
                    output.append(StochasticBranch(branch.probability, branch.state, ("draw_unknown",)))
                    continue
                if not known:
                    unknown_state = branch.state.clone()
                    unknown_state.total_deck_count = max(0, unknown_state.total_deck_count - 1)
                    output.append(StochasticBranch(branch.probability, unknown_state, ("draw_unknown",)))
                    continue
                for card_id, number in candidates:
                    next_state = branch.state.clone()
                    next_state.total_deck_count = max(0, next_state.total_deck_count - 1)
                    next_state.deck_distribution[card_id] = max(0, next_state.deck_distribution.get(card_id, 0) - 1)
                    uid = self._next_instance_uid(next_state)
                    drawn_card = self.card_db.get(card_id) or self._hand_card_for_card(card_id, uid)
                    if drawn_card is None:
                        output.append(StochasticBranch(branch.probability * number / total, branch.state, (f"draw_card:{card_id}",)))
                        continue
                    next_state.hand.append(replace(drawn_card, unique_id=uid))
                    output.append(StochasticBranch(branch.probability * number / total, next_state))
                if known < total:
                    unknown_state = branch.state.clone()
                    unknown_state.total_deck_count = max(0, unknown_state.total_deck_count - 1)
                    output.append(StochasticBranch(branch.probability * (total - known) / total, unknown_state, ("draw_unknown",)))
            current = self._merge_stochastic(output)
        return current

    def _summon_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int) -> list[StochasticBranch]:
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        selector = effect.get("resource_selector") if isinstance(effect.get("resource_selector"), Mapping) else {}
        if count is None:
            return [StochasticBranch(1.0, state, ("variable_amount",))]
        distinct_by = selector.get("distinct_by")
        zone = selector.get("zone", "deck")
        filters = selector.get("filters") if isinstance(selector.get("filters"), Mapping) else {}

        def expand(branch: StochasticBranch, remaining: int, selected_ids: frozenset[int]) -> list[StochasticBranch]:
            if remaining <= 0 or len(branch.state.my_board) >= 5:
                return [branch]
            if zone in ("destroyed_this_match", "destroyed", "graveyard"):
                entities = [item for item in branch.state.destroyed_this_match if self._matches_follower_filters(item, "ally", filters)]
                if distinct_by == "card_id":
                    entities = [item for item in entities if item.card_id not in selected_ids]
                total = len(entities)
                if not total:
                    return [branch]
                output: list[StochasticBranch] = []
                for entity in entities:
                    next_state = branch.state.clone()
                    uid = self._next_instance_uid(next_state)
                    copied = replace(entity, unique_id=uid) if effect.get("copy_mode") == "exact" and effect.get("preserve_state") else self._follower_for_card(entity.card_id, uid)
                    if copied is None:
                        output.append(StochasticBranch(branch.probability / total, branch.state, (f"summon_card:{entity.card_id}",)))
                        continue
                    next_state.my_board.append(copied)
                    next_state.last_created_uid = copied.unique_id
                    output.extend(expand(StochasticBranch(branch.probability / total, next_state), remaining - 1, selected_ids | ({entity.card_id} if distinct_by == "card_id" else set())))
                return output
            candidates = [(cid, n) for cid, n in branch.state.deck_distribution.items() if n > 0 and self._card_matches_selector(cid, selector)]
            if distinct_by == "card_id":
                candidates = [(cid, n) for cid, n in candidates if cid not in selected_ids]
            known_total = sum(n for _, n in candidates)
            if branch.state.deck_replacement:
                return [StochasticBranch(
                    branch.probability,
                    branch.state,
                    tuple(sorted(set(branch.unsupported_ops) | {"summon_replaced_deck"})),
                    branch.warnings,
                )]
            # ``total_deck_count`` can exceed the visible distribution when
            # Tracker has not identified every card in the deck.  Keep that
            # residual as an explicit incomplete branch instead of
            # renormalising known cards to probability 1.  For a complete
            # public deck the residual is zero and existing exact outcomes
            # remain unchanged.
            visible_total = sum(max(0, int(n)) for n in branch.state.deck_distribution.values())
            selector_side = selector.get("side")
            if selector_side is None:
                selector_side = filters.get("side")
            if selector_side not in (None, "ally", "your"):
                # No opponent-deck identities are exposed in the ally deck
                # distribution.  Preserve an opaque branch instead of
                # accidentally summoning a known ally card.
                candidates = []
                hidden_total = max(1, int(branch.state.total_deck_count))
            else:
                hidden_total = max(0, int(branch.state.total_deck_count) - visible_total)
            total = known_total + hidden_total
            if not total:
                # A public deck with no eligible cards is an empty selector,
                # not an unknown pool.  The summon is skipped just like an
                # empty random target; only a positive hidden remainder needs
                # the ``summon_unknown`` incomplete marker below.
                return [branch]
            output: list[StochasticBranch] = []
            for card_id, number in candidates:
                next_state = branch.state.clone()
                uid = self._next_instance_uid(next_state)
                follower = self._follower_for_card(card_id, uid)
                if follower is None:
                    output.append(StochasticBranch(branch.probability * number / total, branch.state, (f"summon_card:{card_id}",)))
                    continue
                next_state.deck_distribution[card_id] = max(0, next_state.deck_distribution.get(card_id, 0) - 1)
                next_state.total_deck_count = max(0, next_state.total_deck_count - 1)
                next_state.my_board.append(follower)
                next_state.last_created_uid = follower.unique_id
                output.extend(expand(StochasticBranch(branch.probability * number / total, next_state), remaining - 1, selected_ids | ({card_id} if distinct_by == "card_id" else set())))
            if hidden_total:
                unknown_state = branch.state.clone()
                unknown_state.total_deck_count = max(0, unknown_state.total_deck_count - 1)
                output.append(StochasticBranch(
                    branch.probability * hidden_total / total,
                    unknown_state,
                    tuple(sorted(set(branch.unsupported_ops) | {"summon_unknown"})),
                ))
            return output

        return self._merge_stochastic(expand(StochasticBranch(1.0, state), max(0, int(count)), frozenset()))

    def _card_matches_selector(self, card_id: int, selector: Mapping[str, Any]) -> bool:
        meta = self.catalog.get(int(card_id), {})
        card = self.card_db.get(int(card_id))
        filters = selector.get("filters") if isinstance(selector.get("filters"), Mapping) else {}
        side = selector.get("side", filters.get("side"))
        # LethalState currently carries the ally deck distribution only.
        # Never reuse that public distribution for an opponent-deck selector;
        # the residual is kept as an explicit unknown branch instead.
        if side not in (None, "ally", "your"):
            return False
        card_type = meta.get("type")
        if card_type is None and card is not None:
            card_type = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(card.type)
        wanted = filters.get("card_type")
        if wanted:
            values = wanted if isinstance(wanted, (list, tuple)) else [wanted]
            if card_type not in values and not (card_type == "countdown_amulet" and "amulet" in values):
                return False
        max_cost = filters.get("max_cost", filters.get("max_base_cost"))
        cost = meta.get("cost", card.cost if card else 0)
        if isinstance(max_cost, (int, float)) and int(cost or 0) > int(max_cost):
            return False
        wanted_tribe = filters.get("tribe")
        if wanted_tribe:
            tribes = meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else (card.tribes if card else ())
            wanted_values = wanted_tribe if isinstance(wanted_tribe, (list, tuple)) else [wanted_tribe]
            if not any(str(value).casefold() in {str(item).casefold() for item in tribes} for value in wanted_values):
                return False
        wanted_class = filters.get("class", filters.get("class_id"))
        if wanted_class is not None:
            class_id = meta.get("class_id")
            class_names = {1: "swordcraft", 2: "dragoncraft", 3: "forestcraft", 4: "runecraft", 5: "shadowcraft", 6: "havencraft", 7: "bloodcraft", 8: "portalcraft", 0: "neutral"}
            wanted_values = {str(item).casefold() for item in (wanted_class if isinstance(wanted_class, (list, tuple)) else [wanted_class])}
            actual_values = {str(class_id).casefold()} if class_id is not None else set()
            if class_id in class_names:
                actual_values.add(class_names[class_id])
            if not actual_values & wanted_values:
                return False
        return True

    @staticmethod
    def _merge_stochastic(branches: list[StochasticBranch]) -> list[StochasticBranch]:
        merged: dict[tuple, StochasticBranch] = {}
        for branch in branches:
            if branch.probability <= 0:
                continue
            key = branch.state.state_key()
            previous = merged.get(key)
            if previous is None:
                merged[key] = branch
                continue
            unsupported = tuple(sorted(set(previous.unsupported_ops) | set(branch.unsupported_ops)))
            warnings = previous.warnings if len(previous.warnings) <= len(branch.warnings) else branch.warnings
            merged[key] = StochasticBranch(previous.probability + branch.probability, previous.state, unsupported, warnings)
        return list(merged.values())

    @staticmethod
    def _hand_indexes(state: LethalState, target: Mapping[str, Any], target_uid: Any, source_uid: int) -> list[int]:
        """Resolve hand targets (field target resolution intentionally stays separate)."""
        scope = target.get("scope")
        selection = target.get("selection", "chosen")
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        requested: set[int] | None = None
        if isinstance(target_uid, Mapping):
            requested = {int(key) for key, value in target_uid.items() if value}
        elif isinstance(target_uid, (list, tuple, set)):
            requested = {int(value) for value in target_uid}
        elif target_uid is not None:
            try:
                requested = {int(target_uid)}
            except (TypeError, ValueError):
                requested = None
        def matches(card: LethalHandCard) -> bool:
            if filters.get("card_id") is not None and int(filters["card_id"]) != card.card_id:
                return False
            wanted_type = filters.get("card_type")
            if wanted_type:
                values = wanted_type if isinstance(wanted_type, (list, tuple)) else [wanted_type]
                actual = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(card.type, card.type)
                if actual not in values and not (actual == "countdown_amulet" and "amulet" in values):
                    return False
            wanted_tribe = filters.get("tribe")
            if wanted_tribe:
                values = wanted_tribe if isinstance(wanted_tribe, (list, tuple)) else [wanted_tribe]
                if not any(str(value).casefold() in {str(item).casefold() for item in card.tribes} for value in values):
                    return False
            return True
        if scope in ("self", "trigger_source"):
            return [index for index, card in enumerate(state.hand) if card.unique_id == source_uid and matches(card)]
        candidates = [index for index, card in enumerate(state.hand) if matches(card)]
        if requested is not None:
            candidates = [index for index in candidates if state.hand[index].unique_id in requested]
        elif selection not in ("all", "each"):
            return []
        count = target.get("count")
        if isinstance(count, int) and count > 0 and selection not in ("all", "each"):
            candidates = candidates[:count]
        return candidates

    def _card_has_trigger(self, card_id: int, trigger: str, card: LethalHandCard | None = None) -> bool:
        """Return whether a card has a runtime ability for ``trigger``.

        Tracker's ``has_spell_boost`` bit is authoritative for the current
        entity, but generated rules remain the source of printed abilities.
        Looking at both lets a fresh snapshot use the exact bit while still
        supporting hand-built fixtures whose card rules carry the trigger.
        """
        if card is not None and trigger == "on_spellboost" and card.has_spell_boost:
            return True
        rule = self.rules.get(int(card_id), {})
        for mode in rule.get("modes", ()) if isinstance(rule, Mapping) else ():
            if not isinstance(mode, Mapping):
                continue
            for ability in mode.get("abilities", ()) if isinstance(mode.get("abilities"), (list, tuple)) else ():
                if isinstance(ability, Mapping) and ability.get("trigger") == trigger:
                    return True
        return False

    def _spellboost_hand_indexes(self, state: LethalState, target: Mapping[str, Any], target_uid: Any, source_uid: int) -> list[int]:
        """Resolve cards eligible for a Spellboost event.

        “Spellboost your hand” affects cards that actually have an
        ``On Spellboost`` ability; the generated target may omit an explicit
        ``has_trigger`` filter, so apply that game rule here.  A selected
        target with no matching card is a normal no-op only for an ``all``
        selector; chosen/random selectors retain a warning or probability
        branch so the searcher cannot invent a target.
        """
        indexes = self._hand_indexes(state, target, target_uid, source_uid)
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        wanted_trigger = filters.get("has_trigger")
        result: list[int] = []
        for index in indexes:
            card = state.hand[index]
            if wanted_trigger:
                if self._card_has_trigger(card.card_id, str(wanted_trigger), card):
                    result.append(index)
            elif self._card_has_trigger(card.card_id, "on_spellboost", card):
                result.append(index)
        return result

    def _resolve_hand_trigger(self, state: LethalState, card_uid: int, trigger: str) -> InterpreterResult:
        """Dispatch one ability against a card that is still in hand."""
        card = next((item for item in state.hand if item.unique_id == card_uid), None)
        if card is None:
            return InterpreterResult(state, warnings=(f"hand trigger source {card_uid} not found",))
        rule = self.rules.get(card.card_id, {})
        result = self._resolve_abilities(state, rule, "normal", trigger, source_uid=card_uid)
        unsupported = set(result.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{card.card_id}")
        return InterpreterResult(result.state, tuple(sorted(unsupported)), result.warnings)

    def _spellboost_effect(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None) -> InterpreterResult:
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        if count is None:
            return InterpreterResult(state, ("spellboost_amount",))
        count = max(0, int(count))
        target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}
        indexes = self._spellboost_hand_indexes(state, target, target_uid, source_uid)
        if not indexes:
            if target.get("selection") not in ("all", "each"):
                return InterpreterResult(state, warnings=("spellboost target not found",))
            return InterpreterResult(state)
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Keep UIDs rather than indexes: an On Spellboost ability can alter a
        # hand (draw/return) and indexes may then shift.
        card_uids = [current.hand[index].unique_id for index in indexes if index < len(current.hand)]
        for card_uid in card_uids:
            for _ in range(count):
                index = next((i for i, item in enumerate(current.hand) if item.unique_id == card_uid), None)
                if index is None:
                    break
                current = current.clone()
                card = current.hand[index]
                current.hand[index] = replace(card, spell_boost_count=max(0, int(card.spell_boost_count) + 1), has_spell_boost=card.has_spell_boost or self._card_has_trigger(card.card_id, "on_spellboost", card))
                triggered = self._resolve_hand_trigger(current, card_uid, "on_spellboost")
                current = triggered.state
                unsupported.update(triggered.unsupported_ops)
                warnings.extend(triggered.warnings)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _spellboost_effect_branches(self, state: LethalState, effect: Mapping[str, Any], source_uid: int, target_uid: Any = None) -> list[StochasticBranch]:
        count = self._resolve_value(state, effect.get("count", 1), source_uid)
        if count is None:
            return [StochasticBranch(1.0, state, ("spellboost_amount",))]
        count = max(0, int(count))
        target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {"scope": "any", "selection": "all", "filters": {"zone": "hand"}}
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        selection = target.get("selection", "chosen")
        all_indexes = self._spellboost_hand_indexes(state, {**target, "selection": "all"}, target_uid, source_uid)
        if selection == "random":
            if not all_indexes:
                return [StochasticBranch(1.0, state)]
            branches: list[StochasticBranch] = []
            for index in all_indexes:
                uid = state.hand[index].unique_id
                bound = dict(effect)
                bound_target = dict(target)
                bound_target["selection"] = "chosen"
                bound_target["scope"] = "any"
                bound["target"] = bound_target
                result = self._spellboost_effect(state, bound, source_uid, uid)
                branches.append(StochasticBranch(1.0 / len(all_indexes), result.state, result.unsupported_ops, result.warnings))
            return self._merge_stochastic(branches)
        indexes = self._spellboost_hand_indexes(state, target, target_uid, source_uid)
        if not indexes:
            if selection not in ("all", "each"):
                return [StochasticBranch(1.0, state, warnings=("spellboost target not found",))]
            return [StochasticBranch(1.0, state)]
        current = [StochasticBranch(1.0, state)]
        for uid in [state.hand[index].unique_id for index in indexes]:
            next_branches: list[StochasticBranch] = []
            for branch in current:
                result = self._spellboost_effect(branch.state, {**effect, "target": {**target, "selection": "chosen", "scope": "any"}}, source_uid, uid)
                next_branches.append(StochasticBranch(branch.probability, result.state, result.unsupported_ops, result.warnings))
            current = self._merge_stochastic(next_branches)
        return current

    def _target_indexes(self, state: LethalState, target: Mapping[str, Any], target_uid: Any, source_uid: int) -> list[tuple[str, int]]:
        """Resolve the small deterministic target subset used by Step 5.

        The return value contains side/index pairs so callers can mutate a
        cloned board without depending on card ids.  Random selectors remain
        unsupported in the deterministic interpreter and are handled by the
        stochastic resolver introduced in Step 6.
        """
        scope = target.get("scope")
        selection = target.get("selection", "chosen")
        filters = target.get("filters") if isinstance(target.get("filters"), Mapping) else {}
        requested: set[int] | None = None
        if isinstance(target_uid, Mapping):
            requested = {int(key) for key, value in target_uid.items() if value}
        elif isinstance(target_uid, (list, tuple, set)):
            requested = {int(value) for value in target_uid}
        elif target_uid is not None:
            try:
                requested = {int(target_uid)}
            except (TypeError, ValueError):
                requested = None

        def matches(follower: LethalFollower, side: str) -> bool:
            if (filters.get("exclude_source") or filters.get("exclude_self")) and follower.unique_id == source_uid:
                return False
            # Ambush blocks enemy-targeted choices, but not effects that apply
            # to every permanent.  This makes targeted damage/buffs safe while
            # preserving the game's area-effect semantics.
            if side == "enemy" and selection not in ("all", "each") and self._source_is_ally(state, source_uid) and _has_keyword(follower, "ambush"):
                return False
            return self._matches_follower_filters(follower, side, filters)

        boards: list[tuple[str, list[LethalFollower]]] = []
        if scope in ("self", "trigger_source", "previous_copy", "previous_summon", "previous_add", "ally_follower"):
            boards.append(("ally", state.my_board))
        elif scope == "previous_target":
            # ``previous_target`` is a relation to the UID selected by the
            # preceding target operation (for example, "remove Ward from
            # it").  It may point at either side of the field; unlike
            # ``self`` there is no safe fallback when the caller did not
            # provide that UID, so the candidate list is intentionally empty
            # in that case below.
            boards.extend((("ally", state.my_board), ("enemy", state.enemy_board)))
        elif scope == "enemy_follower":
            boards.append(("enemy", state.enemy_board))
        elif scope == "any":
            boards.extend((("ally", state.my_board), ("enemy", state.enemy_board)))
        else:
            return []

        result: list[tuple[str, int]] = []
        for side, board in boards:
            candidates = [(i, f) for i, f in enumerate(board) if matches(f, side)]
            if scope in ("self", "trigger_source"):
                candidates = [(i, f) for i, f in candidates if f.unique_id == source_uid]
            elif scope in ("previous_copy", "previous_summon", "previous_add"):
                candidates = [(i, f) for i, f in candidates if f.unique_id == state.last_created_uid]
            elif scope == "previous_target":
                # A selected target is represented by ``target_uid``.  Never
                # guess from ``last_created_uid`` here: doing so can remove a
                # keyword from the wrong permanent when a preceding effect
                # created a token between selection and the relation.
                candidates = [(i, f) for i, f in candidates if requested is not None and f.unique_id in requested]
            if requested is not None:
                candidates = [(i, f) for i, f in candidates if f.unique_id in requested]
            elif selection not in ("all", "each"):
                # A chosen target must be supplied by the caller.  Self is an
                # intentional exception because its uid is the source.
                if scope not in ("self", "trigger_source", "previous_copy", "previous_summon", "previous_add"):
                    candidates = []
            count = target.get("count")
            if isinstance(count, int) and count > 0 and selection not in ("all", "each"):
                candidates = candidates[:count]
            result.extend((side, i) for i, _ in candidates)
        return result

    @staticmethod
    def _source_is_ally(state: LethalState, source_uid: int) -> bool:
        # The interpreter only executes player-owned card rules.  Spell and
        # delayed effects may use a synthetic source UID (the card has already
        # left hand), so an unknown UID is still an allied source unless it is
        # explicitly an enemy-board entity.
        if any(item.unique_id == source_uid for item in state.enemy_board):
            return False
        return True

    @staticmethod
    def _next_instance_uid(state: LethalState) -> int:
        values = [f.unique_id for f in (*state.my_board, *state.enemy_board)]
        values.extend(c.unique_id for c in state.hand)
        values.extend(int(item.get("unique_id")) for item in state.crest_instances if isinstance(item, Mapping) and str(item.get("unique_id", "")).isdigit())
        values.extend(int(item.get("unique_id")) for item in state.enemy_crest_instances if isinstance(item, Mapping) and str(item.get("unique_id", "")).isdigit())
        values.extend(f.unique_id for f in state.destroyed_this_match)
        return max(values, default=0) + 1

    def _is_amulet(self, follower: LethalFollower) -> bool:
        card_type = self.catalog.get(follower.card_id, {}).get("type")
        if card_type in ("amulet", "countdown_amulet"):
            return True
        template = self.card_db.get(follower.card_id)
        return bool(template is not None and template.type in (2, 3))

    def _ensure_catalog_resources(self, state: LethalState, card_id: int, unique_id: int) -> None:
        """Materialize Faith/Crest alternate-mode text from the catalog.

        ``CardRules`` describes the card's immediate effects, while
        ``alt_modes`` in the catalog carries the persistent leader resource
        definition.  Keeping this bridge here means a fresh Tracker snapshot
        does not need a hand-authored Faith fixture for every card.
        """
        meta = self.catalog.get(card_id, {})
        modes = meta.get("alt_modes", ()) if isinstance(meta.get("alt_modes"), (list, tuple)) else ()
        for mode in modes:
            if not isinstance(mode, Mapping):
                continue
            type_key = str(mode.get("type_key", "")).casefold()
            text = mode.get("text", {}) if isinstance(mode.get("text"), Mapping) else {}
            text_eng = str(text.get("eng", ""))
            if type_key == "faith":
                if any(item.get("source_card_id") == card_id and item.get("unique_id") == unique_id for item in state.faith_instances if isinstance(item, Mapping)):
                    continue
                # Older Tracker snapshots may expose only the aggregate
                # Faith value.  Preserve it when materialising the first
                # explicit instance; otherwise syncing the new zero-valued
                # instance would silently erase the public resource.
                seed_value = int(state.faith) if not state.faith_instances else 0
                abilities: list[dict[str, Any]] = []
                trigger = None
                if "allied follower evolves" in text_eng.casefold():
                    trigger = "on_ally_follower_evolve"
                elif "allied amulet is destroyed" in text_eng.casefold():
                    trigger = "on_ally_amulet_destroy"
                elif "select modes" in text_eng.casefold() or "select mode" in text_eng.casefold():
                    trigger = "on_mode_selected"
                if trigger:
                    abilities.append({"trigger": trigger, "effects": [{"op": "modify_resource", "resource": "faith", "amount": 1, "source_card_id": card_id}]})
                state.faith_instances.append({"source_card_id": card_id, "unique_id": unique_id, "value": seed_value, "initial_value": seed_value, "mode_limit_bonus": 0, "abilities": abilities})
                self._sync_faith_aggregate(state)

    def _catalog_crest_abilities(self, card_id: int) -> list[dict[str, Any]]:
        meta = self.catalog.get(card_id, {})
        modes = meta.get("alt_modes", ()) if isinstance(meta.get("alt_modes"), (list, tuple)) else ()
        abilities: list[dict[str, Any]] = []
        for mode in modes:
            if not isinstance(mode, Mapping) or str(mode.get("type_key", "")).casefold() != "crest":
                continue
            text = mode.get("text", {}) if isinstance(mode.get("text"), Mapping) else {}
            text_eng = str(text.get("eng", ""))
            if "at the end of your turn" in text_eng.casefold() and "didn't attack" in text_eng.casefold() and "number of crests" in text_eng.casefold():
                abilities.append({
                    "trigger": "on_turn_end",
                    "condition": {"state": "attacked_with_follower_this_turn", "cmp": "eq", "value": False},
                    "effects": [{"op": "damage", "target": {"scope": "any", "selection": "all", "allocation": "ordered_split", "filters": {"side": "enemy", "card_type": ["follower", "leader"]}}, "amount": "var:crest_count"}],
                })
        return abilities

    @staticmethod
    def _record_destroyed(state: LethalState, follower: LethalFollower) -> None:
        """Append an entity snapshot once to the destroyed-this-match pool."""
        if any(item.unique_id == follower.unique_id for item in state.destroyed_this_match):
            return
        state.destroyed_this_match.append(follower)

    @staticmethod
    def _sync_faith_aggregate(state: LethalState) -> None:
        if state.faith_instances:
            state.faith = sum(int(item.get("value", 0) or 0) for item in state.faith_instances if isinstance(item, Mapping))

    def _hand_card_for_card(self, card_id: int, unique_id: int) -> LethalHandCard | None:
        template = self.card_db.get(card_id)
        if template is not None:
            static = self._static_keywords(card_id, template)
            return replace(
                template,
                unique_id=unique_id,
                has_spell_boost=template.has_spell_boost or self._card_has_trigger(card_id, "on_spellboost"),
                statuses=tuple(sorted(set(template.statuses) | static)),
                has_bane=template.has_bane or "bane" in static,
                has_drain=template.has_drain or "drain" in static,
                has_ambush=template.has_ambush or "ambush" in static,
                buff=template.buff,
            )
        meta = self.catalog.get(card_id, {})
        if not meta:
            return None
        names = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
        stats = meta.get("stats", {}) if isinstance(meta.get("stats"), Mapping) else {}
        type_map = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}
        static = self._static_keywords(card_id)
        return LethalHandCard(
            unique_id=unique_id,
            card_id=card_id,
            name=str(names.get("eng") or names.get("chs") or card_id),
            cost=int(meta.get("cost", 0) or 0),
            type=type_map.get(meta.get("type"), 1),
            atk=int(stats.get("attack", 0) or 0),
            life=int(stats.get("life", 0) or 0),
            tribes=tuple(str(item) for item in (meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ())),
            has_spell_boost=self._card_has_trigger(card_id, "on_spellboost"),
            statuses=tuple(sorted(static)),
            has_bane="bane" in static,
            has_drain="drain" in static,
            has_ambush="ambush" in static,
        )

    def _copy_source_candidates(self, state: LethalState, source: Mapping[str, Any], target_uid: Any, source_uid: int) -> list[LethalFollower | LethalHandCard]:
        """Resolve a copy source while preserving entity state for exact copies."""
        scope = source.get("scope")
        zone = source.get("zone")
        selection = source.get("selection", "chosen")
        filters = source.get("filters") if isinstance(source.get("filters"), Mapping) else {}
        requested: set[int] | None = None
        if isinstance(target_uid, Mapping):
            requested = {int(key) for key, value in target_uid.items() if value}
        elif isinstance(target_uid, (list, tuple, set)):
            requested = {int(value) for value in target_uid}
        elif target_uid is not None:
            try:
                requested = {int(target_uid)}
            except (TypeError, ValueError):
                requested = None

        if scope in ("self", "trigger_source"):
            zone = "field"
            requested = {source_uid}
        elif scope in ("previous_copy", "previous_summon", "previous_add"):
            requested = {state.last_created_uid} if state.last_created_uid is not None else set()
            zone = "field" if scope != "previous_add" else "hand"
        if selection not in ("all", "random") and requested is None:
            return []
        if zone in (None, "field"):
            side = source.get("side") or filters.get("side")
            boards: list[LethalFollower] = []
            if side in (None, "ally"):
                boards.extend(state.my_board)
            if side in (None, "enemy"):
                boards.extend(state.enemy_board)
            candidates: list[LethalFollower] = []
            for follower in boards:
                actual_side = "ally" if any(item.unique_id == follower.unique_id for item in state.my_board) else "enemy"
                if requested is not None and follower.unique_id not in requested:
                    continue
                if not self._matches_follower_filters(follower, actual_side, filters):
                    continue
                candidates.append(follower)
            if not candidates and state.last_destroyed_snapshot is not None:
                snapshot = state.last_destroyed_snapshot
                actual_side = str(filters.get("side") or source.get("side") or "enemy")
                if (requested is None or snapshot.unique_id in requested) and self._matches_follower_filters(snapshot, actual_side, filters):
                    candidates.append(snapshot)
            if selection == "all":
                return candidates
            return candidates[:1]
        if zone in ("hand", "ally_hand"):
            candidates = [card for card in state.hand if (requested is None or card.unique_id in requested)]
            def match_card(card: LethalHandCard) -> bool:
                wanted_type = filters.get("card_type")
                if wanted_type:
                    values = wanted_type if isinstance(wanted_type, (list, tuple)) else [wanted_type]
                    actual = {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(card.type, card.type)
                    if actual not in values and not (actual == "countdown_amulet" and "amulet" in values):
                        return False
                wanted_tribe = filters.get("tribe")
                return not wanted_tribe or any(str(item).casefold() in {str(t).casefold() for t in card.tribes} for item in (wanted_tribe if isinstance(wanted_tribe, (list, tuple)) else [wanted_tribe]))
            candidates = [card for card in candidates if match_card(card)]
            return candidates if selection == "all" else candidates[:1]
        if zone in ("destroyed_this_match", "destroyed", "graveyard"):
            candidates = [item for item in state.destroyed_this_match if (requested is None or item.unique_id in requested) and self._matches_follower_filters(item, "ally", filters)]
            if filters.get("has_last_words") is True:
                candidates = [item for item in candidates if item.last_words or "last_words" in item.statuses]
            return candidates[:1] if selection != "all" else candidates
        # Enemy hand identities are hidden from Tracker and cannot be guessed.
        return []

    def _follower_for_card(self, card_id: int, unique_id: int) -> LethalFollower | None:
        card = self._hand_card_for_card(card_id, unique_id)
        if card is None:
            return None
        rule = self.rules.get(card_id, {})
        static = self._static_keywords(card_id, card)
        storm = card.static_storm or "storm" in static
        rush = card.static_rush or "rush" in static or storm
        countdown = rule.get("countdown") if isinstance(rule, Mapping) and isinstance(rule.get("countdown"), (int, float)) else None
        default_attacks = rule.get("default_attacks", 1) if isinstance(rule, Mapping) else 1
        try:
            default_attacks = max(1, int(default_attacks))
        except (TypeError, ValueError):
            default_attacks = 1
        return LethalFollower(
            unique_id=unique_id,
            card_id=card_id,
            name=card.name,
            atk=card.atk,
            hp=card.life,
            has_storm=storm,
            has_rush=rush,
            is_ward="ward" in static,
            can_attack_leader=storm,
            can_attack_field=rush,
            attacks_left=default_attacks,
            statuses=tuple(sorted(set(str(item) for item in static))),
            countdown=int(countdown) if countdown is not None else None,
            base_cost=card.cost,
            spell_boost_count=card.spell_boost_count,
            has_spell_boost=card.has_spell_boost or self._card_has_trigger(card_id, "on_spellboost"),
            variable_x=card.variable_x,
            supplement_info=card.supplement_info,
            has_bane="bane" in static,
            has_drain="drain" in static,
            has_ambush="ambush" in static,
            buff=card.buff,
        )

    def _resolve_last_words(self, state: LethalState, follower: LethalFollower) -> InterpreterResult:
        """Resolve simple nested Last Words abilities on an allied entity."""
        if not follower.last_words or follower.abilities_removed:
            return InterpreterResult(state)
        current = state.clone()
        current.last_destroyed_snapshot = follower
        unsupported: set[str] = set()
        warnings: list[str] = []
        for ability in follower.last_words:
            if not isinstance(ability, Mapping) or ability.get("trigger") != "on_last_word":
                continue
            current, ops, warns = self._effects(current, ability.get("effects", ()), follower.unique_id)
            unsupported.update(ops)
            warnings.extend(warns)
        current.last_destroyed_snapshot = None
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    @staticmethod
    def _resolve_value(state: LethalState, value: Any, source_uid: int) -> int | None:
        if isinstance(value, (int, float)):
            return int(value)
        if not isinstance(value, str):
            return None
        if value.casefold() in {"var:x", "x"}:
            for card in state.hand:
                if card.unique_id == source_uid:
                    return int(card.variable_x)
            for follower in state.my_board:
                if follower.unique_id == source_uid:
                    return int(follower.variable_x)
            return None
        if value == "var:hand_count":
            return len(state.hand)
        if value == "var:cemetery":
            return state.cemetery
        if value == "var:crest_count":
            return len(state.crest_instances) if state.crest_instances else len(state.active_crests)
        if value in ("var:earth_sigil", "var:earth_rite"):
            return state.earth_sigil
        if value == "var:faith":
            return state.faith
        if value == "var:faith_instance_count":
            return len(state.faith_instances)
        if value == "var:rally":
            return state.rally
        if value == "var:play_count":
            return state.play_count
        if value == "var:extra_pp":
            return state.extra_pp
        if value == "var:skybound_art":
            return state.skybound_art
        if value == "var:super_skybound_art":
            return state.super_skybound_art
        if value == "var:enemy_board_count":
            return len(state.enemy_board)
        if value == "var:source_attack":
            follower = next((f for f in state.my_board if f.unique_id == source_uid), None)
            return follower.atk if follower else None
        if value.startswith("var:hand_tribe:"):
            tribe = value.rsplit(":", 1)[-1]
            aliases = {tribe}
            if tribe in ("fairy", "pixie"):
                aliases.update({"5", "fairy", "pixie"})
            return sum(1 for card in state.hand if aliases & {item.casefold() for item in getattr(card, "tribes", ())})
        if value == "var:evolved_allies_this_turn":
            return state.evolved_allies_this_turn
        if value == "var:evolved_allies_this_match":
            return state.evolved_allies_this_match
        return None


__all__ = ["EventInterpreter", "InterpreterResult", "StochasticBranch"]
