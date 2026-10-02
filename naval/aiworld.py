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
import time

from . import pools, fleet

CLASS_ZH = {"frigate": "海盗护卫舰", "destroyer": "海盗驱逐舰",
            "light_cruiser": "海盗轻巡", "ss_attack": "海盗潜艇",
            "ss_escort": "海盗护航潜艇", "transport": "海盗运输船"}

# 中立/阵营通用的舰种名（雇佣军团等非海盗势力用；CLASS_ZH 是海盗专用叫法）
CLASS_ZH_BASE = {"frigate": "护卫舰", "destroyer": "驱逐舰",
                 "light_cruiser": "轻巡洋舰", "ss_attack": "攻击潜艇",
                 "ss_escort": "护航潜艇", "transport": "运输船"}


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


MERCHANT = "neutral_merchant"
EMPIRE_MERCHANT = "empire"
GUILD = "guild"
ROUTE_FACTIONS = (MERCHANT, EMPIRE_MERCHANT)


def _rand_near(rng, cx, cy, lo, hi):
    d = rng.randint(lo, hi)
    a = rng.uniform(0, 2 * math.pi)
    return (max(0, min(fleet.MAP_SIZE - 1, cx + round(d * math.cos(a)))),
            max(0, min(fleet.MAP_SIZE - 1, cy + round(d * math.sin(a)))))


def _route_fleet(conn, cfg: dict, faction: str, ckey: str, label: str,
                 cx: int, cy: int, war_tick: int, level: int = 1) -> int:
    """通用的"沿航线跑的商船队"生成器（§26.2 中立商船 / 帝国商船）。

    配置段：count / spawn_min / spawn_max / comp[, escort_chance, escort_comp]
            / speed / flee_dist / loot_money / loot_cargo
    """
    mcfg = cfg.get(ckey) or {}
    want = int(mcfg.get("count", 2))
    have = conn.execute("SELECT COUNT(*) c FROM ai_fleets WHERE faction=?",
                        (faction,)).fetchone()["c"]
    made = 0
    rng = random.Random(f"{cx}-{cy}-{war_tick}-{faction}")
    for i in range(max(0, want - have)):
        ax, ay = _rand_near(rng, cx, cy, int(mcfg.get("spawn_min", 12)),
                            int(mcfg.get("spawn_max", 30)))
        bx, by = _rand_near(rng, cx, cy, int(mcfg.get("spawn_min", 12)),
                            int(mcfg.get("spawn_max", 30)))
        if (ax, ay) == (bx, by):
            bx = (bx + 20) % fleet.MAP_SIZE
        groups = mcfg.get("comp") or [["transport", 1, 2]]
        if rng.random() < float(mcfg.get("escort_chance", 0.0)):
            groups = mcfg.get("escort_comp") or groups
        comp = []
        for cls, tier, qty in groups:
            st = standard_stats(cls, tier)
            comp.append({"cls": cls, "tier": tier, "qty": qty,
                         "hp": st["hp"], "maxhp": st["hp"],
                         "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                         "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                         "speed": st.get("speed", 0)})
        mission = {"route": {"ax": ax, "ay": ay, "bx": bx, "by": by},
                   "leg": "a", "init_comp": json.loads(json.dumps(comp)),
                   "cargo": dict(mcfg.get("loot_cargo") or {}),
                   "gold": int(mcfg.get("loot_money", 300)),
                   "flee_dist": int(mcfg.get("flee_dist", 4)),
                   "speed": max(1, int(mcfg.get("speed", 3))),
                   "respawn": int(mcfg.get("respawn_war_ticks", 40))}
        conn.execute(
            "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (faction, f"{label}{i + 1}", level, ax, ay, ax, ay,
             json.dumps(comp, ensure_ascii=False),
             json.dumps(mission, ensure_ascii=False), war_tick))
        made += 1
    if made:
        conn.commit()
    return made


def ensure_merchants(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> int:
    """按配置数量补齐中立商船队（§26.2）。"""
    return _route_fleet(conn, cfg, MERCHANT, "merchant", "中立商船队", cx, cy, war_tick)


def ensure_empire_merchant(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> int:
    """§26.2 帝国商船队：满载钢/油/铝/稀土，全图航标可见，可洗劫（暴富+通缉）。"""
    return _route_fleet(conn, cfg, EMPIRE_MERCHANT, "empire_merchant",
                        "帝国商船队", cx, cy, war_tick, level=4)


def _patrol_fleet(conn, cfg: dict, faction: str, ckey: str, label: str,
                  cx: int, cy: int, war_tick: int) -> int:
    """通用的"在某片海域巡弋的武装巡逻队"生成器（§26.2 协会舰队等）。"""
    pcfg = cfg.get(ckey) or {}
    want = int(pcfg.get("count", 1))
    have = conn.execute("SELECT COUNT(*) c FROM ai_fleets WHERE faction=?",
                        (faction,)).fetchone()["c"]
    made = 0
    rng = random.Random(f"{cx}-{cy}-{war_tick}-{faction}")
    for i in range(max(0, want - have)):
        hx, hy = _rand_near(rng, cx, cy, int(pcfg.get("spawn_min", 20)),
                            int(pcfg.get("spawn_max", 40)))
        comp = []
        for cls, tier, qty in (pcfg.get("comp") or [["frigate", 2, 2]]):
            st = standard_stats(cls, tier)
            comp.append({"cls": cls, "tier": tier, "qty": qty,
                         "hp": st["hp"], "maxhp": st["hp"],
                         "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                         "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                         "speed": st.get("speed", 0)})
        mission = {"init_comp": json.loads(json.dumps(comp)),
                   "hx": hx, "hy": hy,
                   "wander_radius": int(pcfg.get("wander_radius", 20)),
                   "hunt_radius": int(pcfg.get("hunt_radius", 6)),
                   "speed": max(1, int(pcfg.get("speed", 3))),
                   "respawn": int(pcfg.get("respawn_war_ticks", 60)),
                   "hunts": list(pcfg.get("hunts") or ["pirate", "rebel"])}
        conn.execute(
            "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (faction, f"{label}{i + 1}", 10, hx, hy, hx, hy,
             json.dumps(comp, ensure_ascii=False),
             json.dumps(mission, ensure_ascii=False), war_tick))
        made += 1
    if made:
        conn.commit()
    return made


def ensure_guild_patrol(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> int:
    """§26.2 协会舰队：行会商路反海盗巡逻，会主动攻击海盗/叛军。"""
    return _patrol_fleet(conn, cfg, GUILD, "guild_patrol", "协会巡逻队",
                         cx, cy, war_tick)



def _threat_positions(conn):
    """玩家舰队位置列表（商船规避用）。"""
    return [(r["x"], r["y"]) for r in conn.execute(
        "SELECT x,y FROM fleets WHERE x IS NOT NULL").fetchall()]


def war_tick_merchants(conn, cfg: dict, war_tick: int):
    """所有航线商船队每战争 tick：见军舰就躲（flee_dist>0 时），否则沿航线走一格。

    中立商船见军舰就跑；帝国商船 flee_dist=0（有护航部队，不规避）。
    """
    threats = _threat_positions(conn)
    for r in conn.execute(
            "SELECT * FROM ai_fleets WHERE faction IN (?,?)",
            ROUTE_FACTIONS).fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        m = json.loads(r["mission"] or "{}")
        speed = max(1, int(m.get("speed", 3)))
        flee = int(m.get("flee_dist", 4))
        if not comp:
            # 被劫掠：到点在航线 A 端满编重整
            if r["respawn_tick"] is not None and war_tick >= r["respawn_tick"] \
                    and m.get("init_comp"):
                route = m.get("route") or {}
                conn.execute(
                    "UPDATE ai_fleets SET comp_json=?,x=?,y=?,respawn_tick=NULL WHERE id=?",
                    (json.dumps(m["init_comp"], ensure_ascii=False),
                     route.get("ax", r["x"]), route.get("ay", r["y"]), r["id"]))
                conn.commit()
            continue
        if _in_battle(conn, r["id"]):
            continue
        route = m.get("route") or {}
        x, y = r["x"], r["y"]

        if flee > 0:
            near = [(tx, ty) for tx, ty in threats
                    if max(abs(tx - x), abs(ty - y)) <= flee]
            if near:
                dx = sum(1 if x >= tx else -1 for tx, _ in near)
                dy = sum(1 if y >= ty else -1 for _, ty in near)
                if dx == 0 and dy == 0:
                    dx = 1
                nx = max(0, min(fleet.MAP_SIZE - 1, x + (1 if dx > 0 else -1) * speed))
                ny = max(0, min(fleet.MAP_SIZE - 1, y + (1 if dy > 0 else -1) * speed))
                conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
                continue

        if not route:
            continue
        leg = m.get("leg", "a")
        tx = route.get("bx" if leg == "a" else "ax", x)
        ty = route.get("by" if leg == "a" else "ay", y)
        nx = x + max(-speed, min(speed, tx - x))
        ny = y + max(-speed, min(speed, ty - y))
        if (nx, ny) == (tx, ty):
            m["leg"] = "b" if leg == "a" else "a"
            conn.execute("UPDATE ai_fleets SET x=?,y=?,mission=? WHERE id=?",
                         (nx, ny, json.dumps(m, ensure_ascii=False), r["id"]))
        else:
            conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()


MERC = "merc"
MERC_LEVELS = (8, 20, 30)


def merc_spec(cfg: dict, level: int) -> dict:
    """取最接近该等级的雇佣报价档（§26.2 L8~L40）。"""
    levels = (cfg.get("merc") or {}).get("levels") or {}
    keys = sorted((int(k) for k in levels), key=lambda v: abs(v - level))
    return levels.get(str(keys[0])) or {} if keys else {}


def ensure_merc(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> int:
    """§26.2 雇佣军团：全图漂游的成建制小舰队，有公司名。"""
    mcfg = cfg.get("merc") or {}
    want = int(mcfg.get("count", 2))
    have = conn.execute("SELECT COUNT(*) c FROM ai_fleets WHERE faction=?",
                        (MERC,)).fetchone()["c"]
    made = 0
    rng = random.Random(f"{cx}-{cy}-{war_tick}-merc")
    names = list(mcfg.get("names") or ["白鲸佣兵团"])
    for i in range(max(0, want - have)):
        lv = MERC_LEVELS[(have + i) % len(MERC_LEVELS)]
        spec = merc_spec(cfg, lv)
        hx, hy = _rand_near(rng, cx, cy, int(mcfg.get("spawn_min", 25)),
                            int(mcfg.get("spawn_max", 60)))
        comp = []
        for cls, tier, qty in (spec.get("comp") or [["destroyer", 2, 2]]):
            st = standard_stats(cls, tier)
            comp.append({"cls": cls, "tier": tier, "qty": qty,
                         "hp": st["hp"], "maxhp": st["hp"],
                         "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                         "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                         "speed": st.get("speed", 0)})
        name = names[(have + i) % len(names)] + f" L{lv}"
        mission = {"init_comp": json.loads(json.dumps(comp)), "hx": hx, "hy": hy,
                   "wander_radius": int(mcfg.get("wander_radius", 60)),
                   "speed": max(1, int(mcfg.get("speed", 2))),
                   "talk_radius": int(mcfg.get("talk_radius", 5)),
                   "level": lv, "company": names[(have + i) % len(names)]}
        conn.execute(
            "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (MERC, name, lv, hx, hy, hx, hy,
             json.dumps(comp, ensure_ascii=False),
             json.dumps(mission, ensure_ascii=False), war_tick))
        made += 1
    if made:
        conn.commit()
    return made


def war_tick_merc(conn, cfg: dict, war_tick: int):
    """雇佣军团：全图漂游找合同（不主动攻击，认钱不认人）。"""
    mcfg = cfg.get("merc") or {}
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (MERC,)).fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        m = json.loads(r["mission"] or "{}")
        if not comp:
            if r["respawn_tick"] is not None and war_tick >= r["respawn_tick"] \
                    and m.get("init_comp"):
                conn.execute(
                    "UPDATE ai_fleets SET comp_json=?,x=hx,y=hy,respawn_tick=NULL WHERE id=?",
                    (json.dumps(m["init_comp"], ensure_ascii=False), r["id"]))
                conn.commit()
            continue
        if _in_battle(conn, r["id"]):
            continue
        speed = max(1, int(m.get("speed", mcfg.get("speed", 2))))
        radius = int(m.get("wander_radius", mcfg.get("wander_radius", 60)))
        hx, hy = r["hx"], r["hy"]
        if max(abs(r["x"] - hx), abs(r["y"] - hy)) > radius:
            nx = r["x"] + max(-speed, min(speed, hx - r["x"]))
            ny = r["y"] + max(-speed, min(speed, hy - r["y"]))
        else:
            dx, dy = random.choice([(speed, 0), (-speed, 0), (0, speed), (0, -speed)])
            nx, ny = r["x"] + dx, r["y"] + dy
        nx = max(0, min(fleet.MAP_SIZE - 1, nx))
        ny = max(0, min(fleet.MAP_SIZE - 1, ny))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()


REBEL = "rebel"


def spawn_rebel_fleet(conn, cfg, x: int, y: int, level: int, war_tick: int,
                      owner_qq: str = None) -> int:
    """§26.3 哗变：在指定位置生成一支叛军舰队。返回 ai_fleets id。"""
    rcfg = cfg.get("rebellion") or {}
    levels = rcfg.get("rebel_levels") or {}
    key = "20" if level >= 20 else "10"
    groups = levels.get(key) or [["frigate", 1, 2]]
    comp = []
    for cls, tier, qty in groups:
        st = standard_stats(cls, tier)
        comp.append({"cls": cls, "tier": tier, "qty": qty,
                     "hp": st["hp"], "maxhp": st["hp"],
                     "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                     "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                     "speed": st.get("speed", 0)})
    mission = {"init_comp": json.loads(json.dumps(comp)), "hx": x, "hy": y,
               "hunt_radius": int(rcfg.get("rebel_hunt_radius", 10)),
               "control_damage": int(rcfg.get("rebel_control_damage", 4)),
               "expire_tick": war_tick + int(rcfg.get("rebel_lifetime_ticks", 1440)),
               "from_qq": owner_qq}
    cur = conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (REBEL, f"哗变叛军 L{key}", int(key), x, y, x, y,
         json.dumps(comp, ensure_ascii=False),
         json.dumps(mission, ensure_ascii=False), war_tick))
    conn.commit()
    return cur.lastrowid


def normalize_chance(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def settle_rebellion(conn, cfg: dict, war_tick: int) -> list:
    """§26.3/§14.2 叛乱检定（每日一次）：控制度<30 的岛按控制度高低掷骰。

    失败 → 控制度<10 直接易帜中立；否则民心崩 + 生成哗变叛军，并连锁影响周边岛。
    返回 [(qq, text)] 通知列表。
    """
    rcfg = cfg.get("rebellion") or {}
    threshold = int(rcfg.get("control_threshold", 30))
    flip_at = int(rcfg.get("flip_threshold", 10))
    base = float(rcfg.get("base_chance", 0.12))
    maxc = float(rcfg.get("max_chance", 0.55))
    chain_r = int(rcfg.get("chain_radius", 1))
    chain_loss = float(rcfg.get("chain_morale_loss", 10))
    notes = []
    rng = random.Random(f"rebel-{war_tick}")

    rows = conn.execute(
        "SELECT * FROM islands WHERE owner_qq IS NOT NULL AND control < ?",
        (threshold,)).fetchall()
    for isl in rows:
        ctrl = float(isl["control"] or 0)
        # 控制度越低越危险：0 -> maxc，threshold -> base
        ratio = max(0.0, (threshold - ctrl) / max(1.0, threshold))
        chance = min(maxc, base + (maxc - base) * ratio)
        if rng.random() >= chance:
            continue
        owner = isl["owner_qq"]
        is_player = not str(owner).startswith("regular")
        if ctrl < flip_at:
            conn.execute("UPDATE islands SET owner_qq=NULL, owner_kind=NULL,"
                         " control=0, morale=40 WHERE x=? AND y=?",
                         (isl["x"], isl["y"]))
            if is_player:
                notes.append((owner, f"🔥 叛乱成功：({isl['x']},{isl['y']}) 控制度仅 "
                                     f"{ctrl:.0f}，已易帜中立（§14.2 <10 直接易帜）。\n"
                                     f"派舰队过去 /nw登陆 可以重新拿回来。"))
        else:
            conn.execute("UPDATE islands SET morale=MAX(0,COALESCE(morale,50)-?),"
                         " control=MAX(0,control-5) WHERE x=? AND y=?",
                         (chain_loss, isl["x"], isl["y"]))
            lv = 20 if ctrl >= 20 else 10
            n = conn.execute("SELECT COUNT(*) c FROM ai_fleets WHERE faction=?",
                             (REBEL,)).fetchone()["c"]
            spawned = 0
            if n < int(rcfg.get("max_rebels", 4)):
                spawn_rebel_fleet(conn, cfg, isl["x"], isl["y"], lv, war_tick,
                                  owner if is_player else None)
                spawned = 1
            # 连锁：周边岛民心同步下滑（§26.3）
            for nb in conn.execute(
                    "SELECT x,y,morale FROM islands WHERE owner_qq IS NOT NULL"
                    " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                    (isl["x"] - chain_r, isl["x"] + chain_r,
                     isl["y"] - chain_r, isl["y"] + chain_r)).fetchall():
                if (nb["x"], nb["y"]) == (isl["x"], isl["y"]):
                    continue
                conn.execute("UPDATE islands SET morale=MAX(0,COALESCE(morale,50)-?)"
                             " WHERE x=? AND y=?", (chain_loss, nb["x"], nb["y"]))
            if is_player:
                notes.append((owner,
                              f"⚠️ ({isl['x']},{isl['y']}) 守军哗变！控制度 {ctrl:.0f} "
                              f"触发叛乱检定失败。\n"
                              + (f"一支 L{lv} 哗变叛军已在岛上成军。\n" if spawned
                                 else "（叛军数量已达上限，本次未成军）\n")
                              + f"周边岛屿民心 −{chain_loss:.0f}（§26.3 连锁）。\n"
                              f"驻军/舰队在场可镇压，否则尽快派兵。"))
    conn.commit()
    return notes


def war_tick_rebels(conn, cfg: dict, war_tick: int):
    """§26.3 叛军「自己找弱岛打」：扑向最近的控制度低岛并骚扰，到期解散。"""
    rcfg = cfg.get("rebellion") or {}
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (REBEL,)).fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        m = json.loads(r["mission"] or "{}")
        if not comp:
            continue
        if war_tick >= int(m.get("expire_tick", 0)):
            # 风头过去：叛军解散
            conn.execute("UPDATE ai_fleets SET comp_json='[]' WHERE id=?", (r["id"],))
            conn.commit()
            continue
        if _in_battle(conn, r["id"]):
            continue
        radius = int(m.get("hunt_radius", rcfg.get("rebel_hunt_radius", 10)))
        tgt = conn.execute(
            "SELECT x,y,control,owner_qq FROM islands WHERE owner_qq IS NOT NULL"
            " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?"
            " ORDER BY control ASC LIMIT 1",
            (max(0, r["x"] - radius), min(fleet.MAP_SIZE - 1, r["x"] + radius),
             max(0, r["y"] - radius), min(fleet.MAP_SIZE - 1, r["y"] + radius))).fetchone()
        if not tgt:
            continue
        if (r["x"], r["y"]) == (tgt["x"], tgt["y"]):
            # 同格：骚扰削弱控制度与民心
            dmg = int(m.get("control_damage", rcfg.get("rebel_control_damage", 4)))
            conn.execute("UPDATE islands SET control=MAX(0,control-?),"
                         " morale=MAX(0,COALESCE(morale,50)-3) WHERE x=? AND y=?",
                         (dmg, tgt["x"], tgt["y"]))
            conn.commit()
            continue
        nx = r["x"] + max(-1, min(1, tgt["x"] - r["x"]))
        ny = r["y"] + max(-1, min(1, tgt["y"] - r["y"]))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()


def _comp_power(comp: list) -> float:
    return sum(int(s.get("qty", 0) or 0) *
               (float(s.get("fire", 0) or 0) + float(s.get("torpedo", 0) or 0))
               for s in comp)


def resolve_skirmish(conn, cfg: dict, war_tick: int, att, dfd) -> dict:
    """§26.2 协会舰队主动攻击海盗/叛军：AI 对 AI 的遭遇战。

    battle 表是"玩家 vs AI"的结构，装不下纯 AI 交战；这里用一次决定性结算抽象：
    按双方战力比互相削减舰数，打空的一方进入 respawn 冷却。
    返回 {'attacker_loss','defender_loss','wiped'}。
    """
    ac = json.loads(att["comp_json"] or "[]")
    dc = json.loads(dfd["comp_json"] or "[]")
    pa, pd = _comp_power(ac) or 1.0, _comp_power(dc) or 1.0
    total = pa + pd
    # 单次遭遇战最多吃掉对方 40%，避免一击秒杀（抽象，非逐轮模拟）
    kill_d = max(1, int(sum(int(s.get("qty", 0) or 0) for s in dc) * min(0.4, pa / total)))
    kill_a = max(0, int(sum(int(s.get("qty", 0) or 0) for s in ac) * min(0.4, pd / total)))

    def _apply(comp, kills):
        left = kills
        for s in comp:
            if left <= 0:
                break
            take = min(int(s.get("qty", 0) or 0), left)
            s["qty"] = int(s.get("qty", 0) or 0) - take
            left -= take
        return sum(int(s.get("qty", 0) or 0) for s in comp)

    a_left = _apply(ac, kill_a)
    d_left = _apply(dc, kill_d)
    am = json.loads(att["mission"] or "{}")
    dm = json.loads(dfd["mission"] or "{}")
    for row, comp, left, m in ((att, ac, a_left, am), (dfd, dc, d_left, dm)):
        resp = int(m.get("respawn", 60))
        if left <= 0:
            conn.execute("UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
                         (war_tick + resp, row["id"]))
        else:
            conn.execute("UPDATE ai_fleets SET comp_json=? WHERE id=?",
                         (json.dumps(comp, ensure_ascii=False), row["id"]))
    conn.commit()
    return {"attacker_loss": kill_a, "defender_loss": kill_d,
            "defender_wiped": d_left <= 0, "attacker_wiped": a_left <= 0}


def war_tick_patrols(conn, cfg: dict, war_tick: int):
    """§26.2 武装巡逻队（协会等）：在锚点周边巡弋，发现猎物则主动扑上去。"""
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (GUILD,)).fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        m = json.loads(r["mission"] or "{}")
        if not comp:
            if r["respawn_tick"] is not None and war_tick >= r["respawn_tick"] \
                    and m.get("init_comp"):
                conn.execute(
                    "UPDATE ai_fleets SET comp_json=?,x=hx,y=hy,respawn_tick=NULL WHERE id=?",
                    (json.dumps(m["init_comp"], ensure_ascii=False), r["id"]))
                conn.commit()
            continue
        if _in_battle(conn, r["id"]):
            continue
        speed = max(1, int(m.get("speed", 3)))
        hx, hy = r["hx"], r["hy"]
        hunt_r = int(m.get("hunt_radius", 6))
        hunts = m.get("hunts") or ["pirate", "rebel"]

        # 找最近的猎物（§26.2：会主动攻击正在交战中的海盗/叛军）
        prey = None
        best = None
        for p in conn.execute(
                "SELECT id,x,y,faction FROM ai_fleets WHERE comp_json!='[]'"
                " AND faction IN ({})".format(",".join("?" * len(hunts))),
                tuple(hunts)).fetchall():
            if p["id"] == r["id"]:
                continue
            d = max(abs(p["x"] - r["x"]), abs(p["y"] - r["y"]))
            if d <= hunt_r and (best is None or d < best):
                best, prey = d, p
        if prey is not None:
            if best == 0:
                # 已同格：§26.2 协会舰队「会主动攻击海盗/叛军」——直接打
                if _in_battle(conn, r["id"]) or _in_battle(conn, prey["id"]):
                    continue
                row = conn.execute("SELECT * FROM ai_fleets WHERE id=?",
                                   (prey["id"],)).fetchone()
                resolve_skirmish(conn, cfg, war_tick, r, row)
                continue
            nx = r["x"] + max(-speed, min(speed, prey["x"] - r["x"]))
            ny = r["y"] + max(-speed, min(speed, prey["y"] - r["y"]))
            conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
            continue

        # 无猎物：在锚点半径内游荡
        radius = int(m.get("wander_radius", 20))
        if max(abs(r["x"] - hx), abs(r["y"] - hy)) > radius:
            nx = r["x"] + max(-speed, min(speed, hx - r["x"]))
            ny = r["y"] + max(-speed, min(speed, hy - r["y"]))
        else:
            import random as _rnd
            dx, dy = _rnd.choice([(speed, 0), (-speed, 0), (0, speed), (0, -speed)])
            nx, ny = r["x"] + dx, r["y"] + dy
        nx = max(0, min(fleet.MAP_SIZE - 1, nx))
        ny = max(0, min(fleet.MAP_SIZE - 1, ny))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()


ENFORCER = "enforcer"


def enforcer_level(cfg: dict, infamy: int) -> int | None:
    """§25.3：恶名 ≥ trigger 就出执法者，等级按 10/20/30/40/50 分档。"""
    ecfg = cfg.get("enforcer") or {}
    trigger = int(ecfg.get("trigger_infamy", 10))
    if infamy < trigger:
        return None
    for lv in (50, 40, 30, 20, 10):
        if infamy >= lv:
            return lv
    return None


def enforcer_comp(cfg: dict, level: int) -> list:
    """按 §25.3 表生成执法者编制，乘 enforcer.scale 以适配本引擎的结算粒度。"""
    ecfg = cfg.get("enforcer") or {}
    table = (ecfg.get("levels") or {}).get(str(level)) or {}
    scale = float(ecfg.get("scale", 1.0))
    tier = int(ecfg.get("tier", 5))
    comp = []
    for cls, qty in table.items():
        n = max(1, round(int(qty) * scale))
        st = standard_stats(cls, tier)
        comp.append({"cls": cls, "tier": tier, "qty": n,
                     "hp": st["hp"], "maxhp": st["hp"],
                     "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                     "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                     "speed": st.get("speed", 0)})
    return comp


def ensure_enforcer(conn, cfg: dict, qq: str, cx: int, cy: int, war_tick: int) -> int | None:
    """按恶名判定是否要刷执法者批次。返回新批次等级或 None。

    同玩家同时只存在一批（§25.3「执法者损失不重生（同批次内）」）。
    """
    ecfg = cfg.get("enforcer") or {}
    p = conn.execute("SELECT infamy,capital_x,capital_y FROM players WHERE qq=?",
                     (qq,)).fetchone()
    if not p:
        return None
    infamy = int(p["infamy"] or 0)
    level = enforcer_level(cfg, infamy)
    if level is None:
        return None
    # 已有未全歼的批次 → 不重复刷
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (ENFORCER,)).fetchall():
        m = json.loads(r["mission"] or "{}")
        if m.get("target_qq") == qq and json.loads(r["comp_json"] or "[]"):
            return None
        if m.get("target_qq") == qq:
            cd = int(m.get("cooldown_until", 0))
            if war_tick < cd:
                return None

    rng = random.Random(f"enf-{qq}-{war_tick}-{level}")
    d = rng.randint(int(ecfg.get("spawn_dist_min", 20)),
                    int(ecfg.get("spawn_dist_max", 40)))
    a = rng.uniform(0, 2 * math.pi)
    ex = max(0, min(fleet.MAP_SIZE - 1, cx + round(d * math.cos(a))))
    ey = max(0, min(fleet.MAP_SIZE - 1, cy + round(d * math.sin(a))))

    comp = enforcer_comp(cfg, level)
    mission = {"target_qq": qq, "level": level,
               "init_comp": json.loads(json.dumps(comp)),
               "cooldown_until": 0}
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ENFORCER, f"跨国联合执法舰队 L{level}", level, ex, ey, ex, ey,
         json.dumps(comp, ensure_ascii=False),
         json.dumps(mission, ensure_ascii=False), war_tick))
    conn.commit()
    return level


def war_tick_enforcer(conn, cfg: dict, war_tick: int):
    """执法者每战争 tick：全速扑向目标玩家（最近舰队优先，否则首都）。"""
    ecfg = cfg.get("enforcer") or {}
    speed = max(1, int(ecfg.get("hunt_speed", 2)))
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (ENFORCER,)).fetchall():
        comp = json.loads(r["comp_json"] or "[]")
        m = json.loads(r["mission"] or "{}")
        qq = m.get("target_qq")
        if not comp or not qq:
            continue
        if _in_battle(conn, r["id"]):
            continue
        tgt = conn.execute(
            "SELECT x,y FROM fleets WHERE qq=? AND x IS NOT NULL"
            " ORDER BY (ABS(x-?)+ABS(y-?)) LIMIT 1", (qq, r["x"], r["y"])).fetchone()
        if tgt:
            tx, ty = tgt["x"], tgt["y"]
        else:
            pl = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?",
                              (qq,)).fetchone()
            if not pl or pl["capital_x"] is None:
                continue
            tx, ty = pl["capital_x"], pl["capital_y"]
        nx = r["x"] + max(-speed, min(speed, tx - r["x"]))
        ny = r["y"] + max(-speed, min(speed, ty - r["y"]))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, r["id"]))
    conn.commit()


REGULAR = "regular"
REGULAR_QQ = "regular:{}"      # 正规军势力 id（占 islands.owner_qq 的位，不对应 players 行）


def regular_max_level(cfg: dict) -> int:
    return max(int(k) for k in (cfg.get("regular", {}).get("levels") or {"1": {}}))


def regular_level_for_islands(cfg: dict, n: int) -> int:
    """§25.2：等级 = 发展阶段 = 控制岛屿数。返回满足 n 岛的最高等级。"""
    levels = (cfg.get("regular") or {}).get("levels") or {}
    best = 1
    for k, v in levels.items():
        if n >= int(v.get("islands", 1)):
            best = max(best, int(k))
    return best


def regular_comp(cfg: dict, level: int) -> list:
    """按 §25.2 表生成编制（乘 scale；本引擎缺航母/BB/BC/CA，只用已有舰种）。"""
    rcfg = cfg.get("regular") or {}
    row = (rcfg.get("levels") or {}).get(str(level)) or {}
    scale = float(rcfg.get("scale", 1.0))
    tier = int(row.get("tier", rcfg.get("tier", 2)))
    comp = []
    for cls in ("light_cruiser", "destroyer", "frigate", "ss_attack", "transport"):
        qty = int(row.get(cls, 0))
        if qty <= 0:
            continue
        n = max(1, round(qty * scale))
        st = standard_stats(cls, tier)
        comp.append({"cls": cls, "tier": tier, "qty": n,
                     "hp": st["hp"], "maxhp": st["hp"],
                     "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                     "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                     "speed": st.get("speed", 0)})
    return comp


def _regular_state(conn):
    """返回 {faction_qq: {islands, fleet_row, mission}} —— 按 owner_qq 前缀聚合。"""
    out = {}
    for r in conn.execute(
            "SELECT owner_qq, COUNT(*) c FROM islands"
            " WHERE owner_kind='regular' AND owner_qq IS NOT NULL GROUP BY owner_qq").fetchall():
        out[r["owner_qq"]] = {"islands": r["c"], "fleet": None, "mission": {}}
    for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?", (REGULAR,)).fetchall():
        m = json.loads(r["mission"] or "{}")
        qq = m.get("target_qq")
        if not qq:
            continue
        st = out.setdefault(qq, {"islands": 0, "fleet": None, "mission": {}})
        st["fleet"] = r
        st["mission"] = m
    return out


def ensure_regular(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> bool:
    """全服尚无正规军且有名额时，按玩家的 15~30 环带规则拉一股出来。返回是否新建。"""
    rcfg = cfg.get("regular") or {}
    have = conn.execute("SELECT COUNT(DISTINCT json_extract(mission,'$.target_qq')) c"
                        " FROM ai_fleets WHERE faction=?", (REGULAR,)).fetchone()["c"]
    if have >= int(rcfg.get("max_count", 2)):
        return False

    # 出生点：借用玩家同款环带规则（避开已占岛）
    rng = random.Random(f"regular-{cx}-{cy}-{war_tick}-{have}")
    coord = _find_regular_spawn(conn, cfg, cx, cy, rng)
    if coord is None:
        return False
    x, y = coord
    qq = REGULAR_QQ.format(have + 1)
    if conn.execute("SELECT 1 FROM islands WHERE x=? AND y=?", (x, y)).fetchone():
        return False

    now = int(time.time())
    itype = list(cfg["island_types"].keys())[have % len(cfg["island_types"])]
    conn.execute(
        "INSERT INTO islands(x,y,itype,ore_json,dev_level,owner_qq,owner_kind,control,morale,hp,created_at)"
        " VALUES(?,?,?,'{}',1,?,'regular',100,80,?,?)",
        (x, y, itype, qq, cfg["island_types"][itype]["hp"], now))

    lv = 1
    comp = regular_comp(cfg, lv)
    mission = {"target_qq": qq, "level": lv, "hx": x, "hy": y,
               "init_comp": json.loads(json.dumps(comp)),
               "next_expand": war_tick + int(rcfg.get("expand_interval_ticks", 40))}
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (REGULAR, f"正规军第{have + 1}舰队 L{lv}", lv, x, y, x, y,
         json.dumps(comp, ensure_ascii=False),
         json.dumps(mission, ensure_ascii=False), war_tick))
    conn.commit()
    return True


def _find_regular_spawn(conn, cfg, cx, cy, rng):
    """15~30 环带内找一个没被占的格子作为正规军首都。"""
    rcfg = cfg.get("regular") or {}
    lo = int(rcfg.get("spawn_min", 15))
    hi = int(rcfg.get("spawn_max", 30))
    for _ in range(200):
        d = rng.uniform(lo, hi)
        a = rng.uniform(0, 2 * math.pi)
        x = max(0, min(fleet.MAP_SIZE - 1, cx + round(d * math.cos(a))))
        y = max(0, min(fleet.MAP_SIZE - 1, cy + round(d * math.sin(a))))
        if not conn.execute("SELECT 1 FROM islands WHERE x=? AND y=?", (x, y)).fetchone():
            return x, y
    return None


def war_tick_regular(conn, cfg: dict, war_tick: int):
    """正规军每战争 tick：开疆（吞并附近中立岛）、按岛数升级、舰队被歼后重建、舰队巡弋。"""
    rcfg = cfg.get("regular") or {}
    interval = int(rcfg.get("expand_interval_ticks", 40))
    max_dist = int(rcfg.get("expand_max_dist", 6))
    rebuild = int(rcfg.get("fleet_rebuild_ticks", 30))
    speed = max(1, int(rcfg.get("fleet_speed", 2)))

    for qq, st in _regular_state(conn).items():
        f = st["fleet"]
        lv = regular_level_for_islands(cfg, st["islands"])
        # 扩张上限取最高级要求的岛数（§25.2 L10 为 18 岛）。
        # 注意不能拿"当前等级要求的岛数"当上限——那等于当前拥有数，永远不满足 < 条件。
        max_islands = int(((rcfg.get("levels") or {}).get(
            str(regular_max_level(cfg))) or {}).get("islands", 18))

        # 开疆：到期且未达最高级岛数上限，吞并领土附近的空岛
        if f is not None and war_tick >= int(st["mission"].get("next_expand", 0)):
            m = st["mission"]
            m["next_expand"] = war_tick + interval
            if st["islands"] < max_islands:
                owned = conn.execute(
                    "SELECT x,y FROM islands WHERE owner_qq=?", (qq,)).fetchall()
                best = None
                for o in owned:
                    for cand in conn.execute(
                            "SELECT x,y FROM islands WHERE owner_qq IS NULL"
                            " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                            (max(0, o["x"] - max_dist), min(fleet.MAP_SIZE - 1, o["x"] + max_dist),
                             max(0, o["y"] - max_dist), min(fleet.MAP_SIZE - 1, o["y"] + max_dist))
                    ).fetchall():
                        d = max(abs(cand["x"] - o["x"]), abs(cand["y"] - o["y"]))
                        if d <= max_dist and (best is None or d < best[0]):
                            best = (d, cand["x"], cand["y"])
                if best:
                    conn.execute("UPDATE islands SET owner_qq=?, owner_kind='regular',"
                                 " control=100 WHERE x=? AND y=?",
                                 (qq, best[1], best[2]))
            conn.execute("UPDATE ai_fleets SET mission=? WHERE id=?",
                         (json.dumps(m, ensure_ascii=False), f["id"]))

        # 舰队重建 / 升级
        if f is None:
            continue
        comp = json.loads(f["comp_json"] or "[]")
        if not comp:
            m = st["mission"]
            if war_tick >= int(m.get("rebuild_at", 0)):
                newlv = regular_level_for_islands(cfg, st["islands"])
                newcomp = regular_comp(cfg, newlv)
                m["init_comp"] = json.loads(json.dumps(newcomp))
                m["level"] = newlv
                m["rebuild_at"] = 0
                conn.execute(
                    "UPDATE ai_fleets SET comp_json=?,level=?,x=hx,y=hy,mission=?,"
                    "name=? WHERE id=?",
                    (json.dumps(newcomp, ensure_ascii=False), newlv,
                     json.dumps(m, ensure_ascii=False),
                     f"正规军舰队 L{newlv}", f["id"]))
            elif not m.get("rebuild_at"):
                m["rebuild_at"] = war_tick + rebuild
                conn.execute("UPDATE ai_fleets SET mission=? WHERE id=?",
                             (json.dumps(m, ensure_ascii=False), f["id"]))
            continue

        # 升级：岛多了就把编制换大
        if lv > int(f["level"] or 1):
            newcomp = regular_comp(cfg, lv)
            m = st["mission"]
            m["init_comp"] = json.loads(json.dumps(newcomp))
            m["level"] = lv
            conn.execute("UPDATE ai_fleets SET comp_json=?,level=?,mission=?,name=? WHERE id=?",
                         (json.dumps(newcomp, ensure_ascii=False), lv,
                          json.dumps(m, ensure_ascii=False), f"正规军舰队 L{lv}", f["id"]))

        # 巡弋：在本方岛屿附近游走
        owned = conn.execute("SELECT x,y FROM islands WHERE owner_qq=?", (qq,)).fetchall()
        if owned:
            tgt = owned[war_tick % len(owned)]
            nx = f["x"] + max(-speed, min(speed, tgt["x"] - f["x"]))
            ny = f["y"] + max(-speed, min(speed, tgt["y"] - f["y"]))
            conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, f["id"]))
    conn.commit()


YUNMO = "yunmo"


def _comp_from_table(cfg: dict, table: dict, tier: int, scale: float,
                     class_map: dict = None) -> list:
    """把 {舰种: 数量} 表按缩放系数转成编制堆叠。

    class_map 用于把引擎还没有的舰种（如 BB/BC/CV）映射到已有舰种，
    否则那些条目会被整块丢掉，编制规模严重失真。
    """
    cmap = class_map or {}
    known = pools.mod_data()["classes"]
    merged = {}
    for cls, qty in (table or {}).items():
        if cls.startswith("_"):
            continue
        real = cmap.get(cls, cls)
        if real not in known:
            continue
        merged[real] = merged.get(real, 0) + int(qty)
    comp = []
    # 代际钳制：tier_cost_mult 只有有限的档位，越界会让 standard_stats 抛 IndexError
    try:
        max_tier = len(pools.mod_data().get("tier_cost_mult") or []) or 5
    except Exception:
        max_tier = 5
    tier = max(1, min(int(tier or 1), max_tier))
    for cls, qty in merged.items():
        st = standard_stats(cls, tier)
        n = max(1, round(int(qty) * scale))
        comp.append({"cls": cls, "tier": tier, "qty": n,
                     "hp": st["hp"], "maxhp": st["hp"],
                     "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                     "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                     "speed": st.get("speed", 0)})
    return comp


def ensure_yunmo(conn, cfg: dict, cx: int, cy: int, war_tick: int) -> bool:
    """§26.4 生成雲墨主力 + 两道 L50 实验护航队（固定停泊，不动）。

    返回是否新建。已被击杀（每赛季一次）则不再生成。
    """
    if conn.execute("SELECT 1 FROM ai_fleets WHERE faction=?", (YUNMO,)).fetchone():
        return False
    if conn.execute("SELECT value FROM meta WHERE key='yunmo_killed'").fetchone():
        return False
    ycfg = cfg.get("yunmo") or {}
    scale = float(ycfg.get("scale", 0.02))
    cmap = ycfg.get("class_map") or {}
    core = _comp_from_table(cfg, ycfg.get("core_comp") or {}, 5, scale, cmap)
    if not core:
        return False
    hx, hy = _rand_near(random.Random(f"yunmo-{cx}-{cy}"), cx, cy,
                        int(ycfg.get("spawn_min", 90)),
                        int(ycfg.get("spawn_max", 160)))
    mission = {"init_comp": json.loads(json.dumps(core)), "hx": hx, "hy": hy,
               "anchored": True, "level": 80, "role": "core"}
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (YUNMO, "雲墨の实验型舰队 L80", 80, hx, hy, hx, hy,
         json.dumps(core, ensure_ascii=False),
         json.dumps(mission, ensure_ascii=False), war_tick))

    esc = _comp_from_table(cfg, ycfg.get("escort_comp") or {}, 5,
                           max(0.05, scale * 4), cmap)
    off = int(ycfg.get("escort_offset", 8))
    for i, (dx, dy) in enumerate(((off, 0), (-off, 0))[:int(ycfg.get("escort_count", 2))]):
        ex = max(0, min(fleet.MAP_SIZE - 1, hx + dx))
        ey = max(0, min(fleet.MAP_SIZE - 1, hy + dy))
        em = {"init_comp": json.loads(json.dumps(esc)), "hx": ex, "hy": ey,
              "guard_x": hx, "guard_y": hy, "role": "escort", "level": 50,
              "chase_max": int(ycfg.get("escort_chase_max", 20))}
        conn.execute(
            "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (YUNMO, f"实验型武器舰队护航队 L50 #{i + 1}", 50, ex, ey, ex, ey,
             json.dumps(esc, ensure_ascii=False),
             json.dumps(em, ensure_ascii=False), war_tick))
    conn.commit()
    return True


def war_tick_yunmo(conn, cfg: dict, war_tick: int):
    """§26.2 屏护：任何逼近雲墨主力的玩家舰队，两道护航队优先接战（不追过 chase_max 格）。

    雲墨本身「完全不被动、绝不先打」，所以主力永远停在锚点。
    """
    ycfg = cfg.get("yunmo") or {}
    core = conn.execute("SELECT * FROM ai_fleets WHERE faction=? AND level=80",
                        (YUNMO,)).fetchone()
    if not core:
        return
    for e in conn.execute("SELECT * FROM ai_fleets WHERE faction=? AND level=50",
                          (YUNMO,)).fetchall():
        comp = json.loads(e["comp_json"] or "[]")
        if not comp or _in_battle(conn, e["id"]):
            continue
        m = json.loads(e["mission"] or "{}")
        gx, gy = m.get("guard_x", e["hx"]), m.get("guard_y", e["hy"])
        chase_max = int(m.get("chase_max", ycfg.get("escort_chase_max", 20)))
        radius = int(ycfg.get("screening_radius", 25))

        # 找最近的玩家舰队（在主力警戒圈内）
        threat = None
        best = None
        for f in conn.execute("SELECT * FROM fleets").fetchall():
            d_core = max(abs(f["x"] - gx), abs(f["y"] - gy))
            if d_core > radius:
                continue
            d = max(abs(f["x"] - e["x"]), abs(f["y"] - e["y"]))
            if best is None or d < best:
                best, threat = d, f
        if threat is None:
            # 无威胁：回到主力两侧的阵位（自己的锚点）
            ax, ay = e["hx"], e["hy"]
            if (e["x"], e["y"]) != (ax, ay):
                nx = e["x"] + max(-1, min(1, ax - e["x"]))
                ny = e["y"] + max(-1, min(1, ay - e["y"]))
                conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, e["id"]))
            continue
        if best > chase_max:
            continue        # §26.2 不追过 20 格
        nx = e["x"] + max(-2, min(2, threat["x"] - e["x"]))
        ny = e["y"] + max(-2, min(2, threat["y"] - e["y"]))
        conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?", (nx, ny, e["id"]))
    conn.commit()


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
