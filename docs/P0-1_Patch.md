# P0-1 Patch：daily_rest 阈值修复

**修改文件：** `demo/agent/strategy/dispatcher.py`（唯一修改文件）
**修改范围：** `_check_mandatory_rest()` 方法内的 `daily_rest` case
**新增行数：** +2 行（替换 2 行）

---

# Patch

## 修改前（dispatcher.py:590-600）

```python
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
```

## 修改后（dispatcher.py:590-602）

```python
            if rule.rule_type == "daily_rest":
                min_hours = int(rule.params.get("min_hours", 0))
                if min_hours <= 0:
                    continue
                longest_today = daily_rest_max.get(day_idx, 0)
                deficit = min_hours * 60 - longest_today
                if deficit <= 0:
                    continue
                remaining_today = time_utils.minutes_until_next_day(sim_min)
                if deficit > 0:
                    need_rest = min_hours * 60
                    if remaining_today >= need_rest:
                        return need_rest
                    return remaining_today
```

## Diff 摘要

```
- line 599: if remaining_today >= deficit and deficit > 60:
- line 600:     return min(deficit, remaining_today)
+ line 599: if deficit > 0:
+ line 600:     need_rest = min_hours * 60
+ line 601:     if remaining_today >= need_rest:
+ line 602:         return need_rest
+ line 603:     return remaining_today
```

## 改动解释

| 改动 | 原因 |
|------|------|
| `deficit > 60` → `deficit > 0` | 与评测器 `_eval_daily_rest` 的口径对齐：评测器对任何缺口 > 0 即判违规 |
| `min(deficit, remaining_today)` → `min_hours * 60` | `min(deficit, ...)` 返回小值（如 30min）无法创建新的最长连续休息块。`min_hours * 60`（如 480min）确保 wait 时长超过当前最长块，成为新的 `daily_rest_max` |
| 删除 `remaining_today >= deficit` 前置条件 | 不再需要。即使 `remaining_today < deficit`（无法完全补救），仍然返回 `remaining_today`（休息到当天结束），阻止 Agent 继续接单恶化局面 |
| 新增 `remaining_today >= need_rest` 分支 | 区分"可以做完整休息块"和"只能休息到当天结束"两种情况 |

---

# Side Effect

## 潜在副作用

| 副作用 | 场景 | 影响分析 | 严重度 |
|--------|------|---------|--------|
| 额外休息时间 | deficit=30, remaining=600 → Agent 将休息 480min 而不仅仅是 30min | 损失 ~8h 工作时间 ≈ 640 元期望收益。但避免了 2,400 罚分。**净收益 +1,760** | 🟢 正面 |
| 月末最后一天过度休息 | day=30, deficit>0, remaining>=480 → Agent 休息 8h，损失月末赚钱机会 | 月末剩余时间 ≥ 8h 时触发。但 2,400 罚分 > 任何 8h 内的期望收益。除非有罕见高价单（>3,000 元净收益） | 🟢 正面 |
| 月初强制全休 | day=0, 凌晨 0:00, deficit=480 → 原本就满足 `>60`，行为不变 | 此改动对 deficit>60 的场景无影响（原本已触发强制休息） | ⚪ 无变化 |
| 跨天休息 | remaining=240, need_rest=480 → 返回 240（休息到当天结束） | 当天的 daily_rest 仍然违规（评测器只计入 240min）。但 Agent 至少不再接单恶化。此场景对应"当天已无法补救"的情况 | 🟡 当天仍违规 |
| off_days 的强制 rest 不受影响 | off_days case 在 daily_rest case 之后（line 602+），逻辑独立 | 两个 case 互不干扰 | ⚪ 无变化 |

## 不会发生的副作用

- **不会无限循环**：返回 `need_rest`（480min）> 当前 `longest_today`（最多 479），必然创建新的 `daily_rest_max`。下一次决策时 `deficit <= 0`。
- **不会影响 rest_window**：rest_window case（line 582-588）在 daily_rest case 之前，逻辑独立。
- **不会影响 D002**：D002 没有 daily_rest 规则（语法是 rest_window），此 case 对其不触发。
- **不影响其他 driver**：只有配置了 daily_rest 偏好的司机会进入此分支。

---

# Validation

## 验证步骤

### Step 1：单元级验证

在 D001 30 天仿真前后对比：

```bash
cd demo/server
python main.py    # 修改前 → 记录 baseline/monthly_income_202603.json
# 应用 patch
python main.py    # 修改后 → 对比 monthly_income_202603.json
```

### Step 2：检查项

| 检查项 | 预期结果 | 判定标准 |
|--------|---------|---------|
| D001 daily_rest violations | 从 3 → 0 或 1 | `preference_check.rules[0].violations` |
| D001 毛收入变化 | 轻微下降（< 2,000 元） | 额外休息时间替代了工作时间 |
| D001 净收入变化 | 上升 | 罚分下降 > 收益下降 |
| D002 全部指标 | 完全不变 | D002 无 daily_rest 规则 |
| 仿真不崩溃 | 正常完成 | 无异常日志 |
| 无 rest_window 异常 | violations 数量不变 | D002 rest_window 不应受影响 |

### Step 3：回归检查

```bash
cd demo
python calc_monthly_income.py
```

确认：
- `summary.failed_driver_count == 0`
- `summary.total_net_income_all_drivers > 14,905`（当前基线）
- D002 所有指标与修改前完全一致

### Step 4：日志抽查

搜索 agent 日志中的 daily_rest 相关输出，确认新逻辑触发正确：

```
grep "daily_rest\|mandatory rest\|REST.*day=" results/logs/simulation_orchestrator.log
```

预期看到新增的强制休息日志，且对应的 violations 减少。

---

# Rollback

## 回滚方式

```bash
git checkout -- demo/agent/strategy/dispatcher.py
```

或手动恢复 lines 599-603 为原始版本：

```python
                if remaining_today >= deficit and deficit > 60:
                    return min(deficit, remaining_today)
```

## 回滚条件

如果出现以下任一情况，立即回滚：

- D001 净收益**下降**（说明额外休息的成本超过了罚分节省）
- 仿真运行时间显著增加（> 2 倍，说明出现了逻辑循环）
- D002 任何指标发生变化（说明改动影响了无关路径）
- `failed_driver_count > 0`

---

# 变更统计

| 指标 | 值 |
|------|-----|
| 修改文件数 | 1 |
| 修改函数数 | 1 |
| 新增行数 | 3 |
| 删除行数 | 2 |
| 净增行数 | +1 |
| 受影响司机 | 仅含 `daily_rest` 偏好规则的司机（当前仅 D001） |
| 预计罚分下降 | 4,800 ~ 7,200 元 |
| 预计收益影响 | -300 ~ -1,000 元（因额外休息） |
| 预计净收益变化 | **+3,800 ~ +6,900 元** |
