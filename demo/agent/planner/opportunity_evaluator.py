"""机会评估器：等待期权价值 + 跨动作比较 (take vs wait vs reposition)。
v2: 修复 wait_cost 按时长缩放；使用实际金额比较替代无量纲分数比较。"""

from __future__ import annotations

import logging
from typing import Any

from agent.memory.area_memory import AreaMemory
from agent.utils import geo_utils, time_utils


class OpportunityEvaluator:
    """评估'接这个单' vs '等等' vs '空驶去热区'的期望价值。"""

    def __init__(
        self,
        area_memory: AreaMemory,
        min_score_to_wait: float = 0.20,
        wait_cost_per_minute: float = 1.5,
        reposition_gain_threshold: float = 1.3,
        max_wait_for_better: int = 180,
        cost_per_km: float = 1.5,
        time_value_per_minute: float = 1.0,
    ) -> None:
        self._area_memory = area_memory
        self._min_score_to_wait = min_score_to_wait
        self._wait_cost_per_minute = wait_cost_per_minute
        self._reposition_gain_threshold = reposition_gain_threshold
        self._max_wait_for_better = max_wait_for_better
        self._cost_per_km = cost_per_km
        self._time_value = time_value_per_minute
        self._logger = logging.getLogger("agent.opportunity")

    def evaluate_take_vs_wait(
        self,
        best_score: float,
        best_net_profit: float,
        area_lat: float,
        area_lng: float,
        sim_min: int,
    ) -> dict[str, Any]:
        """评估'接当前最优' vs '等待更好货源'。
        返回 {"action": "take"|"wait", "duration": int|None, "rationale": str}
        """
        result: dict[str, Any] = {"action": "take", "duration": None, "rationale": ""}

        # 高质量单直接接，不等待
        if best_score >= self._min_score_to_wait * 3:
            result["rationale"] = "high_score"
            return result

        # 估算等到更好单的概率
        percentile = self._area_memory.get_score_percentile(area_lat, area_lng, best_score)
        density = self._area_memory.get_density(area_lat, area_lng)

        # p_better ≈ 1 - percentile，密度低时打折扣
        p_better = max(0.0, 1.0 - percentile)
        if density < 3:
            p_better *= 0.3

        if p_better < 0.15:
            result["rationale"] = "unlikely_better"
            return result

        # 等待后可能获得的更好单的期望增量（以实际金额估算）
        expected_net_profit_current = best_net_profit
        expected_net_target = self._area_memory.get_expected_net_profit(area_lat, area_lng)
        expected_gain_yuan = max(0.0, (expected_net_target - expected_net_profit_current) * p_better)

        # 等待时长：百分位越低（当前单越差），愿意等越久
        wait_duration = min(
            self._max_wait_for_better,
            int(30 + 60 * (1.0 - percentile)),
        )
        wait_duration = max(30, wait_duration)

        # 等待成本 = 每分钟机会成本 × 等待时长
        wait_cost = self._wait_cost_per_minute * wait_duration

        net_benefit = expected_gain_yuan - wait_cost
        if net_benefit > 0 and p_better > 0.2:
            result["action"] = "wait"
            result["duration"] = wait_duration
            result["rationale"] = (
                f"p_better={p_better:.2f} expected_gain={expected_gain_yuan:.0f}yuan "
                f"wait_cost={wait_cost:.0f}yuan net={net_benefit:.0f} "
                f"percentile={percentile:.2f} density={density:.0f}"
            )
            self._logger.info(
                "[OPPORTUNITY] wait recommended: %s",
                result["rationale"],
            )

        return result

    def evaluate_take_vs_reposition(
        self,
        best_score: float,
        best_net_profit: float,
        current_lat: float,
        current_lng: float,
        sim_min: int,
        simulation_horizon_minutes: int,
    ) -> dict[str, Any]:
        """评估'接当前最优' vs '空驶去热区'。
        返回 {"action": "take"|"reposition", "target": (lat,lng)|None, "rationale": str}
        """
        result: dict[str, Any] = {
            "action": "take",
            "target": None,
            "rationale": "",
        }

        # 分数够高不迁移
        if best_score > 0.30:
            result["rationale"] = "good_enough"
            return result

        current_hour = int(time_utils.sim_min_to_hour_of_day(sim_min)) % 24
        current_heat = self._area_memory.get_heat(current_lat, current_lng)
        hot_zones = self._area_memory.suggest_reposition(
            current_lat, current_lng,
            max_distance_km=200.0,
            top_n=3,
            target_hour=current_hour,
        )

        if not hot_zones:
            result["rationale"] = "no_hot_zones"
            return result

        best_zone = hot_zones[0]
        target_lat, target_lng, target_heat = best_zone

        if target_heat <= current_heat * self._reposition_gain_threshold:
            result["rationale"] = "not_worth_moving"
            return result

        dist = geo_utils.haversine_km(current_lat, current_lng, target_lat, target_lng)
        if dist < 15:
            result["rationale"] = "too_close"
            return result

        # 迁移成本：距离成本 + 时间机会成本
        dist_hours = dist / 60.0
        reposition_cost = dist * self._cost_per_km + dist_hours * self._time_value * 60.0

        # 目标区域期望收益
        avg_price_target = self._area_memory.get_avg_price(target_lat, target_lng)
        avg_deadhead_target = self._area_memory.get_avg_pickup_distance(target_lat, target_lng)
        expected_profit_target = max(0.0, avg_price_target - avg_deadhead_target * self._cost_per_km)

        # 剩余时间能接多少单的估算
        remaining_days = max(0.1, simulation_horizon_minutes - sim_min) / 1440.0
        future_orders = max(0.3, min(3.0, remaining_days * 0.3))
        reposition_gain = (expected_profit_target - best_net_profit) * future_orders

        if reposition_gain > reposition_cost * self._reposition_gain_threshold:
            result["action"] = "reposition"
            result["target"] = (target_lat, target_lng)
            result["rationale"] = (
                f"reposition_gain={reposition_gain:.0f}yuan > "
                f"cost={reposition_cost:.0f}yuan*{self._reposition_gain_threshold} "
                f"from_heat={current_heat:.2f} to_heat={target_heat:.2f} dist={dist:.0f}km"
            )
            self._logger.info(
                "[OPPORTUNITY] reposition recommended: %s",
                result["rationale"],
            )

        return result
