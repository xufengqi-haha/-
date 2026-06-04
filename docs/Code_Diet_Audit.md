# Code Diet Audit — 架构减肥审计

**审计范围：** `demo/agent/` 全部 Python 模块
**审计日期：** 2026-06-04
**原则：** 不优化算法、不修改逻辑、只找该删的代码

---

# Dead Code

## DC-1：geo_utils 中 3 个从未调用的函数

**文件：** `agent/utils/geo_utils.py`
**函数：**
- `in_bounding_box()` — line 41-49
- `midpoint()` — line 52-53
- `region_center()` — line 75-77

**原因：** 三个函数均只有定义，agent 全代码库中无任何调用点。`region_center` 的功能被 `REGION_COORDINATES.get(name)` 直接替代。

**建议删除：** 是。共 ~15 行。

---

## DC-2：time_utils 中 4 个从未调用的函数

**文件：** `agent/utils/time_utils.py`
**函数：**
- `sim_min_to_wall_time()` — line 24-25
- `day_bounds()` — line 28-30
- `is_night_time()` — line 33-35
- `minutes_until_target_hour_next()` — line 55-61

**原因：** 全代码库中无调用点。`is_night_time` 的功能被 `hour_in_range` 替代，`minutes_until_target_hour_next` 被 `minutes_until_target_hour` 替代。均为早期开发迭代的残留。

**建议删除：** 是。共 ~20 行。

---

## DC-3：PreferenceChecker 中 3 个 "evaluate_*_penalty" 方法

**文件：** `agent/scoring/preference_scorer.py`
**函数：**
- `evaluate_daily_rest_penalty()` — line 600-608
- `evaluate_off_days_penalty()` — line 610-618
- `evaluate_min_days_in_region_penalty()` — line 620-628

**原因：** 三个方法在全代码库中零调用。它们的设计意图是在决策后做"如果现在结算，罚分是多少"的预估，但 dispatcher 实际使用的是 `get_pending_requirements()` + gamma 体系。功能已被替代。

**建议删除：** 是。共 ~30 行。

---

## DC-4：PreferenceChecker 中 2 个僵尸坐标检查方法

**文件：** `agent/scoring/preference_scorer.py`
**函数：**
- `check_position_day_avoid()` — line 514-532
- `check_cargo_dest_avoid()` — line 534-556

**原因：** v2 迭代中实现并测试通过，但 dispatcher 从未集成调用。详见 Architecture_Review.md P0-4。

**特殊处理：** 这两个函数是**有用的代码但未被连接**。不应删除——应该激活（P0-4）。如果决定不激活，才应删除。

**建议删除：** 否。应在 P0-4 中激活。

---

## DC-5：PreferenceChecker.check_cargo() —— 被 check_cargo_weighted() 完全替代

**文件：** `agent/scoring/preference_scorer.py`
**函数：** `check_cargo()` — line 396-414

**原因：** `check_cargo` 和 `check_cargo_weighted` 的唯一区别是 gamma 乘法（line 432-433）。`check_cargo` 等价于 `check_cargo_weighted(cargo, pickup, sim_min, driver_state=None)`。

**当前唯一非测试调用点：** `dispatcher.py:867` — `_record_score_samples()` 中用于记录分数样本。可以将此调用改为 `check_cargo_weighted(cargo, pickup_km, sim_min, driver_state=None)` 后删除 `check_cargo`。

**建议删除：** 是。共 ~20 行（含调用点修改 1 行）。

---

## DC-6：DecisionDispatcher._find_safe_location()

**文件：** `agent/strategy/dispatcher.py`
**函数：** `_find_safe_location()` — line 475-494

**原因：** 全代码库中零调用。设计目的是在 day_specific_avoid 日找到最近的安全城市坐标，但从未被 dispatcher 任何决策阶段调用。

**建议删除：** 是。共 ~20 行。

---

## DC-7：DriverStateSnapshot 中 2 个永不被更新的字段

**文件：** `agent/memory/driver_state.py`
**字段：**
- `consecutive_rest_window_violations: int = 0` — line 30
- `consecutive_long_deadhead: int = 0` — line 31

**原因：**
- `consecutive_rest_window_violations` — 声明后从不被 `DriverStateTracker.update()` 更新，也从不被任何地方读取。纯死字段。
- `consecutive_long_deadhead` — 在 `get_gamma()` line 71 被读取（`gamma = 1.0 + 0.2 * self.consecutive_long_deadhead`），但 `DriverStateTracker.update()` 从不更新它的值。它永远是 0，对 gamma 贡献永远是 0。等价于死字段。

**建议删除：** 是。字段声明 2 行 + `get_gamma()` 中 `max_pickup_km` case 的 `+ 0.2 * self.consecutive_long_deadhead` 应改为固定值或删除。

---

## DC-8：test_optimization.py —— 临时冒烟测试脚本

**文件：** `agent/test_optimization.py`

**原因：** 包含硬编码的绝对路径 `f:/天池大赛/demo_docs_release_20260529/demo`（line 4），是开发过程中的临时冒烟测试脚本。正式测试在 `tests/test_modules.py` 中。

**建议删除：** 是。整个文件 ~80 行。

---

# Duplicate Logic

## DL-1：rest_window 检测循环出现 7 次

**模式：** `for rule in checker.rules: if rule.rule_type == "rest_window": ...`

**出现位置：**

| # | 文件 | 函数 | 行号 | 执行动作 |
|---|------|------|------|---------|
| 1 | dispatcher.py | `_check_mandatory_rest` | 582-588 | 强制等待到窗口结束 |
| 2 | dispatcher.py | `_filter_and_score` (P0) | 790-811 | 跳过货源 |
| 3 | dispatcher.py | `_handle_no_cargo` (策略1) | 938-953 | 等待到窗口结束 |
| 4 | preference_scorer.py | `check_transit_violation` | 650-662 | 标记货源为硬违规 |
| 5 | risk_checker.py | `_validate_wait` | 98-111 | 延长等待时长 |
| 6 | risk_checker.py | `_validate_reposition` | 157-170 | 拦截空驶 |
| 7 | risk_checker.py | `_safe_wait` | 186-192 | 延长 fallback 等待 |

**根因：** 每个检查点独立实现了"找到 rest_window 规则 → 判断时间重叠 → 行动"的三步模式。没有抽取公共的 `_find_rest_window_rule()` 或 `_is_in_rest_window()` helper。

**建议：** 在 `PreferenceChecker` 中增加一个 `get_rest_window(checker) -> (start_h, end_h) | None` 方法。7 处调用点改为：`rw = checker.get_rest_window(); if rw and overlaps(...): act()`。约可消除 40 行重复代码。

**建议保留：** 各处不同的"行动"逻辑（等待 vs 过滤 vs 拦截）无法合并。只合并检测部分。

---

## DL-2：_capped_reposition 与 _validate_reposition 重复距离截断

**逻辑 A：** `dispatcher.py:1118-1132` `_capped_reposition()` — 将空驶目标截断到 `reposition_max_km`（300km）

**逻辑 B：** `risk_checker.py:134-142` `_validate_reposition()` 校验 1 — 将空驶目标截断到 300km（硬编码，非配置）

**根因：** 同一个截断逻辑在两个地方独立实现。dispatcher 在构造动作时截断，risk_checker 在安检时再次截断。两次截断用不同的距离上限获取方式（前者从 config 读取 300，后者硬编码 300）。

**建议：** risk_checker 不应负责距离截断（这是动作构造的职责）。删除 `_validate_reposition` 中的校验 1，trust dispatcher 已经截断过的动作。

---

## DL-3：check_cargo 与 check_cargo_weighted 功能重叠

**逻辑 A：** `preference_scorer.py:396-414` `check_cargo()` — 遍历规则，调用 `_check_cargo_rule`，求和

**逻辑 B：** `preference_scorer.py:416-436` `check_cargo_weighted()` — 遍历规则，调用 `_check_cargo_rule`，乘 gamma，求和

**区别：** 仅 line 432-433 的 gamma 乘法。A 是 B 在 `gamma=1.0` 时的特殊情况。

**建议：** 删除 `check_cargo()`。唯一调用点 `dispatcher.py:867` 改为 `check_cargo_weighted(cargo, pickup_km, sim_min, driver_state=None)`。

---

## DL-4：forbidden_region_cargo 与 forbidden_region_entry 共用同一个 case 分支

**位置：** `preference_scorer.py:584-589`

```python
elif rule.rule_type in ("forbidden_region_cargo", "forbidden_region_entry"):
    region = str(rule.params.get("region", ""))
    start_city = str((cargo.get("start") or {}).get("city", "") or "")
    end_city = str((cargo.get("end") or {}).get("city", "") or "")
    if region and (region in start_city or region in end_city):
```

**问题：** `forbidden_region_entry` 的语义是"禁止**进入**该区域"——意味着即使起点不在该区域、仅终点在该区域，也算违规。`forbidden_region_cargo` 的语义是"不接涉及该区域的**货源**"——起点或终点任一涉及就算。

两者在当前实现中行为一致（都用 `in start_city or in end_city`），但语义不同。如果未来要区分（如 forbidden_region_entry 应该检查当前是否已在区域内），这个合并分支就需要拆分。当前不算 bug，但是一种隐式耦合。

**建议：** 暂不拆分，但在 case 分支加注释标注两者的语义差异。

---

# Architecture Debt

## AD-1：Rest Window 检测无统一抽象

**问题：** 7 处代码各自独立实现对 rest_window 的时间重叠判断，每处使用略微不同的边界条件：
- dispatcher P0（line 795）：`start_h <= finish_hour < end_h` — 半开区间
- preference_scorer transit（line 656）：`hour_in_range(float(h), start_h, end_h - 0.5)` — 减去 0.5h 的缓冲区
- risk_checker reposition（line 164）：`hour_in_range(h, start_h, end_h - 0.5)` — 同上
- dispatcher mandatory_rest（line 586）：`start_h <= hour_of_day < end_h` — 半开区间
- dispatcher no_cargo（line 942）：`start_h <= hour_of_day < end_h` — 半开区间

**同一概念有三种不同的边界判断方式。** 任何对 rest_window 语义的修改需要同步 7 处。

**复杂度：** 高
**维护成本：** 每次修改 rest_window 相关逻辑需要改 4-7 个位置
**建议：** 提取 `PreferenceChecker.is_in_rest_window(sim_min) -> bool` 和 `PreferenceChecker.minutes_until_rest_window_end(sim_min) -> int`

---

## AD-2：Dispatcher 的 14 Phase 决策流水线过于庞大

**问题：** `DecisionDispatcher.decide()` — 从 line 131 到 line 378，共 248 行，包含 14 个 Phase。每个 Phase 有独立的条件分支和 return 路径。总共有 **11 个独立的 return 点**。

**复杂度：** 高。理解完整决策流程需要追踪 14 个顺序 Phase × 每个 Phase 内的 2-4 个分支 = 约 40 条决策路径。
**维护成本：** 新增一个决策 Phase 需要理解前 13 个 Phase 的交互。调试需要打 log 确认走了哪个 Phase。
**建议：** 不拆分（保持顺序执行的清晰性），但应增加结构化日志标明每个 decision 走的是哪个 Phase。

---

## AD-3：PreferenceParser 双通道解析（正则 + LLM）

**问题：** `PreferenceParser.parse()` 先用 12 条正则匹配（确定性、零 token），失败后 fallback 到 `_llm_parse_preference()`（消耗 API 调用）。两条通道返回同一个 `PreferenceRule` 数据结构，但 LLM 通道可以返回正则不支持的新 `rule_type`。

**复杂度：** 中。新增规则类型只需加正则可完全避免 LLM 调用。但正则覆盖面有限，LLM 是最可靠的兜底。
**维护成本：** 两条解析通道需要维护同步。LLM system prompt（line 338-352）中列出的规则类型列表需要与正则 `_PATTERNS` 保持同步。
**建议：** 当前架构合理。在 `_llm_parse_preference` 的 system prompt 中增加一条注释标明"此列表需与 _PATTERNS 的 rule_type 值保持同步"。

---

## AD-4：AreaMemory 的 decay 机制从未可靠触发

**问题：** `AreaMemory._apply_decay()` 每 100 代触发一次（`decay_interval=100`），但对长时间运行的仿真（数百到数千步），`gen_diff` 增长使 `decay ** decay_steps` 迅速趋向 0，大量网格被删除（`count < 0.1` 即删）。

**结果：** 在仿真后期（>1000 步），早期探索的区域数据可能已被完全清除。新查询到的区域需要从头积累。这对 FutureIncomeEstimator（依赖历史数据）构成信息丢失。

**复杂度：** 中
**维护成本：** 低（当前 decay 参数 `0.995` 和 interval `100` 未经调优验证）
**建议：** 当前 decay 行为可能是无意的。如果希望保留长期记忆，应改为不删除网格而是将 count 下限设为 0.5（保持查询返回默认值的状态）。

---

# Magic Number 清单

| # | 位置 | 值 | 语义 | 风险 |
|---|------|---|------|------|
| M1 | dispatcher.py:155 | `4.0` | off_day gamma 触发 Phase 1 阈值 | 与 driver_state gamma 公式耦合 |
| M2 | dispatcher.py:155 | `8` | Phase 1 只在早上 8 点前触发 | 月末可能需要全天任意时刻触发 |
| M3 | dispatcher.py:599 | `60` | daily_rest deficit 容忍分钟数 | **P0-1: 应为 0** |
| M4 | dispatcher.py:740 | `200.0` | 全局空驶硬截断 km | **P0-2: 应按司机偏好个性化** |
| M5 | preference_scorer.py:414 | `50000.0` | 单货源罚分上限 | 安全值，影响小 |
| M6 | preference_scorer.py:492 | `30` | reposition 禁区半径 km | 与评测器 bounding box 不对齐 |
| M7 | cargo_scorer.py:96 | `-500.0, 3000.0` | 利润归一化范围 | 调参可影响评分分布 |
| M8 | future_income_estimator.py:55 | `800.0` | 默认平均运价 (分) | 冷启动期间唯一返回值 |
| M9 | opportunity_evaluator.py:49 | `3` | "高分直接接"的倍数阈值 | `min_score_to_wait * 3` |
| M10 | dispatcher.py:448 | `0.85` | prefer_wait 偏好推动折扣 | 无理论依据 |

---

# 临时 Debug / 开发期残留

| # | 位置 | 内容 | 建议 |
|---|------|------|------|
| T1 | agent/test_optimization.py | 硬编码路径的冒烟测试 | **删除**，已有 tests/test_modules.py |
| T2 | preference_scorer.py:26-28 | 注释列出了所有软偏好类型 | 保留作为文档，但标注为"已实现列表" |
| T3 | dispatcher.py:585 | `# ★ 使用半开区间 [start_h, end_h)：6:00整不算休息窗口内` | 保留（说明边界条件的设计决策） |

---

# 司机特判逻辑

**结论：agent/ 目录中无任何 D001/D002 硬编码。** 唯一的 driver_id 引用仅在 `tests/test_modules.py:80-90` 的测试断言中（`assert len(rules_d001) == 5`）。

偏好解析、决策调度、规则检查全部基于运行时 `get_driver_status()` 返回的 `preferences` 数组，对任意司机 ID 通用。

真正的司机特判在 `calc_monthly_income.py`（评测器，不属于 Agent）。

---

# 删除收益最大的 5 处代码

## 第 1 名：7 个重复的 rest_window 检测循环 → 提取公共方法

**预计消除行数：** ~40 行（删除重复 + 新增 15 行 helper = 净减少 ~25 行）
**风险等级：** 低 — 纯重构，不改变逻辑
**是否建议：** 是。减少 7 个维护点 → 1 个维护点。收益不在行数，在维护成本的降低。

---

## 第 2 名：11 个 Dead Code 函数/方法全部删除

**内容：** DC-1 (3 函数) + DC-2 (4 函数) + DC-3 (3 方法) + DC-6 (1 方法) = 11 个
**预计删除行数：** ~85 行
**风险等级：** 极低 — 全部经 grep 验证零调用点
**是否建议：** 是。纯减重，零风险。

---

## 第 3 名：test_optimization.py 删除

**预计删除行数：** ~80 行
**风险等级：** 极低 — 硬编码路径、已被 tests/test_modules.py 替代
**是否建议：** 是。

---

## 第 4 名：DriverStateSnapshot 两个死字段删除

**内容：** `consecutive_rest_window_violations` + `consecutive_long_deadhead`
**预计删除行数：** ~5 行（字段声明 + gamma 公式中的引用）
**风险等级：** 极低 — 两个字段当前值永远为 0，对系统行为无任何影响
**是否建议：** 是。消除"看起来有用但实际无作用"的误导性代码。

---

## 第 5 名：check_cargo() → check_cargo_weighted() 合并

**预计删除行数：** ~18 行（删除 check_cargo + 修改 1 处调用）
**风险等级：** 低 — `check_cargo_weighted(cargo, pickup, sim_min, None)` 等价于 `check_cargo(cargo, pickup, sim_min)`
**是否建议：** 是。减少 API 表面积。

---

# 汇总

| 类别 | 数量 | 可消除行数 |
|------|------|-----------|
| Dead Code（零调用函数） | 11 个函数 + 2 个死字段 | ~90 行 |
| Dead Code（临时测试脚本） | 1 个文件 | ~80 行 |
| Duplicate Logic（检测部分） | 4 处重复模式 | ~25 行净减少 |
| Duplicate Logic（距离截断） | 1 处 | ~5 行 |
| Magic Number | 10 个 | 不增删行数，移入 config |
| Architecture Debt | 4 项 | 结构性改进，不直接删行 |

**如果全部执行：净删除 ~200 行（约占 agent/ 总代码量的 15%），维护点从 7 个 rest_window 检查降为 1 个。**
