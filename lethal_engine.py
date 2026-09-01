from __future__ import annotations
from typing import Dict, List, Optional, Tuple
from lethal_models import LethalState, LethalFollower, LethalHandCard, LethalResult
from stochastic_calculator import StochasticDamageCalculator
from event_interpreter import EventInterpreter

class LethalEngine:
    def __init__(self, card_db: Optional[Dict[int, LethalHandCard]] = None, max_depth: int = 12, rules: Optional[Dict[int, dict]] = None):
        self.card_db = card_db or {}
        self.max_depth = max_depth
        self.interpreter = EventInterpreter(rules)
        self.memo: Dict[Tuple, Tuple[float, List[str]]] = {}

    def solve(self, state: LethalState) -> LethalResult:
        self.memo.clear()
        best_prob, best_seq = self._search(state, depth=0)
        
        incomplete = any(step.startswith("[incomplete:") or step.startswith("[unsupported effects:") for step in best_seq)
        if best_prob > 0.0 and incomplete:
            return LethalResult("INCOMPLETE", best_prob, best_seq)
        if best_prob >= 0.9999:
            return LethalResult("CONFIRMED", 1.0, best_seq)
        elif best_prob > 0.0:
            return LethalResult("PROBABILISTIC", best_prob, best_seq)
        else:
            return LethalResult("NO_LETHAL", 0.0, [])

    def _can_possibly_kill(self, state: LethalState) -> bool:
        potential = sum(f.atk * f.attacks_left for f in state.my_board if f.atk > 0)
        
        total_draws = 0
        for c in state.hand:
            will_enhance = (c.enhance_cost is not None and state.pp >= c.enhance_cost)
            dmg = c.enhance_face_damage if (will_enhance and c.enhance_face_damage > 0) else c.face_damage
            potential += dmg
            
            is_storm = c.static_storm or (will_enhance and c.enhance_gain_storm)
            eff_atk = c.atk + c.buff_atk + (c.enhance_buff_atk if will_enhance else 0)
            if is_storm or c.type == 1:
                potential += eff_atk
                
            if c.is_random_damage:
                potential += c.random_hits * c.damage_per_hit
            total_draws += c.draw_count

        if total_draws > 0 and state.total_deck_count > 0 and self.card_db:
            available_damages = []
            for cid, count in state.deck_distribution.items():
                if count > 0 and cid in self.card_db:
                    dc = self.card_db[cid]
                    d_val = dc.face_damage + (dc.atk if (dc.static_storm or dc.type == 1) else 0)
                    available_damages.extend([d_val] * count)
            available_damages.sort(reverse=True)
            potential += sum(available_damages[:total_draws])

        # 超进化 +3 攻，普通进化 +2 攻
        if state.sep > 0:
            potential += 3
        elif state.ep > 0:
            potential += 2

        return potential >= state.enemy_hp

    def _search(self, state: LethalState, depth: int) -> Tuple[float, List[str]]:
        if state.enemy_hp <= 0:
            return 1.0, state.history
        if depth >= self.max_depth:
            return 0.0, []

        key = state.state_key()
        if key in self.memo:
            return self.memo[key]

        # Engage can unlock delayed effects (damage, summons, buffs), so the
        # coarse damage bound must not prune a state while an activation is
        # available.
        has_engage = any(
            f.countdown is not None and any(
                isinstance(a, dict) and a.get("trigger") == "on_engage"
                for m in self.interpreter.rules.get(f.card_id, {}).get("modes", ())
                for a in m.get("abilities", ())
            )
            for f in state.my_board
        )
        if not has_engage and not self._can_possibly_kill(state):
            self.memo[key] = (0.0, [])
            return 0.0, []

        best_prob = 0.0
        best_path: List[str] = []
        has_ward = any(f.is_ward for f in state.enemy_board)

        # -------------------------------------------------------------
        # 0. 护符启动（Engage）
        # -------------------------------------------------------------
        for source in list(state.my_board):
            if source.countdown is None:
                continue
            rule = self.interpreter.rules.get(source.card_id, {})
            abilities = [a for m in rule.get("modes", ()) for a in m.get("abilities", ()) if isinstance(a, dict) and a.get("trigger") == "on_engage"]
            if not abilities:
                continue
            engage_cost = max((int(a.get("cost", 0) or 0) for a in abilities), default=0)
            if state.pp < engage_cost:
                continue
            resolved = self.interpreter.engage(state, source.unique_id)
            next_s = resolved.state
            self._mark_result_gaps(next_s, resolved.unsupported_ops)
            next_s.history.append(f"【启动】启动 {source.name} (花费 {engage_cost} PP, 倒计时: {source.countdown}->{next((f.countdown for f in next_s.my_board if f.unique_id == source.unique_id), source.countdown)})")
            prob, path = self._search(next_s, depth + 1)
            if prob > best_prob:
                best_prob, best_path = prob, path
                if best_prob >= 0.9999:
                    self.memo[key] = (1.0, best_path)
                    return 1.0, best_path

        # -------------------------------------------------------------
        # 1. 随从走脸
        # -------------------------------------------------------------
        if not has_ward:
            for idx, f in enumerate(state.my_board):
                if f.can_attack_leader and f.attacks_left > 0 and f.atk > 0:
                    resolved = self.interpreter.attack_leader(state, f.unique_id)
                    next_s = resolved.state
                    self._mark_rule_gap(next_s, f.card_id)
                    next_s.history.append(f"【随从走脸】{f.name} 攻击主战者 (造成 {f.atk} 伤, 敌HP剩: {next_s.enemy_hp})")
                    prob, path = self._search(next_s, depth + 1)
                    if prob > best_prob:
                        best_prob, best_path = prob, path
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path

        # -------------------------------------------------------------
        # 2. 进化 / 超进化（区分 SEP +3/+3 与 EP +2/+2）
        # -------------------------------------------------------------
        # 2A: 超进化 (SEP) -> +3/+3
        if state.sep > 0:
            for idx, f in enumerate(state.my_board):
                if not f.is_evolved:
                    rule = self.interpreter.rules.get(f.card_id, {})
                    for target_uid in self._rule_target_options(state, rule, "normal", "on_super_evolve"):
                        resolved = self.interpreter.evolve(state, f.unique_id, super_evolve=True, target_uid=target_uid)
                        next_s = resolved.state
                        self._mark_result_gaps(next_s, resolved.unsupported_ops)
                        new_atk = next_s.my_board[idx].atk
                        next_s.history.append(f"【超进化】超进化 {f.name} (Atk变为: {new_atk})")
                        prob, path = self._search(next_s, depth + 1)
                        if prob > best_prob:
                            best_prob, best_path = prob, path
                            if best_prob >= 0.9999:
                                self.memo[key] = (1.0, best_path)
                                return 1.0, best_path

        # 2B: 普通进化 (EP) -> +2/+2
        if state.ep > 0:
            for idx, f in enumerate(state.my_board):
                if not f.is_evolved:
                    rule = self.interpreter.rules.get(f.card_id, {})
                    target_uid = self._rule_target_options(state, rule, "normal", "on_evolve")[0]
                    resolved = self.interpreter.evolve(state, f.unique_id, target_uid=target_uid)
                    next_s = resolved.state
                    self._mark_result_gaps(next_s, resolved.unsupported_ops)
                    new_atk = next_s.my_board[idx].atk
                    next_s.history.append(f"【普通进化】进化 {f.name} (Atk变为: {new_atk})")
                    prob, path = self._search(next_s, depth + 1)
                    if prob > best_prob:
                        best_prob, best_path = prob, path
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path

        # -------------------------------------------------------------
        # 3. 随从解场（守护存在时强制撞守护）
        # -------------------------------------------------------------
        if len(state.enemy_board) > 0 or len(state.my_board) >= 5:
            for m_idx, f in enumerate(state.my_board):
                if f.can_attack_field and f.attacks_left > 0 and f.atk > 0:
                    for e_idx, enemy in enumerate(state.enemy_board):
                        if has_ward and not enemy.is_ward:
                            continue

                        resolved = self.interpreter.attack_follower(state, f.unique_id, state.enemy_board[e_idx].unique_id)
                        next_s = resolved.state
                        self._mark_rule_gap(next_s, f.card_id)
                        target = state.enemy_board[e_idx]
                        log = f"【随从解场】{f.name} 攻击 敌方{target.name}"
                        if not any(x.unique_id == target.unique_id for x in next_s.enemy_board):
                            log += " (破坏敌方随从)"
                        if not any(x.unique_id == f.unique_id for x in next_s.my_board):
                            log += " (己方随从阵亡腾出格子)"

                        next_s.history.append(log)
                        prob, path = self._search(next_s, depth + 1)
                        if prob > best_prob:
                            best_prob, best_path = prob, path
                            if best_prob >= 0.9999:
                                self.memo[key] = (1.0, best_path)
                                return 1.0, best_path

        # -------------------------------------------------------------
        # 4. 打出手牌
        # -------------------------------------------------------------
        for h_idx, card in enumerate(state.hand):
            if card.enhance_cost is not None and state.pp >= card.enhance_cost:
                cost_to_pay = card.enhance_cost
                is_enhance = True
            elif state.pp >= card.cost:
                cost_to_pay = card.cost
                is_enhance = False
            else:
                continue

            if card.type == 1 and len(state.my_board) >= 5:
                continue

            if card.req_rally > 0 and state.rally < card.req_rally:
                continue
            if card.req_cemetery > 0 and state.cemetery < card.req_cemetery:
                continue
            if card.req_overflow and not state.is_awakening:
                continue

            # 4.1 随机弹幕
            if card.is_random_damage:
                en_hps = [e.hp for e in state.enemy_board if e.hp > 0]
                dp_prob = StochasticDamageCalculator.calculate_lethal_prob(
                    enemy_face_hp=state.enemy_hp,
                    enemy_follower_hps=en_hps,
                    total_hits=card.random_hits,
                    damage_per_hit=card.damage_per_hit
                )
                if dp_prob > best_prob:
                    best_prob = dp_prob
                    best_path = state.history + [
                        f"【随机弹幕】打出 {card.name} (花费 {cost_to_pay} PP) -> 命中主战者斩杀概率: {dp_prob*100:.2f}%"
                    ]
                    if best_prob >= 0.9999:
                        self.memo[key] = (1.0, best_path)
                        return 1.0, best_path

            # 4.2 抽牌分支
            elif card.draw_count > 0 and state.total_deck_count > 0 and self.card_db:
                draw_sum_prob = 0.0
                sample_path = []
                
                for draw_id, count in list(state.deck_distribution.items()):
                    if count <= 0 or draw_id not in self.card_db:
                        continue
                    p_draw = count / state.total_deck_count
                    drawn_card = self.card_db[draw_id]

                    next_s = state.clone()
                    next_s.hand.pop(h_idx)
                    next_s.pp -= cost_to_pay
                    next_s.play_count += 1
                    if card.type == 3:
                        next_s.cemetery += 1
                    next_s.hand.append(drawn_card)
                    next_s.total_deck_count -= 1
                    next_s.deck_distribution[draw_id] -= 1
                    next_s.history.append(f"【抽牌】打出 {card.name} 抽到『{drawn_card.name}』(概率 {p_draw*100:.1f}%)")

                    sub_prob, sub_path = self._search(next_s, depth + 1)
                    draw_sum_prob += p_draw * sub_prob
                    if sub_prob > 0 and not sample_path:
                        sample_path = sub_path

                if draw_sum_prob > best_prob:
                    best_prob = draw_sum_prob
                    best_path = sample_path
                    if best_prob >= 0.9999:
                        self.memo[key] = (1.0, best_path)
                        return 1.0, best_path

            # 4.3 确定性打出
            else:
                target_uids = self._target_options(state, card, is_enhance)
                for target_uid in target_uids:
                    resolved = self.interpreter.play(state, card.unique_id, mode="enhance" if is_enhance else "normal", target_uid=target_uid)
                    next_s = resolved.state
                    if resolved.unsupported_ops:
                        self._mark_result_gaps(next_s, resolved.unsupported_ops)
                    tag = " (爆能)" if is_enhance else ""
                    target_tag = f" -> 目标 {target_uid}" if target_uid is not None else ""
                    next_s.history.append(f"【使用卡牌】打出 {card.name}{tag}{target_tag} (花费 {cost_to_pay} PP, 敌HP剩: {next_s.enemy_hp})")
                    prob, path = self._search(next_s, depth + 1)
                    if prob > best_prob:
                        best_prob, best_path = prob, path
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path

        self.memo[key] = (best_prob, best_path)
        return best_prob, best_path

    def _mark_rule_gap(self, state: LethalState, card_id: int) -> None:
        if not self.interpreter.rules:
            return
        rule = self.interpreter.rules.get(card_id)
        if not rule:
            state.history.append(f"[incomplete: missing_rule:{card_id}]")
        elif rule.get("support") in ("partial", "unsupported"):
            clauses = rule.get("unparsed_clauses", ())
            reason = ""
            if clauses:
                snippet = " ".join(str(clauses[0]).split())[:120]
                reason = f":reason={snippet}"
            state.history.append(f"[incomplete: {rule.get('support')}_rule:{card_id}{reason}]")

    @staticmethod
    def _mark_result_gaps(state: LethalState, gaps) -> None:
        for gap in gaps:
            state.history.append(f"[incomplete: {gap}]")

    def _target_options(self, state: LethalState, card: LethalHandCard, enhanced: bool) -> List[object]:
        """Return target branches for deterministic enemy-follower effects."""
        rule = self.interpreter.rules.get(card.card_id, {})
        modes = rule.get("modes", ()) if isinstance(rule, dict) else ()
        kind = "enhance" if enhanced else "normal"
        selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == kind), None)
        if selected is None:
            selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == "normal"), None)
        return self._rule_target_options(state, rule, "enhance" if enhanced else "normal", "on_play")

    def _rule_target_options(self, state: LethalState, rule: dict, kind: str, trigger: str) -> List[object]:
        modes = rule.get("modes", ()) if isinstance(rule, dict) else ()
        selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == kind), None)
        if selected is None:
            selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == "normal"), None)

        def flatten(effects):
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                yield effect
                if effect.get("op") == "sequence":
                    yield from flatten(effect.get("effects", ()))

        effects = [e for a in (selected or {}).get("abilities", ()) if isinstance(a, dict) and a.get("trigger") == trigger for e in flatten(a.get("effects", ()))]
        target_effects = [e for e in effects if isinstance(e, dict) and e.get("op") == "damage" and isinstance(e.get("target"), dict) and e["target"].get("scope") == "enemy_follower"]
        split_effect = next((e for e in target_effects if e["target"].get("selection") == "all" and e["target"].get("allocation") in ("split", "ordered_split")), None)
        if split_effect is not None and state.enemy_board:
            total = int(split_effect.get("amount", 0))
            remaining = total
            allocation = {}
            for follower in state.enemy_board:
                if remaining <= 0:
                    break
                assigned = min(remaining, max(0, follower.hp))
                if assigned:
                    allocation[follower.unique_id] = assigned
                    remaining -= assigned
            return [allocation]

        needs_target = any(
            isinstance(e, dict)
            and e.get("op") in ("destroy", "damage")
            and isinstance(e.get("target"), dict)
            and e["target"].get("scope") == "enemy_follower"
            and e["target"].get("selection") != "all"
            for e in effects
        )
        return [None] if not needs_target else [f.unique_id for f in state.enemy_board]
