"""Agent配置管理：集中管理所有可调参数，便于实验和调优。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentConfig:
    """Agent全局配置"""

    # 评分权重配置
    scorer_weights: dict[str, float] = field(default_factory=lambda: {
        "w_profit": 0.25,
        "w_efficiency": 0.22,
        "w_dest_heat": 0.20,
        "w_pref_penalty": 0.12,
        "w_time_risk": 0.08,
        "w_future_value": 0.08,
        "w_distance_bonus": 0.05,
    })

    # 决策阈值
    decision_thresholds: dict[str, float] = field(default_factory=lambda: {
        "min_score_to_consider": 0.05,
        "clear_winner_ratio": 1.25,
        "llm_tiebreak_gap": 0.12,
        "llm_min_score": 0.30,
        "marginal_score_threshold": 0.25,
    })

    # LLM调用控制
    llm_control: dict[str, Any] = field(default_factory=lambda: {
        "max_calls_per_driver": 0,  # 禁用LLM，使用纯规则引擎（恢复141908最优版本行为）
        "temperature": 0.3,
        "max_tokens": 256,
    })

    # 区域记忆配置
    area_memory: dict[str, Any] = field(default_factory=lambda: {
        "resolution": 0.1,
        "decay_factor": 0.995,
        "decay_interval": 100,
        "min_heat_threshold": 0.15,
        "max_reposition_distance": 200.0,
        "generation_ttl": 500,
    })

    # 过滤规则
    filters: dict[str, Any] = field(default_factory=lambda: {
        "max_pickup_km": 200.0,
        "min_haul_distance": 50.0,
        "max_haul_distance": 800.0,
        "reposition_max_km": 300.0,
        "cost_per_km": 1.5,
        "speed_km_per_hour": 60.0,
        "time_value_per_minute": 1.0,
    })

    # 等待策略
    wait_strategy: dict[str, Any] = field(default_factory=lambda: {
        "default_wait_minutes": 60,
        "min_wait_minutes": 30,
        "max_wait_minutes": 1440,
    })

    # 期望账：未来收益预估
    future_estimation: dict[str, Any] = field(default_factory=lambda: {
        "look_ahead_horizon_days": 7,
        "cold_zone_heat_threshold": 0.10,
        "cold_zone_penalty": 500.0,
        "time_value_per_minute": 1.0,
        "simulation_duration_days": 31,
        "future_value_discount": 0.5,
    })

    # 博弈账：机会成本评估
    opportunity: dict[str, Any] = field(default_factory=lambda: {
        "min_score_to_consider_wait": 0.20,
        "wait_cost_per_minute": 1.5,
        "reposition_gain_threshold": 1.3,
        "max_wait_for_better_minutes": 180,
    })

    # 偏好反馈：累计推动
    preference_feedback: dict[str, Any] = field(default_factory=lambda: {
        "enable_cumulative_push": True,
        "urgency_start_day": 10,
        "region_bonus_max": 0.15,
        "off_day_wait_tendency_day": 25,
    })

    # 动态K
    dynamic_k: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "default": 100,
        "high_density_threshold": 100,
        "high_density_k": 50,
        "low_density_threshold": 20,
        "low_density_k": 200,
        "no_candidate_k": 200,
        "min_candidates_before_boost": 3,
    })

    # 冷启动保护
    cold_start: dict[str, Any] = field(default_factory=lambda: {
        "min_queries_per_driver": 5,
        "min_total_generation": 10,
    })

    @classmethod
    def from_dict(cls, config_dict: dict[str, Any]) -> "AgentConfig":
        cfg = cls()
        for key, value in config_dict.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_weights": self.scorer_weights,
            "decision_thresholds": self.decision_thresholds,
            "llm_control": self.llm_control,
            "area_memory": self.area_memory,
            "filters": self.filters,
            "wait_strategy": self.wait_strategy,
            "future_estimation": self.future_estimation,
            "opportunity": self.opportunity,
            "preference_feedback": self.preference_feedback,
            "dynamic_k": self.dynamic_k,
            "cold_start": self.cold_start,
        }


DEFAULT_CONFIG = AgentConfig()
