"""Classify bilingual parser gaps and cluster unparsed ability text."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def signature(text: str) -> str:
    value = text.lower().strip()
    value = re.sub(r'"[^"]+"|“[^”]+”|『[^』]+』', "<CARD_OR_EFFECT>", value)
    value = re.sub(r"\b\d+\b", "<N>", value)
    value = re.sub(r"\bx\b", "<X>", value)
    value = re.sub(r"\s+", " ", value)
    return value


CATEGORY_PATTERNS = (
    ("damage", r"damage|伤害"),
    ("buff", r"give .*?[+-]\d|\+\d+/|[-+]\d+/[-+]\d+"),
    ("storm_rush", r"storm|rush|疾驰|突进"),
    ("destroy_banish", r"destroy|banish|破坏|消灭"),
    ("recover_pp", r"recover .*play point|回复.*(?:pp|能量点)"),
    ("summon", r"summon|召唤"),
    ("evolve", r"evolve|进化"),
    ("repeat_random", r"times|random|随机|发动.*次"),
    ("condition_resource", r"rally|combo|necromancy|overflow|faith|crest|spellboost|协作|连击|唤灵|觉醒|信仰|纹章|魔力增幅"),
)


def categories(text: str) -> list[str]:
    result = [name for name, pattern in CATEGORY_PATTERNS if re.search(pattern, text, re.I)]
    return result or ["other"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/generated/card_text_ast.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/card_text_triage_report.json"))
    args = parser.parse_args()
    ast = json.loads(args.input.read_text(encoding="utf-8"))
    classifications = Counter()
    clusters: dict[str, list[dict]] = defaultdict(list)
    category_counts = Counter()
    semantic_conflicts = []
    for cid, card in ast.get("cards", {}).items():
        for ability in card.get("abilities", []):
            classification = ability.get("classification", "unknown")
            classifications[classification] += 1
            if classification == "semantic_conflict":
                semantic_conflicts.append({"card_id": int(cid), "name": card.get("name", {}), "ability": ability})
            if classification in ("unparsed", "parser_asymmetry", "missing_translation"):
                source = ability.get("source_clause", {})
                text = source.get("eng") or source.get("chs") or ""
                category_counts.update(categories(text))
                clusters[signature(text)].append({"card_id": int(cid), "name": card.get("name", {}), "classification": classification, "source_clause": source})
    clustered = [{"signature": key, "count": len(items), "examples": items[:5]} for key, items in clusters.items()]
    clustered.sort(key=lambda item: (-item["count"], item["signature"]))
    report = {"classifications": dict(classifications), "conflict_split": {"semantic_conflict": classifications.get("semantic_conflict", 0), "parser_asymmetry": classifications.get("parser_asymmetry", 0)}, "semantic_conflict_count": len(semantic_conflicts), "semantic_conflicts": semantic_conflicts, "unparsed_category_counts": dict(category_counts), "unparsed_cluster_count": len(clustered), "unparsed_clusters": clustered}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(clustered)} clusters, {len(semantic_conflicts)} semantic conflicts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
