import unittest
import hashlib
import json
from pathlib import Path

from card_text_ast import clause_to_ast, split_mode_clauses
from compile_card_rules import compile_card, effect


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

    def test_mode_choice_banishes_each_resource_type(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Select a Mode to activate. 1. Banish all other followers from the field. 2. Banish all amulets from the field. 3. Banish all crests.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        choice = node["effects"][0]
        self.assertEqual(choice["kind"], "mode_choice")
        self.assertEqual([item["effects"][0]["kind"] for item in choice["choices"]], ["banish", "banish", "destroy_crest"])
        self.assertEqual(choice["choices"][0]["effects"][0]["target"]["filters"], {"zone": "field", "card_type": "follower", "exclude_source": True})
        self.assertEqual(choice["choices"][1]["effects"][0]["target"]["filters"]["card_type"], "amulet")
        self.assertEqual(choice["choices"][2]["effects"][0]["target"]["filters"]["zone"], "crests")

    def test_mode_keyword_in_card_name_is_not_an_enhance_mode(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon 3 copies of Enhanced Puppet.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertIsNone(node["mode"])

    def test_super_mode_split_keeps_normal_mode_header_with_choices(self):
        text = (
            "Select a Mode to activate. Super Skybound Art - Activate all of them instead. "
            "1. Draw a card. 2. Give all allied followers on the field +1/+0 and Rush."
        )
        parts = split_mode_clauses({"language": "eng", "plain": text, "source_key": "skill", "index": 0})
        self.assertEqual([part.get("mode_override") for part in parts], ["mode_selection", "super_skybound_art"])
        self.assertNotIn("Select a Mode to activate.", [part["plain"] for part in parts[1:]])

    def test_skybound_mode_split_keeps_choices_separate_from_evolve(self):
        text = (
            "Fanfare: Select a Mode to activate. Skybound Art - Evolve this follower. "
            "1. Deal 5 damage to a random enemy follower. 2. Draw 2 followers."
        )
        parts = split_mode_clauses({"language": "eng", "plain": text, "source_key": "skill", "index": 0})
        self.assertEqual([part.get("mode_override") for part in parts], ["mode_selection", "skybound_art"])
        choice = clause_to_ast(parts[0])
        self.assertEqual(choice["effects"][0]["kind"], "mode_choice")
        self.assertEqual(len(choice["effects"][0]["choices"]), 2)
        special = clause_to_ast(parts[1])
        self.assertEqual([item["kind"] for item in special["effects"]], ["auto_evolve"])

    def test_dynamic_random_deck_summon_selector(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon a random Havencraft follower that costs 2 or less from your deck.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        summon = node["effects"][0]
        self.assertEqual(summon["kind"], "summon")
        self.assertEqual(summon["resource_selector"]["filters"], {"card_type": "follower", "class": "havencraft", "max_cost": 2})

        abyss = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon 2 random differently named Abysscraft followers that cost 2 or less from your deck.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual(abyss["effects"][0]["resource_selector"]["filters"]["tribe"], "abysscraft")

        amulets = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon 3 random differently named amulets that cost 3 or less from your deck.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        amulet_summon = amulets["effects"][0]
        self.assertEqual(amulet_summon["count"], 3)
        self.assertEqual(amulet_summon["resource_selector"]["filters"], {"card_type": "amulet", "max_cost": 3})
        self.assertEqual(amulet_summon["resource_selector"]["distinct_by"], "card_id")

    def test_resource_keyword_gates_only_suffix(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Deal 6 damage split between all enemy followers. Necromancy (6) - Deal 2 damage to the enemy leader.",
            "trigger": "on_play",
            "structure": {},
        })
        self.assertEqual(node["conditions"], [])
        self.assertEqual([item["kind"] for item in node["effects"]], ["damage", "conditional"])
        gated = node["effects"][1]
        self.assertEqual(gated["condition"], {"state": "cemetery", "cmp": "gte", "value": 6})
        self.assertEqual([item["op"] if "op" in item else item["kind"] for item in gated["effects"]], ["consume_resource", "damage"])

        earth = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon a Guardian Golem. Earth Rite (1) - Evolve this follower.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual([item["kind"] for item in earth["effects"]], ["summon", "conditional"])
        self.assertEqual(earth["effects"][1]["condition"]["state"], "earth_sigil")

    def test_dynamic_destroyed_amulet_copy_selector_is_distinct(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon a copy each of 2 random differently named allied amulets destroyed this match with Last Words and a base cost of 2 or less.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        summon = node["effects"][0]
        self.assertEqual(summon["count"], 2)
        self.assertEqual(summon["resource_selector"]["distinct_by"], "card_id")
        self.assertTrue(summon["preserve_state"])

    def test_exact_copy_preserves_state_and_card_cost_condition(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: If this card's cost isn't 3, summon 2 exact copies of it.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual(node["conditions"], [{"state": "card_cost", "cmp": "ne", "value": 3}])
        copy_effect = node["effects"][0]
        self.assertEqual(copy_effect["kind"], "copy")
        self.assertEqual(copy_effect["source"], {"scope": "self"})
        self.assertTrue(copy_effect["preserve_state"])
        self.assertEqual(copy_effect["count"], 2)

    def test_self_copy_alias_is_entity_copy(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Super-Evolve: Summon an exact copy of itself.",
            "trigger": "on_super_evolve",
            "structure": {},
        })
        copy_effect = node["effects"][0]
        self.assertEqual(copy_effect["kind"], "copy")
        self.assertEqual(copy_effect["source"], {"scope": "self"})
        self.assertTrue(copy_effect["preserve_state"])

    def test_selected_follower_copy_to_hand_keeps_cost_modifier(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Select an allied follower on the field with a base cost of 5 or more, add a copy of it to your hand without revealing it, and reduce the cost of the copy by 3.",
            "trigger": "on_play",
            "structure": {},
        })
        copy_effect = node["effects"][0]
        self.assertEqual(copy_effect["kind"], "copy")
        self.assertEqual(copy_effect["destination"], "hand")
        self.assertEqual(copy_effect["cost_delta"], -3)
        self.assertFalse(copy_effect["preserve_state"])

    def test_historical_and_opponent_deck_copies_keep_zone_and_mode(self):
        historical = clause_to_ast({
            "language": "eng",
            "plain": "Add a copy of a random allied Artifact follower destroyed this match to your hand without revealing it.",
            "trigger": "on_play",
            "structure": {},
        })["effects"][0]
        self.assertEqual(historical["kind"], "copy")
        self.assertEqual(historical["source"]["zone"], "destroyed_this_match")
        self.assertEqual(historical["source"]["filters"]["tribe"], "artifact")
        self.assertEqual(historical["destination"], "hand")
        self.assertEqual(historical["copy_mode"], "card")
        self.assertFalse(historical["preserve_state"])

        opponent = clause_to_ast({
            "language": "eng",
            "plain": "Add an exact copy each of 5 random cards in your opponent's deck to your hand without revealing them.",
            "trigger": "on_evolve",
            "structure": {},
        })["effects"][0]
        self.assertEqual(opponent["source"], {"zone": "deck", "selection": "random", "side": "enemy", "filters": {"card_type": "card"}})
        self.assertEqual(opponent["count"], 5)
        self.assertEqual(opponent["copy_mode"], "exact")
        self.assertTrue(opponent["preserve_state"])

    def test_selected_enemy_card_copy_uses_field_snapshot_selector(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Select an enemy card on the field, banish it, and add a copy of it to your hand.",
            "trigger": "on_play",
            "structure": {},
        })
        copy_effect = next(item for item in node["effects"] if item["kind"] == "copy")
        self.assertEqual(copy_effect["source"]["zone"], "field")
        self.assertEqual(copy_effect["source"]["side"], "enemy")
        self.assertEqual(copy_effect["source"]["filters"]["card_type"], "field_card")
        self.assertEqual(copy_effect["destination"], "hand")
        self.assertEqual([item["kind"] for item in node["effects"]], ["banish", "copy"])

    def test_enemy_named_summon_keeps_count_and_side(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon 2 enemy copies of Knight.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        summon = node["effects"][0]
        self.assertEqual(summon["kind"], "summon")
        self.assertEqual(summon["count"], 2)
        self.assertEqual(summon["source_card_name"], "Knight")
        self.assertEqual(summon["target"], {"scope": "enemy_follower"})
        self.assertEqual(
            effect(summon, {"knight": 90021110}),
            {"op": "summon", "card_id": 90021110, "count": 2, "target": {"scope": "enemy_follower"}},
        )

    def test_last_words_copy_is_deferred_and_targets_selected_follower(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": 'Select an allied follower on the field and give it "Last Words: Summon a copy of this card."',
            "trigger": "on_play",
            "structure": {},
        })
        status = node["effects"][0]
        self.assertEqual(status["kind"], "grant_status")
        self.assertEqual(status["target"]["scope"], "ally_follower")
        self.assertEqual(status["ability"]["effects"][0]["kind"], "copy")
        self.assertEqual(status["ability"]["effects"][0]["source"], {"scope": "trigger_source"})
        self.assertNotIn("summon", [item["kind"] for item in node["effects"]])
        self.assertEqual(node["unparsed_clauses"], [])

    def test_chained_trigger_targets_source_and_leader_separately(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": 'Whenever an enemy follower enters the field, give it "Can\'t attack followers or leaders" until the end of your opponent\'s turn, deal 1 damage to the enemy leader, and restore 1 defense to your leader.',
            "trigger": "on_summon",
            "structure": {},
        })
        self.assertEqual([item["kind"] for item in node["effects"]], ["grant_status", "damage", "heal"])
        self.assertEqual(node["effects"][0]["target"], {"scope": "trigger_source"})
        self.assertEqual(node["effects"][0]["duration"], "until_end_of_opponent_turn")
        self.assertEqual(node["effects"][1]["target"], {"scope": "enemy_leader"})
        self.assertEqual(node["effects"][2]["target"], {"scope": "ally_leader"})

    def test_last_words_damage_is_deferred_not_immediate(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": 'Evolve: Give all allied Puppetry followers on the field Ward and "Last Words: Deal 2 damage to the enemy leader."',
            "trigger": "on_evolve",
            "structure": {},
        })
        self.assertEqual([item["kind"] for item in node["effects"]], ["grant_status", "grant_keyword"])
        self.assertEqual(node["effects"][0]["target"]["filters"]["tribe"], "puppetry")
        self.assertEqual(node["effects"][1]["target"]["filters"]["tribe"], "puppetry")
        nested = node["effects"][0]["ability"]["effects"]
        self.assertEqual(nested, [{"kind": "damage", "target": {"scope": "enemy_leader"}, "amount": 2}])
        self.assertEqual(node["unparsed_clauses"], [])

    def test_remove_all_abilities_keeps_selected_target(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Evolve: Select an enemy follower on the field, remove all abilities from it, and deal it 2 damage.",
            "trigger": "on_evolve",
            "structure": {},
        })
        self.assertEqual([item["kind"] for item in node["effects"]], ["remove_abilities", "damage"])
        self.assertEqual(node["effects"][0]["target"], {"scope": "enemy_follower", "selection": "chosen", "count": 1})
        self.assertEqual(node["effects"][1]["target"], node["effects"][0]["target"])
        self.assertEqual(node["unparsed_clauses"], [])

    def test_remove_all_abilities_and_damage_taken_modifier(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": 'Fanfare: Select 2 enemy followers on the field, remove all abilities from them, and deal them 9 damage. Give the enemy leader "Takes 1 more damage."',
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual([item["kind"] for item in node["effects"]], ["remove_abilities", "damage", "modify_damage_taken"])
        self.assertEqual(node["effects"][0]["target"]["count"], 2)
        self.assertEqual(node["effects"][1]["amount"], 9)
        self.assertEqual(node["effects"][2], {
            "kind": "modify_damage_taken",
            "target": {"scope": "enemy_leader"},
            "amount": 1,
            "duration": "permanent",
        })
        self.assertEqual(node["unparsed_clauses"], [])

    def test_remove_all_abilities_chinese_target_count_matches(self):
        node = clause_to_ast({
            "language": "chs",
            "plain": "选择对手的战场上的2个随从，使其失去所有能力，对其造成9点伤害。",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual(node["effects"][0]["kind"], "remove_abilities")
        self.assertEqual(node["effects"][0]["target"]["count"], 2)
        self.assertEqual(node["effects"][1]["target"]["count"], 2)

    def test_discard_cost_chain_is_mutually_exclusive(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "When this card is discarded, if its cost is 7, add a Beheading Eld Blades to your hand and set its cost to 5. If this card's cost is 5, add a Beheading Eld Blades to your hand and set its cost to 3.",
            "trigger": "on_discard",
            "structure": {},
        })
        chain = node["effects"][0]
        self.assertEqual(chain["kind"], "conditional")
        self.assertEqual(chain["condition"], {"state": "card_cost", "cmp": "eq", "value": 7})
        self.assertEqual([item["kind"] for item in chain["effects"]], ["add_to_hand", "set_cost"])
        self.assertEqual(chain["effects"][1]["target"], {"scope": "previous_add"})
        self.assertEqual(chain["else_effects"][0]["condition"], {"state": "card_cost", "cmp": "eq", "value": 5})
        self.assertEqual(node["unparsed_clauses"], [])

    def test_instead_becomes_conditional_branch(self):
        node = clause_to_ast({"language": "eng", "plain": "Deal 2 damage to a random enemy follower. Combo (3) - Deal 4 damage to all enemy followers instead.", "trigger": "on_play", "structure": {}})
        self.assertEqual(node["effects"][0]["kind"], "conditional")
        self.assertEqual(node["effects"][0]["condition"]["state"], "play_count")
        self.assertEqual(node["effects"][0]["else_effects"][0]["amount"], 2)

    def test_repeat_instead_materializes_both_branches(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": 'Do this 1 time: "Deal 2 damage to a random enemy follower." Combo (3) - Do it 2 times instead.',
            "trigger": "on_play",
            "structure": {},
        })
        conditional = node["effects"][0]
        self.assertEqual(conditional["kind"], "conditional")
        self.assertEqual(conditional["condition"]["state"], "play_count")
        self.assertEqual(conditional["effects"][0]["count"], 2)
        self.assertEqual(conditional["else_effects"][0]["count"], 1)

    def test_generic_instead_keeps_unconditional_discard(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Select a card in your hand and discard it. Restore 3 defense to your leader. If you selected a spell, restore 6 defense instead.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        self.assertEqual(node["effects"][0]["kind"], "discard")
        self.assertEqual(node["effects"][1]["kind"], "conditional")
        self.assertEqual(node["effects"][1]["condition"]["state"], "selected_card_type")

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

    def test_target_choice_preserves_follower_or_leader(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Select an enemy follower on the field or the enemy leader and deal it 1 damage.",
            "trigger": "on_play",
            "structure": {},
        })
        target = node["effects"][0]["target"]
        self.assertEqual(target["scope"], "any")
        self.assertEqual(target["filters"]["card_type"], ["follower", "leader"])

    def test_comma_in_official_card_name_is_not_split(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Super-Evolve: Add 10 copies of Lhynkal, Wandering Fool to your deck.",
            "trigger": "on_super_evolve",
            "structure": {},
        })
        adds = [item for item in node["effects"] if item["kind"] == "add_to_hand"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0]["source_card_name"], "Lhynkal, Wandering Fool")
        self.assertEqual(adds[0]["target_zone"], "deck")

    def test_ampersand_official_name_is_not_split_into_unrelated_cards(self):
        node = clause_to_ast({
            "language": "eng",
            "plain": "Fanfare: Summon a Zeta & Bea, Crimson and Blue.",
            "trigger": "on_fanfare",
            "structure": {},
        })
        summons = [item for item in node["effects"] if item["kind"] == "summon"]
        self.assertEqual(len(summons), 1)
        self.assertEqual(summons[0]["source_card_name"], "Zeta & Bea, Crimson and Blue")

    def test_compiler_normalizes_destination_and_status_names(self):
        self.assertEqual(
            effect({"kind": "add_to_hand", "source_card_name": "Token", "count": 2, "target_zone": "deck"}, {"token": 99}),
            {"op": "add_to_zone", "card_id": 99, "count": 2, "destination": "deck"},
        )
        self.assertEqual(
            effect({"kind": "grant_status", "status": "storm", "target": {"scope": "self"}}, {}),
            {"op": "gain_status", "status": "storm", "duration": "permanent", "target": {"scope": "self"}},
        )

    def test_compiler_keeps_dynamic_selector_and_exact_copy_metadata(self):
        selector = {
            "zone": "deck",
            "selection": "random",
            "side": "ally",
            "filters": {"card_type": "amulet", "max_cost": 3},
            "distinct_by": "card_id",
        }
        self.assertEqual(
            effect({"kind": "summon", "count": 3, "resource_selector": selector}, {}),
            {"op": "summon", "count": 3, "resource_selector": selector},
        )
        self.assertEqual(
            effect({
                "kind": "copy",
                "source": {"scope": "self"},
                "destination": "field",
                "count": 1,
                "copy_mode": "exact",
                "preserve_state": True,
            }, {}),
            {
                "op": "copy",
                "source": {"scope": "self"},
                "destination": "field",
                "count": 1,
                "copy_mode": "exact",
                "preserve_state": True,
            },
        )

    def test_compiler_materializes_mode_instead_from_base(self):
        ast_card = {
            "card_id": 1,
            "source_hash": "fixture",
            "abilities": [
                {
                    "ability_id": "1:skill:0:normal:normal",
                    "section": "normal",
                    "trigger": "static",
                    "mode": None,
                    "classification": "matched",
                    "conditions": [],
                    "effects": [{"kind": "damage", "target": {"scope": "enemy_leader"}, "amount": 1}],
                },
                {
                    "ability_id": "1:skill:0:normal:enhance",
                    "section": "normal",
                    "trigger": "static",
                    "mode": "enhance",
                    "classification": "matched",
                    "conditions": [],
                    "effects": [{"kind": "modify_previous_effect", "field": "amount", "value": 3}],
                },
            ],
        }
        rule = compile_card("1", ast_card, {"type": "spell", "cost": 1})
        enhance = next(mode for mode in rule["modes"] if mode["kind"] == "enhance")
        self.assertEqual(enhance["abilities"][0]["effects"][0], {"op": "damage", "target": {"scope": "enemy_leader"}, "amount": 3})

    def test_compiler_drops_fully_compiled_chinese_translation_marker(self):
        chinese = "【入场曲】召唤1个衍生物。"
        ast_card = {
            "card_id": 2,
            "primary_language": "eng",
            "source_hash": "fixture",
            "abilities": [{
                "ability_id": "2:skill:0:normal:normal",
                "section": "normal",
                "trigger": "on_fanfare",
                "mode": None,
                "classification": "missing_translation",
                "source_clause": {"chs": chinese, "eng": ""},
                "conditions": [],
                "static_keywords": [],
                "effects": [{"kind": "summon", "count": 1, "source_card_name": "Token"}],
                "unparsed_clauses": [chinese],
            }],
        }
        rule = compile_card("2", ast_card, {"type": "follower", "cost": 1}, {"token": 90000001})
        self.assertEqual(rule["support"], "partial")
        self.assertNotIn("unparsed_clauses", rule)
        self.assertEqual(rule["modes"][0]["abilities"][0]["effects"][0]["card_id"], 90000001)

    def test_evolve_trigger_is_not_auto_evolve(self):
        node = clause_to_ast({"language": "chs", "plain": "本随从进化时，发动7次「对对手的战场上的随机1个随从造成1点伤害」。", "trigger": "on_evolve", "structure": {}})
        self.assertNotIn("auto_evolve", [effect["kind"] for effect in node["effects"]])

    def test_earth_rite_evolves_trigger_source(self):
        node = clause_to_ast({"language": "eng", "plain": "Whenever an allied Golem follower enters the field, Earth Rite (1) - Evolve it.", "trigger": "on_summon", "structure": {}})
        gated = next(effect for effect in node["effects"] if effect["kind"] == "conditional")
        self.assertEqual(gated["condition"]["state"], "earth_sigil")
        evolve = next(effect for effect in gated["effects"] if effect["kind"] == "auto_evolve")
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
