"""Run the complete Step 7 CardCatalog/CardRules contract gate.

The gate combines Draft 2020-12 schema validation with cross-document checks:
explicit card/token references must resolve in the catalog, vocabulary values
must be known, and the per-card coverage report must describe the exact rules
being shipped.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from rule_coverage import validate_coverage_report
from rule_support import validate_contract


ROOT = Path(__file__).resolve().parent


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def _schema_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    Draft202012Validator.check_schema(schema)
    return [
        f"{list(error.path)}: {error.message}"
        for error in sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda error: list(error.path))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "generated" / "card_catalog.json")
    parser.add_argument("--rules", type=Path, default=ROOT / "data" / "generated" / "card_rules_v2.json")
    parser.add_argument("--coverage", type=Path, default=ROOT / "data" / "generated" / "card_rules_coverage_report.json")
    parser.add_argument("--catalog-schema", type=Path, default=ROOT / "schemas" / "card_catalog.schema.json")
    parser.add_argument("--rules-schema", type=Path, default=ROOT / "schemas" / "card_rules_v2.schema.json")
    parser.add_argument("--support", type=Path, default=ROOT / "schemas" / "card_rules_v2_support.json")
    args = parser.parse_args()

    catalog = _load(args.catalog)
    rules = _load(args.rules)
    catalog_schema = _load(args.catalog_schema)
    rules_schema = _load(args.rules_schema)
    support = _load(args.support)
    failures: list[str] = []

    catalog_errors = _schema_errors(catalog, catalog_schema)
    if catalog_errors:
        failures.extend(f"Catalog: {error}" for error in catalog_errors)
    else:
        print(f"OK {args.catalog.name} against {args.catalog_schema.name}")
    rules_errors = _schema_errors(rules, rules_schema)
    if rules_errors:
        failures.extend(f"Rules: {error}" for error in rules_errors)
    else:
        print(f"OK {args.rules.name} against {args.rules_schema.name}")

    failures.extend(validate_contract(rules, catalog, schema=rules_schema, matrix=support))
    if failures:
        print("Step 7 contract FAILED:")
        print("\n".join(f"- {error}" for error in failures[:100]))
        if len(failures) > 100:
            print(f"- ... {len(failures) - 100} more")
        return 1

    coverage = _load(args.coverage)
    if not isinstance(coverage, dict):
        print("Step 7 contract FAILED:\n- coverage report must be an object")
        return 1
    coverage_errors = validate_coverage_report(coverage, catalog, rules)
    if coverage_errors:
        print("Step 7 contract FAILED:")
        print("\n".join(f"- {error}" for error in coverage_errors))
        return 1
    print(f"OK {args.coverage.name}: per-card coverage matches catalog and rules ({len(rules.get('rules', {}))} cards)")
    print("Step 7 contract PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
