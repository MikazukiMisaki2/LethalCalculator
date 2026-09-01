from functools import lru_cache
from typing import List, Tuple

class StochasticDamageCalculator:
    @staticmethod
    def calculate_lethal_prob(enemy_face_hp: int, enemy_follower_hps: List[int], total_hits: int = 5, damage_per_hit: int = 2) -> float:
        # 换算为主战者和各随从所需承受的命中次数
        face_hits_needed = (enemy_face_hp + damage_per_hit - 1) // damage_per_hit
        follower_hits_needed = tuple(
            (hp + damage_per_hit - 1) // damage_per_hit 
            for hp in enemy_follower_hps if hp > 0
        )

        @lru_cache(maxsize=None)
        def dp(rem_hits: int, face_req: int, followers_req: Tuple[int, ...]) -> float:
            # 1. 成功击杀主战者
            if face_req <= 0:
                return 1.0
            # 2. 弹幕耗尽但未击杀
            if rem_hits <= 0:
                return 0.0

            # 统计当前有效目标（1个主战者 + 存活随从数）
            alive_followers = [req for req in followers_req if req > 0]
            num_targets = 1 + len(alive_followers)
            prob_per_target = 1.0 / num_targets

            # 情况 A: 命中主战者
            total_prob = prob_per_target * dp(rem_hits - 1, face_req - 1, followers_req)

            # 情况 B: 命中某个存活随从
            for idx, req in enumerate(followers_req):
                if req > 0:
                    next_followers = list(followers_req)
                    next_followers[idx] -= 1
                    # 排序以最大化 LRU Cache 命中率
                    next_followers_tuple = tuple(sorted(next_followers, reverse=True))
                    total_prob += prob_per_target * dp(rem_hits - 1, face_req, next_followers_tuple)

            return total_prob

        # 初始状态排序
        init_followers = tuple(sorted(follower_hits_needed, reverse=True))
        return dp(total_hits, face_hits_needed, init_followers)