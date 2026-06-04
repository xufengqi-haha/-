"""地理计算工具：Haversine距离、网格编码、区域判定。"""

from __future__ import annotations

import math
from typing import Any

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """球面大圆距离（公里）。"""
    p1 = math.radians(lat1)
    l1 = math.radians(lng1)
    p2 = math.radians(lat2)
    l2 = math.radians(lng2)
    dp = p2 - p1
    dl = l2 - l1
    h = (
        math.sin(dp * 0.5) ** 2
        + math.cos(p1) * math.cos(p2) * (math.sin(dl * 0.5) ** 2)
    )
    h = min(1.0, max(0.0, h))
    return 2.0 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def grid_key(lat: float, lng: float, resolution: float = 0.1) -> str:
    """将经纬度映射到网格键（分辨率默认0.1°≈11km）。"""
    lat_bin = int(lat / resolution)
    lng_bin = int(lng / resolution)
    return f"{lat_bin}:{lng_bin}"


def distance_to_minutes(distance_km: float, speed_km_per_hour: float = 60.0) -> int:
    """距离→耗时（分钟），ceil取整，最少1分钟。"""
    if distance_km <= 0:
        return 1
    return max(1, math.ceil((distance_km / speed_km_per_hour) * 60.0))


def in_bounding_box(
    lat: float,
    lng: float,
    lat_min: float,
    lat_max: float,
    lng_min: float,
    lng_max: float,
) -> bool:
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def midpoint(lat1: float, lng1: float, lat2: float, lng2: float) -> tuple[float, float]:
    return ((lat1 + lat2) / 2.0, (lng1 + lng2) / 2.0)


# 统一区域坐标字典（dispatcher 和 preference_scorer 共用）
REGION_COORDINATES: dict[str, tuple[float, float]] = {
    "深圳": (22.54, 114.06),
    "惠州": (23.11, 114.42),
    "增城": (23.15, 113.67),
    "广州": (23.13, 113.26),
    "东莞": (23.02, 113.75),
    "佛山": (23.02, 113.12),
    "中山": (22.52, 113.38),
    "珠海": (22.27, 113.58),
    "江门": (22.59, 113.08),
    "肇庆": (23.05, 112.47),
    "四会": (23.32, 112.83),
}

# 常用区域名称列表（用于遍历检查）
COMMON_REGION_NAMES = ["增城", "深圳", "惠州", "广州", "东莞", "佛山", "中山", "四会"]


def region_center(name: str) -> tuple[float, float] | None:
    """获取区域中心坐标。"""
    return REGION_COORDINATES.get(name)


def near_region(pos: dict[str, Any], region_name: str, radius_km: float = 30.0) -> bool:
    """判断位置是否在区域中心指定半径内。"""
    center = REGION_COORDINATES.get(region_name)
    if center is None:
        return False
    lat = float(pos.get("lat", 0))
    lng = float(pos.get("lng", 0))
    return haversine_km(lat, lng, center[0], center[1]) <= radius_km
