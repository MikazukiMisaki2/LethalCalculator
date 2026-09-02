import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from rule_coverage import build_coverage_report, validate_coverage_report
from rule_support import validate_contract, validate_support


ROOT = Path(__file__).resolve().parent


class Step7ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "schemas" / "card_rules_v2.schema.json").read_text(encoding="utf-8"))
        cls.matrix = json.loads((ROOT / "schemas" / "card_rules_v2_support.json").read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema)

    @staticmethod
    def _catalog(*card_ids: int):
        return {"schema_version": 1, "type_map": {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4}, "cards": {str(card_id): {"card_id": card_id} for card_id in card_ids}}

    @staticmethod
    def _rules(card_id: int, effect: dict):
        return {"schema_version": 2, "catalog_version": 1, "rules": {str(card_id): {"card_id": card_id, "support": "generated", "modes": [{"kind": "normal", "cost": 1, "abilities": [{"trigger": "on_play", "effects": [effect]}]}]}}}

    def test_schema_rejects_unknown_keyword_and_nested_trigger(self):
        unknown_keyword = self._rules(1, {"op": "grant_keyword", "keyword": "teleport", "target": {"scope": "self"}})
        unknown_trigger = self._rules(1, {"op": "replicate_ability", "trigger": "when_magic_happens"})
        self.assertTrue(list(self.validator.iter_errors(unknown_keyword)))
        self.assertTrue(list(self.validator.iter_errors(unknown_trigger)))

    def test_semantic_gate_rejects_missing_card_resource_and_trigger(self):
        instance = self._rules(1, {"op": "add_to_hand", "card_id": 999, "count": 1})
        # Deliberately bypass Schema here to exercise all cross-document
        # checks in one call.
        instance["rules"]["1"]["modes"][0]["abilities"][0]["trigger"] = "not_a_trigger"
        instance["rules"]["1"]["modes"][0]["abilities"][0]["effects"][0]["resource"] = "not_a_resource"
        errors = validate_contract(instance, self._catalog(1), schema=self.schema, matrix=self.matrix)
        self.assertTrue(any("absent from Catalog" in error for error in errors))
        self.assertTrue(any("unknown trigger" in error for error in errors))
        self.assertTrue(any("unknown resource" in error for error in errors))

    def test_coverage_report_tracks_every_card_and_source_hash(self):
        catalog = self._catalog(1, 2)
        rules = {
            "schema_version": 2,
            "catalog_version": 1,
            "rules": {
                "1": {"card_id": 1, "support": "generated", "source_hash": "a", "modes": []},
                "2": {"card_id": 2, "support": "partial", "source_hash": "b", "unparsed_clauses": ["x"], "modes": []},
            },
        }
        report = build_coverage_report(catalog, rules)
        self.assertEqual(report["support"], {"verified": 0, "generated": 1, "partial": 1, "unsupported": 0})
        self.assertEqual(set(report["support_by_card"]), {"1", "2"})
        self.assertEqual(report["unparsed_clause_count_by_card"]["2"], 1)
        self.assertEqual(validate_coverage_report(report, catalog, rules), [])
        report["support_by_card"]["2"] = "generated"
        self.assertTrue(validate_coverage_report(report, catalog, rules))

    def test_keywords_in_support_matrix_match_schema_vocab(self):
        schema_keywords = set(self.schema["$defs"]["keyword"]["enum"])
        self.assertEqual(schema_keywords, set(self.matrix["keywords"]))
        self.assertTrue(set(self.matrix["keywords"].values()) <= set(self.matrix["status_values"]))

    def test_verified_rule_cannot_use_planned_keyword(self):
        matrix = json.loads(json.dumps(self.matrix))
        matrix["keywords"]["storm"] = "planned"
        instance = self._rules(1, {"op": "grant_keyword", "keyword": "storm", "target": {"scope": "self"}})
        instance["rules"]["1"]["support"] = "verified"
        errors = validate_support(instance, matrix)
        self.assertTrue(any("verified rule uses keyword" in error for error in errors))

    @unittest.skipUnless((ROOT / "data" / "generated" / "card_catalog.json").exists(), "generated artifacts are not available")
    def test_current_generated_artifacts_pass_contract(self):
        catalog = json.loads((ROOT / "data" / "generated" / "card_catalog.json").read_text(encoding="utf-8"))
        rules = json.loads((ROOT / "data" / "generated" / "card_rules_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(json.loads((ROOT / "schemas" / "card_catalog.schema.json").read_text(encoding="utf-8"))).iter_errors(catalog)), [])
        self.assertEqual(list(self.validator.iter_errors(rules)), [])
        self.assertEqual(validate_contract(rules, catalog, schema=self.schema, matrix=self.matrix), [])
        coverage_path = ROOT / "data" / "generated" / "card_rules_coverage_report.json"
        if coverage_path.exists():
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            self.assertEqual(validate_coverage_report(coverage, catalog, rules), [])


if __name__ == "__main__":
    unittest.main()
