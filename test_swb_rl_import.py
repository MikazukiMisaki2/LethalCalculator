"""Regression tests for the read-only SWB-RL compatibility import.

The importer is intentionally kept separate from the live lethal runtime.  A
source refresh should therefore be caught by deterministic catalog/rulebook
checks before an adapter is allowed to consume it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from import_swb_rl import build_import_artifacts, write_import_artifacts


ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("SWB_RL_PATH", r"D:\Github\SWB-RL"))
CURRENT_CATALOG = ROOT / "data/generated/card_catalog.json"
CURRENT_RULES = ROOT / "data/generated/card_rules_v2.json"
CURRENT_SCHEMA = ROOT / "schemas/card_rules_v2.schema.json"


@unittest.skipUnless(SOURCE.exists(), f"SWB-RL checkout not found: {SOURCE}")
class SWBRLImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifacts = build_import_artifacts(
            SOURCE,
            current_catalog_path=CURRENT_CATALOG,
            current_rules_path=CURRENT_RULES,
            current_schema_path=CURRENT_SCHEMA,
        )

    def test_catalog_projection_and_current_overlap(self) -> None:
        catalog = self.artifacts["catalog"]
        report = self.artifacts["report"]
        summary = report["summary"]

        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(len(catalog["cards"]), 826)
        self.assertEqual(summary["database_collectible_count"], 735)
        self.assertEqual(summary["database_generated_count"], 91)
        self.assertEqual(summary["overlap_count"], 826)
        self.assertEqual(len(summary["current_only_ids"]), 78)
        self.assertEqual(summary["reference_only_ids"], [])
        self.assertEqual(summary["core_field_mismatch_count"], 0)
        self.assertEqual(summary["name_mismatch_count"], 0)

    def test_projection_keeps_references_modes_and_stable_hashes(self) -> None:
        cards = self.artifacts["catalog"]["cards"]
        fairy_tamer = cards["10011110"]
        self.assertEqual(fairy_tamer["references"][0]["referenced_card_id"], 90011110)
        self.assertTrue(fairy_tamer["source_hash"])
        self.assertEqual(len(fairy_tamer["source_hash"]), 64)

        lyanthoth = cards["10664120"]
        self.assertEqual(lyanthoth["name"]["eng"], "Lyanthoth, Eld Tome")
        self.assertTrue(lyanthoth["alt_modes"][0]["mode_type"])
        self.assertIn("faith", lyanthoth["alt_modes"][0]["text_eng"].lower())
        self.assertIn("Necromancy", cards["10753310"]["skill_texts"][0]["text_eng"] or "")

        known_ids = set(cards)
        for card in cards.values():
            for reference in card["references"]:
                ref_id = reference.get("referenced_card_id")
                if ref_id is not None:
                    self.assertIn(str(ref_id), known_ids)

    def test_rulebook_statistics_expose_adapter_boundary(self) -> None:
        stats = self.artifacts["report"]["rule_statistics"]
        self.assertEqual(stats["rule_file_count"], 119)
        self.assertEqual(stats["real_rule_card_id_count"], 750)
        self.assertEqual(stats["synthetic_rule_card_id_count"], 26)
        self.assertGreaterEqual(stats["effect_kinds_used_in_rules_count"], 80)
        self.assertGreaterEqual(stats["effect_kinds_used_all_definitions_count"], 90)
        self.assertIn("repeat", stats["literal_operation_intersection"])
        self.assertIn("reanimate", stats["literal_operation_intersection"])
        self.assertEqual(
            stats["operation_compatibility"]["damage_unit"]["status"],
            "adapter_required",
        )
        self.assertEqual(
            stats["operation_compatibility"]["select_targets"]["status"],
            "schema_gap",
        )

    def test_generated_report_is_deterministic(self) -> None:
        first = json.dumps(self.artifacts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        second = build_import_artifacts(
            SOURCE,
            current_catalog_path=CURRENT_CATALOG,
            current_rules_path=CURRENT_RULES,
            current_schema_path=CURRENT_SCHEMA,
        )
        second_json = json.dumps(second, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(first, second_json)

    def test_writer_emits_only_import_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_import_artifacts(self.artifacts, Path(temp_dir))
            self.assertEqual(
                {path.name for path in paths.values()},
                {
                    "swb_catalog_projection.json",
                    "swb_rulebook_raw.json",
                    "swb_compatibility_report.json",
                    "swb_compatibility_report.md",
                },
            )
            report = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["overlap_count"], 826)


if __name__ == "__main__":
    unittest.main()
