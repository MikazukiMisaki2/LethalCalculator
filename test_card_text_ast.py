import unittest
import hashlib
import json
from pathlib import Path

from card_text_ast import clause_to_ast, split_mode_clauses


class CardTextAstTests(unittest.TestCase):
    def test_repeat_random_damage_ast(self):
        node = clause_to_ast({
            "language": "eng", "source_key": "skill", "index": 0,
            "plain": "When this follower evolves, do this 7 times: Deal 1 damage to a random enemy follower.",
            "trigger": "on_evolve", "structure": {"repeat": 7}
        })
        self.assertEqual(node["effects"][0]["kind"], "repeat")
        self.assertEqual(node["effects"][0]["count"], 7)
        self.assertEqual(node["effects"][0]["effects"][0]["target"]["selection"], "random")

    def test_inline_modes_are_split_with_costs(self):
        parts = split_mode_clauses({"plain": "Give this follower +1/+1. Enhance (4): Give this follower +3/+3."})
        self.assertEqual([(p.get("mode_override"), p.get("mode_cost")) for p in parts], [(None, None), ("enhance", 4)])

    def test_numbered_mode_choices_are_single_choice_effect(self):
        node = clause_to_ast({"language": "eng", "plain": "Select a Mode to activate. 1. Draw a card. 2. Deal 3 damage to a random enemy follower.", "trigger": "on_play", "structure": {}})
        self.assertEqual([e["kind"] for e in node["effects"]], ["mode_choice"])
        self.assertEqual(len(node["effects"][0]["choices"]), 2)

    def test_instead_becomes_conditional_branch(self):
        node = clause_to_ast({"language": "eng", "plain": "Deal 2 damage to a random enemy follower. Combo (3) - Deal 4 damage to all enemy followers instead.", "trigger": "on_play", "structure": {}})
        self.assertEqual(node["effects"][0]["kind"], "conditional")
        self.assertEqual(node["effects"][0]["condition"]["state"], "play_count")
        self.assertEqual(node["effects"][0]["else_effects"][0]["amount"], 2)

    def test_variable_source_is_bound_when_explanation_is_separate(self):
        node = clause_to_ast({"language": "eng", "plain": "Deal X damage to the enemy leader. X is the number of crests you have.", "trigger": "on_play", "structure": {}})
        self.assertEqual(node["effects"][0]["amount"], "var:crest_count")

    def test_engage_counter_effect_is_structured(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Engage (1): Advance this amulet's count by 1.",
            "trigger": "on_engage",
            "structure": {}
        })
        self.assertEqual(node["effects"][0]["kind"], "modify_counter")
        self.assertEqual(node["effects"][0]["field"], "countdown")
        self.assertEqual(node["effects"][0]["delta"], 1)

    def test_common_partial_templates_are_structured(self):
        cases = {
            "本随从+1/+1。": "buff",
            "【入场曲】使自己的墓场+2。": "modify_resource",
            "【超进化时】本随从获得「1回合可以攻击2次」。": "set_attacks",
            "【入场曲】发动【亡者召还_4】。": "reanimate",
            "【进化时】使自己的所有手牌发动2次魔力增幅。": "spellboost",
            "自己的随从超进化时，使本卡牌的费用变为1。": "set_cost",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                node = clause_to_ast({"language": "chs", "plain": text, "trigger": "on_play", "structure": {}})
                self.assertIn(expected, [effect["kind"] for effect in node["effects"]])

    def test_filtered_spell_draw_keeps_selector(self):
        node = clause_to_ast({"language": "eng", "plain": "Fanfare: Draw a spell.", "trigger": "on_fanfare", "structure": {}})
        draw = next(effect for effect in node["effects"] if effect["kind"] == "draw")
        self.assertEqual(draw["target"]["filters"]["card_type"], "spell")

    def test_evolve_trigger_is_not_auto_evolve(self):
        node = clause_to_ast({"language": "chs", "plain": "本随从进化时，发动7次「对对手的战场上的随机1个随从造成1点伤害」。", "trigger": "on_evolve", "structure": {}})
        self.assertNotIn("auto_evolve", [effect["kind"] for effect in node["effects"]])

    def test_earth_rite_evolves_trigger_source(self):
        node = clause_to_ast({"language": "eng", "plain": "Whenever an allied Golem follower enters the field, Earth Rite (1) - Evolve it.", "trigger": "on_summon", "structure": {}})
        self.assertEqual(node["conditions"][0]["state"], "earth_sigil")
        evolve = next(effect for effect in node["effects"] if effect["kind"] == "auto_evolve")
        self.assertEqual(evolve["target"]["scope"], "trigger_source")

    def test_fusion_and_invoke_are_structured(self):
        fusion = clause_to_ast({"language": "eng", "plain": "Fuse: Loot cards", "trigger": "static", "structure": {}})
        self.assertEqual(fusion["effects"][0]["kind"], "fusion_config")
        self.assertEqual(fusion["effects"][0]["config"]["filters"]["tribe"], "loot")
        invoke = clause_to_ast({"language": "eng", "plain": "At the start of your turn, if allied followers have evolved at least 6 times this match, invoke this card.", "trigger": "on_turn_start", "structure": {}})
        self.assertEqual(invoke["conditions"][0]["state"], "evolved_allies_this_match")
        self.assertIn("invoke", [effect["kind"] for effect in invoke["effects"]])

    def test_progressive_sequence_keeps_step_order(self):
        node = clause_to_ast({"language": "eng", "plain": "At the end of your turn, activate an ability in sequence from the following. 1. Draw a card. 2. Deal 2 damage to the enemy leader.", "trigger": "on_turn_end", "structure": {}})
        effect = node["effects"][0]
        self.assertEqual(effect["kind"], "progressive_sequence")
        self.assertEqual([step["label"] for step in effect["steps"]], ["1", "2"])

    def test_ordered_split_ast(self):
        node = clause_to_ast({
            "language": "eng", "source_key": "skill", "index": 0,
            "plain": "Deal 6 damage split between all enemy followers.",
            "trigger": "on_play", "structure": {}
        })
        self.assertEqual(node["effects"][0]["target"]["allocation"], "ordered_split")

    def test_ally_fairy_summon_trigger_is_preserved(self):
        node = clause_to_ast({
            "language": "eng", "source_key": "skill", "index": 0,
            "plain": "Whenever an allied Pixie follower enters the field, deal 1 damage to a random enemy follower.",
            "trigger": "on_ally_follower_summon", "structure": {}
        })
        self.assertEqual(node["trigger"], "on_ally_follower_summon")
        self.assertEqual(node["effects"][0]["kind"], "damage")

    def test_ast_nodes_have_source_provenance(self):
        node = clause_to_ast({"language": "eng", "source_key": "skill", "index": 0, "plain": "Deal 2 damage to the enemy leader.", "trigger": "on_play", "structure": {}})
        for field in ("source_language", "source_clause", "confidence"):
            self.assertIn(field, node)
        self.assertEqual(node["source_language"], "eng")
        self.assertEqual(node["confidence"], 1.0)

    def test_english_primary_keeps_translation_conflict_as_audit_only(self):
        from card_text_ast import card_to_ast
        card = {"card_id": 1, "source_hash": "x", "name": {}, "clauses": [
            {"language": "chs", "source_key": "skill", "index": 0, "plain": "对主战者造成2点伤害。", "trigger": "on_play", "structure": {}, "effects": [{"kind": "damage", "amount": 2}], "confidence": 1.0},
            {"language": "eng", "source_key": "skill", "index": 0, "plain": "Deal 3 damage to the enemy leader.", "trigger": "on_play", "structure": {}, "effects": [{"kind": "damage", "amount": 3}], "confidence": 1.0},
        ]}
        result = card_to_ast(card)
        self.assertEqual(result["support"], "generated")
        self.assertEqual(result["primary_language"], "eng")
        self.assertEqual(result["abilities"][0]["effects"][0]["amount"], 3)
        self.assertTrue(result["bilingual_conflicts"])

    def test_bilingual_policy_can_still_block_conflicts(self):
        from card_text_ast import card_to_ast
        card = {"card_id": 1, "source_hash": "x", "name": {}, "clauses": [
            {"language": "chs", "source_key": "skill", "index": 0, "plain": "对主战者造成2点伤害。", "trigger": "on_play", "structure": {}},
            {"language": "eng", "source_key": "skill", "index": 0, "plain": "Deal 3 damage to the enemy leader.", "trigger": "on_play", "structure": {}},
        ]}
        self.assertEqual(card_to_ast(card, primary_language="bilingual")["support"], "partial")

    def test_confirmed_100_golden_digest(self):
        root = Path(__file__).resolve().parent
        ast = json.loads((root / "data/generated/card_text_ast.json").read_text(encoding="utf-8"))
        golden = json.loads((root / "fixtures/text_ast/confirmed_100.json").read_text(encoding="utf-8"))
        payload = {str(cid): ast["cards"][str(cid)] for cid in golden["card_ids"]}
        self.assertEqual(sum(len(card["clauses"]) for card in payload.values()), golden["clause_count"])
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(digest, golden["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
