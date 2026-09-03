"""Regression tests for the conservative SWB-RL migration selector."""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from promote_swb_rules import MigrationSelectionError, build_migration_bundle
from rule_support import validate_contract


ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("SWB_RL_PATH", r"D:\SWB-RL"))
CANDIDATE_PATH = ROOT / "data" / "imported" / "swb_card_rules_v2_candidate.json"
BASE_PATH = ROOT / "data" / "generated" / "card_rules_v2.json"
CATALOG_PATH = ROOT / "data" / "generated" / "card_catalog.json"
SCHEMA_PATH = ROOT / "schemas" / "card_rules_v2.schema.json"
MATRIX_PATH = ROOT / "schemas" / "card_rules_v2_support.json"


@unittest.skipUnless(CANDIDATE_PATH.exists(), "run adapt_swb_rules.py before promotion tests")
class SwbRulePromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        cls.base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    def assert_valid(self, document: dict) -> None:
        errors = list(Draft202012Validator(self.schema).iter_errors(document))
        errors.extend(validate_contract(document, self.catalog, schema=self.schema, matrix=self.matrix))
        self.assertEqual([], errors, "migration document must pass schema and cross-document contract")

    def test_default_selection_is_only_adapter_clean_generated(self) -> None:
        document, report = build_migration_bundle(
            self.candidate,
            catalog=self.catalog,
            schema=self.schema,
            matrix=self.matrix,
        )
        selected = document["rules"]
        self.assertEqual(report["summary"]["selected_rule_count"], len(selected))
        self.assertGreater(len(selected), 0)
        self.assertTrue(all(rule.get("support") == "generated" for rule in selected.values()))
        self.assertTrue(all(not rule.get("unparsed_clauses") for rule in selected.values()))
        self.assert_valid(document)

    def test_partial_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(MigrationSelectionError):
            build_migration_bundle(
                self.candidate,
                catalog=self.catalog,
                schema=self.schema,
                matrix=self.matrix,
                card_ids="10474120",
            )
        document, _report = build_migration_bundle(
            self.candidate,
            catalog=self.catalog,
            schema=self.schema,
            matrix=self.matrix,
            card_ids="10474120",
            allowed_support=("partial",),
            allow_partial=True,
        )
        self.assertEqual(set(document["rules"]), {"10474120"})
        self.assert_valid(document)

    def test_verified_base_is_not_replaced_without_opt_in(self) -> None:
        self.assertEqual(self.base["rules"]["10452130"].get("support"), "verified")
        self.assertEqual(self.candidate["rules"]["10452130"].get("support"), "generated")
        with self.assertRaises(MigrationSelectionError):
            build_migration_bundle(
                self.candidate,
                base=self.base,
                catalog=self.catalog,
                schema=self.schema,
                matrix=self.matrix,
                card_ids="10452130",
                mode="merged",
            )

    def test_selection_rejects_card_absent_from_target_catalog(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["rules"]["99999999"] = copy.deepcopy(candidate["rules"]["10753310"])
        candidate["rules"]["99999999"]["card_id"] = 99999999
        with self.assertRaises(MigrationSelectionError):
            build_migration_bundle(
                candidate,
                catalog=self.catalog,
                schema=self.schema,
                matrix=self.matrix,
                card_ids="99999999",
            )

    def test_explicit_overlay_is_deterministic_and_schema_valid(self) -> None:
        kwargs = dict(
            catalog=self.catalog,
            schema=self.schema,
            matrix=self.matrix,
            card_ids="10753310,10413110,10413310",
        )
        document, report = build_migration_bundle(self.candidate, **kwargs)
        again, again_report = build_migration_bundle(self.candidate, **kwargs)
        self.assertEqual(document, again)
        self.assertEqual(report, again_report)
        self.assertEqual(set(document["rules"]), {"10413110", "10413310", "10753310"})
        self.assert_valid(document)

    def test_merged_mode_preserves_unselected_and_changes_only_allowlist(self) -> None:
        before = copy.deepcopy(self.base["rules"])
        document, report = build_migration_bundle(
            self.candidate,
            base=self.base,
            catalog=self.catalog,
            schema=self.schema,
            matrix=self.matrix,
            card_ids="10753310,10413110,10413310",
            mode="merged",
        )
        self.assertEqual(set(document["rules"]), set(before))
        for card_id in ("10753310", "10413110", "10413310"):
            self.assertEqual(document["rules"][card_id], self.candidate["rules"][card_id])
        for card_id in set(before) - {"10753310", "10413110", "10413310"}:
            self.assertEqual(document["rules"][card_id], before[card_id])
        self.assertEqual(report["summary"]["changed_rule_count"], 3)
        self.assert_valid(document)


if __name__ == "__main__":
    unittest.main()
