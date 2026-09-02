from __future__ import annotations
from typing import Dict, List, Optional, Tuple
from dataclasses import replace
from itertools import combinations
from lethal_models import LethalState, LethalFollower, LethalHandCard, LethalResult
from stochastic_calculator import StochasticDamageCalculator
from event_interpreter import EventInterpreter

class LethalEngine:
    def __init__(self, card_db: Optional[Dict[int, LethalHandCard]] = None, max_depth: int = 12, rules: Optional[Dict[int, dict]] = None, catalog: Optional[Dict[int, dict]] = None):
        self.card_db = card_db or {}
        self.max_depth = max_depth
        self.catalog = catalog or {}
        self.interpreter = EventInterpreter(rules, catalog=self.catalog, card_db=self.card_db)
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
            mode_potential = 0
            for mode, _ in self.interpreter.available_modes(c, state):
                is_enhance = mode == "enhance"
                dmg = c.enhance_face_damage if is_enhance and c.enhance_face_damage > 0 else (c.face_damage if mode == "normal" else 0)
                is_storm = c.static_storm or (is_enhance and c.enhance_gain_storm)
                eff_atk = c.atk + (c.enhance_buff_atk if is_enhance else c.buff_atk)
                mode_potential = max(mode_potential, dmg + (eff_atk if is_storm or (c.type == 1 and mode in ("normal", "enhance")) else 0))
            potential += mode_potential
                
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
        board_stochastic_triggers = (
            "on_evolve", "on_super_evolve", "on_ally_follower_summon",
            "on_spellboost", "on_ally_follower_evolve", "on_ally_follower_super_evolve",
        )
        has_stochastic = any(self._card_has_stochastic_rule(c) or self._card_has_choice_rule_any(c) for c in state.hand) or any(
            self._trigger_has_stochastic(f.card_id, trigger)
            for f in state.my_board
            for trigger in board_stochastic_triggers
            if trigger not in ("on_evolve", "on_super_evolve") or not f.is_evolved
        )
        if not has_engage and not has_stochastic and not self._can_possibly_kill(state):
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
            if self.interpreter._available_pp(state) < engage_cost:
                continue
            engage_trigger = self._trigger_has_stochastic(source.card_id, "on_engage")
            target_options = self._rule_target_options(state, rule, "normal", "on_engage")
            if not target_options:
                target_options = [None]
            for engage_target in target_options:
                if engage_trigger:
                    branches = self.interpreter.engage_branches(state, source.unique_id, target_uid=engage_target)
                    aggregate = 0.0
                    sample_path: List[str] = []
                    branch_gaps: set[str] = set()
                    best_branch_sub_prob = -1.0
                    for branch in branches:
                        next_s = branch.state
                        if branch.unsupported_ops:
                            self._mark_result_gaps(next_s, branch.unsupported_ops)
                            branch_gaps.update(branch.unsupported_ops)
                        next_s.history.append(
                            f"【启动】启动 {source.name} (花费 {engage_cost} PP, "
                            f"分支概率 {branch.probability*100:.2f}%)"
                        )
                        sub_prob, sub_path = self._search(next_s, depth + 1)
                        aggregate += branch.probability * sub_prob
                        if sub_prob > 0 and sub_prob > best_branch_sub_prob:
                            sample_path = sub_path
                            best_branch_sub_prob = sub_prob
                    if branch_gaps and aggregate > 0:
                        sample_path = list(sample_path) + [f"[incomplete: stochastic:{gap}]" for gap in sorted(branch_gaps)]
                    prob, path = aggregate, sample_path
                else:
                    resolved = self.interpreter.engage(state, source.unique_id, target_uid=engage_target)
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
                    options = [None] if self._trigger_has_stochastic(f.card_id, "on_super_evolve") else self._rule_target_options(state, rule, "normal", "on_super_evolve")
                    for target_uid in options:
                        prob, path = self._evolve_action(state, f, super_evolve=True, target_uid=target_uid, depth=depth)
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
                    options = [None] if self._trigger_has_stochastic(f.card_id, "on_evolve") else self._rule_target_options(state, rule, "normal", "on_evolve")
                    for target_uid in options:
                        prob, path = self._evolve_action(state, f, super_evolve=False, target_uid=target_uid, depth=depth)
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
            # Each tuple is a separate legal action.  In particular, a card
            # with both normal and Enhance costs is explored twice; no branch
            # ever applies both mode payloads.
            for mode, cost_to_pay in self.interpreter.available_modes(card, state):
                if card.type == 1 and mode in ("normal", "enhance") and len(state.my_board) >= 5:
                    continue
                if card.req_rally > 0 and state.rally < card.req_rally:
                    continue
                if card.req_cemetery > 0 and state.cemetery < card.req_cemetery:
                    continue
                if card.req_overflow and not (state.is_awakening or state.max_pp >= 7):
                    continue
                if self.interpreter._resource_preflight(state, self.interpreter.rules.get(card.card_id, {}), mode, source_uid=card.unique_id) is not None:
                    continue

                is_enhance = mode == "enhance"
                if self._card_has_choice_rule(card, mode):
                    choice_prob = 0.0
                    choice_path: List[str] = []
                    for selected_choice in self._choice_options(card, mode):
                        for target_uid in self._target_options(state, card, mode):
                            # A Mode is a player decision, but the selected
                            # payload may itself contain a random effect
                            # (for example Mode 2: Reanimate (2)).  Keep the
                            # decision deterministic while expanding only the
                            # chosen payload's stochastic outcomes; routing
                            # the whole card through ``play_branches`` without
                            # this choice would leave the mode marker
                            # unresolved and under-count lethal lines.
                            if self._choice_has_stochastic_effects(selected_choice):
                                branches = self.interpreter.play_branches(
                                    state,
                                    card.unique_id,
                                    mode=mode,
                                    target_uid=target_uid,
                                    choice=selected_choice,
                                )
                                selected_probability = 0.0
                                selected_path: List[str] = []
                                selected_gaps: set[str] = set()
                                best_branch_sub_prob = -1.0
                                for branch in branches:
                                    next_s = branch.state
                                    if branch.unsupported_ops:
                                        self._mark_result_gaps(next_s, branch.unsupported_ops)
                                        selected_gaps.update(branch.unsupported_ops)
                                    next_s.history.append(
                                        f"【随机效果】打出 {card.name} [{mode}] 选项{selected_choice} "
                                        f"(花费 {cost_to_pay} PP, 分支概率 {branch.probability*100:.2f}%)"
                                    )
                                    sub_prob, sub_path = self._search(next_s, depth + 1)
                                    selected_probability += branch.probability * sub_prob
                                    if sub_prob > 0 and sub_prob > best_branch_sub_prob:
                                        selected_path = sub_path
                                        best_branch_sub_prob = sub_prob
                                if selected_gaps and selected_probability > 0:
                                    selected_path = list(selected_path) + [
                                        f"[incomplete: stochastic:{gap}]" for gap in sorted(selected_gaps)
                                    ]
                                probability, path = selected_probability, selected_path
                            else:
                                probability, path = self._play_action(
                                    state, card, mode, cost_to_pay, target_uid, depth=depth, choice=selected_choice
                                )
                            if probability > choice_prob:
                                choice_prob, choice_path = probability, path
                    if choice_prob > best_prob:
                        best_prob, best_path = choice_prob, choice_path
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path
                    continue
                if self._card_has_stochastic_rule(card, mode):
                    branches = self.interpreter.play_branches(state, card.unique_id, mode=mode)
                    branch_prob = 0.0
                    branch_path: List[str] = []
                    best_branch_sub_prob = -1.0
                    branch_gaps: set[str] = set()
                    for branch in branches:
                        next_s = branch.state
                        if branch.unsupported_ops:
                            self._mark_result_gaps(next_s, branch.unsupported_ops)
                        mode_tag = f" [{mode}]" if mode != "normal" else ""
                        next_s.history.append(f"【随机效果】打出 {card.name}{mode_tag} (花费 {cost_to_pay} PP, 分支概率 {branch.probability*100:.2f}%)")
                        sub_prob, sub_path = self._search(next_s, depth + 1)
                        branch_prob += branch.probability * sub_prob
                        # An unsupported/hidden random outcome that can still
                        # reach lethal makes the aggregate result incomplete,
                        # even when another known branch supplied the sample
                        # best path.
                        if branch.unsupported_ops and sub_prob > 0:
                            branch_gaps.update(branch.unsupported_ops)
                        if sub_prob > 0 and sub_prob > best_branch_sub_prob:
                            branch_path = sub_path
                            best_branch_sub_prob = sub_prob
                    if branch_prob > best_prob:
                        best_prob, best_path = branch_prob, branch_path
                        if branch_gaps:
                            best_path = list(best_path) + [f"[incomplete: stochastic:{gap}]" for gap in sorted(branch_gaps)]
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path
                    continue
                # 4.1 Legacy random barrage.  Generic repeat/random rules are
                # handled by the Step 6 stochastic resolver below.
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
                            f"【随机弹幕】打出 {card.name} [{mode}] (花费 {cost_to_pay} PP) -> 命中主战者斩杀概率: {dp_prob*100:.2f}%"
                        ]
                        if best_prob >= 0.9999:
                            self.memo[key] = (1.0, best_path)
                            return 1.0, best_path
                    continue

                # 4.2 Legacy draw branch.  A drawn entity receives a fresh uid
                # so two identical cards remain distinct in the state key.
                if mode == "normal" and card.draw_count > 0 and state.total_deck_count > 0 and self.card_db:
                    draw_sum_prob = 0.0
                    sample_path = []
                    for draw_id, count in list(state.deck_distribution.items()):
                        if count <= 0 or draw_id not in self.card_db:
                            continue
                        p_draw = count / state.total_deck_count
                        drawn_card = self.card_db[draw_id]
                        next_s = state.clone()
                        next_s.hand.pop(h_idx)
                        if not self._pay_pp(next_s, cost_to_pay):
                            continue
                        next_s.play_count += 1
                        if self.interpreter._is_spell_card(card):
                            next_s.cemetery += 1
                        next_uid = max((c.unique_id for c in next_s.hand), default=0) + 1
                        next_s.hand.append(replace(drawn_card, unique_id=next_uid))
                        next_s.total_deck_count -= 1
                        next_s.deck_distribution[draw_id] -= 1
                        next_s.history.append(f"【抽牌】打出 {card.name} [{mode}] 抽到『{drawn_card.name}』(概率 {p_draw*100:.1f}%)")
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
                    continue

                # 4.3 Deterministic play for this one mode.
                target_uids = self._target_options(state, card, mode)
                for target_uid in target_uids:
                    prob, path = self._play_action(state, card, mode, cost_to_pay, target_uid, depth=depth)
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

    def _card_has_stochastic_rule(self, card: LethalHandCard, mode: str | None = None) -> bool:
        rule = self.interpreter.rules.get(card.card_id, {})
        selected_modes = rule.get("modes", ()) if isinstance(rule, dict) else ()
        if mode is not None:
            selected_modes = [m for m in selected_modes if isinstance(m, dict) and m.get("kind") == mode]
        def has_random(effects) -> bool:
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                op = effect.get("op")
                target = effect.get("target")
                if op in ("repeat", "random_choice", "random_target", "reanimate", "replicate_ability") or (isinstance(target, dict) and target.get("selection") == "random") or (op == "copy" and isinstance(effect.get("source"), dict) and effect["source"].get("selection") == "random"):
                    return True
                if op == "transform" and isinstance(effect.get("resource_selector"), dict):
                    return True
                if op == "spellboost" and isinstance(target, dict) and target.get("selection") == "random":
                    return True
                if op in ("draw",) and (effect.get("count", 0) or 0):
                    return True
                if op == "summon" and isinstance(effect.get("resource_selector"), dict):
                    return True
                if has_random(effect.get("effects", ())):
                    return True
                if has_random(effect.get("else_effects", ())):
                    return True
                if any(has_random(item.get("effects", ())) for item in effect.get("choices", ()) if isinstance(item, dict)):
                    return True
            return False
        return any(has_random(a.get("effects", ())) for m in selected_modes if isinstance(m, dict) for a in m.get("abilities", ()) if isinstance(a, dict))

    def _trigger_has_stochastic(self, card_id: int, trigger: str) -> bool:
        rule = self.interpreter.rules.get(card_id, {})
        def has_random(effects) -> bool:
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                if effect.get("op") in ("repeat", "random_choice", "random_target", "reanimate", "replicate_ability") or (effect.get("op") == "copy" and isinstance(effect.get("source"), dict) and effect["source"].get("selection") == "random"):
                    return True
                target = effect.get("target")
                if isinstance(target, dict) and target.get("selection") == "random":
                    return True
                if effect.get("op") == "transform" and isinstance(effect.get("resource_selector"), dict):
                    return True
                if effect.get("op") == "spellboost" and isinstance(target, dict) and target.get("selection") == "random":
                    return True
                if effect.get("op") == "draw" and (effect.get("count", 0) or 0):
                    return True
                if effect.get("op") == "summon" and isinstance(effect.get("resource_selector"), dict):
                    return True
                if has_random(effect.get("effects", ())) or has_random(effect.get("else_effects", ())):
                    return True
            return False
        return any(has_random(a.get("effects", ())) for m in rule.get("modes", ()) if isinstance(m, dict) for a in m.get("abilities", ()) if isinstance(a, dict) and a.get("trigger") == trigger)

    def _evolve_action(self, state: LethalState, follower: LethalFollower, *, super_evolve: bool, target_uid: object, depth: int) -> Tuple[float, List[str]]:
        if self._trigger_has_stochastic(follower.card_id, "on_super_evolve" if super_evolve else "on_evolve"):
            branches = self.interpreter.evolve_branches(state, follower.unique_id, super_evolve=super_evolve, target_uid=target_uid)
            total = 0.0
            selected_path: List[str] = []
            best_branch_probability = -1.0
            branch_gaps: set[str] = set()
            for branch in branches:
                next_state = branch.state
                self._mark_result_gaps(next_state, branch.unsupported_ops)
                new_follower = next((item for item in next_state.my_board if item.unique_id == follower.unique_id), follower)
                label = "超进化" if super_evolve else "普通进化"
                next_state.history.append(f"【{label}】{label} {follower.name} (Atk变为: {new_follower.atk}, 分支概率 {branch.probability*100:.2f}%)")
                probability, path = self._search(next_state, depth + 1)
                total += branch.probability * probability
                if branch.unsupported_ops and probability > 0:
                    branch_gaps.update(branch.unsupported_ops)
                if probability > 0 and probability > best_branch_probability:
                    selected_path = path
                    best_branch_probability = probability
            if branch_gaps:
                selected_path = list(selected_path) + [f"[incomplete: stochastic:{gap}]" for gap in sorted(branch_gaps)]
            return total, selected_path
        resolved = self.interpreter.evolve(state, follower.unique_id, super_evolve=super_evolve, target_uid=target_uid)
        next_state = resolved.state
        self._mark_result_gaps(next_state, resolved.unsupported_ops)
        new_follower = next((item for item in next_state.my_board if item.unique_id == follower.unique_id), follower)
        label = "超进化" if super_evolve else "普通进化"
        next_state.history.append(f"【{label}】{label} {follower.name} (Atk变为: {new_follower.atk})")
        return self._search(next_state, depth + 1)

    def _play_action(self, state: LethalState, card: LethalHandCard, mode: str, cost: int, target_uid: object, *, depth: int, choice: object = None) -> Tuple[float, List[str]]:
        resolved = self.interpreter.play(state, card.unique_id, mode=mode, target_uid=target_uid, choice=choice)
        next_state = resolved.state
        if resolved.unsupported_ops:
            self._mark_result_gaps(next_state, resolved.unsupported_ops)
        tag = f" [{mode}]" if mode != "normal" else ""
        choice_tag = f" 选项{choice}" if choice is not None else ""
        target_tag = f" -> 目标 {target_uid}" if target_uid is not None else ""
        next_state.history.append(f"【使用卡牌】打出 {card.name}{tag}{choice_tag}{target_tag} (花费 {cost} PP, 敌HP剩: {next_state.enemy_hp})")
        return self._search(next_state, depth + 1)

    def _card_has_choice_rule(self, card: LethalHandCard, mode: str) -> bool:
        rule = self.interpreter.rules.get(card.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, dict) and m.get("kind") == mode), None)
        def choices(effects):
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                if effect.get("op") == "mode_choice" and effect.get("choices"):
                    return True
                if choices(effect.get("effects", ())) or choices(effect.get("else_effects", ())):
                    return True
            return False
        return bool(selected and any(choices(a.get("effects", ())) for a in selected.get("abilities", ()) if isinstance(a, dict)))

    def _card_has_choice_rule_any(self, card: LethalHandCard) -> bool:
        rule = self.interpreter.rules.get(card.card_id, {})
        return any(self._card_has_choice_rule(card, str(mode.get("kind"))) for mode in rule.get("modes", ()) if isinstance(mode, dict))

    @staticmethod
    def _choice_has_stochastic_effects(choice: object) -> bool:
        """Return whether one concrete Mode payload needs branch expansion."""
        if not isinstance(choice, dict):
            return False

        def visit(effects: object) -> bool:
            if not isinstance(effects, (list, tuple)):
                return False
            for effect in effects:
                if not isinstance(effect, dict):
                    continue
                op = effect.get("op")
                target = effect.get("target")
                if op in {"random_choice", "random_target", "reanimate", "draw"}:
                    return True
                if op == "spellboost" and isinstance(target, dict) and target.get("selection") == "random":
                    return True
                if op == "transform" and isinstance(effect.get("resource_selector"), dict):
                    return True
                source = effect.get("source")
                if op == "copy" and isinstance(source, dict) and source.get("selection") == "random":
                    return True
                if op == "summon" and isinstance(effect.get("resource_selector"), dict):
                    return True
                if isinstance(target, dict) and target.get("selection") == "random":
                    return True
                if visit(effect.get("effects", ())) or visit(effect.get("else_effects", ())):
                    return True
                if any(visit(item.get("effects", ())) for item in effect.get("choices", ()) if isinstance(item, dict)):
                    return True
            return False

        return visit(choice.get("effects", ()))

    def _choice_options(self, card: LethalHandCard, mode: str) -> List[object]:
        rule = self.interpreter.rules.get(card.card_id, {})
        selected = next((m for m in rule.get("modes", ()) if isinstance(m, dict) and m.get("kind") == mode), None)
        def find(effects):
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                if effect.get("op") == "mode_choice":
                    return [item.get("label", i) for i, item in enumerate(effect.get("choices", ())) if isinstance(item, dict)]
                found = find(effect.get("effects", ()))
                if found:
                    return found
            return []
        if not selected:
            return []
        for ability in selected.get("abilities", ()):
            if isinstance(ability, dict):
                found = find(ability.get("effects", ()))
                if found:
                    return found
        return []

    @staticmethod
    def _mark_result_gaps(state: LethalState, gaps) -> None:
        for gap in gaps:
            state.history.append(f"[incomplete: {gap}]")

    def _target_options(self, state: LethalState, card: LethalHandCard, mode: str | bool) -> List[object]:
        """Return target branches for deterministic enemy-follower effects."""
        rule = self.interpreter.rules.get(card.card_id, {})
        kind = ("enhance" if mode else "normal") if isinstance(mode, bool) else str(mode)
        return self._rule_target_options(state, rule, kind, "on_play")

    def _rule_target_options(self, state: LethalState, rule: dict, kind: str, trigger: str) -> List[object]:
        modes = rule.get("modes", ()) if isinstance(rule, dict) else ()
        selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == kind), None)
        if selected is None and kind == "normal":
            selected = next((m for m in modes if isinstance(m, dict) and m.get("kind") == "normal"), None)

        def flatten(effects):
            for effect in effects if isinstance(effects, (list, tuple)) else ():
                if not isinstance(effect, dict):
                    continue
                yield effect
                yield from flatten(effect.get("effects", ()))
                yield from flatten(effect.get("else_effects", ()))
                for branch in effect.get("choices", ()):
                    if isinstance(branch, dict):
                        yield from flatten(branch.get("effects", ()))

        effects = [e for a in (selected or {}).get("abilities", ()) if isinstance(a, dict) and a.get("trigger") == trigger for e in flatten(a.get("effects", ()))]
        target_effects = [e for e in effects if isinstance(e, dict) and e.get("op") in ("damage", "destroy", "remove_abilities", "gain_status", "spellboost", "transform", "return_to_hand", "return_to_deck", "banish") and isinstance(e.get("target"), dict) and e["target"].get("scope") in ("enemy_follower", "ally_follower", "enemy_leader", "any", "hand")]
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

        copy_effect = next((e for e in effects if isinstance(e, dict) and e.get("op") == "copy" and isinstance(e.get("source"), dict) and e["source"].get("zone") == "field" and e["source"].get("selection") not in ("all", "random")), None)
        if copy_effect is not None:
            source = copy_effect["source"]
            side = source.get("side") or (source.get("filters", {}) if isinstance(source.get("filters"), dict) else {}).get("side")
            if side == "ally":
                board = state.my_board
            elif side == "enemy":
                board = state.enemy_board
            else:
                board = list(state.my_board) + list(state.enemy_board)
            filters = source.get("filters") if isinstance(source.get("filters"), dict) else {}
            ids = [f.unique_id for f in board if self.interpreter._matches_follower_filters(f, "ally" if f in state.my_board else "enemy", filters)]
            count = int(copy_effect.get("count", 1) or 1)
            return list(combinations(ids, count)) if count > 1 else ids

        target_effect = next((e for e in target_effects if e["target"].get("selection") not in ("all", "each", "random") and (e["target"].get("scope") in ("enemy_follower", "ally_follower", "any", "hand") or isinstance(e["target"].get("filters"), dict) and e["target"].get("filters", {}).get("zone") == "hand")), None)
        if target_effect is None:
            return [None]
        count = next((int(e.get("target", {}).get("count")) for e in effects if isinstance(e, dict) and isinstance(e.get("target"), dict) and isinstance(e.get("target", {}).get("count"), int) and e.get("target", {}).get("count") > 1), 1)
        scope = target_effect["target"].get("scope")
        target = target_effect["target"]
        filters = target.get("filters") if isinstance(target.get("filters"), dict) else {}
        if (isinstance(filters, dict) and filters.get("zone") in ("hand", "ally_hand")) or scope == "hand":
            ids = [
                c.unique_id
                for c in state.hand
                if self.interpreter._hand_indexes(state, target, c.unique_id, c.unique_id)
                and (target_effect.get("op") != "spellboost" or self.interpreter._card_has_trigger(c.card_id, str(filters.get("has_trigger", "on_spellboost")), c))
            ]
            return list(combinations(ids, count)) if count > 1 else ids
        if scope == "ally_follower":
            board = state.my_board
        elif scope == "any":
            board = list(state.my_board) + list(state.enemy_board)
        else:
            board = state.enemy_board
        ids = [f.unique_id for f in board if self.interpreter._matches_follower_filters(f, "ally" if f in state.my_board else "enemy", filters)]
        return list(combinations(ids, count)) if count > 1 else ids
