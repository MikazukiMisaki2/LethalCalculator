"""Merge cleaned crawler cards, structured cards.json and legacy effects data.

The crawler catalog is authoritative for card text, current type encoding and
Token relations.  ``cards.json`` supplements structured alt modes (Faith,
Crest, Accelerate, ...).  The legacy ``card_effects_chs.json`` is only a
fallback/consistency oracle during migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clean_cards import build_catalog_from_crawler, _read_source


def _load(path_or_url: str) -> tuple[Any, bytes, str]:
    raw, source = _read_source(path_or_url)
    return json.loads(raw.decode("utf-8")), raw, source


def _legacy_cards(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("cards"), dict):
        return {str(k): v for k, v in payload["cards"].items() if isinstance(v, dict)}
    return {}


def _compare_and_enrich(catalog: dict[str, Any], legacy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cards = catalog["cards"]
    catalog_ids = set(cards)
    legacy_ids = set(legacy)
    relation_fallbacks = 0
    relation_mismatches: list[dict[str, Any]] = []
    type_mismatches: list[dict[str, Any]] = []
    for cid, card in cards.items():
        old = legacy.get(cid)
        if old is None:
            continue
        old_related = sorted(int(x) for x in old.get("related_card_ids", []) if isinstance(x, int))
        old_specific = sorted(int(x) for x in old.get("specific_effect_card_ids", []) if isinstance(x, int))
        new_related = sorted(card.get("related_card_ids", []))
        new_specific = sorted(card.get("specific_effect_card_ids", []))
        if not new_related and old_related:
            card["related_card_ids"] = old_related
            relation_fallbacks += 1
        if not new_specific and old_specific:
            card["specific_effect_card_ids"] = old_specific
            relation_fallbacks += 1
        if old_related != sorted(card.get("related_card_ids", [])) or old_specific != sorted(card.get("specific_effect_card_ids", [])):
            relation_mismatches.append({"card_id": int(cid), "catalog_related": card.get("related_card_ids", []), "legacy_related": old_related, "catalog_specific": card.get("specific_effect_card_ids", []), "legacy_specific": old_specific})
        old_type = old.get("type")
        if isinstance(old_type, int) and old_type != card.get("raw_type"):
            type_mismatches.append({"card_id": int(cid), "catalog_raw_type": card.get("raw_type"), "legacy_type": old_type})
        card["sources"]["effects_json"] = True
    return {
        "catalog_cards": len(catalog_ids),
        "legacy_cards": len(legacy_ids),
        "legacy_only_ids": sorted(int(x) for x in legacy_ids - catalog_ids),
        "catalog_only_ids": sorted(int(x) for x in catalog_ids - legacy_ids),
        "relation_fallbacks": relation_fallbacks,
        "relation_mismatches": relation_mismatches,
        "type_mismatches": type_mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chs", required=True, help="crawler shadowverse_cards_chs.json")
    parser.add_argument("--en", required=True, help="crawler shadowverse_cards_en.json")
    parser.add_argument("--structured", help="optional structured cards.json")
    parser.add_argument("--legacy-effects", help="optional legacy card_effects_chs.json")
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_catalog.json"))
    parser.add_argument("--report", type=Path, default=Path("data/generated/card_catalog_merge_report.json"))
    parser.add_argument("--fail-on-conflict", action="store_true")
    args = parser.parse_args()

    chs, chs_raw, chs_source = _load(args.chs)
    en, en_raw, _ = _load(args.en)
    structured = structured_raw = None
    if args.structured:
        structured, structured_raw, _ = _load(args.structured)
    catalog, stats = build_catalog_from_crawler(
        chs, en, structured,
        chs_sha256=hashlib.sha256(chs_raw).hexdigest(),
        en_sha256=hashlib.sha256(en_raw).hexdigest(),
        structured_sha256=hashlib.sha256(structured_raw).hexdigest() if structured_raw else None,
        chs_source=chs_source,
    )

    comparison: dict[str, Any] = {"legacy": None}
    if args.legacy_effects:
        legacy_payload, _, _ = _load(args.legacy_effects)
        comparison["legacy"] = _compare_and_enrich(catalog, _legacy_cards(legacy_payload))
    catalog["source"]["generated_at"] = datetime.now(timezone.utc).isoformat()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"generated_at": catalog["source"]["generated_at"], "stats": stats, "comparison": comparison}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    conflicts = comparison.get("legacy") or {}
    conflict_count = len(conflicts.get("relation_mismatches", [])) + len(conflicts.get("type_mismatches", []))
    print(f"wrote {args.output} ({stats}); report={args.report}; conflicts={conflict_count}")
    return 2 if args.fail_on_conflict and conflict_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
