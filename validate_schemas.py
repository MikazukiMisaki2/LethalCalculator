"""Validate CardCatalog/CardRules fixtures with JSON Schema Draft 2020-12."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent


def validate(instance_path: Path, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "\n".join(f"- {list(error.path)}: {error.message}" for error in errors)
        raise SystemExit(f"{instance_path} is invalid:\n{details}")
    print(f"OK {instance_path.name} against {schema_path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=ROOT / "fixtures" / "catalog_minimal.json")
    parser.add_argument("--rules", type=Path, default=ROOT / "fixtures" / "rules_faith_crest.json")
    args = parser.parse_args()
    validate(args.catalog, ROOT / "schemas" / "card_catalog.schema.json")
    validate(args.rules, ROOT / "schemas" / "card_rules_v2.schema.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
