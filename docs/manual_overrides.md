# Manual CardRules overrides

Use `card_rules_overrides.json` only when the bilingual parser cannot safely
represent a complex card.

1. Find the card in `data/generated/card_rules_generated.json`.
2. Copy its `source_hash` into the override. This binds the review to the exact
   normalized AST and makes an override fail loudly after source/parser changes.
3. Set `support` to `verified` and use only operations marked `implemented` in
   `schemas/card_rules_v2_support.json`.
4. Run `python merge_card_rules.py` and the full unit test suite.

The merge rejects missing cards, stale hashes, non-verified overrides, schema
errors, and verified rules that depend on planned operations. Unsupported parts
must remain `partial`; do not omit them merely to obtain `verified` status.
