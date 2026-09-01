import json
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict, List, Optional

from lethal_models import (
    LethalState, LethalFollower, LethalHandCard, LethalResult, 
    create_hand_card_from_rule
)
from lethal_engine import LethalEngine


class LethalSimulatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Shadowverse WB · 斩杀计算模拟器")
        self.root.geometry("1150x760")
        self.root.minsize(1000, 680)

        self.uid_counter = 1000
        self.card_library: Dict[int, dict] = {}
        self.card_rules: Dict[int, dict] = {}
        self.hand_cards: List[LethalHandCard] = []
        self.my_board_followers: List[LethalFollower] = []
        self.enemy_board_followers: List[LethalFollower] = []

        self._load_cards_database()
        self._setup_ui()
        self._refresh_card_list()

    def _next_uid(self) -> int:
        self.uid_counter += 1
        return self.uid_counter

    def _load_cards_database(self):
        for filename in ("card_rules.json", "card_rules_3.json", "cards.json"):
            if os.path.exists(filename):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    if isinstance(data, dict):
                        self.card_library = {int(k): v for k, v in data.items() if k.isdigit()}
                    elif isinstance(data, list):
                        self.card_library = {
                            int(item["card_id"]): item
                            for item in data if "card_id" in item
                        }
                    self.card_rules = self.card_library
                    print(f"[+] 成功加载卡牌数据库: {filename} (共 {len(self.card_library)} 张卡)")
                    return
                except Exception as e:
                    print(f"[-] 加载 {filename} 失败: {e}")

    def _setup_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Top Frame: 全局状态设置
        top_frame = ttk.LabelFrame(self.root, text=" 🎮 全局对局状态设置 (Global State) ", padding=10)
        top_frame.pack(fill="x", padx=12, pady=6)

        row1 = ttk.Frame(top_frame)
        row1.pack(fill="x", pady=2)

        ttk.Label(row1, text="敌方HP:").pack(side="left", padx=(0, 2))
        self.ent_enemy_hp = ttk.Entry(row1, width=5)
        self.ent_enemy_hp.insert(0, "5")
        self.ent_enemy_hp.pack(side="left", padx=(0, 10))

        ttk.Label(row1, text="当前可用PP:").pack(side="left", padx=(0, 2))
        self.ent_pp = ttk.Entry(row1, width=5)
        self.ent_pp.insert(0, "10")
        self.ent_pp.pack(side="left", padx=(0, 10))

        ttk.Label(row1, text="最大PP:").pack(side="left", padx=(0, 2))
        self.ent_max_pp = ttk.Entry(row1, width=5)
        self.ent_max_pp.insert(0, "10")
        self.ent_max_pp.pack(side="left", padx=(0, 10))

        ttk.Label(row1, text="EP:").pack(side="left", padx=(0, 2))
        self.ent_ep = ttk.Entry(row1, width=4)
        self.ent_ep.insert(0, "1")
        self.ent_ep.pack(side="left", padx=(0, 10))

        ttk.Label(row1, text="SEP(超进化):").pack(side="left", padx=(0, 2))
        self.ent_sep = ttk.Entry(row1, width=4)
        self.ent_sep.insert(0, "1")
        self.ent_sep.pack(side="left", padx=(0, 10))

        self.var_awakening = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="觉醒状态", variable=self.var_awakening).pack(side="left", padx=10)

        row2 = ttk.Frame(top_frame)
        row2.pack(fill="x", pady=4)

        ttk.Label(row2, text="协作数(Rally):").pack(side="left", padx=(0, 2))
        self.ent_rally = ttk.Entry(row2, width=5)
        self.ent_rally.insert(0, "0")
        self.ent_rally.pack(side="left", padx=(0, 10))

        ttk.Label(row2, text="墓场数(Cemetery):").pack(side="left", padx=(0, 2))
        self.ent_cemetery = ttk.Entry(row2, width=5)
        self.ent_cemetery.insert(0, "10")
        self.ent_cemetery.pack(side="left", padx=(0, 10))

        ttk.Label(row2, text="纹章ID列表(英文逗号隔开):").pack(side="left", padx=(0, 2))
        self.ent_crests = ttk.Entry(row2, width=28)
        self.ent_crests.pack(side="left", padx=(0, 10))

        # Main Split Frame
        main_pane = ttk.PanedWindow(self.root, orient="horizontal")
        main_pane.pack(fill="both", expand=True, padx=12, pady=6)

        # Left: 卡牌库
        left_frame = ttk.LabelFrame(main_pane, text=" 📚 卡牌图鉴 (Card Library) ", padding=8)
        main_pane.add(left_frame, weight=3)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(search_frame, text="搜索:").pack(side="left")
        self.ent_search = ttk.Entry(search_frame)
        self.ent_search.pack(side="left", fill="x", expand=True, padx=4)
        self.ent_search.bind("<KeyRelease>", lambda e: self._refresh_card_list())

        self.card_listbox = tk.Listbox(left_frame, font=("Segoe UI", 9), selectmode="single")
        self.card_listbox.pack(fill="both", expand=True, pady=4)

        btn_grid = ttk.Frame(left_frame)
        btn_grid.pack(fill="x", pady=4)

        ttk.Button(btn_grid, text="➕ 添至我方手牌", command=self._add_to_hand).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btn_grid, text="⚔️ 添至我方战场", command=self._add_to_my_board).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btn_grid, text="🛡️ 添至敌方战场(默认守护)", command=self._add_to_enemy_board).grid(row=1, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        btn_grid.columnconfigure(0, weight=1)
        btn_grid.columnconfigure(1, weight=1)

        # Middle: 盘面
        mid_frame = ttk.LabelFrame(main_pane, text=" 🎴 当前盘面设置 (Board State) ", padding=8)
        main_pane.add(mid_frame, weight=4)

        ttk.Label(mid_frame, text="【我方手牌】(双击删除):", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.list_hand = tk.Listbox(mid_frame, height=5, font=("Segoe UI", 9))
        self.list_hand.pack(fill="x", pady=(0, 4))
        self.list_hand.bind("<Double-Button-1>", lambda e: self._delete_selected(self.list_hand, self.hand_cards))

        ttk.Label(mid_frame, text="【我方战场 (最多5格)】(双击删除):", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.list_my_board = tk.Listbox(mid_frame, height=5, font=("Segoe UI", 9))
        self.list_my_board.pack(fill="x", pady=(0, 4))
        self.list_my_board.bind("<Double-Button-1>", lambda e: self._delete_selected(self.list_my_board, self.my_board_followers))

        ttk.Label(mid_frame, text="【敌方战场 (包含守护/减伤)】(双击删除):", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.list_enemy_board = tk.Listbox(mid_frame, height=5, font=("Segoe UI", 9))
        self.list_enemy_board.pack(fill="x", pady=(0, 4))
        self.list_enemy_board.bind("<Double-Button-1>", lambda e: self._delete_selected(self.list_enemy_board, self.enemy_board_followers))

        clear_btn = ttk.Button(mid_frame, text="🗑️ 清空所有场面与手牌", command=self._clear_all)
        clear_btn.pack(anchor="e", pady=4)

        # Right: 计算输出
        right_frame = ttk.LabelFrame(main_pane, text=" ⚡ 斩杀判定与输出 (Solver Output) ", padding=8)
        main_pane.add(right_frame, weight=4)

        self.btn_calc = tk.Button(
            right_frame, 
            text="🚀 开始斩杀求解 (Solve Lethal)", 
            font=("Segoe UI", 11, "bold"), 
            bg="#2E7DA6", 
            fg="white", 
            activebackground="#205875",
            command=self._run_solver
        )
        self.btn_calc.pack(fill="x", pady=(0, 6), ipady=6)

        self.lbl_status = tk.Label(right_frame, text="状态: 等待求解", font=("Segoe UI", 12, "bold"), bg="#E4E3DD", fg="#1A1B1C", pady=4)
        self.lbl_status.pack(fill="x", pady=(0, 6))

        ttk.Label(right_frame, text="【最优动作序列】:").pack(anchor="w")
        self.txt_output = tk.Text(right_frame, font=("Consolas", 9), wrap="word")
        self.txt_output.pack(fill="both", expand=True)

    def _refresh_card_list(self):
        query = self.ent_search.get().strip().lower()
        self.card_listbox.delete(0, tk.END)
        self.filtered_ids = []

        for cid, info in sorted(self.card_library.items(), key=lambda x: (x[1].get("cost", 0), x[0])):
            name = info.get("name", str(cid))
            cost = info.get("cost", 0)
            t_str = "随从" if info.get("type", 1) == 1 else "法术/护符"
            display_text = f"[{cost}费] {name} ({t_str}) - ID:{cid}"
            
            if not query or query in name.lower() or query in str(cid):
                self.card_listbox.insert(tk.END, display_text)
                self.filtered_ids.append(cid)

    def _get_selected_card_id(self) -> Optional[int]:
        sel = self.card_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请先在左侧图鉴中选择一张卡牌！")
            return None
        return self.filtered_ids[sel[0]]

    def _add_to_hand(self):
        cid = self._get_selected_card_id()
        if cid is None: return
        info = self.card_library.get(cid, {})
        card = create_hand_card_from_rule(cid, info, self._next_uid())
        self.hand_cards.append(card)
        self.list_hand.insert(tk.END, f"{card.name} (Cost: {card.cost}, Atk/Hp: {card.atk}/{card.life})")

    def _add_to_my_board(self):
        if len(self.my_board_followers) >= 5:
            messagebox.showwarning("限制", "我方战场最多容纳 5 个随从！")
            return
        cid = self._get_selected_card_id()
        if cid is None: return
        info = self.card_library.get(cid, {})
        static = info.get("static", {})
        has_storm = static.get("has_storm", False)
        has_rush = static.get("has_rush", False)

        follower = LethalFollower(
            unique_id=self._next_uid(),
            card_id=cid,
            name=info.get("name", str(cid)),
            atk=info.get("atk", 2),
            hp=info.get("life", 2),
            has_storm=has_storm,
            has_rush=has_rush or has_storm,
            is_ward=info.get("is_ward", False),
            is_evolved=False,
            can_attack_leader=True,
            can_attack_field=True,
            attacks_left=1,
            damage_cap=None
        )
        self.my_board_followers.append(follower)
        self.list_my_board.insert(tk.END, f"{follower.name} [{follower.atk}/{follower.hp}] (疾驰:{has_storm})")

    def _add_to_enemy_board(self):
        if len(self.enemy_board_followers) >= 5:
            messagebox.showwarning("限制", "敌方战场最多容纳 5 个随从！")
            return
        cid = self._get_selected_card_id()
        if cid is None: return
        info = self.card_library.get(cid, {})
        follower = LethalFollower(
            unique_id=self._next_uid(),
            card_id=cid,
            name=info.get("name", str(cid)),
            atk=info.get("atk", 3),
            hp=info.get("life", 4),
            has_storm=False,
            has_rush=False,
            is_ward=True,
            is_evolved=False,
            can_attack_leader=False,
            can_attack_field=False,
            attacks_left=0,
            damage_cap=info.get("damage_cap")
        )
        self.enemy_board_followers.append(follower)
        self.list_enemy_board.insert(tk.END, f"🛡️ {follower.name} [{follower.atk}/{follower.hp}] (守护:True)")

    def _delete_selected(self, listbox: tk.Listbox, data_list: list):
        sel = listbox.curselection()
        if sel:
            idx = sel[0]
            listbox.delete(idx)
            data_list.pop(idx)

    def _clear_all(self):
        self.hand_cards.clear()
        self.my_board_followers.clear()
        self.enemy_board_followers.clear()
        self.list_hand.delete(0, tk.END)
        self.list_my_board.delete(0, tk.END)
        self.list_enemy_board.delete(0, tk.END)
        self.txt_output.delete("1.0", tk.END)
        self.lbl_status.config(text="状态: 盘面已清空", bg="#E4E3DD", fg="#1A1B1C")

    def _run_solver(self):
        try:
            enemy_hp = int(self.ent_enemy_hp.get())
            pp = int(self.ent_pp.get())
            max_pp = int(self.ent_max_pp.get())
            ep = int(self.ent_ep.get())
            sep = int(self.ent_sep.get())
            rally = int(self.ent_rally.get())
            cemetery = int(self.ent_cemetery.get())
            is_awakening = self.var_awakening.get()
            
            crests_raw = self.ent_crests.get().strip()
            active_crests = [int(x.strip()) for x in crests_raw.split(",") if x.strip().isdigit()]
        except ValueError:
            messagebox.showerror("输入错误", "请确保全局数值输入均为有效整数！")
            return

        state = LethalState(
            enemy_hp=enemy_hp,
            pp=pp,
            max_pp=max_pp,
            ep=ep,
            sep=sep,
            rally=rally,
            cemetery=cemetery,
            is_awakening=is_awakening,
            my_board=self.my_board_followers,
            enemy_board=self.enemy_board_followers,
            hand=self.hand_cards,
            active_crests=active_crests
        )

        engine = LethalEngine(rules=self.card_rules)
        result: LethalResult = engine.solve(state)

        self.txt_output.delete("1.0", tk.END)

        if result.status == "CONFIRMED":
            self.lbl_status.config(text=f"🔥 确认斩杀 (CONFIRMED 100%)", bg="#2E9E44", fg="white")
            self.txt_output.insert(tk.END, ">>> 发现 100% 确定性斩杀路线 <<<\n\n")
            for idx, step in enumerate(result.sequence, start=1):
                self.txt_output.insert(tk.END, f"{idx}. {step}\n")
        elif result.status == "INCOMPLETE":
            self.lbl_status.config(text="⚠ 规则不完整，存在潜在斩杀", bg="#B36B00", fg="white")
            self.txt_output.insert(tk.END, ">>> 找到依赖未完整支持规则的路线，不能确认为斩杀 <<<\n\n")
            for idx, step in enumerate(result.sequence, start=1):
                self.txt_output.insert(tk.END, f"{idx}. {step}\n")
        elif result.status == "PROBABILISTIC":
            self.lbl_status.config(text=f"🎲 概率斩杀 (胜率: {result.probability*100:.2f}%)", bg="#D99000", fg="white")
            self.txt_output.insert(tk.END, f">>> 概率斩杀线路 (成功率: {result.probability*100:.2f}%) <<<\n\n")
            for idx, step in enumerate(result.sequence, start=1):
                self.txt_output.insert(tk.END, f"{idx}. {step}\n")
        else:
            self.lbl_status.config(text="❌ 未发现斩杀 (NO LETHAL)", bg="#EA6668", fg="white")
            self.txt_output.insert(tk.END, "在当前手牌、费用、场地与守护墙约束下，未搜索到任何可斩杀路线。\n")


if __name__ == "__main__":
    root = tk.Tk()
    app = LethalSimulatorApp(root)
    root.mainloop()
