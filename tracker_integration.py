"""Tracker -> SnapshotAdapter -> LethalEngine integration boundary.

The Tracker owns memory reading and emits immutable public snapshots.  This
module keeps the solver side deliberately small: every refresh adapts the
latest snapshot, refuses to solve an incomplete/opponent-turn state, and
returns a UI-friendly status plus legal target/mode projections.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping

from lethal_engine import LethalEngine
from lethal_models import LethalResult, LethalState
from snapshot_adapter import SnapshotAdapter, SnapshotAdapterResult
from shadow_state_adapter import build_shadow_engine
from swb_rl_backend import SwbRlBackend


def _fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Return a stable digest for refresh de-duplication.

    Tracker serializes some collections as tuples and some as lists depending
    on the caller. ``default=list`` gives both representations the same
    canonical JSON while still failing closed for truly opaque values.
    """
    # A Tracker JSONL record wraps the public object under ``snapshot`` and
    # carries a new timestamp on every poll.  Fingerprint the inner game
    # state so replaying a recorded stream has the same de-duplication
    # semantics as the live callback.
    if isinstance(snapshot, Mapping) and "root" not in snapshot:
        nested = snapshot.get("snapshot")
        if isinstance(nested, Mapping):
            snapshot = nested

    def semantic(value: Any) -> Any:
        if isinstance(value, Mapping):
            # Tracker includes managed-memory addresses on the root, players,
            # and each card.  They are not game state and can be reallocated
            # between polls; excluding them keeps refresh de-duplication
            # aligned with TrackerService.without_addresses().
            return {str(key): semantic(item) for key, item in value.items() if str(key) != "address"}
        if isinstance(value, set):
            return sorted((semantic(item) for item in value), key=repr)
        if isinstance(value, (list, tuple)):
            return [semantic(item) for item in value]
        return value

    try:
        encoded = json.dumps(semantic(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=list)
    except (TypeError, ValueError):
        encoded = repr(snapshot)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrackerSolveView:
    """Immutable view model consumed by a desktop/overlay UI."""

    revision: int
    fingerprint: str
    changed: bool
    status: str
    probability: float
    sequence: tuple[str, ...]
    trusted: bool
    usable: bool
    is_ally_turn: bool
    max_damage: int = 0
    max_damage_sequence: tuple[str, ...] = ()
    trust_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    legal_actions: dict[str, Any] | None = None
    attack_targets: dict[int, tuple[Any, ...]] = None  # type: ignore[assignment]
    available_modes: dict[int, tuple[str, ...]] = None  # type: ignore[assignment]
    state: LethalState | None = None

    def __post_init__(self) -> None:
        # Frozen dataclasses cannot use a mutable dict default, but accepting
        # ``None`` in the public constructor keeps the type ergonomic.
        if self.attack_targets is None:
            object.__setattr__(self, "attack_targets", {})
        if self.available_modes is None:
            object.__setattr__(self, "available_modes", {})

    @property
    def status_label(self) -> str:
        """Status string suitable for the UI badge."""
        return self.status

    def targets_for(self, attacker_uid: int) -> tuple[Any, ...]:
        return tuple(self.attack_targets.get(int(attacker_uid), ()))

    def modes_for(self, unique_id: int) -> tuple[str, ...]:
        return tuple(self.available_modes.get(int(unique_id), ()))


class TrackerLethalSession:
    """Incrementally solve snapshots emitted by ShadowverseTracker.

    ``refresh`` is safe to call on every Tracker tick.  Identical snapshots
    return the prior view with ``changed=False`` and do not rerun the search.
    ``on_snapshot`` is an alias convenient for ``TrackerService`` callbacks.
    """

    def __init__(
        self,
        *,
        catalog: Mapping[str, Any] | None = None,
        rules: Mapping[str, Any] | None = None,
        card_db: Mapping[int, Any] | None = None,
        engine: LethalEngine | None = None,
        max_depth: int = 12,
    ) -> None:
        self.catalog = catalog
        self.rules = rules
        self.card_db = dict(card_db or {})
        self.engine = engine or LethalEngine(
            card_db=self.card_db,
            max_depth=max_depth,
            rules=rules,
            catalog=catalog,
        )
        self._fingerprint: str | None = None
        self._revision = 0
        self._view: TrackerSolveView | None = None
        self._selected_targets: dict[int, Any] = {}
        self._selected_card_targets: dict[int, Any] = {}

    @property
    def view(self) -> TrackerSolveView | None:
        return self._view

    @property
    def selected_targets(self) -> dict[int, Any]:
        return dict(self._selected_targets)

    def refresh(self, snapshot: Mapping[str, Any]) -> TrackerSolveView:
        digest = _fingerprint(snapshot)
        if self._view is not None and digest == self._fingerprint:
            return replace(self._view, changed=False)

        # A caller may provide a fully configured LethalEngine instead of
        # passing the same catalog/rules a second time.  Reuse its immutable
        # sources for adaptation so hand-card support detection and current
        # card metadata stay aligned with the engine that will solve.
        effective_catalog = self.catalog
        if effective_catalog is None:
            effective_catalog = getattr(self.engine, "catalog", None)
        effective_rules = self.rules
        if effective_rules is None:
            interpreter = getattr(self.engine, "interpreter", None)
            effective_rules = getattr(interpreter, "rules", None)
        adapted = SnapshotAdapter.adapt(snapshot, catalog=effective_catalog, rules=effective_rules)
        self._revision += 1
        result: LethalResult | None = None
        sequence: tuple[str, ...] = ()
        max_damage = 0
        max_damage_sequence: tuple[str, ...] = ()
        status = "INCOMPLETE"
        probability = 0.0
        warnings = list(adapted.warnings)

        if adapted.trusted and adapted.usable:
            try:
                result = self.engine.solve(adapted.state)
                status = result.status
                probability = float(result.probability)
                sequence = tuple(result.sequence)
                if status in ("NO_LETHAL", "INCOMPLETE"):
                    max_damage_fn = getattr(self.engine, "max_damage", None)
                    if callable(max_damage_fn):
                        try:
                            raw_max_damage, raw_max_sequence = max_damage_fn(adapted.state)
                            max_damage = max(0, int(raw_max_damage))
                            if isinstance(raw_max_sequence, (list, tuple)):
                                max_damage_sequence = tuple(str(item) for item in raw_max_sequence)
                        except Exception as exc:
                            warnings.append(f"max-damage analysis error: {type(exc).__name__}: {exc}")
                # Unknown executable rules mean that a NO_LETHAL answer is
                # not a proof. Keep a confirmed route confirmed when the
                # route itself was fully executable; downgrade only
                # uncertain/no-route output.
                if adapted.unsupported_card_ids and status in ("NO_LETHAL", "PROBABILISTIC"):
                    status = "INCOMPLETE"
                    warnings.append(
                        "unsupported hand card rules: "
                        + ", ".join(str(item) for item in adapted.unsupported_card_ids)
                    )
            except Exception as exc:
                # A malformed/generated rule should not terminate the
                # Tracker callback thread.  Preserve the adapted state for
                # diagnostics and show an explicit incomplete result.
                status = "INCOMPLETE"
                warnings.append(f"solver error: {type(exc).__name__}: {exc}")
                sequence = ("[incomplete: solver error]",)
        else:
            reasons = adapted.trust_reasons or ("snapshot is not usable",)
            sequence = tuple(f"[incomplete: {reason}]" for reason in reasons)
            if not adapted.is_ally_turn:
                status = "INCOMPLETE"

        view = TrackerSolveView(
            revision=self._revision,
            fingerprint=digest,
            changed=True,
            status=status,
            probability=probability,
            max_damage=max_damage,
            sequence=sequence,
            max_damage_sequence=max_damage_sequence,
            trusted=adapted.trusted,
            usable=adapted.usable,
            is_ally_turn=adapted.is_ally_turn,
            trust_reasons=adapted.trust_reasons,
            warnings=tuple(dict.fromkeys(warnings)),
            legal_actions=adapted.legal_actions,
            attack_targets=dict(adapted.state.legal_attack_targets),
            available_modes=dict(adapted.state.legal_modes),
            state=adapted.state,
        )
        self._fingerprint = digest
        self._view = view
        # Remove selections that no longer exist after a refresh. This avoids
        # carrying a stale target UID into a later game/turn.
        if not view.trusted or not view.usable:
            self._selected_targets.clear()
            self._selected_card_targets.clear()
        else:
            valid_uids = set(view.attack_targets)
            self._selected_targets = {
                uid: target for uid, target in self._selected_targets.items()
                if uid in valid_uids and target in view.targets_for(uid)
            }
            valid_hand_uids = {card.unique_id for card in view.state.hand} if view.state is not None else set()
            self._selected_card_targets = {
                uid: target for uid, target in self._selected_card_targets.items()
                if uid in valid_hand_uids
            }
        return view

    def on_snapshot(self, snapshot: Mapping[str, Any]) -> TrackerSolveView:
        return self.refresh(snapshot)

    def target_options(self, attacker_uid: int) -> tuple[Any, ...]:
        if self._view is None:
            return ()
        return self._view.targets_for(attacker_uid)

    def select_target(self, attacker_uid: int, target_uid: Any) -> bool:
        """Select a currently legal attack target for UI confirmation."""
        if self._view is None or not self._view.trusted or not self._view.usable:
            return False
        options = self.target_options(attacker_uid)
        if target_uid not in options:
            return False
        self._selected_targets[int(attacker_uid)] = target_uid
        return True

    def clear_selection(self, attacker_uid: int | None = None) -> None:
        if attacker_uid is None:
            self._selected_targets.clear()
        else:
            self._selected_targets.pop(int(attacker_uid), None)

    def card_target_options(self, unique_id: int, mode: str = "normal", trigger: str = "on_play") -> tuple[Any, ...]:
        """Enumerate deterministic target choices for a hand-card effect."""
        if self._view is None or self._view.state is None:
            return ()
        card = next((item for item in self._view.state.hand if item.unique_id == int(unique_id)), None)
        if card is None:
            return ()
        try:
            rules = getattr(self.engine.interpreter, "rules", {})
            if isinstance(self.rules, Mapping) and "rules" not in self.rules:
                rules = self.rules
            rule = rules.get(card.card_id, {}) if isinstance(rules, Mapping) else {}
            # Generated CardRules use ``on_fanfare`` for follower plays while
            # legacy rules use ``on_play``.  The UI should not require callers
            # to know which normalized trigger a particular compiler version
            # selected; try the equivalent pair when the requested trigger
            # has no target effect.
            triggers = [str(trigger)]
            if str(trigger) == "on_play":
                triggers.append("on_fanfare")
            elif str(trigger) == "on_fanfare":
                triggers.append("on_play")
            for candidate_trigger in dict.fromkeys(triggers):
                options = tuple(self.engine._rule_target_options(self._view.state, rule, mode, candidate_trigger))
                if options and options != (None,):
                    return options
            return ()
        except (AttributeError, TypeError, ValueError):
            return ()

    def select_card_target(self, unique_id: int, target: Any, mode: str = "normal", trigger: str = "on_play") -> bool:
        if self._view is None or not self._view.trusted or not self._view.usable:
            return False
        options = self.card_target_options(unique_id, mode=mode, trigger=trigger)
        if target not in options:
            return False
        self._selected_card_targets[int(unique_id)] = target
        return True

    @property
    def selected_card_targets(self) -> dict[int, Any]:
        return dict(self._selected_card_targets)

    def legal_action_ids(self, action: str) -> tuple[int, ...]:
        """Read a normalized action list from the most recent snapshot."""
        if self._view is None or not isinstance(self._view.legal_actions, Mapping):
            return ()
        value = self._view.legal_actions.get(action, ())
        if not isinstance(value, (list, tuple, set)):
            return ()
        return tuple(int(item) for item in value if isinstance(item, int) and not isinstance(item, bool))


class TrackerShadowLethalSession(TrackerLethalSession):
    """Tracker session backed by SWB-RL's native RuleBook and engine.

    This is an opt-in companion to :class:`TrackerLethalSession`.  It keeps
    the same view/selection contract, but builds a fresh
    ``TrackerShadowEngine`` for each changed snapshot and searches that engine
    through ``SwbRlBackend``.  Public Tracker data is not sufficient for a
    complete match replay (deck order and hidden opponent zones are absent),
    so every such result is downgraded to ``INCOMPLETE`` with the precise
    hydration warnings attached.
    """

    def __init__(
        self,
        *,
        catalog: Mapping[str, Any] | None = None,
        rules: Mapping[str, Any] | None = None,
        max_depth: int = 12,
        node_limit: int = 10_000,
        swb_rl_root: str | None = None,
        seed: int = 0,
    ) -> None:
        # Call the base constructor for the public selection/view helpers.  A
        # CardRules engine is not used by ``refresh``; keeping it here avoids
        # a second subtly different UI API for target selection.
        super().__init__(catalog=catalog, rules=rules, max_depth=max_depth)
        self.node_limit = max(1, int(node_limit))
        self.swb_rl_root = swb_rl_root
        self.seed = int(seed)
        self.backend = SwbRlBackend(max_depth=max_depth, node_limit=self.node_limit)

    def refresh(self, snapshot: Mapping[str, Any]) -> TrackerSolveView:
        digest = _fingerprint(snapshot)
        if self._view is not None and digest == self._fingerprint:
            return replace(self._view, changed=False)

        effective_catalog = self.catalog
        effective_rules = self.rules
        # The adapter needs the normalized catalog/rules for evolution aliases
        # and public names, even though SWB-RL supplies the executable rules.
        adapted = SnapshotAdapter.adapt(
            snapshot,
            catalog=effective_catalog,
            rules=effective_rules,
        )
        self._revision += 1
        status = "INCOMPLETE"
        probability = 0.0
        sequence: tuple[str, ...] = ()
        max_damage = 0
        max_damage_sequence: tuple[str, ...] = ()
        warnings = list(adapted.warnings)
        if adapted.trusted and adapted.usable:
            try:
                built = build_shadow_engine(
                    snapshot,
                    adapted,
                    swb_rl_root=self.swb_rl_root,
                    seed=self.seed,
                )
                warnings.extend(built.warnings)
                if built.engine is None:
                    sequence = tuple(
                        f"[incomplete: {warning}]"
                        for warning in (built.warnings or ("shadow engine unavailable",))
                    )
                else:
                    result = self.backend.solve(built.engine, player_index=0)
                    status = result.status
                    probability = float(result.probability)
                    sequence = tuple(result.sequence)
                    max_damage = max(0, int(result.max_damage))
                    max_damage_sequence = tuple(result.max_damage_sequence)
                    warnings.extend(result.warnings)
                    # A shadow engine is deliberately not allowed to claim a
                    # complete/confirmed route while hidden state or deck
                    # order was synthesized.  This guard is independent of
                    # the backend's random-seed warning.
                    if (
                        built.hidden_state_unknown
                        or built.card_order_unknown
                        or built.warnings
                    ) and status in {"CONFIRMED", "NO_LETHAL", "PROBABILISTIC"}:
                        status = "INCOMPLETE"
                        warnings.append(
                            "shadow state is partial; native result is advisory only"
                        )
            except Exception as exc:
                status = "INCOMPLETE"
                warnings.append(f"shadow solver error: {type(exc).__name__}: {exc}")
                sequence = ("[incomplete: shadow solver error]",)
        else:
            reasons = adapted.trust_reasons or ("snapshot is not usable",)
            sequence = tuple(f"[incomplete: {reason}]" for reason in reasons)

        view = TrackerSolveView(
            revision=self._revision,
            fingerprint=digest,
            changed=True,
            status=status,
            probability=probability,
            sequence=sequence,
            trusted=adapted.trusted,
            usable=adapted.usable,
            is_ally_turn=adapted.is_ally_turn,
            max_damage=max_damage,
            max_damage_sequence=max_damage_sequence,
            trust_reasons=adapted.trust_reasons,
            warnings=tuple(dict.fromkeys(str(item) for item in warnings if str(item))),
            legal_actions=adapted.legal_actions,
            attack_targets=dict(adapted.state.legal_attack_targets),
            available_modes=dict(adapted.state.legal_modes),
            state=adapted.state,
        )
        self._fingerprint = digest
        self._view = view
        # Keep the same stale-target hygiene as the CardRules session.
        if not view.trusted or not view.usable:
            self._selected_targets.clear()
            self._selected_card_targets.clear()
        else:
            valid_uids = set(view.attack_targets)
            self._selected_targets = {
                uid: target
                for uid, target in self._selected_targets.items()
                if uid in valid_uids and target in view.targets_for(uid)
            }
            valid_hand_uids = (
                {card.unique_id for card in view.state.hand}
                if view.state is not None
                else set()
            )
            self._selected_card_targets = {
                uid: target
                for uid, target in self._selected_card_targets.items()
                if uid in valid_hand_uids
            }
        return view


# A descriptive alias for integrations that prefer controller terminology.
TrackerSnapshotController = TrackerLethalSession


__all__ = [
    "TrackerLethalSession",
    "TrackerShadowLethalSession",
    "TrackerSnapshotController",
    "TrackerSolveView",
]
