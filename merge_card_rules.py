"""Merge generated rules with reviewed manual overrides."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from rule_support import validate_support


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", type=Path, default=Path("data/generated/card_rules_generated.json"))
    parser.add_argument("--overrides", type=Path, default=Path("card_rules_overrides.json"))
    parser.add_argument("--schema", type=Path, default=Path("schemas/card_rules_v2.schema.json"))
    parser.add_argument("--support", type=Path, default=Path("schemas/card_rules_v2_support.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_rules_v2.json"))
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
    output = {"schema_version": 2, "ruleset_revision": overrides.get("ruleset_revision", 1), "catalog_version": generated.get("catalog_version", 1), "game_version": generated.get("game_version", ""), "rules": rules}
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(output))
    errors.extend(validate_support(output, json.loads(args.support.read_text(encoding="utf-8"))))
    if errors:
        raise SystemExit("rule merge validation failed:\n" + "\n".join(str(error) for error in errors[:20]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(rules)} rules, {len(overrides.get('rules', {}))} overrides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
