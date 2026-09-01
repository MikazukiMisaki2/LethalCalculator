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
            if info is not None:
                from lethal_models import create_hand_card_from_rule
                card = create_hand_card_from_rule(cid, info, uid)
                return replace(
                    card,
                    cost=_int(raw.get("cost"), card.cost),
                    atk=_int(raw.get("attack"), card.atk),
                    life=_int(raw.get("life"), card.life),
                    tribes=tuple(str(item) for item in (meta.get("tribes", ()) if isinstance(meta.get("tribes"), (list, tuple)) else ())),
                )
            meta = catalog_cards.get(cid, {})
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
            )

        def follower(raw: Mapping[str, Any], *, enemy_side: bool = False) -> LethalFollower:
            uid = _int(raw.get("unique_id"))
            cid = _int(raw.get("card_id"))
            meta = catalog_cards.get(cid, {})
            name = meta.get("name", {}) if isinstance(meta.get("name"), Mapping) else {}
            attacked = _bool(raw.get("has_attacked"))
            attack_limit = _int(raw.get("attack_limit"))
            attacks_left = max(0, attack_limit - int(attacked)) if attack_limit > 0 else int(not attacked)
            return LethalFollower(
                unique_id=uid,
                card_id=cid,
                name=str(name.get("chs") or name.get("eng") or cid),
                atk=_int(raw.get("attack")),
                hp=_int(raw.get("life")),
                has_storm=uid in set(legal.get("can_attack_leader_cards", ())) if legal else False,
                has_rush=uid in set(legal.get("can_attack_field_cards", ())) if legal else False,
                is_ward=_bool(raw.get("has_guard")),
                is_evolved=_int(raw.get("evolve_state")) > 0,
                can_attack_leader=_bool(raw.get("can_attack_leader")),
                can_attack_field=_bool(raw.get("can_attack_field")),
                attacks_left=attacks_left,
                damage_cap=None,
                countdown=(_int(raw.get("countdown"), _int(raw.get("remaining_countdown"), _int(raw.get("count"), -1))) if any(key in raw for key in ("countdown", "remaining_countdown", "count")) else None),
            )

        my_hand = [hand_card(card) for card in mine.get("hand", ()) if isinstance(card, Mapping)]
        my_board = [follower(card) for card in mine.get("field", ()) if isinstance(card, Mapping)]
        enemy_board = [follower(card, enemy_side=True) for card in enemy.get("field", ()) if isinstance(card, Mapping)]
        crest_ids = []
        for crest in (*mine.get("crests", ()), *mine.get("extra_crests", ())):
            if isinstance(crest, Mapping) and isinstance(crest.get("card_id"), int):
                crest_ids.append(crest["card_id"])

        state = LethalState(
            enemy_hp=_int(enemy.get("life")),
            pp=_int(mine.get("pp")),
            max_pp=_int(mine.get("max_pp")),
            ep=_int(mine.get("evolve_points")),
            sep=_int(mine.get("super_evolve_points")),
            rally=_int(mine.get("rally")),
            cemetery=_int(mine.get("cemetery_count")),
            is_awakening=_bool(mine.get("is_awakening")),
            play_count=_int(mine.get("play_count")),
            faith=_int(mine.get("faith", mine.get("faith_value", 0))),
            evolved_allies_this_turn=_int(mine.get("evolved_allies_this_turn", 0)),
            evolved_allies_this_match=_int(mine.get("evolved_allies_this_match", mine.get("evolved_count", 0))),
            my_board=my_board,
            enemy_board=enemy_board,
            hand=my_hand,
            total_deck_count=_int(mine.get("deck_count")),
            active_crests=crest_ids,
        )
        if len(my_board) > 5:
            warnings.append("ally field contains more than five cards")
        if unsupported:
            warnings.append(f"{len(unsupported)} hand card(s) have no executable rule")
        usable = bool(legal is not None and is_ally_turn)
        return SnapshotAdapterResult(state, legal, is_ally_turn, usable, tuple(sorted(unsupported)), tuple(warnings))


__all__ = ["SnapshotAdapter", "SnapshotAdapterResult"]
