"""P2a AI 世界：海盗 L1~3 据点与巡逻队（§25.1 简化版）。

ai_fleets 两行/据点：
  据点行 comp_json='[]'（仅作锚点与海图 ⚓ 标记，P2a 不可攻打）
  巡逻队行 comp_json=编制堆叠，mission={"init_comp":[...]}；被歼后 respawn_war_ticks 补队
编制堆叠：{cls,tier,qty,hp,maxhp,fire,torpedo,asw,hit,speed}
海盗用同 T 白色标准模块套，经 pools.ship_preview 算属性。
"""
import json
import math
import random

from . import pools, fleet

CLASS_ZH = {"frigate": "海盗护卫舰", "destroyer": "海盗驱逐舰",
            "light_cruiser": "海盗轻巡", "ss_attack": "海盗潜艇",
            "ss_escort": "海盗护航潜艇", "transport": "海盗运输船"}


def standard_stats(cls: str, tier: int) -> dict:
    """该舰种同 T 白色标准模块全套 → 整舰属性。"""
    d = pools.mod_data()
    chosen = {}
    for slot in d["classes"][cls]["slots"]:
        for a in d["archetypes"][cls]:
            if a["slot"] == slot and a["rarity"] == "white":
                chosen[slot] = pools.module_id(cls, tier, a["key"])
                break
    return pools.ship_preview(cls, chosen)["stats"]


def make_comp(cfg: dict, level: int) -> list:
    comp = []
    for cls, tier, qty in cfg["pirate"]["patrols"][str(level)]:
        st = standard_stats(cls, tier)
        comp.append({"cls": cls, "tier": tier, "qty": qty,
                     "hp": st["hp"], "maxhp": st["hp"],
                     "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                     "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                     "speed": st.get("speed", 0)})
    return comp


def ensure_pirate(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> bool:
    """全服尚无海盗时，在 (cx,cy) 环带 22~45 格生成 1 据点 + 1 巡逻队。返回是否新建。"""
    if conn.execute("SELECT COUNT(*) c FROM ai_fleets WHERE faction='pirate'").fetchone()["c"]:
        return False
    pcfg = cfg["pirate"]
    # P2a 新服固定 L1（新手仅 T1，可战胜）；P2b 再做随玩家最高 T 浮动的动态难度
    level = 1
    dist = random.randint(pcfg["spawn_min"], pcfg["spawn_max"])
    ang = random.uniform(0, 2 * math.pi)
    hx = max(0, min(fleet.MAP_SIZE - 1, cx + round(dist * math.cos(ang))))
    hy = max(0, min(fleet.MAP_SIZE - 1, cy + round(dist * math.sin(ang))))
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES('pirate',?,?,?,?,?,?, '[]', '{}', ?)",
        (f"海盗据点 L{level}", level, hx, hy, hx, hy, war_tick))
    comp = make_comp(cfg, level)
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES('pirate',?,?,?,?,?,?,?, ?, ?)",
        (f"海盗巡逻队 L{level}", level, hx, hy, hx, hy,
         json.dumps(comp, ensure_ascii=False),
         json.dumps({"init_comp": comp}, ensure_ascii=False), war_tick))
    conn.commit()
    return True


def _in_battle(conn, afid: int):
    for r in conn.execute(
            "SELECT id FROM battles WHERE status='active' AND ai_fleet_id=?",
            (afid,)).fetchall():
        return r["id"]
    return None


def war_tick_ai(conn, cfg: dict, war_tick: int):
    """巡逻队游荡（50%/tick 走 1 格，限据点半径内）；到期回据点满编补队。"""
    radius = cfg["pirate"]["wander_radius"]
    resp = cfg["pirate"]["respawn_war_ticks"]
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction='pirate'").fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        mission = json.loads(r["mission"] or "{}")
        if not comp:
            # 被歼：到点在据点满编重生
            if r["respawn_tick"] is not None and war_tick >= r["respawn_tick"] \
                    and mission.get("init_comp"):
                init = json.loads(json.dumps(mission["init_comp"]))
                conn.execute(
                    "UPDATE ai_fleets SET comp_json=?,x=hx,y=hy,respawn_tick=NULL WHERE id=?",
                    (json.dumps(init, ensure_ascii=False), r["id"]))
                conn.commit()
            continue
        if "init_comp" not in mission or _in_battle(conn, r["id"]):
            continue  # 据点行或交战中不游荡
        x, y, hx, hy = r["x"], r["y"], r["hx"], r["hy"]
        if random.random() >= 0.5:
            continue
        dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        nx, ny = x + dx, y + dy
        if math.hypot(nx - hx, ny - hy) > radius:
            nx, ny = hx, hy  # 越界回锚点
        nx = max(0, min(fleet.MAP_SIZE - 1, nx))
        ny = max(0, min(fleet.MAP_SIZE - 1, ny))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()
