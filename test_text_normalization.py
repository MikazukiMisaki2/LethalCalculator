import hashlib
import json
import unittest
from pathlib import Path

from normalize_card_text import normalize_card


class TextNormalizationTests(unittest.TestCase):
    def test_cupitan_repeat_random_damage_is_structured(self):
        card = {"card_id": 10413110, "name": {"chs": "丘比丹", "eng": "Cupitan"}, "text": {"skill_texts": [{"key": "skill", "chs": "【进化时】发动7次「对对手的战场上的随机1个随从造成1点伤害」。", "eng": "When this follower evolves, do this 7 times: Deal 1 damage to a random enemy follower."}]}}
        clauses = normalize_card(card)["clauses"]
        evolve = next(c for c in clauses if c["language"] == "eng")
        self.assertEqual(evolve["trigger"], "on_evolve")
        self.assertEqual(evolve["structure"]["repeat"], 7)
        self.assertEqual(evolve["structure"]["target"], "enemy_follower_random")
        self.assertEqual(evolve["structure"]["amounts"], [1])

    def test_ordered_split_and_tribe_trigger_are_detected(self):
        card = {"card_id": 1, "name": {}, "text": {"skill_texts": [{"key": "skill", "chs": "自己的妖精·随从进入战场时，对对手的战场上的随机1个随从造成1点伤害。", "eng": "Whenever an allied Pixie follower enters the field, deal 1 damage to a random enemy follower."}, {"key": "evo", "chs": "对对手的战场上的所有随从分配6点伤害。", "eng": "Deal 6 damage split between all enemy followers."}]}}
        clauses = normalize_card(card)["clauses"]
        summon = next(c for c in clauses if c["language"] == "eng" and c["trigger"] == "on_ally_follower_summon")
        split = next(c for c in clauses if c["language"] == "eng" and c["structure"]["target"] == "enemy_follower_ordered_split")
        self.assertIn("tribe_condition", summon["hints"])
        self.assertEqual(split["structure"]["amounts"], [6])

    def test_semantic_boundaries_and_source_hash(self):
        card = {"card_id": 2, "name": {}, "text": {"skill_texts": [{"key": "skill", "chs": "普通。<hr><ev>【进化时】造成3点伤害。</ev>", "eng": "Normal.<hr><ev>Evolve: Deal 3 damage.</ev>"}]}}
        normalized = normalize_card(card)
        self.assertEqual([c["section"] for c in normalized["clauses"]], ["normal", "evolve"] * 2)
        self.assertTrue(normalized["source_hash"])
        self.assertEqual(len(normalized["source_hash"]), 64)
        self.assertEqual(normalized["clauses"][1]["text_chs"], "【进化时】造成3点伤害。")

    def test_spellboost_and_engage_triggers_are_detected(self):
        card = {"card_id": 3, "name": {}, "text": {"skill_texts": [{
            "key": "skill",
            "chs": "【魔力增幅时】本随从+1/+1。",
            "eng": "On Spellboost: Give this follower +1/+1."
        }, {
            "key": "skill2",
            "chs": "费用1【启动】本护符的倒计数-1。",
            "eng": "Engage (1): Advance this amulet's count by 1."
        }]}}
        clauses = normalize_card(card)["clauses"]
        self.assertIn("on_spellboost", [c["trigger"] for c in clauses])
        self.assertIn("on_engage", [c["trigger"] for c in clauses])

    def test_super_evolve_and_opponent_turn_triggers_are_distinct(self):
        card = {"card_id": 4, "name": {}, "text": {"skill_texts": [{
            "key": "skill", "chs": "在手牌中发动。自己的随从超进化时，使本卡牌的费用-3。",
            "eng": "Activates in hand. Whenever an allied follower super-evolves, reduce the cost of this card by 3."
        }, {
            "key": "skill2", "chs": "对手的回合结束时，破坏本卡牌。",
            "eng": "At the end of your opponent's turn, destroy this card."
        }]}}
        clauses = normalize_card(card)["clauses"]
        self.assertIn("on_ally_follower_super_evolve", [c["trigger"] for c in clauses])
        self.assertIn("on_opponent_turn_end", [c["trigger"] for c in clauses])

    def test_golden_50_cards_digest(self):
        root = Path(__file__).resolve().parent
        catalog = json.loads((root / "data/generated/card_text_normalized.json").read_text(encoding="utf-8"))
        golden = json.loads((root / "fixtures/text_normalization/golden_50.json").read_text(encoding="utf-8"))
        payload = {str(cid): catalog["cards"][str(cid)] for cid in golden["card_ids"]}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(digest, golden["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
