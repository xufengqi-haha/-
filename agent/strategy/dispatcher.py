"""决策调度器：Rule-First 多层次过滤→评分→未来价值增强→规则决策→LLM兜底→RiskChecker安检。
v2: 激活 FutureIncomeEstimator；修复 wait_cost/hard_forbidden；新增 RiskChecker 安检锁；
    per-driver 冷启动；LLM 输出 Pydantic 校验；统一区域数据源。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from collections import defaultdict

from pydantic import BaseModel, Field, ValidationError

from agent.memory.area_memory import AreaMemory
from agent.memory.driver_state import DriverStateTracker
from agent.scoring.cargo_scorer import CargoScore, CargoScorer, ScorerConfig
from agent.scoring.preference_scorer import PreferenceChecker, PreferenceParser
from agent.strategy.risk_checker import RiskChecker
from simkit.ports import SimulationApiPort
from agent.utils import geo_utils, time_utils
from agent.utils.geo_utils import REGION_COORDINATES, COMMON_REGION_NAMES, near_region
from agent.config import AgentConfig, DEFAULT_CONFIG
from agent.planner.future_income_estimator import FutureIncomeEstimator
from agent.planner.opportunity_evaluator import OpportunityEvaluator


# ── LLM 输出 Pydantic Schema ──────────────────────────────────────

class TakeOrderParams(BaseModel):
    cargo_id: str

class WaitParams(BaseModel):
    duration_minutes: int = Field(default=60, ge=1, le=1440)

class LLMTiebreakResult(BaseModel):
    action: str = Field(..., pattern="^(take_order|wait)$")
    params: dict[str, Any] = Field(default_factory=dict)


# ── LLM System Prompt ─────────────────────────────────────────────

_TIEBREAK_SYSTEM_PROMPT = (
    "你是货运调度决策助手。给定2个经过预筛选的高分候选货源及司机状态，选出更优动作。\n"
    "评分维度：net_profit(净收益)、profit_per_hour(时薪)、pickup_km(空驶距离)、"
    "haul_km(干线距离)、dest_heat(目的地热度0~1)、pref_penalty(偏好罚分)、gamma(偏好紧迫度)。\n"
    "决策原则：\n"
    "1. 如果净收益 > 偏好罚分×γ，接单更划算（司机愿意忍受扣分赚钱）\n"
    "2. 如果偏好罚分×γ > 净收益的80%，应等待（扣分太多不值得）\n"
    "3. 月末(day>25)+偏好缺口大(γ>3)时，优先满足偏好而非赚钱\n"
    "4. 目的地热度<0.1的冷区单应避免，除非利润极高\n"
    "只输出JSON：{\"action\":\"take_order\",\"params\":{\"cargo_id\":\"...\"}} "
    "或 {\"action\":\"wait\",\"params\":{\"duration_minutes\":整数}}。禁止markdown。"
)


# ── DecisionDispatcher ─────────────────────────────────────────────

class DecisionDispatcher:
    def __init__(self, api: SimulationApiPort, config: AgentConfig | None = None) -> None:
        self._api = api
        self._config = config or DEFAULT_CONFIG
        self._area_memory = AreaMemory(
            resolution=self._config.area_memory["resolution"],
            decay_factor=self._config.area_memory.get("decay_factor", 0.95),
            decay_interval=self._config.area_memory.get("decay_interval", 100),
        )
        self._parser = PreferenceParser(api)
        self._scorer = CargoScorer(ScorerConfig(
            w_profit=self._config.scorer_weights["w_profit"],
            w_efficiency=self._config.scorer_weights["w_efficiency"],
            w_dest_heat=self._config.scorer_weights["w_dest_heat"],
            w_pref_penalty=self._config.scorer_weights["w_pref_penalty"],
            w_time_risk=self._config.scorer_weights["w_time_risk"],
            w_future_value=self._config.scorer_weights.get("w_future_value", 0.08),
            w_distance_bonus=self._config.scorer_weights.get("w_distance_bonus", 0.05),
            cost_per_km=self._config.filters.get("cost_per_km", 1.5),
            time_value_per_minute=self._config.filters.get("time_value_per_minute", 1.0),
        ))
        self._pref_cache: dict[str, tuple[int, PreferenceChecker]] = {}
        self._logger = logging.getLogger("agent.dispatcher")

        # 期望账
        self._future_estimator = FutureIncomeEstimator(
            area_memory=self._area_memory,
            cost_per_km=self._config.filters.get("cost_per_km", 1.5),
            time_value_per_minute=self._config.future_estimation["time_value_per_minute"],
            cold_zone_heat_threshold=self._config.future_estimation["cold_zone_heat_threshold"],
            cold_zone_penalty=self._config.future_estimation["cold_zone_penalty"],
            future_value_discount=self._config.future_estimation.get("future_value_discount", 0.5),
        )

        # 博弈账
        self._opportunity = OpportunityEvaluator(
            area_memory=self._area_memory,
            min_score_to_wait=self._config.opportunity["min_score_to_consider_wait"],
            wait_cost_per_minute=self._config.opportunity["wait_cost_per_minute"],
            reposition_gain_threshold=self._config.opportunity["reposition_gain_threshold"],
            max_wait_for_better=self._config.opportunity["max_wait_for_better_minutes"],
            cost_per_km=self._config.filters.get("cost_per_km", 1.5),
            time_value_per_minute=self._config.future_estimation["time_value_per_minute"],
        )

        # 安检锁
        self._risk_checker = RiskChecker()

        # 司机状态追踪（偏好动态膨胀）
        self._driver_state_tracker = DriverStateTracker()

        # LLM 控制
        self._llm_call_counts: dict[str, int] = defaultdict(int)
        self._max_llm_calls_per_driver = self._config.llm_control["max_calls_per_driver"]

        # 动态K追踪
        self._prev_candidate_counts: dict[str, int] = {}

        # 决策统计
        self._decision_stats: dict[str, dict] = defaultdict(lambda: {
            "total_decisions": 0,
            "take_order_count": 0,
            "wait_count": 0,
            "reposition_count": 0,
            "llm_tiebreak_count": 0,
            "llm_fallback_count": 0,
            "risk_check_triggered": 0,
            "avg_score": 0.0,
        })

    # ── 主决策入口 ─────────────────────────────────────────────────

    def decide(self, driver_id: str) -> dict[str, Any]:
        status = self._api.get_driver_status(driver_id)
        lat = float(status["current_lat"])
        lng = float(status["current_lng"])
        sim_min = int(status["simulation_progress_minutes"])
        pref_list = status.get("preferences") or []
        day_idx = time_utils.sim_min_to_day(sim_min)
        sim_horizon = int(self._config.future_estimation.get("simulation_duration_days", 31)) * 1440

        checker = self._get_or_parse_preferences(driver_id, pref_list)
        hist = self._get_recent_history(driver_id)
        daily_stats = self._compute_daily_stats(driver_id, hist, sim_min)

        stats = self._decision_stats[driver_id]

        # ★ 提前计算 pending 和 driver_state（供 gamma 驱动休息检查使用）
        pending_early = checker.get_pending_requirements(daily_stats, sim_min)
        driver_state = self._driver_state_tracker.update(
            driver_id, sim_min, daily_stats, pending_early, checker.rules,
        )
        hour_of_day = time_utils.sim_min_to_hour_of_day(sim_min)

        # === Phase 1: gamma驱动强制休息（最高优先级）===
        off_days_gamma = driver_state._off_days_gamma()
        if off_days_gamma > 4.0 and hour_of_day < 8:
            until_midnight = time_utils.minutes_until_next_day(sim_min)
            self._logger.info(
                "[GAMMA] off_days gamma=%.1f forcing full rest day %d (%dmin)",
                off_days_gamma, day_idx, until_midnight,
            )
            stats["total_decisions"] += 1
            stats["wait_count"] += 1
            self._update_avg_score(stats, 0.0)
            action = self._make_wait(max(until_midnight, 60))
            return self._risk_checker.validate(action, driver_id, sim_min, checker)

        # === Phase 2: 常规强制休息检查 ===
        rest_minutes = self._check_mandatory_rest(sim_min, day_idx, checker, daily_stats, sim_horizon)
        if rest_minutes > 0:
            stats["total_decisions"] += 1
            stats["wait_count"] += 1
            self._update_avg_score(stats, 0.0)
            self._logger.info("[REST] mandatory rest %dmin day=%d", rest_minutes, day_idx)
            action = self._make_wait(rest_minutes)
            return self._risk_checker.validate(action, driver_id, sim_min, checker)

        # === Phase 2.5: 跨日预排 — 明天有day_specific_location且距离远 → 提前reposition ===
        if hour_of_day >= 18:
            pre_day_action = self._check_pre_day_location(lat, lng, sim_min, day_idx, checker, stats)
            if pre_day_action is not None:
                return self._risk_checker.validate(
                    pre_day_action, driver_id, sim_min, checker,
                    current_lat=lat, current_lng=lng,
                )

        # === Phase 3: 冷启动保护（per-driver） ===
        driver_queries = self._area_memory.get_driver_query_count(driver_id)
        min_driver_queries = self._config.cold_start.get("min_queries_per_driver", 5)
        min_global_gen = self._config.cold_start.get("min_total_generation", 10)
        cold_start = (driver_queries < min_driver_queries) or (self._area_memory.generation < min_global_gen)

        # === Phase 4: 动态K查询货源 ===
        prev_count = self._prev_candidate_counts.get(driver_id, 100)
        k = self._adaptive_k(lat, lng, prev_count)
        cargo_resp = self._api.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng, k=k)
        items = cargo_resp.get("items", [])
        if not isinstance(items, list):
            items = []
        self._area_memory.update_from_cargo(lat, lng, items, sim_min)
        self._area_memory.record_driver_query(driver_id)
        self._prev_candidate_counts[driver_id] = len(items)

        # 记录分数样本到 area_memory
        self._record_score_samples(lat, lng, items, checker, sim_min)

        # === Phase 5: 活跃订单检查 ===
        cargo_id = str(status.get("current_order_cargo_id") or "").strip()
        if cargo_id:
            self._logger.info("driver %s has active order %s, waiting", driver_id, cargo_id)
            return self._make_wait(60)

        # === Phase 6: 构建约束条件 ===
        day_constraint = None   # (lat, lng, radius_km) — day_specific日的区域锁
        region_constraint = None  # (region_name, strict) — 累计缺口升级

        if self._config.preference_feedback["enable_cumulative_push"]:
            # P1: day_specific日 → 启动全程地理约束（放宽到80km半径）
            for req in pending_early:
                if req.get("rule_type") == "day_specific_location" and req.get("action") == "go_to_location":
                    tlat, tlng = req.get("lat"), req.get("lng")
                    if tlat and tlng:
                        dist_to_target = geo_utils.haversine_km(lat, lng, tlat, tlng)
                        if dist_to_target < 80:
                            day_constraint = (tlat, tlng, 80.0)
                            self._logger.info(
                                "[P1] day_constraint active: lock to (%.2f,%.2f) radius=80km dist=%.1fkm",
                                tlat, tlng, dist_to_target,
                            )
                            break
            # P2: 区域累计缺口≥50% → 升级为硬约束（提前触发）
            for req in pending_early:
                if req.get("rule_type") == "min_days_in_region":
                    gap = int(req.get("gap", 0))
                    required = int(req.get("required", 1))
                    gap_ratio_local = gap / max(1, required)
                    if gap_ratio_local >= 0.4 or (gap >= 1 and gap_ratio_local >= 0.25):
                        region_constraint = (req.get("region", ""), True)
                        self._logger.info(
                            "[P2] region_constraint active: only accept cargo to %s (gap=%d)",
                            req.get("region"), gap,
                        )
                        break

        # === Phase 7: 过滤 + 评分 ===
        candidates = self._filter_and_score(
            items, sim_min, checker, self._area_memory, sim_horizon,
            day_constraint=day_constraint,
            region_constraint=region_constraint,
            driver_state=driver_state,
            daily_stats=daily_stats,
        )

        # === Phase 8: 累计偏好反馈 ===
        if self._config.preference_feedback["enable_cumulative_push"]:
            self._apply_preference_push(candidates, pending_early, sim_min)

        scored = [c for c in candidates if c.total_score > 0.05]
        stats["total_decisions"] += 1

        # === Phase 9: 日特定地点检查 ===
        if self._config.preference_feedback["enable_cumulative_push"]:
            day_action = self._check_day_specific_location(
                driver_id, lat, lng, sim_min, day_idx, checker, daily_stats, stats
            )
            if day_action is not None:
                return self._risk_checker.validate(
                    day_action, driver_id, sim_min, checker,
                    scored_candidates=scored, current_lat=lat, current_lng=lng,
                )

        # === Phase 10: 无合格货源 ===
        if not scored:
            action = self._handle_no_cargo(
                driver_id, lat, lng, sim_min, day_idx, checker, daily_stats, stats, cold_start
            )
            return self._risk_checker.validate(
                action, driver_id, sim_min, checker,
                scored_candidates=scored, current_lat=lat, current_lng=lng,
            )

        top3 = scored[:3]
        best = top3[0]

        # === Phase 11: 边际分数 → 等待/迁移评估 ===
        marginal_threshold = self._config.decision_thresholds.get("marginal_score_threshold", 0.25)
        if best.total_score < marginal_threshold and not cold_start:
            wait_eval = self._opportunity.evaluate_take_vs_wait(
                best.total_score, best.net_profit, lat, lng, sim_min,
            )
            if wait_eval["action"] == "wait":
                dur = int(wait_eval.get("duration", 60))
                stats["wait_count"] += 1
                self._update_avg_score(stats, 0.0)
                action = self._make_wait(max(30, dur))
                return self._risk_checker.validate(
                    action, driver_id, sim_min, checker,
                    scored_candidates=scored, current_lat=lat, current_lng=lng,
                )

            repo_eval = self._opportunity.evaluate_take_vs_reposition(
                best.total_score, best.net_profit, lat, lng, sim_min, sim_horizon,
            )
            if repo_eval["action"] == "reposition" and repo_eval["target"] is not None:
                target = repo_eval["target"]
                stats["reposition_count"] += 1
                self._update_avg_score(stats, 0.0)
                action = self._capped_reposition(lat, lng, target[0], target[1])
                return self._risk_checker.validate(
                    action, driver_id, sim_min, checker,
                    scored_candidates=scored, current_lat=lat, current_lng=lng,
                )

        # === Phase 12: 明显优势 → 直接选择 ===
        clear_winner_ratio = self._config.decision_thresholds.get("clear_winner_ratio", 1.25)
        if len(top3) == 1 or best.total_score >= top3[1].total_score * clear_winner_ratio:
            self._logger.info(
                "clear winner cargo_id=%s score=%.4f net_profit=%.1f profit_per_hour=%.1f",
                best.cargo_id, best.total_score, best.net_profit,
                best.breakdown.get("profit_per_hour", 0),
            )
            stats["take_order_count"] += 1
            self._update_avg_score(stats, best.total_score)
            action = self._make_take_order(best.cargo_id)
            return self._risk_checker.validate(
                action, driver_id, sim_min, checker,
                scored_candidates=scored, current_lat=lat, current_lng=lng,
            )

        # === Phase 13: LLM 二选一 ===
        llm_tiebreak_gap = self._config.decision_thresholds.get("llm_tiebreak_gap", 0.12)
        llm_min_score = self._config.decision_thresholds.get("llm_min_score", 0.30)
        if len(top3) >= 2 and best.total_score > 0 and top3[1].total_score > 0:
            gap = (best.total_score - top3[1].total_score) / best.total_score if best.total_score > 0 else 0
            llm_call_count = self._llm_call_counts[driver_id]

            if (gap < llm_tiebreak_gap
                    and best.total_score > llm_min_score
                    and llm_call_count < self._max_llm_calls_per_driver):
                self._logger.info(
                    "close call gap=%.3f top2=(%s,%.4f) (%s,%.4f) → LLM tiebreak (calls=%d/%d)",
                    gap, best.cargo_id, best.total_score,
                    top3[1].cargo_id, top3[1].total_score,
                    llm_call_count, self._max_llm_calls_per_driver,
                )
                llm_result = self._llm_tiebreak(driver_id, status, top3[:2], checker, sim_min)
                self._llm_call_counts[driver_id] += 1
                stats["llm_tiebreak_count"] += 1

                if llm_result is not None:
                    if llm_result["action"] == "take_order":
                        stats["take_order_count"] += 1
                        chosen_cargo_id = (llm_result.get("params") or {}).get("cargo_id", "")
                        for cs in top3[:2]:
                            if cs.cargo_id == chosen_cargo_id:
                                self._update_avg_score(stats, cs.total_score)
                                break
                    elif llm_result["action"] == "wait":
                        stats["wait_count"] += 1
                        self._update_avg_score(stats, 0.0)
                    return self._risk_checker.validate(
                        llm_result, driver_id, sim_min, checker,
                        scored_candidates=scored, current_lat=lat, current_lng=lng,
                    )
                else:
                    stats["llm_fallback_count"] += 1

        # === Phase 14: 默认最高分 ===
        self._logger.info(
            "rule-based decision cargo_id=%s score=%.4f net_profit=%.1f",
            best.cargo_id, best.total_score, best.net_profit,
        )
        stats["take_order_count"] += 1
        self._update_avg_score(stats, best.total_score)
        action = self._make_take_order(best.cargo_id)
        return self._risk_checker.validate(
            action, driver_id, sim_min, checker,
            scored_candidates=scored, current_lat=lat, current_lng=lng,
        )

    # ── 公开接口 ────────────────────────────────────────────────────

    def get_decision_summary(self) -> dict[str, Any]:
        summary = {}
        for driver_id, stats in self._decision_stats.items():
            summary[driver_id] = {
                **stats,
                "llm_usage_rate": (
                    stats["llm_tiebreak_count"] / max(stats["total_decisions"], 1)
                ),
                "llm_fallback_rate": (
                    stats["llm_fallback_count"] / max(stats["llm_tiebreak_count"], 1)
                ) if stats["llm_tiebreak_count"] > 0 else 0.0,
            }
        return summary

    # ── 自适应K ─────────────────────────────────────────────────────

    def _adaptive_k(self, lat: float, lng: float, prev_count: int) -> int:
        if not self._config.dynamic_k.get("enabled", True):
            return self._config.dynamic_k.get("default", 100)
        density = self._area_memory.get_density(lat, lng)
        if density > self._config.dynamic_k.get("high_density_threshold", 100):
            return self._config.dynamic_k.get("high_density_k", 50)
        if density < self._config.dynamic_k.get("low_density_threshold", 20):
            return self._config.dynamic_k.get("low_density_k", 200)
        min_candidates = self._config.dynamic_k.get("min_candidates_before_boost", 3)
        if prev_count < min_candidates:
            return self._config.dynamic_k.get("no_candidate_k", 200)
        return self._config.dynamic_k.get("default", 100)

    # ── 累计偏好推动 ────────────────────────────────────────────────

    def _apply_preference_push(
        self,
        candidates: list[CargoScore],
        pending: list[dict[str, Any]],
        sim_min: int,
    ) -> None:
        """对满足累计式偏好缺口的候选货源加分（替换列表元素，避免修改原对象）。"""
        if not pending:
            return
        day_idx = sim_min // 1440
        region_bonus_max = self._config.preference_feedback["region_bonus_max"]

        for i, cs in enumerate(candidates):
            bonus = 0.0
            penalty_mult = 1.0

            for req in pending:
                if req.get("action") == "boost_region":
                    urgency = float(req.get("urgency", 0))
                    if urgency <= 0:
                        continue
                    region = req.get("region", "")
                    dest_lat = cs.breakdown.get("dest_lat", 0)
                    dest_lng = cs.breakdown.get("dest_lng", 0)
                    cargo_region = self._city_at(dest_lat, dest_lng)
                    if region not in cargo_region:
                        continue
                    gap = int(req.get("gap", 1))
                    required = int(req.get("required", 1))
                    gap_ratio = gap / max(1, required)
                    bonus += region_bonus_max * (0.3 * gap_ratio + 0.7 * urgency)

                elif req.get("action") == "prefer_wait":
                    urgency = float(req.get("urgency", 0))
                    if urgency > 0.3:
                        penalty_mult *= 0.85

            if bonus > 0 or penalty_mult < 1.0:
                candidates[i] = CargoScore(
                    cargo_id=cs.cargo_id,
                    total_score=round(cs.total_score * penalty_mult + bonus, 4),
                    profit_score=cs.profit_score,
                    efficiency_score=cs.efficiency_score,
                    dest_heat_score=cs.dest_heat_score,
                    pref_penalty_score=cs.pref_penalty_score,
                    time_risk_score=cs.time_risk_score,
                    future_value_score=cs.future_value_score,
                    distance_score=cs.distance_score,
                    net_profit=cs.net_profit,
                    total_minutes=cs.total_minutes,
                    dest_lat=cs.dest_lat,
                    dest_lng=cs.dest_lng,
                    breakdown=cs.breakdown,
                )

        if any(req.get("action") == "boost_region" for req in pending):
            boost_reqs = [req for req in pending if req.get("action") == "boost_region"]
            self._logger.info(
                "[PREF_PUSH] boost_region applied: %d regions, day=%d",
                len(boost_reqs), day_idx,
            )

    def _city_at(self, lat: float, lng: float) -> str:
        best_city = ""
        best_dist = float("inf")
        for name, coord in REGION_COORDINATES.items():
            d = geo_utils.haversine_km(lat, lng, coord[0], coord[1])
            if d < best_dist:
                best_dist = d
                best_city = name
        return best_city if best_dist < 50 else ""

    # ── 日特定地点检查 ──────────────────────────────────────────────

    def _check_day_specific_location(
        self,
        driver_id: str,
        lat: float,
        lng: float,
        sim_min: int,
        day_idx: int,
        checker: PreferenceChecker,
        daily_stats: dict[str, Any],
        stats: dict[str, Any],
    ) -> dict[str, Any] | None:
        pending = checker.get_pending_requirements(daily_stats, sim_min)
        for req in pending:
            if req.get("rule_type") == "day_specific_location" and req.get("action") == "go_to_location":
                target_lat = req.get("lat")
                target_lng = req.get("lng")
                if target_lat is not None and target_lng is not None:
                    dist = geo_utils.haversine_km(lat, lng, target_lat, target_lng)
                    if dist > 3:
                        self._logger.info(
                            "[PREF_PUSH] day_specific_location day=%d → reposition to %s (%.1fkm)",
                            day_idx, req.get("region_name", "?"), dist,
                        )
                        stats["reposition_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._capped_reposition(lat, lng, target_lat, target_lng)
        return None

    # ── 跨日预排检查 ──────────────────────────────────────────────

    def _check_pre_day_location(
        self,
        lat: float,
        lng: float,
        sim_min: int,
        day_idx: int,
        checker: PreferenceChecker,
        stats: dict[str, Any],
    ) -> dict[str, Any] | None:
        """检查明天是否有day_specific_location，提前reposition靠近目标。"""
        tomorrow_pending = checker.get_pending_requirements(
            {"daily_active": {}, "off_days": 0, "region_days": {}, "daily_rest_max": {}},
            sim_min + 1440,
        )
        for req in tomorrow_pending:
            if req.get("rule_type") == "day_specific_location" and req.get("action") == "go_to_location":
                tlat = req.get("lat")
                tlng = req.get("lng")
                if tlat and tlng:
                    dist = geo_utils.haversine_km(lat, lng, tlat, tlng)
                    if dist > 50:
                        self._logger.info(
                            "[PRE_DAY] tomorrow day=%d needs %s, %.1fkm away → reposition",
                            day_idx + 1, req.get("region_name", "?"), dist,
                        )
                        stats["reposition_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._capped_reposition(lat, lng, tlat, tlng)
        return None

    # ── 强制休息检查 ────────────────────────────────────────────────

    def _check_mandatory_rest(
        self,
        sim_min: int,
        day_idx: int,
        checker: PreferenceChecker,
        daily_stats: dict[str, Any],
        sim_horizon: int = 31 * 1440,
    ) -> int:
        hour_of_day = time_utils.sim_min_to_hour_of_day(sim_min)
        daily_rest_max = daily_stats.get("daily_rest_max", {})

        for rule in checker.rules:
            if rule.rule_type == "rest_window":
                start_h = int(rule.params["start_hour"])
                end_h = int(rule.params["end_hour"])
                # ★ 使用半开区间 [start_h, end_h)：6:00整不算休息窗口内
                if start_h <= hour_of_day < end_h:
                    remain = time_utils.minutes_until_target_hour(sim_min, end_h)
                    return max(60, remain)

            if rule.rule_type == "daily_rest":
                min_hours = int(rule.params.get("min_hours", 0))
                if min_hours <= 0:
                    continue
                longest_today = daily_rest_max.get(day_idx, 0)
                deficit = min_hours * 60 - longest_today
                if deficit <= 0:
                    continue
                remaining_today = time_utils.minutes_until_next_day(sim_min)
                if remaining_today >= deficit and deficit > 60:
                    return min(deficit, remaining_today)

            if rule.rule_type == "off_days":
                min_days = int(rule.params.get("min_days", 0))
                if min_days <= 0:
                    continue
                achieved = daily_stats.get("off_days", 0)
                deficit = min_days - achieved
                if deficit <= 0:
                    continue
                remaining_days = max(1, (sim_horizon - sim_min) // 1440)
                until_midnight = time_utils.minutes_until_next_day(sim_min)

                # ★ 策略改进：如果已经错过均匀分布窗口，判断今天是
                # 否还没开始工作（早上6点前）→ 直接休一整天
                if day_idx >= 3:
                    expected_off_days = max(0, min_days - achieved)
                    if remaining_days <= expected_off_days:
                        self._logger.warning(
                            "[OFF_DAY] CRITICAL: achieved=%d/%d remaining=%d deficit=%d",
                            achieved, min_days, remaining_days, expected_off_days,
                        )
                        return max(until_midnight, 60)

                # ★ 均匀分布 + 检查今天是否适合休息
                interval = max(1, (remaining_days + 1) // max(1, deficit))
                days_since_last_off = _days_since_last_off(daily_stats, day_idx)
                if days_since_last_off >= max(3, interval) and achieved < min_days:
                    self._logger.info(
                        "[OFF_DAY] scheduling rest: got %d/%d, interval=%d, "
                        "days_since_off=%d, remaining=%d",
                        achieved, min_days, interval, days_since_last_off, remaining_days,
                    )
                    return max(until_midnight, 60)

        return 0

    # ── 偏好解析（带缓存）───────────────────────────────────────────

    def _get_or_parse_preferences(self, driver_id: str, pref_list: list[dict[str, Any]]) -> PreferenceChecker:
        raw_hash = hash(json.dumps(pref_list, ensure_ascii=False, sort_keys=True))
        cached = self._pref_cache.get(driver_id)
        if cached is not None and cached[0] == raw_hash:
            return cached[1]
        rules = self._parser.parse(pref_list)
        checker = PreferenceChecker(rules)
        self._pref_cache[driver_id] = (raw_hash, checker)
        self._logger.info(
            "preferences parsed driver=%s rules=%s",
            driver_id,
            [(r.rule_type, r.params) for r in rules],
        )
        return checker

    def _get_recent_history(self, driver_id: str) -> list[dict[str, Any]]:
        try:
            resp = self._api.query_decision_history(driver_id, -1)
            return resp.get("records") or []
        except Exception:
            return []

    # ── 每日统计计算 ────────────────────────────────────────────────

    def _compute_daily_stats(
        self,
        driver_id: str,
        hist: list[dict[str, Any]],
        current_sim_min: int,
    ) -> dict[str, Any]:
        current_day = time_utils.sim_min_to_day(current_sim_min)
        total_days = current_day + (1 if current_sim_min % 1440 > 0 else 0)
        daily_rest_max: dict[int, int] = {}
        daily_active: dict[int, int] = {}
        daily_region_days: dict[str, set[int]] = {}

        prev_end = 0
        for record in hist:
            action = record.get("action") or {}
            action_name = str(action.get("action", "")).strip().lower()
            result = record.get("result") or {}
            action_exec = int(record.get("action_exec_cost_minutes", 0))
            query_scan = int(record.get("query_scan_cost_minutes", 0))
            pos_before = record.get("position_before") or {}
            pos_after = record.get("position_after") or {}

            action_start = prev_end + query_scan
            action_end = action_start + action_exec
            day = time_utils.sim_min_to_day(action_start)

            if action_name == "wait" and action_exec > 0:
                daily_rest_max[day] = max(daily_rest_max.get(day, 0), action_exec)

            if action_name in ("take_order", "reposition"):
                cur = action_start
                while cur < action_end:
                    day_idx = cur // 1440
                    day_end = (day_idx + 1) * 1440
                    chunk = min(day_end, action_end) - cur
                    daily_active[day_idx] = daily_active.get(day_idx, 0) + chunk
                    cur = day_end
                if action_name == "take_order" and bool(result.get("accepted", False)):
                    for region_name in COMMON_REGION_NAMES:
                        if near_region(pos_before, region_name) or near_region(pos_after, region_name):
                            s = daily_region_days.setdefault(region_name, set())
                            s.add(day)

            prev_end = max(prev_end, action_end)

        off_days = sum(1 for d in range(total_days) if daily_active.get(d, 0) == 0)

        return {
            "daily_rest_max": daily_rest_max,
            "daily_active": daily_active,
            "off_days": off_days,
            "region_days": daily_region_days,
            "total_days": total_days,
        }

    # ── 过滤 + 评分 ─────────────────────────────────────────────────

    def _filter_and_score(
        self,
        items: list[dict[str, Any]],
        sim_min: int,
        checker: PreferenceChecker,
        area_memory: AreaMemory,
        sim_horizon: int,
        day_constraint: tuple[float, float, float] | None = None,
        region_constraint: tuple[str, float] | None = None,
        driver_state: Any = None,
        daily_stats: dict[str, Any] | None = None,
    ) -> list[CargoScore]:
        import math
        from datetime import datetime
        month_end = 31 * 1440
        scored: list[CargoScore] = []
        for item in items:
            cargo = item.get("cargo", {})
            if not isinstance(cargo, dict):
                continue
            pickup_km = float(item.get("distance_km", 0) or 0)
            cost_time = int(cargo.get("cost_time_minutes", 0) or 0)
            pickup_min = max(1, math.ceil((pickup_km / 60.0) * 60.0)) if pickup_km > 1e-6 else 0
            est_completion = sim_min + pickup_min + cost_time
            if est_completion > month_end:
                continue

            if pickup_km > self._config.filters["max_pickup_km"]:
                continue

            # 偏好合规检查（per-rule gamma加权）
            penalty, violations = checker.check_cargo_weighted(cargo, pickup_km, sim_min, driver_state)
            if checker.hard_forbidden(cargo, pickup_km, sim_min, driver_state):
                if violations:
                    self._logger.debug("hard filter: cargo=%s reasons=%s", cargo.get("cargo_id"), violations)
                continue

            # 穿越禁行时段检查（使用结构化返回值）
            transit_penalty, transit_violations, is_hard_transit = checker.check_transit_violation(
                pickup_km, sim_min,
                cargo_cost_time=int(cargo.get("cost_time_minutes", 0) or 0),
            )
            if is_hard_transit:
                self._logger.debug(
                    "hard filter: cargo=%s crosses rest_window: %s",
                    cargo.get("cargo_id"), transit_violations,
                )
                continue
            if transit_violations:
                penalty += transit_penalty
                violations.extend(transit_violations)

            dest_lat = float((cargo.get("end") or {}).get("lat", 0))
            dest_lng = float((cargo.get("end") or {}).get("lng", 0))
            load_wait = 0
            load_time = cargo.get("load_time")
            if isinstance(load_time, list) and len(load_time) == 2:
                try:
                    start_str = str(load_time[0]).strip()
                    start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                    load_start_min = int((start_dt - time_utils.SIMULATION_EPOCH).total_seconds() // 60)
                    arrival = sim_min + pickup_min
                    if arrival < load_start_min:
                        load_wait = load_start_min - arrival
                except (ValueError, KeyError):
                    pass
            total_minutes_est = pickup_min + cost_time + load_wait + 30  # +30min卸货缓冲

            # ★ daily_rest软惩罚：订单导致当天无法8h休息 → penalty×1.3
            if daily_stats is not None:
                day_idx_for_check = sim_min // 1440
                daily_rest_max_chk = daily_stats.get("daily_rest_max", {})
                for rule in checker.rules:
                    if rule.rule_type == "daily_rest":
                        min_h = int(rule.params.get("min_hours", 0))
                        if min_h <= 0:
                            continue
                        longest = daily_rest_max_chk.get(day_idx_for_check, 0)
                        deficit = min_h * 60 - longest
                        if deficit < 120:  # 只有缺口>2h才触发
                            continue
                        minutes_left = time_utils.minutes_until_next_day(sim_min)
                        if total_minutes_est + deficit > minutes_left:
                            penalty *= 1.3
                        break
            finish_min = sim_min + total_minutes_est

            # ★ P0: Cross-Day DailyRest Feasibility Check — 防止跨天单导致次日无法休满
            skip_for_daily_rest = False
            if daily_stats is not None:
                cur_day = sim_min // 1440
                finish_day = finish_min // 1440
                if finish_day > cur_day:
                    for rule in checker.rules:
                        if rule.rule_type == "daily_rest":
                            min_h = int(rule.params.get("min_hours", 0))
                            if min_h <= 0:
                                continue
                            need_rest = min_h * 60
                            blocked = False
                            blocked_day = finish_day
                            for d in range(cur_day + 1, finish_day + 1):
                                day_longest = daily_stats.get("daily_rest_max", {}).get(d, 0)
                                if day_longest >= need_rest:
                                    continue
                                if d < finish_day:
                                    blocked = True
                                    blocked_day = d
                                else:
                                    remaining_next = 1440 - (finish_min % 1440)
                                    if remaining_next < need_rest:
                                        blocked = True
                            if blocked:
                                skip_for_daily_rest = True
                                self._logger.info(
                                    "P0 reject: cross-day daily_rest infeasible "
                                    "blocked_day=%d finish_day=%d need=%d cargo=%s",
                                    blocked_day, finish_day, need_rest,
                                    cargo.get("cargo_id"),
                                )
                            break
            if skip_for_daily_rest:
                continue

            # ★ P0: 全链路完单时间预判 — 防止完单跨入rest_window
            skip_for_rest = False
            for rule in checker.rules:
                if rule.rule_type == "rest_window":
                    start_h = int(rule.params["start_hour"])
                    end_h = int(rule.params["end_hour"])
                    finish_hour = (finish_min % 1440) / 60.0
                    if start_h <= finish_hour < end_h:
                        self._logger.debug(
                            "P0 reject: finish at %.1fh crosses rest_window [%d:00-%d:00] cargo=%s",
                            finish_hour, start_h, end_h, cargo.get("cargo_id"),
                        )
                        skip_for_rest = True
                        break
                    cur_hour = (sim_min % 1440) / 60.0
                    hours_until_rest = (start_h - cur_hour) % 24
                    if hours_until_rest < (total_minutes_est / 60.0) + 0.5:
                        self._logger.debug(
                            "P0 reject: too late for rest_window, cur=%.1fh finish=%.1fh cargo=%s",
                            cur_hour, finish_min, cargo.get("cargo_id"),
                        )
                        skip_for_rest = True
                        break
                    break
            if skip_for_rest:
                continue

            # ★ P1: day_specific日地理约束 — 只接受目标区域附近货源
            if day_constraint is not None:
                clat, clng, crad = day_constraint
                if geo_utils.haversine_km(dest_lat, dest_lng, clat, clng) > crad:
                    continue

            # ★ P2: 区域累计缺口升级 — 只接受去目标区域的货源
            if region_constraint is not None:
                rgn_name, rgn_strict = region_constraint
                cargo_region = self._city_at(dest_lat, dest_lng)
                if rgn_name not in cargo_region:
                    continue

            # ★ 激活 FutureIncomeEstimator ★
            future_val = self._future_estimator.estimate_arrival_value(
                dest_lat, dest_lng,
                sim_min + pickup_min + cost_time,
                sim_horizon,
            )

            s = self._scorer.score(
                cargo,
                pickup_km,
                sim_min,
                area_memory,
                preference_penalty=penalty,
                simulation_horizon_minutes=sim_horizon,
                future_value=future_val,
                gamma=driver_state.get_urgency_gamma() if driver_state else 1.0,
            )
            scored.append(s)

        scored.sort(key=lambda x: x.total_score, reverse=True)
        return scored

    # ── 记录分数样本到 area_memory ──────────────────────────────────

    def _record_score_samples(
        self,
        lat: float,
        lng: float,
        items: list[dict[str, Any]],
        checker: PreferenceChecker,
        sim_min: int,
    ) -> None:
        for item in items:
            cargo = item.get("cargo", {})
            if not isinstance(cargo, dict):
                continue
            pickup_km = float(item.get("distance_km", 0) or 0)
            if pickup_km > self._config.filters["max_pickup_km"]:
                continue
            penalty, _ = checker.check_cargo_weighted(cargo, pickup_km, sim_min, driver_state=None)
            if checker.hard_forbidden(cargo, pickup_km, sim_min):
                continue
            s = self._scorer.score(cargo, pickup_km, sim_min, self._area_memory, preference_penalty=penalty)
            start = cargo.get("start", {})
            self._area_memory.record_score_sample(
                float(start.get("lat", 0)), float(start.get("lng", 0)), s.total_score
            )

    # ── 无货源处理 ──────────────────────────────────────────────────

    def _handle_no_cargo(
        self,
        driver_id: str,
        lat: float,
        lng: float,
        sim_min: int,
        day_idx: int,
        checker: PreferenceChecker,
        daily_stats: dict[str, Any],
        stats: dict[str, Any],
        cold_start: bool = False,
    ) -> dict[str, Any]:
        hour_of_day = time_utils.sim_min_to_hour_of_day(sim_min)
        daily_rest_max = daily_stats.get("daily_rest_max", {})

        # 策略0：日特定地点
        pending = checker.get_pending_requirements(daily_stats, sim_min)
        for req in pending:
            if req.get("rule_type") == "day_specific_location" and req.get("action") == "go_to_location":
                target_lat = req.get("lat")
                target_lng = req.get("lng")
                if target_lat is not None and target_lng is not None:
                    dist = geo_utils.haversine_km(lat, lng, target_lat, target_lng)
                    if dist > 5:
                        self._logger.info(
                            "[PREF_PUSH] day_specific_location → reposition to %s (dist=%.1fkm)",
                            req.get("region_name", "?"), dist,
                        )
                        stats["reposition_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._capped_reposition(lat, lng, target_lat, target_lng)
                    else:
                        self._logger.info(
                            "[PREF_PUSH] day_specific_location → near %s, wait 120min",
                            req.get("region_name", "?"),
                        )
                        stats["wait_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._make_wait(120)

        # 策略0.5：off_days 缺口
        for rule in checker.rules:
            if rule.rule_type == "off_days":
                min_days = int(rule.params.get("min_days", 0))
                achieved = daily_stats.get("off_days", 0)
                deficit = min_days - achieved
                if deficit > 0:
                    _sim_horizon = int(self._config.future_estimation.get("simulation_duration_days", 31)) * 1440
                    remaining_days = max(1, (_sim_horizon - sim_min) // 1440)
                    if remaining_days <= deficit:
                        until_midnight = time_utils.minutes_until_next_day(sim_min)
                        self._logger.info(
                            "[OFF_DAY] no_cargo + deficit=%d remaining=%d → rest %dmin",
                            deficit, remaining_days, until_midnight,
                        )
                        stats["wait_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._make_wait(max(until_midnight, 60))

        # 策略1：rest_window 内
        for rule in checker.rules:
            if rule.rule_type == "rest_window":
                start_h = int(rule.params["start_hour"])
                end_h = int(rule.params["end_hour"])
                if start_h <= hour_of_day < end_h:
                    wait_minutes = time_utils.minutes_until_target_hour(sim_min, end_h)
                    if wait_minutes > 1440:
                        wait_minutes = 60
                    wait_minutes = max(30, wait_minutes)
                    self._logger.info(
                        "no cargo + in rest window [%s:00-%s:00] → wait %s min",
                        start_h, end_h, wait_minutes,
                    )
                    stats["wait_count"] += 1
                    self._update_avg_score(stats, 0.0)
                    return self._make_wait(wait_minutes)

        # 策略2：未达到每日最少休息时长
        longest_rest_today = daily_rest_max.get(day_idx, 0)
        for rule in checker.rules:
            if rule.rule_type == "daily_rest":
                min_hours = int(rule.params.get("min_hours", 8))
                if longest_rest_today < min_hours * 60:
                    remaining = time_utils.minutes_until_next_day(sim_min)
                    if remaining > min_hours * 60:
                        self._logger.info(
                            "no cargo + rest deficit (got %dh, need %dh) → wait until next day",
                            longest_rest_today // 60, min_hours,
                        )
                        stats["wait_count"] += 1
                        self._update_avg_score(stats, 0.0)
                        return self._make_wait(max(remaining, 60))
                    self._logger.info("no cargo + rest deficit but not enough time → wait 60min")
                    stats["wait_count"] += 1
                    self._update_avg_score(stats, 0.0)
                    return self._make_wait(60)

        # 策略3：热区迁移（冷启动跳过）
        if cold_start:
            self._logger.info("no cargo + cold_start → wait 60min (driver_queries=%d gen=%d)",
                              self._area_memory.get_driver_query_count(driver_id),
                              self._area_memory.generation)
            stats["wait_count"] += 1
            self._update_avg_score(stats, 0.0)
            return self._make_wait(60)

        current_heat = self._area_memory.get_heat(lat, lng)
        hot_zones = self._area_memory.suggest_reposition(lat, lng, max_distance_km=200.0, top_n=3)

        if hot_zones and current_heat < 0.15:
            target_lat, target_lng, heat = hot_zones[0]
            dist = geo_utils.haversine_km(lat, lng, target_lat, target_lng)
            if dist > 10 and heat > current_heat * 1.5:
                self._logger.info(
                    "no cargo + cold zone (heat=%.2f) → reposition to hot zone (%.4f,%.4f) heat=%.2f dist=%.1fkm",
                    current_heat, target_lat, target_lng, heat, dist,
                )
                stats["reposition_count"] += 1
                self._update_avg_score(stats, 0.0)
                return self._capped_reposition(lat, lng, target_lat, target_lng)

        # 策略4：短时等待
        self._logger.info("no cargo → short wait 60min (current_heat=%.2f)", current_heat)
        stats["wait_count"] += 1
        self._update_avg_score(stats, 0.0)
        return self._make_wait(60)

    # ── LLM 二选一（含 Pydantic 强类型校验）─────────────────────────

    def _llm_tiebreak(
        self,
        driver_id: str,
        status: dict[str, Any],
        top2: list[CargoScore],
        checker: PreferenceChecker,
        sim_min: int,
    ) -> dict[str, Any] | None:
        try:
            # 获取司机状态用于LLM上下文
            driver_snap = self._driver_state_tracker.get_snapshot(driver_id)
            urgency_gamma = driver_snap.get_urgency_gamma() if driver_snap else 1.0

            context = {
                "driver_id": driver_id,
                "simulation_time": time_utils.format_datetime(sim_min),
                "sim_progress_minutes": sim_min,
                "month_day": sim_min // 1440,
                "current_position": {
                    "lat": float(status.get("current_lat", 0)),
                    "lng": float(status.get("current_lng", 0)),
                },
                "completed_orders": int(status.get("completed_order_count", 0)),
                "driver_state": {
                    "urgency_gamma": round(urgency_gamma, 2),
                    "off_days_gap": (
                        f"{driver_snap.off_days_achieved}/{driver_snap.off_days_required}"
                        if driver_snap else "?"
                    ),
                    "days_since_last_off": driver_snap.days_since_last_off if driver_snap else 0,
                    "month_progress_pct": round((driver_snap.month_progress if driver_snap else 0) * 100),
                } if driver_snap else {},
                "candidates": [
                    {
                        "cargo_id": cs.cargo_id,
                        "total_score": cs.total_score,
                        "net_profit": cs.net_profit,
                        "profit_per_hour": cs.breakdown.get("profit_per_hour", 0),
                        "pickup_km": round(cs.breakdown.get("pickup_km", 0), 2),
                        "haul_km": round(cs.breakdown.get("haul_km", 0), 2),
                        "total_minutes": cs.breakdown.get("total_minutes", 0),
                        "dest_heat": cs.dest_heat_score,
                        "pref_penalty_score": cs.pref_penalty_score,
                    }
                    for cs in top2
                ],
                "driver_preferences": checker.get_rules_summary(),
            }
            prompt_text = json.dumps(context, ensure_ascii=False)
            model_resp = self._api.model_chat_completion({
                "messages": [
                    {"role": "system", "content": _TIEBREAK_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt_text},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
                "max_tokens": 256,
            })

            choices = model_resp.get("choices")
            if not isinstance(choices, list) or not choices:
                self._logger.warning("LLM tiebreak: empty choices")
                return None

            content = choices[0].get("message", {}).get("content", "")
            if not isinstance(content, str) or not content.strip():
                self._logger.warning("LLM tiebreak: empty content")
                return None

            raw = json.loads(content)

            # ★ Pydantic 强类型校验 ★
            validated = LLMTiebreakResult.model_validate(raw)

            if validated.action == "take_order":
                cargo_id = str((validated.params or {}).get("cargo_id", "")).strip()
                if not cargo_id:
                    self._logger.warning("LLM tiebreak: take_order with empty cargo_id")
                    return None
                valid_ids = {cs.cargo_id for cs in top2}
                if cargo_id not in valid_ids:
                    self._logger.warning(
                        "LLM tiebreak: cargo_id=%s not in top2 %s, fallback",
                        cargo_id, valid_ids,
                    )
                    return None
                self._logger.info("LLM tiebreak → take_order %s", cargo_id)
                return self._make_take_order(cargo_id)

            elif validated.action == "wait":
                dur = int((validated.params or {}).get("duration_minutes", 60))
                dur = max(30, min(dur, 1440))
                self._logger.info("LLM tiebreak → wait %d min", dur)
                return self._make_wait(dur)

        except (ValidationError, json.JSONDecodeError) as exc:
            self._logger.warning("LLM tiebreak validation error: %s", exc)
        except Exception as exc:
            self._logger.warning("LLM tiebreak unexpected error: %s", exc)
        return None

    # ── 动作构造 ─────────────────────────────────────────────────────

    @staticmethod
    def _make_take_order(cargo_id: str) -> dict[str, Any]:
        return {"action": "take_order", "params": {"cargo_id": cargo_id}}

    @staticmethod
    def _make_wait(duration_minutes: int) -> dict[str, Any]:
        return {"action": "wait", "params": {"duration_minutes": max(30, int(duration_minutes))}}

    def _capped_reposition(
        self, current_lat: float, current_lng: float, target_lat: float, target_lng: float
    ) -> dict[str, Any]:
        dist = geo_utils.haversine_km(current_lat, current_lng, target_lat, target_lng)
        max_dist = float(self._config.filters.get("reposition_max_km", 300.0))
        if dist <= max_dist:
            return {"action": "reposition", "params": {"latitude": target_lat, "longitude": target_lng}}
        ratio = max_dist / dist
        mid_lat = current_lat + (target_lat - current_lat) * ratio
        mid_lng = current_lng + (target_lng - current_lng) * ratio
        self._logger.info(
            "[REPOSITION] capped %.0fkm -> %.0fkm (%.4f,%.4f)",
            dist, max_dist, mid_lat, mid_lng,
        )
        return {"action": "reposition", "params": {"latitude": mid_lat, "longitude": mid_lng}}

    # ── 统计辅助 ─────────────────────────────────────────────────────

    def _update_avg_score(self, stats: dict[str, Any], score: float) -> None:
        n = max(stats["total_decisions"], 1)
        stats["avg_score"] = round(
            (stats["avg_score"] * (n - 1) + score) / n, 4
        )


def _days_since_last_off(daily_stats: dict[str, Any], current_day: int) -> int:
    """计算距离上一次休息日过去了多少天。如果没有休息过，返回一个大值。"""
    daily_active = daily_stats.get("daily_active", {})
    for d in range(current_day - 1, -1, -1):
        if daily_active.get(d, 0) == 0:
            return current_day - d
    return current_day + 1
