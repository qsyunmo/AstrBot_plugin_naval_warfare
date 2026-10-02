"""蓝图模块池加载与派生计算（数据在 data/modules.json，改数不动代码）

模块 id 规则：{舰种id}_t{T}_{archetype.key}
成本 = 舰种槽位基础成本 × 稀有度系数 × T 成本系数
属性 = 原型 stats × T 武器系数（船体 hp 走独立系数）
"""
import json
import math
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "modules.json"
RARITY_ORDER = ["white", "blue", "purple", "gold"]
COST_KEYS = ("steel", "oil", "money", "manpower")


@lru_cache(maxsize=1)
def mod_data() -> dict:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


# 中文舰种名 → id（含常见别名）
CLASS_ALIAS = {
    "护卫舰": "frigate", "护卫": "frigate", "ff": "frigate",
    "驱逐舰": "destroyer", "驱逐": "destroyer", "dd": "destroyer",
    "轻巡": "light_cruiser", "轻巡洋舰": "light_cruiser", "cl": "light_cruiser",
    "攻击潜艇": "ss_attack", "攻潜": "ss_attack", "ss": "ss_attack",
    "护航潜艇": "ss_escort",
    "运输船": "transport", "运输": "transport", "商船": "transport",
}


def class_id(text: str):
    """把玩家输入解析成舰种 id；无法识别返回 None。"""
    t = text.strip().lower()
    if t in mod_data()["classes"]:
        return t
    return CLASS_ALIAS.get(text.strip())


def parse_tier(text: str):
    """'T3' / '3' → 3；非法返回 None。"""
    t = text.strip().lower().lstrip("t")
    return int(t) if t.isdigit() and 1 <= int(t) <= 5 else None


def module_id(cls: str, tier: int, key: str) -> str:
    return f"{cls}_t{tier}_{key}"


def pool_id(cls: str, tier: int) -> str:
    return f"{cls}_t{tier}"


def archetype(cls: str, key: str):
    for a in mod_data()["archetypes"][cls]:
        if a["key"] == key:
            return a
    return None


def module_info(mid: str):
    """模块 id → 完整信息 dict（含 cost/stats/tier）；无法解析返回 None。"""
    parts = mid.split("_t", 1)
    if len(parts) != 2 or "_" not in parts[1]:
        return None
    cls, rest = parts
    tier_s, key = rest.split("_", 1)
    if cls not in mod_data()["classes"] or not tier_s.isdigit():
        return None
    tier = int(tier_s)
    a = archetype(cls, key)
    if not a:
        return None
    d = mod_data()
    info = {"id": mid, "cls": cls, "tier": tier, "key": key,
            "slot": a["slot"], "rarity": a["rarity"],
            "name": a["name"], "stats": dict(a["stats"])}
    info["cost"] = module_cost(info)
    return info


def pool_modules(cls: str, tier: int):
    """某【舰种×T】池的全部模块。"""
    d = mod_data()
    out = []
    for a in d["archetypes"][cls]:
        mid = module_id(cls, tier, a["key"])
        out.append(module_info(mid))
    return out


def module_cost(mod: dict) -> dict:
    d = mod_data()
    clsdef = d["classes"][mod["cls"]]
    base = clsdef["slot_cost"][mod["slot"]]
    rmult = d["rarity"][mod["rarity"]]["cost_mult"]
    tmult = d["tier_cost_mult"][mod["tier"] - 1]
    cost = {}
    for k, v in base.items():
        cost[k] = max(1, round(v * rmult * tmult))
    return cost


def rarity_label(r: str) -> str:
    return mod_data()["rarity"][r]["name"]


def owned_ids(conn, qq: str) -> set:
    return {r["module_id"] for r in conn.execute(
        "SELECT module_id FROM blueprints WHERE qq=?", (qq,)).fetchall()}


def owned_modules(conn, qq: str, cls: str = None, slot: str = None):
    """已拥有模块信息列表，可按舰种/槽位过滤，按 T、稀有度排序。"""
    out = []
    for mid in owned_ids(conn, qq):
        info = module_info(mid)
        if not info:
            continue
        if cls and info["cls"] != cls:
            continue
        if slot and info["slot"] != slot:
            continue
        out.append(info)
    out.sort(key=lambda m: (m["tier"], RARITY_ORDER.index(m["rarity"]), m["id"]))
    return out


# 武器/功能类属性（随 T 缩放）；船体 hp/spd 与引擎 speed 不乘武器系数
SCALED_STATS = ("fire", "aa", "asw", "torpedo", "detect", "stealth", "cargo", "hit")


def ship_preview(cls: str, chosen: dict):
    """根据 {槽位: 模块id} 计算整舰属性、单舰造价与工时。
    返回 {tier, stats, cost, work_ticks, missing_required}。"""
    d = mod_data()
    clsdef = d["classes"][cls]
    mods = {slot: module_info(mid) for slot, mid in chosen.items() if mid}
    hull = mods.get("hull")
    engine = mods.get("engine")
    tier = hull["tier"] if hull else 1

    stats = {"fire": 0, "aa": 0, "asw": 0, "torpedo": 0, "detect": 0,
             "stealth": 0, "cargo": 0, "hit": 0, "speed": 0, "hp": 0, "range": 0,
             "troop": 0, "fuel_save": 0}
    cost = {k: 0 for k in COST_KEYS}
    work_points = 0
    for slot, m in mods.items():
        for k, v in m["cost"].items():
            if k == "work":
                work_points += v
            elif k in cost:
                cost[k] += v
        for sk, sv in m["stats"].items():
            if sk in SCALED_STATS:
                stats[sk] = stats.get(sk, 0) + sv * d["tier_stat_mult"][m["tier"] - 1]
            else:
                stats[sk] = stats.get(sk, 0) + sv
    if hull:
        stats["hp"] = round(clsdef["base_hp"] * d["tier_hp_mult"][tier - 1]
                            * hull["stats"].get("hp", 1.0))
    if engine:
        stats["speed"] = round(engine["stats"].get("speed", 0) + (hull["stats"].get("spd", 0) if hull else 0))
    for k in ("fire", "aa", "asw", "torpedo", "detect", "stealth", "cargo", "hit"):
        stats[k] = round(stats[k])

    missing = [s for s in clsdef["required"] if s not in mods]
    work_ticks = max(1, math.ceil(work_points / d["work_divisor"]))
    return {"tier": tier, "stats": stats, "cost": cost,
            "work_ticks": work_ticks, "missing_required": missing}


def stats_text(stats: dict) -> str:
    labels = [("hp", "耐久"), ("fire", "火力"), ("torpedo", "雷击"),
              ("aa", "对空"), ("asw", "反潜"), ("speed", "航速"),
              ("detect", "探测"), ("stealth", "隐蔽"), ("cargo", "舱容"), ("hit", "火控")]
    parts = []
    for k, zh in labels:
        if stats.get(k):
            unit = "节" if k == "speed" else ""
            parts.append(f"{zh}{stats[k]}{unit}")
    return "　".join(parts)
