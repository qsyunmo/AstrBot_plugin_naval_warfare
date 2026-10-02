"""§26.4 雲墨残骸打捞 + §11 沉船遗迹。

设计文档依据：
  §26.4 「击杀后残骸区保留 14 日可打捞」；「离子核心」是 T10 单位唯一建造材料，
        「雲墨残骸唯一产出，每轮全服仅掉几份」
  §11   「沉船遗迹 | 一次性钢铁/资金/蓝图碎片 | 随机海格 | 一次性 |
         派船打捞，有概率触雷/伏击」

两类残骸共用一张 wrecks 表，只是过期时间、战利品与风险不同。
"""
import json
import math
import random

YUNMO, WRECK = "yunmo", "wreck"
TICKS_PER_DAY = 72          # 经济 tick
WAR_TICKS_PER_DAY = 144     # 战争 tick


def scfg(cfg: dict) -> dict:
    return cfg.get("salvage") or {}


def get_wreck(conn, x: int, y: int):
    return conn.execute("SELECT * FROM wrecks WHERE x=? AND y=? AND remaining>0",
                        (x, y)).fetchone()


def _share(loot: dict, charges: int) -> dict:
    """把总战利品均分成 charges 份（残骸是有限池，不是每次都给满额）。"""
    n = max(1, int(charges))
    return {k: max(1, int(round(v / n))) for k, v in (loot or {}).items() if v > 0}


def create_yunmo_wreck(conn, cfg, x: int, y: int, war_tick: int) -> int:
    """§26.4 雲墨被歼灭后，在原地留下 14 日可打捞的残骸区。

    战利品在创建时就按可打捞次数均分并落库，避免每次打捞都拿满额。
    """
    s = scfg(cfg)
    days = int(s.get("yunmo_expire_days", 14))
    charges = int(s.get("yunmo_charges", 5))
    loot = _share(dict(s.get("yunmo_loot") or {}), charges)
    cur = conn.execute(
        "INSERT INTO wrecks(x,y,kind,loot_json,remaining,expire_tick,created_tick)"
        " VALUES(?,?,?,?,?,?,?)",
        (x, y, YUNMO, json.dumps(loot, ensure_ascii=False),
         charges, war_tick + days * WAR_TICKS_PER_DAY, war_tick))
    conn.commit()
    return cur.lastrowid


def ensure_wrecks(conn, cfg, cx: int, cy: int, war_tick: int) -> int:
    """§11 随机海格上的沉船遗迹（按数量补齐）。"""
    s = scfg(cfg)
    want = int(s.get("wreck_count", 2))
    have = conn.execute("SELECT COUNT(*) c FROM wrecks WHERE kind=? AND remaining>0",
                        (WRECK,)).fetchone()["c"]
    made = 0
    rng = random.Random(f"{cx}-{cy}-{war_tick}-wreck")
    for _ in range(max(0, want - have)):
        d = rng.randint(20, 60)
        a = rng.uniform(0, 6.2832)
        x = max(0, min(999, cx + round(d * math.cos(a))))
        y = max(0, min(999, cy + round(d * math.sin(a))))
        if conn.execute("SELECT 1 FROM wrecks WHERE x=? AND y=?", (x, y)).fetchone():
            continue
        conn.execute(
            "INSERT INTO wrecks(x,y,kind,loot_json,remaining,expire_tick,created_tick)"
            " VALUES(?,?,?,?,?,?,?)",
            (x, y, WRECK,
             json.dumps(_share(dict(s.get("wreck_loot") or {}),
                               int(s.get("wreck_charges", 1))), ensure_ascii=False),
             int(s.get("wreck_charges", 1)),
             war_tick + int(s.get("wreck_expire_days", 5)) * WAR_TICKS_PER_DAY, war_tick))
        made += 1
    if made:
        conn.commit()
    return made


def war_tick_wrecks(conn, cfg, war_tick: int) -> list:
    """过期残骸清理。返回被清掉的条数（用于日志）。"""
    rows = conn.execute("SELECT * FROM wrecks WHERE remaining>0 AND expire_tick<=?",
                        (war_tick,)).fetchall()
    for r in rows:
        conn.execute("UPDATE wrecks SET remaining=0 WHERE id=?", (r["id"],))
    if rows:
        conn.commit()
    return [dict(r) for r in rows]


def salvage(conn, cfg, qq: str, fleet_id: int, x: int, y: int):
    """在 (x,y) 打捞。返回 (ok, text)。

    §11 沉船遗迹有概率触雷/伏击；雲墨残骸可分批捞完并可能产出离子核心。
    """
    from . import fleet, mines
    w = get_wreck(conn, x, y)
    if not w:
        return False, f"❌ ({x},{y}) 没有可打捞的残骸"
    f = conn.execute("SELECT * FROM fleets WHERE id=?", (fleet_id,)).fetchone()
    if not f:
        return False, "❌ 舰队已不存在"
    if (f["x"], f["y"]) != (x, y):
        return False, (f"❌ 舰队在({f['x']},{f['y']})，打捞需与残骸同格。\n"
                       f"先 /nw移动 {f['name']} {x},{y}")
    if fleet.ship_count(conn, fleet_id) <= 0:
        return False, "❌ 空舰队不能执行打捞"

    s = scfg(cfg)
    loot = json.loads(w["loot_json"] or "{}")   # 已是「每次一份」，不再现算
    lines = []
    risk_txt = ""
    # §11：沉船遗迹有概率触雷/伏击
    if w["kind"] == WRECK and random.random() < float(s.get("wreck_risk", 0.25)):
        dmg = float(s.get("wreck_risk_damage", 90))
        detail = mines.damage_player_fleet(conn, cfg, fleet_id, dmg)
        risk_txt = f"\n⚠️ 打捞过程中遭遇伏击/触雷！{detail}"
        lines.append("　（战利品减半）")
        loot = {k: int(v * 0.5) for k, v in loot.items()}

    sets, vals = [], []
    for res, amount in loot.items():
        if amount <= 0:
            continue
        sets.append(f"{res}=COALESCE({res},0)+?")
        vals.append(amount)
    ion = ""
    if w["kind"] == YUNMO and random.random() < float(s.get("yunmo_ion_chance", 0.3)):
        sets.append("chips=COALESCE(chips,0)+?")
        vals.append(50)
        ion = "\n☢️ 打捞到【离子核心】！（T10 单位的唯一建造材料，全服稀缺）"
    if sets:
        vals.append(qq)
        conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)

    left = int(w["remaining"]) - 1
    conn.execute("UPDATE wrecks SET remaining=? WHERE id=?", (left, w["id"]))
    conn.commit()

    loot_txt = " ".join(f"{k}{v}" for k, v in loot.items() if v > 0) or "（空）"
    kind_name = "雲墨残骸区" if w["kind"] == YUNMO else "沉船遗迹"
    tail = ("残骸已打捞殆尽。" if left <= 0
            else f"该残骸还可打捞 {left} 次。")
    return True, (f"⚓ 在 ({x},{y}) 打捞【{kind_name}】\n"
                  f"获得 {loot_txt}{ion}{risk_txt}\n{tail}")


def wrecks_near(conn, x0: int, y0: int, x1: int, y1: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT x,y,kind,remaining FROM wrecks WHERE remaining>0"
        " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
        (x0, x1, y0, y1)).fetchall()]
