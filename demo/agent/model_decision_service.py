"""模型决策服务：Rule-First架构 — 规则引擎主导，LLM仅在Top-2难分高下时辅助二选一。
v2: 接入 RiskChecker 安检 + FutureIncomeEstimator 未来价值。"""

from __future__ import annotations

import logging
from typing import Any

from simkit.ports import SimulationApiPort
from agent.strategy.dispatcher import DecisionDispatcher
from agent.config import AgentConfig, DEFAULT_CONFIG


class ModelDecisionService:
    """基于Rule-First的单步决策：多层过滤→评分→规则决策→LLM兜底→RiskChecker安检。"""

    def __init__(self, api: SimulationApiPort, config: AgentConfig | None = None) -> None:
        self._api = api
        self._config = config or DEFAULT_CONFIG
        self._dispatcher = DecisionDispatcher(api, self._config)
        self._logger = logging.getLogger("agent.decision_service")

    def decide(self, driver_id: str) -> dict[str, Any]:
        action = self._dispatcher.decide(driver_id)
        self._logger.info(
            "decision driver=%s action=%s params=%s",
            driver_id,
            action.get("action"),
            action.get("params"),
        )
        return action

    def get_summary(self) -> dict[str, Any]:
        """获取所有司机的决策统计。"""
        return self._dispatcher.get_decision_summary()
