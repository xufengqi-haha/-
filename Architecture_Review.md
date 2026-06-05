# Architecture Review

**项目：** 天池大赛 — 卡车司机连续找货Agent
**当前版本：** GitHub main branch (2026-06-01)
**审查人：** Agent系统架构师
**审查日期：** 2026-06-04

---

## 当前成熟度评分

**62 / 100**

扣分项：
- P0问题4个（-20）
- 无可行性分析层（-8）
- Agent与评测器规则双轨运行，口径不一致（-5）
- 无前瞻规划能力，纯反应式架构（-5）

---

# P0 必须解决

## P0-1：daily_rest 强制休息阈值与评测器不一致，导致漏罚

**问题描述：**
Agent侧 `_check_mandatory_rest` 仅在当日休息缺口超过 **60 分钟** 时才触发强制休息。而评测器 `_eval_daily_rest` 在任何缺口 **> 0 分钟** 时即判定违规。缺口在 1-59 分钟之间的所有场景，Agent 不做任何干预，但评测器照罚不误。

**涉及文件：**
- `demo/agent/strategy/dispatcher.py`
- `demo/calc_monthly_income.py`
- `demo/agent/scoring/preference_scorer.py`

**涉及函数：**
- `DecisionDispatcher._check_mandatory_rest()` — dispatcher.py:590-598
- `_eval_daily_rest()` — calc_monthly_income.py:364-368
- `PreferenceChecker._check_cargo_rule()` — 缺少 daily_rest 的 case 分支 (558-598)

**根因分析：**
`_check_mandatory_rest` 中的 daily_rest 检查有两个门控条件：
```
deficit = min_hours * 60 - longest_today
if remaining_today >= deficit and deficit > 60:    ← 问题在此
    return min(deficit, remaining_today)
```

`deficit > 60` 意味着"如果今天还差不到1小时就能满足8h休息，Agent选择放任继续接单"。但评测器 `_eval_daily_rest` 是 `if longest_rest_minutes < min_hours * 60: penalty`，无阈值宽容。

进一步分析，Agent 的三个防御层对 daily_rest 都存在穿透：
1. `_check_mandatory_rest` — deficit ≤ 60 不触发
2. `_check_cargo_rule` — **没有 daily_rest case**，直接 fall through 返回 (0.0, [])
3. `_filter_and_score` — 仅对 deficit > 120 做 1.3× 软惩罚，不拦截

三条防线全部无法拦截 1-59 分钟缺口场景。

**影响：**
- D001 当前 3 次 daily_rest 违规，罚分 7,200 元
- 如果 D001 每天刚好差 5-50 分钟，最多可漏 31 × 2400 = 74,400 元（cap 值）

**预计罚分损失：** 7,200 元（当前 D001 实测）

**预计收益损失：** 0（这是纯罚分问题，不涉及收入）

**修复难度：** 低
- 将 `deficit > 60` 改为 `deficit > 0`
- 在 `_check_cargo_rule` 增加 `daily_rest` case：计算此订单会减少多少今日剩余休息窗口，若会导致不满足则返回罚分
- 在 `hard_forbidden` 逻辑中，让 daily_rest （`rule.is_hard == True`）产生的罚分直接一票否决

**优先级理由：**
单问题贡献当前总罚分的 36%（7200/20080）。一行改动即可消除。且 daily_rest 是所有司机最常见的偏好类型——D006/D007/D008/D009 都有类似规则，此修复的泛化收益极高。

**推荐方案：**
Level 1（立即）：`deficit > 60` → `deficit > 0`
Level 2（后续）：`_check_cargo_rule` 增加 daily_rest case + `hard_forbidden` 拦截

**验证方法：**
本地运行 D001 30 天仿真，检查 `monthly_income_202603.json` 中 daily_rest violations 字段是否为 0。

**状态：** TODO

---

## P0-2：max_pickup_km 全局配置覆盖了司机偏好值，经济阈值永不触发

**问题描述：**
Agent 有两个 max_pickup_km 检查点：
1. `config.filters.max_pickup_km: 200.0` — 硬截断，200km 以上货源直接丢弃（dispatcher.py:740）
2. `_check_cargo_rule` max_pickup_km case — 返回偏好罚分，但不硬过滤（preference_scorer.py:591-594）

D002 的偏好是空驶不超过 55km。55-200km 之间的货源**被硬截断放行，仅扣除偏好罚分**。而偏好罚分的 `hard_forbidden` 经济阈值公式为：
```
penalty * gamma > price * 0.8
120 * 1.0 > 1500 * 0.8 → False  (典型中档货源)
```
**对任何 price > 150 分的货源，此经济阈值永不触发。** 55km 偏好形同虚设。

**涉及文件：**
- `demo/agent/strategy/dispatcher.py`
- `demo/agent/config.py`
- `demo/agent/scoring/preference_scorer.py`

**涉及函数：**
- `DecisionDispatcher._filter_and_score()` — dispatcher.py:740, 硬截断 `max_pickup_km: 200.0`
- `PreferenceChecker._check_cargo_rule()` — preference_scorer.py:591-594, 只返回罚分不拦截
- `PreferenceChecker.hard_forbidden()` — preference_scorer.py:438-456, 经济阈值公式
- `AgentConfig.filters` — config.py:51-57

**根因分析：**
设计上，`max_pickup_km` 被分类为"司机软偏好"（`is_soft=True`），不进入 Platform Hard Rules 集合。导致检查和拦截完全依赖经济账公式，但该公式的阈值设定（`penalty * gamma > price * 0.8`）对于低罚分高价值场景完全失效。

Gamma 膨胀速度也太慢：`gamma = 1.0 + 0.2 * consecutive_long_deadhead`，需要连续 20 次违规才到 gamma=5.0。正常运行中几乎不会触发。

**影响：**
- D002 当前 4 次 max_pickup_km 违规，罚分 480 元
- 在更大数据集中（如 100+ 未知司机含更严 max_pickup 偏好），此问题会导致系统性扣分

**预计罚分损失：** 480 元（当前 D002 实测），随数据规模线性增长

**预计收益损失：** 难以估算——接远单毛收入更高，但不一定是司机想要的

**修复难度：** 中
- 在 `_filter_and_score` 中增加"偏好上限 × 1.2"的硬截断：若 `pickup_km > rule_max_km * 1.2`，直接过滤
- 调整经济阈值公式，使 `penalty * gamma` 对比 `net_profit * 0.3` 而非 `price * 0.8`

**优先级理由：**
当前影响小但泛化风险高。复赛数据集可能包含偏好 max_pickup_km=20km 的司机，届时会暴露为严重问题。

**推荐方案：**
`_filter_and_score` 中搜集所有 `max_pickup_km` 规则的偏好值，取最小值作为该司机的个性化硬截断（加 20% 容差）。

**验证方法：**
D002 本地仿真后检查 violations 和 cargo pickup_km 分布，确保 55km 以上货源被拒绝。

**状态：** TODO

---

## P0-3：day_specific_location Parser 无法解析多站路线，导致复杂事件型偏好结构性失败

**问题描述：**
D002 舅公寿宴偏好要求：先去增城（捎寿礼）→ 12:00 前到四会（赴宴）→ 停留 2 小时。PreferenceParser 的正则 `day_specific_location` 模式只提取**一个**地点+日期组合（增城），完全丢失第二站（四会）和 12:00 截止时间。

Agent 只知道需要在 3/31 到增城，不知道还需要继续前往四会并停留 120 分钟。这导致 Agent 认为自己已完成任务（到达增城），但评测器 `_eval_route_stops` 检查两个顺序停靠点，第二个不满足 → 全额罚 5,000 元。

**涉及文件：**
- `demo/agent/scoring/preference_scorer.py`
- `demo/calc_monthly_income.py`
- `demo/agent/strategy/dispatcher.py`

**涉及函数：**
- `PreferenceParser._PATTERNS` — preference_scorer.py:100-105, day_specific_location 正则
- `PreferenceParser._parse_one()` — preference_scorer.py:201-224, 单目标提取
- `PreferenceChecker.get_pending_requirements()` — preference_scorer.py:718-734, 只生成单个 go_to_location
- `_eval_route_stops()` — calc_monthly_income.py:510-532, 评测器检查两个停靠点
- `DecisionDispatcher._check_day_specific_location()` — dispatcher.py:508-534

**根因分析：**
正则方案的结构性限制——一行正则可匹配"在某天到某地做某事"，但无法表达"先到 A → 再到 B → 停留 N 分钟"的路线结构。这是自然语言理解问题，正则天花板。

Agent 侧更完整的 fallback 是 `_llm_parse_preference()`，但它只接受 `(content, penalty_amount, penalty_cap)` → 返回单个 `PreferenceRule`。当前 `PreferenceRule` 的数据结构也只支持单个 lat/lng/region_name，不支持多停靠点序列。

**影响：**
- D002 当前 1 次 day_specific_location 违规，罚分 5,000 元
- 占总罚分的 25%（5000/20080）
- 该偏好连 cap=5,000 → 全额罚分
- 任何含"先到…再到…"模式的偏好文本，Parser 都会丢失后半部分

**预计罚分损失：** 5,000 元（当前 D002 实测）

**预计收益损失：** 难以量化——Agent 可能因过度空驶到增城而放弃其他订单

**修复难度：** 高
- 需要在 `PreferenceRule` 中新增 `route_stops` 规则类型
- `_llm_parse_preference` 需要能输出多停靠点结构
- Dispatcher 需要能处理多站路线规划（当前只支持单目标 reposition）

**优先级理由：**
单问题罚分最高（5,000），且这是区分"能处理复杂偏好"和"只能处理简单偏好"的关键能力。复赛中复杂路线型偏好可能是拉分项。

**推荐方案：**
分两步走：
Step 1（快速止血）：LLM fallback 解析 D002 舅公寿宴为完整路线 JSON，手动构造 `route_stops` PreferenceRule → 验证可行性
Step 2（系统方案）：升级 `PreferenceRule` 数据模型支持 `stops: list[RouteStop]`，让 `get_pending_requirements` 返回多步路线，`_check_day_specific_location` 改为路线执行器

**验证方法：**
D002 本地仿真，检查 `preference_check.rules[6]` 的 `satisfied` 字段为 true。

**状态：** TODO

---

## P0-4：坐标级禁区检查函数已实现但从未被调用（僵尸代码）

**问题描述：**
`PreferenceChecker` 中有两个坐标级检查函数：
- `check_cargo_dest_avoid()` — 检查货源目的地坐标是否在 day_specific_avoid 或 forbidden_region 禁区内
- `check_position_day_avoid()` — 检查司机当前位置是否在 day_specific_avoid 禁区内

两者都遍历 `REGION_COORDINATES` 字典做 Haversine 距离匹配。但 **dispatcher.py 中没有任何调用点**。Agent 对禁区的判断完全依赖 cargo 的 city **字段 substring 匹配**（`_check_cargo_rule` 中 `region in start_city or region in end_city`）。

city 字段匹配与坐标匹配之间的鸿沟：
- 货源 city="宝安区"（深圳的区）→ substring 不匹配 "深圳" → Agent 放行 → 但货源在深圳 bounding box 内 → 评测器判违规
- 货源 city 含 "深圳" 但坐标在东莞边界 → Agent 拦截 → 但实际不在深圳 bounding box → Agent 过度保守

**涉及文件：**
- `demo/agent/scoring/preference_scorer.py`
- `demo/agent/strategy/dispatcher.py`
- `demo/calc_monthly_income.py`

**涉及函数：**
- `PreferenceChecker.check_cargo_dest_avoid()` — preference_scorer.py:534-556（已实现但未调用）
- `PreferenceChecker.check_position_day_avoid()` — preference_scorer.py:514-532（已实现但未调用）
- `PreferenceChecker._check_cargo_rule()` — preference_scorer.py:568-576，仅做 city 名 substring
- `_eval_d001_shenzhen_march()` — calc_monthly_income.py:466-491, 评测器用 lat/lng bounding box

**根因分析：**
两个函数是在 v2 迭代中加入的，但集成到 dispatcher 主循环的工作未完成。`_filter_and_score` 和 `_check_day_specific_location` 中均缺少调用。

**影响：**
- D001 day_specific_avoid（三/四号不进深圳）：1 次漏判，罚 3,000 元
- D001 forbidden_region_cargo（不接惠州）：1 次漏判，罚 800 元
- 两个问题合计 3,800 元，占总罚分的 19%

**预计罚分损失：** 3,800 元（当前实测）

**预计收益损失：** 可能有少量过度保守带来的机会损失，但相对于罚分较小

**修复难度：** 低
- 在 `_filter_and_score` 返回值前，对 scored candidates 逐一调用 `check_cargo_dest_avoid`
- 在 `_check_day_specific_location` 和 `_handle_no_cargo` 的重定位路径中调用 `check_position_day_avoid`
- 两个函数都已完整实现并测试过（在 test_modules.py 中有覆盖）

**优先级理由：**
实现成本极低，罚分影响大（3,800），且是纯集成工作，不涉及新逻辑。

**推荐方案：**
在 dispatcher.py `_filter_and_score` 中插入坐标级检查：
```
# 在 cargo loop 内，city 字段检查之后
avoid_penalty, avoid_violations = checker.check_cargo_dest_avoid(cargo, sim_min)
if avoid_penalty > 0:
    penalty += avoid_penalty
    violations.extend(avoid_violations)
    # 对于高罚分（≥ 3000），直接 hard_forbidden
```
在 `_check_day_specific_location`、`_handle_no_cargo` 的 reposition 路径中调用 `check_position_day_avoid`。

**验证方法：**
D001 本地仿真，检查 day_specific_avoid 和 forbidden_region_cargo violations 均为 0。

**状态：** TODO

---

# P1 推荐解决

## P1-1：Agent 与评测器区域匹配口径不一致（city 字段 vs 坐标）

**问题描述：**
Agent 和评测器对"货源属于哪个区域"的判断使用不同的方法，且两者都不完备：

| 检查场景 | Agent 方法 | 评测器方法 | 后果 |
|---------|-----------|-----------|------|
| `forbidden_region_cargo` | city 字段 substring | city 字段 substring | 一致，但都有盲区 |
| `day_specific_avoid` | city 字段 substring | lat/lng bounding box | **不一致** |
| `min_days_in_region` 计数 | `near_region()` 坐标 30km | `_cargo_touches_region()` city substring | **不一致** |
| `check_reposition` 禁区 | 坐标 30km 半径 | (不检查 reposition) | N/A |

这意味着 Agent 可能：
- 认为自己完成了 4 天增城（坐标法），但评测器 city 匹配法只认其中 3 天 → Agent 错误停止推动 → 误判漏罚
- 或相反：Agent 认为还没达标继续推动，但实际已经达标 → 浪费决策资源

**涉及文件：**
- `demo/agent/scoring/preference_scorer.py`
- `demo/agent/strategy/dispatcher.py`
- `demo/agent/memory/driver_state.py`
- `demo/calc_monthly_income.py`

**涉及函数：**
- `DecisionDispatcher._compute_daily_stats()` — dispatcher.py:663-709, 用 `near_region()` 坐标法
- `PreferenceChecker._check_cargo_rule()` — preference_scorer.py:568-576, 用 city substring
- `_eval_forbidden_region_cargo()` — calc_monthly_income.py:410-422, 用 city substring
- `_eval_required_region_cargo_days()` — calc_monthly_income.py:451-463, 用 city substring
- `_in_shenzhen()` — calc_monthly_income.py:224-225, 用 lat/lng bounding box

**根因分析：**
Agent 在开发时采用了（自认更准的）坐标匹配方案，但评测器由于业务合同约束使用 city 字段匹配。两套规则未在开发文档中对齐。`_compute_daily_stats` 中的 `near_region()` 用 30km 半径从 `REGION_COORDINATES` 判断归属，评测器用 cargo 原始 city 字段，两种判据在边界地带有本质差异。

**影响：**
- `min_days_in_region` 的计数偏差可能导致推动不足或过度推动
- `day_specific_avoid` 的漏判在 P0-4 中部分重叠

**预计罚分损失：** ~800 元（与 P0-4 有重叠）

**预计收益损失：** 难以量化，主要体现在计数错误导致的策略偏差

**修复难度：** 中
- 统一 `_compute_daily_stats` 的 region_days 计数为 city 字段匹配，与评测器对齐
- 在 P0-4 修复后，坐标检查作为 city 匹配的**补充**而非替代

**优先级理由：**
与 P0-4 重叠约 50%。P0-4 解决了"完全不查坐标"的问题后，P1-1 解决"两个系统判断口径不同"的问题。

**推荐方案：**
1. `_compute_daily_stats` 改为使用 `_city_at(dest_lat, dest_lng)` 判定区域（已有此函数）
2. 在 `_filter_and_score` 中同时使用 city 匹配和坐标匹配，两者都做，任一命中即拦截
3. 添加日志标注 city-match vs coord-match，便于调试口径不一致的案例

**验证方法：**
本地仿真后对比 Agent 日志中 region_days 计数与 `calc_monthly_income.py` 输出。

**状态：** TODO

---

## P1-2：Gamma 系统无可行性校验——"月末爆炸但数学上已不可能完成"

**问题描述：**
`DriverStateTracker` 的 gamma 公式仅基于**缺口比例**和**时间压力**计算紧迫度，不检查可行性。当 `deficit > remaining_days` 时（如：还需要 2 天休息，但只剩 1 天），`time_pressure = 2/1 = 2.0`，gamma 会爆炸到 30+，但实际约束已经**数学上不可能完成**。

爆炸的 gamma 驱动 Phase 1 强制休息，浪费宝贵时间试图完成不可完成的目标，导致 **"双输"** ——既罚了分，又放弃了赚钱机会。

**涉及文件：**
- `demo/agent/memory/driver_state.py`
- `demo/agent/strategy/dispatcher.py`

**涉及函数：**
- `DriverStateSnapshot._off_days_gamma()` — driver_state.py:87-107
- `DriverStateSnapshot.get_urgency_gamma()` — driver_state.py:36-58
- `DecisionDispatcher.decide()` Phase 1 — dispatcher.py:154-165
- `DecisionDispatcher._check_mandatory_rest()` — dispatcher.py:570-635

**根因分析：**
Gamma 公式的 `time_pressure = deficit / remaining` 在 `remaining < deficit` 时产生 >1 的值，导致 gamma 急剧膨胀。这是设计上的"单维度"问题——gamma 只表达"多紧急"，不表达"能不能做完"。

以 off_days 为例的场景推演（required=2, achieved=0）：
```
day=29 (remaining=2): time_pressure=1.0, gamma≈7.6 → 可行，应该推动休息
day=30 (remaining=1): time_pressure=2.0, gamma≈12 → 不可行！需要2880分钟,仅剩1440分钟
```

在 day=30 时，正确的行为是：**接受 off_days 的罚分，转向最大化剩余收益**。但当前系统会驱动 Agent 疯狂休息，最后既罚了分又没钱。

**影响：**
- 当前 D001/D002 双司机场景未触发（恰好都满足了 off_days）
- 但在更多司机、更紧约束的数据集中必然暴露
- 极端情况下一个约束的"濒死挣扎"可能带崩其他约束

**预计罚分损失：** 0（当前未触发），但在 100+ 司机上可能引发连锁失败

**预计收益损失：** 难以估算，取决于触发频率

**修复难度：** 中
- 需要新增 Constraint Feasibility Analyzer 模块
- 不改变 gamma 公式本身，而在 gamma 使用前增加 feasibility gate

**优先级理由：**
当前不暴露，但对泛化到 100+ 司机至关重要。属于架构级改进，不能等到复赛暴露问题再修。

**推荐方案：**
引入 `ConstraintFeasibilityAnalyzer`，在每次 `decide()` 开始时运行：
1. 对每个累积约束计算 `is_feasible`、`min_effort`、`deadline`
2. 若 `status == "infeasible"` → gamma_modifier = 0（放弃推动）
3. 时间预算协调：若多个约束争抢有限时间，按 `penalty / effort` 优先级分配
4. 具体设计见 `CFA_Design.md`（另文详述）

**验证方法：**
构建压力测试场景（3 个司机，各含紧约束），确认 CFA 输出了正确的 infeasible 标记和优先级排序。

**状态：** TODO

---

# P2 有时间再做

## P2-1：FutureIncomeEstimator 依赖冷启动期未充分填充的区域剖面

**问题描述：**
`FutureIncomeEstimator.estimate_arrival_value()` 依赖 `AreaMemory` 的 `get_heat_at_hour`、`get_avg_price_at_hour` 等函数返回值。在仿真早期（前 ~50 步），`AreaMemory` 的网格数据稀疏或为空，这些函数的返回值可能为 0 或 fallback 默认值（800 元 avg_price）。

低质量的未来价值估计导致 `_filter_and_score` 中的 `future_value_score` 分量失真，影响候选货源排序。

**涉及文件：**
- `demo/agent/planner/future_income_estimator.py`
- `demo/agent/memory/area_memory.py`

**涉及函数：**
- `FutureIncomeEstimator.estimate_arrival_value()` — future_income_estimator.py:31-73
- `AreaMemory.get_heat_at_hour()` — area_memory.py:144-145
- `AreaMemory.get_avg_price_at_hour()` — area_memory.py:154-162

**根因分析：**
冷启动期间，大多数网格的 `count < 0.5`，导致所有查询返回默认值。此时未来价值分量退化为常量，不提供有效信号。虽然 dispatcher 有冷启动保护（Phase 3），但这只影响"无货源"分支，不影响"有货源但未来价值噪声很大"的评分。

**影响：**
- 仿真前半月的长途路线选择可能欠优
- 评分权重的 `w_future_value: 0.08` 较小，影响有限

**预计罚分损失：** 0（不影响罚分）

**预计收益损失：** 数百元量级（早期路线选择次优）

**修复难度：** 中
- 增加基于地理先验的 fallback：珠三角核心区（广州-深圳-东莞）默认热度高
- 或者在前 100 步中降低 `w_future_value` 权重，减少噪声影响

**优先级理由：** ROI 相对较低，当前评分权重中未来价值仅占 8%

**推荐方案：** 在 `estimate_arrival_value` 中增加 `confidence` 因子，当网格样本不足时自动降低未来价值权重

**状态：** TODO

---

## P2-2：PreferenceParser 纯正则方案对未知偏好文本的泛化风险

**问题描述：**
`PreferenceParser` 的 `_PATTERNS` 列表包含 12 条手工正则，覆盖当前 D001/D002 的偏好文本模式。当遇到：
- 新的偏好表述方式（"我这人开车不能熬到后半夜" vs "零点以后到早上六点这段我得睡觉"）
- 新的规则类型（评测规则文档中 D006-D010 有 5 种新类型：特定时间窗不接单不空驶、23:00 前回家等）

正则匹配失败时，fallback 到 `_llm_parse_preference()`，但这会消耗 API 调用和 token。依赖 LLM 做偏好解析对于 100+ 司机的规模不可持续。

**涉及文件：**
- `demo/agent/scoring/preference_scorer.py`

**涉及函数：**
- `PreferenceParser._PATTERNS` — preference_scorer.py:91-146
- `PreferenceParser._parse_one()` — preference_scorer.py:192-297
- `PreferenceParser._llm_parse_preference()` — preference_scorer.py:327-385

**根因分析：**
正则方案是确定性的、零 token 消耗的，这是优点。但每一类新偏好表述需要一条新正则。评测规则文档中列出的 D006-D010 偏好类型包括：
- 特定时间窗内不接单不空驶（D007 的 23:00-04:00）
- 23:00 前回家且到次日 08:00 不再接单或空驶（D009）
- 每月至少 N 天到达目标点（D010 规则1）
- 禁止进入禁入区域（D010 规则2）

其中"D007 时间窗内不接单不空驶"和"D009 回家偏好"当前正则集中**完全没有对应模式**。

**影响：**
- 当前 D001/D002 不受影响
- 复赛每遇到一个新偏好类型，都需要手工添加正则

**预计罚分损失：** 0（当前），泛化时可能每个新类型罚分 3,000-10,000

**预计收益损失：** 不可估算

**修复难度：** 中
- 增加正则覆盖 D007/D009/D010 的新规则类型
- 为每种规则类型增加 3-5 条表述变体的正则
- 保留 LLM fallback 作为兜底

**优先级理由：** 重要但不紧急。正则集可以随着数据发布逐步扩充。架构上 LLM fallback 是安全的兜底。

**推荐方案：** 在下一版数据发布后，用全部司机的偏好文本跑一次解析测试，统计命中率，针对性补正则

**状态：** TODO

---

# 技术债务

## 重复逻辑

1. **距离→时间转换在多处重复实现**
   - `simkit/simulation_actions.py:28-31` — `distance_to_minutes()`
   - `agent/utils/geo_utils.py:34-38` — `distance_to_minutes()`（同名不同签名）
   - `agent/scoring/cargo_scorer.py:168-172` — `_distance_to_minutes()`（再实现一次）
   - `agent/strategy/risk_checker.py:156` — `dist_minutes = max(1, int((dist / 60.0) * 60.0))`（内联计算）
   - `agent/scoring/preference_scorer.py:642` — `pickup_minutes = max(1, math.ceil(...))`（再内联）

2. **区域坐标字典有两份**
   - `agent/utils/geo_utils.py:57-69` — `REGION_COORDINATES`
   - `calc_monthly_income.py:28-29` — `SHENZHEN_LAT_MIN/MAX/LNG_MIN/MAX`（深圳 bounding box，不同于点坐标）
   - 两者不可相互替代（一个是城市中心点，一个是矩形范围），但命名和使用场景不清晰

3. **cargo 价格单位转换**
   - `simkit/simulation_actions.py:91-95` — `normalize_cargo_price_to_yuan()` 除以 100
   - `calc_monthly_income.py:111` — `float(item.get("price", 0.0)) / 100.0`（又除一次）
   - Agent 侧 scorer 使用 cargo["price"] 原值（分），评测器使用元。单位不一致在多个文件中混用

## 临时方案

1. **`_extract_days` 中 `if val == 3: continue`**
   - `preference_scorer.py:321-322`
   - 为了区分"三月"（月份）和"三号"（日期），直接跳过数值 3
   - 如果真有偏好要求 3 月 3 日做某事，会被错误跳过

2. **`check_transit_violation` 的 30 分钟采样步长**
   - `preference_scorer.py:643` — `for m in range(sim_min, transit_end + 1, 30)`
   - 注释说"每 30 分钟采样"，逻辑上说如果越界窗口 < 30 分钟可能漏掉
   - 实际用小时粒度（`(m % 1440) // 60`）聚合后判断，30 分钟采样足够覆盖所有小时，但逻辑上不严谨

3. **`_check_mandatory_rest` 的 off_day 均匀分布算法**
   - `dispatcher.py:625` — `interval = max(1, (remaining_days + 1) // max(1, deficit))`
   - `days_since_last_off >= max(3, interval)` 的决定是否休息
   - 启发式合理但不精确——没有考虑当日是否已经做了不可逆的动作

## Magic Number

| 位置 | 值 | 含义 | 应如何 |
|------|---|------|--------|
| `dispatcher.py:155` | `off_days_gamma > 4.0` | Phase 1 触发阈值 | 移入 config |
| `dispatcher.py:156` | `hour_of_day < 8` | 早上 8 点前才强制休整天 | 移入 config |
| `dispatcher.py:598` | `deficit > 60` | daily_rest 缺口容忍 | P0-1 修复为 0 |
| `dispatcher.py:740` | `max_pickup_km: 200.0` | 全局空驶硬截断 | P0-2 修复 |
| `preference_scorer.py:22` | `_PLATFORM_HARD_RULES` | 哪些规则一票否决 | 应可配置 |
| `preference_scorer.py:414` | `min(total_penalty, 50000.0)` | 单货源罚分上限 | 移入 config |
| `preference_scorer.py:492` | `< 30` (km) | reposition 禁区半径 | 应与评测器 bounding box 对齐 |
| `cargo_scorer.py:96` | `_normalize(net_profit, -500.0, 3000.0)` | 利润归一化范围 | 移入 config |
| `future_income_estimator.py:55` | `avg_price = 800.0` | 默认平均运价 | 移入 config |
| `opportunity_evaluator.py:49` | `best_score >= min_score_to_wait * 3` | 高分直接接 | magic ×3 |

## Hard Code

1. **评测器 `calc_monthly_income.py` 中的 D001/D002 硬编码**
   - `DriverD001PreferenceCalculator` / `DriverD002PreferenceCalculator` 类 (line 544-594)
   - 每个司机的偏好规则以固定 Python 代码形式存在
   - **这不是 Agent 的责任**（Agent 正确地从 API 读取偏好），但评价口径的扩展方式意味着每增加新司机就要新增一个 Calculator 类
   - 对 Agent 无影响，但影响本地自测效率——新增测试司机时需要同步更新 calc_monthly_income.py

2. **`_apply_preference_push` 中的 `penalty_mult *= 0.85`**
   - `dispatcher.py:448` — 硬编码的偏好推动折扣因子
   - 对所有 prefer_wait 类型规则统一使用 ×0.85

3. **`simulation_duration_days: 31` 硬编码**
   - 多处使用 `31 * 1440` 作为月仿真上界
   - 如果赛方改变仿真时长，需要改动多处

---

# 冠军路线图

目标：双司机净收益 > 30,000（当前 14,905 → 目标 30,000）

## Phase 1：罚分止血（预计 +10,000 净收益）

| 问题 | 当前罚分 | 目标罚分 | 收益提升 | 实现难度 |
|------|---------|---------|---------|---------|
| P0-1 daily_rest 阈值修复 | 7,200 | 0 | +7,200 | 低 |
| P0-2 max_pickup_km 偏好化 | 480 | 0 | +480 | 中 |
| P0-4 僵尸代码调用 | 3,800 | 0 | +3,800 | 低 |
| P1-1 区域口径对齐 | ~800 | 0 | +800 | 中 |

**阶段目标：** 净收益 24,000-26,000，罚分 < 5,000
**实现成本：** 2-3 天

## Phase 2：复杂事件突破（预计 +5,000 净收益）

| 问题 | 当前罚分 | 目标罚分 | 收益提升 | 实现难度 |
|------|---------|---------|---------|---------|
| P0-3 route_stops 多站路线 | 5,000 | 0 | +5,000 | 高 |
| P1-2 CFA 可行性分析 | 0 | 保障 | 防退化 | 中 |
| rest_window 被动→主动预警 | 3,600 | < 1,800 | +1,800 | 中 |

**阶段目标：** 净收益 29,000-31,000，罚分 < 1,000
**实现成本：** 3-5 天

## Phase 3：收益最大化（预计 +2,000-4,000 净收益）

| 方向 | 预期提升 | 实现难度 |
|------|---------|---------|
| 未来价值估计冷启动优化 | +500-1,000 | 低 |
| 评分权重网格搜索调优 | +1,000-2,000 | 中 |
| P2-2 泛化正则补全 | 防退化 | 中 |
| Magic Number 配置化 + 自动调参 | +不确定 | 中 |

**阶段目标：** 净收益 > 32,000，罚分接近 0
**实现成本：** 3-5 天

---

**总结：** 当前 14,905 的净收益中，有 20,080 是罚分。仅 P0 四个问题的修复就可收回 ~16,500 的罚分，直接将净收益推到 31,000+。这还没算 Phase 2-3 的收益优化。**罚分是当前最大的短板，也是最容易修复的短板。**
