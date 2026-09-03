"""Contract and regression tests for the isolated SWB-RL rule adapter.

The adapter deliberately produces a candidate ruleset rather than replacing
the runtime rules.  These tests therefore focus on deterministic conversion,
schema/reference safety, and a small set of source-rule semantics that are
easy to lose when translating between the two rule languages.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from adapt_swb_rules import build_swb_v2_candidate, write_adapter_artifacts
from import_swb_rl import build_catalog_projection
from rule_support import validate_contract


ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("SWB_RL_PATH", r"D:\Github\SWB-RL"))
SCHEMA_PATH = ROOT / "schemas" / "card_rules_v2.schema.json"
SUPPORT_PATH = ROOT / "schemas" / "card_rules_v2_support.json"


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _effects(rule: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _walk(rule) if isinstance(item.get("op"), str)]


@unittest.skipUnless(
    (SOURCE / "data" / "cards.sqlite3").exists() and (SOURCE / "data" / "rules").is_dir(),
    "SWB-RL checkout is not available; set SWB_RL_PATH to run adapter tests",
)
class SwbRuleAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog, _metadata = build_catalog_projection(SOURCE / "data" / "cards.sqlite3")
        cls.candidate, cls.report = build_swb_v2_candidate(SOURCE, catalog=cls.catalog)
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.support = json.loads(SUPPORT_PATH.read_text(encoding="utf-8"))

    def test_schema_and_cross_document_contract(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        errors = sorted(Draft202012Validator(self.schema).iter_errors(self.candidate), key=lambda error: list(error.path))
        self.assertEqual([], errors, "adapter candidate must be valid CardRules v2")
        self.assertEqual(
            [],
            validate_contract(self.candidate, self.catalog, schema=self.schema, matrix=self.support),
        )

    def test_deterministic_build(self) -> None:
        again, again_report = build_swb_v2_candidate(SOURCE, catalog=self.catalog)
        self.assertEqual(self.candidate, again)
        self.assertEqual(self.report, again_report)

    def test_catalog_coverage_and_no_synthetic_rules(self) -> None:
        catalog_ids = {str(card_id) for card_id in self.catalog["cards"]}
        self.assertEqual(catalog_ids, set(self.candidate["rules"]))
        self.assertFalse(any(card_id.startswith("999") for card_id in self.candidate["rules"]))
        support = self.report["summary"]["support"]
        self.assertEqual(sum(support.values()), len(catalog_ids))
        self.assertNotIn("verified", support, "an imported rule must never be promoted automatically")
        unsupported_hashes = {
            rule.get("source_hash")
            for rule in self.candidate["rules"].values()
            if rule.get("support") == "unsupported"
        }
        self.assertEqual(len(unsupported_hashes), len([rule for rule in self.candidate["rules"].values() if rule.get("support") == "unsupported"]))

    def test_damage_distribution_and_necromancy(self) -> None:
        effects = _effects(self.candidate["rules"]["10753310"])
        damage = [item for item in effects if item.get("op") == "damage"]
        self.assertTrue(any(item.get("target", {}).get("allocation") == "ordered_split" for item in damage))
        self.assertTrue(any(item.get("op") == "consume_resource" and item.get("resource") == "cemetery" for item in effects))
        gated = next(item for item in effects if item.get("op") == "sequence" and any(child.get("op") == "consume_resource" for child in item.get("effects", ())))
        self.assertEqual("consume_resource", gated["effects"][0]["op"])
        self.assertEqual("damage", gated["effects"][1]["op"])

    def test_target_binding_is_explicitly_partial(self) -> None:
        rule = self.candidate["rules"]["10474120"]
        self.assertEqual("partial", rule["support"])
        effects = _effects(rule)
        self.assertTrue(any(item.get("op") == "remove_abilities" for item in effects))
        self.assertTrue(any(item.get("op") == "modify_damage_taken" for item in effects))
        self.assertTrue(any("select_targets" in item for item in rule["unparsed_clauses"]))

    def test_destroyed_amulet_random_exact_copy(self) -> None:
        effects = _effects(self.candidate["rules"]["10664110"])
        summons = [item for item in effects if item.get("op") == "summon" and "resource_selector" in item]
        self.assertTrue(summons)
        selector = summons[0]["resource_selector"]
        self.assertEqual("destroyed_this_match", selector.get("zone"))
        self.assertEqual("card_id", selector.get("distinct_by"))
        self.assertEqual("exact", summons[0].get("copy_mode"))
        self.assertTrue(summons[0].get("preserve_state"))

    def test_repeat_union_and_mode_choice(self) -> None:
        cupitan = self.candidate["rules"]["10413110"]
        self.assertTrue(any(item.get("op") == "repeat" and item.get("count") == 7 for item in _effects(cupitan)))
        mode_card = self.candidate["rules"]["10413310"]
        self.assertTrue(any(item.get("op") == "mode_choice" for item in _effects(mode_card)))
        self.assertTrue(any(item.get("op") == "conditional" for item in _effects(mode_card)))
        # Super Skybound Art replaces the ordinary mode choice.  It must be a
        # single conditional ability; retaining the base ability beside it
        # would execute a selected mode twice when the burst is unavailable.
        play_abilities = [
            ability
            for mode in mode_card["modes"]
            for ability in mode["abilities"]
            if ability.get("trigger") == "on_play"
        ]
        self.assertEqual(len(play_abilities), 1)
        self.assertEqual(play_abilities[0]["effects"][0].get("op"), "conditional")

    def test_copy_semantics_and_self_summon(self) -> None:
        # The source explicitly distinguishes summon_copy (which SWB-RL marks
        # as requiring a state audit) from summon_exact_copy.  Preserve that
        # distinction instead of upgrading the former based on card text.
        self.assertTrue(any(item.get("card_id") == 10424110 for item in _effects(self.candidate["rules"]["10424110"])))
        self.assertTrue(any(item.get("op") == "copy" and item.get("copy_mode") == "exact" and item.get("preserve_state") for item in _effects(self.candidate["rules"]["10711110"])))
        self.assertTrue(any(item.get("op") == "copy" and item.get("copy_mode") == "card" for item in _effects(self.candidate["rules"]["10333110"])))
        self.assertEqual("partial", self.candidate["rules"]["10333110"]["support"])

    def test_invocation_uses_schema_approved_selector(self) -> None:
        effects = _effects(self.candidate["rules"]["10404110"])
        invocations = [item for item in effects if item.get("op") == "invoke"]
        self.assertTrue(invocations)
        self.assertEqual("deck", invocations[0].get("resource_selector", {}).get("zone"))
        self.assertNotIn("from_zone", invocations[0])

    def test_numeric_subtraction_is_folded(self) -> None:
        # A generated rule must not hide an unsupported value-level negate
        # expression.  Dynamic subtraction remains partial and is reported.
        for card_id, rule in self.candidate["rules"].items():
            if rule.get("support") != "generated":
                continue
            self.assertFalse(any(item.get("op") in {"negate", "swb_expr"} for item in _effects(rule)), card_id)

    def test_writer_isolated_from_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = write_adapter_artifacts(self.candidate, self.report, directory)
            self.assertEqual({"candidate", "report", "markdown"}, set(paths))
            for path in paths.values():
                self.assertTrue(path.exists())
            self.assertFalse((Path(directory) / "card_rules_v2.json").exists())


if __name__ == "__main__":
    unittest.main()
