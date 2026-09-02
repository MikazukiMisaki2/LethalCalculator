import unittest
from collections import defaultdict
from fractions import Fraction

from event_interpreter import EventInterpreter
from lethal_engine import LethalEngine
from lethal_models import LethalFollower, LethalHandCard, LethalState


class StochasticEffectsTests(unittest.TestCase):
    @staticmethod
    def _brute_force_random_damage(board, hits, amount):
        """Reference implementation for a small sequential random effect.

        This intentionally knows nothing about the production interpreter.  A
        hit samples uniformly from the currently surviving followers, removes
        a follower immediately when its defense reaches zero, and skips later
        activations once the pool is empty.
        """
        current = {tuple(board): Fraction(1, 1)}
        for _ in range(hits):
            next_states = defaultdict(Fraction)
            for state, probability in current.items():
                candidates = [index for index, (_, hp) in enumerate(state) if hp > 0]
                if not candidates:
                    next_states[state] += probability
                    continue
                for index in candidates:
                    updated = list(state)
                    unique_id, hp = updated[index]
                    hp -= amount
                    if hp <= 0:
                        updated.pop(index)
                    else:
                        updated[index] = (unique_id, hp)
                    next_states[tuple(updated)] += probability / len(candidates)
            current = dict(next_states)
        return current

    def test_small_random_effect_matches_independent_bruteforce_oracle(self):
        rules = {"rules": {615: {"card_id": 615, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 3, "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random"}, "amount": 1}]}]}]}]}}}
        card = LethalHandCard(615, 615, "暴力枚举对照", 0, 4)
        initial_board = ((1, 2), (2, 3))
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            hand=[card],
            enemy_board=[
                LethalFollower(1, 700, "A", 1, 2),
                LethalFollower(2, 701, "B", 1, 3),
            ],
        )
        branches = EventInterpreter(rules).play_branches(state, card.unique_id)
        actual = {
            tuple((f.unique_id, f.hp) for f in branch.state.enemy_board): Fraction(branch.probability).limit_denominator(1_000_000)
            for branch in branches
        }
        expected = self._brute_force_random_damage(initial_board, hits=3, amount=1)
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), Fraction(1, 1))
        self.assertEqual(len(branches), 3)

    def test_representative_random_branch_mass_is_one(self):
        """Every Step 6 branch family preserves a complete probability mass."""
        scenarios = []
        damage_rules = {"rules": {616: {"card_id": 616, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "random_choice", "choices": [{"label": "a", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}]}, {"label": "b", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}]}]}]}]}]}}}
        scenarios.append((damage_rules, LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[LethalHandCard(616, 616, "随机选项", 0, 4)]), 616))
        repeat_rules = {"rules": {617: {"card_id": 617, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 2, "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random"}, "amount": 1}]}]}]}]}}}
        scenarios.append((repeat_rules, LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[LethalHandCard(617, 617, "重复", 0, 4)], enemy_board=[LethalFollower(1, 700, "A", 1, 2)]), 617))
        draw_rules = {"rules": {618: {"card_id": 618, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "draw", "count": 1}]}]}]}}}
        draw_card = LethalHandCard(618, 618, "随机抽牌", 0, 4)
        known = LethalHandCard(619, 700, "牌堆牌", 0, 1)
        scenarios.append((draw_rules, LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[draw_card], deck_distribution={700: 2, 701: 1}, total_deck_count=3), 618, {700: known, 701: LethalHandCard(620, 701, "另一张", 0, 1)}))
        for item in scenarios:
            rules, state, source_uid, *rest = item
            card_db = rest[0] if rest else None
            branches = EventInterpreter(rules, card_db=card_db).play_branches(state, source_uid)
            self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0, places=12)

    def test_repeat_recomputes_alive_random_pool_and_merges_states(self):
        rules = {"rules": {"600": {"card_id": 600, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 2, "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random", "count": 1}, "amount": 1}]}]}]}]}}}
        card = LethalHandCard(6, 600, "重复随机伤害", 0, 3)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            enemy_board=[LethalFollower(1, 1, "A", 0, 1), LethalFollower(2, 2, "B", 0, 3)],
            hand=[card],
        )
        branches = EventInterpreter(rules).play_branches(state, 6)
        self.assertAlmostEqual(sum(item.probability for item in branches), 1.0)
        self.assertEqual(len(branches), 2)
        outcomes = {(tuple((f.unique_id, f.hp) for f in branch.state.enemy_board), round(branch.probability, 6)) for branch in branches}
        self.assertIn((((2, 2),), 0.75), outcomes)
        self.assertIn((((1, 1), (2, 1)), 0.25), outcomes)

    def test_random_target_count_is_multiple_fresh_activations(self):
        rules = {"rules": {613: {"card_id": 613, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random", "count": 2}, "amount": 2}]}]}]}}}
        card = LethalHandCard(30, 613, "两次随机", 0, 4)
        state = LethalState(
            enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0,
            enemy_board=[LethalFollower(1, 1, "脆弱", 1, 1), LethalFollower(2, 2, "坚韧", 1, 5)],
            hand=[card],
        )
        branches = EventInterpreter(rules).play_branches(state, card.unique_id)
        outcomes = {(tuple((f.unique_id, f.hp) for f in branch.state.enemy_board), round(branch.probability, 6)) for branch in branches}
        # If the durable follower is selected first, it remains eligible for
        # the second activation; the two-hit outcome is therefore distinct
        # from the path where the 1-defense follower dies first.
        self.assertIn((((2, 3),), 0.75), outcomes)
        self.assertIn((((1, 1), (2, 1)), 0.25), outcomes)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)

    def test_random_damage_to_leader_is_probabilistic(self):
        rules = {"rules": {"601": {"card_id": 601, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 2, "effects": [{"op": "damage", "target": {"scope": "any", "selection": "random", "filters": {"side": "enemy", "card_type": ["follower", "leader"], "zone": "field"}}, "amount": 3}]}]}]}]}}}
        card = LethalHandCard(7, 601, "随机主战者伤害", 0, 3)
        state = LethalState(enemy_hp=5, pp=0, max_pp=0, ep=0, sep=0, enemy_board=[LethalFollower(1, 1, "目标", 0, 10)], hand=[card])
        result = LethalEngine(rules=rules).solve(state)
        self.assertEqual(result.status, "PROBABILISTIC")
        self.assertNotEqual(result.status, "CONFIRMED")
        self.assertLess(result.probability, 1.0)
        self.assertAlmostEqual(result.probability, 0.25)

    def test_random_choice_is_confirmed_when_every_outcome_is_lethal(self):
        rules = {"rules": {610: {"card_id": 610, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "random_choice", "choices": [
            {"label": "four", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 4}]},
            {"label": "five", "effects": [{"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 5}]},
        ]}]}]}]}}}
        card = LethalHandCard(26, 610, "必杀随机选项", 0, 4)
        state = LethalState(enemy_hp=4, pp=0, max_pp=0, ep=0, sep=0, hand=[card])
        result = LethalEngine(rules=rules).solve(state)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.probability, 1.0)

    def test_hidden_random_outcome_that_can_still_kill_is_incomplete(self):
        rules = {"rules": {611: {"card_id": 611, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [
            {"op": "summon", "count": 1, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}}},
            {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 2},
        ]}]}]}}}
        card = LethalHandCard(27, 611, "隐藏随机池", 0, 4)
        known = LethalHandCard(28, 700, "可见随从", 0, 1, 1, 1)
        state = LethalState(enemy_hp=2, pp=0, max_pp=0, ep=0, sep=0, hand=[card], deck_distribution={700: 1}, total_deck_count=2)
        result = LethalEngine(rules=rules, card_db={700: known}).solve(state)
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.probability, 1.0)

    def test_random_draw_uses_deck_multiplicity(self):
        rules = {"rules": {"602": {"card_id": 602, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "draw", "count": 1}]}]}]}}}
        draw = LethalHandCard(8, 602, "抽牌", 0, 3)
        a = LethalHandCard(10, 700, "A", 0, 1)
        b = LethalHandCard(11, 701, "B", 0, 1)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[draw], deck_distribution={700: 1, 701: 3}, total_deck_count=4)
        branches = EventInterpreter(rules, card_db={700: a, 701: b}).play_branches(state, 8)
        probabilities = {branch.state.hand[0].card_id: branch.probability for branch in branches}
        self.assertAlmostEqual(probabilities[700], 0.25)
        self.assertAlmostEqual(probabilities[701], 0.75)

    def test_unknown_draw_mass_is_removed_before_the_next_draw(self):
        rules = {"rules": {608: {"card_id": 608, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "draw", "count": 2}]}]}]}}}
        draw = LethalHandCard(21, 608, "连续抽牌", 0, 4)
        known = LethalHandCard(22, 700, "可见牌", 0, 1)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[draw], deck_distribution={700: 1}, total_deck_count=2)
        branches = EventInterpreter(rules, card_db={700: known}).play_branches(state, draw.unique_id)
        self.assertEqual(len(branches), 1)
        branch = branches[0]
        self.assertEqual(branch.probability, 1.0)
        self.assertEqual(branch.state.total_deck_count, 0)
        self.assertEqual([item.card_id for item in branch.state.hand], [700])
        self.assertIn("draw_unknown", branch.unsupported_ops)

    def test_random_copy_from_ally_hand_expands_the_whole_public_pool(self):
        rules = {"rules": {609: {"card_id": 609, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "copy", "source": {"zone": "hand", "selection": "random", "side": "ally", "filters": {"card_type": "follower"}}, "destination": "hand", "count": 1, "copy_mode": "exact", "preserve_state": True}]}]}]}}}
        spell = LethalHandCard(23, 609, "随机复制手牌", 0, 4)
        first = LethalHandCard(24, 700, "A", 1, 1, 3, 2)
        second = LethalHandCard(25, 701, "B", 2, 1, 5, 4)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[spell, first, second])
        branches = EventInterpreter(rules, card_db={700: first, 701: second}).play_branches(state, spell.unique_id)
        self.assertEqual(len(branches), 2)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertEqual({branch.state.hand[-1].card_id for branch in branches}, {700, 701})
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

    def test_random_distinct_summons_from_deck(self):
        rules = {"rules": {"603": {"card_id": 603, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "summon", "count": 2, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}, "distinct_by": "card_id"}}]}]}]}}}
        summon = LethalHandCard(9, 603, "随机召唤", 0, 3)
        a = LethalHandCard(10, 700, "A", 2, 1, 2, 2)
        b = LethalHandCard(11, 701, "B", 3, 1, 3, 3)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[summon], deck_distribution={700: 2, 701: 1}, total_deck_count=3)
        branches = EventInterpreter(rules, card_db={700: a, 701: b}).play_branches(state, 9)
        self.assertAlmostEqual(sum(item.probability for item in branches), 1.0)
        self.assertEqual({tuple(f.card_id for f in item.state.my_board) for item in branches}, {(700, 701), (701, 700)})

    def test_random_summon_keeps_hidden_deck_mass_as_incomplete_branch(self):
        rules = {"rules": {607: {"card_id": 607, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "summon", "count": 1, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}}}]}]}]}}}
        summon = LethalHandCard(19, 607, "含未知牌堆", 0, 4)
        known = LethalHandCard(20, 700, "已知随从", 2, 1, 2, 2)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[summon], deck_distribution={700: 1}, total_deck_count=2)
        branches = EventInterpreter(rules, card_db={700: known}).play_branches(state, summon.unique_id)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertAlmostEqual(next(branch.probability for branch in branches if branch.state.my_board), 0.5)
        self.assertAlmostEqual(next(branch.probability for branch in branches if not branch.state.my_board), 0.5)
        self.assertIn("summon_unknown", next(branch.unsupported_ops for branch in branches if not branch.state.my_board))

    def test_distinct_summon_is_distinct_within_effect_not_against_existing_board(self):
        rules = {"rules": {605: {"card_id": 605, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "summon", "count": 2, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}, "distinct_by": "card_id"}}]}]}]}}}
        summon = LethalHandCard(13, 605, "不同名召唤", 0, 3)
        a = LethalHandCard(14, 700, "A", 2, 1, 2, 2)
        b = LethalHandCard(15, 701, "B", 3, 1, 3, 3)
        existing = LethalFollower(1, 700, "A already present", 2, 2)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[summon], my_board=[existing], deck_distribution={700: 2, 701: 1}, total_deck_count=3)
        branches = EventInterpreter(rules, card_db={700: a, 701: b}).play_branches(state, summon.unique_id)
        self.assertEqual({tuple(item.card_id for item in branch.state.my_board) for branch in branches}, {(700, 700, 701), (700, 701, 700)})

    def test_empty_random_pool_skips_remaining_repeat(self):
        rules = {"rules": {"604": {"card_id": 604, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 3, "effects": [{"op": "damage", "target": {"scope": "enemy_follower", "selection": "random"}, "amount": 2}]}]}]}]}}}
        card = LethalHandCard(12, 604, "空随机池", 0, 3)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[card])
        branches = EventInterpreter(rules).play_branches(state, 12)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].probability, 1.0)
        self.assertEqual(branches[0].state.enemy_hp, 10)
        self.assertFalse(branches[0].unsupported_ops)

    def test_empty_random_summon_pool_is_skipped(self):
        rules = {"rules": {612: {"card_id": 612, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "summon", "count": 1, "resource_selector": {"zone": "deck", "selection": "random", "filters": {"card_type": "follower"}}}]}]}]}}}
        card = LethalHandCard(29, 612, "空召唤池", 0, 4)
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[card], deck_distribution={701: 1}, total_deck_count=1)
        branches = EventInterpreter(rules).play_branches(state, card.unique_id)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].probability, 1.0)
        self.assertEqual(branches[0].state.my_board, [])
        self.assertFalse(branches[0].unsupported_ops)

    def test_random_copy_from_public_field_merges_by_probability(self):
        rules = {"rules": {606: {"card_id": 606, "support": "generated", "modes": [{"kind": "normal", "cost": 0, "abilities": [{"trigger": "on_play", "effects": [{"op": "copy", "source": {"zone": "field", "selection": "random", "side": "ally", "filters": {"card_type": "follower"}}, "destination": "hand", "count": 1, "copy_mode": "exact", "preserve_state": True}]}]}]}}}
        spell = LethalHandCard(16, 606, "随机复制", 0, 4)
        a = LethalFollower(1, 700, "A", 4, 2)
        b = LethalFollower(2, 701, "B", 7, 5)
        db = {700: LethalHandCard(17, 700, "A", 1, 1, 4, 2), 701: LethalHandCard(18, 701, "B", 2, 1, 7, 5)}
        state = LethalState(enemy_hp=10, pp=0, max_pp=0, ep=0, sep=0, hand=[spell], my_board=[a, b])
        branches = EventInterpreter(rules, card_db=db).play_branches(state, spell.unique_id)
        self.assertEqual({branch.state.hand[0].card_id for branch in branches}, {700, 701})
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))


if __name__ == "__main__":
    unittest.main()
