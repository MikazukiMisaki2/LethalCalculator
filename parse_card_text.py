"""Parse normalized card clauses into the auditable intermediate AST."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from card_text_ast import card_to_ast


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/generated/card_text_normalized.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_text_ast.json"))
    parser.add_argument("--report", type=Path, default=Path("data/generated/card_text_parse_report.json"))
    parser.add_argument("--primary-language", choices=("eng", "chs", "bilingual"), default="eng")
    args = parser.parse_args()
    normalized = json.loads(args.input.read_text(encoding="utf-8"))
    ast_cards = {cid: card_to_ast(card, primary_language=args.primary_language) for cid, card in normalized.get("cards", {}).items()}
    stats = Counter()
    for card in ast_cards.values():
        stats["cards"] += 1
        stats["partial_cards"] += card.get("support") == "partial"
        stats["bilingual_conflicts"] += len(card.get("bilingual_conflicts", []))
        for ability in card.get("abilities", []):
            stats["unique_abilities"] += 1
            stats[f"classification_{ability.get('classification', 'unknown')}"] += 1
        for clause in card.get("clauses", []):
            stats["clauses"] += 1
            stats["parsed_clauses"] += bool(clause.get("effects"))
            stats["unparsed_clauses"] += len(clause.get("unparsed_clauses", []))
            stats.update(effect.get("kind") for effect in clause.get("effects", []))
    output = {"schema_version": 1, "normalized_source": str(args.input), "primary_language": args.primary_language, "cards": ast_cards}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(dict(stats), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({stats['cards']} cards)")
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
