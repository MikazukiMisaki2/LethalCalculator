from __future__ import annotations

import json
from pathlib import Path
import unittest

from snapshot_adapter import SnapshotAdapter
from shadow_state_adapter import TrackerShadowEngine, build_shadow_engine


ROOT = Path(__file__).resolve().parent
SWB_ROOT = Path(r"D:\Github\SWB-RL")
CATALOG_PATH = ROOT / "data" / "generated" / "card_catalog.json"
RULES_PATH = ROOT / "data" / "generated" / "card_rules_v2.json"
FIXTURE_PATH = ROOT / "fixtures" / "tracker_snapshots" / "complete.json"


@unittest.skipUnless(
    (SWB_ROOT / "data" / "cards.sqlite3").is_file(),
    "SWB-RL checkout is unavailable",
)
class ShadowStateAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        cls.snapshot = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _adapt(self, snapshot: dict) -> object:
        return SnapshotAdapter.adapt(
            snapshot,
            catalog=self.catalog,
            rules=self.rules,
        )

    def test_hydrates_visible_state_and_tracker_root_commands(self) -> None:
        adapted = self._adapt(self.snapshot)
        self.assertTrue(adapted.trusted)
        self.assertTrue(adapted.usable)
        built = build_shadow_engine(
            self.snapshot,
            adapted,
            swb_rl_root=SWB_ROOT,
            seed=0,
        )
        self.assertIsNotNone(built.engine)
        assert built.engine is not None
        self.assertIsInstance(built.engine, TrackerShadowEngine)
        command_names = {type(command).__name__ for command in built.engine.legal_commands()}
        self.assertIn("PlayCard", command_names)
        self.assertIn("Attack", command_names)
        # The public snapshot has one Extra PP.  The adapter should expose the
        # resource command even when no individual card is marked as extra-only.
        self.assertIn("UseExtraPP", command_names)
        self.assertTrue(built.hidden_state_unknown)
        self.assertTrue(any("opponent hand" in warning for warning in built.warnings))

    def test_clone_and_apply_are_isolated_and_root_legality_is_enforced(self) -> None:
        adapted = self._adapt(self.snapshot)
        built = build_shadow_engine(self.snapshot, adapted, swb_rl_root=SWB_ROOT)
        self.assertIsNotNone(built.engine)
        assert built.engine is not None
        root_commands = built.engine.legal_commands()
        play = next(command for command in root_commands if type(command).__name__ == "PlayCard")
        illegal_clone = built.engine.clone()
        with self.assertRaises(ValueError):
            illegal_clone.apply(type(play)(0, 99, "normal"))
        clone = built.engine.clone()
        clone.apply(play)
        self.assertEqual(
            {type(command).__name__ for command in built.engine.legal_commands()},
            {type(command).__name__ for command in root_commands},
        )

    def test_extra_only_cards_require_the_extra_pp_command_first(self) -> None:
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["legal_actions"]["can_play_cards"] = []
        snapshot["legal_actions"]["can_play_cards_with_extra_pp"] = [101]
        snapshot["root"]["players"][0]["extra_pp"] = 1
        adapted = self._adapt(snapshot)
        built = build_shadow_engine(snapshot, adapted, swb_rl_root=SWB_ROOT)
        self.assertIsNotNone(built.engine)
        assert built.engine is not None
        commands = built.engine.legal_commands()
        self.assertIn("UseExtraPP", {type(command).__name__ for command in commands})
        self.assertNotIn("PlayCard", {type(command).__name__ for command in commands})

    def test_puzzle_mode_can_explicitly_remove_own_deck(self) -> None:
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["battle_mode"] = "puzzle"
        snapshot["shadow_empty_deck"] = True
        adapted = self._adapt(snapshot)
        built = build_shadow_engine(snapshot, adapted, swb_rl_root=SWB_ROOT)
        self.assertIsNotNone(built.engine)
        assert built.engine is not None
        self.assertEqual(built.engine.players[0].deck, [])
        self.assertTrue(any("empty" in warning for warning in built.warnings))

    def test_public_leader_damage_buff_is_projected(self) -> None:
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["root"]["players"][1]["buff"] = {
            "sources": [],
            "max_life": 0,
            "temp_shield": False,
            "damage_cut": 0,
            "increase_damage": 1,
        }
        adapted = self._adapt(snapshot)
        built = build_shadow_engine(snapshot, adapted, swb_rl_root=SWB_ROOT)
        self.assertIsNotNone(built.engine)
        assert built.engine is not None
        modifiers = built.engine.players[1].leader_damage_modifiers
        self.assertEqual(len(modifiers), 1)
        self.assertEqual(modifiers[0].amount, 1)


if __name__ == "__main__":
    unittest.main()
