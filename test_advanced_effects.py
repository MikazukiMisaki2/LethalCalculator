import unittest

from event_interpreter import EventInterpreter
from lethal_engine import LethalEngine
from lethal_models import LethalFollower, LethalHandCard, LethalState
from snapshot_adapter import SnapshotAdapter


class AdvancedEffectTests(unittest.TestCase):
    def test_reanimate_branches_over_destroyed_follower_multiplicity(self):
        rules = {"rules": {
            "1": {"card_id": 1, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "reanimate", "cost": 2}]}]}]},
            "2": {"card_id": 2, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
            "3": {"card_id": 3, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(10, 1, "Reanimator", 0, 4)
        db = {1: source, 2: LethalHandCard(20, 2, "A", 2, 1, 2, 2), 3: LethalHandCard(21, 3, "B", 2, 1, 1, 3)}
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[source], cemetery=2,
            destroyed_pool_known=True,
            destroyed_this_match=[
                LethalFollower(100, 2, "A", 2, 2, base_cost=2),
                LethalFollower(101, 3, "B", 1, 3, base_cost=2),
            ],
        )
        branches = EventInterpreter(rules, card_db=db).play_branches(state, source.unique_id)
        self.assertEqual({branch.state.my_board[0].card_id for branch in branches}, {2, 3})
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        # Cemetery is the Shadow count.  Reanimate selects from the destroyed
        # pool but does not spend Shadow; only playing the spell adds one.
        self.assertTrue(all(branch.state.cemetery == 3 for branch in branches))
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

    def test_reanimate_probability_uses_duplicate_pool_multiplicity(self):
        rules = {"rules": {
            1: {"card_id": 1, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "reanimate", "cost": 2}]}]}]},
            2: {"card_id": 2, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
            3: {"card_id": 3, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
            4: {"card_id": 4, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(10, 1, "Reanimator", 0, 4)
        db = {
            1: source,
            2: LethalHandCard(20, 2, "A", 2, 1, 2, 2),
            3: LethalHandCard(21, 3, "B", 2, 1, 1, 3),
            4: LethalHandCard(22, 4, "C", 2, 1, 3, 1),
        }
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[source], cemetery=4,
            destroyed_pool_known=True,
            destroyed_this_match=[
                LethalFollower(100, 2, "A", 2, 2, base_cost=2),
                LethalFollower(101, 2, "A", 2, 2, base_cost=2),
                LethalFollower(102, 2, "A", 2, 2, base_cost=2),
                LethalFollower(103, 3, "B", 1, 3, base_cost=2),
                LethalFollower(104, 4, "C", 3, 1, base_cost=2),
            ],
        )
        branches = EventInterpreter(rules, card_db=db).play_branches(state, source.unique_id)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        probabilities = {branch.state.my_board[0].card_id: branch.probability for branch in branches}
        self.assertEqual(set(probabilities), {2, 3, 4})
        self.assertAlmostEqual(probabilities[2], 3 / 5)
        self.assertAlmostEqual(probabilities[3], 1 / 5)
        self.assertAlmostEqual(probabilities[4], 1 / 5)
        self.assertTrue(all([item.card_id for item in branch.state.destroyed_this_match] == [2, 2, 2, 3, 4] for branch in branches))

    def test_reanimate_keeps_pool_entry_for_later_reanimate(self):
        rules = {"rules": {
            4: {"card_id": 4, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "effects": [{"op": "reanimate", "cost": 2, "count": 2}]}
            ]}]},
            5: {"card_id": 5, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(40, 4, "Double Reanimate", 0, 4)
        dead = LethalFollower(500, 5, "Dead Follower", 2, 2, base_cost=2)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[source],
            cemetery=7, destroyed_pool_known=True, destroyed_this_match=[dead],
        )
        branches = EventInterpreter(rules, card_db={4: source, 5: LethalHandCard(50, 5, "Dead Follower", 2, 1, 2, 2)}).play_branches(state, source.unique_id)
        self.assertEqual(len(branches), 1)
        self.assertEqual([f.card_id for f in branches[0].state.my_board], [5, 5])
        self.assertEqual([f.card_id for f in branches[0].state.destroyed_this_match], [5])
        self.assertEqual(branches[0].state.cemetery, 8)
        self.assertFalse(branches[0].unsupported_ops)

    def test_consuming_shadow_does_not_remove_destroyed_pool_entry(self):
        rules = {"rules": {
            6: {"card_id": 6, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "effects": [
                    {"op": "consume_resource", "resource": "cemetery", "amount": 2},
                    {"op": "reanimate", "cost": 2},
                ]}
            ]}]},
            7: {"card_id": 7, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(60, 6, "Consume and Reanimate", 0, 4)
        dead = LethalFollower(600, 7, "Dead Follower", 2, 2, base_cost=2)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[source], cemetery=2,
            destroyed_pool_known=True, destroyed_this_match=[dead],
        )
        result = EventInterpreter(
            rules,
            card_db={6: source, 7: LethalHandCard(61, 7, "Dead Follower", 2, 1, 2, 2)},
        ).play(state, source.unique_id)
        self.assertEqual(result.state.cemetery, 1)  # 2 consumed, then source spell +1
        self.assertEqual([f.card_id for f in result.state.my_board], [7])
        self.assertEqual([f.card_id for f in result.state.destroyed_this_match], [7])
        self.assertFalse(result.unsupported_ops)

    def test_reanimate_unknown_history_is_incomplete_but_known_empty_is_noop(self):
        rules = {"rules": {1: {"card_id": 1, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "reanimate", "cost": 2}]}]}]}}}
        source = LethalHandCard(10, 1, "Reanimator", 0, 4)
        interpreter = EventInterpreter(rules, card_db={1: source})
        unknown = LethalState(10, 0, 0, 0, 0, hand=[source])
        self.assertIn("reanimate_unknown_pool", interpreter.play(unknown, 10).unsupported_ops)
        known_empty = unknown.clone()
        known_empty.destroyed_pool_known = True
        self.assertNotIn("reanimate_unknown_pool", interpreter.play(known_empty, 10).unsupported_ops)

    def test_spellboost_updates_hand_counter_cost_and_on_spellboost_effect(self):
        rules = {"rules": {
            10: {"card_id": 10, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": []}]},
            11: {"card_id": 11, "support": "verified", "modes": [{"kind": "normal", "cost": 3, "abilities": [
                {"trigger": "on_spellboost", "effects": [
                    {"op": "modify_cost", "target": {"scope": "self"}, "amount": -1},
                    {"op": "buff", "target": {"scope": "self"}, "attack": 1, "life": 1},
                ]}
            ]}]},
        }}
        spell = LethalHandCard(1, 10, "Spell", 0, 4)
        target = LethalHandCard(2, 11, "Boostable", 3, 1, 2, 2)
        state = LethalState(10, 0, 0, 0, 0, hand=[spell, target])
        result = EventInterpreter(rules).play(state, spell.unique_id)
        boosted = result.state.hand[0]
        self.assertEqual((boosted.spell_boost_count, boosted.cost, boosted.atk, boosted.life), (1, 2, 3, 3))
        self.assertFalse(result.unsupported_ops)

        direct = EventInterpreter(rules)._effects(
            state, [{"op": "spellboost", "target": {"scope": "any", "selection": "chosen", "filters": {"zone": "hand"}}, "count": 2}],
            source_uid=spell.unique_id, target_uid=target.unique_id,
        )
        self.assertEqual((direct[0].hand[1].spell_boost_count, direct[0].hand[1].cost), (2, 1))

    def test_spellboost_updates_hand_variable_x_for_followup_damage(self):
        rules = {"rules": {
            16: {"card_id": 16, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": []}]},
            17: {"card_id": 17, "support": "verified", "variables": {"X": 0}, "modes": [{"kind": "normal", "cost": 6, "abilities": [
                {"trigger": "on_spellboost", "effects": [{"op": "modify_counter", "field": "variable_x", "delta": 1}]},
                {"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": "var:X"}]},
            ]}]},
        }}
        spell = LethalHandCard(1, 16, "Spell", 0, 4)
        x_card = LethalHandCard(2, 17, "X Damage", 6, 1, 1, 1)
        state = LethalState(10, 6, 6, 0, 0, hand=[spell, x_card])
        result = EventInterpreter(rules).play(state, spell.unique_id)
        boosted = result.state.hand[0]
        self.assertEqual((boosted.spell_boost_count, boosted.variable_x), (1, 1))
        played = EventInterpreter(rules).play(result.state, boosted.unique_id)
        self.assertEqual(played.state.enemy_hp, 9)
        self.assertFalse(played.unsupported_ops)

    def test_hand_targeted_buff_does_not_touch_board_followers(self):
        rules = {"rules": {
            12: {"card_id": 12, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "effects": [{"op": "buff", "target": {"scope": "ally_follower", "selection": "all", "filters": {"zone": "hand"}}, "attack": 2, "life": 1}]}
            ]}]},
        }}
        source = LethalHandCard(1, 12, "Hand Buff", 0, 4)
        in_hand = LethalHandCard(2, 13, "Hand Target", 1, 1, 2, 2)
        on_board = LethalFollower(3, 13, "Board Target", 2, 2)
        state = LethalState(10, 0, 0, 0, 0, hand=[source, in_hand], my_board=[on_board])
        result = EventInterpreter(rules).play(state, source.unique_id)
        self.assertEqual((result.state.hand[0].atk, result.state.hand[0].life), (4, 3))
        self.assertEqual((result.state.my_board[0].atk, result.state.my_board[0].hp), (2, 2))

    def test_board_targeted_buff_does_not_touch_hand_cards(self):
        rules = {"rules": {
            14: {"card_id": 14, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "effects": [{"op": "buff", "target": {"scope": "ally_follower", "selection": "all", "filters": {"zone": "field"}}, "attack": 2, "life": 1}]}
            ]}]},
        }}
        source = LethalHandCard(1, 14, "Board Buff", 0, 4)
        in_hand = LethalHandCard(2, 15, "Hand Target", 1, 1, 2, 2)
        on_board = LethalFollower(3, 15, "Board Target", 2, 2)
        state = LethalState(10, 0, 0, 0, 0, hand=[source, in_hand], my_board=[on_board])
        result = EventInterpreter(rules).play(state, source.unique_id)
        self.assertEqual((result.state.hand[0].atk, result.state.hand[0].life), (2, 2))
        self.assertEqual((result.state.my_board[0].atk, result.state.my_board[0].hp), (4, 3))

    def test_static_transform_replaces_identity_without_cemetery_or_last_words(self):
        rules = {"rules": {
            20: {"card_id": 20, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "transform", "target": {"scope": "enemy_follower", "selection": "chosen"}, "card_id": 21}]}]}]},
            21: {"card_id": 21, "support": "verified", "modes": [{"kind": "normal", "cost": 5, "abilities": []}]},
        }}
        source = LethalHandCard(1, 20, "Transformer", 0, 4)
        target = LethalFollower(9, 99, "Old", 1, 1, attacks_left=0, last_words=({"trigger": "on_last_word", "effects": []},))
        template = LethalHandCard(2, 21, "New", 5, 1, 5, 5)
        state = LethalState(10, 0, 0, 0, 0, hand=[source], enemy_board=[target], cemetery=0)
        result = EventInterpreter(rules, card_db={20: source, 21: template}).play(state, source.unique_id, target_uid=target.unique_id)
        transformed = result.state.enemy_board[0]
        self.assertEqual((transformed.unique_id, transformed.card_id, transformed.atk, transformed.hp, transformed.attacks_left), (9, 21, 5, 5, 0))
        # The transforming card is a spell, so its own play contributes one
        # cemetery entry; transforming the enemy permanent is not a destroy.
        self.assertEqual(result.state.cemetery, 1)
        self.assertFalse(transformed.last_words)
        self.assertFalse(result.unsupported_ops)

    def test_transform_from_known_deck_branches_and_hidden_mass_is_incomplete(self):
        rules = {"rules": {
            30: {"card_id": 30, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "transform", "target": {"scope": "ally_follower", "selection": "all", "filters": {"zone": "field", "card_type": "follower"}}, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}}}]}]}]},
            31: {"card_id": 31, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
            32: {"card_id": 32, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(1, 30, "Deck Transformer", 0, 4)
        db = {30: source, 31: LethalHandCard(2, 31, "One", 2, 1, 4, 4), 32: LethalHandCard(3, 32, "Two", 2, 1, 6, 6)}
        ally = LethalFollower(9, 99, "Old", 1, 1)
        state = LethalState(10, 0, 0, 0, 0, hand=[source], my_board=[ally], deck_distribution={31: 1, 32: 1}, total_deck_count=2)
        branches = EventInterpreter(rules, card_db=db).play_branches(state, source.unique_id)
        self.assertEqual({branch.state.my_board[0].card_id for branch in branches}, {31, 32})
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))
        hidden = state.clone()
        hidden.total_deck_count = 3
        hidden_branches = EventInterpreter(rules, card_db=db).play_branches(hidden, source.unique_id)
        self.assertTrue(any("transform_unknown" in branch.unsupported_ops for branch in hidden_branches))

    def test_dynamic_transform_in_engage_branches_and_opponent_deck_is_unknown(self):
        rules = {"rules": {
            33: {"card_id": 33, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{
                "trigger": "on_engage", "cost": 0, "effects": [{"op": "destroy", "target": {"scope": "self"}}, {"op": "transform", "target": {"scope": "any", "selection": "chosen", "filters": {"zone": "hand"}}, "resource_selector": {"side": "ally", "zone": "deck", "selection": "random", "filters": {"card_type": "follower"}}}
                ]
            }]}]},
            34: {"card_id": 34, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": []}]},
            35: {"card_id": 35, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        amulet = LethalFollower(1, 33, "Transformer Amulet", 0, 1, countdown=1)
        hand_target = LethalHandCard(2, 34, "Hand Target", 1, 1, 1, 1)
        replacement_a = LethalHandCard(3, 35, "Replacement A", 2, 1, 3, 3)
        state = LethalState(10, 0, 0, 0, 0, my_board=[amulet], hand=[hand_target], deck_distribution={35: 1}, total_deck_count=1)
        interpreter = EventInterpreter(rules, card_db={33: LethalHandCard(1, 33, "Transformer Amulet", 0, 2), 34: hand_target, 35: replacement_a})
        branches = interpreter.engage_branches(state, 1, target_uid=2)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].state.hand[0].card_id, 35)
        self.assertFalse(branches[0].unsupported_ops)

        opponent_rule = {"rules": {36: {"card_id": 36, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{
            "trigger": "on_play", "effects": [{"op": "transform", "target": {"scope": "hand", "selection": "chosen", "filters": {"zone": "hand"}}, "resource_selector": {"side": "enemy", "zone": "deck", "selection": "random"}}]
        }]}]}}}
        source = LethalHandCard(4, 36, "Opponent Deck Transform", 0, 4)
        target = LethalHandCard(5, 34, "Target", 1, 1, 1, 1)
        unknown_state = LethalState(10, 0, 0, 0, 0, hand=[source, target])
        unknown_branches = EventInterpreter(opponent_rule, card_db={36: source, 34: hand_target}).play_branches(unknown_state, 4, target_uid=5)
        self.assertTrue(any("transform_unknown" in item.unsupported_ops for item in unknown_branches))

    def test_replicate_fanfare_on_evolve_and_engage(self):
        rules = {"rules": {
            40: {"card_id": 40, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]},
                {"trigger": "on_evolve", "effects": [{"op": "replicate_ability", "trigger": "on_fanfare"}]},
            ]}]},
            41: {"card_id": 41, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3}]},
                {"trigger": "on_engage", "effects": [{"op": "destroy", "target": {"scope": "self"}}, {"op": "replicate_ability", "trigger": "on_fanfare"}]},
            ]}]},
        }}
        follower = LethalHandCard(1, 40, "Replicator", 0, 1, 2, 2)
        state = LethalState(10, 0, 0, 1, 0, hand=[follower])
        interpreter = EventInterpreter(rules)
        played = interpreter.play(state, follower.unique_id)
        evolved = interpreter.evolve(played.state, follower.unique_id)
        self.assertEqual(evolved.state.enemy_hp, 6)
        self.assertFalse(evolved.unsupported_ops)

        amulet = LethalHandCard(2, 41, "Engage Replicator", 0, 2)
        engage_state = LethalState(10, 0, 0, 0, 0, hand=[], my_board=[LethalFollower(2, 41, "Engage Replicator", 0, 1, countdown=1)])
        engaged = interpreter.engage(engage_state, 2)
        self.assertEqual(engaged.state.enemy_hp, 7)
        self.assertEqual(engaged.state.my_board, [])
        self.assertFalse(engaged.unsupported_ops)

    def test_engine_expands_random_effect_inside_selected_mode(self):
        rules = {"rules": {
            50: {"card_id": 50, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{
                "trigger": "on_play", "effects": [{"op": "mode_choice", "choices": [
                    {"label": "1", "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random"}, "amount": 1}]},
                    {"label": "2", "effects": [{"op": "reanimate", "cost": 2}]},
                ]}],
            }]}]},
            51: {"card_id": 51, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": []}]},
        }}
        source = LethalHandCard(1, 50, "Mode Reanimate", 0, 4)
        dead = LethalHandCard(2, 51, "Stormy Dead", 2, 1, 2, 2, static_storm=True)
        state = LethalState(
            enemy_hp=2, pp=0, max_pp=0, ep=0, sep=0, hand=[source], cemetery=1,
            destroyed_pool_known=True,
            destroyed_this_match=[LethalFollower(100, 51, "Stormy Dead", 2, 2, base_cost=2)],
        )
        result = LethalEngine(card_db={50: source, 51: dead}, rules=rules).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.probability, 1.0)


class SnapshotHistoryTests(unittest.TestCase):
    def test_adapter_reads_tracker_destroyed_ids_and_spellboost_fields(self):
        catalog = {"cards": {
            "101": {"card_id": 101, "name": {"eng": "Boostable"}, "type": "follower", "cost": 3, "stats": {"attack": 2, "life": 2}, "tribes": []},
            "201": {"card_id": 201, "name": {"eng": "Dead One"}, "type": "follower", "cost": 2, "stats": {"attack": 1, "life": 1}, "tribes": []},
            "202": {"card_id": 202, "name": {"eng": "Dead Two"}, "type": "follower", "cost": 4, "stats": {"attack": 4, "life": 4}, "tribes": []},
            "301": {"card_id": 301, "name": {"eng": "A Spell"}, "type": "spell", "cost": 1, "stats": {"attack": 0, "life": 0}, "tribes": []},
        }}
        snapshot = {"root": {"is_ally_turn": True, "players": [
            {"life": 20, "max_life": 20, "pp": 3, "max_pp": 3, "hand": [{"unique_id": 7, "card_id": 101, "cost": 3, "attack": 2, "life": 2, "card_type": 1, "has_spell_boost": True, "spell_boost_count": 4, "variable_x": 4, "supplement_info": {"boost": 4}}, {"unique_id": 8, "card_id": 301, "cost": 1, "attack": 0, "life": 0, "card_type": 4}], "field": [], "destroyed_card_ids": [[201, 0], [201, 0], [202, 3]], "cemetery_count": 3},
            {"life": 10, "max_life": 10, "hand": [], "field": []},
        ]}, "legal_actions": {}}
        result = SnapshotAdapter.adapt(snapshot, catalog=catalog)
        self.assertTrue(result.state.destroyed_pool_known)
        self.assertTrue(result.state.destroyed_pool_exact)
        self.assertEqual([item.card_id for item in result.state.destroyed_this_match], [201, 201, 202])
        self.assertEqual((result.state.hand[0].spell_boost_count, result.state.hand[0].variable_x, result.state.hand[0].supplement_info), (4, 4, (("boost", 4),)))
        self.assertEqual(result.state.hand[0].enhance_costs, ())
        self.assertEqual(result.state.hand[1].type, 4)
        self.assertEqual([item.unique_id for item in result.state.destroyed_this_match], [-1, -2, -3])

    def test_adapter_keyword_mapping_ignores_disabled_flags(self):
        catalog = {"cards": {
            "101": {"card_id": 101, "name": {"eng": "Keywords"}, "type": "follower", "cost": 1, "stats": {"attack": 1, "life": 1}, "tribes": []},
        }}
        snapshot = {"root": {"is_ally_turn": True, "players": [
            {"life": 10, "max_life": 10, "pp": 1, "max_pp": 1, "hand": [], "field": [
                {"unique_id": 1, "card_id": 101, "attack": 1, "life": 1, "keywords": {"storm": True, "bane": False}}
            ]},
            {"life": 10, "max_life": 10, "hand": [], "field": []},
        ]}, "legal_actions": {}}
        result = SnapshotAdapter.adapt(snapshot, catalog=catalog)
        follower = result.state.my_board[0]
        self.assertIn("storm", follower.statuses)
        self.assertNotIn("bane", follower.statuses)
        self.assertFalse(follower.has_bane)


if __name__ == "__main__":
    unittest.main()
