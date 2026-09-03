"""Select reviewed SWB-RL rules without replacing the live ruleset.

``adapt_swb_rules.py`` is deliberately broad: it translates every card in the
SWB-RL catalog and records every lossy conversion.  This module is the next,
more conservative step.  It turns a small, explicit selection of candidate
rules into either an overlay (only selected cards) or an isolated merged
ruleset (the current rules plus the selected replacements).

The command never writes ``data/generated/card_rules_v2.json`` by default.
Replacing an existing ``verified`` rule or accepting a ``partial`` rule needs
an explicit opt-in, so a large adapter run cannot silently regress a reviewed
rule.  ``generated`` means that the adapter found no known translation gap;
the result is still not gameplay-verified and is not relabelled ``verified``.

Examples::

    # A review bundle for three representative cards.
    python promote_swb_rules.py --cards 10753310,10413110,10413310

    # Isolated full ruleset, keeping every other current rule unchanged.
    python promote_swb_rules.py --cards 10753310,10413110,10413310 \
        --mode merged --output-dir data/imported

    # Inspect all adapter-clean cards (still isolated; no live file is touched).
    python promote_swb_rules.py --mode overlay --output-dir data/imported
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from rule_support import validate_contract


ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATE = ROOT / "data" / "imported" / "swb_card_rules_v2_candidate.json"
DEFAULT_BASE = ROOT / "data" / "generated" / "card_rules_v2.json"
DEFAULT_CATALOG = ROOT / "data" / "generated" / "card_catalog.json"
DEFAULT_SCHEMA = ROOT / "schemas" / "card_rules_v2.schema.json"
DEFAULT_MATRIX = ROOT / "schemas" / "card_rules_v2_support.json"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "imported"


class MigrationSelectionError(ValueError):
    """Raised when a requested migration violates the selection contract."""


def _read_json(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {source}")
    return payload


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    """Yield every mapping in a rule, including nested effects."""

    if isinstance(value, Mapping):
        # Copying is unnecessary here; this helper is read-only and is used
        # only for diagnostics and stable operation summaries.
        yield dict(value)
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _rule_operations(rule: Mapping[str, Any]) -> list[str]:
    return sorted({str(item["op"]) for item in _walk(rule) if isinstance(item.get("op"), str)})


def _candidate_rules(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = candidate.get("rules")
    if not isinstance(rules, Mapping):
        raise ValueError("candidate.rules must be an object")
    return rules


def _parse_card_ids(value: str | Sequence[int | str] | None) -> list[str] | None:
    """Parse a comma/space separated allowlist into sorted numeric keys."""

    if value is None:
        return None
    values: list[Any]
    if isinstance(value, str):
        values = [part for part in value.replace(",", " ").split() if part]
    else:
        values = list(value)
    result: set[str] = set()
    invalid: list[Any] = []
    for item in values:
        ident = _as_int(item)
        if ident is None or ident < 0:
            invalid.append(item)
        else:
            result.add(str(ident))
    if invalid:
        raise MigrationSelectionError(f"invalid card id(s): {', '.join(map(str, invalid))}")
    return sorted(result, key=int)


def _source_hash(rule: Mapping[str, Any]) -> str | None:
    value = rule.get("source_hash")
    return str(value) if isinstance(value, str) else None


def _validate_document(
    document: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    schema: Mapping[str, Any],
    matrix: Mapping[str, Any],
    label: str,
) -> None:
    errors = list(Draft202012Validator(schema).iter_errors(document))
    errors.extend(validate_contract(document, catalog, schema=schema, matrix=matrix))
    if errors:
        formatted = "\n".join(str(error) for error in errors[:20])
        raise ValueError(f"{label} failed CardRules v2 validation:\n{formatted}")


def _copy_document_header(source: Mapping[str, Any], *, revision: int) -> dict[str, Any]:
    """Copy only schema-approved top-level fields from a rules document."""

    output: dict[str, Any] = {
        "schema_version": 2,
        "ruleset_revision": revision,
        "catalog_version": int(source.get("catalog_version", 1) or 1),
        "game_version": str(source.get("game_version", "") or ""),
    }
    resources = source.get("resources")
    if isinstance(resources, Mapping):
        output["resources"] = copy.deepcopy(resources)
    return output


def build_migration_bundle(
    candidate: Mapping[str, Any],
    *,
    base: Mapping[str, Any] | None = None,
    catalog: Mapping[str, Any],
    schema: Mapping[str, Any],
    matrix: Mapping[str, Any],
    card_ids: Sequence[int | str] | str | None = None,
    allowed_support: Sequence[str] = ("generated",),
    allow_partial: bool = False,
    allow_unsupported: bool = False,
    allow_verified_replace: bool = False,
    mode: str = "overlay",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic overlay/merged rules document and audit report.

    ``mode=overlay`` emits only selected candidate rules.  ``mode=merged``
    starts from ``base`` and replaces only the selected keys.  Both outputs
    are valid v2 documents and are validated against the supplied catalog.
    """

    if mode not in {"overlay", "merged"}:
        raise ValueError("mode must be 'overlay' or 'merged'")
    if mode == "merged" and base is None:
        raise ValueError("base is required for mode='merged'")

    candidate_rules = _candidate_rules(candidate)
    base_rules = _candidate_rules(base) if base is not None else {}
    catalog_cards = catalog.get("cards", {}) if isinstance(catalog, Mapping) else {}
    catalog_ids = {str(key) for key in catalog_cards} if isinstance(catalog_cards, Mapping) else set()
    requested = _parse_card_ids(card_ids)
    allowed = {str(item) for item in allowed_support}
    if not allowed:
        raise ValueError("allowed_support must not be empty")
    # An explicit allowlist is safest.  When omitted, only adapter-clean
    # generated cards are selected; partial/unsupported rules can never enter
    # an accidental bulk migration.
    if requested is None:
        requested = sorted(
            (
                str(key)
                for key, rule in candidate_rules.items()
                if isinstance(rule, Mapping)
                and str(rule.get("support")) in allowed
                and str(rule.get("support")) == "generated"
                and not rule.get("unparsed_clauses")
            ),
            key=int,
        )

    selected: dict[str, Any] = {}
    cards_report: dict[str, Any] = {}
    rejected: list[dict[str, Any]] = []
    for key in requested:
        candidate_rule = candidate_rules.get(key)
        if not isinstance(candidate_rule, Mapping):
            rejected.append({"card_id": int(key), "reason": "candidate_missing"})
            continue
        support = str(candidate_rule.get("support", "unsupported"))
        gaps = list(candidate_rule.get("unparsed_clauses", ()) or ())
        base_rule = base_rules.get(key)
        base_support = str(base_rule.get("support")) if isinstance(base_rule, Mapping) else None
        reasons: list[str] = []
        if key not in catalog_ids:
            reasons.append("card is absent from the target Catalog")
        if support not in allowed:
            reasons.append(f"support={support} is not allowed")
        if support == "partial" and not allow_partial:
            reasons.append("partial requires allow_partial=True")
        if support == "unsupported" and not allow_unsupported:
            reasons.append("unsupported requires allow_unsupported=True")
        if gaps and support == "generated":
            reasons.append("generated candidate contains unparsed_clauses")
        if base_support == "verified" and not allow_verified_replace:
            reasons.append("replacing a verified base rule requires allow_verified_replace=True")
        if reasons:
            rejected.append({"card_id": int(key), "reason": "; ".join(reasons), "support": support, "base_support": base_support})
            cards_report[key] = {
                "selected": False,
                "reason": "; ".join(reasons),
                "candidate_support": support,
                "base_support": base_support,
                "candidate_source_hash": _source_hash(candidate_rule),
                "base_source_hash": _source_hash(base_rule) if isinstance(base_rule, Mapping) else None,
                "operations": _rule_operations(candidate_rule),
            }
            continue
        selected[key] = copy.deepcopy(candidate_rule)
        cards_report[key] = {
            "selected": True,
            "candidate_support": support,
            "base_support": base_support,
            "candidate_source_hash": _source_hash(candidate_rule),
            "base_source_hash": _source_hash(base_rule) if isinstance(base_rule, Mapping) else None,
            "changed": base_rule != candidate_rule,
            "operations": _rule_operations(candidate_rule),
            "gap_count": len(gaps),
        }

    if rejected:
        details = "; ".join(f"{item['card_id']}: {item['reason']}" for item in rejected)
        raise MigrationSelectionError(f"migration selection rejected: {details}")

    if mode == "overlay":
        revision = int(candidate.get("ruleset_revision", 1) or 1)
        document = _copy_document_header(candidate, revision=revision)
        document["game_version"] = "SWB-RL selected candidate"
        document["rules"] = selected
    else:
        assert base is not None
        revision = max(int(base.get("ruleset_revision", 1) or 1), int(candidate.get("ruleset_revision", 1) or 1))
        document = _copy_document_header(base, revision=revision)
        document["game_version"] = str(base.get("game_version", "") or "")
        document["rules"] = copy.deepcopy(dict(base_rules))
        document["rules"].update(selected)

    _validate_document(document, catalog=catalog, schema=schema, matrix=matrix, label=f"{mode} migration")
    selected_support = Counter(str(rule.get("support")) for rule in selected.values())
    report = {
        "schema_version": 1,
        "source": {
            "candidate_sha256": _sha256(candidate),
            "base_sha256": _sha256(base) if base is not None else None,
            "catalog_sha256": _sha256(catalog),
            "mode": mode,
        },
        "selection": {
            "requested_card_ids": requested,
            "allowed_support": sorted(allowed),
            "allow_partial": bool(allow_partial),
            "allow_unsupported": bool(allow_unsupported),
            "allow_verified_replace": bool(allow_verified_replace),
        },
        "summary": {
            "candidate_rule_count": len(candidate_rules),
            "base_rule_count": len(base_rules),
            "selected_rule_count": len(selected),
            "changed_rule_count": sum(1 for item in cards_report.values() if item.get("selected") and item.get("changed")),
            "selected_support": dict(sorted(selected_support.items())),
            "output_rule_count": len(document["rules"]),
            "output_sha256": _sha256(document),
        },
        "cards": cards_report,
    }
    return document, report


def write_migration_artifacts(
    document: Mapping[str, Any],
    report: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    *,
    mode: str = "overlay",
) -> dict[str, Path]:
    """Write isolated migration artifacts and return their paths."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    stem = "swb_rules_selected_overlay" if mode == "overlay" else "swb_rules_selected_merged"
    document_path = target / f"{stem}.json"
    report_path = target / f"swb_rule_migration_report_{mode}.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"document": document_path, "report": report_path}


def report_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report, Mapping) else {}
    selection = report.get("selection", {}) if isinstance(report, Mapping) else {}
    lines = [
        "# SWB-RL selected migration report",
        "",
        "This artifact is isolated. It does not replace `data/generated/card_rules_v2.json`.",
        "",
        f"- Mode: `{report.get('source', {}).get('mode', 'unknown')}`",
        f"- Candidate rules: {summary.get('candidate_rule_count', 0)}",
        f"- Selected rules: {summary.get('selected_rule_count', 0)}",
        f"- Output rules: {summary.get('output_rule_count', 0)}",
        f"- Changed rules: {summary.get('changed_rule_count', 0)}",
        f"- Requested IDs: {', '.join(map(str, selection.get('requested_card_ids', [])))}",
        "",
        "## Selected support",
        "",
        "| status | cards |",
        "|---|---:|",
    ]
    for status, count in sorted((summary.get("selected_support") or {}).items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## Cards", "", "| card_id | selected | candidate | base | changed | operations |", "|---:|:---:|---|---|:---:|---|"])
    cards = report.get("cards", {}) if isinstance(report, Mapping) else {}
    for card_id, item in sorted(cards.items(), key=lambda pair: int(pair[0])):
        if not isinstance(item, Mapping):
            continue
        operations = ", ".join(f"`{op}`" for op in item.get("operations", []))
        lines.append(
            f"| {card_id} | {'yes' if item.get('selected') else 'no'} | {item.get('candidate_support', '')} | "
            f"{item.get('base_support') or '—'} | {'yes' if item.get('changed') else 'no'} | {operations or '—'} |"
        )
        if not item.get("selected") and item.get("reason"):
            lines.append(f"|  |  |  |  |  | rejected: {item['reason']} |")
    lines.append("")
    return "\n".join(lines)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE, help="isolated adapter candidate JSON")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE, help="current v2 rules, used by merged mode")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--support", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--cards", help="comma/space separated explicit card IDs; omitted means adapter-clean generated cards")
    parser.add_argument("--mode", choices=("overlay", "merged"), default="overlay")
    parser.add_argument("--allow-partial", action="store_true", help="allow explicitly selected partial rules")
    parser.add_argument("--allow-unsupported", action="store_true", help="allow explicitly selected unsupported rules")
    parser.add_argument("--allow-verified-replace", action="store_true", help="allow replacing an existing verified rule")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    candidate = _read_json(args.candidate)
    base = _read_json(args.base) if args.mode == "merged" else None
    catalog = _read_json(args.catalog)
    schema = _read_json(args.schema)
    matrix = _read_json(args.support)
    try:
        document, report = build_migration_bundle(
            candidate,
            base=base,
            catalog=catalog,
            schema=schema,
            matrix=matrix,
            card_ids=args.cards,
            allow_partial=args.allow_partial,
            allow_unsupported=args.allow_unsupported,
            allow_verified_replace=args.allow_verified_replace,
            mode=args.mode,
        )
    except (MigrationSelectionError, ValueError) as error:
        raise SystemExit(str(error)) from error
    paths = write_migration_artifacts(document, report, args.output_dir, mode=args.mode)
    markdown_path = Path(args.output_dir) / f"swb_rule_migration_report_{args.mode}.md"
    markdown_path.write_text(report_markdown(report), encoding="utf-8")
    print(f"wrote {paths['document']} ({len(document['rules'])} rules)")
    print(f"wrote {paths['report']}")
    print(f"wrote {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "MigrationSelectionError",
    "build_migration_bundle",
    "write_migration_artifacts",
    "report_markdown",
]
