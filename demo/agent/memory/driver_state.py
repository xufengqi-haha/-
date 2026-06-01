"""司机状态追踪器：记忆驱动的偏好动态膨胀机制。
追踪每个司机的累计状态（连续未回家天数、区域缺口、休息缺口），
动态计算每个偏好规则的膨胀系数 γ（gamma）。
前半月 γ≈1.0 正常跑，后半月缺口越大 γ 越膨胀。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections import defaultdict


@dataclass
class DriverStateSnapshot:
    """单个司机的当前累积状态快照"""
    driver_id: str = ""
    current_day: int = 0
    total_days: int = 31

    # 累计缺口
    days_since_last_off: int = 0          # 距上次整日休息过了多少天
    off_days_achieved: int = 0            # 已完成的整日休息天数
    off_days_required: int = 0            # 需要的整日休息天数

    # 区域偏好缺口
    region_gaps: dict[str, float] = field(default_factory=dict)  # region_name → gap_ratio (0~1)

    # 连续违规计数
    consecutive_rest_window_violations: int = 0
    consecutive_long_deadhead: int = 0

    # 全局紧迫度（基于月份进度 0~1）
    month_progress: float = 0.0

    def get_urgency_gamma(self) -> float:
        """全局紧迫度γ：综合所有偏好缺口的膨胀系数，用于整体评分调整。
        off_days缺口贡献最大权重，确保月末优先满足休息需求。
        """
        gamma = 1.0
        # off_days 贡献（权重最高）
        off_gamma = self._off_days_gamma()
        gamma = max(gamma, off_gamma)
        # 区域缺口贡献
        for gap_ratio in self.region_gaps.values():
            if gap_ratio > 0.75:
                gamma = max(gamma, 6.0)
            elif gap_ratio > 0.5:
                gamma = max(gamma, 4.0)
            elif gap_ratio > 0.25:
                gamma = max(gamma, 2.5)
        # ★ 月份紧迫度更早启动(day>15开始，之前权重低)
        if self.month_progress > 0.5:
            gamma *= 1.0 + 0.3 * (self.month_progress - 0.5) / 0.5
        # 如果off_days缺口在月末还没解决，直接拉满
        if self.current_day > 25 and off_gamma > 5.0:
            gamma = max(gamma, 8.0)
        return max(1.0, min(gamma, 10.0))

    def get_gamma(self, rule_type: str, rule_params: dict[str, Any] | None = None) -> float:
        """计算该偏好规则的动态膨胀系数 γ。
        γ=1.0 表示无膨胀，γ>1 表示惩罚加重。
        """
        gamma = 1.0

        if rule_type == "off_days":
            gamma = self._off_days_gamma()
        elif rule_type == "min_days_in_region":
            gamma = self._region_gamma(rule_params or {})
        elif rule_type == "max_pickup_km":
            gamma = 1.0 + 0.2 * self.consecutive_long_deadhead
        elif rule_type == "day_specific_location":
            gamma = 3.0 if self.month_progress > 0.8 else 1.5
        elif rule_type == "forbidden_category":
            gamma = 1.0  # 品类偏好不膨胀
        elif rule_type == "forbidden_region_cargo" or rule_type == "forbidden_region_entry":
            gamma = 1.0  # 区域禁入不膨胀

        # 全局月份紧迫度加成
        if self.month_progress > 0.7:
            gamma *= 1.0 + 0.5 * (self.month_progress - 0.7) / 0.3

        return max(1.0, min(gamma, 10.0))

    def _off_days_gamma(self) -> float:
        """off_days 膨胀系数：缺口越大、月底越近，γ越大。
        指数级增长确保月末缺口必须被填补。"""
        if self.off_days_required <= 0:
            return 1.0
        remaining = max(1, self.total_days - self.current_day)
        deficit = max(0, self.off_days_required - self.off_days_achieved)
        if deficit <= 0:
            return 1.0
        gap_ratio = deficit / max(1, self.off_days_required)
        time_pressure = deficit / max(1, remaining)

        # ★ 指数膨胀：缺口+时间压力非线性叠加
        # gap越大/剩余天数越少 → gamma急剧上升
        gamma = 1.0 + (gap_ratio ** 2) * 5.0 + (time_pressure ** 1.5) * 6.0
        # 连续不休息的累加惩罚
        gamma += min(3.0, self.days_since_last_off * 0.5)
        # 月末(day>20)且缺口>0 → 额外加成
        if self.current_day > 20 and deficit > 0:
            gamma *= 1.5
        return gamma

    def _region_gamma(self, params: dict[str, Any]) -> float:
        """区域偏好膨胀系数"""
        region = str(params.get("region", ""))
        gap_ratio = self.region_gaps.get(region, 0.0)
        if gap_ratio <= 0:
            return 1.0
        # 指数膨胀：缺口超过50%时急剧上升
        if gap_ratio > 0.75:
            return 5.0
        elif gap_ratio > 0.5:
            return 3.0
        elif gap_ratio > 0.25:
            return 2.0
        return 1.0 + gap_ratio


class DriverStateTracker:
    """跨司机共享的状态追踪器。

    在每次 decide() 调用时更新司机的累计状态，
    供 preference_scorer 查询动态 γ 系数。
    """

    def __init__(self) -> None:
        self._states: dict[str, DriverStateSnapshot] = {}

    def update(
        self,
        driver_id: str,
        sim_min: int,
        daily_stats: dict[str, Any],
        pending_requirements: list[dict[str, Any]],
        checker_rules: list[Any],
    ) -> DriverStateSnapshot:
        """更新并返回司机当前状态快照"""
        current_day = sim_min // 1440
        snap = self._states.get(driver_id)
        if snap is None:
            snap = DriverStateSnapshot(driver_id=driver_id)
            self._states[driver_id] = snap

        snap.current_day = current_day
        snap.month_progress = min(1.0, current_day / 31.0)

        # off_days 状态
        snap.off_days_achieved = daily_stats.get("off_days", 0)
        for rule in checker_rules:
            if rule.rule_type == "off_days":
                snap.off_days_required = int(rule.params.get("min_days", 0))
                break

        # 距上次 off_day 天数
        daily_active = daily_stats.get("daily_active", {})
        days_since = 0
        for d in range(current_day - 1, -1, -1):
            if daily_active.get(d, 0) == 0:
                break
            days_since += 1
        snap.days_since_last_off = days_since

        # 区域缺口
        snap.region_gaps.clear()
        for req in pending_requirements:
            if req.get("rule_type") == "min_days_in_region":
                region = str(req.get("region", ""))
                achieved = int(req.get("achieved", 0))
                required = int(req.get("required", 1))
                gap = max(0, required - achieved)
                snap.region_gaps[region] = gap / max(1, required)

        return snap

    def get_snapshot(self, driver_id: str) -> DriverStateSnapshot | None:
        return self._states.get(driver_id)
