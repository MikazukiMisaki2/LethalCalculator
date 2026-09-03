"""Focused tests for the optional native SWB-RL backend.

The unit tests use a tiny fake engine so they do not require the SWB-RL
checkout, torch, a checkpoint or card images.  The real engine is covered by
the SWB-RL project's own test suite and can be smoke-tested through the
adapter once a simulator match is running.
"""

from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass

from swb_rl_backend import SwbRlBackend


@dataclass(frozen=True)
class FakeEvent:
    type: str


@dataclass(frozen=True)
class EndTurn:
    player_index: int
    type: str = "end_turn"


@dataclass(frozen=True)
class UseExtraPP:
    player_index: int
    type: str = "use_extra_pp"


@dataclass(frozen=True)
class PlayCard:
    player_index: int
    hand_index: int
    mode_id: str = "normal"
    type: str = "play_card"


@dataclass(frozen=True)
class Attack:
    player_index: int
    attacker_id: int
    target_id: int | None
    type: str = "attack"


@dataclass(frozen=True)
class Choose:
    player_index: int
    option_id: str
    type: str = "choose"


class FakeCard:
    def __init__(self, name: str, cost: int = 1):
        self.name = name
        self.cost = cost


class FakeUnit:
    def __init__(self, entity_id: int, name: str):
        self.entity_id = entity_id
        self.name = name


class FakePlayer:
    def __init__(self, health: int, *, hand=None, board=None):
        self.health = health
        self.hand = list(hand or [])
        self.board = list(board or [])


class FakeEngine:
    """Small command-driven core with the same seam used by GameEngine."""

    def __init__(
        self,
        *,
        enemy_hp: int = 5,
        mana: int = 3,
        extra_active: bool = False,
        played: bool = False,
        attacked: bool = False,
        choice_pending: bool = False,
        random_event: bool = False,
        phase: str = "main",
    ):
        self.current_player = 0
        self.turn = 5
        self.terminated = False
        self.winner = None
        self.mana = mana
        self.extra_active = extra_active
        self.played = played
        self.attacked = attacked
        self.choice_pending = choice_pending
        self.random_event = random_event
        self.phase = phase
        self.players = [
            FakePlayer(
                20,
                hand=[FakeCard("Burst", 4), FakeCard("Choice", 1)],
                board=[FakeUnit(11, "Attacker")],
            ),
            FakePlayer( enemy_hp, board=[FakeUnit(21, "Target")]),
        ]

    @property
    def state(self):
        class State:
            pass

        state = State()
        state.active_player = self.current_player
        state.phase = self.phase
        state.pending_choice = None
        if self.choice_pending:
            class Option:
                option_id = "face"
                label = "Enemy leader"

            class Request:
                options = (Option(),)

            state.pending_choice = Request()
        return state

    def clone(self):
        return copy.deepcopy(self)

    def deterministic_fingerprint(self):
        return {
            "state": {
                "enemy_hp": self.players[1].health,
                "mana": self.mana,
                "extra_active": self.extra_active,
                "played": self.played,
                "attacked": self.attacked,
                "choice_pending": self.choice_pending,
            }
        }

    def legal_commands(self):
        if self.choice_pending:
            return [Choose(0, "face")]
        commands = [EndTurn(0)]
        if self.mana >= 3 and not self.extra_active:
            commands.append(UseExtraPP(0))
        if not self.played and self.mana >= 1:
            commands.append(PlayCard(0, 1, "normal"))
        if not self.played and (self.mana >= 4 or self.extra_active):
            commands.append(PlayCard(0, 0, "normal"))
        if not self.attacked and (self.played or self.extra_active):
            commands.append(Attack(0, 11, None))
        return commands

    def apply(self, command):
        if isinstance(command, EndTurn):
            self.current_player = 1
            return type("Transition", (), {"events": ()})()
        if isinstance(command, UseExtraPP):
            self.extra_active = True
            return type("Transition", (), {"events": ()})()
        if isinstance(command, PlayCard):
            if command.hand_index == 0:
                if self.mana < 4 and not self.extra_active:
                    raise ValueError("not enough mana")
                self.mana = max(0, self.mana - 4)
                self.players[1].health -= 4
            else:
                self.mana = max(0, self.mana - 1)
                self.choice_pending = True
            self.played = True
            return type("Transition", (), {"events": ()})()
        if isinstance(command, Choose):
            self.choice_pending = False
            self.players[1].health -= 1
            return type("Transition", (), {"events": ()})()
        if isinstance(command, Attack):
            self.attacked = True
            self.players[1].health -= 3
            events = (FakeEvent("random_choices_selected"),) if self.random_event else ()
            if self.players[1].health <= 0:
                self.terminated = True
                self.winner = 0
            return type("Transition", (), {"events": events})()
        raise ValueError(command)


class SwbRlBackendTests(unittest.TestCase):
    def test_search_uses_only_legal_commands_and_does_not_end_turn(self):
        result = SwbRlBackend(max_depth=4).solve(FakeEngine(enemy_hp=6))
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.probability, 1.0)
        self.assertTrue(any("Attack" in step for step in result.sequence))
        self.assertNotIn("EndTurn", " ".join(result.sequence))

    def test_extra_pp_and_mode_are_separate_legal_actions(self):
        result = SwbRlBackend(max_depth=5).solve(FakeEngine(enemy_hp=7, mana=3))
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("Use Extra PP" in step for step in result.sequence))
        self.assertTrue(any("Burst" in step for step in result.sequence))

    def test_pending_choice_is_enumerated(self):
        result = SwbRlBackend(max_depth=4).solve(
            FakeEngine(enemy_hp=1, mana=1, choice_pending=True)
        )
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("Choose Enemy leader" in step for step in result.sequence))

    def test_seeded_random_path_is_not_confirmed(self):
        result = SwbRlBackend(max_depth=4).solve(
            FakeEngine(enemy_hp=6, random_event=True)
        )
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertTrue(result.lethal_found)
        self.assertTrue(result.random_observed)
        self.assertEqual(result.probability, 0.0)
        self.assertTrue(any("exact probability" in warning for warning in result.warnings))

    def test_node_limit_is_reported(self):
        result = SwbRlBackend(max_depth=8, node_limit=1).solve(
            FakeEngine(enemy_hp=20, mana=3)
        )
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertTrue(result.truncated)
        self.assertIn("search limit reached", result.warnings)

    def test_mulligan_is_not_treated_as_a_lethal_turn(self):
        engine = FakeEngine(enemy_hp=1, phase="mulligan")
        result = SwbRlBackend(max_depth=4).solve(engine)
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertIn("unsupported phase: mulligan", result.warnings)

    def test_source_is_not_mutated_and_results_are_deterministic(self):
        engine = FakeEngine(enemy_hp=3)
        before = engine.deterministic_fingerprint()
        first = SwbRlBackend(max_depth=4).solve(engine)
        second = SwbRlBackend(max_depth=4).solve(engine)
        self.assertEqual(before, engine.deterministic_fingerprint())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
