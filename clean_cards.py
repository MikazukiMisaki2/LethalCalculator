"""Build the compact CardCatalog from the upstream cards.json.

The upstream file is kept as an immutable/raw input.  This command only keeps
fields needed by the catalog/rules compiler and deliberately preserves the
markup in effect text (``<ev>``, ``<sev>``, ``<ridx>`` ...), because the rules
compiler needs those boundaries.

Examples:
    python clean_cards.py --input cards.json
    python clean_cards.py --input https://sva.hypd.asia/data/cards.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_URL = "https://sva.hypd.asia/data/cards.json"
DEFAULT_OUTPUT = Path("data/generated/card_catalog.json")


def _read_source(source: str) -> tuple[bytes, str]:
    if source.startswith(("http://", "https://")):
        request = Request(source, headers={"User-Agent": "LethalCalculator/1.0"})
        with urlopen(request, timeout=30) as response:  # noqa: S310 - explicit user input
            return response.read(), source
    path = Path(source)
    return path.read_bytes(), str(path.resolve())


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _is_mojibake(value: str) -> bool:
    return "\ufffd" in value


def _canonical_type(raw_type: Any) -> str:
    # cards.json uses 1/2/3.  Type 2 can be refined to countdown_amulet by
    # the secondary Tracker effect catalog; do not guess from display text.
    return {1: "follower", 2: "amulet", 3: "spell"}.get(raw_type, "unknown")


def _canonical_crawler_type(raw_type: Any) -> str:
    """Normalize the crawler/API's Tracker-compatible type values."""
    return {1: "follower", 2: "amulet", 3: "countdown_amulet", 4: "spell"}.get(raw_type, "unknown")


def _clean_texts(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key"))
        chs = _text(item.get("text_chs"))
        eng = _text(item.get("text_eng"))
        if not (key or chs or eng):
            continue
        dedupe_key = (chs.strip(), eng.strip())
        if dedupe_key in seen:
            existing = result[seen[dedupe_key]]
            keys = existing.setdefault("source_keys", [existing.get("key", "")])
            if key and key not in keys:
                keys.append(key)
            continue
        item_out: dict[str, Any] = {"key": key, "chs": chs, "eng": eng}
        seen[dedupe_key] = len(result)
        result.append(item_out)
    return result


def _dedupe_text_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate crawler skill/evo entries by bilingual text."""
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (_text(entry.get("chs")).strip(), _text(entry.get("eng")).strip())
        if not any(key):
            continue
        if key in seen:
            existing = result[seen[key]]
            source_keys = existing.setdefault("source_keys", [existing.get("key", "")])
            if entry.get("key") and entry["key"] not in source_keys:
                source_keys.append(entry["key"])
        else:
            seen[key] = len(result)
            result.append(dict(entry))
    return result


def _clean_alt_modes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        type_key = _text(item.get("type_key")) or "unknown"
        cost = item.get("cost", 0)
        if not isinstance(cost, int):
            cost = 0
        text = {"chs": _text(item.get("text_chs")), "eng": _text(item.get("text_eng"))}
        # The source currently repeats identical Crest entries.  Keep one
        # canonical copy while retaining a deterministic dedupe key.
        dedupe_key = json.dumps(
            {"type_key": type_key, "cost": cost, "text": text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(
            {
                "type_key": type_key,
                "type": _text(item.get("type")),
                "cost": cost,
                "text": text,
                "dedupe_key": hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16],
            }
        )
    return result


def _clean_skills(raw: Any) -> list[dict[str, int]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        skill_type = item.get("type")
        subtype = item.get("subtype")
        if not all(isinstance(value, int) for value in (skill_id, skill_type, subtype)):
            continue
        result.append({"skill_id": skill_id, "type": skill_type, "subtype": subtype})
    return result


def clean_card(raw: dict[str, Any]) -> dict[str, Any] | None:
    card_id = raw.get("card_id")
    base_card_id = raw.get("base_card_id", card_id)
    if not isinstance(card_id, int) or not isinstance(base_card_id, int):
        return None
    raw_type = raw.get("type")
    if not isinstance(raw_type, int):
        raw_type = 0
    tribe = raw.get("tribe")
    tribes = [tribe] if isinstance(tribe, int) and tribe > 0 else []
    evolves_to = raw.get("evolves_to")
    if not isinstance(evolves_to, int) or evolves_to <= 0:
        evolves_to = None
    card = {
        "card_id": card_id,
        "base_card_id": base_card_id,
        "name": {
            "chs": _text(raw.get("name_chs")),
            "eng": _text(raw.get("name_eng")),
        },
        "type": _canonical_type(raw_type),
        "raw_type": raw_type,
        "cost": max(0, raw.get("cost", 0)) if isinstance(raw.get("cost", 0), int) else 0,
        "stats": {
            "attack": max(0, raw.get("atk", 0)) if isinstance(raw.get("atk", 0), int) else 0,
            "life": max(0, raw.get("life", 0)) if isinstance(raw.get("life", 0), int) else 0,
        },
        "class_id": raw.get("class", 0) if isinstance(raw.get("class", 0), int) else 0,
        "tribes": tribes,
        "evolves_to": evolves_to,
        "skill_refs": _clean_skills(raw.get("skills")),
        "text": {"skill_texts": _clean_texts(raw.get("skill_texts"))},
        "alt_modes": _clean_alt_modes(raw.get("alt_modes")),
        "sources": {"cards_json": True, "effects_json": False},
    }
    # The upstream cards.json does not currently expose token-ness.  Omit the
    # field instead of claiming ``false``; the enrichment pass will fill it
    # from card_effects_chs.json.
    if isinstance(raw.get("is_token"), bool):
        card["is_token"] = raw["is_token"]
    return card


def _unwrap_crawler(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("crawler card data must contain a top-level data object")
    data = payload["data"]
    details = data.get("card_details")
    relations = data.get("cards")
    if not isinstance(details, dict) or not isinstance(relations, dict):
        raise ValueError("crawler data must contain data.card_details and data.cards")
    return details, relations


def _crawler_evo_text(raw: Any) -> str:
    if isinstance(raw, dict):
        return _text(raw.get("skill_text"))
    if isinstance(raw, list):
        return "\n".join(_text(item.get("skill_text")) for item in raw if isinstance(item, dict))
    return ""


def clean_crawler_card(
    card_id: str,
    chs: dict[str, Any],
    eng: dict[str, Any] | None,
    relation: dict[str, Any],
    structured: dict[str, Any] | None,
) -> dict[str, Any] | None:
    common = chs.get("common") if isinstance(chs.get("common"), dict) else chs
    en_common = eng.get("common") if isinstance(eng, dict) and isinstance(eng.get("common"), dict) else (eng or {})
    try:
        cid = int(common.get("card_id", card_id))
    except (TypeError, ValueError):
        return None
    base_id = common.get("base_card_id", cid)
    if not isinstance(base_id, int):
        base_id = cid
    raw_type = common.get("type", 0) if isinstance(common.get("type", 0), int) else 0
    tribes = common.get("tribes", [])
    if not isinstance(tribes, list):
        tribes = [tribes] if isinstance(tribes, int) else []
    tribes = [value for value in tribes if isinstance(value, int) and value > 0]
    skill = _text(common.get("skill_text"))
    evo = _crawler_evo_text(chs.get("evo"))
    en_skill = _text(en_common.get("skill_text"))
    en_evo = _crawler_evo_text(eng.get("evo") if isinstance(eng, dict) else None)
    structured = structured or {}
    structured_texts = structured.get("skill_texts", []) if isinstance(structured.get("skill_texts"), list) else []
    structured_chs = "\n".join(_text(item.get("text_chs")) for item in structured_texts if isinstance(item, dict) and _text(item.get("text_chs")))
    structured_name_chs = _text(structured.get("name_chs"))
    if _is_mojibake(skill) and structured_chs:
        skill = structured_chs
        evo = structured_chs
    name_chs = _text(common.get("name"))
    if _is_mojibake(name_chs) and structured_name_chs:
        name_chs = structured_name_chs
    card = {
        "card_id": cid,
        "base_card_id": base_id,
        "name": {"chs": name_chs, "eng": _text(en_common.get("name"))},
        "type": _canonical_crawler_type(raw_type),
        "raw_type": raw_type,
        "cost": common.get("cost", 0) if isinstance(common.get("cost", 0), int) else 0,
        "stats": {
            "attack": common.get("atk", 0) if isinstance(common.get("atk", 0), int) else 0,
            "life": common.get("life", 0) if isinstance(common.get("life", 0), int) else 0,
        },
        "class_id": common.get("class", 0) if isinstance(common.get("class", 0), int) else 0,
        "tribes": tribes,
        "evolves_to": structured.get("evolves_to") if isinstance(structured.get("evolves_to"), int) else None,
        "is_token": bool(common.get("is_token", False)),
        "related_card_ids": [int(x) for x in relation.get("related_card_ids", []) if isinstance(x, int)],
        "specific_effect_card_ids": [int(x) for x in relation.get("specific_effect_card_ids", []) if isinstance(x, int)],
        "skill_refs": _clean_skills(structured.get("skills")),
        "text": {"skill_texts": _dedupe_text_entries([
            {"key": "skill", "chs": skill, "eng": en_skill},
            {"key": "evo", "chs": evo, "eng": en_evo},
        ])},
        "alt_modes": _clean_alt_modes(structured.get("alt_modes")),
        "sources": {"cards_json": bool(structured), "effects_json": False, "crawler_chs": True, "crawler_en": eng is not None},
    }
    return card


def build_catalog_from_crawler(
    chs_payload: Any,
    en_payload: Any,
    structured_payload: Any | None,
    *,
    chs_sha256: str,
    en_sha256: str,
    structured_sha256: str | None,
    chs_source: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    chs_details, chs_relations = _unwrap_crawler(chs_payload)
    en_details, _ = _unwrap_crawler(en_payload)
    structured_by_id = {
        str(x.get("card_id")): x for x in structured_payload or []
        if isinstance(x, dict) and isinstance(x.get("card_id"), int)
    }
    cards: dict[str, dict[str, Any]] = {}
    missing_en = 0
    language_mismatch_ids: list[int] = []
    for raw_id, raw_chs in chs_details.items():
        raw_en = en_details.get(raw_id)
        if raw_en is None:
            missing_en += 1
        else:
            chs_common = raw_chs.get("common", raw_chs) if isinstance(raw_chs, dict) else {}
            en_common = raw_en.get("common", raw_en) if isinstance(raw_en, dict) else {}
            if any(chs_common.get(key) != en_common.get(key) for key in ("card_id", "base_card_id", "cost", "type", "atk", "life")):
                try:
                    language_mismatch_ids.append(int(raw_id))
                except (TypeError, ValueError):
                    pass
        relation = chs_relations.get(raw_id, {})
        card = clean_crawler_card(raw_id, raw_chs, raw_en, relation if isinstance(relation, dict) else {}, structured_by_id.get(raw_id))
        if card is not None:
            cards[str(card["card_id"])] = card
    source = {"crawler_chs_sha256": chs_sha256, "crawler_en_sha256": en_sha256, "source_url": chs_source, "generated_at": datetime.now(timezone.utc).isoformat()}
    if structured_sha256:
        source["structured_cards_sha256"] = structured_sha256
    catalog = {
        "schema_version": 1,
        "game_version": "",
        "source": source,
        "type_map": {"follower": 1, "amulet": 2, "countdown_amulet": 3, "spell": 4},
        "cards": cards,
    }
    return catalog, {
        "chs": len(chs_details),
        "en": len(en_details),
        "cards": len(cards),
        "missing_en": missing_en,
        "en_only": len(set(en_details) - set(chs_details)),
        "language_mismatch_ids": language_mismatch_ids,
    }


def build_catalog(payload: Any, *, source: str, source_sha256: str) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(payload, list):
        raise ValueError("cards.json must contain a top-level array")
    cards: dict[str, dict[str, Any]] = {}
    skipped = 0
    for raw in payload:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        card = clean_card(raw)
        if card is None:
            skipped += 1
            continue
        cards[str(card["card_id"])] = card
    catalog = {
        "schema_version": 1,
        "game_version": "",
        "source": {
            "cards_json_sha256": source_sha256,
            "source_url": source,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "type_map": {
            "follower": 1,
            "amulet": 2,
            "countdown_amulet": 3,
            "spell": 4,
        },
        "cards": cards,
    }
    return catalog, {"input": len(payload), "cards": len(cards), "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help=f"legacy local cards.json or URL (upstream: {DEFAULT_URL})")
    parser.add_argument("--chs", help="crawler shadowverse_cards_chs.json")
    parser.add_argument("--en", help="crawler shadowverse_cards_en.json")
    parser.add_argument("--structured", help="optional upstream cards.json for alt_modes/skill refs")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.chs or args.en:
        if not args.chs or not args.en:
            parser.error("--chs and --en must be supplied together")
        chs_bytes, chs_source = _read_source(args.chs)
        en_bytes, _ = _read_source(args.en)
        structured_bytes = structured_source = None
        structured_payload = None
        if args.structured:
            structured_bytes, structured_source = _read_source(args.structured)
            structured_payload = json.loads(structured_bytes.decode("utf-8"))
        catalog, stats = build_catalog_from_crawler(
            json.loads(chs_bytes.decode("utf-8")),
            json.loads(en_bytes.decode("utf-8")),
            structured_payload,
            chs_sha256=hashlib.sha256(chs_bytes).hexdigest(),
            en_sha256=hashlib.sha256(en_bytes).hexdigest(),
            structured_sha256=hashlib.sha256(structured_bytes).hexdigest() if structured_bytes else None,
            chs_source=chs_source,
        )
    else:
        input_source = args.input or "cards.json"
        raw_bytes, source = _read_source(input_source)
        catalog, stats = build_catalog(
            json.loads(raw_bytes.decode("utf-8")),
            source=source,
            source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    details = "; ".join(f"{key}={value}" for key, value in stats.items())
    print(f"wrote {args.output} ({details})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
