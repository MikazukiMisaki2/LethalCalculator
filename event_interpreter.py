"""Minimal event-driven rules interpreter for CardRules v2.

This first slice intentionally supports only deterministic lethal primitives:
play, leader damage, buffs, PP recovery, Storm/Rush, Ward combat, and evolve.
Unsupported effects are reported instead of being silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from lethal_models import LethalFollower, LethalHandCard, LethalState


@dataclass(frozen=True)
class InterpreterResult:
    state: LethalState
    unsupported_ops: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EventInterpreter:
    def __init__(self, rules: Mapping[str, Any] | None = None) -> None:
        raw = rules.get("rules", {}) if isinstance(rules, Mapping) and isinstance(rules.get("rules"), Mapping) else rules
        self.rules: dict[int, dict[str, Any]] = {}
        for key, value in (raw or {}).items():
            if isinstance(value, Mapping):
                try:
                    self.rules[int(key)] = dict(value)
                except (TypeError, ValueError):
                    continue

    def play(self, state: LethalState, unique_id: int, mode: str = "normal", target_uid: Any = None) -> InterpreterResult:
        index = next((i for i, card in enumerate(state.hand) if card.unique_id == unique_id), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"hand card {unique_id} not found",))
        card = state.hand[index]
        enhanced = mode == "enhance" and card.enhance_cost is not None
        play_cost = card.enhance_cost if enhanced else card.cost
        if play_cost is None or state.pp < play_cost:
            return InterpreterResult(state, warnings=(f"insufficient PP for {card.name}",))
        if card.type == 1 and len(state.my_board) >= 5:
            return InterpreterResult(state, warnings=("ally field is full",))
        next_state = state.clone()
        next_state.hand.pop(index)
        next_state.pp -= play_cost
        next_state.play_count += 1
        if card.type in (3, 4):
            next_state.cemetery += 1
        rule = self.rules.get(card.card_id, {})
        # A follower is already on the field when its Fanfare resolves, so
        # self-targeting buffs and status grants must be able to find it.
        intrinsic_damage = card.enhance_face_damage if enhanced and card.enhance_face_damage else card.face_damage
        intrinsic_recover = card.enhance_recover_pp if enhanced and card.enhance_recover_pp else card.recover_pp
        intrinsic_buff = card.enhance_buff_atk if enhanced and card.enhance_buff_atk else card.buff_atk
        if card.type == 1:
            static = rule.get("static_keywords", ()) if isinstance(rule, Mapping) else ()
            storm = card.static_storm or (enhanced and card.enhance_gain_storm) or "storm" in static
            rush = card.static_rush or "rush" in static or storm
            next_state.my_board.append(LethalFollower(
                unique_id=card.unique_id, card_id=card.card_id, name=card.name,
                atk=card.atk + intrinsic_buff, hp=card.life, has_storm=storm, has_rush=rush,
                is_ward="ward" in static, can_attack_leader=storm, can_attack_field=rush, attacks_left=1,
            ))
        on_play = self._resolve_abilities(next_state, rule, mode, "on_play", source_uid=card.unique_id, target_uid=target_uid)
        fanfare = self._resolve_abilities(on_play.state, rule, mode, "on_fanfare", source_uid=card.unique_id, target_uid=target_uid)
        next_state = fanfare.state
        # Spellboost is a global event: playing a spell triggers each allied
        # follower that has an on_spellboost ability. Resolve in board order
        # so deterministic buffs and resource changes are reproducible.
        spellboost = InterpreterResult(next_state)
        if card.type == 4:
            spellboost = self._resolve_board_trigger(next_state, "on_spellboost")
            next_state = spellboost.state
        # Compatibility for hand cards created directly in tests or by the
        # legacy catalog: these are intrinsic effects, not engine transitions.
        if intrinsic_damage or intrinsic_recover:
            next_state = next_state.clone()
            next_state.enemy_hp -= intrinsic_damage
            next_state.pp = min(next_state.max_pp, next_state.pp + intrinsic_recover)
        unsupported = set(on_play.unsupported_ops) | set(fanfare.unsupported_ops) | set(spellboost.unsupported_ops)
        static_keywords = rule.get("static_keywords", ()) if isinstance(rule, Mapping) else ()
        unsupported.update(f"static_keyword:{keyword}" for keyword in static_keywords if keyword not in ("storm", "rush", "ward"))
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{card.card_id}")
        return InterpreterResult(next_state, tuple(sorted(unsupported)), on_play.warnings + fanfare.warnings + spellboost.warnings)

    def attack_follower(self, state: LethalState, attacker_uid: int, target_uid: int) -> InterpreterResult:
        ai = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        ti = next((i for i, f in enumerate(state.enemy_board) if f.unique_id == target_uid), None)
        if ai is None or ti is None:
            return InterpreterResult(state, warnings=("combat target not found",))
        attacker = state.my_board[ai]
        target = state.enemy_board[ti]
        if not attacker.can_attack_field or attacker.attacks_left <= 0:
            return InterpreterResult(state, warnings=(f"{attacker.name} cannot attack a follower",))
        if any(f.is_ward for f in state.enemy_board) and not target.is_ward:
            return InterpreterResult(state, warnings=("Ward must be attacked first",))
        next_state = state.clone()
        next_state.enemy_board.pop(ti)
        remaining_target_hp = target.hp - attacker.atk
        if remaining_target_hp > 0:
            next_state.enemy_board.insert(ti, replace(target, hp=remaining_target_hp))
        remaining_attacker_hp = attacker.hp - target.atk
        next_state.my_board.pop(ai)
        if remaining_attacker_hp > 0:
            next_state.my_board.insert(ai, replace(attacker, hp=remaining_attacker_hp, attacks_left=attacker.attacks_left - 1))
        else:
            next_state.cemetery += 1
        return InterpreterResult(next_state)

    def attack_leader(self, state: LethalState, attacker_uid: int) -> InterpreterResult:
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"attacker {attacker_uid} not found",))
        attacker = state.my_board[index]
        if any(f.is_ward for f in state.enemy_board):
            return InterpreterResult(state, warnings=("Ward blocks leader attack",))
        if not attacker.can_attack_leader or attacker.attacks_left <= 0:
            return InterpreterResult(state, warnings=(f"{attacker.name} cannot attack leader",))
        next_state = state.clone()
        next_state.enemy_hp -= attacker.atk
        next_state.my_board[index] = replace(attacker, attacks_left=attacker.attacks_left - 1)
        return InterpreterResult(next_state)

    def evolve(self, state: LethalState, attacker_uid: int, *, super_evolve: bool = False, target_uid: Any = None) -> InterpreterResult:
        index = next((i for i, f in enumerate(state.my_board) if f.unique_id == attacker_uid), None)
        if index is None:
            return InterpreterResult(state, warnings=(f"follower {attacker_uid} not found",))
        follower = state.my_board[index]
        if follower.is_evolved:
            return InterpreterResult(state, warnings=(f"{follower.name} already evolved",))
        points = state.sep if super_evolve else state.ep
        if points <= 0:
            return InterpreterResult(state, warnings=("no evolve points",))
        next_state = state.clone()
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
            can_attack_leader=follower.has_storm,
            can_attack_field=True,
        )
        next_state.evolved_allies_this_turn += 1
        next_state.evolved_allies_this_match += 1
        rule = self.rules.get(follower.card_id, {})
        result = self._resolve_abilities(next_state, rule, "normal", trigger, source_uid=follower.unique_id, target_uid=target_uid)
        unsupported = set(result.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{follower.card_id}")
        return InterpreterResult(result.state, tuple(sorted(unsupported)), result.warnings)

    def engage(self, state: LethalState, source_uid: int, target_uid: Any = None) -> InterpreterResult:
        """Activate an allied amulet's Engage ability.

        Amulets are currently carried by the unified board model; countdown
        changes are applied when the rule exposes a deterministic delta.
        """
        source = next((f for f in state.my_board if f.unique_id == source_uid), None)
        if source is None:
            return InterpreterResult(state, warnings=(f"engage source {source_uid} not found",))
        rule = self.rules.get(source.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, Mapping) and m.get("kind") == "normal"), {})
        engage_cost = next((a.get("cost", 0) for a in selected.get("abilities", ()) if isinstance(a, Mapping) and a.get("trigger") == "on_engage"), 0)
        if state.pp < engage_cost:
            return InterpreterResult(state, warnings=(f"insufficient PP for Engage {source.name}",))
        working = state.clone()
        working.pp -= int(engage_cost or 0)
        result = self._resolve_abilities(working, rule, "normal", "on_engage", source_uid=source_uid, target_uid=target_uid)
        unsupported = set(result.unsupported_ops)
        if rule.get("support") in ("partial", "unsupported"):
            unsupported.add(f"{rule.get('support')}_rule:{source.card_id}")
        return InterpreterResult(result.state, tuple(sorted(unsupported)), result.warnings)

    def _resolve_abilities(self, state: LethalState, rule: Mapping[str, Any], mode: str, trigger: str, *, source_uid: int, target_uid: Any = None) -> InterpreterResult:
        modes = rule.get("modes", ()) if isinstance(rule, Mapping) else ()
        selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == mode), None)
        if selected is None and mode == "normal":
            selected = next((item for item in modes if isinstance(item, Mapping) and item.get("kind") == "normal"), None)
        abilities = selected.get("abilities", ()) if isinstance(selected, Mapping) else ()
        unsupported: set[str] = set()
        warnings: list[str] = []
        current = state
        for ability in abilities:
            if not isinstance(ability, Mapping) or ability.get("trigger") != trigger:
                continue
            condition = ability.get("condition")
            if condition is not None:
                condition_result = self._condition_met(current, condition)
                if condition_result is None:
                    unsupported.add("conditional")
                    continue
                if not condition_result:
                    continue
            current, ops, warns = self._effects(current, ability.get("effects", ()), source_uid, target_uid)
            unsupported.update(ops)
            warnings.extend(warns)
        return InterpreterResult(current, tuple(sorted(unsupported)), tuple(warnings))

    def _resolve_board_trigger(self, state: LethalState, trigger: str) -> InterpreterResult:
        """Resolve a trigger for each allied follower currently on board."""
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        # Snapshot IDs because an effect may remove or add followers.
        source_ids = [f.unique_id for f in current.my_board]
        for source_uid in source_ids:
            if not any(f.unique_id == source_uid for f in current.my_board):
                continue
            follower = next(f for f in current.my_board if f.unique_id == source_uid)
            rule = self.rules.get(follower.card_id, {})
            result = self._resolve_abilities(current, rule, "normal", trigger, source_uid=source_uid)
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
        values = {
            "pp": state.pp, "max_pp": state.max_pp, "rally": state.rally,
            "play_count": state.play_count, "cemetery": state.cemetery,
            "faith": state.faith,
            "awakening": state.is_awakening, "is_awakening": state.is_awakening,
            "ally_board_count": len(state.my_board), "enemy_board_count": len(state.enemy_board),
            "board_count": len(state.my_board) + len(state.enemy_board),
            "crest_count": len(state.active_crests),
            "evolved_allies_this_turn": state.evolved_allies_this_turn,
            "evolved_allies_this_match": state.evolved_allies_this_match,
        }
        if condition.get("state") not in values:
            return None
        left, right, cmp = values[condition["state"]], condition.get("value"), condition.get("cmp")
        comparisons = {"eq": lambda: left == right, "ne": lambda: left != right, "gte": lambda: left >= right, "gt": lambda: left > right, "lte": lambda: left <= right, "lt": lambda: left < right}
        try:
            return comparisons[cmp]() if cmp in comparisons else None
        except TypeError:
            return None

    def _effects(self, state: LethalState, effects: Any, source_uid: int, target_uid: Any = None) -> tuple[LethalState, set[str], list[str]]:
        current = state
        unsupported: set[str] = set()
        warnings: list[str] = []
        for effect in effects if isinstance(effects, (list, tuple)) else ():
            if not isinstance(effect, Mapping):
                continue
            op = effect.get("op")
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
            if selection == "random":
                unsupported.add("random_target")
            if op == "damage" and scope == "enemy_leader":
                current = current.clone()
                current.enemy_hp -= int(amount)
            elif op == "damage" and scope == "enemy_follower":
                current = current.clone()
                if isinstance(target_uid, Mapping):
                    allocations = target_uid
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        damage = int(allocations.get(follower.unique_id, 0))
                        if damage <= 0:
                            continue
                        remaining_hp = follower.hp - damage
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            current.enemy_board.pop(i)
                elif selection == "all":
                    for i, follower in list(enumerate(current.enemy_board))[::-1]:
                        remaining_hp = follower.hp - int(amount)
                        if remaining_hp > 0:
                            current.enemy_board[i] = replace(follower, hp=remaining_hp)
                        else:
                            current.enemy_board.pop(i)
                elif target_uid is None:
                    warnings.append("damage enemy_follower requires target_uid")
                else:
                    for i, follower in enumerate(current.enemy_board):
                        if follower.unique_id == target_uid:
                            remaining_hp = follower.hp - int(amount)
                            if remaining_hp > 0:
                                current.enemy_board[i] = replace(follower, hp=remaining_hp)
                            else:
                                current.enemy_board.pop(i)
                            break
            elif op == "recover_pp":
                current = current.clone()
                current.pp = min(current.max_pp, current.pp + int(amount))
            elif op == "modify_counter":
                # Countdown counters are stored on the unified board follower
                # model for deterministic Engage simulations.
                if scope not in ("self", None):
                    unsupported.add("modify_counter_target")
                    continue
                delta = int(effect.get("delta", amount) or 0)
                current = current.clone()
                for i, follower in enumerate(current.my_board):
                    if follower.unique_id == source_uid and follower.countdown is not None:
                        current.my_board[i] = replace(follower, countdown=max(0, follower.countdown - delta))
                        break
                else:
                    unsupported.add("modify_counter_state")
            elif op == "modify_resource":
                resource = effect.get("resource")
                current = current.clone()
                if resource == "faith":
                    current.faith += int(amount)
                elif resource == "cemetery":
                    current.cemetery = max(0, current.cemetery + int(amount))
                else:
                    unsupported.add(f"resource:{resource}")
            elif op == "consume_resource":
                resource = effect.get("resource")
                current = current.clone()
                if resource == "cemetery":
                    current.cemetery = max(0, current.cemetery - int(amount))
                elif resource == "faith":
                    current.faith = max(0, current.faith - int(amount))
                else:
                    unsupported.add(f"resource:{resource}")
            elif op == "buff":
                current = current.clone()
                attack = self._resolve_value(current, effect.get("attack", amount), source_uid)
                life = self._resolve_value(current, effect.get("life", 0), source_uid)
                if attack is None or life is None:
                    unsupported.add("variable_amount")
                attack = int(attack or 0)
                life = int(life or 0)
                if scope in ("self", "ally_follower"):
                    targets = [i for i, f in enumerate(current.my_board) if (scope == "self" and f.unique_id == source_uid) or (scope == "ally_follower" and (selection == "all" or f.unique_id == target_uid))]
                    for i in targets:
                        current.my_board[i] = replace(current.my_board[i], atk=current.my_board[i].atk + attack, hp=current.my_board[i].hp + life)
                elif scope == "enemy_follower":
                    targets = [i for i, f in enumerate(current.enemy_board) if selection == "all" or f.unique_id == target_uid]
                    for i in list(targets)[::-1]:
                        follower = current.enemy_board[i]
                        new_hp = follower.hp + life
                        if new_hp <= 0:
                            current.enemy_board.pop(i)
                        else:
                            current.enemy_board[i] = replace(follower, atk=max(0, follower.atk + attack), hp=new_hp)
                else:
                    unsupported.add("buff_target")
            elif op == "grant_keyword":
                current = current.clone()
                keyword = effect.get("keyword")
                for i, follower in enumerate(current.my_board):
                    if scope == "self" and follower.unique_id != source_uid:
                        continue
                    if scope not in ("self", "ally_follower"):
                        continue
                    current.my_board[i] = replace(follower, has_storm=follower.has_storm or keyword == "storm", has_rush=follower.has_rush or keyword == "rush", is_ward=follower.is_ward or keyword == "ward", can_attack_leader=follower.can_attack_leader or keyword == "storm", can_attack_field=follower.can_attack_field or keyword in ("storm", "rush"))
            elif op == "gain_status":
                current = current.clone()
                keyword = effect.get("status") or effect.get("keyword")
                for i, follower in enumerate(current.my_board):
                    if follower.unique_id != source_uid:
                        continue
                    current.my_board[i] = replace(
                        follower,
                        has_storm=follower.has_storm or keyword == "storm",
                        has_rush=follower.has_rush or keyword == "rush",
                        can_attack_leader=follower.can_attack_leader or keyword == "storm",
                        can_attack_field=True,
                    )
            elif op == "destroy" and scope == "enemy_follower":
                current = current.clone()
                if selection == "all":
                    current.enemy_board = []
                elif target_uid is None:
                    warnings.append("destroy enemy_follower requires target_uid")
                else:
                    current.enemy_board = [f for f in current.enemy_board if f.unique_id != target_uid]
            elif op == "set_attacks":
                current = current.clone()
                for i, follower in enumerate(current.my_board):
                    if follower.unique_id == source_uid:
                        current.my_board[i] = replace(follower, attacks_left=int(amount))
            elif op == "sequence":
                current, nested_ops, nested_warns = self._effects(current, effect.get("effects", ()), source_uid, target_uid)
                unsupported.update(nested_ops)
                warnings.extend(nested_warns)
            elif op == "conditional":
                verdict = self._condition_met(current, effect.get("condition", {}))
                if verdict is None:
                    unsupported.add("conditional")
                else:
                    branch = effect.get("effects", ()) if verdict else effect.get("else_effects", ())
                    current, nested_ops, nested_warns = self._effects(current, branch, source_uid, target_uid)
                    unsupported.update(nested_ops)
                    warnings.extend(nested_warns)
            elif op == "mode_choice":
                unsupported.add("mode_choice")
            else:
                unsupported.add(str(op))
        return current, unsupported, warnings

    @staticmethod
    def _resolve_value(state: LethalState, value: Any, source_uid: int) -> int | None:
        if isinstance(value, (int, float)):
            return int(value)
        if not isinstance(value, str):
            return None
        if value == "var:hand_count":
            return len(state.hand)
        if value == "var:cemetery":
            return state.cemetery
        if value == "var:crest_count":
            return len(state.active_crests)
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


__all__ = ["EventInterpreter", "InterpreterResult"]
