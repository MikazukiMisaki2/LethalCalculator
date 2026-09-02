import unittest
from dataclasses import replace

from event_interpreter import EventInterpreter
from lethal_models import LethalFollower, LethalHandCard, LethalState


class EventInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.rules = {"rules": {"100": {"card_id": 100, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}, {"op": "recover_pp", "amount": 1}]}]}]}}}
        self.state = LethalState(enemy_hp=5, pp=3, max_pp=5, ep=1, sep=0, hand=[LethalHandCard(10, 100, "测试随从", 2, 1, 2, 2)])

    def test_play_resolves_damage_and_pp(self):
        result = EventInterpreter(self.rules).play(self.state, 10)
        self.assertEqual(result.state.enemy_hp, 3)
        self.assertEqual(result.state.pp, 2)
        self.assertEqual(len(result.state.my_board), 1)
        self.assertFalse(result.unsupported_ops)

    def test_play_resolves_fanfare_trigger(self):
        rules = {"rules": {"100": {"card_id": 100, "support": "verified", "modes": [{"kind": "normal", "cost": 2, "abilities": [{"trigger": "on_fanfare", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]}]}]}}}
        result = EventInterpreter(rules).play(self.state, 10)
        self.assertEqual(result.state.enemy_hp, 3)

    def test_playing_spell_triggers_allied_spellboost(self):
        rules = {"rules": {
            "401": {"card_id": 401, "support": "verified", "modes": [{"kind": "normal", "abilities": [
                {"trigger": "on_spellboost", "effects": [{"op": "buff", "target": {"scope": "self"}, "attack": 1, "life": 1}]}
            ]}]},
            "402": {"card_id": 402, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": []}]}
        }}
        state = LethalState(
            enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0,
            my_board=[LethalFollower(1, 401, "增幅随从", 2, 2)],
            hand=[LethalHandCard(2, 402, "法术", 1, 4)],
        )
        result = EventInterpreter(rules).play(state, 2)
        self.assertEqual((result.state.my_board[0].atk, result.state.my_board[0].hp), (3, 3))
        self.assertFalse(result.unsupported_ops)

    def test_engage_advances_countdown(self):
        rules = {"rules": {403: {"card_id": 403, "support": "verified", "modes": [{"kind": "normal", "abilities": [
            {"trigger": "on_engage", "effects": [{"op": "modify_counter", "field": "countdown", "delta": 1}]}
        ]}]}}}
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0,
                            my_board=[LethalFollower(7, 403, "倒计时护符", 0, 1, countdown=3)])
        result = EventInterpreter(rules).engage(state, 7)
        self.assertEqual(result.state.my_board[0].countdown, 2)
        self.assertFalse(result.unsupported_ops)

    def test_ward_blocks_leader_attack(self):
        state = self.state.clone()
        state.my_board = [LethalFollower(1, 1, "攻击者", 3, 3, has_storm=True, can_attack_leader=True)]
        state.enemy_board = [LethalFollower(2, 2, "守护", 1, 3, is_ward=True)]
        result = EventInterpreter().attack_leader(state, 1)
        self.assertEqual(result.state.enemy_hp, 5)
        self.assertTrue(result.warnings)

    def test_evolve_consumes_ep_and_adds_stats(self):
        state = self.state.clone()
        state.my_board = [LethalFollower(1, 1, "进化者", 2, 2, has_storm=True)]
        result = EventInterpreter().evolve(state, 1)
        self.assertEqual(result.state.ep, 0)
        self.assertEqual(result.state.my_board[0].atk, 4)
        self.assertTrue(result.state.my_board[0].is_evolved)

    def test_spell_clears_follower_and_super_evolve_storm(self):
        rules = {"rules": {
            "300": {"card_id": 300, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_play", "effects": [{"op": "destroy", "target": {"scope": "enemy_follower"}}]}]}]},
            "301": {"card_id": 301, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_super_evolve", "effects": [{"op": "gain_status", "status": "storm"}]}]}]},
        }}
        state = LethalState(
            enemy_hp=5, pp=1, max_pp=1, ep=0, sep=1,
            my_board=[LethalFollower(1, 301, "超进化者", 2, 2)],
            enemy_board=[LethalFollower(2, 999, "守护", 5, 5, is_ward=True)],
            hand=[LethalHandCard(3, 300, "解场法术", 1, 3)],
        )
        from lethal_engine import LethalEngine
        result = LethalEngine(rules=rules).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("目标 2" in step for step in result.sequence))
        self.assertTrue(any("超进化" in step for step in result.sequence))

    def test_selected_enemy_follower_damage_and_evolve_trigger(self):
        rules = {"rules": {
            "302": {"card_id": 302, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_follower"}, "amount": 4}]}]}]},
            "303": {"card_id": 303, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_evolve", "effects": [{"op": "damage", "target": {"scope": "enemy_follower"}, "amount": 3}]}]}]},
        }}
        spell = LethalHandCard(4, 302, "随机点杀", 1, 3)
        state = LethalState(
            enemy_hp=2, pp=1, max_pp=1, ep=1, sep=0,
            my_board=[LethalFollower(1, 303, "进化者", 2, 2, has_storm=True)],
            enemy_board=[LethalFollower(2, 998, "目标A", 1, 4), LethalFollower(3, 999, "目标B", 1, 1)],
            hand=[spell],
        )
        interpreter = EventInterpreter(rules)
        played = interpreter.play(state, 4, target_uid=3)
        self.assertEqual([f.unique_id for f in played.state.enemy_board], [2])
        evolved = interpreter.evolve(played.state, 1, target_uid=2)
        self.assertEqual(evolved.state.enemy_board[0].hp, 1)

    def test_split_damage_allocation(self):
        rules = {"rules": {"304": {"card_id": 304, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "all", "allocation": "split"}, "amount": 5}]}]}]}}}
        state = LethalState(
            enemy_hp=10, pp=3, max_pp=3, ep=0, sep=0,
            enemy_board=[LethalFollower(1, 1, "A", 1, 3), LethalFollower(2, 2, "B", 1, 3)],
            hand=[LethalHandCard(9, 304, "分配伤害", 3, 3)],
        )
        result = EventInterpreter(rules).play(state, 9, target_uid={1: 2, 2: 3})
        self.assertEqual(result.state.enemy_board[0].hp, 1)
        self.assertEqual(len(result.state.enemy_board), 1)

    def test_split_damage_follows_enemy_board_order(self):
        rules = {"rules": {"305": {"card_id": 305, "modes": [{"kind": "normal", "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "all", "allocation": "ordered_split"}, "amount": 6}]}]}]}}}
        from lethal_engine import LethalEngine
        engine = LethalEngine(rules=rules)
        card = LethalHandCard(10, 305, "顺序分配", 1, 3)
        for hps, expected in [([3, 1, 1], []), ([7, 1, 1], [1, 1, 1]), ([1, 1, 7], [3])]:
            state = LethalState(enemy_hp=20, pp=1, max_pp=1, ep=0, sep=0, hand=[card], enemy_board=[LethalFollower(i + 1, i + 1, str(i), 1, hp) for i, hp in enumerate(hps)])
            options = engine._target_options(state, card, False)
            result = EventInterpreter(rules).play(state, 10, target_uid=options[0])
            self.assertEqual([f.hp for f in result.state.enemy_board], expected)

    def test_condition_is_checked_before_effect(self):
        rules = {"rules": {"306": {"card_id": 306, "support": "generated", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "condition": {"state": "rally", "cmp": "gte", "value": 5}, "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3}]}]}]}}}
        card = LethalHandCard(11, 306, "条件伤害", 1, 3)
        low = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, rally=4, hand=[card])
        high = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, rally=5, hand=[card])
        self.assertEqual(EventInterpreter(rules).play(low, 11).state.enemy_hp, 10)
        self.assertEqual(EventInterpreter(rules).play(high, 11).state.enemy_hp, 7)

    def test_random_target_is_never_silently_deterministic(self):
        rules = {"rules": {"307": {"card_id": 307, "support": "generated", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random", "count": 1}, "amount": 3}]}]}]}}}
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, enemy_board=[LethalFollower(1, 1, "A", 1, 3)], hand=[LethalHandCard(12, 307, "随机伤害", 1, 3)])
        result = EventInterpreter(rules).play(state, 12, target_uid=1)
        self.assertIn("random_target", result.unsupported_ops)

    def test_variable_amount_is_incomplete(self):
        rules = {"rules": {"309": {"card_id": 309, "support": "generated", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": "variable"}]}]}]}}}
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, hand=[LethalHandCard(14, 309, "变量伤害", 1, 3)])
        result = EventInterpreter(rules).play(state, 14)
        self.assertIn("variable_amount", result.unsupported_ops)

    def test_variable_sources_and_resource_updates(self):
        rules = {"rules": {
            "310": {"card_id": 310, "support": "generated", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": "var:hand_tribe:fairy"}, {"op": "modify_resource", "resource": "faith", "amount": 2}]}]}]},
        }}
        fairy = LethalHandCard(15, 1, "Fairy", 1, 1, tribes=("5",))
        spell = LethalHandCard(16, 310, "变量资源", 1, 3)
        state = LethalState(enemy_hp=10, pp=2, max_pp=2, ep=0, sep=0, hand=[spell, fairy], faith=3)
        result = EventInterpreter(rules).play(state, 16)
        self.assertEqual(result.state.enemy_hp, 9)
        self.assertEqual(result.state.faith, 5)

    def test_attack_life_buff_fields_are_applied(self):
        rules = {"rules": {"308": {"card_id": 308, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "buff", "target": {"scope": "self"}, "attack": 2, "life": 3}]}]}]}}}
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, hand=[LethalHandCard(13, 308, "自我强化", 1, 1, 1, 1)])
        result = EventInterpreter(rules).play(state, 13)
        self.assertEqual((result.state.my_board[0].atk, result.state.my_board[0].hp), (3, 4))

    def test_play_modes_are_mutually_exclusive(self):
        rules = {"rules": {"500": {"card_id": 500, "support": "verified", "modes": [
            {"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]}]},
            {"kind": "enhance", "cost": 3, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3}]}]},
            {"kind": "accelerate", "cost": 2, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 5}]}]},
        ]}}}
        card = LethalHandCard(50, 500, "多模式", 1, 3, enhance_cost=3, accelerate_cost=2)
        base = LethalState(enemy_hp=10, pp=3, max_pp=3, ep=0, sep=0, hand=[card])
        interpreter = EventInterpreter(rules)
        normal = interpreter.play(base, 50, mode="normal")
        enhanced = interpreter.play(base, 50, mode="enhance")
        accelerated = interpreter.play(base, 50, mode="accelerate")
        self.assertEqual(normal.state.enemy_hp, 9)
        self.assertEqual(enhanced.state.enemy_hp, 7)
        self.assertEqual(accelerated.state.enemy_hp, 5)
        self.assertEqual(normal.state.pp, 2)
        self.assertEqual(enhanced.state.pp, 0)
        self.assertEqual(accelerated.state.pp, 1)

    def test_engine_treats_mode_choice_as_player_choice_not_random(self):
        rules = {"rules": {"505": {"card_id": 505, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "mode_choice", "choices": [{"label": "damage", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 4}]}, {"label": "blank", "effects": []}]}]}]}]}}}
        card = LethalHandCard(55, 505, "选择模式", 1, 3)
        state = LethalState(enemy_hp=4, pp=1, max_pp=1, ep=0, sep=0, hand=[card])
        result = __import__("lethal_engine").LethalEngine(rules=rules).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertTrue(any("选项damage" in step for step in result.sequence))

    def test_mode_choice_can_banish_selected_field_category(self):
        rules = {"rules": {"506": {"card_id": 506, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "mode_choice", "choices": [{"label": "followers", "effects": [{"op": "banish", "target": {"scope": "any", "selection": "all", "filters": {"zone": "field", "card_type": "follower", "exclude_source": True}}}]}, {"label": "crests", "effects": [{"op": "destroy_crest", "target": {"scope": "any", "selection": "all", "filters": {"zone": "crests"}}}]}]}]}]}]}}}
        card = LethalHandCard(56, 506, "清场模式", 0, 1, 1, 1)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
                            my_board=[LethalFollower(55, 99, "友方", 1, 1)],
                            enemy_board=[LethalFollower(57, 98, "敌方", 1, 1)],
                            hand=[card])
        result = EventInterpreter(rules).play(state, 56, choice="followers")
        self.assertEqual([f.unique_id for f in result.state.my_board], [56])
        self.assertEqual(result.state.enemy_board, [])

    def test_last_words_nested_copy_preserves_entity_state(self):
        rules = {"rules": {"507": {"card_id": 507, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "gain_status", "status": "Last Words: Summon a copy of this card.", "target": {"scope": "self"}, "ability": {"trigger": "on_last_word", "effects": [{"op": "copy", "source": {"scope": "trigger_source"}, "destination": "field", "count": 1, "copy_mode": "exact", "preserve_state": True}]}}]}]}]}}}
        card = LethalHandCard(57, 507, "遗言复制", 0, 1, 4, 6)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[card])
        interpreter = EventInterpreter(rules)
        played = interpreter.play(state, 57)
        self.assertEqual(len(played.state.my_board), 1)
        self.assertTrue(played.state.my_board[0].last_words)
        after_destroy = interpreter._effects(played.state, [{"op": "destroy", "target": {"scope": "self"}}], 57)[0]
        self.assertEqual(len(after_destroy.my_board), 1)
        self.assertEqual((after_destroy.my_board[0].atk, after_destroy.my_board[0].hp), (4, 6))

    def test_resource_threshold_boundaries(self):
        def make(condition):
            return {"rules": {"501": {"card_id": 501, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "condition": condition, "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]}]}]}}}
        card = LethalHandCard(51, 501, "临界条件", 1, 1)
        cases = [
            ({"state": "rally", "cmp": "gte", "value": 5}, {"rally": 4}, {"rally": 5}),
            ({"state": "cemetery", "cmp": "gte", "value": 6}, {"cemetery": 5}, {"cemetery": 6}),
            ({"state": "necromancy", "cmp": "gte", "value": 6}, {"cemetery": 5}, {"cemetery": 6}),
            ({"state": "awakening", "cmp": "eq", "value": True}, {"max_pp": 6}, {"max_pp": 7}),
            ({"state": "earth_sigil", "cmp": "gte", "value": 1}, {"earth_sigil": 0}, {"earth_sigil": 1}),
            ({"state": "skybound_art", "cmp": "gte", "value": 1}, {"skybound_art": 0}, {"skybound_art": 1}),
            ({"state": "super_skybound_art", "cmp": "gte", "value": 1}, {"super_skybound_art": 0}, {"super_skybound_art": 1}),
        ]
        for condition, low_values, high_values in cases:
            low = LethalState(enemy_hp=10, pp=1, max_pp=low_values.pop("max_pp", 1), ep=0, sep=0, hand=[card], **low_values)
            high = LethalState(enemy_hp=10, pp=1, max_pp=high_values.pop("max_pp", 1), ep=0, sep=0, hand=[card], **high_values)
            self.assertEqual(EventInterpreter(make(condition)).play(low, 51).state.enemy_hp, 10, condition)
            self.assertEqual(EventInterpreter(make(condition)).play(high, 51).state.enemy_hp, 8, condition)

    def test_resource_consumption_is_atomic_when_insufficient(self):
        rules = {"rules": {"502": {"card_id": 502, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "consume_resource", "resource": "earth_sigil", "amount": 2}, {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 4}]}]}]}}}
        card = LethalHandCard(52, 502, "消耗土之印", 1, 3)
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, earth_sigil=1, hand=[card])
        result = EventInterpreter(rules).play(state, 52)
        self.assertEqual(result.state.earth_sigil, 1)
        self.assertEqual(result.state.enemy_hp, 10)
        self.assertIn("insufficient_resource:earth_sigil", result.unsupported_ops)

    def test_resource_preflight_blocks_effects_before_failed_payment(self):
        rules = {"rules": {508: {"card_id": 508, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 4}, {"op": "consume_resource", "resource": "cemetery", "amount": 2}]}]}]}}}
        card = LethalHandCard(58, 508, "预检消耗", 0, 4)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, cemetery=1, hand=[card])
        result = EventInterpreter(rules).play(state, card.unique_id)
        self.assertEqual(result.state.enemy_hp, 10)
        self.assertEqual(result.state.hand[0].unique_id, card.unique_id)
        self.assertIn("insufficient_resource:cemetery", result.unsupported_ops)

    def test_resource_preflight_accumulates_multiple_payments(self):
        rules = {"rules": {513: {"card_id": 513, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [
            {"op": "consume_resource", "resource": "cemetery", "amount": 1},
            {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2},
            {"op": "consume_resource", "resource": "cemetery", "amount": 1},
            {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 5},
        ]}]}]}}}
        card = LethalHandCard(63, 513, "累计墓地消耗", 0, 4)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, cemetery=1, hand=[card])
        result = EventInterpreter(rules).play(state, card.unique_id)
        # The second payment sees the first payment in the shadow state, so
        # the whole card remains unusable and neither preceding damage fires.
        self.assertEqual(result.state.enemy_hp, 10)
        self.assertEqual(result.state.cemetery, 1)
        self.assertEqual(result.state.hand[0].unique_id, card.unique_id)
        self.assertIn("insufficient_resource:cemetery", result.unsupported_ops)

    def test_faith_instances_and_mode_selection(self):
        rules = {"rules": {"503": {"card_id": 503, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": []}]}}}
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, faith=3,
            faith_instances=[
                {"source_card_id": 10, "unique_id": 100, "value": 1, "abilities": [{"trigger": "on_mode_selected", "effects": [{"op": "modify_resource", "resource": "faith", "amount": 1}]}]},
                {"source_card_id": 11, "unique_id": 101, "value": 2, "abilities": []},
            ],
            hand=[LethalHandCard(53, 503, "信仰", 0, 3)],
        )
        result = EventInterpreter(rules).select_mode(state)
        # Each independent Faith instance observes the mode selection.  The
        # first also has a granted +1 listener, so both values are retained
        # after the listener's state clone.
        self.assertEqual([item["value"] for item in result.state.faith_instances], [3, 3])
        self.assertEqual(result.state.faith, 6)
        self.assertFalse(result.unsupported_ops)

    def test_playing_a_selected_mode_emits_faith_event_once(self):
        rules = {"rules": {511: {"card_id": 511, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{
            "trigger": "on_play", "effects": [{"op": "mode_choice", "choices": [
                {"label": "one", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]},
                {"label": "two", "effects": []},
            ]}]
        }]}]}}}
        card = LethalHandCard(61, 511, "模式事件", 0, 4)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[card], faith=2,
            faith_instances=[{"source_card_id": 10, "unique_id": 10, "value": 2, "abilities": []}],
        )
        # The Faith instance models the standard “when a Mode is selected”
        # listener; the card play should increment it exactly once.
        state.faith_instances[0]["abilities"] = [{"trigger": "on_mode_selected", "effects": [{"op": "modify_resource", "resource": "faith", "amount": 1, "source_card_id": 10}]}]
        result = EventInterpreter(rules).play(state, card.unique_id, choice="one")
        self.assertEqual(result.state.enemy_hp, 9)
        self.assertEqual(result.state.faith_instances[0]["value"], 4)
        self.assertEqual(result.state.faith, 4)

    def test_crest_instances_countdown_and_multi_instance(self):
        rules = {"rules": {"504": {"card_id": 504, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "gain_crest", "card_id": 9001, "player": "ally"}]}]}]}}}
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            crest_instances=[{"card_id": 1, "unique_id": 1, "countdown": 2}, {"card_id": 2, "unique_id": 2, "countdown": 3}],
            active_crests=[1, 2], hand=[LethalHandCard(54, 504, "纹章", 0, 3)],
        )
        interpreter = EventInterpreter(rules)
        gained = interpreter.play(state, 54)
        self.assertEqual(len(gained.state.crest_instances), 3)
        advanced = interpreter.end_turn(gained.state)
        self.assertEqual([item["card_id"] for item in advanced.state.crest_instances], [1, 2, 9001])
        advanced = interpreter.end_turn(advanced.state)
        self.assertEqual([item["card_id"] for item in advanced.state.crest_instances], [2, 9001])
        advanced = interpreter.end_turn(advanced.state)
        self.assertEqual([item["card_id"] for item in advanced.state.crest_instances], [9001])

    def test_earth_rite_alias_and_faith_cost_gate(self):
        rules = {"rules": {
            "509": {"card_id": 509, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "condition": {"state": "earth_rite", "cmp": "gte", "value": 1}, "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]}
            ]}]},
            "510": {"card_id": 510, "support": "verified", "modes": [{"kind": "normal", "cost": 0, "abilities": [
                {"trigger": "on_play", "effects": [
                    {"op": "modify_resource", "resource": "faith", "amount": -2},
                    {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 4},
                ]}
            ]}]},
        }}
        interpreter = EventInterpreter(rules)
        earth = LethalHandCard(59, 509, "土之印条件", 0, 4)
        low = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, earth_sigil=0, hand=[earth])
        high = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, earth_sigil=1, hand=[earth])
        self.assertEqual(interpreter.play(low, earth.unique_id).state.enemy_hp, 10)
        self.assertEqual(interpreter.play(high, earth.unique_id).state.enemy_hp, 9)

        faith_card = LethalHandCard(60, 510, "信仰费用", 0, 4)
        insufficient = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, faith=1, hand=[faith_card])
        blocked = interpreter.play(insufficient, faith_card.unique_id)
        self.assertEqual(blocked.state.enemy_hp, 10)
        self.assertEqual(blocked.state.hand[0].unique_id, faith_card.unique_id)
        self.assertIn("insufficient_resource:faith", blocked.unsupported_ops)
        payable = replace(insufficient, faith=2)
        paid = interpreter.play(payable, faith_card.unique_id)
        self.assertEqual(paid.state.enemy_hp, 6)
        self.assertEqual(paid.state.faith, 0)

    def test_countdown_amulet_is_not_counted_as_spell_cemetery(self):
        rules = {"rules": {512: {"card_id": 512, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": []}]}}}
        catalog = {"cards": {"512": {"card_id": 512, "type": "countdown_amulet"}}}
        card = LethalHandCard(62, 512, "倒计时护符", 1, 3)
        state = LethalState(enemy_hp=10, pp=1, max_pp=1, ep=0, sep=0, hand=[card])
        result = EventInterpreter(rules, catalog=catalog).play(state, card.unique_id)
        self.assertEqual(result.state.cemetery, 0)

    def test_crest_split_uses_only_remaining_damage_on_leader(self):
        catalog = {"cards": {"9000": {"card_id": 9000, "alt_modes": [{
            "type_key": "crest", "text": {"eng": "At the end of your turn, if allied followers didn't attack this turn, deal X damage split between all enemies. X is the number of crests you have."}
        }]}}}
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            enemy_board=[LethalFollower(1, 1, "A", 1, 2)],
            crest_instances=[{"card_id": 9000, "unique_id": 1, "countdown": 3}, {"card_id": 9000, "unique_id": 2, "countdown": 3}],
            active_crests=[9000, 9000],
        )
        interpreter = EventInterpreter(catalog=catalog)
        # Two Crest instances => X=2 for each trigger.  The first spends all
        # damage on the 2-defense follower; the second has no follower left
        # and deals only its remaining 2 to the leader.
        result = interpreter.end_turn(state)
        self.assertEqual(result.state.enemy_board, [])
        self.assertEqual(result.state.enemy_hp, 8)

    def test_crest_does_not_trigger_when_attacker_dies_in_combat(self):
        catalog = {"cards": {"9000": {"card_id": 9000, "alt_modes": [{
            "type_key": "crest", "text": {"eng": "At the end of your turn, if allied followers didn't attack this turn, deal X damage split between all enemies. X is the number of crests you have."}
        }]}}}
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            my_board=[LethalFollower(10, 10, "attacker", 1, 1, can_attack_field=True)],
            enemy_board=[LethalFollower(11, 11, "defender", 3, 3)],
            crest_instances=[{"card_id": 9000, "unique_id": 1, "countdown": 3}],
            active_crests=[9000],
        )
        interpreter = EventInterpreter(catalog=catalog)
        attacked = interpreter.attack_follower(state, 10, 11)
        self.assertTrue(attacked.state.attacked_with_follower_this_turn)
        ended = interpreter.end_turn(attacked.state)
        self.assertEqual(ended.state.enemy_board[0].hp, 2)
        self.assertEqual(ended.state.enemy_hp, 10)


if __name__ == "__main__":
    unittest.main()
