"""Deterministic per-card coverage reports for the CardRules build.

The report is deliberately derived from the exact catalog/rules objects that
are written by the pipeline.  It contains no timestamps or absolute paths, so
it is suitable for byte-for-byte reproducibility checks and for reviewing
whether a manual override changed a card's support status.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SUPPORT_STATUSES = ("verified", "generated", "partial", "unsupported")


def _sort_ids(values: Any) -> list[str]:
    return sorted((str(value) for value in values), key=lambda value: (0, int(value)) if value.isdigit() else (1, value))


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def build_coverage_report(
    catalog: Mapping[str, Any],
    rules_instance: Mapping[str, Any],
    *,
    source_catalog: str | None = None,
    source_rules: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """Return a stable coverage report for one catalog/rules pair."""
    catalog_cards = catalog.get("cards", {}) if isinstance(catalog, Mapping) else {}
    rules = rules_instance.get("rules", {}) if isinstance(rules_instance, Mapping) else {}
    catalog_cards = catalog_cards if isinstance(catalog_cards, Mapping) else {}
    rules = rules if isinstance(rules, Mapping) else {}
    catalog_ids = _sort_ids(catalog_cards)
    rule_ids = _sort_ids(rules)
    support_by_card: dict[str, str] = {}
    source_hash_by_card: dict[str, str] = {}
    unparsed_clause_count_by_card: dict[str, int] = {}
    for card_id in rule_ids:
        rule = rules.get(card_id, {})
        if not isinstance(rule, Mapping):
            support_by_card[card_id] = "invalid"
            source_hash_by_card[card_id] = ""
            unparsed_clause_count_by_card[card_id] = 0
            continue
        support_by_card[card_id] = str(rule.get("support", "invalid"))
        source_hash_by_card[card_id] = str(rule.get("source_hash", ""))
        clauses = rule.get("unparsed_clauses", [])
        unparsed_clause_count_by_card[card_id] = len(clauses) if isinstance(clauses, list) else 0
    support = {status: sum(value == status for value in support_by_card.values()) for status in _SUPPORT_STATUSES}
    cards_by_support = {
        status: [card_id for card_id in rule_ids if support_by_card.get(card_id) == status]
        for status in _SUPPORT_STATUSES
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "ruleset_revision": _as_int(rules_instance.get("ruleset_revision"), 2),
        "catalog_version": _as_int(rules_instance.get("catalog_version"), _as_int(catalog.get("schema_version"), 1)),
        "cards": len(rule_ids),
        "catalog_cards": len(catalog_ids),
        "support": support,
        "support_by_card": support_by_card,
        "cards_by_support": cards_by_support,
        "source_hash_by_card": source_hash_by_card,
        "unparsed_clause_count_by_card": unparsed_clause_count_by_card,
        "missing_rule_ids": [card_id for card_id in catalog_ids if card_id not in set(rule_ids)],
        "extra_rule_ids": [card_id for card_id in rule_ids if card_id not in set(catalog_ids)],
    }
    # Logical filenames are useful provenance while remaining independent of
    # the checkout location or a temporary directory used by CI.
    if source_catalog:
        report["source_catalog"] = str(source_catalog)
    if source_rules:
        report["source_rules"] = str(source_rules)
    if phase:
        report["phase"] = str(phase)
    return report


def validate_coverage_report(
    report: Mapping[str, Any],
    catalog: Mapping[str, Any],
    rules_instance: Mapping[str, Any],
) -> list[str]:
    """Compare a report with a freshly derived report.

    Source/phase metadata is intentionally ignored; all coverage fields must
    match exactly, including the per-card source hashes and unparsed counts.
    """
    expected = build_coverage_report(catalog, rules_instance)
    errors: list[str] = []
    for key in expected:
        if key in {"source_catalog", "source_rules", "phase"}:
            continue
        if report.get(key) != expected[key]:
            errors.append(f"coverage.{key} does not match rules/catalog")
    return errors


__all__ = ["build_coverage_report", "validate_coverage_report"]
