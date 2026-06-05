"""偏好解析与检查：自然语言偏好→结构化规则→候选货源合规检查。
v2: 修复 hard_forbidden 为 rule_type 语义判断；统一区域数据源；新增 check_reposition。"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.utils.geo_utils import haversine_km, REGION_COORDINATES
from agent.utils.time_utils import hour_in_range

_CN_NUM = r'(?:[零一二两三四五六七八九十廿卅百俩]+|\d+)'
_HAN = r'[一-鿿]'

_REGION_SUFFIX_RE = re.compile(r'[区县市镇乡]$')

# 平台红线（法律/安全）：一票否决，不进经济账
_PLATFORM_HARD_RULES: set[str] = {
    "rest_window",
    "daily_rest",
}

# 司机软偏好：参与经济账，不硬过滤
# forbidden_category, forbidden_region_*, day_specific_*, max_pickup_km,
# off_days, min_days_in_region, day_specific_location — 全部走偏好评分


def _parse_int(text: str) -> int:
    t = str(text).strip()
    if not t:
        return 0
    if t.isdigit():
        return int(t)
    mapping = {
        "零": 0, "一": 1, "二": 2, "两": 2, "俩": 2,
        "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
        "八": 8, "九": 9, "十": 10, "廿": 20, "卅": 30,
        "百": 100,
    }
    if t in mapping:
        return mapping[t]
    if t.startswith("十"):
        return 10 + mapping.get(t[1], 0)
    if t.endswith("十"):
        return mapping.get(t[0], 0) * 10
    if "十" in t:
        parts = t.split("十", 1)
        return mapping.get(parts[0], 0) * 10 + mapping.get(parts[1], 0)
    total = 0
    for i, ch in enumerate(reversed(t)):
        val = mapping.get(ch, 0)
        if val >= 10 and i == 0:
            total += val
        elif val > 0:
            total += val * (10 ** i)
    return total


def _strip_region_suffix(name: str) -> str:
    return _REGION_SUFFIX_RE.sub("", name)


@dataclass
class PreferenceRule:
    rule_type: str
    penalty_amount: float
    penalty_cap: float | None
    params: dict[str, Any] = field(default_factory=dict)
    original_content: str = ""
    stops: list[dict[str, Any]] = field(default_factory=list)

    def max_penalty(self) -> float:
        if self.penalty_cap is not None:
            return min(self.penalty_amount, self.penalty_cap)
        return self.penalty_amount

    @property
    def is_hard(self) -> bool:
        """是否为平台红线（rest_window/daily_rest）— 一票否决制。"""
        return self.rule_type in _PLATFORM_HARD_RULES

    @property
    def is_soft(self) -> bool:
        """是否为司机偏好 — 参与经济账。"""
        return not self.is_hard and self.rule_type != "unknown"


class PreferenceParser:
    _PATTERNS: list[tuple[str, str]] = [
        (
            r"([一二三]?\s*" + _CN_NUM + r")\s*[月号日]"
            r"(?:\s*(?:[和、及]|[,，])?\s*(?:[一二三]?\s*" + _CN_NUM + r")\s*[号日])?"
            r".*?(?:交警|查车).*?"
            r"(?:不[往进到]\s*(" + _HAN + r"{2,4}?)|"
            r"别.*?派.*?进.*?(" + _HAN + r"{2,4}?))",
            "day_specific_avoid",
        ),
        (
            r"([一二三]?\s*" + _CN_NUM + r")\s*[月号日]"
            r"(?:\s*(?:[和、]|[,，])?\s*(?:[一二三]?\s*" + _CN_NUM + r")\s*[号日])?"
            r".*?(?:到|在|去)\s*(" + _HAN + r"{2,4}?)\s*(?:区|县|市)?"
            r".*?(?:停|花|待|把).*?(?:[一两二三四五六七八九十\d]+)\s*小?时",
            "day_specific_location",
        ),
        (
            r"(" + _CN_NUM + r")\s*点.*?(?:以后|到早).*?(?:到|上)\s*(" + _CN_NUM + r")\s*点"
            r".*?(?:这段)?(?:我)?(?:得?[休睡]|熄火|停[着车])",
            "rest_window",
        ),
        (
            r"每天.*?连续.*?休息[满满足足]?(" + _CN_NUM + r")\s*小?时",
            "daily_rest",
        ),
        (
            r"(?:装货|卸货).*?在\s*(" + _HAN + r"{2,4}?)\s*(?:区|县|市)?\s*的货"
            r".*?(?:起码|至少).*?接够\s*(" + _CN_NUM + r")\s*个?不同的?日子",
            "min_days_in_region_v2",
        ),
        (
            r"(?:起码|至少).*?接够\s*(" + _CN_NUM + r")\s*个?不同的?日子"
            r".*?(?:装货|卸货).*?在\s*(" + _HAN + r"{2,4}?)\s*(?:区|县|市)?",
            "min_days_in_region",
        ),
        (
            r"空驶.*?超过\s*(" + _CN_NUM + r")\s*公里.*?(?:不想接|不接|不跑|不[想愿])",
            "max_pickup_km",
        ),
        (
            r"(" + _HAN + r"{2,6}?)\s*(?:这类|这类活儿|货源).*?(?:干不了|推掉|不接|不做|一律)",
            "forbidden_category",
        ),
        (
            r"(?:起码|至少|抽|留)\s*(" + _CN_NUM + r")\s*个整天",
            "off_days",
        ),
        (
            r"装货地或卸货地在\s*(" + _HAN + r"{2,4}?)\s*(?:区|县|市)?\s*的货.*?(?:不接|推掉|一律)",
            "forbidden_region_cargo",
        ),
        (
            r"不[往进到]\s*(" + _HAN + r"{2,4}?)\s*(?:跑|去|那边)",
            "forbidden_region_entry",
        ),
    ]

    _FORBIDDEN_CATEGORY_WORDS: dict[str, list[str]] = {
        "机械设备": ["机械设备", "机械", "机床", "铸件", "龙门吊", "底座"],
        "蔬菜": ["蔬菜", "青菜", "叶菜", "生鲜蔬菜"],
    }

    def __init__(self, api=None) -> None:
        self._api = api
        self._logger = logging.getLogger("agent.preference_parser")
        self._llm_parse_cache: dict[str, PreferenceRule] = {}

    # 多阶段路线特征关键词：检测到则绕过正则，强制走 LLM 高精度解析
    _MULTI_STOP_KEYWORDS: tuple[str, ...] = (
        "先到", "再到", "赶到", "捎上", "宴", "寿宴", "赴宴",
        "先去", "再去", "顺路", "途经", "路过",
    )

    def parse(self, preferences: list[dict[str, Any]]) -> list[PreferenceRule]:
        rules: list[PreferenceRule] = []
        for pref in preferences:
            if not isinstance(pref, dict):
                continue
            content = str(pref.get("content", "")).strip()
            if not content:
                continue
            penalty_amount = float(pref.get("penalty_amount", 0) or 0)
            cap_raw = pref.get("penalty_cap")
            penalty_cap = float(cap_raw) if cap_raw is not None else None

            # 前置强分流：检测多阶段路线特征关键词 → 跳过正则匹配，直送 LLM
            is_multi_stop = any(kw in content for kw in self._MULTI_STOP_KEYWORDS)
            if is_multi_stop and self._api is not None:
                rule = self._llm_parse_preference(content, penalty_amount, penalty_cap)
            else:
                rule = self._parse_one(content, penalty_amount, penalty_cap)

            if rule is None or rule.rule_type == "unknown":
                if self._api is not None and not is_multi_stop:
                    rule = self._llm_parse_preference(content, penalty_amount, penalty_cap)
                elif rule is None:
                    rule = PreferenceRule(
                        rule_type="unknown",
                        penalty_amount=penalty_amount,
                        penalty_cap=penalty_cap,
                        params={"content": content},
                        original_content=content,
                    )

            if rule is not None:
                rules.append(rule)
                self._logger.info(
                    "Parsed preference: type=%s, penalty=%.0f, content=%s",
                    rule.rule_type, rule.penalty_amount, content[:50]
                )
        return rules

    def _parse_one(
        self, content: str, penalty_amount: float, penalty_cap: float | None
    ) -> PreferenceRule | None:
        for pattern, rule_type in self._PATTERNS:
            m = re.search(pattern, content)
            if not m:
                continue
            g = [gv for gv in m.groups() if gv is not None]

            if rule_type in ("day_specific_avoid", "day_specific_location"):
                days = self._extract_days(content)
                region = self._pick_region(g)
                if not region:
                    continue
                region = _strip_region_suffix(region)
                if rule_type == "day_specific_avoid":
                    return PreferenceRule(
                        rule_type="day_specific_avoid",
                        penalty_amount=penalty_amount,
                        penalty_cap=penalty_cap,
                        params={"days": days, "region": region},
                        original_content=content,
                    )
                coord = REGION_COORDINATES.get(region)
                if coord is None:
                    continue
                return PreferenceRule(
                    rule_type="day_specific_location",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"lat": coord[0], "lng": coord[1], "region_name": region, "text": content},
                    original_content=content,
                )

            if rule_type == "rest_window":
                return PreferenceRule(
                    rule_type="rest_window",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"start_hour": _parse_int(g[0]), "end_hour": _parse_int(g[1])},
                    original_content=content,
                )
            if rule_type == "daily_rest":
                return PreferenceRule(
                    rule_type="daily_rest",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"min_hours": _parse_int(g[0])},
                    original_content=content,
                )
            if rule_type in ("min_days_in_region", "min_days_in_region_v2"):
                if rule_type == "min_days_in_region":
                    region = _strip_region_suffix(str(g[1] if len(g) > 1 else ""))
                    days_val = _parse_int(g[0])
                else:
                    region = _strip_region_suffix(str(g[0]))
                    days_val = _parse_int(g[1] if len(g) > 1 else "0")
                return PreferenceRule(
                    rule_type="min_days_in_region",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"region": region, "min_days": days_val},
                    original_content=content,
                )
            if rule_type == "max_pickup_km":
                return PreferenceRule(
                    rule_type="max_pickup_km",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"max_km": float(_parse_int(g[0]))},
                    original_content=content,
                )
            if rule_type == "forbidden_category":
                word = self._match_known_category(content, g[0] if g else "")
                cats = self._FORBIDDEN_CATEGORY_WORDS.get(word, [word])
                return PreferenceRule(
                    rule_type="forbidden_category",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"categories": cats, "keyword": word},
                    original_content=content,
                )
            if rule_type == "off_days":
                return PreferenceRule(
                    rule_type="off_days",
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"min_days": _parse_int(g[0])},
                    original_content=content,
                )
            if rule_type in ("forbidden_region_cargo", "forbidden_region_entry"):
                region = _strip_region_suffix(str(g[0]))
                return PreferenceRule(
                    rule_type=rule_type,
                    penalty_amount=penalty_amount,
                    penalty_cap=penalty_cap,
                    params={"region": region},
                    original_content=content,
                )
        return PreferenceRule(
            rule_type="unknown",
            penalty_amount=penalty_amount,
            penalty_cap=penalty_cap,
            params={"content": content},
            original_content=content,
        )

    @staticmethod
    def _pick_region(groups: list[str]) -> str:
        for gv in groups:
            s = str(gv).strip()
            if re.match(r'^[一-鿿]{2,6}$', s):
                return s
        return ""

    @staticmethod
    def _match_known_category(content: str, captured: str) -> str:
        for known in PreferenceParser._FORBIDDEN_CATEGORY_WORDS:
            if known in content:
                return known
        return captured

    @staticmethod
    def _extract_days(content: str) -> list[int]:
        days: list[int] = []
        date_pattern = re.compile(r'(?:[一二三]?\s*(' + _CN_NUM + r'))\s*[月号日]')
        for m in date_pattern.finditer(content):
            num_text = m.group(1)
            val = _parse_int(num_text)
            if val == 3:
                continue
            if 1 <= val <= 31:
                days.append(val - 1)
        return sorted(set(days))

    def _llm_parse_preference(
        self,
        content: str,
        penalty_amount: float,
        penalty_cap: float | None
    ) -> PreferenceRule | None:
        cache_key = f"{content}_{penalty_amount}"
        if cache_key in self._llm_parse_cache:
            return self._llm_parse_cache[cache_key]

        try:
            system_prompt = (
                "你是货运偏好解析专家。将司机的自然语言偏好转换为结构化规则。\n"
                "支持的规则类型：\n"
                "- rest_window: 指定时间窗必须休息，参数start_hour, end_hour\n"
                "- daily_rest: 每日连续休息≥X小时，参数min_hours\n"
                "- max_pickup_km: 最大空驶距离，参数max_km\n"
                "- forbidden_category: 禁接品类，参数categories列表\n"
                "- off_days: 最少休息天数，参数min_days\n"
                "- forbidden_region_cargo: 禁接某区域货源，参数region\n"
                "- forbidden_region_entry: 禁止进入某区域，参数region\n"
                "- min_days_in_region: 某区域最少活跃天数，参数region, min_days\n"
                "- day_specific_avoid: 特定日期禁入某区域，参数days列表, region\n"
                "- route_stops: 多阶段复合路线偏好（先去A再去B、时间前到达），\n"
                "  参数stops数组，每项含region_name,lat,lng,可选time_deadline(hour)\n"
                "\n只输出JSON格式：{\"rule_type\":\"...\", \"params\":{...}}\n"
                "如果无法识别，返回{\"rule_type\":\"unknown\", \"params\":{\"content\":\"原文\"}}\n"
                "\n示例：文本：三月三十一号舅公做寿，上午得先过增城区档口捎上寿礼，"
                "中午十二点前赶到四会县城赴宴到下午两点。\n"
                "输出：{\"rule_type\":\"route_stops\", \"params\":{\"text\":\"舅公寿宴路线\"},"
                "\"stops\":[{\"lat\":23.15,\"lng\":113.67,\"region_name\":\"增城\"},"
                "{\"lat\":23.32,\"lng\":112.83,\"region_name\":\"四会\","
                "\"time_deadline\":\"12:00\",\"duration_hours\":2}]}"
            )

            model_resp = self._api.model_chat_completion({
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"偏好文本：{content}"}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 256
            })

            choices = model_resp.get("choices")
            if isinstance(choices, list) and choices:
                resp_content = choices[0].get("message", {}).get("content", "")
                if resp_content:
                    result = json.loads(resp_content)
                    rule_type = result.get("rule_type", "unknown")
                    params = result.get("params", {})

                    stops = result.get("stops") or params.get("stops") or []
                    rule = PreferenceRule(
                        rule_type=rule_type,
                        penalty_amount=penalty_amount,
                        penalty_cap=penalty_cap,
                        params=params,
                        original_content=content,
                        stops=stops,
                    )
                    self._llm_parse_cache[cache_key] = rule
                    self._logger.info("LLM parsed preference: %s", rule_type)
                    return rule
        except Exception as e:
            self._logger.warning("LLM preference parsing failed: %s", e)

        return None


class PreferenceChecker:
    def __init__(self, rules: list[PreferenceRule]) -> None:
        self._rules = list(rules)

    @property
    def rules(self) -> list[PreferenceRule]:
        return self._rules

    def check_cargo_weighted(
        self,
        cargo: dict[str, Any],
        pickup_distance_km: float,
        sim_progress_minutes: int,
        driver_state: Any = None,
    ) -> tuple[float, list[str]]:
        """per-rule gamma加权检查。逐规则乘以driver_state.get_gamma()。"""
        total_penalty = 0.0
        violations: list[str] = []
        day_idx = sim_progress_minutes // 1440
        for rule in self._rules:
            penalty, desc = self._check_cargo_rule(
                cargo, pickup_distance_km, day_idx, sim_progress_minutes, rule
            )
            if penalty > 0:
                rule_gamma = driver_state.get_gamma(rule.rule_type, rule.params) if driver_state else 1.0
                total_penalty += penalty * max(0.4, rule_gamma)
                if desc:
                    violations.extend(desc)
        return min(total_penalty, 50000.0), violations

    def hard_forbidden(
        self, cargo: dict[str, Any], pickup_distance_km: float, sim_progress_minutes: int,
        driver_state: Any = None,
    ) -> bool:
        """判断货源是否硬违规（必须过滤）。
        平台红线一票否决；per-rule gamma加权后超运价80%也硬过滤。
        """
        day_idx = sim_progress_minutes // 1440
        price = float(cargo.get("price", 0) or 0)
        for rule in self._rules:
            p, _ = self._check_cargo_rule(cargo, pickup_distance_km, day_idx, sim_progress_minutes, rule)
            if p <= 0:
                continue
            if rule.is_hard:
                return True
            rule_gamma = driver_state.get_gamma(rule.rule_type, rule.params) if driver_state else 1.0
            if p * rule_gamma > price * 0.8:
                return True
        return False

    def get_rules_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "type": r.rule_type,
                "params": r.params,
                "penalty": r.penalty_amount,
                "cap": r.penalty_cap,
                "is_hard": r.is_hard,
                "content": r.original_content,
            }
            for r in self._rules
        ]

    def check_reposition(
        self,
        current_lat: float,
        current_lng: float,
        target_lat: float,
        target_lng: float,
        sim_min: int,
    ) -> tuple[float, list[str]]:
        """检查空驶迁移动作本身是否违反偏好规则。
        返回 (罚分, 违规描述列表)。"""
        total_penalty = 0.0
        violations: list[str] = []
        day_idx = sim_min // 1440
        dist = haversine_km(current_lat, current_lng, target_lat, target_lng)
        dist_minutes = max(1, int((dist / 60.0) * 60.0))

        for rule in self._rules:
            if rule.rule_type == "forbidden_region_entry":
                region = str(rule.params.get("region", ""))
                for rname, coord in REGION_COORDINATES.items():
                    if region in rname:
                        if haversine_km(target_lat, target_lng, coord[0], coord[1]) < 30:
                            violations.append(f"reposition进入禁入区域{region}")
                            total_penalty += rule.max_penalty()

            elif rule.rule_type == "day_specific_avoid":
                specified_days = rule.params.get("days", [])
                if day_idx in specified_days:
                    region = str(rule.params.get("region", ""))
                    for rname, coord in REGION_COORDINATES.items():
                        if region in rname:
                            if haversine_km(target_lat, target_lng, coord[0], coord[1]) < 30:
                                violations.append(f"day{day_idx+1}禁入{region}")
                                total_penalty += rule.max_penalty()

            elif rule.rule_type == "max_pickup_km":
                max_km = float(rule.params.get("max_km", 9999))
                if dist > max_km:
                    violations.append(f"空驶迁移{dist:.1f}km超偏好上限{max_km:.0f}km")
                    total_penalty += rule.max_penalty()

        return total_penalty, violations

    def check_position_day_avoid(
        self, lat: float, lng: float, sim_min: int
    ) -> tuple[bool, float, list[str]]:
        """检查司机当前位置是否在day_specific_avoid禁区内。
        坐标匹配，解决城市名（宝安）vs 区域名（深圳）不匹配问题。"""
        day_idx = sim_min // 1440
        for rule in self._rules:
            if rule.rule_type != "day_specific_avoid":
                continue
            specified_days = rule.params.get("days", [])
            if day_idx not in specified_days:
                continue
            region = str(rule.params.get("region", ""))
            coord = REGION_COORDINATES.get(region)
            if coord is None:
                continue
            if haversine_km(lat, lng, coord[0], coord[1]) < 40:
                return True, rule.max_penalty(), [f"day{day_idx+1}位置在{region}禁区内"]
        return False, 0.0, []

    def check_cargo_dest_avoid(
        self, cargo: dict[str, Any], sim_min: int
    ) -> tuple[float, list[str]]:
        """检查货源目的地坐标是否在day_specific_avoid禁区内。"""
        day_idx = sim_min // 1440
        end = cargo.get("end") or {}
        dest_lat = float(end.get("lat", 0) or 0)
        dest_lng = float(end.get("lng", 0) or 0)
        if not (dest_lat and dest_lng):
            return 0.0, []
        for rule in self._rules:
            if rule.rule_type != "day_specific_avoid":
                continue
            specified_days = rule.params.get("days", [])
            if day_idx not in specified_days:
                continue
            region = str(rule.params.get("region", ""))
            coord = REGION_COORDINATES.get(region)
            if coord is None:
                continue
            if haversine_km(dest_lat, dest_lng, coord[0], coord[1]) < 40:
                return rule.max_penalty(), [f"day{day_idx+1}目的地{region}禁入"]
        return 0.0, []

    def _check_cargo_rule(
        self,
        cargo: dict[str, Any],
        pickup_distance_km: float,
        day_idx: int,
        sim_progress_minutes: int,
        rule: PreferenceRule,
    ) -> tuple[float, list[str]]:
        violations: list[str] = []

        if rule.rule_type == "day_specific_avoid":
            specified_days = rule.params.get("days", [])
            if specified_days and day_idx not in specified_days:
                return 0.0, []
            region = str(rule.params.get("region", ""))
            start_city = str((cargo.get("start") or {}).get("city", "") or "")
            end_city = str((cargo.get("end") or {}).get("city", "") or "")
            if region and (region in start_city or region in end_city):
                violations.append(f"day{day_idx+1}禁入{region}")

        elif rule.rule_type == "forbidden_category":
            cargo_name = str(cargo.get("cargo_name", "") or "")
            for cat in rule.params.get("categories", []):
                if cat in cargo_name:
                    violations.append(f"禁接品类{cat}")

        elif rule.rule_type in ("forbidden_region_cargo", "forbidden_region_entry"):
            region = str(rule.params.get("region", ""))
            start_city = str((cargo.get("start") or {}).get("city", "") or "")
            end_city = str((cargo.get("end") or {}).get("city", "") or "")
            if region and (region in start_city or region in end_city):
                violations.append(f"涉及禁入区域{region}")

        elif rule.rule_type == "max_pickup_km":
            max_km = float(rule.params.get("max_km", 9999))
            if pickup_distance_km > max_km:
                violations.append(f"赴装空驶{pickup_distance_km:.1f}km超上限{max_km:.0f}km")

        if violations:
            return rule.max_penalty(), violations
        return 0.0, []

    def check_transit_violation(
        self,
        pickup_km: float,
        sim_min: int,
        cargo_cost_time: int = 0,
        speed_km_per_hour: float = 60.0,
    ) -> tuple[float, list[str], bool]:
        """检查空驶+运输过程中是否会穿越 rest_window 禁行时段。
        返回 (累计罚分, 违规描述, 是否硬违规需要过滤)。
        """
        import math
        violations: list[str] = []
        pickup_minutes = max(1, math.ceil((pickup_km / speed_km_per_hour) * 60.0)) if pickup_km > 1e-6 else 0
        transit_end = sim_min + pickup_minutes + cargo_cost_time
        transit_hours: set[int] = set()
        for m in range(sim_min, transit_end + 1, 30):
            transit_hours.add((m % 1440) // 60)

        total_penalty = 0.0
        is_hard_violation = False
        for rule in self._rules:
            if rule.rule_type != "rest_window":
                continue
            start_h = int(rule.params.get("start_hour", 0))
            end_h = int(rule.params.get("end_hour", 6))
            for h in transit_hours:
                if hour_in_range(float(h), start_h, end_h - 0.5):
                    violations.append(
                        f"空驶/运输穿越禁行时段{start_h}:00-{end_h}:00 (触及时段{h}:00)"
                    )
                    total_penalty += rule.max_penalty()
                    is_hard_violation = True
                    break
        return min(total_penalty, 50000.0), violations, is_hard_violation

    def get_pending_requirements(
        self,
        daily_stats: dict[str, Any],
        sim_min: int,
        simulation_duration_days: int = 30,
    ) -> list[dict[str, Any]]:
        """返回未满足的累计式偏好需求列表，供 dispatcher 决策推动。"""
        from agent.utils.time_utils import sim_min_to_day
        current_day = sim_min_to_day(sim_min)
        remaining_days = max(1, simulation_duration_days - current_day)
        pending: list[dict[str, Any]] = []

        day_urgency = 0.0 if current_day < 10 else min(1.0, ((current_day - 10) / 17.0) ** 1.5)

        for rule in self._rules:
            if rule.rule_type == "off_days":
                achieved = daily_stats.get("off_days", 0)
                required = int(rule.params.get("min_days", 0))
                if achieved < required:
                    gap = required - achieved
                    gap_urgency = min(1.0, gap / max(1, remaining_days))
                    urgency = max(day_urgency, gap_urgency)
                    pending.append({
                        "rule_type": "off_days",
                        "achieved": achieved,
                        "required": required,
                        "urgency": urgency,
                        "penalty": rule.max_penalty(),
                        "action": "prefer_wait",
                    })

            elif rule.rule_type == "min_days_in_region":
                region = str(rule.params.get("region", ""))
                region_days = daily_stats.get("region_days", {})
                achieved = len(region_days.get(region, set()))
                required = int(rule.params.get("min_days", 0))
                if achieved < required:
                    gap = required - achieved
                    feasible = gap <= remaining_days
                    gap_urgency = min(1.0, gap / max(1, remaining_days))
                    urgency = max(day_urgency, gap_urgency)
                    pending.append({
                        "rule_type": "min_days_in_region",
                        "region": region,
                        "achieved": achieved,
                        "required": required,
                        "gap": gap,
                        "urgency": urgency,
                        "feasible": feasible,
                        "penalty": rule.max_penalty(),
                        "action": "boost_region",
                    })

            elif rule.rule_type == "day_specific_location":
                days = set()
                for m in re.finditer(r'([一二三]?\s*\d+|[零一二两三四五六七八九十廿卅百]+)', rule.original_content):
                    val = _parse_int(m.group(1))
                    if 1 <= val <= 31 and val != 3:
                        days.add(val - 1)
                if current_day in days:
                    pending.append({
                        "type": "spatio_temporal_constraint",
                        "rule_type": "day_specific_location",
                        "day": current_day,
                        "lat": rule.params.get("lat"),
                        "lng": rule.params.get("lng"),
                        "region_name": rule.params.get("region_name", ""),
                        "penalty": rule.max_penalty(),
                        "action": "go_to_location",
                        "deadline_min": (current_day + 1) * 1440,
                    })

            elif rule.rule_type == "route_stops":
                stops = rule.stops or rule.params.get("stops", [])
                if not stops:
                    continue
                # 可行性止损：提取目标日期，若仿真活不到截止时间则跳过
                target_day = None
                for m in re.finditer(r'([一二三]?\s*\d+|[零一二两三四五六七八九十廿卅百]+)', rule.original_content):
                    val = _parse_int(m.group(1))
                    if 20 <= val <= 31:
                        target_day = val - 1
                        break
                if target_day is not None:
                    last_stop = stops[-1]
                    deadline_str = str(last_stop.get("time_deadline", "23:59"))
                    try:
                        dh, dm = (int(x) for x in deadline_str.split(":")[:2])
                    except (ValueError, TypeError):
                        dh, dm = 23, 59
                    deadline_min = target_day * 1440 + dh * 60 + dm
                    sim_end = simulation_duration_days * 1440
                    if deadline_min > sim_end:
                        continue  # 仿真活不到截止时间，放弃此规则
                region_days = daily_stats.get("region_days", {})
                # 状态机：按序推进，找到第一个未打卡的站点
                for stop in stops:
                    region = str(stop.get("region_name", ""))
                    visited = len(region_days.get(region, set())) > 0 if region else False
                    if not visited:
                        target_lat = float(stop.get("lat", 0))
                        target_lng = float(stop.get("lng", 0))
                        if not (target_lat and target_lng):
                            # 尝试从REGION_COORDINATES回退
                            coord = REGION_COORDINATES.get(region)
                            if coord:
                                target_lat, target_lng = coord
                        deadline = stop.get("time_deadline")
                        pending.append({
                            "type": "spatio_temporal_constraint",
                            "rule_type": "day_specific_location",
                            "source_rule": "route_stops",
                            "day": current_day,
                            "lat": target_lat,
                            "lng": target_lng,
                            "region_name": region,
                            "penalty": rule.max_penalty(),
                            "action": "go_to_location",
                            "deadline_min": deadline_min,
                        })
                        break  # 只推进到第一个未完成的站点

        return pending
