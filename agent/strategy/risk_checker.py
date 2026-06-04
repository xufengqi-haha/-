"""风险拦截器（Risk Checker）：动作输出前的最终安检锁。
确保任何从 decide() 返回的动作都通过合法性校验，否则自动降级为安全的 fallback。
"""

from __future__ import annotations

import logging
from typing import Any

from agent.scoring.preference_scorer import PreferenceChecker
from agent.scoring.cargo_scorer import CargoScore
from agent.utils import geo_utils, time_utils


class RiskChecker:
    """动作安检锁：在 dispatcher 返回最终动作前进行多维度合法性校验。"""

    def __init__(self) -> None:
        self._logger = logging.getLogger("agent.risk_checker")

    def validate(
        self,
        action: dict[str, Any],
        driver_id: str,
        sim_min: int,
        checker: PreferenceChecker,
        scored_candidates: list[CargoScore] | None = None,
        current_lat: float = 0.0,
        current_lng: float = 0.0,
    ) -> dict[str, Any]:
        """统一入口：对任意动作执行全套安检。
        如果动作合法，原样返回；如果不合法，返回降级后的安全动作。
        """
        action_type = str(action.get("action", "")).strip().lower()

        if action_type == "take_order":
            return self._validate_take_order(action, driver_id, sim_min, checker, scored_candidates)
        elif action_type == "wait":
            return self._validate_wait(action, driver_id, sim_min, checker)
        elif action_type == "reposition":
            return self._validate_reposition(action, driver_id, sim_min, checker, current_lat, current_lng)
        else:
            # 未知动作类型，fallback 为短等待
            self._logger.warning(
                "[RISK] unknown action type '%s' for driver %s, fallback to wait 60min",
                action_type, driver_id,
            )
            return self._safe_wait(sim_min, checker, default_minutes=60)

    # ── take_order 校验 ────────────────────────────────────────────

    def _validate_take_order(
        self,
        action: dict[str, Any],
        driver_id: str,
        sim_min: int,
        checker: PreferenceChecker,
        scored_candidates: list[CargoScore] | None,
    ) -> dict[str, Any]:
        cargo_id = str((action.get("params") or {}).get("cargo_id", "")).strip()

        # 校验1：cargo_id 非空
        if not cargo_id:
            self._logger.warning("[RISK] take_order with empty cargo_id for %s", driver_id)
            return self._safe_wait(sim_min, checker, default_minutes=60)

        # 校验2：cargo_id 在候选列表中
        if scored_candidates is not None:
            matched = [cs for cs in scored_candidates if cs.cargo_id == cargo_id]
            if not matched:
                self._logger.warning(
                    "[RISK] take_order cargo_id=%s not in scored candidates for %s, fallback",
                    cargo_id, driver_id,
                )
                # 如果LLM选了一个不在候选列表中的货，降级为选最高分
                if scored_candidates:
                    return {"action": "take_order", "params": {"cargo_id": scored_candidates[0].cargo_id}}
                return self._safe_wait(sim_min, checker, default_minutes=60)

        return action

    # ── wait 校验 ──────────────────────────────────────────────────

    def _validate_wait(
        self,
        action: dict[str, Any],
        driver_id: str,
        sim_min: int,
        checker: PreferenceChecker,
    ) -> dict[str, Any]:
        duration = int((action.get("params") or {}).get("duration_minutes", 60))
        duration = max(1, duration)

        hour_of_day = time_utils.sim_min_to_hour_of_day(sim_min)
        day_idx = sim_min // 1440

        # 如果在 rest_window 内，确保等待至少覆盖到窗口结束
        for rule in checker.rules:
            if rule.rule_type == "rest_window":
                start_h = int(rule.params["start_hour"])
                end_h = int(rule.params["end_hour"])
                if start_h <= hour_of_day < end_h:
                    min_wait = time_utils.minutes_until_target_hour(sim_min, end_h)
                    if duration < min_wait:
                        self._logger.info(
                            "[RISK] wait %dmin too short in rest_window [%d:00-%d:00], "
                            "extending to %dmin",
                            duration, start_h, end_h, min_wait,
                        )
                        duration = min_wait
                    break

        # 至少等30分钟，避免高频无效查询
        duration = max(30, duration)
        duration = min(duration, 1440)

        return {"action": "wait", "params": {"duration_minutes": duration}}

    # ── reposition 校验 ────────────────────────────────────────────

    def _validate_reposition(
        self,
        action: dict[str, Any],
        driver_id: str,
        sim_min: int,
        checker: PreferenceChecker,
        current_lat: float,
        current_lng: float,
    ) -> dict[str, Any]:
        params = action.get("params") or {}
        target_lat = float(params.get("latitude", 0))
        target_lng = float(params.get("longitude", 0))

        # 校验1：距离上限 300km
        dist = geo_utils.haversine_km(current_lat, current_lng, target_lat, target_lng)
        if dist > 300.0:
            ratio = 300.0 / dist
            target_lat = current_lat + (target_lat - current_lat) * ratio
            target_lng = current_lng + (target_lng - current_lng) * ratio
            self._logger.info(
                "[RISK] reposition capped from %.0fkm to 300km for %s", dist, driver_id,
            )

        # 校验2：不违反区域禁入/日期禁入
        penalty, violations = checker.check_reposition(
            current_lat, current_lng, target_lat, target_lng, sim_min,
        )
        if penalty > 500:
            self._logger.warning(
                "[RISK] reposition violates preferences: %s, fallback to wait",
                violations,
            )
            return self._safe_wait(sim_min, checker, default_minutes=60)

        # 校验3：迁移过程不穿越 rest_window
        dist_minutes = max(1, int((dist / 60.0) * 60.0))
        for rule in checker.rules:
            if rule.rule_type == "rest_window":
                transit_end = sim_min + dist_minutes
                start_h = int(rule.params["start_hour"])
                end_h = int(rule.params["end_hour"])
                for m in range(sim_min, transit_end + 1, 30):
                    h = (m % 1440) / 60.0
                    if time_utils.hour_in_range(h, start_h, end_h - 0.5):
                        self._logger.warning(
                            "[RISK] reposition crosses rest_window [%d:00-%d:00], "
                            "fallback to wait",
                            start_h, end_h,
                        )
                        return self._safe_wait(sim_min, checker, default_minutes=60)

        return {"action": "reposition", "params": {"latitude": target_lat, "longitude": target_lng}}

    # ── 安全降级 ───────────────────────────────────────────────────

    def _safe_wait(
        self,
        sim_min: int,
        checker: PreferenceChecker,
        default_minutes: int = 60,
    ) -> dict[str, Any]:
        """生成安全的 wait 动作，确保不在 rest_window 内过早醒来。"""
        hour_of_day = time_utils.sim_min_to_hour_of_day(sim_min)
        duration = default_minutes

        for rule in checker.rules:
            if rule.rule_type == "rest_window":
                start_h = int(rule.params["start_hour"])
                end_h = int(rule.params["end_hour"])
                if start_h <= hour_of_day < end_h:
                    duration = max(duration, time_utils.minutes_until_target_hour(sim_min, end_h))
                    break

        duration = max(30, min(duration, 1440))
        return {"action": "wait", "params": {"duration_minutes": duration}}
