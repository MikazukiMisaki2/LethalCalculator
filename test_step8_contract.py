import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from lethal_models import LethalResult
from snapshot_adapter import SnapshotAdapter
from tracker_integration import TrackerLethalSession


ROOT = Path(__file__).resolve().parent


class _CountingEngine:
    def __init__(self):
        self.calls = 0

    def solve(self, state):
        self.calls += 1
        return LethalResult("CONFIRMED", 1.0, ["test route"])


class _FailingEngine:
    def solve(self, state):
        raise RuntimeError("synthetic solver failure")


class _MaxDamageEngine:
    def solve(self, state):
        return LethalResult("NO_LETHAL", 0.0, [])

    def max_damage(self, state):
        return 7, ["best non-lethal line"]


class Step8ContractTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / "fixtures" / "tracker_snapshots" / name).read_text(encoding="utf-8"))

    def test_complete_tracker_fixture_maps_full_contract(self):
        result = SnapshotAdapter.adapt(self.load("complete.json"))
        self.assertTrue(result.trusted)
        self.assertTrue(result.usable)
        state = result.state
        self.assertEqual([f.unique_id for f in state.enemy_board], [401])
        self.assertEqual((state.my_board[0].atk, state.my_board[0].hp), (3, 2))
        self.assertEqual(state.my_board[0].attack_targets, (401, 999))
        self.assertEqual(state.legal_attack_targets[201], (401, 999))
        self.assertEqual(state.legal_actions["can_attack_leader_cards"], [201])
        self.assertEqual(state.legal_modes[101], ("normal",))
        self.assertEqual((state.pp, state.extra_pp, state.ep, state.sep), (5, 1, 1, 1))
        self.assertEqual((state.rally, state.play_count, state.cemetery), (12, 4, 6))
        self.assertEqual((state.faith_instances[0]["value"], state.crest_instances[0]["countdown"]), (8, 2))
        self.assertEqual([item.card_id for item in state.destroyed_this_match], [2001, 2001, 2002])
        self.assertEqual(state.my_board[0].buff["attack"], 1)

    def test_snapshot_distinguishes_auto_evolve_from_manual_evolve(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        ally["is_evolved_this_turn"] = True
        ally["_recent_actions"] = [{"turn": ally["turn"], "kind": "自动进化", "card_id": 10021110}]
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual(result.state.evolved_allies_this_turn, 1)
        self.assertEqual(result.state.manual_evolutions_this_turn, 0)
        ally["_recent_actions"].append({"turn": ally["turn"], "kind": "手动进化", "card_id": 10021110})
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual(result.state.manual_evolutions_this_turn, 1)

    def test_attack_targets_are_authoritative_restrictions(self):
        result = SnapshotAdapter.adapt(self.load("target_restricted.json"))
        self.assertTrue(result.trusted)
        follower = result.state.my_board[0]
        self.assertFalse(follower.can_attack_leader)
        self.assertTrue(follower.can_attack_field)
        from event_interpreter import EventInterpreter
        interpreter = EventInterpreter({})
        self.assertTrue(interpreter.attack_leader(result.state, 200).warnings)
        self.assertFalse(interpreter.attack_follower(result.state, 200, 300).warnings)

    def test_field_attack_targets_are_enforced_without_legal_actions(self):
        payload = self.load("target_restricted.json")
        payload.pop("legal_actions")
        result = SnapshotAdapter.adapt(payload)
        self.assertFalse(result.trusted)
        self.assertTrue(result.state.attack_targets_known)
        from event_interpreter import EventInterpreter
        interpreter = EventInterpreter({})
        self.assertTrue(interpreter.attack_leader(result.state, 200).warnings)
        self.assertFalse(interpreter.attack_follower(result.state, 200, 300).warnings)

    def test_explicit_empty_target_map_is_trusted_when_no_attacker_is_legal(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        ally["field"][0]["attack_targets"] = []
        payload["legal_actions"]["attack_targets"] = {}
        payload["legal_actions"]["can_attack_leader_cards"] = []
        payload["legal_actions"]["can_attack_field_cards"] = []
        result = SnapshotAdapter.adapt(payload)
        self.assertTrue(result.trusted)
        self.assertEqual(result.state.legal_attack_targets, {})

    def test_tracker_leader_buff_modifier_is_projected(self):
        payload = self.load("complete.json")
        payload["root"]["players"][1]["buff"] = {
            "damage_cut": 2,
            "increase_damage": 1,
        }
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual(result.state.enemy_damage_taken_modifier, -1)
        self.assertEqual(result.state.enemy_buff["damage_cut"], 2)

    def test_tracker_idle_damage_cut_sentinel_does_not_add_damage(self):
        payload = self.load("complete.json")
        payload["root"]["players"][1]["buff"] = {
            "sources": [],
            "damage_cut": -1,
            "increase_damage": 0,
        }
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual(result.state.enemy_damage_taken_modifier, 0)

    def test_faith_instances_without_uids_keep_distinct_identity(self):
        payload = self.load("complete.json")
        payload["root"]["players"][0]["faith_instances"] = [
            {"card_id": 10354110, "value": 3},
            {"card_id": 10354110, "value": 5},
        ]
        payload["root"]["players"][0].pop("faith", None)
        payload["root"]["players"][0].pop("faith_value", None)
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual([item["unique_id"] for item in result.state.faith_instances], [-1, -2])
        self.assertEqual(result.state.faith, 8)

    def test_compact_faith_and_destroyed_count_maps_preserve_multiplicity(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        ally.pop("faith_instances", None)
        ally.pop("faith", None)
        ally["faith_resources"] = {
            "501": {"source_card_id": 10354110, "value": 3},
            "502": {"source_card_id": 10354110, "value": 5},
        }
        ally["destroyed_card_ids"] = {"2001": 3, "2002": 1}
        result = SnapshotAdapter.adapt(payload)
        self.assertEqual([item["unique_id"] for item in result.state.faith_instances], [501, 502])
        self.assertEqual(result.state.faith, 8)
        self.assertEqual([item.card_id for item in result.state.destroyed_this_match], [2001, 2001, 2001, 2002])

    def test_common_tracker_aliases_are_normalized(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        for canonical, alias in (("pp", "PP"), ("max_pp", "MaxPP"), ("evolve_points", "EP"), ("super_evolve_points", "SEP"), ("rally", "Rally"), ("play_count", "PlayCount"), ("cemetery_count", "Cemetery"), ("is_awakening", "Awakening")):
            ally[alias] = ally.pop(canonical)
        result = SnapshotAdapter.adapt(payload)
        self.assertTrue(result.trusted)
        self.assertEqual((result.state.pp, result.state.max_pp, result.state.ep, result.state.sep), (5, 7, 1, 1))
        self.assertEqual((result.state.rally, result.state.play_count, result.state.cemetery), (12, 4, 6))

    def test_hand_alias_keeps_legal_modes_and_empty_buff_authoritative(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        ally["Hand"] = ally.pop("hand")
        ally["Hand"][0]["buff"] = {}
        rules = {
            "rules": {
                "10021110": {
                    "card_id": 10021110,
                    "cost": 1,
                    "stats": {"attack": 1, "life": 1},
                    "static_keywords": ["storm"],
                    "modes": [{"kind": "normal", "cost": 1, "abilities": []}],
                }
            }
        }
        result = SnapshotAdapter.adapt(payload, rules=rules)
        self.assertTrue(result.trusted)
        self.assertEqual(result.state.legal_modes[101], ("normal",))
        self.assertEqual(result.state.hand[0].buff, {})

    def test_remove_all_abilities_clears_catalog_keywords(self):
        payload = self.load("complete.json")
        payload["root"]["players"][0]["field"][0].update({
            "abilities_removed": True,
            "has_storm": False,
            "attack_targets": [],
        })
        rules = {"rules": {"10021110": {"static_keywords": ["storm"]}}}
        result = SnapshotAdapter.adapt(payload, rules=rules)
        follower = result.state.my_board[0]
        self.assertTrue(follower.abilities_removed)
        self.assertNotIn("storm", follower.statuses)

    def test_branch_actions_honor_tracker_mode_and_evolve_lists(self):
        from event_interpreter import EventInterpreter
        from lethal_models import LethalHandCard, LethalFollower, LethalState
        card = LethalHandCard(10, 100, "Mode", 1, 1, enhance_cost=2)
        follower = LethalFollower(20, 101, "Follower", 2, 2)
        state = LethalState(
            enemy_hp=10, pp=2, max_pp=2, ep=1, sep=0,
            hand=[card], my_board=[follower], legal_actions_known=True,
            legal_modes={10: ("normal",)}, legal_modes_known=True,
            legal_evolve_uids=(),
        )
        interpreter = EventInterpreter({})
        self.assertTrue(interpreter.play_branches(state, 10, mode="enhance")[0].warnings)
        self.assertTrue(interpreter.evolve_branches(state, 20)[0].warnings)

    def test_only_one_manual_evolution_is_allowed_per_turn(self):
        from event_interpreter import EventInterpreter
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=20, pp=0, max_pp=0, ep=1, sep=2,
            evolved_allies_this_turn=1,
            manual_evolutions_this_turn=1,
            my_board=[
                LethalFollower(20, 10021110, "Auto target", 3, 3),
                LethalFollower(21, 10021120, "Manual target", 2, 2),
            ],
        )
        interpreter = EventInterpreter({})
        normal = interpreter.evolve(state, 20)
        super_result = interpreter.evolve_branches(state, 20, super_evolve=True)[0]
        self.assertIs(normal.state, state)
        self.assertIs(super_result.state, state)
        self.assertTrue(any("only one manual evolution" in item for item in normal.warnings))
        self.assertTrue(any("only one manual evolution" in item for item in super_result.warnings))

    def test_engine_does_not_chain_two_manual_super_evolutions(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=20, pp=0, max_pp=0, ep=0, sep=2,
            manual_evolutions_this_turn=0,
            my_board=[
                LethalFollower(20, 10021110, "First", 1, 1),
                LethalFollower(21, 10021120, "Second", 1, 1),
            ],
            turn_number=7, super_evolve_turn=7,
        )
        damage, route = LethalEngine(max_depth=6).max_damage(state)
        self.assertEqual(damage, 0)
        self.assertLessEqual(sum("超进化" in item for item in route), 1)

    def test_auto_evolve_does_not_consume_manual_evolution_budget(self):
        from event_interpreter import EventInterpreter
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=20, pp=0, max_pp=0, ep=1, sep=0,
            evolved_allies_this_turn=1,
            manual_evolutions_this_turn=0,
            my_board=[
                LethalFollower(20, 10021110, "Auto target", 3, 3),
                LethalFollower(21, 10021120, "Manual target", 2, 2),
            ],
            turn_number=7, evolve_turn=4,
        )
        interpreter = EventInterpreter({})
        after_auto = interpreter.auto_evolve(state, 20).state
        self.assertEqual(after_auto.manual_evolutions_this_turn, 0)
        after_manual = interpreter.evolve(after_auto, 21)
        self.assertIsNot(after_manual.state, after_auto)
        self.assertEqual(after_manual.state.manual_evolutions_this_turn, 1)

    def test_baal_mode_one_buffs_self_and_one_random_other_ally(self):
        from event_interpreter import EventInterpreter
        from lethal_models import LethalFollower, LethalHandCard, LethalState

        rules = json.loads((ROOT / "data" / "generated" / "card_rules_v2.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "data" / "generated" / "card_catalog.json").read_text(encoding="utf-8"))
        interpreter = EventInterpreter(rules, catalog=catalog)
        baal = LethalHandCard(10, 10452130, "Baal", 3, 1, atk=2, life=2)
        allies = [
            LethalFollower(20, 1, "Ally A", 3, 3),
            LethalFollower(21, 2, "Ally B", 4, 4),
        ]
        state = LethalState(enemy_hp=10, pp=3, max_pp=3, ep=0, sep=0, hand=[baal], my_board=allies)
        branches = interpreter.play_branches(state, 10, choice="1")
        self.assertEqual({round(item.probability, 6) for item in branches}, {0.5})
        self.assertEqual({tuple((f.atk, f.hp) for f in item.state.my_board) for item in branches}, {
            ((4, 4), (4, 4), (3, 3)),
            ((3, 3), (5, 5), (3, 3)),
        })

    def test_engine_considers_baal_mode_choice_before_attack(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalHandCard, LethalState

        rules = json.loads((ROOT / "data" / "generated" / "card_rules_v2.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "data" / "generated" / "card_catalog.json").read_text(encoding="utf-8"))
        # The fixture ally has no printed ability; declaring it keeps the
        # route fully confirmed instead of introducing an unrelated
        # missing-rule gap.
        rules["rules"]["123"] = {"card_id": 123, "support": "verified", "modes": []}
        baal = LethalHandCard(10, 10452130, "Baal", 3, 1, atk=2, life=2)
        ally = LethalFollower(1, 123, "Stormy", 3, 3, has_storm=True, can_attack_leader=True, can_attack_field=True)
        state = LethalState(enemy_hp=4, pp=3, max_pp=3, ep=0, sep=0, hand=[baal], my_board=[ally])
        result = LethalEngine(rules=rules, catalog=catalog, max_depth=6).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("Baal" in item and "选项1" in item for item in result.sequence))
        self.assertTrue(any("造成 4 伤" in item for item in result.sequence))

    def test_baal_random_buff_reports_probability_instead_of_confirmed(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalHandCard, LethalState

        rules = json.loads((ROOT / "data" / "generated" / "card_rules_v2.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "data" / "generated" / "card_catalog.json").read_text(encoding="utf-8"))
        rules["rules"]["123"] = {"card_id": 123, "support": "verified", "modes": []}
        rules["rules"]["124"] = {"card_id": 124, "support": "verified", "modes": []}
        baal = LethalHandCard(10, 10452130, "Baal", 3, 1, atk=2, life=2)
        storm = LethalFollower(1, 123, "Storm", 4, 3, has_storm=True, can_attack_leader=True, can_attack_field=True)
        idle = LethalFollower(2, 124, "Idle", 1, 3)
        state = LethalState(enemy_hp=5, pp=3, max_pp=3, ep=0, sep=0, hand=[baal], my_board=[storm, idle])
        result = LethalEngine(rules=rules, catalog=catalog, max_depth=6).solve(state)
        self.assertEqual(result.status, "PROBABILISTIC")
        self.assertAlmostEqual(result.probability, 0.5)

    def test_evolution_variant_ids_use_base_catalog_name_and_rule(self):
        """Tracker's ``...21`` field id is an evolution alias, not a card.

        The generated catalog keeps one canonical ``...20`` entry.  The
        adapter must still expose the base id to CardRules so an evolved
        follower is named and interpreted instead of appearing as a numeric
        unsupported card.
        """
        payload = self.load("complete.json")
        payload["root"]["players"][0]["field"][0].update({
            "card_id": 10021111,
            "base_card_id": 10021110,
            "evolve_state": 1,
        })
        catalog = {
            "cards": {
                "10021110": {
                    "card_id": 10021110,
                    "base_card_id": 10021110,
                    "evolves_to": 10021111,
                    "name": {"chs": "测试进化体", "eng": "Test Evolved"},
                    "type": "follower",
                    "cost": 1,
                    "stats": {"attack": 1, "life": 1},
                }
            }
        }
        rules = {
            "rules": {
                "10021110": {
                    "card_id": 10021110,
                    "support": "generated",
                    "static_keywords": ["storm"],
                    "modes": [{"kind": "normal", "cost": 1, "abilities": []}],
                }
            }
        }
        result = SnapshotAdapter.adapt(payload, catalog=catalog, rules=rules)
        follower = result.state.my_board[0]
        self.assertEqual(follower.card_id, 10021110)
        self.assertEqual(follower.name, "测试进化体")
        self.assertNotEqual(follower.name, "10021111")

    def test_evolve_unlock_turn_survives_hypothetical_attack(self):
        """Stale LegalActions must not unlock evolution before its turn."""
        from event_interpreter import EventInterpreter
        from lethal_models import LethalFollower, LethalState

        follower = LethalFollower(20, 10021110, "Locked follower", 3, 3, can_attack_leader=True)
        state = LethalState(
            enemy_hp=20,
            pp=0,
            max_pp=0,
            ep=2,
            sep=2,
            my_board=[follower],
            legal_actions_known=True,
            legal_attack_targets={20: (999,)},
            attack_targets_known=True,
            legal_actions={"can_attack_leader_cards": [20], "attack_targets": {"20": [999]}},
            enemy_leader_uid=999,
            turn_number=2,
            evolve_turn=5,
            super_evolve_turn=7,
        )
        interpreter = EventInterpreter({})
        after_attack = interpreter.attack_leader(state, 20).state
        self.assertFalse(after_attack.legal_actions_known)
        normal = interpreter.evolve(after_attack, 20, super_evolve=False)
        super_result = interpreter.evolve(after_attack, 20, super_evolve=True)
        self.assertIs(normal.state, after_attack)
        self.assertIs(super_result.state, after_attack)
        self.assertTrue(any("locked until turn" in warning for warning in normal.warnings))
        self.assertTrue(any("locked until turn" in warning for warning in super_result.warnings))

    def test_max_damage_does_not_add_locked_evolution_after_attack(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=20,
            pp=0,
            max_pp=0,
            ep=2,
            sep=2,
            my_board=[LethalFollower(20, 10021110, "Locked follower", 3, 3, can_attack_leader=True)],
            legal_actions_known=True,
            attack_targets_known=True,
            legal_attack_targets={20: (999,)},
            legal_actions={"can_attack_leader_cards": [20], "attack_targets": {"20": [999]}},
            enemy_leader_uid=999,
            turn_number=2,
            evolve_turn=5,
            super_evolve_turn=7,
        )
        damage, route = LethalEngine(max_depth=6).max_damage(state)
        self.assertEqual(damage, 3)
        self.assertTrue(route)
        self.assertFalse(any("进化" in step for step in route))

    def test_attack_route_uses_effective_damage_and_not_idle_buff_sentinel(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=1,
            pp=0,
            max_pp=0,
            ep=0,
            sep=0,
            my_board=[LethalFollower(20, 10021110, "Two damage", 2, 2, can_attack_leader=True)],
        )
        result = LethalEngine(max_depth=3).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("造成 2 伤" in step and "敌HP剩: -1" in step for step in result.sequence))

    def test_engine_does_not_prefix_a_rejected_attack(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=1,
            pp=0,
            max_pp=0,
            ep=0,
            sep=0,
            my_board=[
                LethalFollower(1, 100, "Illegal attacker", 9, 9, can_attack_field=True),
                LethalFollower(2, 101, "Legal attacker", 1, 1, can_attack_leader=True),
            ],
            enemy_board=[LethalFollower(10, 200, "Target", 0, 9)],
            legal_actions_known=True,
            attack_targets_known=True,
            legal_attack_targets={1: (), 2: (999,)},
            legal_actions={"can_attack_field_cards": [1], "can_attack_leader_cards": [2]},
            enemy_leader_uid=999,
        )
        result = LethalEngine(max_depth=4).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(all("Illegal attacker" not in step for step in result.sequence))

    def test_leader_target_options_do_not_use_enemy_follower_uids(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalFollower, LethalState

        state = LethalState(
            enemy_hp=10,
            pp=0,
            max_pp=0,
            ep=0,
            sep=0,
            enemy_board=[LethalFollower(401, 200, "Enemy", 2, 2)],
            enemy_leader_uid=999,
        )
        rule = {"modes": [{"kind": "normal", "abilities": [{
            "trigger": "on_fanfare",
            "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3}],
        }]}]}
        options = LethalEngine(rules={})._rule_target_options(state, rule, "normal", "on_fanfare")
        self.assertEqual(options, [999])

    def test_any_target_can_bind_to_enemy_leader(self):
        from event_interpreter import EventInterpreter
        from lethal_models import LethalHandCard, LethalFollower, LethalState

        rules = {"rules": {50: {"card_id": 50, "modes": [{"kind": "normal", "cost": 0, "abilities": [{
            "trigger": "on_play",
            "effects": [{"op": "damage", "target": {"scope": "any", "selection": "chosen", "filters": {"side": "enemy", "card_type": ["follower", "leader"]}}, "amount": 2}],
        }]}]}}}
        source = LethalHandCard(1, 50, "Any damage", 0, 4)
        state = LethalState(
            enemy_hp=3, pp=0, max_pp=0, ep=0, sep=0,
            hand=[source], enemy_board=[LethalFollower(401, 200, "Enemy", 1, 5)], enemy_leader_uid=999,
        )
        interpreter = EventInterpreter(rules)
        result = interpreter.play(state, 1, target_uid=999)
        self.assertEqual(result.state.enemy_hp, 1)
        self.assertFalse(result.warnings)

    def test_fanfare_target_options_are_used_by_engine(self):
        from lethal_engine import LethalEngine
        from lethal_models import LethalHandCard, LethalFollower, LethalState

        rules = {"rules": {51: {"card_id": 51, "modes": [{"kind": "normal", "cost": 0, "abilities": [{
            "trigger": "on_fanfare",
            "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "chosen"}, "amount": 5}],
        }]}]}}}
        source = LethalHandCard(1, 51, "Fanfare shot", 0, 4)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            hand=[source], enemy_board=[LethalFollower(401, 200, "Enemy", 1, 5)],
        )
        engine = LethalEngine(rules=rules)
        options = engine._target_options(state, source, "normal")
        self.assertEqual(options, [401])
        resolved = engine.interpreter.play(state, 1, target_uid=401)
        self.assertEqual(resolved.state.enemy_board, [])

    def test_attacked_cards_does_not_hide_remaining_multi_attack(self):
        payload = self.load("complete.json")
        ally = payload["root"]["players"][0]
        ally["field"][0].pop("attack_limit", None)
        ally["field"][0]["attacks_left"] = None
        payload["legal_actions"]["attacked_cards"] = [201]
        # The live target projection still contains the attacker.  A Tracker
        # build without attack_limit therefore means another attack is legal,
        # not that the follower is exhausted.
        result = SnapshotAdapter.adapt(payload)
        self.assertTrue(result.trusted)
        self.assertEqual(result.state.my_board[0].attacks_left, 1)

    def test_missing_critical_fields_are_explicitly_untrusted(self):
        result = SnapshotAdapter.adapt(self.load("missing_critical.json"))
        self.assertFalse(result.trusted)
        self.assertFalse(result.usable)
        self.assertTrue(any("pp" in reason for reason in result.trust_reasons))
        self.assertTrue(any("legal_actions" in reason for reason in result.trust_reasons))

    def test_malformed_legal_target_payload_is_untrusted(self):
        payload = self.load("complete.json")
        payload["legal_actions"]["attack_targets"] = None
        result = SnapshotAdapter.adapt(payload)
        self.assertFalse(result.trusted)
        self.assertTrue(any("attack_targets" in reason for reason in result.trust_reasons))

    def test_malformed_turn_and_field_target_payload_are_untrusted(self):
        payload = self.load("complete.json")
        payload["root"]["is_ally_turn"] = "true"
        payload["root"]["players"][0]["field"][0]["attack_targets"] = {"not": "an array"}
        result = SnapshotAdapter.adapt(payload)
        self.assertFalse(result.trusted)
        self.assertTrue(any("is_ally_turn" in reason for reason in result.trust_reasons))
        self.assertTrue(any("field[0].attack_targets" in reason for reason in result.trust_reasons))

    def test_tracker_schema_accepts_public_fixtures(self):
        schema = json.loads((ROOT / "schemas" / "tracker_snapshot.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for filename in ("complete.json", "missing_critical.json", "target_restricted.json"):
            self.assertEqual(list(validator.iter_errors(self.load(filename))), [], filename)

    def test_tracker_schema_accepts_privacy_safe_hidden_opponent_hand(self):
        schema = json.loads((ROOT / "schemas" / "tracker_snapshot.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        payload = self.load("complete.json")
        payload["root"]["players"][1]["hand"] = [{"hidden": True}, {"hidden": True}]
        self.assertEqual(list(validator.iter_errors(payload)), [])

    def test_adapter_accepts_tracker_jsonl_record_envelope(self):
        payload = self.load("complete.json")
        result = SnapshotAdapter.adapt({"timestamp": "2026-09-02T00:00:00Z", "snapshot": payload})
        self.assertTrue(result.trusted)
        self.assertEqual(result.state.enemy_hp, 9)

    def test_adapter_accepts_bare_battle_root(self):
        payload = self.load("complete.json")
        result = SnapshotAdapter.adapt({**payload["root"], "legal_actions": payload["legal_actions"]})
        self.assertTrue(result.trusted)
        self.assertEqual(result.state.turn_number, 7)

    def test_session_refresh_deduplicates_and_exposes_selection(self):
        engine = _CountingEngine()
        session = TrackerLethalSession(engine=engine)
        payload = self.load("complete.json")
        first = session.refresh(payload)
        second = session.refresh(payload)
        self.assertEqual((first.status, first.probability), ("CONFIRMED", 1.0))
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(engine.calls, 1)
        address_only = json.loads(json.dumps(payload))
        address_only["root"]["address"] = "0xreallocated"
        address_only["root"]["players"][0]["address"] = "0xally-reallocated"
        address_only["root"]["players"][0]["field"][0]["address"] = "0xcard-reallocated"
        unchanged = session.refresh(address_only)
        self.assertFalse(unchanged.changed)
        self.assertEqual(engine.calls, 1)

        # Recorded Tracker JSONL envelopes contain a changing timestamp, but
        # that metadata must not force a second solve for identical game state.
        wrapped = session.refresh({"timestamp": "2026-09-02T00:00:01Z", "snapshot": payload})
        self.assertFalse(wrapped.changed)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(first.targets_for(201), (401, 999))
        self.assertTrue(session.select_target(201, 401))
        self.assertFalse(session.select_target(201, 12345))
        self.assertEqual(session.selected_targets, {201: 401})

        stale = json.loads(json.dumps(payload))
        stale["root"]["is_ally_turn"] = False
        view = session.refresh(stale)
        self.assertEqual(view.status, "INCOMPLETE")
        self.assertFalse(view.usable)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(session.selected_targets, {})

    def test_session_converts_unexpected_solver_errors_to_incomplete(self):
        session = TrackerLethalSession(engine=_FailingEngine())
        view = session.refresh(self.load("complete.json"))
        self.assertEqual(view.status, "INCOMPLETE")
        self.assertTrue(any("RuntimeError" in warning for warning in view.warnings))

    def test_session_exposes_max_damage_for_no_lethal(self):
        session = TrackerLethalSession(
            engine=_MaxDamageEngine(),
            rules={"rules": {"10021110": {"card_id": 10021110}}},
        )
        view = session.refresh(self.load("complete.json"))
        self.assertEqual(view.status, "NO_LETHAL")
        self.assertEqual(view.max_damage, 7)
        self.assertEqual(view.max_damage_sequence, ("best non-lethal line",))


if __name__ == "__main__":
    unittest.main()
