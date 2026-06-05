"""多体演化沙箱：从 D001/D002 种子模板动态生成 50 名长尾虚拟司机。"""
from __future__ import annotations

import copy
import json
import random
import shutil
import math
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRIVERS_PATH = PROJECT_ROOT / "server" / "data" / "drivers.json"
DRIVERS_BAK = DRIVERS_PATH.with_suffix(".json.bak")

random.seed(42)

# ── 种子模板 ──────────────────────────────────────────────────
CITIES = [
    ("广州", 23.13, 113.26), ("深圳", 22.54, 114.06), ("东莞", 23.02, 113.75),
    ("佛山", 23.02, 113.12), ("中山", 22.52, 113.38), ("珠海", 22.27, 113.58),
    ("惠州", 23.11, 114.42), ("江门", 22.59, 113.08), ("肇庆", 23.05, 112.47),
    ("清远", 23.70, 113.03), ("湛江", 21.27, 110.36), ("茂名", 21.66, 110.92),
    ("阳江", 21.86, 111.98), ("韶关", 24.80, 113.59), ("河源", 23.74, 114.70),
    ("梅州", 24.29, 116.12), ("汕尾", 22.78, 115.37), ("汕头", 23.35, 116.68),
    ("揭阳", 23.55, 116.37), ("潮州", 23.66, 116.63), ("云浮", 22.92, 112.04),
    ("增城", 23.15, 113.67), ("四会", 23.32, 112.83),
]

CATEGORIES_POOL = ["蔬菜", "机械设备", "建材", "家具家居", "食品饮料", "数码家电", "钢材", "木材质板"]
LAST_NAMES = ["陈", "林", "李", "王", "张", "刘", "黄", "周", "吴", "郑",
              "赵", "钱", "孙", "杨", "马", "朱", "胡", "何", "罗", "梁"]

VEHICLE_LENGTHS = ["4.2米", "5米", "6.2米", "9.6米"]

# ── 偏好模板库 ────────────────────────────────────────────────

def _daily_rest(mid: int) -> dict:
    hours = random.choice([6, 7, 8, 9, 10])
    return {
        "content": f"每天必须连续休息满{hours}小时，车得停着熄火。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": hours * 300, "penalty_cap": hours * 300 * 31,
    }

def _rest_window(mid: int) -> dict:
    start = random.choice([0, 1, 22, 23])
    end = (start + random.choice([6, 7, 8])) % 24
    return {
        "content": f"每天{start}点到{end}点我必须休息，雷打不动。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": 1800, "penalty_cap": 55800,
    }

def _off_days(mid: int) -> dict:
    days = random.choice([2, 3, 4])
    return {
        "content": f"三月至少休{days}个整天，车完全不动。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": 10000, "penalty_cap": 10000,
    }

def _forbidden_category(mid: int) -> dict:
    cat = random.choice(CATEGORIES_POOL)
    return {
        "content": f"{cat}这活儿我干不了，每接一次都扣钱。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": random.choice([1200, 2100, 3400]), "penalty_cap": None,
    }

def _max_pickup_km(mid: int) -> dict:
    km = random.choice([30, 40, 50, 60, 80, 100])
    return {
        "content": f"空驶超过{km}公里我就不想接，超一次扣120。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": 120, "penalty_cap": None,
    }

def _forbidden_region(mid: int) -> dict:
    region = random.choice([c[0] for c in CITIES if c[0] not in ("广州", "深圳")])
    return {
        "content": f"装货地或卸货地在{region}的货，我一律不接，每接一次都要扣钱。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": random.choice([500, 800, 1200]), "penalty_cap": None,
    }

def _min_region_days(mid: int, region: str = "增城", min_days: int = 5) -> dict:
    return {
        "content": f"我在{region}有档口要照应，每月装货或卸货在{region}的货起码接够{min_days}个不同日子。",
        "start_time": "2026-03-01 00:00:00", "end_time": "2026-03-31 23:59:59",
        "penalty_amount": 6000, "penalty_cap": 6000,
    }

def _day_specific_avoid(mid: int) -> dict:
    day = random.randint(5, 28)
    region = random.choice(["深圳", "广州", "东莞", "惠州"])
    return {
        "content": f"三月{day}号交警在{region}查车，这天我不往{region}跑。",
        "start_time": f"2026-03-{day-1:02d} 00:00:00", "end_time": f"2026-03-{day+1:02d} 23:59:59",
        "penalty_amount": 3000, "penalty_cap": 3000,
    }

def _multi_stop_route(mid: int) -> dict:
    """跨天长途：从 A 城市赶到 B 城市"""
    cities = random.sample(CITIES[:20], 2)
    day = random.randint(20, 30)
    return {
        "content": (
            f"三月{day}号要去{cities[0][0]}办事，"
            f"上午得先到{cities[0][0]}停两小时，中午十二点前赶到{cities[1][0]}赴约。"
        ),
        "start_time": f"2026-03-{day-1:02d} 00:00:00", "end_time": f"2026-03-{day:02d} 23:59:59",
        "penalty_amount": 5000, "penalty_cap": 5000,
    }

# ── 司机生成 ──────────────────────────────────────────────────

def _generate_driver(driver_id: str, idx: int) -> dict:
    city_name, clat, clng = CITIES[idx % len(CITIES)]
    last_name = LAST_NAMES[idx % len(LAST_NAMES)]
    truck = VEHICLE_LENGTHS[idx % len(VEHICLE_LENGTHS)]

    prefs: list[dict] = []

    # A. 区域内卷型 (S001-S020): 增城5天
    if 0 <= idx < 20:
        prefs.append(_min_region_days(idx, "增城", 5))
    # B. 跨天长途型 (S021-S030): 多站路线
    if 20 <= idx < 30:
        prefs.append(_multi_stop_route(idx))
    # C. 红线高压型 (S031-S040): daily_rest + rest_window
    if 30 <= idx < 40:
        prefs.append(_daily_rest(idx))
        prefs.append(_rest_window(idx))
    # D. 品类挑剔型 (S041-S050): 随机禁接品类
    if 40 <= idx < 50:
        prefs.append(_forbidden_category(idx))
        prefs.append(_forbidden_category(idx + 100))  # 双品类

    # 公共偏好：每人随机 2-4 个额外偏好
    extra_pool = [_off_days, _max_pickup_km, _forbidden_region, _day_specific_avoid]
    if idx >= 20:
        extra_pool.append(_daily_rest)
    if idx < 30:
        extra_pool.append(_rest_window)

    n_extra = random.randint(2, 4)
    for _ in range(n_extra):
        fn = random.choice(extra_pool)
        prefs.append(fn(idx))

    return {
        "driver_id": driver_id,
        "name": f"{last_name}师傅",
        "vehicle_no": f"粤{chr(65 + idx % 26)}{10000 + idx:05d}",
        "truck_length": truck,
        "cost_per_km": round(random.uniform(1.3, 1.8), 2),
        "current_lat": round(clat + random.uniform(-0.15, 0.15), 4),
        "current_lng": round(clng + random.uniform(-0.15, 0.15), 4),
        "preferences": prefs,
    }


def main() -> int:
    if not DRIVERS_PATH.exists():
        print(f"[ERROR] drivers.json not found at {DRIVERS_PATH}")
        return 1

    # 备份
    shutil.copy2(DRIVERS_PATH, DRIVERS_BAK)
    print(f"[OK] backed up to {DRIVERS_BAK}")

    # 保留原始 D001/D002 作对照
    with open(DRIVERS_PATH, "r", encoding="utf-8") as f:
        original = json.load(f)

    # 生成 50 名沙箱司机
    sandbox = list(original)  # keep D001, D002
    for i in range(50):
        sid = f"S{i+1:03d}"
        driver = _generate_driver(sid, i)
        sandbox.append(driver)

    # 写入
    with open(DRIVERS_PATH, "w", encoding="utf-8") as f:
        json.dump(sandbox, f, ensure_ascii=False, indent=2)

    print(f"[OK] wrote {len(sandbox)} drivers to {DRIVERS_PATH}")
    print(f"     D001/D002 (seeds) + S001-S050 (sandbox)")

    # 统计
    total_prefs = sum(len(d["preferences"]) for d in sandbox)
    print(f"     total preferences: {total_prefs}")
    print(f"     categories: A(增城卷)={sum(1 for i in range(50) if 0<=i<20)}")
    print(f"                B(跨天长途)={sum(1 for i in range(50) if 20<=i<30)}")
    print(f"                C(红线高压)={sum(1 for i in range(50) if 30<=i<40)}")
    print(f"                D(品类挑剔)={sum(1 for i in range(50) if 40<=i<50)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
