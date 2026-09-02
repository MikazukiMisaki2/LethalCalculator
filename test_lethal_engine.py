import unittest
from lethal_models import LethalState, LethalFollower, LethalHandCard
from lethal_engine import LethalEngine
from stochastic_calculator import StochasticDamageCalculator

class TestLethalEngine(unittest.TestCase):

    def test_partial_rule_route_returns_incomplete(self):
        card = LethalHandCard(90, 900, "未完整疾驰", cost=1, type=1, atk=3, life=1, static_storm=True)
        rules = {"rules": {"900": {"card_id": 900, "support": "partial", "modes": [{"kind": "normal", "cost": 1, "abilities": []}]}}}
        result = LethalEngine(rules=rules).solve(LethalState(enemy_hp=3, pp=1, max_pp=1, ep=0, sep=0, hand=[card]))
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertTrue(any("partial_rule" in step for step in result.sequence))

    def test_example_1_randall_enhance_gain_storm(self):
        """验证例1：信念腿法·兰德尔 (2费3/2, 爆能5->获得疾驰)。5 PP 下强制爆能打出并走脸斩杀 3 血"""
        randall = LethalHandCard(
            unique_id=1, card_id=10421110, name="信念腿法·兰德尔", 
            cost=2, type=1, atk=3, life=2, 
            enhance_cost=5, enhance_gain_storm=True
        )
        state = LethalState(
            enemy_hp=3, pp=5, max_pp=5, ep=0, sep=0,
            hand=[randall]
        )
        engine = LethalEngine()
        result = engine.solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.probability, 1.0)
        self.assertTrue(any("信念腿法·兰德尔" in s and "走脸" in s for s in result.sequence))

    def test_example_2_loyal_guard_enhance_buff_atk_breaks_ward(self):
        """验证例2：忠烈的近卫兵 (3费3/3, 爆能4->攻击+2变为5/3)。破除 5/5 守护随从后，场上 2/2 疾驰走脸斩杀 2 血"""
        ward = LethalFollower(unique_id=10, card_id=999, name="5/5守护随从", atk=5, hp=5, is_ward=True)
        storm_follower = LethalFollower(
            unique_id=1, card_id=100, name="2/2疾驰随从", atk=2, hp=2, 
            has_storm=True, can_attack_leader=True, can_attack_field=True, attacks_left=1
        )
        guard = LethalHandCard(
            unique_id=2, card_id=10622110, name="忠烈的近卫兵", 
            cost=3, type=1, atk=3, life=3, 
            enhance_cost=4, enhance_buff_atk=2, static_rush=True
        )
        state = LethalState(
            enemy_hp=2, pp=5, max_pp=5, ep=0, sep=0,
            my_board=[storm_follower], enemy_board=[ward], hand=[guard]
        )
        engine = LethalEngine()
        result = engine.solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.probability, 1.0)
        self.assertTrue(any("破坏敌方随从" in s for s in result.sequence))
        self.assertTrue(any("2/2疾驰随从 攻击主战者" in s for s in result.sequence))

    def test_enhance_mandatory_cost_prevents_illegal_play(self):
        """测试：10 PP 下打出瓦路兹强制扣 6 PP，导致剩余 4 PP 无法打出 5 费阿尔贝尔"""
        albert = LethalHandCard(1, 10124110, "雷维翁迅雷·阿尔贝尔", cost=5, type=1, atk=3, life=5, static_storm=True)
        valse = LethalHandCard(2, 10123120, "沉默狙击手·瓦路兹", cost=3, type=1, enhance_cost=6, atk=2, life=1)
        quickblader = LethalHandCard(3, 10021110, "须臾剑士", cost=1, type=1, atk=1, life=1, static_storm=True)
        goliath = LethalFollower(10, 10001130, "激震歌利亚", atk=4, hp=5, is_ward=True)

        state = LethalState(
            enemy_hp=6, pp=10, max_pp=10, ep=1, sep=0,
            my_board=[], enemy_board=[goliath], hand=[valse, albert, quickblader]
        )
        engine = LethalEngine()
        result = engine.solve(state)
        self.assertEqual(result.status, "NO_LETHAL")

    def test_sandalphon_dp_probability_certain_kill(self):
        """测试：圣德芬 5 次 2 伤弹幕对 6 血主战者与 1 个 3 血怪的准确概率为 1.0"""
        prob = StochasticDamageCalculator.calculate_lethal_prob(
            enemy_face_hp=6, enemy_follower_hps=[3], total_hits=5, damage_per_hit=2
        )
        self.assertAlmostEqual(prob, 1.0, places=4)

    def test_max_damage_reports_best_non_lethal_sequence(self):
        storm = LethalFollower(
            unique_id=1,
            card_id=100,
            name="Storm",
            atk=3,
            hp=2,
            has_storm=True,
            can_attack_leader=True,
            attacks_left=1,
        )
        shot = LethalHandCard(
            unique_id=2,
            card_id=101,
            name="Shot",
            cost=1,
            type=4,
            face_damage=2,
        )
        state = LethalState(
            enemy_hp=10,
            pp=1,
            max_pp=1,
            ep=0,
            sep=0,
            my_board=[storm],
            hand=[shot],
        )
        damage, sequence = LethalEngine(max_depth=4).max_damage(state)
        self.assertEqual(damage, 5)
        self.assertEqual(len(sequence), 2)
        self.assertEqual(state.enemy_hp, 10)  # analysis never mutates input

if __name__ == "__main__":
    unittest.main()
