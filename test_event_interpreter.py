import unittest

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


if __name__ == "__main__":
    unittest.main()
