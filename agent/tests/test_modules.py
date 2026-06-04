"""快速验证脚本：确保所有新模块导入正确、核心逻辑断言通过。"""

from __future__ import annotations

import sys
from pathlib import Path

_DEMO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEMO_ROOT))


def test_imports():
    from agent.utils import geo_utils, time_utils
    from agent.memory.area_memory import AreaMemory
    from agent.scoring.cargo_scorer import CargoScorer, CargoScore, ScorerConfig
    from agent.scoring.preference_scorer import (
        PreferenceParser,
        PreferenceChecker,
        PreferenceRule,
        _parse_int,
        _strip_region_suffix,
    )
    from agent.strategy.dispatcher import DecisionDispatcher
    from agent.model_decision_service import ModelDecisionService
    print("[PASS] All imports OK")


def test_chinese_numeral():
    from agent.scoring.preference_scorer import _parse_int
    cases = {("零", 0), ("一", 1), ("五", 5), ("六", 6), ("十", 10), ("十二", 12),
             ("二十", 20), ("五十五", 55), ("两", 2), ("俩", 2), ("8", 8)}
    for text, expected in cases:
        assert _parse_int(text) == expected, f"_parse_int({text!r}) = {_parse_int(text)} != {expected}"
    print("[PASS] Chinese numeral parsing OK")


def test_geo():
    from agent.utils.geo_utils import haversine_km, grid_key
    d = haversine_km(22.54, 114.06, 23.13, 113.26)
    assert 95.0 < d < 115.0, f"Haversine SZ->GZ ~105km, got {d:.1f}"
    key = grid_key(22.54, 114.06)
    assert ":" in key
    print(f"[PASS] Geo utils OK (SZ->GZ = {d:.1f} km)")


def test_time_utils():
    from agent.utils.time_utils import sim_min_to_day, sim_min_to_hour_of_day, minutes_until_target_hour
    assert sim_min_to_day(0) == 0
    assert sim_min_to_day(1440) == 1
    assert sim_min_to_day(1440 * 30) == 30
    h = sim_min_to_hour_of_day(360)  # 6:00 AM
    assert abs(h - 6.0) < 0.01, f"Expected 6.0, got {h}"
    wait = minutes_until_target_hour(60, 6)  # 1:00 AM → 6:00 AM = 5h = 300min
    assert wait == 300, f"Expected 300 min, got {wait}"
    print("[PASS] Time utils OK")


def test_preference_parsing():
    from agent.scoring.preference_scorer import PreferenceParser
    parser = PreferenceParser()
    d001 = [
        {"content": "我这人熬不住连轴转，每天至少连续停车熄火休息满8小时。", "penalty_amount": 2400, "penalty_cap": 74400},
        {"content": "接过两次龙门吊底座、机床铸件，死沉还容易砸坏挡板，机械设备这类活儿我干不了，每接一次都要扣钱。", "penalty_amount": 2100, "penalty_cap": None},
        {"content": "惠州那一路我熟也怕，装货地或卸货地在惠州的货，我一律不接，每接一次都要扣钱。", "penalty_amount": 800, "penalty_cap": None},
        {"content": "爹妈在老家盼着，三月怎么也得抽三个整天完全歇着（00:00~24:00，车处于静止状态），回去陪陪二老。", "penalty_amount": 10000, "penalty_cap": 10000},
        {"content": "三月四号五号交警在深圳查车，这天我不往深圳跑，也别给我派进去那边的货。", "penalty_amount": 3000, "penalty_cap": 3000},
    ]
    d002 = [
        {"content": "零点以后到早上六点这段我得睡觉，车得停着熄火，雷打不动。", "penalty_amount": 1800, "penalty_cap": 55800},
        {"content": "蔬菜几分钱一斤的路一颠就烂，赔不起损耗，凡是蔬菜货源我一律推掉，每接一次都扣钱。", "penalty_amount": 3400, "penalty_cap": None},
        {"content": "我在广州市增城区有家老档口（23.15，113.67）要照应，自然月里装货或卸货在增城的货，起码得接够四个不同的日子，同一天几单也只算一天。", "penalty_amount": 6000, "penalty_cap": 6000},
        {"content": "看好了单子再动身，接单后赶去装货那一程，空驶超过五十五公里我就不想接，超一次扣120。", "penalty_amount": 120, "penalty_cap": None},
        {"content": "这月得进修理厂做保养，起码留两个整天停驶检修，那天别给我排活。", "penalty_amount": 10000, "penalty_cap": 10000},
        {"content": "三月十二号增城老档口清库存，当天得到增城区停一趟，花两小时把数目对清楚。", "penalty_amount": 5000, "penalty_cap": 5000},
        {"content": "三月三十一号舅公做寿，上午得先过增城区档口捎上寿礼，中午十二点前赶到四会县城（23.32，112.83）赴宴到下午两点。", "penalty_amount": 5000, "penalty_cap": 5000},
    ]
    rules_d001 = parser.parse(d001)
    rules_d002 = parser.parse(d002)
    assert len(rules_d001) == 5, f"D001: expected 5, got {len(rules_d001)}"
    assert len(rules_d002) == 7, f"D002: expected 7, got {len(rules_d002)}"
    d001_types = {r.rule_type for r in rules_d001}
    assert "daily_rest" in d001_types
    assert "forbidden_category" in d001_types
    assert "day_specific_avoid" in d001_types
    d002_types = {r.rule_type for r in rules_d002}
    assert "rest_window" in d002_types
    assert "max_pickup_km" in d002_types
    assert "off_days" in d002_types
    print(f"[PASS] Preference parsing OK (D001:{len(rules_d001)}, D002:{len(rules_d002)})")


def test_preference_checker():
    from agent.scoring.preference_scorer import PreferenceParser, PreferenceChecker
    parser = PreferenceParser()
    d001_prefs = [
        {"content": "机械设备这类活儿我干不了，每接一次都要扣钱。", "penalty_amount": 2100, "penalty_cap": None},
        {"content": "装货地或卸货地在惠州的货，我一律不接。", "penalty_amount": 800, "penalty_cap": None},
        {"content": "三月四号五号交警在深圳查车，这天我不往深圳跑。", "penalty_amount": 3000, "penalty_cap": 3000},
    ]
    d002_prefs = [
        {"content": "蔬菜几分钱一斤的路一颠就烂，凡是蔬菜货源我一律推掉。", "penalty_amount": 3400, "penalty_cap": None},
        {"content": "空驶超过五十五公里我就不想接。", "penalty_amount": 120, "penalty_cap": None},
    ]
    rules_d001 = parser.parse(d001_prefs)
    checker_d001 = PreferenceChecker(rules_d001)
    mech = {"cargo_id": "C1", "cargo_name": "龙门吊底座运输", "start": {"city": "东莞"}, "end": {"city": "广州"}, "price": 800}
    p1, _ = checker_d001.check_cargo_weighted(mech, 10.0, 0)
    assert p1 > 0, "机械设备 should be penalized"
    hz = {"cargo_id": "C2", "cargo_name": "普通货", "start": {"city": "惠州"}, "end": {"city": "广州"}, "price": 500}
    p2, _ = checker_d001.check_cargo_weighted(hz, 10.0, 0)
    assert p2 > 0, "惠州 cargo should be penalized"
    sz = {"cargo_id": "C3", "cargo_name": "电子", "start": {"city": "深圳"}, "end": {"city": "广州"}, "price": 1500}
    p3_day4, _ = checker_d001.check_cargo_weighted(sz, 10.0, 4 * 1440)
    p3_day10, _ = checker_d001.check_cargo_weighted(sz, 10.0, 10 * 1440)
    assert p3_day4 > 0, "深圳 on day 4 should be penalized"
    assert p3_day10 == 0, f"深圳 on day 10 should NOT be penalized, got {p3_day10}"

    rules_d002 = parser.parse(d002_prefs)
    checker_d002 = PreferenceChecker(rules_d002)
    veg = {"cargo_id": "C4", "cargo_name": "青菜运输", "start": {"city": "深圳"}, "end": {"city": "广州"}, "price": 300}
    p4, _ = checker_d002.check_cargo_weighted(veg, 10.0, 0)
    assert p4 > 0, "蔬菜 should be penalized"
    far = {"cargo_id": "C5", "cargo_name": "普通", "start": {"city": "广州"}, "end": {"city": "深圳"}, "price": 600}
    p5, _ = checker_d002.check_cargo_weighted(far, 70.0, 0)
    assert p5 > 0, "70km pickup should be penalized"
    normal = {"cargo_id": "C6", "cargo_name": "电子", "start": {"city": "深圳"}, "end": {"city": "广州"}, "price": 1500}
    p6, _ = checker_d002.check_cargo_weighted(normal, 15.0, 0)
    assert p6 == 0, f"Normal cargo should NOT be penalized, got {p6}"
    print("[PASS] Preference checker OK (7 assertions)")


def test_cargo_scorer():
    from agent.scoring.cargo_scorer import CargoScorer
    from agent.memory.area_memory import AreaMemory
    scorer = CargoScorer()
    area = AreaMemory()
    area.update_from_cargo(22.54, 114.06, [{"cargo": {"start": {"lat": 23.13, "lng": 113.26}, "price": 1000}}])
    good = {"cargo_id": "G", "cargo_name": "电子", "price": 1500, "cost_time_minutes": 120,
            "start": {"lat": 22.54, "lng": 114.06}, "end": {"lat": 23.13, "lng": 113.26}}
    bad = {"cargo_id": "B", "cargo_name": "废品", "price": 100, "cost_time_minutes": 2000,
           "start": {"lat": 40.0, "lng": 116.0}, "end": {"lat": 30.0, "lng": 120.0}}
    s_good = scorer.score(good, 10.0, 0, area, 0)
    s_bad = scorer.score(bad, 150.0, 0, area, 3000)
    assert s_good.total_score > 0.3, f"Good cargo score {s_good.total_score} should be > 0.3"
    assert s_good.total_score > s_bad.total_score, f"Good({s_good.total_score}) > Bad({s_bad.total_score})"
    assert s_good.net_profit > 0, f"Good cargo net profit {s_good.net_profit} should be > 0"
    print(f"[PASS] Cargo scorer OK (good={s_good.total_score:.3f}, bad={s_bad.total_score:.3f})")


def test_area_memory():
    from agent.memory.area_memory import AreaMemory
    area = AreaMemory()
    area.update_from_cargo(22.54, 114.06, [
        {"cargo": {"start": {"lat": 23.13, "lng": 113.26}, "price": 1000}},
        {"cargo": {"start": {"lat": 23.13, "lng": 113.26}, "price": 1500}},
    ])
    heat = area.get_heat(23.13, 113.26)
    assert heat > 0, f"Heat should be > 0, got {heat}"
    zones = area.suggest_reposition(22.54, 114.06, max_distance_km=300)
    assert len(zones) > 0, "Should find at least 1 hot zone"
    print(f"[PASS] Area memory OK (GZ heat={heat:.3f}, hot_zones={len(zones)})")


def test_dispatcher_structure():
    from agent.strategy.dispatcher import DecisionDispatcher
    d = DecisionDispatcher
    assert hasattr(d, "decide"), "Dispatcher must have decide method"
    actions = ["_make_take_order", "_make_wait", "_make_reposition"]
    for a in actions:
        assert hasattr(d, a), f"Dispatcher must have {a}"
    print("[PASS] Dispatcher structure OK")


if __name__ == "__main__":
    test_imports()
    test_chinese_numeral()
    test_geo()
    test_time_utils()
    test_preference_parsing()
    test_preference_checker()
    test_cargo_scorer()
    test_area_memory()
    test_dispatcher_structure()
    print("\n===== ALL 9 TEST MODULES PASSED =====")
