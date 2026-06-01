"""未来收益预估器：基于时间感知区域剖面，估算送达后的期望收益。
v2: 改进公式，使输出更加合理并可直接用于打分。"""

from __future__ import annotations

from typing import Any

from agent.memory.area_memory import AreaMemory
from agent.utils.time_utils import sim_min_to_hour_of_day


class FutureIncomeEstimator:
    """估算送到目的地后能接到什么单。"""

    def __init__(
        self,
        area_memory: AreaMemory,
        cost_per_km: float = 1.5,
        time_value_per_minute: float = 1.0,
        cold_zone_heat_threshold: float = 0.10,
        cold_zone_penalty: float = 500.0,
        future_value_discount: float = 0.5,
    ) -> None:
        self._area_memory = area_memory
        self._cost_per_km = cost_per_km
        self._time_value_per_minute = time_value_per_minute
        self._cold_threshold = cold_zone_heat_threshold
        self._cold_penalty = cold_zone_penalty
        self._discount = future_value_discount

    def estimate_arrival_value(
        self,
        dest_lat: float,
        dest_lng: float,
        arrival_sim_min: int,
        simulation_horizon_minutes: int,
    ) -> float:
        """估算送达后从该位置能接到的下一单期望净收益 (元)。"""
        arrival_hour = int(sim_min_to_hour_of_day(arrival_sim_min)) % 24
        remaining_minutes = max(0, simulation_horizon_minutes - arrival_sim_min)
        remaining_days = remaining_minutes / 1440.0
        if remaining_days < 0.1:
            return 0.0

        # 时段热度优先，无时段数据用全天热度
        heat_now = self._area_memory.get_heat_at_hour(dest_lat, dest_lng, arrival_hour)
        heat_all = self._area_memory.get_heat(dest_lat, dest_lng)
        heat = heat_now if heat_now > 0 else heat_all

        # 时段价格优先
        avg_price = self._area_memory.get_avg_price_at_hour(dest_lat, dest_lng, arrival_hour)
        if avg_price <= 0:
            avg_price = self._area_memory.get_avg_price(dest_lat, dest_lng)
        if avg_price <= 0:
            avg_price = 800.0

        avg_deadhead = self._area_memory.get_avg_pickup_distance(dest_lat, dest_lng)

        # 期望净收益 = 平均运费 - 平均空驶成本
        expected_net = avg_price - avg_deadhead * self._cost_per_km
        expected_net = max(0.0, expected_net)

        # 时间因子：剩余时间越短，机会越少
        time_factor = min(1.0, remaining_days / 7.0)

        # 热度加权：热度越高的地区，拿到好单的概率越大
        value = expected_net * time_factor * (0.3 + 0.7 * heat)

        # 冷区惩罚：如果当前区域很冷且剩余时间还长，衰减未来价值
        if heat_all < self._cold_threshold and remaining_days > 5:
            value -= self._cold_penalty * time_factor

        return max(0.0, value)

    def estimate_total_value(
        self,
        net_profit: float,
        dest_lat: float,
        dest_lng: float,
        current_sim_min: int,
        total_minutes: int,
        simulation_horizon_minutes: int,
    ) -> float:
        """计算 current_net_profit + discounted * future_value 的综合值。
        返回值可直接用于排序比较（单位：元）。"""
        arrival_min = current_sim_min + total_minutes
        future_val = self.estimate_arrival_value(
            dest_lat, dest_lng, arrival_min, simulation_horizon_minutes
        )
        return net_profit + self._discount * future_val

    def rank_by_total_value(
        self,
        cargo_scores: list[Any],
        current_sim_min: int,
        simulation_horizon_minutes: int,
    ) -> list[tuple[Any, float]]:
        """对候选货源按 current_net + discounted * future_value 重新排序。"""
        results: list[tuple[Any, float]] = []
        for cs in cargo_scores:
            dest_lat = cs.breakdown.get("dest_lat", 0)
            dest_lng = cs.breakdown.get("dest_lng", 0)
            arrival_min = current_sim_min + int(cs.total_minutes)
            future_val = self.estimate_arrival_value(
                dest_lat, dest_lng, arrival_min, simulation_horizon_minutes
            )
            total = cs.net_profit + self._discount * future_val
            results.append((cs, total))
        results.sort(key=lambda x: x[1], reverse=True)
        return results
