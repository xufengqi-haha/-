# P0-1 Evidence Review：daily_rest 阈值 `>60` → `>0`

---

## 1. 当前代码位置

| 层级 | 文件 | 函数 | 行号 | 角色 |
|------|------|------|------|------|
| Agent 强制休息 | `agent/strategy/dispatcher.py` | `_check_mandatory_rest` | **599** | 唯一强制干预点 |
| Agent 软惩罚 | `agent/strategy/dispatcher.py` | `_filter_and_score` | **780** | 第二个阈值 `deficit < 120` |
| Agent 货源过滤 | `agent/scoring/preference_scorer.py` | `_check_cargo_rule` | **558-598** | daily_rest **无 case 分支** |
| 评测器 | `calc_monthly_income.py` | `_eval_daily_rest` | **364-368** | 最终裁定 |

---

## 2. 当前逻辑

### 2.1 完整的三层防御链路

```
Layer 1: _check_mandatory_rest (dispatcher.py:590-600)
  ↓ 如果通过了 → 正常接单
  ↓ 如果被拦截 → 强制 wait

Layer 2: _filter_and_score (dispatcher.py:769-785)
  ↓ 如果通过了 → 货源进入候选池
  ↓ 如果被拦截 → penalty *= 1.3 (仅仅软惩罚, 不拦截)

Layer 3: _check_cargo_rule (preference_scorer.py:558-598)
  ↓ 没有 daily_rest case → 永远返回 (0.0, [])
  ↓ hard_forbidden 对 daily_rest 永远不拦截
```

### 2.2 Layer 1 的精确逻辑

```python
# dispatcher.py:590-600
if rule.rule_type == "daily_rest":
    min_hours = int(rule.params.get("min_hours", 0))       # D001: 8
    longest_today = daily_rest_max.get(day_idx, 0)         # 当天最长单次 wait 的分钟数
    deficit = min_hours * 60 - longest_today               # 480 - longest_today
    if deficit <= 0:
        continue                                            # 已满足, 放行
    remaining_today = time_utils.minutes_until_next_day(sim_min)
    if remaining_today >= deficit and deficit > 60:         # ← 关键门控
        return min(deficit, remaining_today)                # 强制 wait
```

### 2.3 为什么会触发 `>60`

`deficit > 60` 确保：当 Agent 强制休息时，休息时长至少 61 分钟。这意味着当天最长休息块会从当前值增加到至少 `longest_today + 61`。

设计意图（推测）：小的休息缺口无法通过短 wait 来修复。因为评测器检查的是**最长连续休息块**，而 `daily_rest_max` 跟踪的也是单个 wait 的 `action_exec`（dispatcher.py:690）。如果 `longest_today = 450`（7h30min），做一个 30min 的 wait 不会增加 `daily_rest_max = max(450, 30) = 450`。

### 2.4 为什么不会触发 `>0`

当 `deficit` 在 **1 到 60 之间**时，门控条件 `deficit > 60` 为 False。Agent 跳过强制休息，继续进入 Phase 7 货源过滤和接单。

同时，Layer 2 的 `deficit < 120`（dispatcher.py:780）意味着 deficit 在 1-119 范围时不触发软惩罚。Layer 3 完全没有 daily_rest 检查。

**结论：deficit ∈ [1, 60] 时，三道防线全开，Agent 毫无阻拦地继续接单。**

### 2.5 Agent 与评测器的算法差异（加剧因子）

| | Agent `_compute_daily_stats` | 评测器 `_eval_daily_rest` |
|---|---|---|
| 行号 | dispatcher.py:689-690 | calc_monthly_income.py:220-221 + 256-265 |
| 算法 | `max(individual_action_exec)` | `max(merged_interval_spans)` |
| 合并连续 wait？ | **否** | **是** |
| 示例 | wait(200)+wait(200)+wait(200) → max=200 | 同三次 wait → merged=600 |

Agent 可能**低估**实际最长连续休息，导致 `deficit` 被**高估**。后果：Agent 可能因为感知的 deficit 较大而触发 `>60` 门控，或者因为真实 deficit 较小而实际上评测器判定已满足。

但这个差异对 D001 的 3 次违规是**保守的**——评测器（合并算法更宽松）仍然判定违规，说明即使以评测器的宽松标准也不满足 8h。

---

## 3. 违规案例

### 3.1 来源确认

来自 GitHub 版仿真运行结果 `monthly_income_202603.json`：

```json
{
  "driver_id": "D001",
  "preference_check": {
    "rules": [{
      "rule": "每日连续休息≥8小时",
      "penalty": 7200.0,
      "violations": 3,
      "penalty_amount": 2400
    }]
  }
}
```

### 3.2 违规机制推断

每违规日的事件序列（无法精确确认，基于代码逻辑推演）：

```
违规日典型时序：

00:00  决策点: longest=0, deficit=480, remaining=1440
       deficit(480) > 60 → 强制 wait(480) → 休息至 08:00

08:00  决策点: longest=480, deficit=0 → 放行
       接单 → 完成 → 时间推进至 14:00

14:00  决策点: longest=480, deficit=0 → 放行
       接单 → 完成 → 时间推进至 22:00

22:00  决策点: longest=480, deficit=0 → 放行
       接短单 → 完成 → 23:30

23:30  决策点: longest=480, deficit=0 → 放行
       wait(30) → 当天结束

评测器: 合并 wait 区间 = max(480, 30) = 480 ≥ 480 → 满足 ✅
```

上面是**不违规**的天。违规天需要出现"早上没有一次性休够 8h"的情况：

```
违规日时序（推测）：

前一天 22:00  完成长途单, 到达目的地
前一天 22:10  决策: 接短单(2h) → 00:10 完成

00:10  决策点(新的一天): longest=0, deficit=480, remaining=1430
       deficit(480) > 60 → 强制 wait(480) → 休息至 08:10

08:10  决策点: longest=480, deficit=0 → 放行
       接单(4h) → 12:10 完成

12:10  决策点: longest=480, deficit=0 → 放行
       接单(3h) → 15:10 完成

15:10  决策点: longest=480, deficit=0 → 放行
       空驶(1h) → 16:10

16:10  决策点: longest=480, deficit=0 → 放行
       接单(6h) → 22:10 完成

22:10  决策点: longest=480, deficit=0 → 放行
       wait(110) → 00:00 (当天结束)

评测器: 合并 wait 区间 = 480 ≥ 480 → 满足 ✅
```

上面的也是满足的。违规天需要出现**中断了休息连续性**的模式：

```
违规日时序（更可能的情景）：

前一天 23:00  完成订单, 到达目的地
23:00-23:30  决策+等待(30min)
23:30-07:30  wait(480) → 跨天: dayₙ 占 30min, dayₙ₊₁ 占 450min

07:30  决策点(新的一天): longest=450, deficit=30, remaining=990
       ┌─ remaining(990) >= deficit(30) → True
       └─ deficit(30) > 60 → False  ← 门控失败!
       → 不强制休息

07:30  接单A(3h) → 10:30
10:30  接单B(4h) → 14:30
14:30  接单C(5h) → 19:30
19:30  wait(270) → 00:00

当天 wait 区间: [0, 450] 和 [1170, 1440]
评测器合并: max(450, 270) = 450 < 480 → 违规 ❌
罚分: 2,400
```

**关键点：** 跨天休息起始于前一天 23:30，导致当天只计入 450 分钟。07:30 时的 deficit=30，不满足 `>60` 门控 → Agent 全天正常接单 → 最后靠 270 分钟晚间休息无法弥补。

### 3.3 3 次违规的对应

| 违规 | 当天对应日期 | 司机 | 规则 | 罚分 |
|------|------------|------|------|------|
| #1 | 3 月中某日 | D001 | 每日连续休息≥8h | 2,400 |
| #2 | 3 月中某日 | D001 | 每日连续休息≥8h | 2,400 |
| #3 | 3 月中某日 | D001 | 每日连续休息≥8h | 2,400 |
| **合计** | | | | **7,200** |

---

## 4. 反事实分析

### 4.1 如果只改 `>60` → `>0`（不做任何其他改动）

**场景回放（违规日，07:30）：**

```
longest=450, deficit=30, remaining=990
deficit > 0 → True
return min(30, 990) = 30

Agent wait(30) → 08:00
daily_rest_max = max(450, 30) = 450  ← 不变!

08:00 决策点: longest=450, deficit=30, remaining=960
deficit > 0 → True
return min(30, 960) = 30

Agent wait(30) → 08:30
daily_rest_max = max(450, 30) = 450  ← 仍然不变

→ 无限循环。Agent 全天做 30min 的微等待, 永远修不好 deficit。
```

**❌ 仅改阈值会导致新的 bug：无限循环微等待。**

### 4.2 如果同时修改 return 值

将 `return min(deficit, remaining_today)` 改为 `return max(min_hours * 60, remaining_today)` 或等效逻辑：

**场景回放（违规日，07:30）：**

```
longest=450, deficit=30, remaining=990
deficit > 0 → True
return 480  (min_hours * 60)

Agent wait(480) → 15:30
daily_rest_max = max(450, 480) = 480 ≥ 480 → 满足 ✅
```

**✅ 这个违规日被消除。** Agent 从 07:30 休息到 15:30，损失一个上午+半个下午的工作时间。但避免了 2,400 罚分。盈亏平衡点：这笔休息时间如果用来接单，期望净收益 ≈ 8h × 80 元/h = 640 元，远小于 2,400。

### 4.3 是否一定消除 3 次违规？

**不一定全部消除。** 需要区分两种违规场景：

**场景 A（可修复）：** deficit 首次出现时，remaining_today ≥ 480。Agent 可以做一个完整的 8h 休息块来满足需求。
→ 估计 2-3 次属于此场景

**场景 B（不可修复）：** deficit 首次出现时，remaining_today < 480。一天中已经没有足够时间生成 480 分钟的连续休息块。
→ 估计 0-1 次属于此场景

场景 B 发生在 Agent 在傍晚才完成上一个长休息块时（如 18:00 才结束 7h 的休息）。此时 remaining=360，<480，无法补救。

**保守估计：消除 2/3 违规 = 4,800 罚分下降。乐观估计：3/3 = 7,200。**

### 4.4 是否可能引入新问题？

| 潜在问题 | 分析 | 结论 |
|---------|------|------|
| 过度休息降低收益 | 480min 休息 ≈ 损失 8h × 80元/h = 640元期望收益。但避免了 2,400 罚分。净收益改善 +1,760。 | 不会 |
| Phase 1 与 Phase 2 冲突 | Phase 1（gamma 强制休整天）与 Phase 2 都操作 wait。Phase 1 先执行。两者目标一致（休息），不会冲突。 | 不会 |
| 月初过度保守 | 月初(day<5)的 daily_rest 需求同样会触发 8h 强制休息。但月初休息本来就是合理的——早晨 8h 休息后下午仍可工作。 | 不会 |
| 与其他偏好规则冲突 | 8h 休息期间不会违反任何偏好（位置不变）。不会触发 forbidden_region、max_pickup_km 等。 | 不会 |
| 无限循环 | 仅改阈值不改 return 值会导致微等待循环，见 4.1。必须同步修改 return 逻辑。 | **会——必须同步修复** |

### 4.5 是否可能降低收益？

8h 强制休息替代了 8h 工作时间 + 2,400 罚分。期望净效果：

```
收益变化 = -8h × 期望时薪 + 2,400
         = -8 × 80 + 2,400
         = -640 + 2,400
         = +1,760 元/违规日
```

**净收益上升。**

---

## 5. 可信度评分

### 5.1 总体评分：72 / 100

| 维度 | 评分 | 说明 |
|------|------|------|
| 逻辑漏洞的存在性 | 95/100 | 代码检查确认: `>60` vs 评测器 `>0` 不一致，Layer 1/2/3 在[1,60]区间全开 |
| 罚分归因准确性 | 80/100 | 3 次违规 = 7200 罚分来自评测器输出，确认是 daily_rest 规则 |
| 修复的充分性 | 55/100 | **仅改阈值不够**，必须同时改 return 值。否则引入无限循环 |
| 修复后罚分消除率 | 70/100 | 估计消除 2/3 违规。第 3 个取决于违规日剩余时间 |
| 无副作用 | 60/100 | 8h 午休可能错过好单，但罚分 >> 期望收益 |

### 5.2 7,200 预估的可靠性

**7,200 是乐观估计（3/3 违规消除）。**
**4,800 是保守估计（2/3 违规消除）。**
**期望值 ≈ 5,600。**

剩余不确定性来源于无法访问 actions JSONL 日志确认每个违规日的精确时间线。

### 5.3 修正建议

P0-1 应改写为 **两处同步修改**，而非一行改动：

```
修改 1: dispatcher.py:599
  deficit > 60  →  deficit > 0

修改 2: dispatcher.py:600
  return min(deficit, remaining_today)
  →
  return max(min_hours * 60, remaining_today)  // 或 min(min_hours * 60, remaining_today)
```

不做修改 2 的情况下单独做修改 1，会引入无限循环微等待。**两处必须同步上线。**
