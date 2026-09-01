import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from rule_support import validate_support


ROOT = Path(__file__).resolve().parent


class CardRulesSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "schemas" / "card_rules_v2.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def test_complex_v21_fixture_is_valid(self):
        fixture = json.loads((ROOT / "fixtures" / "rules_v21_complex.json").read_text(encoding="utf-8"))
        errors = list(self.validator.iter_errors(fixture))
        self.assertEqual(errors, [], "\n".join(error.message for error in errors))

    def test_required_step1_fixtures_are_valid(self):
        for name in ("rules_basic.json", "rules_random_repeat.json", "rules_resources.json", "rules_faith_crest.json"):
            with self.subTest(fixture=name):
                fixture = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
                self.assertEqual(list(self.validator.iter_errors(fixture)), [])

    def test_invalid_effect_fixtures_are_rejected(self):
        cases = json.loads((ROOT / "fixtures" / "invalid_rules_v21.json").read_text(encoding="utf-8"))["cases"]
        for case in cases:
            instance = {
                "schema_version": 2,
                "catalog_version": 1,
                "game_version": "invalid-fixture",
                "rules": {
                    "1": {
                        "card_id": 1,
                        "support": "generated",
                        "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [case["effect"]]}]}],
                    }
                },
            }
            with self.subTest(case=case["name"]):
                self.assertTrue(list(self.validator.iter_errors(instance)))

    def test_support_matrix_covers_every_schema_operation(self):
        schema_ops = set(self.schema["$defs"]["effect"]["properties"]["op"]["enum"])
        support = json.loads((ROOT / "schemas" / "card_rules_v2_support.json").read_text(encoding="utf-8"))
        self.assertEqual(schema_ops, set(support["operations"]))
        self.assertTrue(set(support["operations"].values()) <= set(support["status_values"]))

    def test_verified_rules_cannot_use_planned_operations(self):
        matrix = json.loads((ROOT / "schemas" / "card_rules_v2_support.json").read_text(encoding="utf-8"))
        invalid = {"schema_version": 2, "catalog_version": 1, "rules": {"1": {"card_id": 1, "support": "verified", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [{"op": "repeat", "count": 2, "effects": [{"op": "draw", "count": 1}]}]}]}]}}}
        self.assertTrue(validate_support(invalid, matrix))


if __name__ == "__main__":
    unittest.main()
