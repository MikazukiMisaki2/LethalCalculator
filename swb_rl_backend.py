"""Native SWB-RL search backend for the lethal calculator.

The normal calculator operates on ``LethalState`` and CardRules v2.  This
module is an intentionally separate integration boundary: it searches a live
SWB-RL ``GameEngine`` (or a ``MatchSimulator`` exposing ``env._core``) by
asking the engine for *its* legal commands and applying commands to cloned
engines.  Consequently the RuleBook, target validation, resource checks and
effect resolution all remain owned by SWB-RL.

Only a small duck-typed interface is required.  Keeping the module free of a
hard ``swb`` import makes it usable when SWB-RL is not installed and keeps the
existing Tracker/CardRules path unchanged.

The first version is a bounded, deterministic-seed probe.  It does not claim
an exact probability for random effects: if a selected path emits a random
event, the result is marked ``INCOMPLETE`` and includes a warning.  Exact
probabilities need a future engine-level branch API (one branch per RNG
outcome), not repeated calls with a single seed.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from lethal_models import LethalResult


_END_TURN_NAMES = {"endturn", "end_turn"}


@dataclass(frozen=True)
class SwbRlSearchResult:
    """Result of a bounded search over one SWB-RL turn.

    ``max_damage`` is the highest leader damage observed in the explored
    deterministic seed.  ``lethal_found`` deliberately remains separate from
    ``status``: a seeded random route may reach zero HP, but cannot be called
    confirmed without enumerating its other random outcomes.
    """

    status: str
    probability: float
    sequence: tuple[str, ...] = ()
    max_damage: int = 0
    max_damage_sequence: tuple[str, ...] = ()
    lethal_found: bool = False
    random_observed: bool = False
    nodes: int = 0
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    def to_lethal_result(self) -> LethalResult:
        """Project to the existing calculator result shape.

        The legacy result has no field for a potential seeded lethal.  We keep
        the conservative ``INCOMPLETE`` status and expose the route/warnings in
        its sequence rather than falsely returning a confirmed probability.
        """

        sequence = list(self.sequence)
        for warning in self.warnings:
            marker = f"[incomplete: {warning}]"
            if marker not in sequence:
                sequence.append(marker)
        return LethalResult(self.status, float(self.probability), sequence)


@dataclass(frozen=True)
class _Outcome:
    future_damage: int = 0
    path: tuple[str, ...] = ()
    lethal: bool = False
    random_observed: bool = False


class SwbRlBackend:
    """Search an SWB-RL ``GameEngine`` without translating cards to v2 rules.

    Parameters are deliberately conservative for a live overlay.  ``node``
    and ``depth`` limits are reported as incomplete instead of being hidden.
    ``player_index`` is the player whose turn should be searched; if omitted,
    the engine's active player is used.
    """

    def __init__(self, *, max_depth: int = 16, node_limit: int = 50_000):
        self.max_depth = max(1, int(max_depth))
        self.node_limit = max(1, int(node_limit))

    @staticmethod
    def core_from(source: Any) -> Any:
        """Return a GameEngine from an engine, environment or MatchSimulator.

        The simulator intentionally keeps the core private.  This adapter is
        the one place where that documented integration seam is accepted; no
        private fields are read during search after this resolution step.
        """

        if source is None:
            raise TypeError("an SWB-RL GameEngine or MatchSimulator is required")
        if callable(getattr(source, "legal_commands", None)) and callable(
            getattr(source, "clone", None)
        ):
            return source
        env = getattr(source, "env", None)
        core = getattr(env, "_core", None)
        if core is not None and callable(getattr(core, "legal_commands", None)):
            return core
        core = getattr(source, "_core", None)
        if core is not None and callable(getattr(core, "legal_commands", None)):
            return core
        raise TypeError(
            "source does not expose SWB-RL GameEngine legal_commands()/clone()"
        )

    def solve(self, source: Any, *, player_index: int | None = None) -> SwbRlSearchResult:
        """Search the current main phase without crossing ``EndTurn``.

        No command is applied to ``source`` itself.  Every successor is made
        with ``clone()`` so this method is safe to call from a Tracker refresh
        or a simulator UI endpoint.
        """

        core = self.core_from(source)
        if bool(getattr(core, "terminated", False)):
            return SwbRlSearchResult(
                status="INCOMPLETE",
                probability=0.0,
                warnings=("match already terminated",),
            )

        active = self._active_player(core)
        root_player = active if player_index is None else int(player_index)
        if active is not None and active != root_player:
            return SwbRlSearchResult(
                status="INCOMPLETE",
                probability=0.0,
                warnings=(
                    f"not active player (requested {root_player}, active {active})",
                ),
            )
        enemy_player = self._enemy_player(core, root_player)
        root_enemy_hp = self._leader_hp(core, enemy_player)
        if root_enemy_hp is None:
            return SwbRlSearchResult(
                status="INCOMPLETE",
                probability=0.0,
                warnings=("enemy leader health unavailable",),
            )
        phase = self._phase_name(core)
        if phase is not None and phase not in {"main", "main_phase"}:
            return SwbRlSearchResult(
                status="INCOMPLETE",
                probability=0.0,
                warnings=(f"unsupported phase: {phase}",),
            )

        nodes = 0
        truncated = False
        errors: list[str] = []
        memo: dict[tuple[int, str], _Outcome] = {}
        root_fingerprint = self._state_digest(core)

        def search(current: Any, depth: int) -> _Outcome:
            nonlocal nodes, truncated
            current_hp = self._leader_hp(current, enemy_player)
            if current_hp is None:
                errors.append("enemy leader health unavailable after action")
                return _Outcome()
            if current_hp <= 0:
                return _Outcome(
                    future_damage=0,
                    lethal=True,
                    path=(),
                    random_observed=False,
                )

            try:
                commands = list(current.legal_commands())
            except Exception as exc:  # pragma: no cover - defensive boundary
                errors.append(f"legal_commands failed: {type(exc).__name__}: {exc}")
                return _Outcome()
            commands = [command for command in commands if not self._is_end_turn(command)]
            if not commands:
                return _Outcome()

            if depth >= self.max_depth:
                truncated = True
                return _Outcome()

            key = (depth, self._state_digest(current))
            cached = memo.get(key)
            if cached is not None:
                return cached

            nodes += 1
            if nodes > self.node_limit:
                truncated = True
                return _Outcome()

            best = _Outcome()
            for command in commands:
                if nodes > self.node_limit:
                    truncated = True
                    break
                label = self._describe_command(current, command, root_player)
                try:
                    branch = current.clone()
                    transition = branch.apply(command)
                except Exception as exc:
                    # ``legal_commands`` is the authority.  A command failing
                    # to apply signals an engine/adapter mismatch and must be
                    # visible rather than silently converted into a legal line.
                    errors.append(
                        f"apply failed for {type(command).__name__}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                child_hp = self._leader_hp(branch, enemy_player)
                if child_hp is None:
                    errors.append("enemy leader health unavailable after action")
                    continue
                immediate = max(0, int(current_hp) - int(child_hp))
                random_here = self._transition_has_random(transition)
                if child_hp <= 0:
                    candidate = _Outcome(
                        future_damage=immediate,
                        path=(label,),
                        lethal=True,
                        random_observed=random_here,
                    )
                else:
                    suffix = search(branch, depth + 1)
                    candidate = _Outcome(
                        future_damage=immediate + suffix.future_damage,
                        path=(label,) + suffix.path,
                        lethal=suffix.lethal,
                        random_observed=random_here or suffix.random_observed,
                    )
                if self._better(candidate, best):
                    best = candidate

            memo[key] = best
            return best

        outcome = search(core, 0)
        # Run a separate memoized projection for the maximum damage route only
        # when the lethal route is not already the maximum explored damage.
        # In this search every candidate is ranked by damage, so ``outcome`` is
        # already the maximum route.  Keep a stable root digest in the local
        # scope to make accidental mutation of the source easy to detect.
        if self._state_digest(core) != root_fingerprint:
            errors.append("source engine mutated during search")

        random_observed = outcome.random_observed
        warning_set = list(dict.fromkeys(errors))
        if truncated:
            warning_set.append("search limit reached")
        if random_observed:
            warning_set.append(
                "random outcome observed under a single seed; exact probability not enumerated"
            )

        if outcome.lethal:
            status = "INCOMPLETE" if warning_set else "CONFIRMED"
            probability = 0.0 if random_observed else 1.0
        else:
            status = "INCOMPLETE" if warning_set else "NO_LETHAL"
            probability = 0.0

        return SwbRlSearchResult(
            status=status,
            probability=probability,
            sequence=outcome.path,
            max_damage=max(0, int(outcome.future_damage)),
            max_damage_sequence=outcome.path,
            lethal_found=outcome.lethal,
            random_observed=random_observed,
            nodes=nodes,
            truncated=truncated,
            warnings=tuple(warning_set),
        )

    @staticmethod
    def _better(candidate: _Outcome, current: _Outcome) -> bool:
        """Stable ranking: damage, then lethal, then shorter route."""

        # A resource-only command is useful when it unlocks a later damaging
        # command (the recursive suffix then gives a positive score).  It is
        # not a useful maximum-damage route by itself, however; keep the empty
        # route when both candidates deal zero and neither is lethal.
        if (
            candidate.future_damage == 0
            and current.future_damage == 0
            and not candidate.lethal
            and not current.lethal
        ):
            return False
        if candidate.future_damage != current.future_damage:
            return candidate.future_damage > current.future_damage
        if candidate.lethal != current.lethal:
            return candidate.lethal
        if candidate.path and not current.path:
            return True
        if len(candidate.path) != len(current.path):
            return len(candidate.path) < len(current.path)
        return candidate.path < current.path

    @staticmethod
    def _active_player(core: Any) -> int | None:
        value = getattr(core, "current_player", None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        state = getattr(core, "state", None)
        value = getattr(state, "active_player", None)
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _enemy_player(core: Any, player_index: int) -> int:
        players = getattr(core, "players", None)
        if players is not None and len(players) > 1:
            return 1 - player_index
        return 1 - player_index

    @staticmethod
    def _phase_name(core: Any) -> str | None:
        state = getattr(core, "state", None)
        phase = getattr(state, "phase", None)
        if phase is None:
            return None
        value = getattr(phase, "value", phase)
        text = str(value).strip().casefold().replace(" ", "_")
        return text or None

    @staticmethod
    def _leader_hp(core: Any, player_index: int) -> int | None:
        players = getattr(core, "players", None)
        try:
            player = players[player_index]
        except (TypeError, IndexError, KeyError):
            return None
        for name in ("health", "life", "hp"):
            value = getattr(player, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        if isinstance(player, Mapping):
            for name in ("health", "life", "hp"):
                value = player.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
        return None

    @staticmethod
    def _state_digest(core: Any) -> str:
        """Hash future-determining state while excluding diagnostic history."""

        value: Any
        fingerprint = getattr(core, "deterministic_fingerprint", None)
        if callable(fingerprint):
            try:
                value = fingerprint().get("state")
            except Exception:  # pragma: no cover - compatibility fallback
                value = None
        else:
            value = None
        if value is None:
            snapshot = getattr(core, "snapshot", None)
            if callable(snapshot):
                try:
                    captured = snapshot()
                    value = getattr(captured, "payload", captured)
                except Exception:  # pragma: no cover - compatibility fallback
                    value = None
        if value is None:
            value = repr(core)
        try:
            payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except (TypeError, ValueError, pickle.PickleError):
            payload = repr(value).encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _command_type(command: Any) -> str:
        value = getattr(command, "type", None)
        if value is not None:
            value = getattr(value, "value", value)
            text = str(value).strip().casefold()
            if text:
                return text
        return type(command).__name__.replace("-", "_").casefold()

    @classmethod
    def _is_end_turn(cls, command: Any) -> bool:
        kind = cls._command_type(command).replace(" ", "_")
        return kind in _END_TURN_NAMES or kind.endswith(".end_turn")

    @staticmethod
    def _collection_item(collection: Any, key: Any) -> Any:
        if collection is None:
            return None
        if isinstance(collection, Mapping):
            return collection.get(key)
        try:
            for item in collection:
                if getattr(item, "entity_id", None) == key or getattr(item, "id", None) == key:
                    return item
        except TypeError:
            return None
        return None

    @classmethod
    def _name_for_entity(cls, entity: Any) -> str:
        if entity is None:
            return "?"
        for candidate in (
            getattr(entity, "name", None),
            getattr(getattr(entity, "definition", None), "name", None),
            getattr(entity, "card_id", None),
            getattr(getattr(entity, "definition", None), "card_id", None),
        ):
            if candidate not in (None, ""):
                return str(candidate)
        return str(entity)

    @classmethod
    def _describe_command(cls, core: Any, command: Any, player_index: int) -> str:
        kind = cls._command_type(command).replace(" ", "_")
        players = getattr(core, "players", None)
        player = None
        try:
            player = players[player_index]
        except (TypeError, IndexError, KeyError):
            pass

        if kind in {"play_card", "playcard"}:
            index = getattr(command, "hand_index", "?")
            card = None
            try:
                card = player.hand[int(index)]
            except (AttributeError, TypeError, ValueError, IndexError):
                pass
            name = cls._name_for_entity(card)
            mode = getattr(command, "mode_id", "normal")
            return f"Play {name} [{mode}]"
        if kind == "attack":
            attacker_id = getattr(command, "attacker_id", "?")
            target_id = getattr(command, "target_id", None)
            board = getattr(player, "board", None)
            attacker = cls._collection_item(board, attacker_id)
            attacker_name = cls._name_for_entity(attacker)
            if target_id is None:
                return f"Attack {attacker_name} -> enemy leader"
            enemy = None
            try:
                enemy = players[1 - player_index]
            except (TypeError, IndexError, KeyError):
                pass
            target = cls._collection_item(getattr(enemy, "board", None), target_id)
            return f"Attack {attacker_name} -> {cls._name_for_entity(target)}"
        if kind in {"evolve", "super_evolve"}:
            unit_id = getattr(command, "unit_id", "?")
            unit = cls._collection_item(getattr(player, "board", None), unit_id)
            action = "Super-evolve" if kind == "super_evolve" else "Evolve"
            return f"{action} {cls._name_for_entity(unit)}"
        if kind in {"use_extra_pp", "useextrapp"}:
            return "Use Extra PP"
        if kind in {"choose", "choice"}:
            option = getattr(command, "option_id", "?")
            request = getattr(getattr(core, "state", None), "pending_choice", None)
            label = None
            for candidate in getattr(request, "options", ()) or ():
                if getattr(candidate, "option_id", None) == option:
                    label = getattr(candidate, "label", None)
                    break
            return f"Choose {label or option}"
        if kind in {"activate_amulet", "activateamulet"}:
            entity = cls._collection_item(
                getattr(player, "board", None), getattr(command, "amulet_id", "?")
            )
            return f"Activate {cls._name_for_entity(entity)}"
        if kind in {"begin_fusion", "beginfusion"}:
            entity = cls._collection_item(
                getattr(player, "hand", None), getattr(command, "fusion_entity_id", "?")
            )
            return f"Begin fusion {cls._name_for_entity(entity)}"
        return type(command).__name__

    @classmethod
    def _transition_has_random(cls, transition: Any) -> bool:
        for event in getattr(transition, "events", ()) or ():
            event_type = getattr(event, "type", event)
            event_type = getattr(event_type, "value", event_type)
            text = str(event_type).casefold()
            if "random" in text:
                return True
        return False


def solve_swb_rl(
    source: Any,
    *,
    player_index: int | None = None,
    max_depth: int = 16,
    node_limit: int = 50_000,
) -> SwbRlSearchResult:
    """Convenience wrapper used by callers that do not need a backend object."""

    return SwbRlBackend(max_depth=max_depth, node_limit=node_limit).solve(
        source, player_index=player_index
    )


__all__ = ["SwbRlBackend", "SwbRlSearchResult", "solve_swb_rl"]
