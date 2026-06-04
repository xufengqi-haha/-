# Top 10 改进项 — 按 ROI 排序

**评估人：** 冠军队技术负责人
**系统成熟度评分：** 60 / 100

扣分明细：
- 规则引擎有4个已确认的罚分泄漏点：-20
- 僵尸代码（已实现未集成）：-8
- 无可行性分析层，gamma 可失控：-7
- FutureValue 一维信号，无偏好感知：-5

---

# P0（必须做）

## P0-1：daily_rest 强制休息阈值与评测器不一致

**具体位置：** `dispatcher.py:599` — `if remaining_today >= deficit and deficit > 60:`

**当前行为：** 当天连续休息缺口 ≤ 60 分钟时，Agent 不做任何干预，继续接单。

**评测器行为：** `calc_monthly_income.py:364-368` — `if longest_rest_minutes < min_hours * 60: penalty`，任何缺口 > 0 即全额罚分（2400 元/天）。

**根因：** `deficit > 60` 是开发阶段留下的容差，与评测器口径不一致。

**预计收益：** 0
**预计罚分下降：** 7,200 元（D001 当前 3 次违规）
**实现难度：** 低 — 改一行：`deficit > 60` → `deficit > 0`
**风险：** 极低。会增加 rest 频率但不改变决策质量
**验证：** D001 运行 30 天仿真，check daily_rest violations == 0

---

## P0-2：坐标级禁区检查已实现但从未调用（僵尸代码）

**具体位置：**
- 已实现但未调用：`preference_scorer.py:514-532` `check_position_day_avoid()`
- 已实现但未调用：`preference_scorer.py:534-556` `check_cargo_dest_avoid()`
- 缺失调用点：`dispatcher.py:736-845` `_filter_and_score()` cargo loop 内
- 缺失调用点：`dispatcher.py:508-534` `_check_day_specific_location()`

**当前行为：** Agent 对"不进入某区域"的判断完全依赖 cargo city 字段 substring 匹配（`_check_cargo_rule:568-576`）。city="宝安区"不匹配"深圳" → 漏判。

**评测器行为：** `calc_monthly_income.py:466-491` `_eval_d001_shenzhen_march()` 使用 lat/lng bounding box，与 city 字段无关。

**根因：** 两个坐标检查函数在 v2 迭代中实现、测试通过，但集成到 dispatcher 主循环的工作未完成。

**预计收益：** 0
**预计罚分下降：** 3,800 元（D001 day_specific_avoid: 3,000 + forbidden_region: 800）
**实现难度：** 低 — 纯集成工作，两个函数已完整实现。在 `_filter_and_score` cargo loop 内、city 检查之后插入坐标检查调用
**风险：** 低。坐标检查和 city 检查取并集（任一命中即拦截），不会漏掉更多
**验证：** D001 运行仿真，check day_specific_avoid violations == 0 AND forbidden_region violations == 0

---

## P0-3：max_pickup_km 全局配置覆盖司机偏好值

**具体位置：**
- 硬截断：`dispatcher.py:740` — `if pickup_km > self._config.filters["max_pickup_km"]: continue` (200km)
- 偏好仅软罚分：`preference_scorer.py:591-594` — `if pickup_distance_km > max_km: violations.append(...)`
- 经济阈值永不触发：`preference_scorer.py:454` — `if p * rule_gamma > price * 0.8`。D002 偏好 max=55km, penalty=120, gamma≈1.0 → 120 > 1500×0.8=False

**当前行为：** D002 偏好空驶 ≤ 55km，但 55-200km 之间的货源全部放行，只扣分不拦截。扣分太少（120 元）不足以影响排序。

**根因：** `config.filters.max_pickup_km: 200` 作为全局截断，覆盖了每条偏好规则中提取的个性化 max_km 值。`hard_forbidden` 的经济阈值对低罚分偏好永远不触发。

**预计收益：** 0
**预计罚分下降：** 480 元（D002 当前 4 次违规）+ 泛化收益（复赛可能有 max=20km 的司机）
**实现难度：** 中 — 需在 `_filter_and_score` 中搜集所有 `max_pickup_km` 规则的最小偏好值，用作该司机的个性化硬截断（加 20% 容差）
**风险：** 中。过度过滤可能减少候选货源池 → 需观测 accept rate 变化
**验证：** D002 仿真后 cargo pickup_km 分布 ≤ 66km（55 × 1.2）

---

## P0-4：day_specific_location Parser 无法解析多站路线

**具体位置：**
- 正则只提取单目标：`preference_scorer.py:100-105` `_PATTERNS` 中的 `day_specific_location` 正则
- 单目标构造：`preference_scorer.py:215-224` 只提取一个 lat/lng
- 单 go_to_location 指令：`preference_scorer.py:718-734` `get_pending_requirements`
- 评测器期望双停靠点：`calc_monthly_income.py:510-532` `_eval_route_stops()`

**当前行为：** D002 舅公寿宴（"先到增城捎寿礼 → 12:00 前到四会赴宴 → 停留 2h"）被解析为单一 `day_specific_location(增城)`。Agent 到增城就算完成，不知道还需要去四会。

**根因：** 正则方案的结构性限制——一条正则匹配一个地点+时间。"先到 A 再到 B 停留 N 小时"的路线语义超出正则表达能力。LLM fallback（`_llm_parse_preference:327-385`）可用但返回结构也是单个 `PreferenceRule`。

**预计收益：** 0
**预计罚分下降：** 5,000 元（D002 舅公寿宴全额罚分）
**实现难度：** 高 — 需新增 `route_stops` 规则类型、升级 `PreferenceRule` 数据结构支持多停靠点列表 `stops: list[RouteStop]`、改造 `get_pending_requirements` 和 `_check_day_specific_location` 为路线执行器
**风险：** 中。LLM fallback 解析路线的准确性需要充分测试
**验证：** D002 仿真后 check preference_check.rules[6].satisfied == true

---

# P1（推荐做）

## P1-1：_check_cargo_rule 缺少 daily_rest case —— hard_forbidden 对 daily_rest 完全无效

**具体位置：**
- 缺失 case：`preference_scorer.py:558-598` `_check_cargo_rule()` — 有 `day_specific_avoid/forbidden_category/forbidden_region/max_pickup_km` 四个 case，无 `daily_rest`
- 影响下游：`preference_scorer.py:447-455` `hard_forbidden()` — 对 daily_rest 调用 `_check_cargo_rule` 返回 `(0.0, [])`，永远不拦截

**当前行为：** 即使一笔订单会导致今天只剩 1 小时休息窗口（远不满足 8h 要求），`hard_forbidden` 也不会拦截。Agent 仅依赖 P0-1 的强制休息逻辑，如果 P0-1 因为 `deficit ≤ 60` 未触发，订单就畅通无阻。

**与 P0-1 的关系：** P0-1 修复了强制休息的触发条件，P1-1 为货源过滤增加第二道防线。两个互补。

**预计罚分下降：** ~2,400 元（防御性，与 P0-1 协同）
**实现难度：** 低 — 在 `_check_cargo_rule` 中增加 `daily_rest` case：计算订单完成后的 remaining_today，若不足以满足 min_hours × 60，返回罚分
**风险：** 低
**验证：** 边界场景测试——23:00 决策时只剩 1h 休息窗口，所有订单被拦截

---

## P1-2：rest_window 只有被动触发，无进入前的主动预警

**具体位置：**
- 被动触发：`dispatcher.py:582-588` — 仅当前小时已在窗口内才强制休息
- 货源过滤中的预警：`dispatcher.py:788-813` P0 检查 — 只检查完单时刻是否跨越边界
- 缺失：进入窗口前 30-60 分钟的"提前等待"逻辑

**当前行为：** Agent 在 23:30 做完一单后做决策。`hour_of_day=23.5` 不在 `[0,6)` 窗口内 → `_check_mandatory_rest` 不触发。P0 货源过滤会拦截所有货源（因为完单时间会跨入 0:00），但 `_handle_no_cargo` 策略 1 只在"已在窗口内"时触发，不在窗口前触发 → Agent 只能等待 60 分钟然后再次决策，浪费一个决策步。

**根因：** `_check_mandatory_rest` 的 rest_window 检查（line 582-588）和 `_handle_no_cargo` 策略 1（line 937-953）都使用 `start_h <= hour_of_day < end_h` 半开区间，没有"距离窗口开始不到 N 分钟"的预警区间。

**预计罚分下降：** ~1,800 元（D002 当前 2 次 rest_window 违规的一半）
**实现难度：** 中 — 在 `_check_mandatory_rest` 开头增加：`if minutes_until_rest_window < 60: return wait(minutes_until_rest_window)`
**风险：** 低
**验证：** D002 仿真后 check rest_window violations ≤ 1

---

## P1-3：Gamma 系统无可行性校验——月末可能"双输"

**具体位置：**
- Gamma 计算：`driver_state.py:87-107` `_off_days_gamma()` — `time_pressure = deficit / remaining`，当 deficit > remaining 时值 > 1，gamma 爆炸
- Gamma 驱动的强制动作：`dispatcher.py:154-165` Phase 1 — `if off_days_gamma > 4.0 and hour_of_day < 8: force rest`

**当前行为：** 当 `deficit=2, remaining=1`（缺 2 天休息只剩 1 天），gamma 爆炸到 30+，驱动 Agent 全天休息，但仍无法完成目标（需要 2880 分钟，只有 1440 分钟）。Agent 浪费最后一天休息→双输（罚分 + 无收入）。

**根因：** Gamma 公式只表达"多紧急"，不检查"能不能做到"。`time_pressure > 1.0` 就是数学上不可能的明确信号，但当前代码照常返回高 gamma。

**预计罚分下降：** 0（防御性改进，当前 D001/D002 未触发）
**实现难度：** 中 — 在 Phase 1 前增加 feasibility gate：`if deficit > remaining_days: skip off_day enforcement, accept_loss`
**风险：** 低。只在已经不可能完成的场景下改变行为
**验证：** 构造压力测试（3 司机，紧约束），确认 infeasible 约束被正确识别并放弃

---

## P1-4：Agent 与评测器对"区域归属"的判断口径不一致

**具体位置：**
- Agent 坐标法：`dispatcher.py:696` `_compute_daily_stats()` — `near_region(pos, region_name, 30km)` 坐标匹配
- 评测器 city 法：`calc_monthly_income.py:451-463` `_eval_required_region_cargo_days()` — `_cargo_touches_region()` city 字段 substring
- Agent city 法：`dispatcher.py:824-825` `_city_at(dest_lat, dest_lng)` — 坐标反查城市名（仅 P2 约束使用）

**当前行为：** `_compute_daily_stats` 统计 region_days 时用坐标 30km 半径判定是否在区域内，评测器用 city 字段判定。同一个货源可能 Agent 认为"在增城"但评测器不认为。

**根因：** Agent 开发时采用坐标法（认为更准），评测器按业务合同采用 city 字段法。两套口径未在开发阶段对齐。

**预计罚分下降：** ~800 元（减少口径不一致导致的推动偏差）
**实现难度：** 中 — 将 `_compute_daily_stats` 改为 city 字段匹配 + 坐标匹配双验证
**风险：** 低
**验证：** 对比 Agent 日志中 region_days 计数值与 `monthly_income_202603.json` 评测结果是否一致

---

# P2（有时间做）

## P2-1：FutureIncomeEstimator 一维信号 → 多维分解

**具体位置：**
- 当前：`future_income_estimator.py:31-73` `estimate_arrival_value()` — 单一标量输出
- 接入：`dispatcher.py:829-833` — 对每个候选货源调用一次
- 评分：`cargo_scorer.py:119-120` — `_normalize(future_value, 0.0, 2000.0)` 权重 0.08

**当前行为：** FutureValue = avg_price - avg_deadhead × cost × time_factor × heat_coeff。仅占评分 8%，不知道偏好的存在，不知道目的地是枢纽还是孤岛。

**改进方向：** 拆为 Future24hProfit + Future72hHubPremium + PreferenceCompletionValue 三个独立信号，总权重提升到 20-26%。详见 `FutureValue_V2_Design.md`。

**预计收益提升：** +1,000 ~ 2,000 元（长途方向选择优化 + 偏好完成引导）
**预计罚分下降：** ~1,000 元（PreferenceCompletionValue 负分 → 推开禁区方向货源）
**实现难度：** 中 — 需要 AreaMemory 新增 `avg_duration`、`count_nearby_hot_zones` 字段
**风险：** 中。多维权重需要调优，不合适的权重可能降低决策质量
**验证：** A/B 对比（开启/关闭 V2）30 天仿真的净收益差异

---

## P2-2：off_day 调度无绝对 deadline——最后一天可能错过强制休息窗口

**具体位置：**
- 均匀分布调度：`dispatcher.py:625-633` — `interval = (remaining+1)//deficit` + `days_since_last_off >= max(3, interval)`
- Phase 1 守卫：`dispatcher.py:155` — `hour_of_day < 8` 限制了强制休息的触发时间窗口

**当前行为：** 如果 Agent 在倒数第 2 天 10:00 做完一单后才做决策（因为上一单是长途），`hour_of_day < 8` 失效，Phase 1 不触发。Phase 2 的均匀分布算法可能因为 `days_since_last_off < max(3, interval)` 也不触发 → 错过最后一个可用的 off_day。

**根因：** Phase 1 的 `hour_of_day < 8` 守卫目的是"早上 8 点前才值得休一整天"，但这个守卫在月末可能成为阻碍——如果 10:00 才做决策且缺 1 天 rest，休到明天 10:00 虽然不完美，但至少完成了目标。

**预计罚分下降：** 防御性（当前未触发）
**实现难度：** 低 — 在 Phase 1 末尾增加 fallback：`if deficit >= remaining_days: force rest regardless of hour`
**风险：** 低
**验证：** 边界测试——月末最后一天 10:00 做决策，deficit=1 → 正确触发全天休息

---

# 汇总

| # | 优先级 | 改进项 | 罚分下降 | 难度 | 风险 |
|---|--------|--------|---------|------|------|
| 1 | P0 | daily_rest 阈值 `>60` → `>0` | 7,200 | 低 | 低 |
| 2 | P0 | 坐标禁区检查僵尸代码激活 | 3,800 | 低 | 低 |
| 3 | P0 | max_pickup_km 偏好化截断 | 480 | 中 | 中 |
| 4 | P0 | 多站路线 parser + route_stops | 5,000 | 高 | 中 |
| 5 | P1 | _check_cargo_rule 补 daily_rest case | 2,400 | 低 | 低 |
| 6 | P1 | rest_window 提前预警 | 1,800 | 中 | 低 |
| 7 | P1 | Gamma 可行性 gate | 防御 | 中 | 低 |
| 8 | P1 | 区域口径统一 | 800 | 中 | 低 |
| 9 | P2 | FutureValue 多维分解 | 1,000 | 中 | 中 |
| 10 | P2 | off_day 绝对 deadline | 防御 | 低 | 低 |

**P0 四项合计罚分下降：16,480 元 → 净收益可直接到达 31,400 元。**

这还不包括 P1 的增量收益（4-5,000）和 P2 的收益优化（1-2,000）。罚分是当前唯一的短板也是最容易修复的短板。
