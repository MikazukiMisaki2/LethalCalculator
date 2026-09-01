import json
import unittest
from pathlib import Path

from snapshot_adapter import SnapshotAdapter


class SnapshotAdapterTests(unittest.TestCase):
    def test_maps_resources_board_hand_legal_actions_and_crests(self):
        payload = json.loads((Path(__file__).parent / "fixtures" / "tracker_snapshot_minimal.json").read_text(encoding="utf-8"))
        result = SnapshotAdapter.adapt(payload)
        self.assertTrue(result.usable)
        self.assertEqual(result.state.enemy_hp, 6)
        self.assertEqual(result.state.pp, 5)
        self.assertEqual(result.state.rally, 8)
        self.assertEqual(result.state.total_deck_count, 25)
        self.assertEqual(result.state.active_crests, [10364122])
        self.assertEqual(result.state.hand[0].unique_id, 101)
        self.assertEqual(result.state.my_board[0].can_attack_field, True)
        self.assertEqual(result.legal_actions["attack_targets"]["201"], [401])

    def test_missing_legal_actions_is_not_usable(self):
        payload = {"root": {"is_ally_turn": True, "players": [{"life": 20}, {"life": 5}]}}
        result = SnapshotAdapter.adapt(payload)
        self.assertFalse(result.usable)
        self.assertTrue(any("legal_actions" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
