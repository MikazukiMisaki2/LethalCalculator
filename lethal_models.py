from __future__ import annotations
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
        req_overflow=req_overflow
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
    evolved_allies_this_match: int = 0
    my_board: List[LethalFollower] = field(default_factory=list)
    enemy_board: List[LethalFollower] = field(default_factory=list)
    hand: List[LethalHandCard] = field(default_factory=list)
    deck_distribution: Dict[int, int] = field(default_factory=dict)
    total_deck_count: int = 0
    active_crests: List[int] = field(default_factory=list)
    history: List[str] = field(default_factory=list)

    def state_key(self) -> Tuple:
        my_b = tuple(sorted((f.card_id, f.atk, f.hp, f.attacks_left, f.is_evolved, f.can_attack_leader, f.countdown) for f in self.my_board))
        en_b = tuple(sorted((f.card_id, f.atk, f.hp, f.is_ward, f.damage_cap) for f in self.enemy_board))
        h_ids = tuple(sorted(c.unique_id for c in self.hand))
        deck = tuple(sorted((k, v) for k, v in self.deck_distribution.items() if v > 0))
        return (
            self.enemy_hp, self.pp, self.ep, self.sep, self.cemetery, 
            self.rally, self.is_awakening, self.play_count, 
            self.faith, self.evolved_allies_this_turn, self.evolved_allies_this_match,
            my_b, en_b, h_ids, deck
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
            evolved_allies_this_match=self.evolved_allies_this_match,
            my_board=list(self.my_board),
            enemy_board=list(self.enemy_board),
            hand=list(self.hand),
            deck_distribution=dict(self.deck_distribution),
            total_deck_count=self.total_deck_count,
            active_crests=list(self.active_crests),
            history=list(self.history)
        )

@dataclass
class LethalResult:
    status: str
    probability: float
    sequence: List[str]
