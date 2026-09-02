from __future__ import annotations
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass(frozen=True)
class LethalFollower:
    unique_id: int
    card_id: int
    name: str
    atk: int
    hp: int
    has_storm: bool = False
    has_rush: bool = False
    is_ward: bool = False
    is_evolved: bool = False
    can_attack_leader: bool = False
    can_attack_field: bool = False
    attacks_left: int = 1
    damage_cap: Optional[int] = None
    # Countdown is used by amulets represented on the unified board model.
    countdown: Optional[int] = None
    # Runtime status flags.  These are deliberately explicit instead of
    # hiding text effects in the card id so copies can preserve the current
    # entity state.
    is_super_evolved: bool = False
    abilities_removed: bool = False
    statuses: Tuple[str, ...] = ()
    last_words: Tuple[Dict[str, Any], ...] = ()
    # Abilities granted by another card (for example, an exact copy that is
    # destroyed at the end of the opponent's turn).  These are kept on the
    # entity rather than on the catalog rule so an exact copy preserves the
    # temporary listener independently of its printed card text.
    granted_abilities: Tuple[Dict[str, Any], ...] = ()
    # Public dynamic fields exposed by ShadowverseTracker.  They are kept on
    # the entity so transform/reanimate and exact-copy effects can preserve
    # the parts of a card that are observable in the current snapshot.
    base_cost: Optional[int] = None
    spell_boost_count: int = 0
    has_spell_boost: bool = False
    variable_x: int = 0
    supplement_info: Tuple[Tuple[str, int], ...] = ()
    # Index of the next step for a printed progressive/"each turn" sequence.
    # It belongs to the entity (rather than the catalog rule) so exact copies
    # and two copies of the same amulet advance independently.
    progressive_sequence_index: int = 0
    # Explicit projections for combat keywords without legacy boolean fields.
    # ``statuses`` remains the extensible source of truth; these aliases make
    # hand-built fixtures and Tracker snapshots as convenient as Storm/Rush/
    # Ward while being appended fields for positional compatibility.
    has_bane: bool = False
    has_drain: bool = False
    has_ambush: bool = False
    # Tracker's authoritative target projection. Entries are normally enemy
    # follower unique ids; the opponent leader unique id is also included for
    # a legal face attack. ``LethalState`` carries a separate known bit so an
    # empty tuple is not mistaken for missing snapshot data.
    attack_targets: Tuple[Any, ...] = ()
    # Preserve the complete Tracker buff object (sources, stat deltas and
    # temporary flags) instead of re-deriving it from current stats.
    buff: Optional[Dict[str, Any]] = None

@dataclass(frozen=True)
class LethalHandCard:
    unique_id: int
    card_id: int
    name: str
    cost: int
    type: int  # 1: 随从, 2: 护符, 3: 法术
    atk: int = 0
    life: int = 0
    static_storm: bool = False
    static_rush: bool = False
    
    # 爆能强化相关字段
    enhance_cost: Optional[int] = None
    enhance_buff_atk: int = 0
    enhance_gain_storm: bool = False
    enhance_recover_pp: int = 0
    enhance_face_damage: int = 0
    enhance_attacks_per_turn: Optional[int] = None
    
    # 常规属性与触发
    face_damage: int = 0
    draw_count: int = 0
    recover_pp: int = 0
    buff_atk: int = 0
    is_random_damage: bool = False
    random_hits: int = 0
    damage_per_hit: int = 0
    req_rally: int = 0
    req_cemetery: int = 0
    req_overflow: bool = False
    tribes: Tuple[str, ...] = ()
    # Alternate play modes are separate costs.  A card may legally expose
    # more than one of these at the same time; the engine branches over the
    # selected mode and never applies two mode payloads to one play.
    accelerate_cost: Optional[int] = None
    crystallize_cost: Optional[int] = None
    mode_costs: Tuple[Tuple[str, int], ...] = ()
    # Tracker exposes these values directly for cards in hand.  A spellboost
    # operation increments the counter and then dispatches the card's
    # ``on_spellboost`` ability against this same hand entity.
    spell_boost_count: int = 0
    has_spell_boost: bool = False
    variable_x: int = 0
    supplement_info: Tuple[Tuple[str, int], ...] = ()
    # Temporary/static keywords observed on a card while it is in hand.  The
    # old boolean Storm/Rush fields remain for compatibility; this tuple lets
    # a grant/remove keyword operation preserve Bane/Drain/Ambush as well.
    statuses: Tuple[str, ...] = ()
    has_bane: bool = False
    has_drain: bool = False
    has_ambush: bool = False
    # Full temporary buff payload observed while this card is in hand.
    buff: Optional[Dict[str, Any]] = None
    # Tracker can expose more than one legal cost for a mode (for example
    # multi-level Enhance).  Keep the complete arrays even though the current
    # interpreter chooses the first executable cost for a single action.
    enhance_costs: Tuple[int, ...] = ()
    accelerate_costs: Tuple[int, ...] = ()
    crystallize_costs: Tuple[int, ...] = ()


def create_hand_card_from_rule(card_id: int, info: Dict[str, Any], unique_id: int) -> LethalHandCard:
    """自动将 card_rules.json 的 DSL 结构转换为 LethalHandCard"""
    cost = info.get("cost", 0)
    c_type = info.get("type", 1)
    atk = info.get("atk") or 0
    life = info.get("life") or 0
    name = info.get("name", str(card_id))
    
    static = info.get("static", {})
    static_storm = static.get("has_storm", False)
    static_rush = static.get("has_rush", False)
    statuses = {
        str(item).casefold()
        for item in (info.get("static_keywords", ()) if isinstance(info.get("static_keywords"), (list, tuple, set)) else ())
        if str(item).strip()
    }
    if static_storm:
        statuses.add("storm")
    if static_rush:
        statuses.add("rush")

    enhance_cost = None
    enhance_buff_atk = 0
    enhance_gain_storm = False
    enhance_recover_pp = 0
    enhance_face_damage = 0
    enhance_attacks_per_turn = None

    face_damage = 0
    draw_count = 0
    recover_pp = 0
    buff_atk = 0
    req_rally = 0
    req_cemetery = 0
    req_overflow = False
    accelerate_cost = None
    crystallize_cost = None
    mode_costs: list[tuple[str, int]] = []

    # v2 rules keep alternate mode costs on the mode object.  Preserve all
    # modes here so a later caller can choose one explicitly; do not collapse
    # Enhance/Accelerate/Crystallize into a single boolean.
    for mode_info in info.get("modes", ()) if isinstance(info.get("modes"), (list, tuple)) else ():
        if not isinstance(mode_info, dict):
            continue
        kind = str(mode_info.get("kind", ""))
        raw_cost = mode_info.get("cost")
        if isinstance(raw_cost, (int, float)):
            mode_costs.append((kind, int(raw_cost)))
            if kind == "enhance":
                enhance_cost = int(raw_cost)
            elif kind == "accelerate":
                accelerate_cost = int(raw_cost)
            elif kind == "crystallize":
                crystallize_cost = int(raw_cost)

    is_random = (card_id == 10404110)
    random_hits = 5 if is_random else 0
    damage_per_hit = 2 if is_random else 0

    for item in info.get("on_play", []):
        cond = item.get("if", {})
        actions = item.get("do", [])
        is_enh = "enhance_cost" in cond
        
        if is_enh:
            enhance_cost = cond["enhance_cost"]

        for act in actions:
            op = act.get("op")
            if op == "deal_damage" and act.get("target") in ("enemy_leader", "all_leaders"):
                amt = act.get("amount", 0)
                if is_enh:
                    enhance_face_damage += amt
                else:
                    face_damage += amt
                    if "cemetery_gte" in cond:
                        req_cemetery = cond["cemetery_gte"]
                    if "rally_gte" in cond:
                        req_rally = cond["rally_gte"]
                    if cond.get("overflow"):
                        req_overflow = True
            elif op == "gain_status" and act.get("status") == "storm":
                if is_enh:
                    enhance_gain_storm = True
                else:
                    static_storm = True
            elif op == "buff_attack":
                amt = act.get("amount", 0)
                if is_enh:
                    enhance_buff_atk += amt
                else:
                    buff_atk += amt
            elif op == "recover_pp":
                amt = act.get("amount", 0)
                if is_enh:
                    enhance_recover_pp += amt
                else:
                    recover_pp += amt
            elif op == "set_max_attacks":
                amt = act.get("amount", 1)
                if is_enh:
                    enhance_attacks_per_turn = amt

    statuses.update(
        str(item).casefold()
        for item in (info.get("statuses", ()) if isinstance(info.get("statuses"), (list, tuple, set)) else ())
        if str(item).strip()
    )
    status_values = tuple(sorted(statuses))
    return LethalHandCard(
        unique_id=unique_id,
        card_id=card_id,
        name=name,
        cost=cost,
        type=c_type,
        atk=atk,
        life=life,
        static_storm=static_storm,
        static_rush=static_rush,
        enhance_cost=enhance_cost,
        enhance_buff_atk=enhance_buff_atk,
        enhance_gain_storm=enhance_gain_storm,
        enhance_recover_pp=enhance_recover_pp,
        enhance_face_damage=enhance_face_damage,
        enhance_attacks_per_turn=enhance_attacks_per_turn,
        face_damage=face_damage,
        draw_count=draw_count,
        recover_pp=recover_pp,
        buff_atk=buff_atk,
        is_random_damage=is_random,
        random_hits=random_hits,
        damage_per_hit=damage_per_hit,
        req_rally=req_rally,
        req_cemetery=req_cemetery,
        req_overflow=req_overflow,
        accelerate_cost=accelerate_cost,
        crystallize_cost=crystallize_cost,
        mode_costs=tuple(mode_costs),
        spell_boost_count=int(info.get("spell_boost_count", 0) or 0),
        has_spell_boost=bool(info.get("has_spell_boost", False)),
        variable_x=int(info.get("variable_x", 0) or 0),
        supplement_info=tuple(sorted((str(key), int(value)) for key, value in (info.get("supplement_info", {}) or {}).items() if isinstance(value, (int, float)))) if isinstance(info.get("supplement_info"), dict) else (),
        statuses=status_values,
        has_bane="bane" in status_values,
        has_drain="drain" in status_values,
        has_ambush="ambush" in status_values,
        enhance_costs=tuple(cost for kind, cost in mode_costs if kind == "enhance"),
        accelerate_costs=tuple(cost for kind, cost in mode_costs if kind == "accelerate"),
        crystallize_costs=tuple(cost for kind, cost in mode_costs if kind == "crystallize"),
    )

@dataclass
class LethalState:
    enemy_hp: int
    pp: int
    max_pp: int
    ep: int
    sep: int
    rally: int = 0
    cemetery: int = 0
    is_awakening: bool = False
    play_count: int = 0
    faith: int = 0
    evolved_allies_this_turn: int = 0
    # Player-initiated evolution is a separate budget from automatic card
    # effects.  Tracker's ``is_evolved_this_turn`` includes both kinds, so
    # SnapshotAdapter derives this counter from the recent action stream when
    # available; auto_evolve deliberately leaves it unchanged.
    manual_evolutions_this_turn: int = 0
    evolved_allies_this_match: int = 0
    my_board: List[LethalFollower] = field(default_factory=list)
    enemy_board: List[LethalFollower] = field(default_factory=list)
    hand: List[LethalHandCard] = field(default_factory=list)
    deck_distribution: Dict[int, int] = field(default_factory=dict)
    total_deck_count: int = 0
    active_crests: List[int] = field(default_factory=list)
    history: List[str] = field(default_factory=list)
    ally_hp: int = 0
    ally_max_hp: int = 0
    # Public tracker resources which were not in the first solver slice.
    extra_pp: int = 0
    earth_sigil: int = 0
    skybound_art: int = 0
    super_skybound_art: int = 0
    # Faith is a resource family: ``faith`` remains the aggregate/backwards
    # compatible value while each source instance may carry its own value and
    # granted abilities.
    faith_instances: List[Dict[str, Any]] = field(default_factory=list)
    # Crest instances retain identity and countdown; active_crests is kept as
    # a compatibility projection for older callers.
    crest_instances: List[Dict[str, Any]] = field(default_factory=list)
    # The opponent's Crests are public too.  Keep them separate from allied
    # Crests so an effect such as “give yourself and your opponent Crest: X”
    # never changes the ally crest-count condition or its end-of-turn pool.
    enemy_active_crests: List[int] = field(default_factory=list)
    enemy_crest_instances: List[Dict[str, Any]] = field(default_factory=list)
    enemy_damage_taken_modifier: int = 0
    last_played_card_cost: Optional[int] = None
    last_played_mode: Optional[str] = None
    last_played_card_type: Optional[Any] = None
    last_played_tribes: Tuple[str, ...] = ()
    attacked_with_follower_this_turn: bool = False
    selected_mode_choice: Optional[Any] = None
    last_created_uid: Optional[int] = None
    last_destroyed_snapshot: Optional[LethalFollower] = None
    # Entity snapshots in the public destroyed pool.  A card enters this pool
    # when destroyed and remains available after Reanimate; preserve every
    # occurrence so duplicate card ids retain their probability weight.  Keep
    # the full follower snapshot so exact-copy effects retain current stats,
    # keywords and Last Words.
    destroyed_this_match: List[LethalFollower] = field(default_factory=list)
    # Tracker may omit the pool field in hand-built/minimal snapshots; the
    # distinction lets Reanimate report an unknown pool instead of treating
    # missing public information as an empty graveyard.
    destroyed_pool_known: bool = False
    # Deprecated compatibility flag retained for older fixtures.  Tracker's
    # destroyed pool is the reanimation pool, so current interpreter paths do
    # not use this flag to downgrade a result.
    destroyed_pool_exact: bool = True
    # Base costs played during this match.  Zerael-style Invoke conditions use
    # the set, while retaining a tuple keeps state keys deterministic.
    played_base_costs: Tuple[int, ...] = ()
    # ``replace_deck`` changes the deck identity to an opaque named template.
    # The public count may remain known, but individual card identities are no
    # longer available to the solver.
    deck_replacement: Optional[str] = None
    # Snapshot legality contract. These fields are populated by
    # ``SnapshotAdapter`` and intentionally default to unknown for hand-built
    # solver fixtures, preserving their historical intrinsic-keyword rules.
    legal_actions_known: bool = False
    legal_actions: Dict[str, Any] = field(default_factory=dict)
    legal_attack_targets: Dict[int, Tuple[Any, ...]] = field(default_factory=dict)
    # ``FieldCard.attack_targets`` is useful even when a caller did not
    # request/receive the separate LegalActions object.  Keep a distinct
    # presence bit so an explicitly empty target list (no legal attack) is
    # not confused with an omitted projection (unknown).
    attack_targets_known: bool = False
    legal_modes: Dict[int, Tuple[str, ...]] = field(default_factory=dict)
    legal_modes_known: bool = False
    legal_play_uids: Tuple[int, ...] = ()
    legal_evolve_uids: Tuple[int, ...] = ()
    legal_super_evolve_uids: Tuple[int, ...] = ()
    attacked_card_uids: Tuple[int, ...] = ()
    enemy_leader_uid: Optional[int] = None
    turn_number: Optional[int] = None
    # Tracker exposes the first turn on which ordinary and super evolution
    # become legal.  These values must survive hypothetical actions because
    # ``LegalActions`` is invalidated after each simulated mutation; otherwise
    # the search could evolve before the unlock turn simply because the live
    # UID lists are no longer considered fresh.
    evolve_turn: Optional[int] = None
    super_evolve_turn: Optional[int] = None
    # Complete player-level buff payloads (damage cut/increase, max life,
    # temporary shields, and their source list) from Tracker.
    ally_buff: Optional[Dict[str, Any]] = None
    enemy_buff: Optional[Dict[str, Any]] = None

    @property
    def available_modes(self) -> Dict[int, Tuple[str, ...]]:
        """Alias used by UI/Tracker callers for the legal mode projection."""
        return self.legal_modes

    @property
    def attack_targets(self) -> Dict[int, Tuple[Any, ...]]:
        return self.legal_attack_targets

    @property
    def current_turn_attacks(self) -> Tuple[int, ...]:
        return self.attacked_card_uids

    def state_key(self) -> Tuple:
        # Board order is observable (ordered split damage and entrance order),
        # so retain sequence order when memoising.  Hand/deck order remains
        # abstract because only multiplicity is public to the solver.
        my_b = tuple((f.unique_id, f.card_id, f.atk, f.hp, f.attacks_left, f.is_evolved, f.is_super_evolved, f.can_attack_leader, f.can_attack_field, f.countdown, f.abilities_removed, f.has_bane, f.has_drain, f.has_ambush, tuple(f.attack_targets), tuple(f.statuses), repr(f.last_words), repr(f.granted_abilities), repr(f.buff), f.base_cost, f.spell_boost_count, f.has_spell_boost, f.variable_x, f.supplement_info, f.progressive_sequence_index) for f in self.my_board)
        en_b = tuple((f.unique_id, f.card_id, f.atk, f.hp, f.is_ward, f.has_storm, f.has_rush, f.damage_cap, f.is_evolved, f.is_super_evolved, f.abilities_removed, f.has_bane, f.has_drain, f.has_ambush, tuple(f.attack_targets), tuple(f.statuses), repr(f.last_words), repr(f.granted_abilities), repr(f.buff), f.base_cost, f.spell_boost_count, f.has_spell_boost, f.variable_x, f.supplement_info, f.progressive_sequence_index) for f in self.enemy_board)
        h_ids = tuple(sorted((c.unique_id, c.card_id, c.cost, c.atk, c.life, c.static_storm, c.static_rush, c.has_bane, c.has_drain, c.has_ambush, c.tribes, c.spell_boost_count, c.has_spell_boost, c.variable_x, c.supplement_info, tuple(c.statuses), repr(c.buff), tuple(c.enhance_costs), tuple(c.accelerate_costs), tuple(c.crystallize_costs)) for c in self.hand))
        deck = tuple(sorted((k, v) for k, v in self.deck_distribution.items() if v > 0))
        faith = tuple(sorted((str(item.get("unique_id", "")), int(item.get("source_card_id", 0) or 0), int(item.get("value", 0) or 0), int(item.get("mode_limit_bonus", 0) or 0), repr(item.get("abilities", ()))) for item in self.faith_instances if isinstance(item, dict)))
        crests = tuple(sorted((str(item.get("unique_id", "")), int(item.get("card_id", 0) or 0), int(item.get("style_id", 0) or 0), int(item.get("countdown", -1) if item.get("countdown") is not None else -1), int(item.get("variable_x", 0) or 0), repr(item.get("buff")), repr(item.get("abilities", ()))) for item in self.crest_instances if isinstance(item, dict)))
        enemy_crests = tuple(sorted((str(item.get("unique_id", "")), int(item.get("card_id", 0) or 0), int(item.get("style_id", 0) or 0), int(item.get("countdown", -1) if item.get("countdown") is not None else -1), int(item.get("variable_x", 0) or 0), repr(item.get("buff")), repr(item.get("abilities", ()))) for item in self.enemy_crest_instances if isinstance(item, dict)))
        return (
            self.enemy_hp, self.ally_hp, self.ally_max_hp, self.pp, self.max_pp, self.extra_pp, self.ep, self.sep, self.cemetery,
            self.rally, self.is_awakening, self.play_count, 
            self.faith, faith, self.earth_sigil, self.skybound_art, self.super_skybound_art,
            self.evolved_allies_this_turn, self.evolved_allies_this_match,
            self.manual_evolutions_this_turn,
            self.enemy_damage_taken_modifier, self.last_played_card_cost,
            self.last_played_mode, self.last_played_card_type, self.last_played_tribes,
            self.attacked_with_follower_this_turn,
            self.selected_mode_choice,
            self.last_created_uid,
             tuple((f.unique_id, f.card_id, f.atk, f.hp, f.is_evolved, f.is_super_evolved, f.countdown, f.abilities_removed, f.has_bane, f.has_drain, f.has_ambush, tuple(f.attack_targets), tuple(f.statuses), repr(f.last_words), repr(f.granted_abilities), repr(f.buff), f.base_cost, f.spell_boost_count, f.has_spell_boost, f.variable_x, f.supplement_info, f.progressive_sequence_index) for f in self.destroyed_this_match),
             self.destroyed_pool_known, self.destroyed_pool_exact, tuple(self.active_crests), crests,
             tuple(self.enemy_active_crests), self.total_deck_count, self.deck_replacement, tuple(self.played_base_costs), deck,
             self.legal_actions_known, repr(self.legal_actions), self.attack_targets_known, tuple(sorted((int(uid), tuple(targets)) for uid, targets in self.legal_attack_targets.items())),
             tuple(sorted((int(uid), tuple(modes)) for uid, modes in self.legal_modes.items())), self.legal_modes_known,
             tuple(self.legal_play_uids), tuple(self.legal_evolve_uids), tuple(self.legal_super_evolve_uids),
            tuple(self.attacked_card_uids), self.enemy_leader_uid, self.turn_number,
            self.evolve_turn, self.super_evolve_turn, my_b, en_b, h_ids,
             repr(self.ally_buff), repr(self.enemy_buff)
        )

    def clone(self) -> LethalState:
        return LethalState(
            enemy_hp=self.enemy_hp,
            pp=self.pp,
            max_pp=self.max_pp,
            ep=self.ep,
            sep=self.sep,
            rally=self.rally,
            cemetery=self.cemetery,
            is_awakening=self.is_awakening,
            play_count=self.play_count,
            faith=self.faith,
            evolved_allies_this_turn=self.evolved_allies_this_turn,
            manual_evolutions_this_turn=self.manual_evolutions_this_turn,
            evolved_allies_this_match=self.evolved_allies_this_match,
            my_board=list(self.my_board),
            enemy_board=list(self.enemy_board),
            hand=list(self.hand),
            deck_distribution=dict(self.deck_distribution),
            total_deck_count=self.total_deck_count,
            active_crests=list(self.active_crests),
            history=list(self.history),
            ally_hp=self.ally_hp,
            ally_max_hp=self.ally_max_hp,
            extra_pp=self.extra_pp,
            earth_sigil=self.earth_sigil,
            skybound_art=self.skybound_art,
            super_skybound_art=self.super_skybound_art,
            # Resource instances carry nested ability lists.  A shallow dict
            # copy lets one stochastic branch append a granted listener into
            # its sibling's state; deep-copy these public instance payloads
            # so branch expansion remains isolated.
            faith_instances=copy.deepcopy(self.faith_instances),
            crest_instances=copy.deepcopy(self.crest_instances),
            enemy_active_crests=list(self.enemy_active_crests),
            enemy_crest_instances=copy.deepcopy(self.enemy_crest_instances),
            enemy_damage_taken_modifier=self.enemy_damage_taken_modifier,
            last_played_card_cost=self.last_played_card_cost,
            last_played_mode=self.last_played_mode,
            last_played_card_type=self.last_played_card_type,
            last_played_tribes=tuple(self.last_played_tribes),
            attacked_with_follower_this_turn=self.attacked_with_follower_this_turn,
            selected_mode_choice=self.selected_mode_choice,
            last_created_uid=self.last_created_uid,
            last_destroyed_snapshot=self.last_destroyed_snapshot,
            destroyed_this_match=list(self.destroyed_this_match),
            destroyed_pool_known=self.destroyed_pool_known,
            destroyed_pool_exact=self.destroyed_pool_exact,
            played_base_costs=tuple(self.played_base_costs),
            deck_replacement=self.deck_replacement,
            legal_actions_known=self.legal_actions_known,
            legal_actions=copy.deepcopy(self.legal_actions),
            legal_attack_targets={int(uid): tuple(targets) for uid, targets in self.legal_attack_targets.items()},
            attack_targets_known=self.attack_targets_known,
            legal_modes={int(uid): tuple(modes) for uid, modes in self.legal_modes.items()},
            legal_modes_known=self.legal_modes_known,
            legal_play_uids=tuple(self.legal_play_uids),
            legal_evolve_uids=tuple(self.legal_evolve_uids),
            legal_super_evolve_uids=tuple(self.legal_super_evolve_uids),
            attacked_card_uids=tuple(self.attacked_card_uids),
            enemy_leader_uid=self.enemy_leader_uid,
            turn_number=self.turn_number,
            evolve_turn=self.evolve_turn,
            super_evolve_turn=self.super_evolve_turn,
            ally_buff=copy.deepcopy(self.ally_buff),
            enemy_buff=copy.deepcopy(self.enemy_buff),
        )

@dataclass
class LethalResult:
    status: str
    probability: float
    sequence: List[str]
