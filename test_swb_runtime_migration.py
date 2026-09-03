"""Small runtime smoke tests for the first SWB-RL migration bundle.

These tests intentionally exercise the candidate through the same
``EventInterpreter`` used by the lethal engine.  They are not a claim that
the entire SWB-RL RuleBook is verified; they are the acceptance fixtures that
must pass before a card is moved from the isolated bundle into a reviewed
override.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from event_interpreter import EventInterpreter
from lethal_models import LethalFollower, LethalState, create_hand_card_from_rule
from promote_swb_rules import build_migration_bundle


ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("SWB_RL_PATH", r"D:\Github\SWB-RL"))
CANDIDATE_PATH = ROOT / "data" / "imported" / "swb_card_rules_v2_candidate.json"
BASE_PATH = ROOT / "data" / "generated" / "card_rules_v2.json"
CATALOG_PATH = ROOT / "data" / "generated" / "card_catalog.json"
SCHEMA_PATH = ROOT / "schemas" / "card_rules_v2.schema.json"
MATRIX_PATH = ROOT / "schemas" / "card_rules_v2_support.json"


@unittest.skipUnless(CANDIDATE_PATH.exists(), "run adapt_swb_rules.py before migration smoke tests")
class SwbRuntimeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        cls.base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        cls.overlay, _report = build_migration_bundle(
            cls.candidate,
            catalog=cls.catalog,
            schema=cls.schema,
            matrix=cls.matrix,
            card_ids="10753310,10413110,10413310",
        )

    def make_card(self, card_id: int, unique_id: int):
        metadata = self.catalog["cards"][str(card_id)]
        rule = self.overlay["rules"][str(card_id)]
        type_id = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}[metadata["type"]]
        return create_hand_card_from_rule(
            card_id,
            dict(
                rule,
                cost=metadata.get("cost", 0),
                type=type_id,
                atk=metadata.get("stats", {}).get("attack", 0),
                life=metadata.get("stats", {}).get("life", 0),
                name=metadata.get("name", {}).get("eng", str(card_id)),
            ),
            unique_id,
        )

    def test_night_song_ordered_split_and_necromancy_are_executable(self) -> None:
        spell = self.make_card(10753310, 1)
        enemies = [
            LethalFollower(2, 9000, "enemy-1", 1, 3),
            LethalFollower(3, 9001, "enemy-2", 1, 1),
            LethalFollower(4, 9002, "enemy-3", 1, 1),
        ]
        state = LethalState(
            enemy_hp=10,
            pp=3,
            max_pp=3,
            ep=0,
            sep=0,
            cemetery=6,
            hand=[spell],
            enemy_board=enemies,
            destroyed_pool_known=True,
        )
        interpreter = EventInterpreter(self.overlay, catalog=self.catalog, card_db={spell.card_id: spell})
        result = interpreter.play(state, spell.unique_id)
        self.assertEqual(result.state.enemy_board, [])
        self.assertEqual(result.state.enemy_hp, 8)
        # The six shadows are consumed before the spell itself enters the
        # cemetery, so the net visible counter is one.
        self.assertEqual(result.state.cemetery, 1)
        self.assertFalse(result.unsupported_ops)

    def test_cupitan_repeat_keeps_probability_mass_and_removes_dead_targets(self) -> None:
        card = self.make_card(10413110, 10)
        state = LethalState(
            enemy_hp=10,
            pp=0,
            max_pp=0,
            ep=1,
            sep=0,
            my_board=[LethalFollower(10, card.card_id, card.name, card.atk, card.life)],
            enemy_board=[
                LethalFollower(20, 9000, "enemy-1", 1, 1),
                LethalFollower(21, 9001, "enemy-2", 1, 3),
            ],
        )
        interpreter = EventInterpreter(self.overlay, catalog=self.catalog, card_db={card.card_id: card})
        branches = interpreter.evolve_branches(state, 10)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertTrue(branches)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))
        # The one-life follower must disappear after the first hit and can no
        # longer receive the remaining random activations.
        self.assertTrue(all(all(f.unique_id != 20 for f in branch.state.enemy_board) for branch in branches))

    def test_mode_choice_can_be_executed_as_a_player_selection(self) -> None:
        card = self.make_card(10413310, 30)
        ally = LethalFollower(31, 9900, "ally", 1, 1)
        state = LethalState(
            enemy_hp=10,
            pp=2,
            max_pp=2,
            ep=0,
            sep=0,
            hand=[card],
            my_board=[ally],
        )
        interpreter = EventInterpreter(self.overlay, catalog=self.catalog, card_db={card.card_id: card})
        result = interpreter.play(state, card.unique_id, choice="attack_rush")
        self.assertEqual(result.state.my_board[0].atk, 2)
        self.assertTrue(result.state.my_board[0].has_rush)
        self.assertFalse(result.unsupported_ops)


if __name__ == "__main__":
    unittest.main()
