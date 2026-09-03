"""Hydrate a public Tracker snapshot into a conservative SWB-RL shadow state.

The Tracker and SWB-RL intentionally expose different boundaries.  Tracker
has the authoritative *current* legal actions and live entity values, while
SWB-RL owns card resolution and RuleBook semantics.  This module joins those
two views without pretending that the public snapshot contains a complete
match history:

* visible hand/board/resources are copied into ``GameState``;
* the root command list is overlaid from Tracker ``legal_actions``;
* all later transitions are resolved by SWB-RL's ``GameEngine``;
* missing deck order, hidden hand, unknown card ids and unsupported resource
  payloads are retained as explicit ``warnings`` and ``hidden_state_unknown``.

The resulting ``TrackerShadowEngine`` implements the small protocol consumed
by :mod:`swb_rl_backend` (``legal_commands``, ``clone`` and ``apply``).  It is
not a replacement for a real SWB-RL match snapshot.  Callers must keep a
shadow result marked ``INCOMPLETE`` whenever the build reports incomplete
hidden state.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


_VALID_CLASS_IDS = frozenset(range(1, 8))
_TRACKER_TYPE_TO_NAME = {
    1: "随从",
    2: "护符",
    3: "护符",
    4: "法术",
}
_KEYWORD_TO_SWB = {
    "storm": "疾驰",
    "rush": "突进",
    "ward": "守护",
    "bane": "必杀",
    "drain": "吸血",
    "ambush": "潜行",
    "intimidate": "威慑",
    "aura": "灵气",
    "barrier": "屏障",
}
_FLAG_TO_KEYWORD = {
    "has_storm": "疾驰",
    "has_rush": "突进",
    "has_guard": "守护",
    "has_ward": "守护",
    "has_bane": "必杀",
    # The Tracker's BattleFieldCard calls the same keyword ``has_killer``.
    "has_killer": "必杀",
    "has_drain": "吸血",
    "has_sneak": "潜行",
    "has_ambush": "潜行",
}


@dataclass(frozen=True)
class ShadowStateBuildResult:
    """Outcome of constructing a shadow engine from a Tracker snapshot."""

    engine: "TrackerShadowEngine | None"
    source_root: Path | None
    warnings: tuple[str, ...] = ()
    unsupported_card_ids: tuple[int, ...] = ()
    hidden_state_unknown: bool = True
    card_order_unknown: bool = True

    @property
    def complete(self) -> bool:
        """Whether the engine is safe to label as a complete match state."""

        return self.engine is not None and not self.hidden_state_unknown and not self.card_order_unknown and not self.warnings


class TrackerShadowEngine:
    """A thin SWB-RL ``GameEngine`` wrapper with Tracker root legality.

    ``_root_commands`` is used exactly once: the first ``legal_commands`` call
    returns Tracker's authoritative list, and applying one of those commands
    releases the wrapper to SWB-RL's normal legality calculation.  This keeps
    stale printed keywords from resurrecting an attack that Tracker rejected,
    while still letting hypothetical branches use the real RuleBook.
    """

    def __init__(
        self,
        core: Any,
        root_commands: Sequence[Any],
        *,
        warnings: Sequence[str] = (),
        hidden_state_unknown: bool = True,
        card_order_unknown: bool = True,
        root_override_active: bool = True,
    ) -> None:
        self._core = core
        self._root_commands = tuple(root_commands)
        self._root_override_active = bool(root_override_active)
        self.warnings = tuple(dict.fromkeys(str(item) for item in warnings if str(item)))
        self.hidden_state_unknown = bool(hidden_state_unknown)
        self.card_order_unknown = bool(card_order_unknown)

    @property
    def players(self):
        return self._core.players

    @property
    def state(self):
        return self._core.state

    @property
    def current_player(self) -> int:
        return int(self._core.current_player)

    @property
    def turn(self) -> int:
        return int(self._core.turn)

    @property
    def terminated(self) -> bool:
        return bool(self._core.terminated)

    @property
    def winner(self):
        return self._core.winner

    @property
    def state_version(self) -> int:
        return int(getattr(self._core, "state_version", 0))

    @property
    def core(self):
        """Expose the hydrated core for diagnostics, never for mutation."""

        return self._core

    def legal_commands(self) -> list[Any]:
        if self._root_override_active:
            return list(self._root_commands)
        return list(self._core.legal_commands())

    def apply(self, command: Any) -> Any:
        if self._root_override_active and command not in self._root_commands:
            raise ValueError("command is not present in Tracker legal_actions")
        transition = self._core.apply(command)
        self._root_override_active = False
        return transition

    def clone(self) -> "TrackerShadowEngine":
        return TrackerShadowEngine(
            self._core.clone(),
            self._root_commands,
            warnings=self.warnings,
            hidden_state_unknown=self.hidden_state_unknown,
            card_order_unknown=self.card_order_unknown,
            root_override_active=self._root_override_active,
        )

    def __getattr__(self, name: str) -> Any:
        # Keep the wrapper compatible with optional diagnostics in the native
        # backend while keeping all mutation routed through ``apply``.
        return getattr(self._core, name)


def _unwrap_snapshot(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    if "root" not in snapshot:
        nested = snapshot.get("snapshot")
        if isinstance(nested, Mapping):
            return nested
    return snapshot


def _int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    return default


def _raw_players(snapshot: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    root = snapshot.get("root")
    players = root.get("players") if isinstance(root, Mapping) else ()
    if not isinstance(players, (list, tuple)):
        return {}, {}
    mine = players[0] if len(players) > 0 and isinstance(players[0], Mapping) else {}
    enemy = players[1] if len(players) > 1 and isinstance(players[1], Mapping) else {}
    return mine, enemy


def _discover_root(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for name in ("SHADOWVERSE_SWB_RL_ROOT", "SWB_RL_ROOT"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value))
    # D:/Github/LethalCalculator/shadow_state_adapter.py -> D:/Github/SWB-RL
    candidates.append(Path(__file__).resolve().parents[1] / "SWB-RL")
    candidates.append(Path.cwd() / "SWB-RL")
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        if (candidate / "swb").is_dir() and (candidate / "data" / "cards.sqlite3").is_file():
            return candidate
    return None


def _load_swb(root: Path) -> dict[str, Any]:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return {
        "CardRepository": importlib.import_module("swb.db.repository").CardRepository,
        "GameEngine": importlib.import_module("swb.engine.resolution").GameEngine,
        "GameConfig": importlib.import_module("swb.engine.resolution").GameConfig,
        "commands": importlib.import_module("swb.engine.commands"),
        "state": importlib.import_module("swb.engine.state"),
        "origin": importlib.import_module("swb.engine.origin"),
        "deck": importlib.import_module("swb.engine.deck"),
        "card_rules": importlib.import_module("swb.engine.card_rules"),
        "faith": importlib.import_module("swb.engine.faith"),
        "emblem": importlib.import_module("swb.engine.emblem"),
    }


def _resolve_definition(repository: Any, card_id: int, cache: dict[int, Any], missing: set[int]) -> Any | None:
    if card_id in cache:
        return cache[card_id]
    try:
        definition = repository.get(int(card_id))
    except Exception:
        # Tracker field cards can expose an evolution variant while SWB-RL's
        # database stores the base definition.  The public adapter normally
        # normalizes this, but this fallback keeps compact snapshots usable.
        definition = None
        if card_id > 1:
            try:
                definition = repository.get(int(card_id) - 1)
            except Exception:
                definition = None
    if definition is None:
        missing.add(int(card_id))
        return None
    cache[card_id] = definition
    return definition


def _tracker_card_type(raw: Mapping[str, Any], definition: Any) -> str:
    numeric = _int(raw.get("card_type"))
    if numeric in _TRACKER_TYPE_TO_NAME:
        return _TRACKER_TYPE_TO_NAME[int(numeric)]
    return str(getattr(definition, "card_type", "随从"))


def _as_modes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("kind", item.get("mode", ""))
        text = str(item).strip().casefold()
        if text and text not in result:
            result.append(text)
    return tuple(dict.fromkeys(result))


def _legal_ids(legal: Mapping[str, Any], *keys: str) -> tuple[int, ...]:
    values: Any = None
    for key in keys:
        if key in legal:
            values = legal.get(key)
            break
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(int(item) for item in values if _int(item) is not None))


def _root_commands(adapted: Any, core: Any, raw_mine: Mapping[str, Any]) -> tuple[Any, ...]:
    """Convert Tracker legal-action projections to SWB-RL commands."""

    commands_module = importlib.import_module("swb.engine.commands")
    player_index = 0
    state = adapted.state
    legal = adapted.legal_actions if isinstance(adapted.legal_actions, Mapping) else {}
    hand = tuple(getattr(state, "hand", ()) or ())
    board = tuple(getattr(state, "my_board", ()) or ())
    enemy_board = tuple(getattr(state, "enemy_board", ()) or ())
    hand_indexes = {int(card.unique_id): index for index, card in enumerate(hand) if _int(card.unique_id) is not None}
    board_ids = {int(entity.unique_id) for entity in board if _int(entity.unique_id) is not None}
    enemy_ids = {int(entity.unique_id) for entity in enemy_board if _int(entity.unique_id) is not None}
    leader_uid = _int(getattr(state, "enemy_leader_uid", None))
    # ``can_play_cards_with_extra_pp`` is a *conditional* projection: before
    # Extra PP is opened those cards must not be sent as ordinary PlayCard
    # commands, otherwise the shadow engine would accept an action Tracker
    # only permits after a separate UseExtraPP step.  Once the live snapshot
    # says Extra PP is already open/used, both lists are executable normally.
    can_play = _legal_ids(legal, "can_play_cards")
    can_play_extra = _legal_ids(legal, "can_play_cards_with_extra_pp")
    extra_open = bool(raw_mine.get("is_used_extra_pp_this_turn", False)) or (
        (_int(raw_mine.get("open_extra_pp_state"), 0) or 0) > 0
    )
    if extra_open:
        can_play = tuple(dict.fromkeys(can_play + can_play_extra))
    can_enhance = _legal_ids(legal, "can_enhance_play_cards")
    can_accelerate = _legal_ids(legal, "can_accelerate_play_cards")
    can_crystallize = _legal_ids(legal, "can_crystal_play_cards", "can_crystallize_play_cards")
    can_fusion = _legal_ids(legal, "can_fusion_cards")
    legal_modes = getattr(state, "legal_modes", {}) or {}
    result: list[Any] = [commands_module.EndTurn(player_index)]

    mode_sets: dict[int, set[str]] = {}
    for uid in can_play:
        mode_sets.setdefault(uid, set()).add("normal")
    for uid in can_enhance:
        mode_sets.setdefault(uid, set()).add("enhance")
    for uid in can_accelerate:
        mode_sets.setdefault(uid, set()).add("accelerate")
    for uid in can_crystallize:
        mode_sets.setdefault(uid, set()).add("crystallize")
    for uid in can_fusion:
        # Fusion is a separate command, not a PlayCard mode.
        if uid in hand_indexes:
            result.append(commands_module.BeginFusion(player_index, uid))
    mode_order = ("normal", "enhance", "accelerate", "crystallize")
    for uid, modes in sorted(mode_sets.items(), key=lambda item: hand_indexes.get(item[0], 10_000)):
        if uid not in hand_indexes:
            continue
        advertised = set(_as_modes(legal_modes.get(uid, ())))
        if advertised:
            modes &= advertised
        for mode in mode_order:
            if mode in modes:
                result.append(commands_module.PlayCard(player_index, hand_indexes[uid], mode))

    # Tracker does not expose a dedicated UseExtraPP list.  A card that is
    # legal only with Extra PP is sufficient evidence that the action is
    # available; ``extra_pp`` also covers the explicit UI button.
    extra_available = (
        bool(_legal_ids(legal, "can_play_cards_with_extra_pp"))
        or bool(_legal_ids(legal, "can_activation_field_cards_with_extra_pp"))
        or (_int(raw_mine.get("extra_pp"), 0) or 0) > 0
        or (_int(raw_mine.get("preparation_extra_pp"), 0) or 0) > 0
    )
    if extra_available and not extra_open:
        result.append(commands_module.UseExtraPP(player_index))

    can_leader = set(_legal_ids(legal, "can_attack_leader_cards"))
    can_field = set(_legal_ids(legal, "can_attack_field_cards"))
    target_map = getattr(state, "legal_attack_targets", {}) or {}
    for uid in sorted(can_leader):
        if uid in board_ids:
            result.append(commands_module.Attack(player_index, uid, None))
    for uid in sorted(can_field | set(int(item) for item in target_map if _int(item) is not None)):
        if uid not in board_ids:
            continue
        for target in target_map.get(uid, ()):
            target_id = _int(target)
            if target_id is None:
                continue
            if leader_uid is not None and target_id == leader_uid:
                # Tracker sometimes exposes the leader in AttackTargets but
                # omits the parallel can_attack_leader list.  Preserve the
                # authoritative target as SWB-RL's None leader sentinel.
                result.append(commands_module.Attack(player_index, uid, None))
            elif target_id in enemy_ids:
                result.append(commands_module.Attack(player_index, uid, target_id))

    board_by_id = {
        int(entity.unique_id): entity
        for entity in board
        if _int(getattr(entity, "unique_id", None)) is not None
    }
    for uid in _legal_ids(legal, "can_evolve_cards"):
        entity = board_by_id.get(uid)
        if uid in board_ids and not bool(
            getattr(entity, "evolved", False) or getattr(entity, "is_evolved", False)
        ):
            result.append(commands_module.Evolve(player_index, uid))
    for uid in _legal_ids(legal, "can_super_evolve_cards", "can_super_evolve_with_skill_cards"):
        entity = board_by_id.get(uid)
        if uid in board_ids and not bool(
            getattr(entity, "evolved", False) or getattr(entity, "is_evolved", False)
        ):
            result.append(commands_module.SuperEvolve(player_index, uid))
    can_activate = _legal_ids(legal, "can_activation_field_cards")
    can_activate_extra = _legal_ids(legal, "can_activation_field_cards_with_extra_pp")
    if extra_open:
        can_activate = tuple(dict.fromkeys(can_activate + can_activate_extra))
    for uid in can_activate:
        if uid in board_ids:
            result.append(commands_module.ActivateAmulet(player_index, uid))

    # Special-action/fusion target semantics are not represented by the
    # current public command adapter.  Keep the root executable but expose the
    # gap in the build warning instead of inventing a command.  Commands are
    # frozen dataclasses, so de-duplicate the leader target that may appear in
    # both ``can_attack_leader_cards`` and ``AttackTargets``.
    return tuple(dict.fromkeys(result))


def _make_hand_card(
    item: Any,
    definition: Any,
    *,
    state_module: Any,
    origin_module: Any,
    rulebook: Any,
    warnings: list[str],
) -> Any:
    hand_card = state_module.HandCard(
        definition=definition,
        entity_id=int(item.unique_id),
        origin=origin_module.CardOrigin.DECK,
        spellboost_count=max(0, int(getattr(item, "spell_boost_count", 0) or 0)),
    )
    current_cost = max(0, int(getattr(item, "cost", definition.cost) or 0))
    reduction = int(rulebook.spellboost_cost_reduction(definition.card_id) or 0)
    # ``current_cost`` already includes the visible Spellboost discount.  Set
    # the pre-Spellboost cost so GameEngine's property does not double-apply it.
    desired_before_boost = current_cost + hand_card.spellboost_count * reduction
    if desired_before_boost != int(definition.cost):
        hand_card.cost_modifiers.append(
            state_module.CostModifier(
                modifier_id=10_000_000 + int(item.unique_id),
                mode="set",
                amount=desired_before_boost,
                duration="permanent",
            )
        )
    if getattr(item, "variable_x", 0) or getattr(item, "supplement_info", ()):
        warnings.append(f"hand card {item.card_id}: variable/supplement fields are advisory only")
    if str(getattr(definition, "card_type", "")) == "随从":
        for value in getattr(item, "statuses", ()) or ():
            keyword = _KEYWORD_TO_SWB.get(str(value).casefold())
            if keyword:
                try:
                    hand_card.add_keyword(keyword)
                except (TypeError, ValueError):
                    warnings.append(f"hand card {item.card_id}: unsupported keyword {value}")
    return hand_card


def _make_board_entity(
    item: Any,
    raw: Mapping[str, Any],
    definition: Any,
    *,
    state_module: Any,
    origin_module: Any,
    current_turn: int,
    enemy_side: bool,
    warnings: list[str],
) -> Any:
    card_type = _tracker_card_type(raw, definition)
    uid = int(item.unique_id)
    if card_type in {"护符", "结晶护符"} or str(getattr(definition, "card_type", "")) == "护符":
        countdown = getattr(item, "countdown", None)
        countdown_value = None if countdown is None or int(countdown) < 0 else int(countdown)
        raw_sigil = bool(raw.get("is_earth_sigil", False))
        sigil_count = int(raw.get("stack", 1) or 1) if raw_sigil else 0
        return state_module.Amulet(
            definition=definition,
            entity_id=uid,
            origin=origin_module.CardOrigin.DECK,
            countdown=countdown_value,
            play_mode_id=(str(raw.get("play_mode_id")) if raw.get("play_mode_id") else None),
            earth_sigil_count=max(0, sigil_count),
            entered_turn=current_turn,
        )

    unit = state_module.Unit.summon(
        definition,
        entity_id=uid,
        origin=origin_module.CardOrigin.DECK,
    )
    current_attack = max(0, int(getattr(item, "atk", definition.attack or 0) or 0))
    current_health = max(0, int(getattr(item, "hp", definition.life or 1) or 0))
    raw_max_health = _int(raw.get("max_life"), None)
    max_health = max(current_health, raw_max_health or current_health or 1)
    unit.base_attack = current_attack
    unit.base_health = max_health
    unit.attack = current_attack
    unit.max_health = max_health
    unit.health = current_health
    unit.evolved = bool(getattr(item, "is_evolved", False))
    unit.super_evolved = bool(getattr(item, "is_super_evolved", False))
    unit.super_evolved_turn = current_turn if unit.super_evolved else None
    unit.summoned_this_turn = False
    unit.attacks_remaining = max(0, int(getattr(item, "attacks_left", 0) or 0))

    if bool(getattr(item, "abilities_removed", False)):
        unit.remove_all_abilities()
    else:
        for value in getattr(item, "statuses", ()) or ():
            keyword = _KEYWORD_TO_SWB.get(str(value).casefold())
            if not keyword or keyword in unit.effective_keywords:
                continue
            try:
                unit.add_keyword(keyword)
            except (TypeError, ValueError):
                warnings.append(f"field card {item.card_id}: unsupported keyword {value}")
        for flag, keyword in _FLAG_TO_KEYWORD.items():
            if flag not in raw:
                continue
            try:
                if raw.get(flag) is True and not unit.has_keyword(keyword):
                    unit.add_keyword(keyword)
                elif raw.get(flag) is False and unit.has_keyword(keyword):
                    unit.remove_keyword(keyword)
            except (TypeError, ValueError):
                warnings.append(f"field card {item.card_id}: unsupported keyword {keyword}")

    if bool(raw.get("has_cant_destroy", False)):
        # BattleFieldCard exposes the public wording as ``Can't be
        # destroyed``; SWB-RL models the same effect as destroy immunity.
        unit.effect_destroy_immunity = True
    if bool(raw.get("has_cant_be_attacked", False)):
        warnings.append(f"field card {item.card_id}: cannot-be-attacked restriction is root-only")
    unit.can_attack = bool(getattr(item, "can_attack_leader", False) or getattr(item, "can_attack_field", False)) and unit.attacks_remaining > 0
    unit.rush_only = bool(getattr(item, "has_rush", False) and not getattr(item, "has_storm", False))
    if bool(raw.get("has_cant_attack", False)):
        try:
            unit.add_attack_restriction(state_module.AttackRestriction.CANNOT_ATTACK, duration="permanent")
        except (TypeError, ValueError):
            warnings.append(f"field card {item.card_id}: cannot-attack restriction unavailable")
    if bool(raw.get("has_cant_select", False)):
        try:
            unit.add_targeting_restriction(
                state_module.TargetingRestriction.CANNOT_BE_TARGETED_BY_ENEMY_EFFECTS,
                duration="permanent",
            )
        except (TypeError, ValueError):
            warnings.append(f"field card {item.card_id}: targeting restriction unavailable")
    if getattr(item, "last_words", ()) or raw.get("has_last_word"):
        warnings.append(f"field card {item.card_id}: Last Words payload is not hydrated from public text")
    if getattr(item, "buff", None):
        warnings.append(f"field card {item.card_id}: buff sources are preserved as final stats only")
    return unit


def _make_faiths(
    instances: Sequence[Any],
    *,
    rulebook: Any,
    faith_module: Any,
    warnings: list[str],
    controller: int,
) -> list[Any]:
    result: list[Any] = []
    for index, item in enumerate(instances):
        if not isinstance(item, Mapping):
            continue
        source_card_id = _int(item.get("source_card_id", item.get("card_id")))
        if source_card_id is None:
            warnings.append("Faith instance without source_card_id")
            continue
        definition = rulebook.faith_for(source_card_id)
        if definition is None:
            warnings.append(f"Faith {source_card_id}: no SWB-RL definition")
            continue
        entity_id = _int(item.get("unique_id"), -(index + 1)) or -(index + 1)
        instance = faith_module.FaithInstance(
            definition=definition,
            entity_id=entity_id,
            controller=controller,
            created_sequence=index + 1,
            value=max(0, int(item.get("value", item.get("faith_value", 0)) or 0)),
            mode_selection_bonus=max(0, int(item.get("mode_limit_bonus", 0) or 0)),
        )
        if item.get("abilities"):
            warnings.append(f"Faith {source_card_id}: granted abilities are not reconstructed from JSON")
        result.append(instance)
    return result


def _make_emblems(
    instances: Sequence[Any],
    *,
    rulebook: Any,
    state_module: Any,
    warnings: list[str],
    controller: int,
) -> list[Any]:
    result: list[Any] = []
    for index, item in enumerate(instances):
        if not isinstance(item, Mapping):
            continue
        source_card_id = _int(item.get("card_id"))
        if source_card_id is None:
            continue
        matches = [
            (emblem_id, definition)
            for emblem_id, definition in getattr(rulebook, "_emblem_defs", {}).items()
            if int(getattr(definition, "source_card_id", 0)) == source_card_id
        ]
        if not matches:
            warnings.append(f"Crest {source_card_id}: no SWB-RL emblem definition")
            continue
        emblem_id, definition = matches[0]
        raw_countdown = _int(item.get("countdown"), -1)
        countdown = None if raw_countdown is None or raw_countdown < 0 else raw_countdown
        entity_id = _int(item.get("unique_id"), -(index + 1)) or -(index + 1)
        result.append(
            state_module.EmblemInstance(
                emblem_id=emblem_id,
                definition=definition,
                entity_id=entity_id,
                controller=controller,
                created_sequence=index + 1,
                countdown=countdown,
            )
        )
        if item.get("abilities"):
            warnings.append(f"Crest {source_card_id}: granted abilities are not reconstructed from JSON")
    return result


def _make_leader_damage_modifiers(
    raw: Mapping[str, Any],
    *,
    state_module: Any,
    controller: int,
) -> list[Any]:
    """Project public leader damage buffs into SWB-RL's additive ledger.

    Tracker normally exposes the aggregate ``PlayerBuff`` (``damage_cut`` /
    ``increase_damage``).  When a richer modifier list is available, retain
    its individual entries; otherwise synthesize one permanent additive
    modifier.  This is intentionally a projection of the current value, not a
    claim about the original source/listener lifetime.
    """

    result: list[Any] = []
    raw_modifiers = raw.get("leader_damage_modifiers")
    if isinstance(raw_modifiers, (list, tuple)):
        for index, item in enumerate(raw_modifiers):
            if not isinstance(item, Mapping):
                continue
            amount = _int(item.get("amount"), None)
            if amount is None or amount == 0:
                continue
            result.append(
                state_module.LeaderDamageModifier(
                    modifier_id=_int(item.get("modifier_id"), -(index + 1)) or -(index + 1),
                    amount=amount,
                    duration=str(item.get("duration", "permanent")),
                    expires_for_player=_int(item.get("expires_for_player"), None),
                    source_controller=controller if _int(item.get("source_entity_id"), None) is not None else None,
                    source_entity_id=_int(item.get("source_entity_id"), None),
                    source_card_id=_int(item.get("source_card_id"), None),
                    mode=str(item.get("mode", "additive")),
                )
            )
    if result:
        return result

    buff = raw.get("buff", raw.get("leader_buff"))
    if not isinstance(buff, Mapping):
        return result
    increase = _int(buff.get("increase_damage"), 0) or 0
    damage_cut = _int(buff.get("damage_cut"), 0) or 0
    amount = int(increase) - int(damage_cut)
    if amount:
        result.append(
            state_module.LeaderDamageModifier(
                modifier_id=-(controller + 1),
                amount=amount,
                duration="permanent",
                mode="additive",
            )
        )
    return result


def _ledger_cards(snapshot: Mapping[str, Any], repository: Any, resolve: Any, warnings: list[str]) -> tuple[list[Any], bool]:
    """Return known remaining cards and whether their order is exact."""

    mine, _ = _raw_players(snapshot)
    explicit = mine.get("deck_order", mine.get("deck_cards"))
    if isinstance(explicit, (list, tuple)):
        cards: list[Any] = []
        exact = True
        for raw_id in explicit:
            cid = _int(raw_id)
            if cid is None:
                continue
            definition = resolve(cid)
            if definition is not None:
                cards.append(definition)
        return cards, exact

    ledger = snapshot.get("deck_ledger")
    rows = ledger.get("rows") if isinstance(ledger, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return [], False
    cards = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cid = _int(row.get("card_id"))
        count = _int(row.get("remaining"), 0) or 0
        if cid is None or count <= 0:
            continue
        definition = resolve(cid)
        if definition is None:
            continue
        cards.extend([definition] * count)
    if cards:
        warnings.append("deck ledger supplies a multiset; remaining deck order is unknown")
    return cards, False


def build_shadow_engine(
    snapshot: Mapping[str, Any],
    adapted: Any,
    *,
    swb_rl_root: str | os.PathLike[str] | None = None,
    seed: int = 0,
) -> ShadowStateBuildResult:
    """Build one hydrated ``TrackerShadowEngine``.

    ``adapted`` should be the result of ``SnapshotAdapter.adapt`` using the
    same catalog/rules as the calling session.  The function does not mutate
    the snapshot or the adapter state.
    """

    if not bool(getattr(adapted, "trusted", False)) or not bool(getattr(adapted, "usable", False)):
        reasons = tuple(getattr(adapted, "trust_reasons", ()) or ())
        return ShadowStateBuildResult(
            engine=None,
            source_root=None,
            warnings=tuple(f"snapshot not usable: {reason}" for reason in reasons) or ("snapshot not usable",),
            hidden_state_unknown=True,
            card_order_unknown=True,
        )

    source_root = _discover_root(swb_rl_root)
    if source_root is None:
        return ShadowStateBuildResult(
            engine=None,
            source_root=None,
            warnings=("SWB-RL root/data/cards.sqlite3 not found",),
            hidden_state_unknown=True,
            card_order_unknown=True,
        )

    snapshot_value = _unwrap_snapshot(snapshot)
    raw_mine, raw_enemy = _raw_players(snapshot_value)
    warnings: list[str] = []
    missing: set[int] = set()
    if any(snapshot_value.get(key) is not None for key in ("pending_choice", "choice_request", "pending_target_choice")):
        warnings.append("pending Tracker choice is not hydrated into SWB-RL ChoiceRequest")
    try:
        swb = _load_swb(source_root)
        repository = swb["CardRepository"](source_root / "data" / "cards.sqlite3")
        rulebook = swb["card_rules"].RuleBook.from_directory(source_root / "data" / "rules")
        all_cards = repository.all_cards()
    except Exception as exc:
        return ShadowStateBuildResult(
            engine=None,
            source_root=source_root,
            warnings=(f"SWB-RL assets unavailable: {type(exc).__name__}: {exc}",),
            hidden_state_unknown=True,
            card_order_unknown=True,
        )

    definitions_cache: dict[int, Any] = {}
    resolve = lambda cid: _resolve_definition(repository, int(cid), definitions_cache, missing)
    class_names = swb["deck"].CLASS_NAMES
    # Normal BattleModel envelopes provide these fields.  Puzzle/root-only
    # snapshots often do not; use a legal fallback but retain the warning.
    self_class = _int(snapshot_value.get("self_class_id"), _int(raw_mine.get("class_id"), None))
    opponent_class = _int(snapshot_value.get("opponent_class_id"), _int(raw_enemy.get("class_id"), self_class))
    if self_class not in _VALID_CLASS_IDS:
        self_class = 1
        warnings.append("self_class_id missing; shadow state uses class 1 fallback")
    if opponent_class not in _VALID_CLASS_IDS:
        opponent_class = self_class
        warnings.append("opponent_class_id missing; shadow state uses ally class fallback")

    # Constructor validation requires two legal 40-card decks.  We replace
    # those lists immediately after construction with the visible remaining
    # counts/order from Tracker.
    eligible = [
        card for card in all_cards
        if bool(getattr(card, "is_collectible", False))
        and int(getattr(card, "class_id", -1)) in {0, self_class}
    ]
    enemy_eligible = [
        card for card in all_cards
        if bool(getattr(card, "is_collectible", False))
        and int(getattr(card, "class_id", -1)) in {0, opponent_class}
    ]
    if not eligible or not enemy_eligible:
        return ShadowStateBuildResult(
            engine=None,
            source_root=source_root,
            warnings=("SWB-RL has no legal placeholder card for the snapshot classes",),
            hidden_state_unknown=True,
            card_order_unknown=True,
        )
    own_placeholder = eligible[0]
    enemy_placeholder = enemy_eligible[0]
    own_constructor_deck = [own_placeholder] * 40
    enemy_constructor_deck = [enemy_placeholder] * 40
    # The regular Tracker payload carries ``is_first_side``.  Compact/test
    # snapshots sometimes omit it; an available Extra PP is strong evidence
    # that the ally is the second player, because SWB-RL grants that resource
    # only to the non-first side.
    raw_extra_pp = (_int(raw_mine.get("extra_pp"), 0) or 0) + (
        _int(raw_mine.get("preparation_extra_pp"), 0) or 0
    )
    if isinstance(raw_mine.get("is_first_side"), bool):
        first_player = 0 if bool(raw_mine.get("is_first_side")) else 1
    else:
        first_player = 1 if raw_extra_pp > 0 else 0
        if raw_extra_pp > 0:
            warnings.append("is_first_side missing; inferred second-player Extra PP")
    try:
        config = swb["GameConfig"](
            starting_player=first_player,
            enable_mulligan=False,
            starting_hand=0,
            retain_text_logs=False,
        )
        core = swb["GameEngine"](
            own_constructor_deck,
            enemy_constructor_deck,
            class_a=self_class,
            class_b=opponent_class,
            seed=int(seed),
            config=config,
            rulebook=rulebook,
            card_resolver=repository.get,
        )
    except Exception as exc:
        return ShadowStateBuildResult(
            engine=None,
            source_root=source_root,
            warnings=(f"failed to construct SWB-RL engine: {type(exc).__name__}: {exc}",),
            hidden_state_unknown=True,
            card_order_unknown=True,
        )

    state_module = swb["state"]
    origin_module = swb["origin"]
    current_turn_value = _int(raw_mine.get("turn"), _int(snapshot_value.get("current_turn"), 1))
    current_turn = max(0, 1 if current_turn_value is None else current_turn_value)
    raw_hand = raw_mine.get("hand", ())
    raw_ally_field = raw_mine.get("field", ())
    raw_enemy_field = raw_enemy.get("field", ())
    raw_by_uid: dict[int, Mapping[str, Any]] = {}
    for collection in (raw_hand, raw_ally_field, raw_enemy_field):
        if isinstance(collection, (list, tuple)):
            for raw in collection:
                if isinstance(raw, Mapping) and _int(raw.get("unique_id")) is not None:
                    raw_by_uid[int(raw["unique_id"])] = raw

    def make_hand_items() -> list[Any]:
        result: list[Any] = []
        for item in getattr(adapted.state, "hand", ()) or ():
            definition = resolve(int(item.card_id))
            if definition is None:
                warnings.append(f"hand card {item.card_id}: absent from SWB-RL catalog")
                continue
            result.append(
                _make_hand_card(
                    item,
                    definition,
                    state_module=state_module,
                    origin_module=origin_module,
                    rulebook=rulebook,
                    warnings=warnings,
                )
            )
        return result

    def make_board_items(items: Sequence[Any], *, enemy_side: bool) -> list[Any]:
        result: list[Any] = []
        for item in items:
            definition = resolve(int(item.card_id))
            if definition is None:
                warnings.append(f"field card {item.card_id}: absent from SWB-RL catalog")
                continue
            raw = raw_by_uid.get(int(item.unique_id), {})
            entity = _make_board_entity(
                item,
                raw,
                definition,
                state_module=state_module,
                origin_module=origin_module,
                current_turn=current_turn,
                enemy_side=enemy_side,
                warnings=warnings,
            )
            if entity is not None:
                result.append(entity)
        return result

    own_deck_cards, own_order_exact = _ledger_cards(snapshot_value, repository, resolve, warnings)
    deck_count = _int(raw_mine.get("deck_count"), None)
    if deck_count is None:
        deck_count = len(own_deck_cards)
        warnings.append("ally deck_count missing; inferred from known cards")
    deck_count = max(0, int(deck_count))
    puzzle_empty = str(snapshot_value.get("battle_mode", "")).casefold() == "puzzle" or bool(snapshot_value.get("shadow_empty_deck", False))
    if puzzle_empty:
        own_deck_cards = []
        deck_count = 0
        own_order_exact = True
        warnings.append("puzzle mode: shadow deck explicitly set to empty")
    elif not own_deck_cards:
        own_deck_cards = [own_placeholder] * deck_count
    elif len(own_deck_cards) < deck_count:
        own_deck_cards.extend([own_placeholder] * (deck_count - len(own_deck_cards)))
    else:
        own_deck_cards = own_deck_cards[:deck_count]

    enemy_deck_count = max(0, _int(raw_enemy.get("deck_count"), 0) or 0)
    enemy_deck_cards = [enemy_placeholder] * enemy_deck_count
    if enemy_deck_count:
        warnings.append("opponent deck identity/order is hidden")

    own_hand = make_hand_items()
    own_board = make_board_items(getattr(adapted.state, "my_board", ()) or (), enemy_side=False)
    enemy_board = make_board_items(getattr(adapted.state, "enemy_board", ()) or (), enemy_side=True)

    def make_player(
        raw: Mapping[str, Any],
        *,
        index: int,
        class_id: int,
        deck: list[Any],
        hand: list[Any],
        board: list[Any],
        faiths: list[Any],
        emblems: list[Any],
        leader_damage_modifiers: list[Any],
    ) -> Any:
        raw_life = _int(raw.get("life"), 20)
        life = max(0, 20 if raw_life is None else raw_life)
        raw_max_life = _int(raw.get("max_life"), life)
        max_life = max(life, life if raw_max_life is None else raw_max_life)
        max_mana = max(0, _int(raw.get("max_pp"), 0) or 0)
        mana = max(0, _int(raw.get("pp"), 0) or 0)
        extra_pp = max(0, _int(raw.get("extra_pp"), 0) or 0)
        preparation = max(0, _int(raw.get("preparation_extra_pp"), 0) or 0)
        extra_open = bool(raw.get("is_used_extra_pp_this_turn", False)) or (
            (_int(raw.get("open_extra_pp_state"), 0) or 0) > 0
        )
        attacked = raw.get("attacked_cards", ())
        attacked_count = len(attacked) if isinstance(attacked, (list, tuple, set)) else 0
        # The regular Tracker payload keeps the authoritative attacked UID
        # list under ``legal_actions``; it is already normalized by
        # SnapshotAdapter.  Prefer that projection for the active player so
        # Crest/"didn't attack" listeners do not lose an attack merely because
        # the per-player root field is omitted.
        if index == 0:
            observed_attacks = getattr(adapted.state, "attacked_card_uids", ()) or ()
            if isinstance(observed_attacks, (list, tuple, set)):
                attacked_count = len(observed_attacks)
        return state_module.PlayerState(
            deck=deck,
            class_id=class_id,
            class_name=class_names.get(class_id, str(class_id)),
            hand=hand,
            hand_entity_ids=[int(card.entity_id) for card in hand],
            board=board,
            health=life,
            max_health=max_life,
            max_mana=max_mana,
            mana=mana,
            extra_pp_available=bool(extra_pp or preparation) and not extra_open,
            extra_pp_uses=1 if bool(raw.get("is_used_extra_pp_this_turn", False)) else 0,
            extra_pp_active_turn=current_turn if extra_open else None,
            extra_pp_pending=(extra_open and not bool(raw.get("is_used_extra_pp_this_turn", False))),
            evolution_points=max(0, _int(raw.get("evolve_points"), 0) or 0),
            super_evolution_points=max(0, _int(raw.get("super_evolve_points"), 0) or 0),
            turns_started=max(0, _int(raw.get("turn"), current_turn) or current_turn),
            evolved_this_turn=bool(raw.get("is_evolved_this_turn", False))
            or bool(getattr(adapted.state, "evolved_this_turn", False)),
            super_evolved_this_turn=bool(raw.get("is_super_evolved_this_turn", False))
            or bool(getattr(adapted.state, "super_evolved_this_turn", False)),
            followers_evolved_this_match=max(0, _int(raw.get("evolve_count"), 0) or 0),
            cards_played_this_turn=max(0, _int(raw.get("play_count"), 0) or 0),
            follower_attacks_this_turn=attacked_count,
            followers_destroyed_this_turn=0,
            cooperation=max(0, _int(raw.get("rally"), 0) or 0),
            shadows=max(0, _int(raw.get("cemetery_count"), 0) or 0),
            leader_barrier_charges=max(0, _int(raw.get("leader_barrier_charges"), 0) or 0),
            leader_damage_modifiers=leader_damage_modifiers,
            faiths=faiths,
            emblems=emblems,
        )

    faith_module = swb["faith"]
    own_faiths = _make_faiths(getattr(adapted.state, "faith_instances", ()) or (), rulebook=rulebook, faith_module=faith_module, warnings=warnings, controller=0)
    enemy_faiths: list[Any] = []
    own_emblems = _make_emblems(getattr(adapted.state, "crest_instances", ()) or (), rulebook=rulebook, state_module=state_module, warnings=warnings, controller=0)
    enemy_emblems = _make_emblems(getattr(adapted.state, "enemy_crest_instances", ()) or (), rulebook=rulebook, state_module=state_module, warnings=warnings, controller=1)

    own_player = make_player(
        raw_mine,
        index=0,
        class_id=self_class,
        deck=own_deck_cards,
        hand=own_hand,
        board=own_board,
        faiths=own_faiths,
        emblems=own_emblems,
        leader_damage_modifiers=_make_leader_damage_modifiers(
            raw_mine, state_module=state_module, controller=0
        ),
    )
    enemy_player = make_player(
        raw_enemy,
        index=1,
        class_id=opponent_class,
        deck=enemy_deck_cards,
        hand=[],
        board=enemy_board,
        faiths=enemy_faiths,
        emblems=enemy_emblems,
        leader_damage_modifiers=_make_leader_damage_modifiers(
            raw_enemy, state_module=state_module, controller=1
        ),
    )

    game_state = state_module.GameState(
        players=[own_player, enemy_player],
        active_player=0,
        first_player=first_player,
        mulligan_completed=[True, True],
        turn=current_turn,
        phase=state_module.Phase.MAIN,
        next_entity_id=max(
            1,
            max(
                [
                    int(getattr(entity, "entity_id", 0))
                    for player in (own_player, enemy_player)
                    for entity in (*player.hand, *player.board, *player.faiths, *player.emblems)
                    if _int(getattr(entity, "entity_id", 0)) is not None
                ]
                or [0]
            )
            + 1,
        ),
    )
    core.state = game_state
    # Tracker's destroyed pool is public match history.  Reconstruct known
    # follower records without removing them when a future Reanimate occurs.
    destroyed_records: list[Any] = []
    for sequence, item in enumerate(getattr(adapted.state, "destroyed_this_match", ()) or (), start=1):
        definition = resolve(int(item.card_id))
        if definition is None or str(getattr(definition, "card_type", "")) != "随从":
            continue
        destroyed_records.append(
            state_module.DestroyedFollowerRecord(
                definition=definition,
                owner=0,
                death_sequence=sequence,
                cause=state_module.DeathCause.EFFECT_DESTROY,
                origin=origin_module.CardOrigin.DECK,
                destroyed_turn=0,
            )
        )
    core.state.destroyed_followers = destroyed_records
    core.state._next_death_sequence = len(destroyed_records) + 1

    if not raw_enemy.get("hand"):
        warnings.append("opponent hand identity/order is hidden")
    else:
        warnings.append("opponent hand is not hydrated because the public snapshot may redact it")
    if not own_order_exact and not puzzle_empty:
        warnings.append("own remaining deck order is unknown")
    if getattr(adapted.state, "skybound_art", 0) or getattr(adapted.state, "super_skybound_art", 0):
        warnings.append("Skybound Art gauges are exposed by Tracker but not a native PlayerState resource")
    if getattr(adapted.state, "earth_sigil", 0):
        # The aggregate is visible; individual sigil ownership/countdowns are
        # only hydrated when field cards expose is_earth_sigil/stack.
        known_sigils = sum(int(getattr(entity, "earth_sigil_count", 0)) for entity in own_player.board if hasattr(entity, "earth_sigil_count"))
        if known_sigils != int(getattr(adapted.state, "earth_sigil", 0)):
            warnings.append("earth sigil aggregate exceeds per-amulet public detail")

    unsupported_ids = tuple(sorted(missing))
    if unsupported_ids:
        warnings.append("missing SWB-RL card definitions: " + ", ".join(str(item) for item in unsupported_ids))
    legal_projection = adapted.legal_actions if isinstance(adapted.legal_actions, Mapping) else {}
    special = tuple(
        dict.fromkeys(
            _legal_ids(legal_projection, "can_special_action_field_cards")
            + _legal_ids(legal_projection, "can_special_action_area_cards")
        )
    )
    if special:
        warnings.append("special action cards are visible but not mapped to GameCommand")
    mode_skill_cards = tuple(
        dict.fromkeys(
            _legal_ids(legal_projection, "can_mode_skill_cards")
            + _legal_ids(legal_projection, "super_evolve_can_mode_skill_cards")
        )
    )
    if mode_skill_cards:
        warnings.append("mode-skill actions are visible but their choice payload is not hydrated")

    root_commands = _root_commands(adapted, core, raw_mine)
    hidden_unknown = True
    # A puzzle with an explicitly empty deck has no hidden draw source.  The
    # opponent hand/deck is still hidden, so callers may choose to keep it
    # incomplete if a card effect targets that zone.
    if puzzle_empty and not enemy_deck_count and not raw_enemy.get("hand"):
        hidden_unknown = True
    engine = TrackerShadowEngine(
        core,
        root_commands,
        warnings=warnings,
        hidden_state_unknown=hidden_unknown,
        card_order_unknown=not own_order_exact and not puzzle_empty,
    )
    return ShadowStateBuildResult(
        engine=engine,
        source_root=source_root,
        warnings=tuple(dict.fromkeys(warnings)),
        unsupported_card_ids=unsupported_ids,
        hidden_state_unknown=hidden_unknown,
        card_order_unknown=not own_order_exact and not puzzle_empty,
    )


__all__ = [
    "ShadowStateBuildResult",
    "TrackerShadowEngine",
    "build_shadow_engine",
]
