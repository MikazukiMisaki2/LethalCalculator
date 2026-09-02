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

    def test_maps_extended_resources_and_crest_instances(self):
        payload = {
            "root": {
                "is_ally_turn": True,
                "players": [
                    {
                        "life": 18,
                        "max_life": 20,
                        "pp": 2,
                        "max_pp": 7,
                        "extra_pp": 1,
                        "evolve_points": 1,
                        "super_evolve_points": 1,
                        "cemetery_count": 6,
                        "rally": 5,
                        "play_count": 3,
                        "is_awakening": True,
                        "earth_sigil": 2,
                        "skybound_art": 1,
                        "super_skybound_art": 2,
                        "faith_instances": [{"source_card_id": 100, "unique_id": 900, "value": 4}],
                        "crests": [{"card_id": 1, "unique_id": 901, "countdown": 2}],
                        "extra_crests": [{"card_id": 2, "unique_id": 902, "countdown": 4}],
                        "hand": [],
                        "field": [],
                    },
                    {"life": 10, "hand": [], "field": []},
                ],
            },
            "legal_actions": {},
        }
        state = SnapshotAdapter.adapt(payload).state
        self.assertEqual((state.extra_pp, state.earth_sigil, state.skybound_art, state.super_skybound_art), (1, 2, 1, 2))
        self.assertEqual(state.faith_instances[0]["value"], 4)
        self.assertEqual([(item["card_id"], item["countdown"]) for item in state.crest_instances], [(1, 2), (2, 4)])
        self.assertEqual(state.active_crests, [1, 2])


if __name__ == "__main__":
    unittest.main()
