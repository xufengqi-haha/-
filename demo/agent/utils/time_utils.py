"""仿真时间工具：分钟↔日期转换、夜间判定、时间窗口计算。"""

from __future__ import annotations

from datetime import datetime, timedelta

SIMULATION_EPOCH = datetime(2026, 3, 1, 0, 0, 0)
_WALL_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def sim_min_to_datetime(sim_min: int) -> datetime:
    return SIMULATION_EPOCH + timedelta(minutes=int(sim_min))


def sim_min_to_day(sim_min: int) -> int:
    """返回0-based天数索引（0=3月1日，30=3月31日）。"""
    return int(sim_min) // 1440


def sim_min_to_hour_of_day(sim_min: int) -> float:
    return (int(sim_min) % 1440) / 60.0


def sim_min_to_wall_time(sim_min: int) -> str:
    return (SIMULATION_EPOCH + timedelta(minutes=int(sim_min))).strftime(_WALL_TIME_FMT)


def day_bounds(day: int) -> tuple[int, int]:
    """返回第day天(0-based)的开始/结束仿真分钟数。"""
    return day * 1440, (day + 1) * 1440


def is_night_time(sim_min: int, night_start_hour: int = 0, night_end_hour: int = 6) -> bool:
    hour = sim_min_to_hour_of_day(sim_min)
    return hour >= night_start_hour and hour < night_end_hour


def minutes_until_next_day(sim_min: int) -> int:
    return 1440 - (int(sim_min) % 1440)


def minutes_until_target_hour(sim_min: int, target_hour: int) -> int:
    """到下一个target_hour（0-23）还需多少分钟。
    如果当前已到达或超过target_hour，返回到明天同一时刻的分钟数。
    如果恰好在target_hour整点，返回0（已到达）。"""
    current_hour_min = int(sim_min) % 1440
    target_min = target_hour * 60
    if current_hour_min < target_min:
        return target_min - current_hour_min
    if current_hour_min == target_min:
        return 0
    return 1440 - current_hour_min + target_min


def minutes_until_target_hour_next(sim_min: int, target_hour: int) -> int:
    """到下一个target_hour的分钟数，即使当前恰好在target_hour也返回明天。
    用于需要严格等到下一个周期开始的场景。"""
    result = minutes_until_target_hour(sim_min, target_hour)
    if result == 0:
        return 1440
    return result


def format_datetime(sim_min: int) -> str:
    return sim_min_to_datetime(sim_min).strftime(_WALL_TIME_FMT)


def hour_in_range(hour: float, start: int, end: int) -> bool:
    """检查hour是否在[start, end]区间内（支持跨天区间）。"""
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end
