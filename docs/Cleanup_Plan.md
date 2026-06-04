# Cleanup Plan

**基于：** Architecture_Review.md / ROI_Ranking.md / Code_Diet_Audit.md
**原则：** 不修改逻辑、不重构、不优化。仅清理已确认安全删除的代码。
**目标：** 净删除 ~200 行，减少维护面，消除误导性死代码。

---

# Safe Delete（低风险）

证据标准：全代码库 grep 确认零调用点，或值永为常量对系统行为零影响。

---

## SD-1：geo_utils 中 3 个从未调用的函数

**文件：** `agent/utils/geo_utils.py`

| 函数 | 行号 | 删除原因 | 证据 |
|------|------|---------|------|
| `in_bounding_box()` | 41-49 | 全代码库零调用 | `grep -rn "in_bounding_box" agent/` 仅返回定义行 |
| `midpoint()` | 52-53 | 全代码库零调用 | `grep -rn "midpoint(" agent/` 仅返回定义行 |
| `region_center()` | 75-77 | 功能被 `REGION_COORDINATES.get(name)` 直接替代 | `grep -rn "region_center(" agent/` 仅返回定义行 |

**预计删除行数：** 15 行
**回滚方式：** `git revert`

---

## SD-2：time_utils 中 4 个从未调用的函数

**文件：** `agent/utils/time_utils.py`

| 函数 | 行号 | 删除原因 | 证据 |
|------|------|---------|------|
| `sim_min_to_wall_time()` | 24-25 | 全代码库零调用 | `grep -rn "sim_min_to_wall_time" agent/` 仅返回定义行 |
| `day_bounds()` | 28-30 | 全代码库零调用 | `grep -rn "day_bounds(" agent/` 仅返回定义行 |
| `is_night_time()` | 33-35 | 功能被 `hour_in_range()` 替代 | `grep -rn "is_night_time(" agent/` 仅返回定义行 |
| `minutes_until_target_hour_next()` | 55-61 | 功能被 `minutes_until_target_hour()` 替代 | `grep -rn "minutes_until_target_hour_next" agent/` 仅返回定义行 |

**预计删除行数：** 20 行
**回滚方式：** `git revert`

---

## SD-3：PreferenceChecker 中 3 个从未调用的 evaluate_*_penalty 方法

**文件：** `agent/scoring/preference_scorer.py`

| 方法 | 行号 | 删除原因 | 证据 |
|------|------|---------|------|
| `evaluate_daily_rest_penalty()` | 600-608 | 全代码库零调用 | `grep -rn "evaluate_daily_rest_penalty" agent/` 仅返回定义行 |
| `evaluate_off_days_penalty()` | 610-618 | 全代码库零调用 | `grep -rn "evaluate_off_days_penalty" agent/` 仅返回定义行 |
| `evaluate_min_days_in_region_penalty()` | 620-628 | 全代码库零调用 | `grep -rn "evaluate_min_days_in_region_penalty" agent/` 仅返回定义行 |

**功能已被替代：** dispatcher 实际使用 `get_pending_requirements()` + DriverStateTracker gamma 体系完成相同功能。这三个方法是早期迭代中"决策后结算预估"的残留。

**预计删除行数：** 30 行
**回滚方式：** `git revert`

---

## SD-4：DecisionDispatcher._find_safe_location()

**文件：** `agent/strategy/dispatcher.py`
**行号：** 475-494

**删除原因：** 设计目的是在 day_specific_avoid 日找到最近的安全城市坐标，但从未被 dispatcher 任何 Phase 调用。`grep -rn "_find_safe_location" agent/` 仅返回定义行。

**预计删除行数：** 20 行
**回滚方式：** `git revert`

---

## SD-5：DriverStateSnapshot 中 2 个永为 0 的死字段

**文件：** `agent/memory/driver_state.py`

| 字段 | 行号 | 删除原因 | 证据 |
|------|------|---------|------|
| `consecutive_rest_window_violations` | 30 | 声明后从不被 `DriverStateTracker.update()` 更新，也从不被任何代码读取 | `grep -rn "consecutive_rest_window_violations" agent/` 仅返回定义行 + dataclass 声明 |
| `consecutive_long_deadhead` | 31 | 仅在 `get_gamma()` line 71 被读取，但 `update()` 从不更新其值，始终为 0 | 对 gamma 公式的实际贡献为 `+ 0.2 * 0 = 0`，等价于不存在 |

**连带影响：** `get_gamma()` line 71 中 `max_pickup_km` case 的 `1.0 + 0.2 * self.consecutive_long_deadhead` → 应简化为 `1.0`。

**预计删除行数：** 5 行（2 字段声明 + line 71 中移除 `+ 0.2 * ...`）
**回滚方式：** `git revert`

---

## SD-6：PreferenceChecker.check_cargo() —— 被 check_cargo_weighted() 完全替代

**文件：** `agent/scoring/preference_scorer.py`
**行号：** 396-414

**删除原因：** `check_cargo` 和 `check_cargo_weighted` 的唯一区别是后者在 line 432-433 乘了 gamma。当 `driver_state=None` 时，`check_cargo_weighted` 等价于 `check_cargo`（gamma 默认为 1.0）。

**唯一调用点：** `dispatcher.py:867` — `_record_score_samples()` 中。

**方案：** 将 dispatcher.py:867 的 `checker.check_cargo(cargo, pickup_km, sim_min)` 改为 `checker.check_cargo_weighted(cargo, pickup_km, sim_min, driver_state=None)`，然后删除 `check_cargo()`。

**预计删除行数：** 18 行（方法定义） + 1 行（调用点修改）
**回滚方式：** `git revert`

---

## SD-7：test_optimization.py —— 临时冒烟测试脚本

**文件：** `agent/test_optimization.py`

**删除原因：**
- 包含硬编码绝对路径 `f:/天池大赛/demo_docs_release_20260529/demo` (line 4)
- 是开发期快速冒烟测试，不是正式测试
- 正式测试位于 `tests/test_modules.py`，覆盖相同功能
- 文件不在 `tests/` 目录下，不会被 pytest 自动发现

**预计删除行数：** 80 行（整个文件）
**回滚方式：** `git revert`

---

## SD-8：RiskChecker._validate_reposition 中重复的距离截断

**文件：** `agent/strategy/risk_checker.py`
**行号：** 134-142（校验 1 — 距离截断）

**删除原因：** `_capped_reposition()` (dispatcher.py:1118-1132) 已经在构造动作时做了距离截断。RiskChecker 不应该再做第二次。这不影响安全性——dispatcher 永远不会构造超过 300km 的 reposition 动作。

**证据：** dispatcher 中所有 reposition 动作都经过 `_capped_reposition()` 发出。RiskChecker 的二重截断是 pure defense-in-depth，但 defense-in-depth 的对象（dispatcher 自己）不会产生超限动作。

**预计删除行数：** 8 行
**回滚方式：** `git revert`

---

# Medium Risk（中风险）

---

## MR-1：rest_window 检测循环提取为公共 helper

**文件：**
- `agent/scoring/preference_scorer.py`
- `agent/strategy/dispatcher.py`
- `agent/strategy/risk_checker.py`

**涉及位置（7 处）：**

| # | 文件 | 函数 | 行号 |
|---|------|------|------|
| 1 | dispatcher.py | `_check_mandatory_rest` | 582-588 |
| 2 | dispatcher.py | `_filter_and_score` (P0) | 790-811 |
| 3 | dispatcher.py | `_handle_no_cargo` (策略1) | 938-953 |
| 4 | preference_scorer.py | `check_transit_violation` | 650-662 |
| 5 | risk_checker.py | `_validate_wait` | 98-111 |
| 6 | risk_checker.py | `_validate_reposition` | 157-170 |
| 7 | risk_checker.py | `_safe_wait` | 186-192 |

**风险：** 每处对 rest_window 的边界判断使用了略微不同的条件（半开区间 `[start, end)` vs `hour_in_range(start, end-0.5)` vs `start <= h < end`）。提取 helper 时如果选错了统一的边界条件，可能出现漏判或误判。

**验证方式：**
1. 提取后在 D002 上运行 30 天仿真
2. 对比提取前后的 `actions_*.jsonl` 和 `monthly_income_202603.json` — 必须逐字节一致
3. 特别检查 rest_window violations 数量不变

**预计净减少行数：** ~25 行（删除 40 行重复 + 新增 15 行 helper）

---

## MR-2：CargoScorer 中 Magic Number 移入 AgentConfig

**文件：** `agent/scoring/cargo_scorer.py`
**涉及值：**

| 行号 | 值 | 含义 |
|------|-----|------|
| 96 | `-500.0, 3000.0` | 利润归一化范围 (low, high) |
| 97 | `-50.0, 500.0` | 时薪归一化范围 |
| 120 | `0.0, 2000.0` | 未来价值归一化范围 |
| 175-183 | `50, 800, 200, 500` | 距离合理性评分区间 |

**风险：** 这些值直接影响评分分布。移入 config 后如果误改默认值，会导致评分排序变化 → 决策变化 → 收益变化。

**验证方式：** 移入 config 时保持默认值与当前硬编码值完全一致。运行 A/B 对比仿真，确认 `monthly_income_202603.json` 完全一致。

**预计净减少行数：** 0（不删代码，只是搬家）

---

## MR-3：Dispatcher Phase 数量标注与结构化日志

**文件：** `agent/strategy/dispatcher.py`
**涉及范围：** `decide()` line 131-378

**风险：** 新增日志语句不影响逻辑。但如果在 11 个 return 点处插日志出现笔误（如 log 了错误的 Phase 编号），会误导调试。

**验证方式：** 代码 review 每条日志的 Phase 编号与所在位置一致。

**预计新增行数：** ~15 行（日志语句）

---

# High Risk（高风险）

以下代码属于核心决策链路，任何清理都存在改变行为的风险。**不建议在无充分测试覆盖的情况下触碰。**

---

## HR-1：DecisionDispatcher.decide() 主决策流水线

**文件：** `agent/strategy/dispatcher.py` line 131-378

**风险说明：** 14 个 Phase 顺序执行，每个 Phase 的触发条件和顺序是经过多次迭代调优的结果。改动任何一个 Phase 的条件、顺序、或 return 行为都可能改变决策结果。

**具体高风险点：**
- Phase 1 (gamma 强制休息，line 154-165)：`off_days_gamma > 4.0` 和 `hour_of_day < 8` 两个条件
- Phase 2 (强制休息，line 167-175)：`_check_mandatory_rest()` 内部有 3 种规则类型的复杂分支
- Phase 7 (过滤+评分，line 244-251)：硬截断、软罚分、P0/P1/P2 三层过滤的顺序
- Phase 11 (边际分数，line 284-311)：OpportunityEvaluator 的等待/迁移决策

**建议：** 不改。当前 14,905 的收益是从这个流程中跑出来的。任何重排或简化都应以充分的 A/B 测试为前提。

---

## HR-2：DriverStateTracker.get_urgency_gamma() 及 _off_days_gamma()

**文件：** `agent/memory/driver_state.py` line 36-107

**风险说明：** Gamma 公式 (`1.0 + gap_ratio² * 5.0 + time_pressure¹·⁵ * 6.0 + days_since_last_off * 0.5`) 是当前系统"月末驱动休息"的唯一机制。改动 gamma 公式会影响 Phase 1 的触发时机、Phase 7 的评分权重、以及 `hard_forbidden` 的经济阈值。

**具体高风险点：**
- `_off_days_gamma()` (line 87-107)：指数膨胀曲线
- `get_urgency_gamma()` (line 36-58)：综合所有偏好的全局膨胀
- `get_gamma()` (line 60-85)：per-rule 膨胀

**已知问题但不应在清理阶段修复：** `time_pressure > 1.0` 时 gamma 爆炸但约束已不可行（见 P1-3）。这是一个逻辑改进，不是清理。

**建议：** 不改。

---

## HR-3：PreferenceChecker._check_cargo_rule()

**文件：** `agent/scoring/preference_scorer.py` line 558-598

**风险说明：** 这是所有候选货源合规检查的单一入口。`hard_forbidden()`、`check_cargo_weighted()`、`check_cargo()` 全部调用它。缺少 `daily_rest` case 是一个已知问题（见 P1-1），但增加 case 是逻辑改动，不是清理。

**建议：** 不改。等 P1-1 修复时一并处理。

---

## HR-4：FutureIncomeEstimator.estimate_arrival_value()

**文件：** `agent/planner/future_income_estimator.py` line 31-73

**风险说明：** 虽然当前 FutureValue 只占评分 8%，但它对长途路线选择有定向作用。删除或简化此函数会改变候选货源的排序。

**建议：** 不改。等 FutureValue V2 重构时一并替换。

---

## HR-5：CargoScorer.score() 评分公式

**文件：** `agent/scoring/cargo_scorer.py` line 50-163

**风险说明：** 7 个评分维度 × 7 个权重 = 49 维参数空间。当前权重组合是从多次迭代中收敛的。改任何一个权重都会改变候选排序。

**建议：** 不改。

---

# Recommended Cleanup Order

按"单位时间内的代码健康度提升"排序：

| 顺序 | 项目 | 风险 | 预计删行 | 时间 | 收益 |
|------|------|------|---------|------|------|
| **1** | SD-7: 删除 test_optimization.py | 极低 | 80 | 5 min | 消除硬编码路径 + 最大单文件删除 |
| **2** | SD-1→SD-6 全部 Safe Delete 一次性执行 | 极低 | 108 | 30 min | 消除 85% 的死代码 |
| **3** | SD-8: RiskChecker 重复距离截断 | 极低 | 8 | 10 min | 消除 dispatcher/risk_checker 重复逻辑 |
| **4** | MR-2: Magic Number → config | 中 | 0 | 30 min | 消除散落常量，集中管理 |
| **5** | MR-1: rest_window helper 提取 | 中 | -25 | 60 min | 7 维护点 → 1 维护点 |
| **6** | MR-3: Phase 结构化日志 | 中 | +15 | 20 min | 提升可调试性 |

---

## 执行建议

**第 1 步 + 第 2 步合并执行（一次性 Safe Delete）：**

删除 11 个死函数 + 2 个死字段 + 合并 check_cargo → 净删除 ~116 行。跑一次 D001/D002 仿真确认 `monthly_income_202603.json` 与删除前逐字节一致。

**如果结果一致 → 提交。如果不一致 → 回滚，逐个排查。**

预计总时间：2 小时完成全部 6 步。

---

## 不建议清理的清单

以下虽在审计中被标记为问题，但属于"逻辑改进"而非"代码清理"，不应在 Cleanup 阶段处理：

| 问题 | 原因 | 应在何时处理 |
|------|------|------------|
| P0-1: daily_rest 阈值 `>60` → `>0` | 改逻辑，不是删代码 | Architecture_Review Phase 1 |
| P0-2: max_pickup_km 配置覆盖偏好 | 改逻辑 | Architecture_Review Phase 1 |
| P0-3: day_specific_location 多站路线 | 新增功能 | Architecture_Review Phase 2 |
| P0-4: 僵尸坐标检查激活 | 激活代码，不是删除 | Architecture_Review Phase 1 |
| P1-2: rest_window 提前预警 | 新增逻辑 | Architecture_Review Phase 2 |
| P1-3: Gamma 可行性 gate | 新增模块 | Architecture_Review Phase 2 |
| 评分权重调优 | 参数优化 | Architecture_Review Phase 3 |
| FutureValue V2 | 架构升级 | Architecture_Review Phase 3 |
