import json
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from event_interpreter import EventInterpreter
from lethal_models import LethalFollower, LethalHandCard, LethalState, create_hand_card_from_rule


ROOT = Path(__file__).resolve().parent


class GeneratedComplexRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = json.loads((ROOT / "data/generated/card_rules_v2.json").read_text(encoding="utf-8"))
        cls.catalog = json.loads((ROOT / "data/generated/card_catalog.json").read_text(encoding="utf-8"))
        cls.cards = cls.catalog["cards"]

    def make_card(self, card_id: int, unique_id: int) -> LethalHandCard:
        meta = self.cards[str(card_id)]
        rule = self.rules["rules"][str(card_id)]
        type_id = {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}[meta["type"]]
        return create_hand_card_from_rule(
            card_id,
            dict(
                rule,
                cost=meta.get("cost", 0),
                type=type_id,
                atk=meta.get("stats", {}).get("attack", 0),
                life=meta.get("stats", {}).get("life", 0),
                name=meta.get("name", {}).get("eng", str(card_id)),
            ),
            unique_id,
        )

    def follower_from_catalog(self, card_id: int, unique_id: int, *, hp=None) -> LethalFollower:
        card = self.make_card(card_id, unique_id)
        rule = self.rules["rules"].get(str(card_id), {})
        static = rule.get("static_keywords", ())
        return LethalFollower(
            unique_id=unique_id,
            card_id=card_id,
            name=card.name,
            atk=card.atk,
            hp=card.life if hp is None else hp,
            has_storm="storm" in static,
            has_rush="rush" in static or "storm" in static,
            is_ward="ward" in static,
            can_attack_leader="storm" in static,
            can_attack_field="rush" in static or "storm" in static,
        )

    def find_cards(self, *, card_type=None, class_id=None, max_cost=None, limit=4):
        result = []
        for raw_id, meta in self.cards.items():
            if card_type and meta.get("type") != card_type:
                continue
            if class_id is not None and meta.get("class_id") != class_id:
                continue
            if max_cost is not None and int(meta.get("cost", 0) or 0) > max_cost:
                continue
            result.append(int(raw_id))
            if len(result) >= limit:
                break
        return result

    def test_cupitan_repeat_random_recomputes_pool(self):
        card = self.make_card(10413110, 1000)
        enemies = [LethalFollower(i, 900 + i, f"enemy-{i}", 1, 3) for i in range(1, 4)]
        state = LethalState(
            enemy_hp=20,
            pp=4,
            max_pp=4,
            ep=1,
            sep=0,
            hand=[card],
            enemy_board=enemies,
            skybound_art=1,
            super_skybound_art=1,
        )
        branches = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card}).play_branches(state, card.unique_id)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0)
        self.assertGreater(len(branches), 1)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

    def test_cupitan_seven_random_damage_matches_golden_fixture(self):
        """Cupitan's seven hits are checked against an exact distribution.

        The fixture uses three equal-defense followers so the result is small
        enough to enumerate exhaustively while still exercising target-pool
        refresh, immediate deaths, and state merging.
        """
        fixture = json.loads((ROOT / "fixtures/stochastic/cupitan_7_random.json").read_text(encoding="utf-8"))
        source_uid = int(fixture["source_uid"])
        cupitan = self.follower_from_catalog(fixture["card_id"], source_uid)
        enemies = [
            LethalFollower(
                int(item["unique_id"]), int(item["card_id"]), f"enemy-{item['unique_id']}",
                int(item.get("atk", 1)), int(item["hp"]),
            )
            for item in fixture["enemy_board"]
        ]
        state = LethalState(
            enemy_hp=20,
            pp=0,
            max_pp=0,
            ep=int(fixture["ep"]),
            sep=0,
            my_board=[cupitan],
            enemy_board=enemies,
        )
        interpreter = EventInterpreter(self.rules, catalog=self.catalog)
        branches = interpreter.evolve_branches(state, source_uid)
        self.assertAlmostEqual(sum(branch.probability for branch in branches), 1.0, places=12)
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

        actual = {
            tuple((f.unique_id, f.hp) for f in branch.state.enemy_board): branch.probability
            for branch in branches
        }
        expected = {
            tuple((int(item["unique_id"]), int(item["hp"])) for item in outcome["enemy_board"]): Fraction(
                int(outcome["probability"]["numerator"]),
                int(outcome["probability"]["denominator"]),
            )
            for outcome in fixture["expected_distribution"]
        }
        self.assertEqual(set(actual), set(expected))
        for board, probability in expected.items():
            self.assertEqual(Fraction(actual[board]).limit_denominator(1_000_000), probability)
        self.assertEqual(sum(expected.values()), Fraction(1, 1))

    def test_bahamut_mode_choice_executes_one_branch(self):
        card = self.make_card(10804110, 1001)
        follower_id = self.find_cards(card_type="follower", limit=1)[0]
        amulet_id = self.find_cards(card_type="amulet", limit=1)[0]
        state = LethalState(
            enemy_hp=20,
            pp=9,
            max_pp=9,
            ep=0,
            sep=0,
            hand=[card],
            enemy_board=[self.follower_from_catalog(follower_id, 2001), self.follower_from_catalog(amulet_id, 2002)],
            crest_instances=[{"card_id": 1, "unique_id": 1, "countdown": 2}],
            active_crests=[1],
        )
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card})
        followers = interpreter.play(state, card.unique_id, choice="1")
        self.assertEqual([item.unique_id for item in followers.state.enemy_board], [2002])
        self.assertFalse(followers.unsupported_ops)
        amulets = interpreter.play(state, card.unique_id, choice="2")
        self.assertEqual([item.unique_id for item in amulets.state.enemy_board], [2001])
        self.assertFalse(amulets.unsupported_ops)
        crests = interpreter.play(state, card.unique_id, choice="3")
        self.assertEqual(crests.state.crest_instances, [])

    def test_sham_faith_and_super_evolve_exact_copy(self):
        card = self.make_card(10354110, 1002)
        target_card_id = self.find_cards(card_type="follower", limit=1)[0]
        target = self.follower_from_catalog(target_card_id, 2100, hp=4)
        target_template = self.make_card(target_card_id, 2101)
        state = LethalState(
            enemy_hp=20,
            pp=2,
            max_pp=2,
            ep=0,
            sep=1,
            faith=15,
            faith_instances=[{"source_card_id": 42, "unique_id": 42, "value": 15, "abilities": []}],
            hand=[card],
            enemy_board=[target],
        )
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card, target_card_id: target_template})
        played = interpreter.play(state, card.unique_id)
        self.assertEqual(played.state.faith, 5)
        evolved = interpreter.evolve(played.state, card.unique_id, super_evolve=True, target_uid=target.unique_id)
        self.assertEqual(evolved.state.enemy_board, [])
        self.assertEqual(len(evolved.state.hand), 1)
        self.assertEqual(evolved.state.hand[0].card_id, target.card_id)
        self.assertFalse(evolved.unsupported_ops)

    def test_sham_faith_mode_bonus_is_not_a_faith_value_tick(self):
        card = self.make_card(10354110, 10020)
        mode_card = self.make_card(10413310, 10021)
        state = LethalState(
            enemy_hp=20, pp=4, max_pp=4, ep=0, sep=0, faith=10,
            hand=[card, mode_card],
        )
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card, mode_card.card_id: mode_card})
        played = interpreter.play(state, card.unique_id)
        # Satisfying the -10 Faith cost leaves the source Faith at zero and
        # grants a Mode-capacity listener, not a +1 Faith-value listener.
        self.assertEqual(played.state.faith, 0)
        self.assertEqual(played.state.faith_instances[0].get("mode_limit_bonus"), 0)
        selected = interpreter.play(played.state, mode_card.unique_id, choice="2")
        self.assertEqual(selected.state.faith, 1)
        self.assertEqual(selected.state.faith_instances[0].get("mode_limit_bonus"), 1)

    def test_sophia_random_havencraft_summon_uses_catalog_class(self):
        card = self.make_card(10462120, 1003)
        candidates = self.find_cards(card_type="follower", class_id=6, max_cost=2, limit=2)
        self.assertGreaterEqual(len(candidates), 1)
        card_db = {card.card_id: card}
        for index, card_id in enumerate(candidates, 1):
            card_db[card_id] = self.make_card(card_id, 3000 + index)
        state = LethalState(enemy_hp=20, pp=4, max_pp=4, ep=0, sep=0, hand=[card], deck_distribution={card_id: 1 for card_id in candidates}, total_deck_count=len(candidates))
        branches = EventInterpreter(self.rules, catalog=self.catalog, card_db=card_db).play_branches(state, card.unique_id)
        self.assertAlmostEqual(sum(item.probability for item in branches), 1.0)
        self.assertTrue(all(branch.state.my_board[-1].card_id in candidates for branch in branches))
        self.assertTrue(all(not branch.unsupported_ops for branch in branches))

    def test_eld_tome_summons_distinct_destroyed_amulets(self):
        card = self.make_card(10664110, 1004)
        candidates = self.find_cards(card_type="amulet", max_cost=2, limit=2)
        self.assertGreaterEqual(len(candidates), 2)
        destroyed = []
        card_db = {card.card_id: card}
        for index, card_id in enumerate(candidates, 1):
            template = self.make_card(card_id, 4000 + index)
            card_db[card_id] = template
            destroyed.append(self.follower_from_catalog(card_id, 4100 + index, hp=1))
            destroyed[-1] = replace(destroyed[-1], statuses=("last_words",), last_words=({"trigger": "on_last_word", "effects": []},))
        state = LethalState(enemy_hp=20, pp=4, max_pp=4, ep=0, sep=0, hand=[card], destroyed_this_match=destroyed)
        branches = EventInterpreter(self.rules, catalog=self.catalog, card_db=card_db).play_branches(state, card.unique_id)
        self.assertAlmostEqual(sum(item.probability for item in branches), 1.0)
        self.assertTrue(all(len(item.state.my_board) == 3 for item in branches))
        self.assertTrue(all(not item.unsupported_ops for item in branches))

    def test_beelzebub_multi_target_and_damage_modifier(self):
        card = self.make_card(10474120, 1005)
        enemies = [self.follower_from_catalog(self.find_cards(card_type="follower", limit=1)[0], 5001, hp=8), self.follower_from_catalog(self.find_cards(card_type="follower", limit=1)[0], 5002, hp=8)]
        state = LethalState(enemy_hp=10, pp=9, max_pp=9, ep=0, sep=0, hand=[card], enemy_board=enemies)
        result = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card}).play(state, card.unique_id, target_uid=[5001, 5002])
        self.assertEqual(result.state.enemy_board, [])
        self.assertEqual(result.state.enemy_damage_taken_modifier, 1)
        self.assertFalse(result.unsupported_ops)

    def test_generated_ordered_split_damage_matches_board_order(self):
        card = self.make_card(10753310, 1011)
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card})
        for index, hps, expected in (
            (0, (3, 1, 1), ()),
            (1, (7, 1, 1), (1, 1, 1)),
            (2, (1, 1, 7), (3,)),
        ):
            source = replace(card, unique_id=1011 + index)
            enemies = [LethalFollower(8000 + i, 9900 + i, f"enemy-{i}", 1, hp) for i, hp in enumerate(hps)]
            state = LethalState(enemy_hp=20, pp=3, max_pp=3, ep=0, sep=0, cemetery=6, hand=[source], enemy_board=enemies)
            result = interpreter.play(state, source.unique_id)
            self.assertEqual(tuple(f.hp for f in result.state.enemy_board), expected)
            self.assertEqual(result.state.enemy_hp, 18)
            self.assertEqual(result.state.cemetery, 1)
            self.assertFalse(result.unsupported_ops)

    def test_generated_necromancy_only_consumes_when_threshold_is_met(self):
        card = self.make_card(10753310, 1014)
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card})
        enemies = [
            LethalFollower(8100, 9900, "enemy-0", 1, 7),
            LethalFollower(8101, 9901, "enemy-1", 1, 1),
            LethalFollower(8102, 9902, "enemy-2", 1, 1),
        ]

        # The base six damage is unconditional; with only four pre-existing
        # shadows the spell's own shadow brings the preflight total to five,
        # so Necromancy(6) does not pay or deal the extra two leader damage.
        low = LethalState(
            enemy_hp=20, pp=3, max_pp=3, ep=0, sep=0, cemetery=4,
            hand=[card], enemy_board=list(enemies),
        )
        low_result = interpreter.play(low, card.unique_id)
        self.assertEqual(low_result.state.enemy_hp, 20)
        self.assertEqual(tuple(f.hp for f in low_result.state.enemy_board), (1, 1, 1))
        self.assertEqual(low_result.state.cemetery, 5)
        self.assertFalse(low_result.unsupported_ops)

        # At six shadows, the extra branch is enabled and consumes exactly
        # six; the spell's own shadow remains in the cemetery afterwards.
        high = replace(low, cemetery=6, hand=[replace(card, unique_id=1015)], enemy_board=list(enemies))
        high_result = interpreter.play(high, 1015)
        self.assertEqual(high_result.state.enemy_hp, 18)
        self.assertEqual(tuple(f.hp for f in high_result.state.enemy_board), (1, 1, 1))
        self.assertEqual(high_result.state.cemetery, 1)
        self.assertFalse(high_result.unsupported_ops)

    def test_catalog_faith_listener_and_crest_end_turn_effect(self):
        sathanid = self.make_card(10614120, 1006)
        # Sathanid's Fanfare explicitly spends 10 Faith; the play is only
        # legal when that payment is available.
        state = LethalState(enemy_hp=20, pp=1, max_pp=1, ep=1, sep=0, faith=10, hand=[sathanid])
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={sathanid.card_id: sathanid})
        played = interpreter.play(state, sathanid.unique_id)
        self.assertEqual(len(played.state.faith_instances), 1)
        evolved = interpreter.evolve(played.state, sathanid.unique_id)
        self.assertEqual(evolved.state.faith, 1)
        self.assertFalse(evolved.unsupported_ops)

        marwynn = self.make_card(10364120, 1007)
        enemy = [LethalFollower(7001, self.find_cards(card_type="follower", limit=1)[0], "enemy", 1, 1), LethalFollower(7002, self.find_cards(card_type="follower", limit=1)[0], "enemy", 1, 3)]
        crest_state = LethalState(enemy_hp=20, pp=4, max_pp=4, ep=1, sep=0, hand=[marwynn], enemy_board=enemy)
        played = interpreter.play(crest_state, marwynn.unique_id)
        evolved = interpreter.evolve(played.state, marwynn.unique_id)
        self.assertEqual(len(evolved.state.crest_instances), 1)
        ended = interpreter.end_turn(evolved.state)
        self.assertEqual([f.unique_id for f in ended.state.enemy_board], [7002])
        self.assertFalse(ended.unsupported_ops)

    def test_zeta_enhance_does_not_also_run_normal_fanfare(self):
        card = self.make_card(10424110, 1008)
        interpreter = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card})
        normal_state = LethalState(enemy_hp=20, pp=4, max_pp=4, ep=0, sep=0, hand=[card])
        normal = interpreter.play(normal_state, card.unique_id, mode="normal")
        self.assertEqual(len(normal.state.my_board), 2)  # body + its normal summon
        enhance_state = LethalState(enemy_hp=20, pp=6, max_pp=6, ep=0, sep=0, hand=[card])
        enhance = interpreter.play(enhance_state, card.unique_id, mode="enhance")
        self.assertEqual(len(enhance.state.my_board), 1)
        self.assertEqual(enhance.state.pp, 0)
        self.assertNotIn("summon_card", " ".join(enhance.unsupported_ops))

    def test_super_skybound_mode_choice_replaces_choose_one_with_all(self):
        card = self.make_card(10413310, 1009)
        drawn = self.make_card(10424110, 9009)
        ally = self.follower_from_catalog(self.find_cards(card_type="follower", limit=1)[0], 6001, hp=2)
        # Keep the leader below a realistic 20-defense cap so the heal from
        # Mode 1 is observable in the final state.
        state = LethalState(enemy_hp=20, pp=2, max_pp=2, ep=0, sep=0, hand=[card], my_board=[ally], ally_hp=10, ally_max_hp=20, super_skybound_art=1, deck_distribution={drawn.card_id: 1}, total_deck_count=1)
        branches = EventInterpreter(self.rules, catalog=self.catalog, card_db={card.card_id: card, drawn.card_id: drawn}).play_branches(state, card.unique_id)
        self.assertEqual(len(branches), 1)
        result = branches[0]
        self.assertEqual(result.state.ally_hp, 11)
        self.assertEqual(result.state.my_board[0].atk, ally.atk + 1)
        self.assertTrue(result.state.my_board[0].is_ward)
        self.assertEqual(len(result.state.hand), 1)
        self.assertFalse(result.unsupported_ops)


if __name__ == "__main__":
    unittest.main()
