"""Merge generated rules with reviewed manual overrides."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from rule_coverage import build_coverage_report
from rule_support import validate_contract


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", type=Path, default=Path("data/generated/card_rules_generated.json"))
    parser.add_argument("--overrides", type=Path, default=Path("card_rules_overrides.json"))
    parser.add_argument("--schema", type=Path, default=Path("schemas/card_rules_v2.schema.json"))
    parser.add_argument("--support", type=Path, default=Path("schemas/card_rules_v2_support.json"))
    parser.add_argument("--catalog", type=Path, default=Path("data/generated/card_catalog.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_rules_v2.json"))
    parser.add_argument("--coverage-report", type=Path, default=Path("data/generated/card_rules_coverage_report.json"))
    args = parser.parse_args()
    generated = json.loads(args.generated.read_text(encoding="utf-8"))
    overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
    rules = dict(generated.get("rules", {}))
    for card_id, override in overrides.get("rules", {}).items():
        original = rules.get(card_id)
        if not original:
            raise SystemExit(f"override {card_id} has no generated source rule")
        if override.get("support") != "verified":
            raise SystemExit(f"override {card_id} must use support=verified")
        if override.get("source_hash") != original.get("source_hash"):
            raise SystemExit(f"override {card_id} is stale or missing source_hash; expected {original.get('source_hash')}")
    rules.update(overrides.get("rules", {}))
    # The support matrix is the executable contract for this output.  Keep
    # the merged artifact on the v2 revision even when an older/empty
    # overrides file has not been bumped yet.
    support_matrix = json.loads(args.support.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    revision = max(int(overrides.get("ruleset_revision", 1) or 1), int(support_matrix.get("ruleset_revision", 2) or 2))
    output = {"schema_version": 2, "ruleset_revision": revision, "catalog_version": generated.get("catalog_version", 1), "game_version": generated.get("game_version", ""), "rules": rules}
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(output))
    errors.extend(validate_contract(output, catalog, schema=schema, matrix=support_matrix))
    if errors:
        raise SystemExit("rule merge validation failed:\n" + "\n".join(str(error) for error in errors[:20]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.coverage_report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage = build_coverage_report(
        catalog,
        output,
        source_catalog=args.catalog.name,
        source_rules=args.output.name,
        phase="merge",
    )
    args.coverage_report.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(rules)} rules, {len(overrides.get('rules', {}))} overrides)")
    print(f"wrote {args.coverage_report} (per-card coverage)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
