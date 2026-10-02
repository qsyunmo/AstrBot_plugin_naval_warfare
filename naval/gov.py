"""内政：建造 / 生产 / 研究的共用逻辑（QQ 指令与 Web 图形界面共用一套）。

把原先只存在于 GameCommands.build 里的校验与落库逻辑抽到这里，
避免 Web 再实现一遍导致两套规则漂移（本项目已经吃过这类亏）。

对外主要接口：
    building_cost(cfg, def_id, target_lv) -> (costs, work_ticks)
    build_options(conn, cfg, qq, x=None, y=None) -> dict
    do_build(conn, cfg, qq, origin, def_id, x=None, y=None, rush=False) -> (ok, text)
    queue_snapshot(conn, cfg, qq) -> dict          # 三条队列 + 剩余时间 + 加急价
    rush_queue(conn, cfg, qq, kind, qid) -> (ok, text)   # kind: build/production
"""
import json
import time

from . import pools, research
from .db import meta_get

RES_ZH = {"steel": "钢材", "oil": "石油", "money": "资金",
          "aluminium": "铝材", "rare_earth": "稀土", "chips": "芯片",
          "food": "食物", "supply": "补给", "manpower": "人力", "science": "科研"}


def rush_mult(cfg) -> float:
    return float(cfg["tick"].get("rush_mult", 1.6))


def rush_enabled(cfg) -> bool:
    return bool(cfg["tick"].get("rush_enabled", True))


def building_cost(cfg: dict, def_id: str, target_lv: int):
    """返回 (costs{steel,oil,money}, work_ticks)。原 GameCommands._building_cost。"""
    bdef = cfg["buildings"][def_id]
    if bdef.get("cost_special") == "governor":
        base = cfg["cost_tiers"]["2"]
        mult = target_lv ** 1.3
        return ({k: int(base[k] * mult) for k in ("steel", "oil", "money")},
                max(2, int(base["work"] * mult / 2)))
    tier = min(5, target_lv)
    base = cfg["cost_tiers"][str(tier)]
    extra = 1.6 ** max(0, target_lv - 5)
    return ({k: int(base[k] * extra) for k in ("steel", "oil", "money")},
            int(base["work"] * extra))


def _mins(cfg, ticks: int) -> int:
    return int(ticks * cfg["tick"]["economic_min"])


def fmt_mins(m: int) -> str:
    """人类可读的时长。经济 tick 调到 8 分钟后，多数建造已不足 1 小时，
    原来的「0小时24分」很难看。"""
    m = int(m)
    if m <= 0:
        return "立即"
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h} 小时" + (f"{mm} 分" if mm else "")
    d, hh = divmod(h, 24)
    return f"{d} 天" + (f"{hh} 小时" if hh else "")


_fmt_mins = fmt_mins   # 兼容内部旧调用


def _rush_cost(cfg, costs: dict) -> dict:
    """加急总价（含基础造价）。"""
    m = rush_mult(cfg)
    return {k: int(round(v * m)) for k, v in costs.items()}


def _val(p, k: str) -> float:
    """兼容 sqlite3.Row 与 dict（Row 没有 .get）。"""
    try:
        v = p[k]
    except (KeyError, IndexError):
        v = None
    return float(v or 0)


def _can_pay(p, costs: dict):
    return all(_val(p, k) >= float(v) for k, v in costs.items())


def _pay(conn, qq, costs: dict):
    sets = ",".join(f"{k}=COALESCE({k},0)-?" for k in costs)
    conn.execute(f"UPDATE players SET {sets} WHERE qq=?", (*costs.values(), qq))


def _short(p, costs: dict):
    return [f"{RES_ZH.get(k, k)}{v - _val(p, k):.0f}"
            for k, v in costs.items() if _val(p, k) < v]


def build_options(conn, cfg, qq: str, x: int = None, y: int = None) -> dict:
    """某岛的可建/可升级建筑清单（供 Web 图形界面渲染）。

    每项含：def_id/name/desc/level/max_lv/next_lv/cost/work/work_min/
    affordable/missing/blocked(不可建原因)/can_build/rush_cost/rush_work_min。
    """
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return {"ok": False, "reason": "尚未注册"}
    if x is None or y is None:
        x, y = p["capital_x"], p["capital_y"]
    isl = conn.execute("SELECT * FROM islands WHERE x=? AND y=?",
                       (x, y)).fetchone()
    if not isl:
        return {"ok": False, "reason": "该坐标没有岛屿"}
    if isl["owner_qq"] != qq:
        return {"ok": False, "reason": "不是你的岛"}
    tdef = cfg["island_types"].get(isl["itype"], {})
    ore = json.loads(isl["ore_json"] or "{}")
    slots_total = tdef.get("slots", 0) + (isl["dev_level"] or 0)
    used = conn.execute("SELECT COUNT(*) c FROM buildings WHERE x=? AND y=?",
                        (x, y)).fetchone()["c"]
    cur = {r["def_id"]: r["level"] for r in conn.execute(
        "SELECT def_id,level FROM buildings WHERE x=? AND y=?", (x, y)).fetchall()}

    out = []
    for def_id, bdef in cfg["buildings"].items():
        level = cur.get(def_id, 0)
        max_lv = bdef["max_lv"]
        if level >= max_lv:
            out.append({"def_id": def_id, "name": bdef["name"],
                        "desc": bdef.get("desc", ""), "level": level,
                        "max_lv": max_lv, "next_lv": None, "maxed": True,
                        "can_build": False, "blocked": "已满级"})
            continue
        target_lv = level + 1
        costs, work = building_cost(cfg, def_id, target_lv)
        rcost = _rush_cost(cfg, costs)
        missing = _short(p, costs)
        blocked = ""
        if level == 0:
            if bdef.get("unique") and def_id in cur:
                blocked = "每岛限 1 座"
            elif used >= slots_total:
                blocked = f"槽位不足（{used}/{slots_total}），先升总督府"
            elif bdef.get("min_dev") and (isl["dev_level"] or 0) < bdef["min_dev"]:
                blocked = f"需岛级 D≥{bdef['min_dev']}"
            elif bdef.get("need_ore") and ore.get(bdef["need_ore"], 0) < 1:
                blocked = {"iron": "本岛无铁矿床", "oil": "本岛无油田"}.get(
                    bdef["need_ore"], "本岛缺少所需矿床")
            elif bdef.get("need_fishery") and ore.get("fish", 0) < 1:
                blocked = "本岛周边无渔场"
        out.append({
            "def_id": def_id, "name": bdef["name"], "desc": bdef.get("desc", ""),
            "level": level, "max_lv": max_lv, "next_lv": target_lv, "maxed": False,
            "cost": costs, "work": work, "work_min": _mins(cfg, work),
            "work_text": _fmt_mins(_mins(cfg, work)),
            "rush_cost": rcost,
            "rush_extra": {k: rcost[k] - costs[k] for k in costs},
            "rush_text": _fmt_mins(0),
            "affordable": not missing, "missing": missing,
            "blocked": blocked, "upgrade": level > 0,
            "can_build": (not blocked) and (not missing),
            "production": bdef.get("production"),
        })
    out.sort(key=lambda o: (o["level"] > 0, o["def_id"]))
    return {"ok": True, "x": x, "y": y, "island": tdef.get("name", ""),
            "dev_level": isl["dev_level"], "slots_used": used,
            "slots_total": slots_total,
            "rush_enabled": rush_enabled(cfg), "rush_mult": rush_mult(cfg),
            "buildings": out}


def do_build(conn, cfg, qq: str, origin: str, def_id: str,
             x: int = None, y: int = None, rush: bool = False):
    """执行建造/升级。返回 (ok, text)。QQ 与 Web 共用，服务端完整校验。"""
    if def_id not in cfg["buildings"]:
        return False, "❌ 没有这种建筑"
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return False, "❌ 先 /nw注册"
    if x is None or y is None:
        x, y = p["capital_x"], p["capital_y"]
    isl = conn.execute("SELECT * FROM islands WHERE x=? AND y=?",
                       (x, y)).fetchone()
    if not isl:
        return False, "❌ 该坐标没有岛屿"
    if isl["owner_qq"] != qq:
        return False, "❌ 不是你的岛"
    bdef = cfg["buildings"][def_id]
    tdef = cfg["island_types"].get(isl["itype"], {})
    ore = json.loads(isl["ore_json"] or "{}")

    existing = conn.execute(
        "SELECT level FROM buildings WHERE x=? AND y=? AND def_id=?",
        (x, y, def_id)).fetchone()
    target_lv = (existing["level"] + 1) if existing else 1
    if target_lv > bdef["max_lv"]:
        return False, f"❌ {bdef['name']} 已满级 Lv{bdef['max_lv']}"
    if not existing:
        used = conn.execute("SELECT COUNT(*) c FROM buildings WHERE x=? AND y=?",
                            (x, y)).fetchone()["c"]
        if used >= tdef.get("slots", 0) + (isl["dev_level"] or 0):
            return False, (f"❌ 槽位不足（{used}/{tdef.get('slots',0)+(isl['dev_level'] or 0)}），"
                           f"先升级总督府提升岛级D")
        if bdef.get("min_dev") and (isl["dev_level"] or 0) < bdef["min_dev"]:
            return False, f"❌ {bdef['name']} 需要岛级 D≥{bdef['min_dev']}（先升总督府）"
        if bdef.get("need_ore") and ore.get(bdef["need_ore"], 0) < 1:
            zh = {"iron": "铁矿", "oil": "油田"}.get(bdef["need_ore"], "矿床")
            return False, f"❌ 本岛无{zh}，无法建造{bdef['name']}"
        if bdef.get("need_fishery") and ore.get("fish", 0) < 1:
            return False, "❌ 本岛周边无渔场，无法建造渔场码头"
    # 同岛同建筑已在队列里排队 → 阻止重复下单
    dup = conn.execute(
        "SELECT id FROM build_queue WHERE qq=? AND x=? AND y=? AND def_id=?",
        (qq, x, y, def_id)).fetchone()
    if dup:
        return False, f"❌ 【{bdef['name']}】已在建造队列中（#{dup['id']}）"

    costs, work = building_cost(cfg, def_id, target_lv)
    if rush:
        if not rush_enabled(cfg):
            return False, "❌ 本服未开启加急"
        pay = _rush_cost(cfg, costs)
    else:
        pay = costs
    missing = _short(p, pay)
    if missing:
        return False, "❌ 资源不足：缺 " + "、".join(missing)

    _pay(conn, qq, pay)
    tick = meta_get(conn, "econ_tick", int, 0)
    name = bdef["name"]
    if rush:
        # 立即完工：直接落库，不等 tick
        if existing:
            conn.execute("UPDATE buildings SET level=? WHERE x=? AND y=? AND def_id=?",
                         (target_lv, x, y, def_id))
        else:
            conn.execute("INSERT INTO buildings(x,y,def_id,level,hp,built_tick)"
                         " VALUES(?,?,?,?,?,?)", (x, y, def_id, target_lv, None, tick))
        conn.commit()
        extra = {k: pay[k] - costs[k] for k in costs}
        ex = "、".join(f"{RES_ZH.get(k,k)}{v}" for k, v in extra.items() if v)
        return True, (f"⚡【{name} Lv{target_lv}】加急完成！({x},{y})\n"
                      f"　基础造价 钢{costs['steel']} 油{costs['oil']} "
                      f"资金{costs['money']}　加急费 {ex or '0'}")
    conn.execute(
        "INSERT INTO build_queue(qq,origin,x,y,def_id,target_level,start_tick,end_tick)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (qq, origin or f"web:{qq}", x, y, def_id, target_lv, tick, tick + work))
    conn.commit()
    return True, (f"✅【{name} Lv{target_lv}】已入队，{_fmt_mins(_mins(cfg, work))}后完成。\n"
                  f"　想立刻拿到？加急费 " +
                  "、".join(f"{RES_ZH.get(k,k)}{_rush_cost(cfg,costs)[k]-costs[k]}"
                            for k in costs if _rush_cost(cfg, costs)[k] > costs[k]))


def queue_snapshot(conn, cfg, qq: str) -> dict:
    """三条队列 + 剩余时间 + 加急价（供 Web 渲染）。"""
    tick = meta_get(conn, "econ_tick", int, 0)
    now = int(time.time())
    em = cfg["tick"]["economic_min"]
    wm = cfg["tick"]["war_min"]

    builds = []
    for q in conn.execute(
            "SELECT * FROM build_queue WHERE qq=? ORDER BY end_tick", (qq,)).fetchall():
        bdef = cfg["buildings"].get(q["def_id"], {})
        total = max(1, q["end_tick"] - q["start_tick"])
        left = max(0, q["end_tick"] - tick)
        costs, work = building_cost(cfg, q["def_id"], q["target_level"])
        frac = left / total
        extra = {k: int(round(costs[k] * (rush_mult(cfg) - 1) * frac)) for k in costs}
        builds.append({
            "id": q["id"], "def_id": q["def_id"], "name": bdef.get("name", q["def_id"]),
            "target_lv": q["target_level"], "x": q["x"], "y": q["y"],
            "left_ticks": left, "left_min": left * em,
            "left_text": _fmt_mins(left * em),
            "extra": extra,
            "extra_text": "、".join(f"{RES_ZH.get(k,k)}{v}" for k, v in extra.items() if v)
                          or "0",
        })

    prods = []
    for q in conn.execute(
            "SELECT pq.*, d.name dn FROM production_queue pq"
            " JOIN designs d ON d.id=pq.design_id WHERE pq.qq=? ORDER BY pq.end_tick",
            (qq,)).fetchall():
        total = max(1, q["end_tick"] - q["start_tick"])
        left = max(0, q["end_tick"] - tick)
        d = conn.execute("SELECT cost_json FROM designs WHERE id=?",
                         (q["design_id"],)).fetchone()
        cost = json.loads((d["cost_json"] if d else None) or "{}")
        base = {k: float(cost.get(k, 0)) * max(1, q["qty"])
                for k in ("steel", "oil", "money")}
        frac = left / total
        extra = {k: int(round(v * (rush_mult(cfg) - 1) * frac)) for k, v in base.items()}
        prods.append({
            "id": q["id"], "name": q["dn"], "qty": q["qty"], "x": q["x"], "y": q["y"],
            "left_ticks": left, "left_text": _fmt_mins(left * em),
            "extra": extra,
            "extra_text": "、".join(f"{RES_ZH.get(k,k)}{v}" for k, v in extra.items() if v)
                          or "0",
        })

    res = []
    for r in conn.execute(
            "SELECT * FROM research_queue WHERE qq=? ORDER BY end_ts", (qq,)).fetchall():
        left_s = max(0, int(r["end_ts"]) - now)
        cls = r["unit_type"]
        try:
            cname = pools.mod_data()["classes"][cls]["name"]
        except Exception:
            cname = cls
        res.append({
            "id": r["id"], "cls": cls, "cls_name": cname, "tier": r["tier"],
            "mode": r["mode"], "left_sec": left_s, "left_text": _fmt_mins(left_s // 60),
        })
    return {"ok": True, "builds": builds, "productions": prods, "researches": res,
            "rush_enabled": rush_enabled(cfg), "rush_mult": rush_mult(cfg),
            "econ_min": em}


def rush_queue(conn, cfg, qq: str, kind: str, qid: int):
    """花额外资源把已排队的建造/生产立刻完成。返回 (ok, text)。"""
    if not rush_enabled(cfg):
        return False, "❌ 本服未开启加急"
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return False, "❌ 先 /nw注册"
    tick = meta_get(conn, "econ_tick", int, 0)
    if kind == "build":
        q = conn.execute("SELECT * FROM build_queue WHERE id=? AND qq=?",
                         (qid, qq)).fetchone()
        if not q:
            return False, f"❌ 找不到你的建造队列 #{qid}"
        total = max(1, q["end_tick"] - q["start_tick"])
        left = max(0, q["end_tick"] - tick)
        costs, _ = building_cost(cfg, q["def_id"], q["target_level"])
        extra = {k: int(round(costs[k] * (rush_mult(cfg) - 1) * (left / total)))
                 for k in costs}
        missing = _short(p, extra)
        if missing:
            return False, "❌ 加急费不足：缺 " + "、".join(missing)
        _pay(conn, qq, extra)
        ex = conn.execute("SELECT level FROM buildings WHERE x=? AND y=? AND def_id=?",
                          (q["x"], q["y"], q["def_id"])).fetchone()
        if ex:
            conn.execute("UPDATE buildings SET level=? WHERE x=? AND y=? AND def_id=?",
                         (q["target_level"], q["x"], q["y"], q["def_id"]))
        else:
            conn.execute("INSERT INTO buildings(x,y,def_id,level,hp,built_tick)"
                         " VALUES(?,?,?,?,?,?)",
                         (q["x"], q["y"], q["def_id"], q["target_level"], None, tick))
        conn.execute("DELETE FROM build_queue WHERE id=?", (qid,))
        conn.commit()
        nm = cfg["buildings"][q["def_id"]]["name"]
        return True, (f"⚡【{nm} Lv{q['target_level']}】加急完成！"
                      f"（加急费 " +
                      "、".join(f"{RES_ZH.get(k,k)}{v}" for k, v in extra.items() if v) + "）")

    if kind == "production":
        q = conn.execute(
            "SELECT pq.*, d.name dn, d.tier dtier, d.stats_json, d.cost_json"
            " FROM production_queue pq JOIN designs d ON d.id=pq.design_id"
            " WHERE pq.id=? AND pq.qq=?", (qid, qq)).fetchone()
        if not q:
            return False, f"❌ 找不到你的生产队列 #{qid}"
        total = max(1, q["end_tick"] - q["start_tick"])
        left = max(0, q["end_tick"] - tick)
        cost = json.loads(q["cost_json"] or "{}")
        base = {k: float(cost.get(k, 0)) * max(1, q["qty"])
                for k in ("steel", "oil", "money")}
        extra = {k: int(round(v * (rush_mult(cfg) - 1) * (left / total)))
                 for k, v in base.items()}
        missing = _short(p, extra)
        if missing:
            return False, "❌ 加急费不足：缺 " + "、".join(missing)
        _pay(conn, qq, extra)
        stats = json.loads(q["stats_json"] or "{}")
        hp = stats.get("hp", 0)
        snap = json.dumps({"stats": stats, "cost": cost}, ensure_ascii=False)
        for _ in range(max(1, q["qty"])):
            conn.execute(
                "INSERT INTO ships(qq,fleet_id,def_id,name,tier,hp,max_hp,data_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (qq, None, str(q["design_id"]), q["dn"], q["dtier"], hp, hp, snap))
        conn.execute("DELETE FROM production_queue WHERE id=?", (qid,))
        conn.commit()
        return True, (f"⚡【{q['dn']}】×{q['qty']} 加急下水！（加急费 " +
                      "、".join(f"{RES_ZH.get(k,k)}{v}" for k, v in extra.items() if v) + "）")

    if kind == "research":
        r = conn.execute("SELECT * FROM research_queue WHERE id=? AND qq=?",
                         (qid, qq)).fetchone()
        if not r:
            return False, f"❌ 找不到你的研究队列 #{qid}"
        left_s = max(0, int(r["end_ts"]) - int(time.time()))
        total_s = max(1, int(r["end_ts"]) - int(r["start_ts"]))
        # 加急研究费按剩余比例收科研点（研究费用在 pools.mod_data()['research_cost']）
        rc = pools.mod_data().get("research_cost", {}).get(str(r["tier"]), {})
        base = float(rc.get("science", 30))
        extra = {"science": max(1, int(round(base * (rush_mult(cfg) - 1)
                                             * (left_s / total_s))))}
        missing = _short(p, extra)
        if missing:
            return False, "❌ 加急科研不足：缺 " + "、".join(missing)
        _pay(conn, qq, extra)
        conn.execute("UPDATE research_queue SET end_ts=? WHERE id=?",
                     (int(time.time()) - 1, qid))
        conn.commit()
        return True, "⚡ 研究已加急，下个结算周期立即出货"

    return False, "❌ 未知队列类型"
