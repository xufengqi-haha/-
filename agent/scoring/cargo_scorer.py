"""货源多维度综合评分引擎。
v2: 可配置的 distance_score 权重；接受外部注入的未来价值分量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.memory.area_memory import AreaMemory
from agent.utils.geo_utils import haversine_km


@dataclass
class ScorerConfig:
    w_profit: float = 0.25
    w_efficiency: float = 0.22
    w_dest_heat: float = 0.20
    w_pref_penalty: float = 0.12
    w_time_risk: float = 0.08
    w_future_value: float = 0.08
    w_distance_bonus: float = 0.05
    max_pickup_km: float = 200.0
    cost_per_km: float = 1.5
    speed_km_per_hour: float = 60.0
    time_value_per_minute: float = 1.0


@dataclass
class CargoScore:
    cargo_id: str
    total_score: float = 0.0
    profit_score: float = 0.0
    efficiency_score: float = 0.0
    dest_heat_score: float = 0.0
    pref_penalty_score: float = 0.0
    time_risk_score: float = 0.0
    future_value_score: float = 0.0
    distance_score: float = 0.0
    net_profit: float = 0.0
    total_minutes: float = 0.0
    dest_lat: float = 0.0
    dest_lng: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


class CargoScorer:
    def __init__(self, config: ScorerConfig | None = None) -> None:
        self._cfg = config or ScorerConfig()

    def score(
        self,
        cargo: dict[str, Any],
        pickup_distance_km: float,
        sim_progress_minutes: int,
        area_memory: AreaMemory | None = None,
        preference_penalty: float = 0.0,
        simulation_horizon_minutes: int | None = None,
        future_value: float = 0.0,
        gamma: float = 1.0,
    ) -> CargoScore:
        price = float(cargo.get("price", 0) or 0)
        cost_time = float(cargo.get("cost_time_minutes", 0) or 0)
        start = cargo.get("start", {})
        end = cargo.get("end", {})

        pickup_cost = pickup_distance_km * self._cfg.cost_per_km
        haul_distance = haversine_km(
            float(start.get("lat", 0)), float(start.get("lng", 0)),
            float(end.get("lat", 0)), float(end.get("lng", 0)),
        )
        haul_cost = haul_distance * self._cfg.cost_per_km

        pickup_minutes = self._distance_to_minutes(pickup_distance_km)

        # 装货窗等待成本
        load_wait_minutes = 0.0
        load_time = cargo.get("load_time")
        if isinstance(load_time, list) and len(load_time) == 2:
            try:
                from agent.utils.time_utils import SIMULATION_EPOCH
                from datetime import datetime
                start_str = str(load_time[0]).strip()
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                load_start_min = int((start_dt - SIMULATION_EPOCH).total_seconds() // 60)
                arrival_at_load = sim_progress_minutes + pickup_minutes
                if arrival_at_load < load_start_min:
                    load_wait_minutes = float(load_start_min - arrival_at_load)
            except (ValueError, KeyError):
                pass

        total_minutes = pickup_minutes + cost_time + load_wait_minutes
        load_wait_cost = load_wait_minutes * self._cfg.time_value_per_minute
        net_profit = price - pickup_cost - haul_cost - load_wait_cost
        profit_per_hour = (net_profit / total_minutes * 60.0) if total_minutes > 0 else 0.0

        profit_score = self._normalize(net_profit, -500.0, 3000.0)
        efficiency_score = self._normalize(profit_per_hour, -50.0, 500.0)

        dest_lat = float(end.get("lat", 0))
        dest_lng = float(end.get("lng", 0))
        dest_heat_score = area_memory.get_heat(dest_lat, dest_lng) if area_memory else 0.5

        start_lat = float(start.get("lat", 0))
        start_lng = float(start.get("lng", 0))
        start_heat_score = area_memory.get_heat(start_lat, start_lng) if area_memory else 0.5

        combined_heat_score = 0.4 * start_heat_score + 0.6 * dest_heat_score

        # ★ V2 供需水位线：容量/运力比决定热度溢价或折价
        arrival_hour = int((sim_progress_minutes + total_minutes) // 60) % 24
        shadow_count = area_memory.get_shadow_count(dest_lat, dest_lng, arrival_hour) if area_memory else 0.0
        capacity = area_memory.get_grid_capacity(dest_lat, dest_lng, arrival_hour) if area_memory else 2.0
        supply_demand_ratio = capacity / (shadow_count + 1.0)
        combined_heat_score *= min(1.0, supply_demand_ratio)

        # ★ 经济账公式：有效罚分 = 原始罚分 × γ（动态膨胀系数）
        effective_penalty = preference_penalty * gamma
        pref_penalty_score = max(0.0, 1.0 - effective_penalty / 5000.0)

        time_risk_score = self._eval_time_risk(
            sim_progress_minutes, pickup_minutes, cargo, simulation_horizon_minutes
        )

        distance_score = self._eval_distance_reasonability(haul_distance)

        # 未来价值归一化（future_value 大约 0~2000 元 → 0~1）
        future_value_score = self._normalize(future_value, 0.0, 2000.0)

        total = (
            self._cfg.w_profit * profit_score
            + self._cfg.w_efficiency * efficiency_score
            + self._cfg.w_dest_heat * combined_heat_score
            - self._cfg.w_pref_penalty * (1.0 - pref_penalty_score)
            - self._cfg.w_time_risk * time_risk_score
            + self._cfg.w_future_value * future_value_score
            + self._cfg.w_distance_bonus * distance_score
        )

        return CargoScore(
            cargo_id=str(cargo.get("cargo_id", "")),
            total_score=round(total, 4),
            profit_score=round(profit_score, 4),
            efficiency_score=round(efficiency_score, 4),
            dest_heat_score=round(combined_heat_score, 4),
            pref_penalty_score=round(pref_penalty_score, 4),
            time_risk_score=round(time_risk_score, 4),
            future_value_score=round(future_value_score, 4),
            distance_score=round(distance_score, 4),
            net_profit=round(net_profit, 2),
            total_minutes=round(total_minutes, 1),
            dest_lat=dest_lat,
            dest_lng=dest_lng,
            breakdown={
                "price": price,
                "pickup_cost": round(pickup_cost, 2),
                "haul_cost": round(haul_cost, 2),
                "net_profit": round(net_profit, 2),
                "profit_per_hour": round(profit_per_hour, 1),
                "pickup_km": round(pickup_distance_km, 2),
                "haul_km": round(haul_distance, 2),
                "total_minutes": round(total_minutes, 1),
                "start_heat": round(start_heat_score, 4),
                "dest_heat": round(dest_heat_score, 4),
                "dest_lat": dest_lat,
                "dest_lng": dest_lng,
                "load_wait_minutes": round(load_wait_minutes, 1),
                "distance_score": round(distance_score, 4),
                "future_value": round(future_value, 1),
            },
        )

    def _normalize(self, value: float, low: float, high: float) -> float:
        return max(0.0, min(1.0, (value - low) / (high - low + 1e-9)))

    def _distance_to_minutes(self, distance_km: float) -> float:
        if distance_km <= 0:
            return 0.0
        import math
        return max(1, math.ceil((distance_km / self._cfg.speed_km_per_hour) * 60.0))

    def _eval_distance_reasonability(self, haul_distance: float) -> float:
        if haul_distance < 50:
            return 0.3
        elif haul_distance > 800:
            return 0.4
        elif 200 <= haul_distance <= 500:
            return 1.0
        else:
            return 0.7

    def _eval_time_risk(
        self,
        sim_min: int,
        pickup_minutes: float,
        cargo: dict[str, Any],
        horizon_minutes: int | None,
    ) -> float:
        risk = 0.0
        load_time = cargo.get("load_time")
        if isinstance(load_time, list) and len(load_time) == 2:
            try:
                from agent.utils.time_utils import SIMULATION_EPOCH
                from datetime import datetime
                end_str = str(load_time[1]).strip()
                end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
                window_end_min = int((end_dt - SIMULATION_EPOCH).total_seconds() // 60)
                arrival_min = sim_min + pickup_minutes

                if arrival_min > window_end_min:
                    risk = 1.0
                elif window_end_min - arrival_min < 30:
                    risk = 0.5
                elif window_end_min - arrival_min < 120:
                    risk = 0.2
            except (ValueError, KeyError):
                pass

        if horizon_minutes is not None:
            total_minutes = pickup_minutes + float(cargo.get("cost_time_minutes", 0) or 0)
            finish_min = sim_min + total_minutes
            if finish_min > horizon_minutes:
                risk = max(risk, 1.0)
            elif horizon_minutes - finish_min < 120:
                risk = max(risk, 0.3)
            elif horizon_minutes - finish_min < 360:
                risk = max(risk, 0.1)

        return min(risk, 1.0)
