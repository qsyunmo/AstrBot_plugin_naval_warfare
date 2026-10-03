"""为 naval_warfare 生成 22 个新舰种的 classes / archetypes 数据。

放置位置在插件目录之外，不随部署。

原理（勘察 modules.json 得出）：
- archetypes 里每个条目**不带 tier**；pools.pool_modules(cls, tier) 会把该舰种的
  **全部** archetypes 套上 `_t{tier}` 后缀返回，即「写 1 个 archetype 自动获得 5 个模块」。
- 因此新增一个舰种只需写 1 份 archetypes（约 12~20 条），5 个代际自动都有。
- 数值缩放由 tier_stat_mult / tier_hp_mult 统一处理，archetype 只写「同代内的横向取舍」。

为了不写 22×18 = 400 条各自独立的数据，按**吨位档**（light/medium/heavy）共享槽位模块池，
再让每个舰种挑选自己需要的槽位。主力舰额外补充专属主炮/装甲条目。
"""
import json
import pathlib

# 目标 modules.json：优先用「与本脚本同级的 naval_warfare/」（开发目录布局），
# 否则用「与本脚本同级的 naval/」（脚本放在插件仓库根目录时的布局）。
_HERE = pathlib.Path(__file__).resolve().parent
_CANDS = [_HERE / "naval_warfare" / "naval" / "data" / "modules.json",
          _HERE / "naval" / "data" / "modules.json"]
SRC = next((p for p in _CANDS if p.exists()), _CANDS[0])

# ---------------------------------------------------------------- 槽位模块池
# 每项: (key, rarity, 名称, stats)
# stats 语义: hp=耐久倍率, spd=航速增减, 其余为可叠加数值
POOL = {
    # ---------------- light（护卫舰级，500~2000t）----------------
    "light": {
        "hull": [
            ("hull_std", "white", "标准船体", {"hp": 1.0}),
            ("hull_light", "white", "轻量船体", {"hp": 0.85, "spd": 2}),
            ("hull_tough", "blue", "强化船体", {"hp": 1.2, "spd": -1}),
            ("hull_sea", "blue", "适航船体", {"hp": 1.05, "range": 1}),
            ("hull_steel", "purple", "装甲船体", {"hp": 1.45, "spd": -2}),
            ("hull_legend", "gold", "传奇艇体", {"hp": 1.7, "spd": 1}),
        ],
        "engine": [
            ("eng_std", "white", "标准柴油机", {"speed": 4}),
            ("eng_econ", "white", "经济轮机", {"speed": 3, "fuel_save": 1}),
            ("eng_fast", "blue", "高速轮机", {"speed": 6, "fuel_save": -1}),
            ("eng_turbine", "purple", "燃气轮机", {"speed": 8}),
            ("eng_jet", "gold", "喷水推进", {"speed": 11, "stealth": 3}),
        ],
        "gun": [
            ("gun_76", "white", "76mm舰炮", {"fire": 4}),
            ("gun_100", "blue", "100mm舰炮", {"fire": 6, "hit": 1}),
            ("gun_twin", "blue", "双联100mm", {"fire": 8, "hit": -1}),
            ("gun_auto", "purple", "自动炮塔", {"fire": 10, "aa": 3}),
            ("gun_rail", "gold", "电磁炮", {"fire": 16, "hit": 3}),
        ],
        "torpedo": [
            ("torp_533", "white", "533mm鱼雷", {"torpedo": 8}),
            ("torp_quad", "blue", "四联533", {"torpedo": 14, "hit": -1}),
            ("torp_oxygen", "purple", "氧气鱼雷", {"torpedo": 20}),
            ("torp_homing", "gold", "声自导鱼雷", {"torpedo": 26, "hit": 3}),
        ],
        "asw": [
            ("asw_rail", "white", "深弹滑轨", {"asw": 6}),
            ("asw_hedge", "blue", "刺猬弹", {"asw": 10}),
            ("asw_torp", "blue", "反潜鱼雷", {"asw": 12, "torpedo": 4}),
            ("asw_rocket", "purple", "反潜火箭", {"asw": 16}),
            ("asw_heli", "gold", "反潜直升机", {"asw": 24, "detect": 6}),
        ],
        "aa": [
            ("aa_mg", "white", "机枪群", {"aa": 4}),
            ("aa_bofors", "blue", "博福斯炮群", {"aa": 8}),
            ("aa_ciws", "purple", "近防炮", {"aa": 14}),
            ("aa_sam", "gold", "舰空导弹", {"aa": 22}),
        ],
        "electronic": [
            ("el_lookout", "white", "瞭望哨", {"detect": 4}),
            ("el_radar", "blue", "对海雷达", {"detect": 8}),
            ("el_link", "purple", "数据链", {"detect": 12, "hit": 2}),
            ("el_aegis", "gold", "小型相控阵", {"detect": 18, "hit": 4, "aa": 4}),
        ],
        "sonar": [
            ("son_basic", "white", "基础声呐", {"detect": 4, "asw": 3}),
            ("son_hull", "blue", "舰壳声呐", {"detect": 8, "asw": 6}),
            ("son_towed", "purple", "拖曳阵声呐", {"detect": 14, "asw": 10}),
            ("son_sphere", "gold", "球鼻艏声呐", {"detect": 20, "asw": 15}),
        ],
        "stealth": [
            ("st_quiet", "white", "降噪处理", {"stealth": 5}),
            ("st_anechoic", "blue", "消声瓦", {"stealth": 10}),
            ("st_pumpjet", "purple", "泵喷推进", {"stealth": 16, "speed": 2}),
            ("st_aip", "gold", "AIP 不依赖空气", {"stealth": 24}),
        ],
        "cargo": [
            ("cg_std", "white", "标准货舱", {"cargo": 10}),
            ("cg_large", "blue", "大容量货舱", {"cargo": 20}),
            ("cg_armory", "blue", "弹药舱", {"cargo": 12, "troop": 2}),
            ("cg_med", "purple", "医疗舱", {"cargo": 14, "hp": 1.1}),
            ("cg_repair", "gold", "维修舱", {"cargo": 18, "hp": 1.2}),
        ],
        "deck": [
            ("dk_flush", "white", "平甲板", {"deck_hp": 60}),
            ("dk_angled", "blue", "斜角甲板", {"deck_hp": 90, "hangar": 1}),
            ("dk_armor", "purple", "装甲甲板", {"deck_hp": 130}),
        ],
        "hangar": [
            ("hg_std", "white", "标准机库", {"hangar": 6}),
            ("hg_large", "blue", "大型机库", {"hangar": 10, "deck_hp": -10}),
            ("hg_armored", "purple", "装甲机库", {"hangar": 14}),
        ],
    },
    # ---------------- medium（驱逐~轻巡级，2000~12000t）----------------
    "medium": {
        "hull": [
            ("hull_std", "white", "标准船体", {"hp": 1.0}),
            ("hull_ocean", "blue", "远洋船体", {"hp": 0.9, "spd": 2, "range": 1}),
            ("hull_heavy", "blue", "重装船体", {"hp": 1.25, "spd": -2}),
            ("hull_recon", "purple", "侦察船体", {"hp": 0.95, "detect": 6, "spd": 3}),
            ("hull_armor", "gold", "装甲船体", {"hp": 1.6, "spd": -3}),
        ],
        "engine": [
            ("eng_std", "white", "蒸汽轮机", {"speed": 5}),
            ("eng_econ", "white", "巡航省油轮机", {"speed": 4, "fuel_save": 2}),
            ("eng_over", "blue", "增压轮机", {"speed": 7, "fuel_save": -1}),
            ("eng_ge", "purple", "燃气轮机", {"speed": 10}),
        ],
        "gun": [
            ("gun_127", "white", "单装127mm炮", {"fire": 6}),
            ("gun_dp127", "blue", "双联高平127", {"fire": 10, "aa": 3}),
            ("gun_auto127", "blue", "自动127mm", {"fire": 12, "aa": 4}),
            ("gun_152", "purple", "三联152mm", {"fire": 18, "hit": -1}),
            ("gun_203", "gold", "双联203mm", {"fire": 24, "hit": -2}),
        ],
        "torpedo": [
            ("torp_3x533", "white", "三联533鱼雷", {"torpedo": 10}),
            ("torp_4x533", "blue", "四联533鱼雷", {"torpedo": 16, "hit": -1}),
            ("torp_oxygen", "purple", "氧气鱼雷", {"torpedo": 24}),
            ("torp_homing", "gold", "声自导鱼雷", {"torpedo": 30, "hit": 3}),
        ],
        "asw": [
            ("asw_rail", "white", "深弹滑轨", {"asw": 6}),
            ("asw_hedge", "blue", "刺猬弹", {"asw": 11}),
            ("asw_torp", "blue", "反潜鱼雷", {"asw": 14, "torpedo": 4}),
            ("asw_rocket", "purple", "反潜火箭深弹", {"asw": 18}),
        ],
        "aa": [
            ("aa_mg", "white", "机炮群", {"aa": 5}),
            ("aa_bofors", "blue", "博福斯炮群", {"aa": 10}),
            ("aa_ciws", "purple", "近防炮", {"aa": 16}),
            ("aa_sam", "gold", "舰空导弹", {"aa": 26}),
        ],
        "electronic": [
            ("el_lookout", "white", "瞭望哨", {"detect": 5}),
            ("el_radar", "blue", "对海雷达", {"detect": 9}),
            ("el_fc", "blue", "火控雷达", {"detect": 7, "hit": 3}),
            ("el_link", "purple", "数据链", {"detect": 14, "hit": 2}),
            ("el_aegis", "gold", "相控阵", {"detect": 22, "hit": 5, "aa": 6}),
        ],
        "sonar": [
            ("son_basic", "white", "基础声呐", {"detect": 5, "asw": 4}),
            ("son_hull", "blue", "舰壳声呐", {"detect": 10, "asw": 8}),
            ("son_towed", "purple", "拖曳阵声呐", {"detect": 16, "asw": 12}),
            ("son_sphere", "gold", "球鼻艏声呐", {"detect": 24, "asw": 18}),
        ],
        "stealth": [
            ("st_quiet", "white", "降噪处理", {"stealth": 6}),
            ("st_anechoic", "blue", "消声瓦", {"stealth": 12}),
            ("st_pumpjet", "blue", "泵喷推进", {"stealth": 16, "speed": 2}),
            ("st_aip", "purple", "AIP 不依赖空气", {"stealth": 22}),
        ],
        "cargo": [
            ("cg_std", "white", "标准货舱", {"cargo": 16}),
            ("cg_large", "blue", "大容量货舱", {"cargo": 30}),
            ("cg_refrig", "blue", "冷藏舱", {"cargo": 24, "hp": 1.05}),
            ("cg_landing", "purple", "登陆舱", {"troop": 6, "cargo": 12}),
        ],
        "deck": [
            ("dk_flush", "white", "平甲板", {"deck_hp": 80}),
            ("dk_catapult", "blue", "弹射甲板", {"deck_hp": 110, "hangar": 2}),
            ("dk_armor", "purple", "装甲甲板", {"deck_hp": 160}),
        ],
        "hangar": [
            ("hg_std", "white", "标准机库", {"hangar": 8}),
            ("hg_large", "blue", "大型机库", {"hangar": 14, "deck_hp": -10}),
            ("hg_exp", "blue", "扩展机库", {"hangar": 18, "hp": -0.05}),
            ("hg_armored", "purple", "装甲机库", {"hangar": 20}),
        ],
    },
    # ---------------- heavy（战列~航母级，20000t+）----------------
    "heavy": {
        "hull": [
            ("hull_std", "white", "标准船体", {"hp": 1.0}),
            ("hull_armor", "blue", "装甲带船体", {"hp": 1.3, "spd": -2}),
            ("hull_all", "blue", "全域装甲", {"hp": 1.5, "spd": -4}),
            ("hull_comp", "purple", "分区防护", {"hp": 1.7, "spd": -3}),
            ("hull_heavy", "gold", "重装甲船体", {"hp": 2.1, "spd": -6}),
        ],
        "engine": [
            ("eng_std", "white", "大型蒸汽轮机", {"speed": 5}),
            ("eng_econ", "white", "巡航轮机", {"speed": 4, "fuel_save": 3}),
            ("eng_over", "blue", "增压轮机", {"speed": 7}),
            ("eng_ge", "purple", "燃气轮机", {"speed": 10}),
            ("eng_nuke", "gold", "核动力", {"speed": 12, "fuel_save": 5}),
        ],
        "gun": [
            ("gun_280", "white", "三联280mm", {"fire": 22}),
            ("gun_356", "blue", "三联356mm", {"fire": 32, "hit": -1}),
            ("gun_406", "blue", "三联406mm", {"fire": 42, "hit": -2}),
            ("gun_460", "purple", "三联460mm", {"fire": 56, "hit": -3}),
            ("gun_missile", "gold", "反舰导弹发射箱", {"fire": 64, "hit": 4}),
        ],
        "torpedo": [
            ("torp_610", "white", "610mm鱼雷", {"torpedo": 16}),
            ("torp_oxygen", "blue", "氧气鱼雷", {"torpedo": 26}),
            ("torp_homing", "purple", "声自导鱼雷", {"torpedo": 36, "hit": 3}),
            ("torp_wake", "gold", "尾流自导鱼雷", {"torpedo": 48, "hit": 5}),
        ],
        "aa": [
            ("aa_heavy", "white", "重型高炮", {"aa": 10}),
            ("aa_bofors", "blue", "博福斯炮群", {"aa": 18}),
            ("aa_ciws", "blue", "近防炮", {"aa": 24}),
            ("aa_sam", "purple", "舰空导弹", {"aa": 36}),
            ("aa_phased", "gold", "相控阵+垂发", {"aa": 52}),
        ],
        "electronic": [
            ("el_radar", "white", "对海雷达", {"detect": 10}),
            ("el_fc", "blue", "火控雷达", {"detect": 12, "hit": 4}),
            ("el_link", "blue", "数据链", {"detect": 18, "hit": 3}),
            ("el_aegis", "purple", "相控阵", {"detect": 28, "hit": 6, "aa": 8}),
            ("el_cic", "gold", "综合作战中心", {"detect": 40, "hit": 9, "aa": 10}),
        ],
        "sonar": [
            ("son_hull", "white", "舰壳声呐", {"detect": 8, "asw": 6}),
            ("son_towed", "blue", "拖曳阵声呐", {"detect": 16, "asw": 12}),
            ("son_sphere", "purple", "球鼻艏声呐", {"detect": 26, "asw": 20}),
            ("son_bow", "gold", "大型艏声呐", {"detect": 36, "asw": 28}),
        ],
        "stealth": [
            ("st_quiet", "white", "降噪处理", {"stealth": 8}),
            ("st_anechoic", "blue", "消声瓦", {"stealth": 15}),
            ("st_pumpjet", "purple", "泵喷推进", {"stealth": 22, "speed": 2}),
            ("st_aip", "gold", "AIP 动力", {"stealth": 30}),
        ],
        "deck": [
            ("dk_flush", "white", "平甲板", {"deck_hp": 120}),
            ("dk_armor", "blue", "装甲甲板", {"deck_hp": 180}),
            ("dk_angled", "blue", "斜角甲板", {"deck_hp": 150, "hangar": 3}),
            ("dk_steam", "purple", "蒸汽弹射", {"deck_hp": 200, "hangar": 4}),
        ],
        "hangar": [
            ("hg_std", "white", "标准机库", {"hangar": 14}),
            ("hg_large", "blue", "大型机库", {"hangar": 24, "deck_hp": -15}),
            ("hg_exp", "purple", "扩展机库", {"hangar": 34, "hp": -0.08}),
            ("hg_armored", "gold", "装甲机库", {"hangar": 40}),
        ],
        "cargo": [
            ("cg_std", "white", "大型货舱", {"cargo": 30}),
            ("cg_large", "blue", "超大型货舱", {"cargo": 55}),
            ("cg_landing", "purple", "登陆舱", {"troop": 12, "cargo": 24}),
            ("cg_repair", "gold", "维修舱", {"cargo": 40, "hp": 1.15}),
        ],
    },
}

# ---------------------------------------------------------------- 22 个新舰种
# 字段: id -> (中文名, base_hp, 吨位档, slots, required, 是否主力舰需法案, 造价倍率)
NEW = {
    # ---- MVP 补漏 ----
    "landing":        ("登陆艇",     60,  "light",
                       ["hull", "engine", "cargo"], ["hull", "engine", "cargo"], False, 0.7),
    # ---- 二期 ----
    "heavy_cruiser":  ("重巡洋舰",   420, "medium",
                       ["hull", "engine", "gun", "torpedo", "aa", "electronic"],
                       ["hull", "engine", "gun"], False, 2.2),
    "battlecruiser":  ("战列巡洋舰", 1200, "heavy",
                       ["hull", "engine", "gun", "aa", "electronic"],
                       ["hull", "engine", "gun"], True, 6.0),
    "battleship":     ("战列舰",     1800, "heavy",
                       ["hull", "engine", "gun", "torpedo", "aa", "electronic"],
                       ["hull", "engine", "gun"], True, 8.0),
    # ---- 三次科技 ----
    "escort_cv":      ("护航航母",   520, "medium",
                       ["hull", "engine", "deck", "hangar", "aa"],
                       ["hull", "engine", "deck", "hangar"], True, 3.0),
    "seaplane_tender": ("水上机母舰", 380, "medium",
                        ["hull", "engine", "hangar", "deck", "aa"],
                        ["hull", "engine", "deck"], False, 1.6),
    "minelayer":      ("布雷舰",     180, "light",
                       ["hull", "engine", "cargo", "aa"], ["hull", "engine"], False, 1.0),
    "minesweeper":    ("扫雷舰",     170, "light",
                       ["hull", "engine", "asw", "aa"], ["hull", "engine"], False, 1.0),
    "sub_chaser":     ("猎潜舰",     200, "light",
                       ["hull", "engine", "asw", "sonar", "aa"],
                       ["hull", "engine", "sonar"], False, 1.2),
    "torpedo_boat":   ("鱼雷艇",     70,  "light",
                       ["hull", "engine", "torpedo", "aa"], ["hull", "engine"], False, 0.6),
    "gunboat":        ("炮艇",       110, "light",
                       ["hull", "engine", "gun", "aa"], ["hull", "engine"], False, 0.7),
    "supply_ship":    ("补给舰",     240, "light",
                       ["hull", "engine", "cargo", "aa"], ["hull", "engine", "cargo"],
                       False, 1.1),
    "hospital_ship":  ("医疗舰",     260, "light",
                       ["hull", "engine", "cargo"], ["hull", "engine", "cargo"],
                       False, 1.1),
    # ---- 冷战 ----
    "missile_boat":   ("导弹艇",     90,  "light",
                       ["hull", "engine", "gun", "electronic"], ["hull", "engine"],
                       False, 0.9),
    "missile_destroyer": ("导弹驱逐舰", 260, "medium",
                          ["hull", "engine", "gun", "torpedo", "aa", "electronic"],
                          ["hull", "engine"], False, 2.6),
    "missile_cruiser": ("导弹巡洋舰", 620, "medium",
                        ["hull", "engine", "gun", "torpedo", "aa", "electronic"],
                        ["hull", "engine", "gun"], False, 4.2),
    "ssn":            ("攻击核潜艇", 480, "medium",
                       ["hull", "engine", "torpedo", "sonar", "stealth"],
                       ["hull", "engine"], False, 3.4),
    "ssbn":           ("战略核潜艇", 700, "medium",
                       ["hull", "engine", "torpedo", "sonar", "stealth", "electronic"],
                       ["hull", "engine"], False, 4.6),
    "helicopter_cv":  ("直升机航母", 900, "heavy",
                       ["hull", "engine", "deck", "hangar", "aa", "electronic"],
                       ["hull", "engine", "deck", "hangar"], True, 4.4),
    # ---- 奇观向 ----
    "aviation_battleship": ("航空战列舰", 1600, "heavy",
                            ["hull", "engine", "gun", "aa", "deck", "hangar",
                             "electronic"],
                            ["hull", "engine", "gun", "deck"], True, 9.0),
    "submarine_cv":   ("潜水航母",   1100, "heavy",
                       ["hull", "engine", "hangar", "torpedo", "sonar", "stealth"],
                       ["hull", "engine", "hangar"], True, 9.5),
    "ekranoplan":     ("地效翼飞船", 700, "heavy",
                       ["hull", "engine", "gun", "torpedo", "aa"],
                       ["hull", "engine"], False, 6.5),
}

# 保底金模块：若某舰种一条 gold 都没有，就给它补一条（挂在第一个「有明确属性
# 意义」的槽位上）。设计目标要求每个舰种都能抽出白/蓝/紫/金四档。
GOLD_FALLBACK = {
    "hull": ("hull_gold", "传奇船体", {"hp": 1.8, "spd": -2}),
    "gun": ("gun_gold", "传奇主炮", {"fire": 30, "hit": 2}),
    "torpedo": ("torp_gold", "传奇鱼雷", {"torpedo": 30, "hit": 2}),
    "aa": ("aa_gold", "传奇防空", {"aa": 28}),
    "hangar": ("hg_gold", "传奇机库", {"hangar": 24}),
    "deck": ("dk_gold", "传奇甲板", {"deck_hp": 180}),
    "cargo": ("cg_gold", "传奇货舱", {"cargo": 30}),
    "sonar": ("son_gold", "传奇声呐", {"detect": 24, "asw": 18}),
    "asw": ("asw_gold", "传奇反潜", {"asw": 22}),
    "electronic": ("el_gold", "传奇电子", {"detect": 30, "hit": 6}),
    "stealth": ("st_gold", "传奇静音", {"stealth": 28}),
    "engine": ("eng_gold", "传奇动力", {"speed": 12}),
}


def ensure_gold(d: dict) -> list:
    """确保每个舰种至少有一条 gold archetype。返回补过的舰种列表。"""
    fixed = []
    for cid, cdef in d["classes"].items():
        arch = d["archetypes"].setdefault(cid, [])
        if any(m["rarity"] == "gold" for m in arch):
            continue
        # 优先挂必选槽，其次第一个槽
        order = list(cdef.get("required") or []) + list(cdef["slots"])
        for sk in order:
            if sk not in GOLD_FALLBACK:
                continue
            if any(m["slot"] == sk for m in arch):
                key, name, stats = GOLD_FALLBACK[sk]
                if any(m["key"] == key for m in arch):
                    continue
                arch.append({"key": key, "slot": sk, "rarity": "gold",
                             "name": name, "stats": stats})
                fixed.append(cid)
                break
    return fixed


def slot_cost_for(weight: str, cls_mult: float) -> dict:
    """按吨位档给各槽位造价，再乘舰种倍率。"""
    base = {
        "light": {"hull": (22, 20, 6), "engine": (14, 16, 4),
                  "gun": (12, 16, 3), "torpedo": (8, 12, 3),
                  "asw": (7, 11, 2), "aa": (6, 10, 2), "electronic": (7, 20, 2),
                  "sonar": (9, 18, 3), "stealth": (10, 22, 3), "cargo": (8, 10, 4),
                  "deck": (20, 26, 6), "hangar": (18, 24, 5)},
        "medium": {"hull": (50, 60, 8), "engine": (25, 40, 4),
                   "gun": (20, 30, 3), "torpedo": (12, 20, 3),
                   "asw": (10, 18, 2), "aa": (10, 18, 2), "electronic": (8, 40, 2),
                   "sonar": (12, 32, 3), "stealth": (14, 38, 3), "cargo": (12, 20, 5),
                   "deck": (40, 60, 8), "hangar": (36, 54, 7)},
        "heavy": {"hull": (140, 160, 16), "engine": (70, 100, 9),
                  "gun": (60, 90, 8), "torpedo": (34, 50, 6),
                  "asw": (26, 40, 4), "aa": (26, 40, 4), "electronic": (20, 90, 5),
                  "sonar": (30, 70, 6), "stealth": (34, 80, 6), "cargo": (30, 50, 9),
                  "deck": (90, 130, 15), "hangar": (80, 110, 13)},
    }[weight]
    out = {}
    for sk, (steel, money, work) in base.items():
        out[sk] = {"steel": int(steel * cls_mult), "money": int(money * cls_mult),
                   "work": max(1, int(work * cls_mult))}
        if sk in ("engine",):
            out[sk]["oil"] = int(10 * cls_mult)
        if sk in ("hull", "cargo"):
            out[sk]["manpower"] = int(20 * cls_mult)
    return out


def build():
    d = json.loads(SRC.read_text(encoding="utf-8"))
    added_cls, added_arch, updated = [], 0, []
    for cid, (name, hp, weight, slots, required, capital, mult) in NEW.items():
        # 已存在则只更新「槽位/必选/造价」（幂等重跑），不动已生成的 archetypes
        if cid in d["classes"]:
            cur = d["classes"][cid]
            if (cur.get("slots") != list(slots)
                    or cur.get("required") != list(required)):
                cur["slots"] = list(slots)
                cur["required"] = list(required)
                cur["slot_cost"] = {s: slot_cost_for(weight, mult)[s] for s in slots}
                # 补齐新增槽位缺失的 archetypes
                have = {m["slot"] for m in d["archetypes"].get(cid, [])}
                arch = d["archetypes"].setdefault(cid, [])
                for sk in slots:
                    if sk in have:
                        continue
                    for (key, rarity, mname, stats) in POOL[weight].get(sk, []):
                        arch.append({"key": key, "slot": sk, "rarity": rarity,
                                     "name": mname, "stats": stats})
                        added_arch += 1
                # 移除已不属于该舰种的槽位模块
                keep = [m for m in arch if m["slot"] in slots]
                d["archetypes"][cid] = keep
                updated.append(cid)
            continue
        d["classes"][cid] = {
            "name": name, "base_hp": hp, "slots": list(slots),
            "required": list(required),
            "slot_cost": {s: slot_cost_for(weight, mult)[s] for s in slots},
            "_capital": capital,
            "_tier_source": "四期/冷战/奇观",
        }
        arch = []
        for sk in slots:
            for (key, rarity, mname, stats) in POOL[weight].get(sk, []):
                arch.append({"key": key, "slot": sk, "rarity": rarity,
                             "name": mname, "stats": stats})
        d["archetypes"][cid] = arch
        added_cls.append(cid)
        added_arch += len(arch)
    fixed_gold = ensure_gold(d)
    return d, added_cls, added_arch, updated, fixed_gold


if __name__ == "__main__":
    d, cls, n, upd, gold = build()
    print(f"新增舰种 {len(cls)} 个；更新槽位 {len(upd)} 个；"
          f"archetypes 变动 {n} 条；补金模块 {len(gold)} 个舰种")
    for c in cls:
        v = d["classes"][c]
        print(f"  NEW  {c:<20} {v['name']:<8} hp={v['base_hp']:<5}"
              f" 槽位 {len(v['slots'])}　模块 {len(d['archetypes'][c])}")
    for c in upd:
        print(f"  UPD  {c:<20} {d['classes'][c]['name']:<8}"
              f" 槽位 {d['classes'][c]['slots']}"
              f"　模块 {len(d['archetypes'][c])}")
    for c in gold:
        print(f"  GOLD {c:<20} {d['classes'][c]['name']:<8}"
              f"　补 {[m['name'] for m in d['archetypes'][c] if m['rarity']=='gold']}")
    print(f"\n总舰种数: {len(d['classes'])}")
    print(f"总 archetypes: {sum(len(v) for v in d['archetypes'].values())}")
    print(f"换算成模块 id（×5 代际）: "
          f"{sum(len(v) for v in d['archetypes'].values()) * 5}")
    if "--write" in __import__("sys").argv:
        SRC.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {SRC}")
    else:
        print("\n（未写入，加 --write 才落盘）")
