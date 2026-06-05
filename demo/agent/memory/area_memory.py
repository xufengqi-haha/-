"""区域记忆：时空流体引力势场（Dynamic Markov Potential Field）。
Phase 4 Pure-Matrix: 废除静态容量水位线，根据全网司机已宣告的远期目的地与预抵达
时间桶，利用一阶马尔可夫动态转移概率连续测绘未来 24 小时每个网格的时空运力堆积
势能 Φ(Potential Energy)，驱动 CargoScorer 的流体力学热度势能公式实现宏观纳什均衡。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from agent.utils.geo_utils import grid_key, haversine_km

# 势场衰减系数
_SPATIAL_BANDWIDTH_KM = 30.0    # 空间衰减带宽：30km 外势能衰减至 ~37%
_TEMPORAL_BANDWIDTH_H = 2.0     # 时间衰减带宽：±2h 外势能衰减至 ~37%
_NEIGHBOR_WEIGHT = 0.10          # 相邻网格势能传导权重（收拢泄漏，防背景泛滥）
_TEMPORAL_NEIGHBOR_WEIGHT = 0.20  # 相邻时段势能传导权重（聚焦核心时段）


def _neighbor_keys(lat: float, lng: float, resolution: float) -> list[str]:
    """返回中心网格及其 8 个邻居的 grid_key 列表（3×3 卷积核）。"""
    base_lat = round(lat / resolution) * resolution
    base_lng = round(lng / resolution) * resolution
    keys = []
    for dlat in (-resolution, 0.0, resolution):
        for dlng in (-resolution, 0.0, resolution):
            keys.append(grid_key(base_lat + dlat, base_lng + dlng, resolution))
    return keys


def _new_grid() -> dict[str, Any]:
    return {
        "total_price": 0.0,
        "count": 0.0,
        "sum_lat": 0.0,
        "sum_lng": 0.0,
        "gen": 0,
        "hour_buckets": [{"count": 0.0, "total_price": 0.0} for _ in range(24)],
        "pickup_distances": [],
        "score_samples": [],
        # Phase 4: 势能画布替代原始影子计数
        "intent_potential": [0.0 for _ in range(24)],
    }


class AreaMemory:
    """全局区域统计数据 + 时空势场，跨司机共享。"""

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
        self._driver_query_counts: dict[str, int] = defaultdict(int)

    @property
    def generation(self) -> int:
        return self._generation

    def record_driver_query(self, driver_id: str) -> None:
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
        key = grid_key(lat, lng, self._resolution)
        g = self._grids.get(key)
        if g is None:
            return 0.5
        samples = g.get("score_samples", [])
        if len(samples) >= 5:
            better = sum(1 for s in samples if s <= target_score)
            return better / len(samples)
        if g["count"] < 2:
            return 0.5
        avg_price = g["total_price"] / g["count"] if g["count"] > 0 else 0.0
        avg_pickup = self.get_avg_pickup_distance(lat, lng)
        cost_per_km = 1.5
        typical_net = avg_price - avg_pickup * cost_per_km
        typical_net_per_hour = (typical_net / max(1, (avg_pickup / 60.0 * 60.0 + 180.0))) * 60.0
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

    # ── Phase 4 时空势场引擎 ────────────────────────────────────────

    def register_intent(self, lat: float, lng: float, hour: int) -> None:
        """宣告运力意图：向目标网格及其时空邻域注入势能。
        一阶马尔可夫转移：势能向相邻网格（空间）和相邻时段（时间）扩散。
        """
        h = hour % 24
        center_key = grid_key(lat, lng, self._resolution)
        neighbor_keys = _neighbor_keys(lat, lng, self._resolution)

        for nk in neighbor_keys:
            g = self._grids[nk]
            is_center = (nk == center_key)
            spatial_w = 1.0 if is_center else _NEIGHBOR_WEIGHT

            # 空间传导后的基值
            base = spatial_w

            # 时间传导：当前小时 + 前后各 2 小时
            for dh in range(-2, 3):
                th = (h + dh) % 24
                temporal_w = 1.0 if dh == 0 else (
                    _TEMPORAL_NEIGHBOR_WEIGHT if abs(dh) == 1 else
                    _TEMPORAL_NEIGHBOR_WEIGHT * 0.5
                )
                g["intent_potential"][th] += base * temporal_w

    def get_potential_energy(self, lat: float, lng: float, hour: int) -> float:
        """查询目标网格在目标小时的时空运力堆积势能 Φ。
        聚合中心网格及其空间邻域在目标小时±1范围内的势能贡献。
        """
        h = hour % 24
        total = 0.0
        neighbor_keys = _neighbor_keys(lat, lng, self._resolution)
        center_key = grid_key(lat, lng, self._resolution)

        for nk in neighbor_keys:
            g = self._grids.get(nk)
            if g is None:
                continue
            is_center = (nk == center_key)
            spatial_w = 1.0 if is_center else _NEIGHBOR_WEIGHT

            for dh in range(-1, 2):
                th = (h + dh) % 24
                temporal_w = 1.0 if dh == 0 else _TEMPORAL_NEIGHBOR_WEIGHT
                total += g["intent_potential"][th] * spatial_w * temporal_w

        return total

    # ── 向后兼容 API ──────────────────────────────────────────────

    def get_shadow_count(self, lat: float, lng: float, hour: int) -> float:
        """返回目标网格目标小时的原始势能值（向后兼容）。"""
        return self.get_potential_energy(lat, lng, hour)

    def get_grid_capacity(self, lat: float, lng: float, hour: int) -> float:
        """Phase 4 已废除硬编码容量基线，返回势能的倒数归一化值（向后兼容）。"""
        pe = self.get_potential_energy(lat, lng, hour)
        return 3.0 / (1.0 + 0.3 * math.sqrt(pe)) if pe > 0 else 3.0
