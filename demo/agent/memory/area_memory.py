"""区域热度记忆：按地理网格统计货源密度与平均收益，支撑长期规划与迁移策略。
v3: 修复 score_percentile 量纲错误，新增加权历史分数估算。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agent.utils.geo_utils import grid_key, haversine_km


def _new_grid() -> dict[str, Any]:
    return {
        "total_price": 0.0,
        "count": 0.0,
        "sum_lat": 0.0,
        "sum_lng": 0.0,
        "gen": 0,
        "hour_buckets": [{"count": 0.0, "total_price": 0.0} for _ in range(24)],
        "pickup_distances": [],
        "score_samples": [],  # 历史分数采样（最多保留50个）
    }


class AreaMemory:
    """全局区域统计数据，跨司机共享。使用代际计数器 + 按需衰减。"""

    def __init__(
        self,
        resolution: float = 0.1,
        decay_factor: float = 0.95,
        decay_interval: int = 100,
    ) -> None:
        self._resolution = resolution
        self._decay_factor = decay_factor
        self._decay_interval = decay_interval
        self._generation = 0
        self._grids: dict[str, dict[str, Any]] = defaultdict(_new_grid)
        #  per-driver 冷启动追踪
        self._driver_query_counts: dict[str, int] = defaultdict(int)

    @property
    def generation(self) -> int:
        return self._generation

    def record_driver_query(self, driver_id: str) -> None:
        """记录该司机的查询次数，用于 per-driver 冷启动判断。"""
        self._driver_query_counts[driver_id] += 1

    def get_driver_query_count(self, driver_id: str) -> int:
        return self._driver_query_counts.get(driver_id, 0)

    def update_from_cargo(
        self,
        query_lat: float,
        query_lng: float,
        items: list[dict[str, Any]],
        sim_min: int = 0,
    ) -> None:
        """每步决策查询货源后调用。sim_min 用于记录时段剖面。"""
        self._generation += 1
        hour = (sim_min % 1440) // 60

        if self._generation % self._decay_interval == 0:
            self._apply_decay()

        for item in items:
            cargo = item.get("cargo", {})
            if not isinstance(cargo, dict):
                continue
            start = cargo.get("start", {})
            if not isinstance(start, dict):
                continue
            try:
                lat = float(start.get("lat", 0))
                lng = float(start.get("lng", 0))
            except (TypeError, ValueError):
                continue
            price = float(cargo.get("price", 0) or 0)
            pickup_km = float(item.get("distance_km", 0) or 0)

            key = grid_key(lat, lng, self._resolution)
            g = self._grids[key]
            g["total_price"] += price
            g["count"] += 1.0
            g["sum_lat"] += lat
            g["sum_lng"] += lng
            g["gen"] = self._generation
            g["hour_buckets"][hour]["count"] += 1.0
            g["hour_buckets"][hour]["total_price"] += price
            g["pickup_distances"].append(pickup_km)
            if len(g["pickup_distances"]) > 50:
                g["pickup_distances"] = g["pickup_distances"][-50:]

    def record_score_sample(self, lat: float, lng: float, score: float) -> None:
        """记录一个货源评分样本到对应网格，用于百分位估算。"""
        key = grid_key(lat, lng, self._resolution)
        g = self._grids[key]
        g["score_samples"].append(score)
        if len(g["score_samples"]) > 50:
            g["score_samples"] = g["score_samples"][-50:]

    def _apply_decay(self) -> None:
        decay = self._decay_factor
        current_gen = self._generation
        to_delete: list[str] = []
        for key, g in self._grids.items():
            gen_diff = current_gen - g.get("gen", 0)
            if gen_diff <= 0:
                continue
            decay_steps = gen_diff // self._decay_interval
            if decay_steps > 0:
                d = decay ** decay_steps
                g["total_price"] *= d
                g["count"] *= d
                for hb in g["hour_buckets"]:
                    hb["count"] *= d
                    hb["total_price"] *= d
                if g["count"] < 0.1:
                    to_delete.append(key)
        for key in to_delete:
            del self._grids[key]

    def get_heat(self, lat: float, lng: float, hour: int | None = None) -> float:
        """货源热度（0~1）。hour=None 用全天数据，指定时用时段数据。"""
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None:
            return 0.0
        if hour is not None:
            hb = g["hour_buckets"][hour % 24]
            count = hb["count"]
            total_price = hb["total_price"]
        else:
            count = g["count"]
            total_price = g["total_price"]
        if count < 0.5:
            return 0.0
        avg_price = total_price / count if count > 0 else 0.0
        count_score = min(count / 50.0, 1.0)
        price_score = min(avg_price / 2000.0, 1.0)
        return 0.4 * count_score + 0.6 * price_score

    def get_heat_at_hour(self, lat: float, lng: float, hour: int) -> float:
        return self.get_heat(lat, lng, hour=hour)

    def get_avg_price(self, lat: float, lng: float) -> float:
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None or g["count"] < 0.5:
            return 0.0
        return g["total_price"] / g["count"] if g["count"] > 0 else 0.0

    def get_avg_price_at_hour(self, lat: float, lng: float, hour: int) -> float:
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None:
            return 0.0
        hb = g["hour_buckets"][hour % 24]
        if hb["count"] < 0.5:
            return self.get_avg_price(lat, lng)
        return hb["total_price"] / hb["count"] if hb["count"] > 0 else 0.0

    def get_density(self, lat: float, lng: float) -> float:
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None:
            return 0.0
        return g["count"]

    def get_avg_pickup_distance(self, lat: float, lng: float) -> float:
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None or not g["pickup_distances"]:
            return 50.0
        return sum(g["pickup_distances"]) / len(g["pickup_distances"])

    def get_score_percentile(self, lat: float, lng: float, target_score: float) -> float:
        """估算 target_score (0~1) 在该区域历史分数中的百分位 (0~1)。
        使用存储的历史分数样本计算，样本不足时基于价格/热度启发式估算。
        返回值越接近 1 表示 target_score 超过了越多历史分数（即当前单越好）。
        """
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None:
            return 0.5

        samples = g.get("score_samples", [])
        if len(samples) >= 5:
            better = sum(1 for s in samples if s <= target_score)
            return better / len(samples)

        # 样本不足时，基于价格和密度启发式估算
        if g["count"] < 2:
            return 0.5

        avg_price = g["total_price"] / g["count"] if g["count"] > 0 else 0.0
        avg_pickup = self.get_avg_pickup_distance(lat, lng)
        cost_per_km = 1.5
        typical_net = avg_price - avg_pickup * cost_per_km
        typical_net_per_hour = (typical_net / max(1, (avg_pickup / 60.0 * 60.0 + 180.0))) * 60.0

        # 用 net_profit 和 profit_per_hour 构建一个近似 score
        profit_score = max(0.0, min(1.0, (typical_net + 500) / 3500.0))
        efficiency_score = max(0.0, min(1.0, (typical_net_per_hour + 50) / 550.0))
        typical_score_est = 0.25 * profit_score + 0.22 * efficiency_score + 0.20 * self.get_heat(lat, lng)

        ratio = target_score / max(0.01, typical_score_est)
        if ratio >= 2.0:
            return 0.95
        elif ratio >= 1.5:
            return 0.80
        elif ratio >= 1.1:
            return 0.60
        elif ratio >= 0.9:
            return 0.45
        elif ratio >= 0.6:
            return 0.25
        else:
            return 0.08

    def get_expected_net_profit(self, lat: float, lng: float) -> float:
        """估算在该区域可接单的期望净收益（元），基于历史均价和平均空驶距离。"""
        avg_price = self.get_avg_price(lat, lng)
        if avg_price <= 0:
            return 400.0
        avg_deadhead = self.get_avg_pickup_distance(lat, lng)
        return avg_price - avg_deadhead * 1.5

    def suggest_reposition(
        self,
        current_lat: float,
        current_lng: float,
        max_distance_km: float = 200.0,
        top_n: int = 5,
        target_hour: int | None = None,
    ) -> list[tuple[float, float, float]]:
        candidates: list[tuple[float, float, float]] = []
        for key, g in self._grids.items():
            if g["count"] < 1.0:
                continue
            center_lat = g["sum_lat"] / g["count"] if g["count"] > 0 else 0.0
            center_lng = g["sum_lng"] / g["count"] if g["count"] > 0 else 0.0
            dist = haversine_km(current_lat, current_lng, center_lat, center_lng)
            if dist > max_distance_km:
                continue
            heat = self.get_heat(center_lat, center_lng, hour=target_hour)
            if heat > 0.1:
                candidates.append((center_lat, center_lng, heat))
        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates[:top_n]

    def snapshot(self) -> dict[str, Any]:
        active = sum(1 for g in self._grids.values() if g["count"] >= 1.0)
        total_obs = sum(g["count"] for g in self._grids.values())
        return {
            "grid_count": active,
            "resolution": self._resolution,
            "total_observations": total_obs,
            "generation": self._generation,
        }
