import unittest

from event_interpreter import EventInterpreter
from lethal_models import LethalFollower, LethalHandCard, LethalState


def _rules(card_id, abilities, *, cost=0, static_keywords=()):
    rule = {
        "card_id": card_id,
        "support": "verified",
        "modes": [{"kind": "normal", "cost": cost, "abilities": abilities}],
    }
    if static_keywords:
        rule["static_keywords"] = list(static_keywords)
    return {"rules": {str(card_id): rule}}


class PlannedRuntimeTests(unittest.TestCase):
    def test_remove_keyword_updates_flags_and_statuses(self):
        interpreter = EventInterpreter({})
        follower = LethalFollower(
            1, 10, "Keywords", 4, 4,
            has_storm=True, has_rush=True, is_ward=True,
            can_attack_leader=True, can_attack_field=True,
            statuses=("ambush", "bane", "drain", "rush", "storm", "ward"),
        )
        state = LethalState(10, 0, 0, 0, 0, my_board=[follower])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "remove_keyword", "keyword": "storm", "target": {"scope": "self"}}],
            source_uid=1,
        )
        value = updated.my_board[0]
        self.assertFalse(value.has_storm)
        self.assertTrue(value.has_rush)  # explicit Rush remains
        self.assertFalse(value.can_attack_leader)
        self.assertTrue(value.can_attack_field)
        self.assertNotIn("storm", value.statuses)
        self.assertFalse(unsupported)

    def test_remove_keyword_previous_target_mutates_selected_enemy(self):
        interpreter = EventInterpreter({})
        source = LethalFollower(1, 10, "Source", 1, 1)
        target = LethalFollower(2, 11, "Ward", 2, 2, is_ward=True, statuses=("ward",))
        state = LethalState(10, 0, 0, 0, 0, my_board=[source], enemy_board=[target])
        updated, unsupported, warnings = interpreter._effects(
            state,
            [{"op": "remove_keyword", "keyword": "ward", "target": {"scope": "previous_target", "selection": "chosen"}}],
            source_uid=1,
            target_uid=2,
        )
        self.assertFalse(updated.enemy_board[0].is_ward)
        self.assertNotIn("ward", updated.enemy_board[0].statuses)
        self.assertFalse(unsupported)
        self.assertFalse(warnings)

    def test_remove_storm_keeps_explicit_rush_on_hand_card(self):
        interpreter = EventInterpreter({})
        card = LethalHandCard(3, 12, "Storm Rush", 2, 1, static_storm=True, static_rush=True, statuses=("storm", "rush"))
        state = LethalState(10, 0, 0, 0, 0, hand=[card])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "remove_keyword", "keyword": "storm", "target": {"scope": "hand", "selection": "chosen"}}],
            source_uid=99,
            target_uid=3,
        )
        self.assertFalse(updated.hand[0].static_storm)
        self.assertTrue(updated.hand[0].static_rush)
        self.assertEqual(updated.hand[0].statuses, ("rush",))
        self.assertFalse(unsupported)

    def test_remove_storm_also_removes_implicit_rush_from_follower(self):
        interpreter = EventInterpreter({})
        follower = LethalFollower(
            1, 10, "Storm only", 3, 3,
            has_storm=True, has_rush=True,
            can_attack_leader=True, can_attack_field=True,
            statuses=("storm",),
        )
        state = LethalState(10, 0, 0, 0, 0, my_board=[follower])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "remove_keyword", "keyword": "storm", "target": {"scope": "self"}}],
            source_uid=1,
        )
        value = updated.my_board[0]
        self.assertFalse(value.has_storm)
        self.assertFalse(value.has_rush)
        self.assertFalse(value.can_attack_leader)
        self.assertFalse(value.can_attack_field)
        self.assertEqual(value.statuses, ())
        self.assertFalse(unsupported)

    def test_remove_all_abilities_clears_keyword_flags_and_listeners(self):
        interpreter = EventInterpreter({})
        follower = LethalFollower(
            1, 10, "Ward Storm", 3, 3,
            has_storm=True, has_rush=True, is_ward=True,
            can_attack_leader=True, can_attack_field=True,
            statuses=("storm", "ward"),
            last_words=({"trigger": "on_last_word", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 5}]},),
        )
        target = LethalFollower(2, 11, "Target", 1, 5, is_ward=False)
        state = LethalState(10, 0, 0, 0, 0, my_board=[follower], enemy_board=[target])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "remove_abilities", "target": {"scope": "self"}}],
            source_uid=1,
        )
        value = updated.my_board[0]
        self.assertTrue(value.abilities_removed)
        self.assertFalse(value.has_storm)
        self.assertFalse(value.has_rush)
        self.assertFalse(value.is_ward)
        self.assertFalse(value.can_attack_leader)
        self.assertFalse(value.can_attack_field)
        self.assertEqual(value.statuses, ())
        self.assertEqual(value.last_words, ())
        # A later turn event must not execute the printed listener.
        after_turn = interpreter.end_turn(updated).state
        self.assertEqual(after_turn.enemy_hp, 10)
        self.assertFalse(unsupported)

    def test_bane_combat_destroys_survivor_and_drain_heals_actual_damage(self):
        interpreter = EventInterpreter({})
        attacker = LethalFollower(
            1, 10, "Bane Drain", 3, 4,
            can_attack_field=True, attacks_left=1,
            statuses=("bane", "drain"),
        )
        target = LethalFollower(2, 11, "Large Target", 1, 7)
        state = LethalState(10, 0, 0, 0, 0, my_board=[attacker], enemy_board=[target], ally_hp=10, ally_max_hp=20)
        result = interpreter.attack_follower(state, 1, 2)
        self.assertEqual(result.state.enemy_board, [])
        self.assertEqual(result.state.my_board[0].hp, 3)
        self.assertEqual(result.state.ally_hp, 13)
        self.assertFalse(result.unsupported_ops)

    def test_status_only_keyword_projection_is_combat_usable(self):
        interpreter = EventInterpreter({})
        attacker = LethalFollower(1, 10, "Status Storm", 3, 3, attacks_left=1, statuses=("storm",))
        target = LethalFollower(2, 11, "Status Ward", 1, 2, statuses=("ward",))
        state = LethalState(10, 0, 0, 0, 0, my_board=[attacker], enemy_board=[target])
        result = interpreter.attack_follower(state, 1, 2)
        self.assertEqual(result.state.enemy_board, [])
        self.assertFalse(result.unsupported_ops)

    def test_drain_leader_attack_and_ambush_break(self):
        interpreter = EventInterpreter({})
        attacker = LethalFollower(
            1, 10, "Hidden Drain", 4, 4,
            can_attack_leader=True, attacks_left=1,
            statuses=("ambush", "drain"),
        )
        state = LethalState(10, 0, 0, 0, 0, my_board=[attacker], ally_hp=5, ally_max_hp=10)
        result = interpreter.attack_leader(state, 1)
        self.assertEqual(result.state.enemy_hp, 6)
        self.assertEqual(result.state.ally_hp, 9)
        self.assertNotIn("ambush", result.state.my_board[0].statuses)
        self.assertFalse(result.state.my_board[0].has_ambush)
        self.assertFalse(result.unsupported_ops)

    def test_status_only_ward_blocks_leader_attack(self):
        interpreter = EventInterpreter({})
        attacker = LethalFollower(1, 10, "Storm", 3, 3, has_storm=True, can_attack_leader=True)
        ward = LethalFollower(2, 11, "Ward", 1, 2, statuses=("ward",))
        state = LethalState(10, 0, 0, 0, 0, my_board=[attacker], enemy_board=[ward])
        result = interpreter.attack_leader(state, 1)
        self.assertEqual(result.state.enemy_hp, 10)
        self.assertTrue(result.warnings)

    def test_status_only_storm_survives_auto_evolve_as_leader_attack_permission(self):
        interpreter = EventInterpreter({})
        attacker = LethalFollower(1, 10, "Storm", 3, 3, statuses=("storm",), attacks_left=1)
        state = LethalState(10, 0, 0, 0, 0, my_board=[attacker])
        evolved = interpreter.auto_evolve(state, 1)
        self.assertTrue(evolved.state.my_board[0].can_attack_leader)

    def test_ambush_blocks_targeted_ability_but_area_damage_hits(self):
        interpreter = EventInterpreter({})
        source = LethalFollower(1, 1, "Source", 1, 1)
        hidden = LethalFollower(2, 2, "Ambush", 1, 4, statuses=("ambush",))
        state = LethalState(10, 0, 0, 0, 0, my_board=[source], enemy_board=[hidden])
        targeted, _, warnings = interpreter._effects(
            state,
            [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "chosen"}, "amount": 2}],
            source_uid=1,
            target_uid=2,
        )
        self.assertEqual(targeted.enemy_board[0].hp, 4)
        self.assertTrue(warnings)
        area, unsupported, _ = interpreter._effects(
            state,
            [{"op": "damage", "target": {"scope": "any", "selection": "all", "filters": {"side": "enemy", "card_type": "follower"}}, "amount": 2}],
            source_uid=1,
        )
        self.assertEqual(area.enemy_board[0].hp, 2)
        self.assertFalse(unsupported)

    def test_progressive_sequence_advances_per_entity(self):
        rules = _rules(20, [{
            "trigger": "on_turn_end",
            "effects": [{"op": "progressive_sequence", "steps": [
                {"label": "one", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]},
                {"label": "two", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]},
            ]}],
        }])
        interpreter = EventInterpreter(rules)
        state = LethalState(10, 0, 0, 0, 0, my_board=[LethalFollower(7, 20, "Sequence", 0, 1)])
        first = interpreter.end_turn(state).state
        second = interpreter.end_turn(first).state
        third = interpreter.end_turn(second).state
        self.assertEqual((first.enemy_hp, second.enemy_hp, third.enemy_hp), (9, 7, 7))
        self.assertEqual(third.my_board[0].progressive_sequence_index, 2)

    def test_invoke_from_known_deck_resolves_fanfare_without_pp(self):
        card = LethalHandCard(99, 30, "Invoked", 3, 1, 2, 2)
        rules = _rules(30, [{"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]}], cost=3)
        interpreter = EventInterpreter(rules, card_db={30: card})
        state = LethalState(10, 0, 0, 0, 0, deck_distribution={30: 1}, total_deck_count=1)
        result = interpreter._effects(state, [{"op": "invoke", "card_id": 30, "from_zone": "deck", "target": {"scope": "self"}}], 0)
        updated, unsupported, _ = result
        self.assertEqual(updated.enemy_hp, 8)
        self.assertEqual(updated.pp, 0)
        self.assertEqual(updated.deck_distribution.get(30), 0)
        self.assertFalse(unsupported)

    def test_invoke_specific_trigger_does_not_fire_on_normal_play(self):
        card = LethalHandCard(5, 30, "Invoke-only", 1, 1, 2, 2)
        rule = _rules(30, [
            {"trigger": "on_invoke", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3}]},
            {"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]},
        ], cost=1)["rules"]["30"]
        interpreter = EventInterpreter({"rules": {"30": rule}}, card_db={30: card})
        played = interpreter.play(LethalState(10, 1, 1, 0, 0, hand=[card]), 5)
        self.assertEqual(played.state.enemy_hp, 9)
        state = LethalState(10, 0, 0, 0, 0, deck_distribution={30: 1}, total_deck_count=1)
        invoked = interpreter._effects(state, [{"op": "invoke", "card_id": 30, "from_zone": "deck", "target": {"scope": "self"}}], 0)
        self.assertEqual(invoked[0].enemy_hp, 7)
        self.assertFalse(invoked[1])

    def test_invoke_from_selected_hand_entity_does_not_use_board_source(self):
        source = LethalFollower(1, 99, "Invoker", 1, 1)
        hand_card = LethalHandCard(99, 30, "Hand Follower", 3, 1, 2, 2)
        rules = {
            "rules": {
                "30": _rules(30, [{
                    "trigger": "on_fanfare",
                    "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}],
                }], cost=3)["rules"]["30"],
            }
        }
        interpreter = EventInterpreter(rules, card_db={30: hand_card})
        state = LethalState(10, 0, 0, 0, 0, my_board=[source], hand=[hand_card])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "invoke", "from_zone": "hand", "target": {"scope": "hand", "selection": "chosen"}}],
            source_uid=1,
            target_uid=99,
        )
        self.assertEqual(updated.enemy_hp, 8)
        self.assertEqual([card.unique_id for card in updated.hand], [])
        self.assertEqual([f.card_id for f in updated.my_board], [99, 30])
        self.assertFalse(unsupported)

    def test_invoke_self_from_hand_listener_consumes_the_hand_entity(self):
        card = LethalHandCard(40, 32, "Hand Invoke", 0, 1, 1, 1)
        rules = _rules(32, [{
            "trigger": "on_fanfare",
            "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}],
        }], cost=0)["rules"]["32"]
        interpreter = EventInterpreter({"rules": {"32": rules}}, card_db={32: card})
        state = LethalState(10, 0, 0, 0, 0, hand=[card])
        updated, unsupported, _ = interpreter._effects(
            state,
            [{"op": "invoke", "target": {"scope": "self"}}],
            source_uid=40,
        )
        self.assertEqual(updated.enemy_hp, 9)
        self.assertEqual(updated.hand, [])
        self.assertEqual([f.card_id for f in updated.my_board], [32])
        self.assertFalse(unsupported)

    def test_random_invoke_uses_visible_deck_multiplicity(self):
        card_a = LethalHandCard(30, 30, "A", 1, 1, 1, 1)
        card_b = LethalHandCard(31, 31, "B", 1, 1, 2, 2)
        rules_a = _rules(30, [], cost=1)["rules"]["30"]
        rules_b = _rules(31, [], cost=1)["rules"]["31"]
        interpreter = EventInterpreter({"rules": {"30": rules_a, "31": rules_b}}, card_db={30: card_a, 31: card_b})
        state = LethalState(10, 0, 0, 0, 0, deck_distribution={30: 2, 31: 1}, total_deck_count=3)
        effect = {
            "op": "invoke",
            "from_zone": "deck",
            "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}},
            "target": {"scope": "self"},
        }
        branches = interpreter._effects_branches(state, [effect], source_uid=0)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        by_card = {branch.state.my_board[0].card_id: branch.probability for branch in branches}
        self.assertAlmostEqual(by_card[30], 2 / 3)
        self.assertAlmostEqual(by_card[31], 1 / 3)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

    def test_invoke_branch_preserves_invoked_fanfare_randomness(self):
        card_a = LethalHandCard(30, 30, "A", 1, 1, 1, 1)
        card_b = LethalHandCard(31, 31, "B", 1, 1, 1, 1)
        random_fanfare = [{
            "trigger": "on_fanfare",
            "effects": [{"op": "damage", "target": {"scope": "enemy_leader", "selection": "random"}, "amount": 1}],
        }]
        rules = {"rules": {
            "30": _rules(30, random_fanfare, cost=1)["rules"]["30"],
            "31": _rules(31, random_fanfare, cost=1)["rules"]["31"],
        }}
        interpreter = EventInterpreter(rules, card_db={30: card_a, 31: card_b})
        state = LethalState(10, 0, 0, 0, 0, deck_distribution={30: 1}, total_deck_count=1)
        # The random Fanfare has only one legal target (the enemy leader), so
        # it is still deterministic but must not be reported as random_target.
        branches = interpreter._effects_branches(state, [{"op": "invoke", "from_zone": "deck", "card_id": 30, "target": {"scope": "self"}}], source_uid=0)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].state.enemy_hp, 9)
        self.assertNotIn("random_target", branches[0].unsupported_ops)

    def test_invoke_this_card_is_exact_not_a_random_whole_deck_draw(self):
        card = LethalHandCard(20, 30, "Self Invoker", 1, 1, 1, 1)
        rules = _rules(30, [], cost=1)["rules"]["30"]
        interpreter = EventInterpreter({"rules": {"30": rules}}, card_db={30: card})
        source = LethalFollower(7, 30, "Self Invoker", 1, 1)
        state = LethalState(10, 0, 0, 0, 0, my_board=[source], deck_distribution={30: 1, 31: 9}, total_deck_count=10)
        branches = interpreter._effects_branches(state, [{"op": "invoke", "target": {"scope": "self"}}], source_uid=7)
        self.assertEqual(len(branches), 1)
        self.assertAlmostEqual(branches[0].probability, 1.0)
        self.assertEqual([f.card_id for f in branches[0].state.my_board], [30, 30])
        self.assertNotIn("invoke_unknown_pool", branches[0].unsupported_ops)

    def test_played_base_cost_condition_and_replace_deck_draw_gap(self):
        source = LethalHandCard(1, 31, "Spell", 1, 4)
        rules = _rules(31, [], cost=1)
        interpreter = EventInterpreter(rules, card_db={31: source})
        state = LethalState(10, 1, 1, 0, 0, hand=[source])
        played = interpreter.play(state, 1).state
        self.assertEqual(played.played_base_costs, (1,))
        self.assertTrue(interpreter._condition_met(played, {"state": "played_base_cost_set", "cmp": "contains_all", "value": [1]}))
        replaced, unsupported, _ = interpreter._effects(played, [{"op": "replace_deck", "replacement": "the Apocalypse Deck"}], 0)
        self.assertEqual(replaced.deck_replacement, "the Apocalypse Deck")
        self.assertEqual(replaced.deck_distribution, {})
        branches = interpreter._draw_branches(replaced, {"op": "draw", "count": 1}, 0)
        self.assertTrue(any("draw_replaced_deck" in branch.unsupported_ops for branch in branches))
        self.assertFalse(unsupported)

    def test_modify_previous_effect_replays_only_modified_operation(self):
        interpreter = EventInterpreter({})
        state = LethalState(10, 0, 0, 0, 0)
        updated, unsupported, _ = interpreter._effects(state, [
            {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2},
            {"op": "modify_previous_effect", "field": "amount", "value": 5},
        ], 0)
        self.assertEqual(updated.enemy_hp, 5)
        self.assertFalse(unsupported)

    def test_modify_previous_effect_also_works_across_stochastic_branches(self):
        interpreter = EventInterpreter({})
        state = LethalState(10, 0, 0, 0, 0)
        branches = interpreter._effects_branches(state, [
            {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2},
            {"op": "modify_previous_effect", "field": "amount", "value": 5},
        ], source_uid=0)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].state.enemy_hp, 5)
        self.assertNotIn("modify_previous_effect_context", branches[0].unsupported_ops)


if __name__ == "__main__":
    unittest.main()
