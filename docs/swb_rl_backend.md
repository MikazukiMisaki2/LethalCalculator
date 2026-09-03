# Native SWB-RL lethal backend

`swb_rl_backend.py` is an optional search backend for experiments where SWB-RL
is the source of truth for card effects.  It does not translate the SWB-RL
`RuleBook` into CardRules v2 and it does not modify the SWB-RL checkout.

The backend accepts either:

* a SWB-RL `GameEngine`; or
* a SWB-RL `MatchSimulator` (it resolves `simulator.env._core` once at the
  integration boundary).

For every node it calls `legal_commands()`, skips `EndTurn`, clones the engine,
and applies one command.  This means play modes, exact target IDs, choices,
resource checks, Ward restrictions, evolution and all effects implemented by
SWB-RL are evaluated by the same code used by its simulator.  The source
engine is never mutated.

```python
from swb_rl_backend import SwbRlBackend

backend = SwbRlBackend(max_depth=16, node_limit=50_000)
result = backend.solve(simulator, player_index=simulator.human_player)
print(result.status, result.max_damage, result.sequence)
```

`SwbRlSearchResult.to_lethal_result()` projects to the existing calculator
result shape when a caller needs `LethalResult`.

## Safety and current scope

The search is one-turn and bounded.  It refuses to search mulligan, setup or
other non-main phases and reports `INCOMPLETE` when the node/depth limit is
reached, a command fails to apply, or the enemy health is unavailable.

The first backend is a deterministic-seed probe.  If a selected transition
emits an SWB-RL random event, the route is reported as `INCOMPLETE` with
`random_observed=true`; it is never presented as a 100% confirmed lethal.  A
future exact-probability mode should be implemented in SWB-RL as a branch/RNG
snapshot API rather than by repeatedly changing the seed.

`TrackerShadowLethalSession` is the opt-in bridge for the other direction.  It
hydrates the visible part of a Tracker snapshot into a fresh SWB-RL
`GameState`, overlays Tracker's root legal-action/target projection, and then
uses the same native backend.  Set `SHADOWVERSE_LETHAL_BACKEND=swb_rl_shadow`
and `SHADOWVERSE_SWB_RL_ROOT` when starting Tracker.  The bridge is
conservative: hidden opponent zones, exact deck order, pending effects and
unmapped public resources remain explicit warnings, so the session always
downgrades a native `CONFIRMED`/`NO_LETHAL` result to `INCOMPLETE` while those
gaps exist.  The default `card_rules_v2` backend is unchanged.

## Why this is the migration direction

The two projects have different responsibilities:

| Layer | Source of truth |
| --- | --- |
| Card definitions, typed effects, target legality, resolution | SWB-RL RuleBook/GameEngine |
| Tracker memory reading and live UI | ShadowverseTracker |
| Public-snapshot fallback and legacy fixtures | LethalCalculator CardRules v2 |

Keeping this boundary avoids maintaining a second, partially compatible copy
of the 800+ card rules.  Cards not implemented by SWB-RL still need an explicit
runtime coverage result; the backend does not silently fall back to a guessed
v2 effect.
