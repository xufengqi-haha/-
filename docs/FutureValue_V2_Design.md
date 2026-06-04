# FutureIncomeEstimator V2 — 架构设计

## 一、当前版本诊断

### 1.1 当前 Future Value 来源

`FutureIncomeEstimator.estimate_arrival_value()` 的完整数据流：

```
输入: dest_lat, dest_lng, arrival_sim_min, sim_horizon

Step 1 → AreaMemory.get_heat_at_hour(dest, arrival_hour)
         获取目的地 + 到达时段的热度 (0~1)
         公式: 0.4 × min(count/50, 1) + 0.6 × min(avg_price/2000, 1)

Step 2 → AreaMemory.get_avg_price_at_hour(dest, arrival_hour)  
         目的地平均运价 (分)
         fallback: get_avg_price(dest) → fallback: 800

Step 3 → AreaMemory.get_avg_pickup_distance(dest)
         目的地平均空驶距离 (km)
         fallback: 50

Step 4 → expected_net = avg_price - avg_deadhead × 1.5
         等于 "在该区域接一单的期望净收益"

Step 5 → time_factor = min(1.0, remaining_days / 7.0)
         剩余天数不足7天时线性衰减

Step 6 → value = expected_net × time_factor × (0.3 + 0.7 × heat)
         热度以0.3为底、0.7为系数参与乘法

Step 7 → if heat < 0.1 and remaining_days > 5:
             value -= 500 × time_factor

输出: value (单位: 元, 典型范围 0 ~ 1500)
```

### 1.2 四个维度评估

| 维度 | 当前状态 | 评分 |
|------|---------|------|
| 区域热度 | 深度使用，是唯一信号源 | ★★★★☆ |
| 时间维度 | 仅有 `time_factor` 和 `arrival_hour` 热度查询 | ★★☆☆☆ |
| 偏好完成价值 | **完全缺失** | ☆☆☆☆☆ |
| 未来订单链价值 | 仅估计"一个平均订单"，无链式思考 | ★☆☆☆☆ |

#### 1.2.1 区域热度 — 当前做得最好的维度

`AreaMemory` 的网格系统按 `0.1°×0.1°`（≈11km）分辨率积累 `count`、`total_price`、24小时分桶 `hour_buckets`、历史 `score_samples`。热度公式 `0.4×密度 + 0.6×价格` 合理。

**但有一个盲区：** 热度只反映"这个区域货源多不多、贵不贵"，不反映"从这里出发能不能方便地接到下一单"。深圳某工业区可能货源密集但都是短途微利单，而东莞某物流园虽然密度中等但全是长途高利润单。当前热度无法区分这两种情况。

#### 1.2.2 时间维度 — 有骨架但缺肌肉

`time_factor = min(1.0, remaining_days / 7.0)` 是全月尺度的粗粒度衰减。`get_heat_at_hour()` 提供了小时粒度，但两个信号之间没有联动：

- **同一天内的时间价值没有区分**：到达时刻是 06:00 还是 22:00，在公式中的唯一区别是 `get_heat_at_hour` 返回的热度不同。但 22:00 到达意味着当天只剩 2 小时可工作（且可能很快进入 rest_window），06:00 到达意味着有完整的 18 小时——这个差异在 `time_factor` 中完全不体现，`time_factor` 只看天数。

- **rest_window 没有任何影响**：如果 arriving at 23:30，且 D002 需要 0:00-6:00 休息，那么前 6.5 小时是无法工作的。但 `estimate_arrival_value` 对此毫不知情，仍然按正常热度估算。

- **跨天连续工作能力未建模**：如果 22:00 到达，睡一觉到次日 6:00，第二天可以工作一整天。这对 Future24h 是好的。但 06:00 到达，工作到 18:00 后也需要休息……这些都是时间维度的盲区。

#### 1.2.3 偏好完成价值 — 完全缺失

假设 D002 还有 1 天增城缺口（需要 4 天，已有 3 天），现在有两个候选货源：

- 货源 A：目的地 增城，净收益 300 元
- 货源 B：目的地 惠州，净收益 500 元

当前系统会选 B——纯经济账 B 更好。但选 A 能完成增城偏好（避免 6,000 元罚分），其 **真实价值 = 300 + 6,000 = 6,300 元**。偏好完成价值是离散的大额奖励，被当前的连续评分函数完全忽略。

#### 1.2.4 未来订单链价值 — 一阶近似

`expected_net = avg_price - avg_deadhead × cost_per_km` 是一个"平均订单"的估计。但：

- 没有考虑**链式效应**：到达枢纽后能不能连续接单？
- 没有考虑**选项价值**：高密度区可以挑单（挑利润最高的），低密度区只能有什么接什么
- 没有考虑**逃离成本**：如果目的地是冷区，下一次空驶迁出的成本是多少？

### 1.3 当前在评分中的权重

```
future_value_score = normalize(future_value, 0, 2000)     # 0~1
总分贡献 = 0.08 × future_value_score                       # 最多 8% 影响力
```

即 **Future Value 对总分的影响不超过 8%**。对于 1,500 元的未来价值估计，贡献为 `0.08 × 0.75 = 0.06`，约 6% 的影响力。这明显偏低——对一个月度优化问题，未来价值应该占 20-30% 的决策权重。

---

## 二、V2 设计：三层时间 + 偏好维度

### 2.1 核心公式

```
FutureValue = ImmediateProfit
            + α × Future24hProfit
            + β × Future72hProfit
            + δ × PreferenceCompletionValue
```

| 分量 | 含义 | 时间范围 | 信心 | 推荐权重 |
|------|------|---------|------|---------|
| ImmediateProfit | 当前货源净收益 | 当前单 | 确定性 | (已在 scorer 中) |
| Future24hProfit | 到达后 24h 内期望收益 | 0~24h | 高 | α = 0.8 |
| Future72hProfit | 24~72h 期望收益 | 24~72h | 低 | β = 0.3 |
| PreferenceCompletionValue | 偏好完成/破坏价值 | 月末 | 确定性 | δ = 1.0 |

**为什么是这三个时间层？**

- **24h**（明天）：司机一定会在这个区域。基于小时粒度数据做精确估计。置信度高。
- **72h**（后天+大后天）：司机可能已经离开了。基于区域连通性做粗略估计。置信度低，需要大幅折现。
- 24h 和 72h 之间的衰减不是连续的——24h 后司机完成 1-3 单，地点已高度不确定。

### 2.2 Future24hProfit — 基于到达时刻的 24 小时剖面

#### 2.2.1 数学公式

```
Future24hProfit = Σ_{h=0}^{23} V_h × 1_{productive}(arrival_hour + h)

其中:
  V_h = 第 h 个小时的期望贡献

  V_h = P_find(h) × E[net_profit | find_order_at_h] × τ(h)

  P_find(h) = min(1.0, density_h / D_sat)
    density_h  = AreaMemory.get_density_at_hour(dest, hour_of_day)
    D_sat       = 10.0  (密度饱和阈值: 10个货源/网格 ≈ 找到单的概率近100%)

  E[net_profit | find_order_at_h] = max(0, avg_price_h - avg_deadhead × cost_per_km)

  τ(h) = min(1.0, 60 / max(60, avg_duration_minutes))
    如果平均订单耗时 3h, τ = 60/180 = 0.33
    含义: 一小时最多贡献 0.33 个订单的期望利润

  1_{productive}(hour) = 0 如果该小时在 rest_window 内
                       = 0 如果 hour > 24 且已近月底
                       = 1 否则
```

#### 2.2.2 到达时刻因子的引入

```
arrival_hour_quality:
  06:00 到达 → 当天有 ~18 工作小时 → multiplier 1.0
  12:00 到达 → 当天有 ~12 工作小时 → multiplier 0.7
  18:00 到达 → 当天有 ~6 工作小时  → multiplier 0.4
  23:00 到达 → 当天几乎无法工作   → multiplier 0.1

这个因子隐含在 Σ 求和中 (rest_window 小时自动跳过, 夜间货源少 density 低),
不需要单独乘。
```

#### 2.2.3 所需特征

| 特征 | 来源 | 当前状态 |
|------|------|---------|
| `density_at_hour(dest, h)` | AreaMemory hour_buckets | 已有 count |
| `avg_price_at_hour(dest, h)` | AreaMemory hour_buckets | 已有 total_price |
| `avg_deadhead(dest)` | AreaMemory pickup_distances | 已有 |
| `avg_duration_minutes(dest)` | AreaMemory (新增) | **需新增** |
| `D_sat` (密度饱和阈值) | 配置 | **需新增** |
| `rest_window_hours` | PreferenceChecker | 已有 |

### 2.3 Future72hProfit — 区域连通性与枢纽溢价

#### 2.3.1 核心思想

24h 之后的位置不确定。但可以从目的地出发，评估它的"枢纽价值"：

- **枢纽**：周围 100km 内有多个热区 → 明天可以轻松 reposition 到更好的位置 → 选项价值高
- **孤岛**：最近的货源区在 150km 外 → 明天可能需要长距离空驶才能找到货 → 逃离成本高

#### 2.3.2 数学公式

```
Future72hProfit = hub_premium × regional_avg_potential × remaining_days_after_24h

其中:
  hub_premium = min(1.0, N_nearby_hot / 5.0)
    N_nearby_hot = count of grids within 100km with heat > 0.15
    
  regional_avg_potential = mean of nearby hot zones' daily_potential
    daily_potential(g) = density(g)/D_sat × (avg_price(g) - avg_deadhead(g) × c_km)
                         × productive_hours_per_day × orders_per_hour

  remaining_days_after_24h = max(0, remaining_days - 1)
    折现到"有效天数", 每天约贡献 daily_potential

  整体 × β (0.3) 折扣因子
```

#### 2.3.3 枢纽 vs 孤岛示例

```
目的地 A: 广州物流园 (23.13, 113.26)
  100km内热区: 深圳、东莞、佛山、中山、江门 = 5个 → hub_premium = 1.0
  → Future72h 估值高

目的地 B: 某个山区县 (23.80, 115.40)
  100km内热区: 0个 → hub_premium = 0.0
  → Future72h 估值 = 0 (到达后可能需要长距离空驶才能离开)
```

#### 2.3.4 所需特征

| 特征 | 来源 | 当前状态 |
|------|------|---------|
| `N_nearby_hot` | AreaMemory 网格遍历 | 已有网格数据,需新增聚合查询 |
| `daily_potential(g)` | 复用 Future24h 的 area 计算 | 需新增 |
| `neighbor_hot_count` (缓存) | AreaMemory grid 字段 | **需新增** |

### 2.4 PreferenceCompletionValue — 偏好驱动的目的地价值

#### 2.4.1 数学公式

```
PreferenceCompletionValue = Σ_{req ∈ pending} V_pref(dest, req)

每个 pending requirement 的贡献:

Case 1: min_days_in_region
  if dest_city contains req.region:
    V_pref = req.penalty_at_stake × req.urgency × P(this_trip_counts_as_new_day)
    
    P(this_trip_counts_as_new_day):
      # 如果今天已经在该区域接过单 → 不计入新的一天 → P = 0
      # 如果今天还没在该区域接过单 → 大概率计入 → P ≈ 0.9
      = 0.9 if region_days_today == 0 else 0.0

Case 2: day_specific_location (距离目标还有多远)
  if req.day is within 2 days of arrival_day:
    dist_current_to_target = haversine(current_pos, req.target)
    dist_dest_to_target = haversine(dest, req.target)
    
    distance_saved = dist_current_to_target - dist_dest_to_target
    
    if distance_saved > 10 km:  # 离目标更近了
      reposition_cost_saved = distance_saved × cost_per_km
      V_pref = reposition_cost_saved × req.urgency
    
    if dist_dest_to_target < 5 km:  # 直接到达目标附近
      V_pref = req.penalty_at_stake × 0.5  # 完成了一半

Case 3: off_days
  # 到达后如果还有足够时间在当天休一整天
  minutes_until_midnight = minutes_until_next_day(arrival_min)
  if minutes_until_midnight > 12 × 60 AND off_days_deficit > 0:
    V_pref = req.penalty_at_stake × req.urgency × 0.15
    # 15%: 只是创造了休息的条件, 不代表一定会休

Case 4: forbidden_region / day_specific_avoid
  if dest is in forbidden region:
    V_pref = -(req.penalty_at_stake + 500)  # 强烈惩罚
    # penalty + 500 确保在所有经济账上都被压制

Case 5: daily_rest
  if order_completion_leaves_less_than_8h_for_rest:
    V_pref = -(req.penalty_amount × 0.5)
    # 当前单会吃掉今天的休息窗口

总 PreferenceCompletionValue = Σ V_pref
  裁剪到 [-10000, +10000] 范围
```

#### 2.4.2 所需特征

| 特征 | 来源 | 当前状态 |
|------|------|---------|
| `pending_requirements` | PreferenceChecker.get_pending_requirements() | 已有 |
| `region_days_today` | daily_stats | 已有 |
| `driver_state.off_days_gap` | DriverStateTracker | 已有 |
| `dest_city` | AreaMemory + cargo end city | 已有 `_city_at()` |
| `haversine(dest, target)` | geo_utils | 已有 |

### 2.5 数据结构

```
@dataclass
class FutureValueComponents:
    """V2 未来价值分解"""
    immediate_profit: float              # 当前单净利润 (元), 复用现有 net_profit
    
    future_24h_profit: float             # 到达后24h期望利润 (元)
    future_24h_confidence: float         # 0~1, 基于网格数据充分度
    
    future_72h_profit: float             # 24-72h远期期望利润 (元)
    hub_score: float                     # 0~1, 目的地枢纽评分
    n_nearby_hot_zones: int              # 100km内热区数量
    
    preference_completion_value: float   # 偏好完成净值 (元), 可为负
    preference_contributions: dict       # {rule_type: contribution_yuan}
    
    total_future_value: float            # 加权总和 (元)
    
    breakdown: dict[str, float]          # 调试用字段


@dataclass  
class FutureValueWeights:
    """可配置权重, 移入 AgentConfig"""
    alpha_24h: float = 0.8       # Future24h 折扣
    beta_72h: float = 0.3        # Future72h 折扣  
    delta_pref: float = 1.0      # PreferenceCompletion 权重
    D_sat: float = 10.0          # 密度饱和阈值
    productive_hours_per_day: float = 14.0  # 每天有效工作小时
    hub_radius_km: float = 100.0  # 枢纽评估半径
```

### 2.6 Dispatcher 接入点

#### 2.6.1 调用位置

`dispatcher.py:_filter_and_score()` 中，现有调用之后：

```
# 位置: line 829-843, cargo loop 内

# === OLD (line 829-843) ===
future_val = self._future_estimator.estimate_arrival_value(...)
s = self._scorer.score(cargo, ..., future_value=future_val, ...)

# === NEW ===
fv = self._future_estimator.estimate_full(
    dest_lat=dest_lat,
    dest_lng=dest_lng,
    arrival_sim_min=sim_min + pickup_min + cost_time,
    simulation_horizon_minutes=sim_horizon,
    current_lat=lat,
    current_lng=lng,
    checker=checker,
    pending_requirements=pending_early,
    driver_state=driver_state,
    daily_stats=daily_stats,
)
s = self._scorer.score(cargo, ..., future_value_components=fv, ...)
```

#### 2.6.2 调用频率

每个候选货源调用一次。D002 通常有 50-200 个候选货源 × 每步一次 = 每决策最多 200 次调用。`estimate_full` 需要：
- `Future24hProfit`: O(24) = 24 次 get_density_at_hour 查询
- `Future72hProfit`: O(N_nearby) = 需要遍历网格（需缓存）
- `PreferenceCompletionValue`: O(N_pending) = 通常 1-5 个 pending

总计每 candidate 约 30-50 次 AreaMemory 查询。需要在 `Future72hProfit` 的网格遍历上加缓存（每决策步计算一次，所有 candidate 复用）。

#### 2.6.3 CargoScorer 适配

```
# CargoScorer.score() 改为接收 FutureValueComponents

# 当前: 单个 future_value → 单个 future_value_score (权重 0.08)
# V2:    三个独立分量 → 三个独立评分维度

future_24h_score = self._normalize(fv.future_24h_profit, 0, 3000)
hub_premium_score = self._normalize(fv.future_72h_profit, 0, 5000)  
pref_completion_score = self._normalize(fv.preference_completion_value, -3000, 3000)

total = (
    self._cfg.w_profit * profit_score
    + self._cfg.w_efficiency * efficiency_score
    + self._cfg.w_dest_heat * combined_heat_score
    - self._cfg.w_pref_penalty * (1.0 - pref_penalty_score)
    - self._cfg.w_time_risk * time_risk_score
    + self._cfg.w_future_24h * future_24h_score          # 新增, 权重 ~0.10
    + self._cfg.w_hub_premium * hub_premium_score        # 新增, 权重 ~0.06
    + self._cfg.w_pref_completion * pref_completion_score # 新增, 权重 ~0.10
    + self._cfg.w_distance_bonus * distance_score
)
```

#### 2.6.4 AreaMemory 新增查询

```
class AreaMemory:
    # 新增
    def get_avg_duration(self, lat, lng) -> float:
        """该网格货源的平均运输时长 (分钟)"""
        
    def count_nearby_hot_zones(self, lat, lng, radius_km=100, heat_threshold=0.15) -> int:
        """统计周围radius_km内热度>threshold的网格数"""
        
    def get_regional_avg_potential(self, lat, lng, radius_km=100) -> float:
        """周围热区的平均 daily_potential"""
    
    # 修改
    def update_from_cargo(self, ...):
        # 新增记录 cost_time_minutes 到 total_duration 字段
```

### 2.7 效果预期

#### 2.7.1 场景对比

**场景：D002 第28天，还需要1天增城，当前在东莞**

| 候选 | 目的地 | 净收益 | 当前FutureValue | V2 FutureValue | 当前会选择 | V2会选择 |
|------|--------|--------|----------------|----------------|-----------|---------|
| A | 增城 | 300元 | ~200元 | 300+200+100+**3,000**=3,600 | ❌ (净收益低) | ✅ (偏好完成) |
| B | 深圳 | 500元 | ~400元 | 500+350+200+0=1,050 | ✅ (净收益高) | ❌ |

V2 的核心改进：偏好完成价值（3,000 元）是一个阶梯函数式的离散价值，远大于候选间的连续利润差异。它让 Agent 在关键时刻做正确的战略选择。

#### 2.7.2 量化预期

| 指标 | 当前 | V2 预期 | 理由 |
|------|------|---------|------|
| FutureValue 在总分中的影响力 | 0~8% | 15~26% | 三个独立维度贡献 |
| D002 增城偏好罚分 | 0 (已满足) | 0 | 维持 |
| D002 舅公寿宴罚分 | 5,000 | 通过偏好路径改善 | PrefCompletion 在 3/30-31 推动增城方向 |
| D001 惠州罚分 | 800 | 0 | forbidden_region pref_completion < 0 → 硬拒绝 |
| 决策"远见" | 只看一单 | 看 3 天 | 72h 枢纽溢价指引长途方向 |
