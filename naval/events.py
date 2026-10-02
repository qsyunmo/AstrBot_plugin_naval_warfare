"""§8 随机事件（可订阅推送、可配置频率）+ §18.3 的事件系数 eF。

设计文档 §8 列出的 12 类事件都在 config.events.catalog 里，按 scope 分两类：
  global —— 全服性环境事件（台风/浓雾/洋流/鱼群/火山/潮汐），影响所有人
  player —— 落在某个提督头上的事件（井喷/罢工/哗变/疫情/难民/幽灵船）

产出公式里的「事件系数 eF」就是把这些事件对某资源的倍率叠乘起来
（§18.3：Y = … × 民心系数 mF × 航线系数 rtF × 电力系数 pF × 事件系数 eF）。
"""
import json
import math
import random

TICKS_PER_DAY = 72
WAR_TICKS_PER_DAY = 144


def ecfg(cfg: dict) -> dict:
    return cfg.get("events") or {}


def catalog(cfg: dict) -> dict:
    return ecfg(cfg).get("catalog") or {}


def event_name(cfg: dict, key: str) -> str:
    return (catalog(cfg).get(key) or {}).get("name", key)


def active_events(conn, now_tick: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM active_events WHERE end_tick>?", (now_tick,)).fetchall()]


def expire_events(conn, econ_tick: int, war_tick: int) -> list:
    """把过期事件清掉。返回被清掉的 event key 列表（用于推送"事件结束"）。"""
    rows = conn.execute("SELECT * FROM active_events").fetchall()
    gone = []
    for r in rows:
        if int(r["end_tick"] or 0) <= econ_tick:
            gone.append(r["event"])
            conn.execute("DELETE FROM active_events WHERE id=?", (r["id"],))
    if gone:
        conn.commit()
    return gone


def roll_event(conn, cfg, econ_tick: int, war_tick: int, players: list):
    """按频率掷一个新事件并应用即时效果。返回 [(qq 或 None, text)]。"""
    c = ecfg(cfg)
    if not c.get("enabled", True):
        return []
    if random.random() >= float(c.get("chance", 0.35)):
        return []
    live = active_events(conn, econ_tick)
    if len(live) >= int(c.get("max_active", 6)):
        return []
    pool = catalog(cfg)
    if not pool:
        return []
    keys = [k for k in pool if not k.startswith("_")]
    # 已在生效的同名事件不重复触发
    keys = [k for k in keys if k not in {r["event"] for r in live}]
    if not keys:
        return []
    weights = [float((pool[k] or {}).get("weight", 1)) for k in keys]
    key = random.choices(keys, weights=weights, k=1)[0]
    spec = pool[key] or {}
    days = float(spec.get("days", 1))
    end = econ_tick + max(1, int(days * TICKS_PER_DAY))
    scope = spec.get("scope", "global")
    who = None
    if scope == "player" and players:
        pick = random.choice(players)
        who = pick["qq"] if isinstance(pick, dict) else pick
    conn.execute(
        "INSERT INTO active_events(event,scope,qq,start_tick,end_tick,data_json)"
        " VALUES(?,?,?,?,?,'{}')", (key, scope, who, econ_tick, end))
    conn.commit()

    name = spec.get("name", key)
    desc = spec.get("desc", "")
    out = []

    # —— 即时效果 ——
    targets = [who] if who else [(p["qq"] if isinstance(p, dict) else p)
                                 for p in players]
    for qq in targets:
        if not qq:
            continue
        sets, vals = [], []
        if spec.get("morale"):
            sets.append("morale=MAX(0,MIN(100,COALESCE(morale,60)+?))")
            vals.append(float(spec["morale"]))
        if spec.get("manpower"):
            sets.append("manpower=MAX(0,COALESCE(manpower,0)+?)")
            vals.append(float(spec["manpower"]))
        if spec.get("food"):
            sets.append("food=MAX(0,COALESCE(food,0)+?)")
            vals.append(float(spec["food"]))
        if spec.get("money"):
            sets.append("money=COALESCE(money,0)+?")
            vals.append(float(spec["money"]))
        if sets:
            vals.append(qq)
            conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)
    conn.commit()

    extra = []
    if spec.get("new_island"):
        extra.append(_spawn_island(conn, cfg, players))
    if spec.get("spawn_boss"):
        extra.append(_spawn_boss(conn, cfg, players, war_tick))

    eff = []
    if spec.get("eF"):
        eff.append("产出 " + "、".join(f"{k}×{v}" for k, v in spec["eF"].items()))
    if spec.get("morale"):
        eff.append(f"民心{spec['morale']:+.0f}")
    if spec.get("manpower"):
        eff.append(f"人力{spec['manpower']:+.0f}")
    if spec.get("food"):
        eff.append(f"食物{spec['food']:+.0f}")
    if spec.get("money"):
        eff.append(f"资金{spec['money']:+.0f}")
    txt = (f"📢 随机事件【{name}】{desc}\n"
           + ("　影响：" + "、".join(eff) if eff else "")
           + ("\n　" + "\n　".join(x for x in extra if x) if any(extra) else ""))
    out.append((who, txt))
    return out


def _spawn_island(conn, cfg, players) -> str:
    """火山喷发造新岛（§8）。"""
    if not players:
        return ""
    p = random.choice(players)
    cx, cy = p.get("capital_x"), p.get("capital_y")
    if cx is None:
        return ""
    rng = random.Random(f"volcano-{cx}-{cy}-{random.random()}")
    for _ in range(60):
        d = rng.randint(25, 70)
        a = rng.uniform(0, 6.2832)
        x = max(0, min(999, cx + round(d * math.cos(a))))
        y = max(0, min(999, cy + round(d * math.sin(a))))
        if conn.execute("SELECT 1 FROM islands WHERE x=? AND y=?", (x, y)).fetchone():
            continue
        itype = list((cfg.get("island_types") or {"rocky": {}}).keys())[0]
        hp = (cfg["island_types"].get(itype) or {}).get("hp", 100)
        conn.execute(
            "INSERT INTO islands(x,y,itype,ore_json,dev_level,owner_qq,owner_kind,"
            "control,morale,hp) VALUES(?,?,?,'{}',1,NULL,NULL,0,50,?)",
            (x, y, itype, hp))
        conn.commit()
        return f"🌋 火山喷发在 ({x},{y}) 造出一座新岛——无主，可派舰队 /nw登陆 占领。"
    return "🌋 火山喷发，但新岛没入海底（没有合适位置）。"


def _spawn_boss(conn, cfg, players, war_tick: int) -> str:
    """超级 BOSS 潮汐（§8）：刷一支深海巨兽舰队。"""
    from . import aiworld
    if not players:
        return ""
    p = random.choice(players)
    cx, cy = p.get("capital_x"), p.get("capital_y")
    if cx is None:
        return ""
    rng = random.Random(f"boss-{cx}-{cy}-{war_tick}")
    d = rng.randint(30, 60)
    a = rng.uniform(0, 6.2832)
    x = max(0, min(999, cx + round(d * math.cos(a))))
    y = max(0, min(999, cy + round(d * math.sin(a))))
    comp = aiworld._comp_from_table(
        cfg, {"light_cruiser": 20, "destroyer": 60, "ss_attack": 20}, 5, 0.1, {})
    conn.execute(
        "INSERT INTO ai_fleets(faction,name,level,x,y,hx,hy,comp_json,mission,created_tick)"
        " VALUES('rebel','深海巨兽 L40',40,?,?,?,?,?,'{}',?)",
        (x, y, x, y, json.dumps(comp, ensure_ascii=False), war_tick))
    conn.commit()
    return f"🌊 深海巨兽在 ({x},{y}) 浮出水面（L40，海图 b 标记）——可打可避。"


def event_factor(conn, cfg, qq: str, res: str, econ_tick: int) -> float:
    """§18.3 事件系数 eF：叠加所有生效事件对该资源的倍率。"""
    f = 1.0
    for r in active_events(conn, econ_tick):
        spec = catalog(cfg).get(r["event"]) or {}
        if r["scope"] == "player" and r["qq"] != qq:
            continue
        f *= float((spec.get("eF") or {}).get(res, 1.0))
    return f


def summary(conn, cfg, econ_tick: int) -> str:
    """当前生效事件的一行摘要（供 /nw我 显示）。"""
    rows = active_events(conn, econ_tick)
    if not rows:
        return ""
    parts = []
    for r in rows:
        nm = event_name(cfg, r["event"])
        if r["scope"] == "player" and r["qq"]:
            parts.append(f"{nm}(限{r['qq']})")
        else:
            parts.append(nm)
    return "　".join(parts)
