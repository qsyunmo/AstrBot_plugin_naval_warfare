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
        return (f"{h} 小时 " + f"{mm} 分") if mm else f"{h} 小时"
    d, hh = divmod(h, 24)
    return (f"{d} 天 " + f"{hh} 小时") if hh else f"{d} 天"


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


# ------------------------------------------------ 岛屿管理（占岛后要能管起来）
def active_coord(conn, qq: str):
    """当前岛坐标。players.active_x/y 为空时回落到首都（老账号行为不变）。"""
    p = conn.execute("SELECT capital_x,capital_y,active_x,active_y FROM players"
                     " WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return None
    if p["active_x"] is not None and p["active_y"] is not None:
        # 当前岛可能已经丢了/被拆了，这时悄悄落回首都，别让玩家卡死在坏状态
        own = conn.execute("SELECT 1 FROM islands WHERE x=? AND y=? AND owner_qq=?",
                           (p["active_x"], p["active_y"], qq)).fetchone()
        if own:
            return p["active_x"], p["active_y"]
    return p["capital_x"], p["capital_y"]


def set_active(conn, qq: str, x: int, y: int) -> bool:
    """切换当前岛。必须是自己的岛。"""
    if not conn.execute("SELECT 1 FROM islands WHERE x=? AND y=? AND owner_qq=?",
                        (x, y, qq)).fetchone():
        return False
    conn.execute("UPDATE players SET active_x=?,active_y=? WHERE qq=?", (x, y, qq))
    conn.commit()
    return True


def my_islands(conn, cfg: dict, qq: str) -> list:
    """我的全部岛屿（编号从 1 开始，与 /nw岛 列表一致）。

    含：坐标/岛型/岛级/槽位/控制度/耐久/矿床/建筑明细/是否首都/是否当前岛。
    """
    ax = active_coord(conn, qq)
    p = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?",
                     (qq,)).fetchone()
    cap = (p["capital_x"], p["capital_y"]) if p else (None, None)
    rows = conn.execute(
        "SELECT * FROM islands WHERE owner_qq=? ORDER BY y,x", (qq,)).fetchall()

    out = []
    for i, isl in enumerate(rows, 1):
        tdef = cfg["island_types"].get(isl["itype"], {})
        bs = conn.execute(
            "SELECT def_id,level FROM buildings WHERE x=? AND y=?"
            " ORDER BY def_id", (isl["x"], isl["y"])).fetchall()
        slots_total = int(tdef.get("slots", 0)) + int(isl["dev_level"] or 0)
        try:
            ore = json.loads(isl["ore_json"] or "{}")
        except (ValueError, TypeError):
            ore = {}
        d_max = tdef.get("d_max", 0)
        out.append({
            "idx": i,
            "x": isl["x"], "y": isl["y"],
            "itype": isl["itype"], "type_name": tdef.get("name", isl["itype"]),
            "dev_level": isl["dev_level"], "d_max": d_max,
            "slots_used": len(bs), "slots_total": slots_total,
            "control": isl["control"], "hp": isl["hp"],
            "ore": ore,
            "buildings": [{"def_id": b["def_id"], "level": b["level"],
                           "name": (cfg["buildings"].get(b["def_id"]) or {})
                                    .get("name", b["def_id"])} for b in bs],
            "is_capital": (isl["x"], isl["y"]) == cap,
            "is_active": (isl["x"], isl["y"]) == ax,
            "can_level_up": bool(d_max and isl["dev_level"] < d_max),
        })
    return out


def parse_island_ref(conn, cfg: dict, qq: str, raw: str):
    """解析玩家给的岛指代：`#编号` / 编号 / `@编号` / "x,y"。

    用 `#` 而不是只认 `@`：Router.normalize() 会用 AT_RE 把 `@xxx` 当 QQ
    at-mention 剥掉（`@2` 会整段消失），`@` 只作为兼容写法保留。
    返回 (x, y, err)。err 非空表示解析失败。
    """
    s = str(raw or "").strip().lstrip("@#").replace("，", ",")
    if not s:
        return None, None, "空"
    mines = my_islands(conn, cfg, qq)
    if s.isdigit():
        n = int(s)
        if 1 <= n <= len(mines):
            m = mines[n - 1]
            return m["x"], m["y"], ""
        return None, None, f"没有编号 {n} 的岛（你共有 {len(mines)} 座）"
    parts = s.split(",")
    if len(parts) >= 2 and all(p.strip().lstrip("-").isdigit() for p in parts[:2]):
        return int(parts[0]), int(parts[1]), ""
    return None, None, f"看不懂的岛指代「{raw}」（用 #编号 或 x,y）"


def build_options(conn, cfg, qq: str, x: int = None, y: int = None) -> dict:
    """某岛的可建/可升级建筑清单（供 Web 图形界面渲染）。

    每项含：def_id/name/desc/level/max_lv/next_lv/cost/work/work_min/
    affordable/missing/blocked(不可建原因)/can_build/rush_cost/rush_work_min。
    x/y 省略时用**当前岛**（不再写死首都 —— 否则占来的岛没法经营）。
    """
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return {"ok": False, "reason": "尚未注册"}
    if x is None or y is None:
        x, y = active_coord(conn, qq) or (p["capital_x"], p["capital_y"])
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
        x, y = active_coord(conn, qq) or (p["capital_x"], p["capital_y"])
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


def research_options(conn, cfg, qq: str) -> dict:
    """可研究的「舰种 × 代际」清单（供 Web 图形化研究面板）。

    每项含：cls/cls_name/tier/cost_normal/cost_express/dur_sec/dur_text/
    pool_owned/pool_size/can_normal/can_express/missing_*/blocked。
    """
    mc = pools.mod_data()
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return {"ok": False, "reason": "尚未注册"}
    lab_lv, proving_lv, lx, ly = research.lab_info(conn, qq)
    slots = research.research_slots(lab_lv)
    active = research.active_research(conn, qq)
    owned = pools.owned_ids(conn, qq)
    dur = research.research_duration(cfg, mc, proving_lv)
    emult = int(mc.get("express_mult", 10))

    out = []
    for cls, cdef in mc["classes"].items():
        for tier in range(1, 6):
            rc = mc["research_cost"].get(str(tier)) or {}
            normal = {k: rc.get(k, 0) for k in
                      ("science", "money", "rare_earth", "chips")}
            express = {k: v * emult for k, v in normal.items()}
            pool = pools.pool_modules(cls, tier)
            pown = len({m["id"] for m in pool} & owned)
            blocked = ""
            if lab_lv < 1:
                blocked = "尚无研究所"
            elif lab_lv < tier:
                blocked = f"需研究所 Lv{tier}（当前 Lv{lab_lv}）"
            miss_n = _short(p, normal)
            miss_e = _short(p, express)
            out.append({
                "cls": cls, "cls_name": cdef["name"], "tier": tier,
                "tier_name": f"T{tier}",
                "cost_normal": normal, "cost_express": express,
                "express_mult": emult,
                "dur_sec": dur, "dur_text": fmt_mins(dur // 60),
                "pool_owned": pown, "pool_size": len(pool),
                "pool_pct": round(pown / len(pool) * 100) if pool else 0,
                "missing_normal": miss_n, "missing_express": miss_e,
                "blocked": blocked,
                "can_normal": (not blocked) and (not miss_n) and (active < slots),
                "can_express": (not blocked) and (not miss_e),
                "slots_full": active >= slots,
                "required": list(cdef.get("required") or []),
            })
    return {"ok": True, "lab_lv": lab_lv, "proving_lv": proving_lv,
            "slots": slots, "active": active,
            "express_mult": emult, "duration_text": fmt_mins(dur // 60),
            "tiers": out}


def do_research(conn, cfg, qq: str, origin: str, cls: str, tier: int,
                mode: str = "normal"):
    """下单研究。mode: normal（排队）| express（立即出货，花费×express_mult）。

    返回 (ok, text)。normal 模式下若研究位已满会被拒。
    """
    mc = pools.mod_data()
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return False, "❌ 先 /nw注册"
    if cls not in mc["classes"]:
        return False, "❌ 不认识该舰种"
    if not (1 <= int(tier) <= 5):
        return False, "❌ 等级应为 T1~T5"
    tier = int(tier)
    lab_lv, proving_lv, _, _ = research.lab_info(conn, qq)
    if lab_lv < 1:
        return False, "❌ 首都还没有【研究所】，先建一座"
    if lab_lv < tier:
        return False, f"❌ T{tier} 池需要研究所 Lv{tier}（当前 Lv{lab_lv}）"
    cname = mc["classes"][cls]["name"]
    rc = mc["research_cost"].get(str(tier)) or {}
    normal = {k: rc.get(k, 0) for k in ("science", "money", "rare_earth", "chips")}
    emult = int(mc.get("express_mult", 10))
    express = {k: v * emult for k, v in normal.items()}
    dur = research.research_duration(cfg, mc, proving_lv)

    if mode == "express":
        cost = express
        miss = _short(p, cost)
        if miss:
            return False, "❌ 资源不足：缺 " + "、".join(miss)
        _pay(conn, qq, cost)
        result = research.roll_gacha(conn, mc, qq, cls, tier)
        conn.commit()
        txt = research.gacha_text(mc, cls, tier, result)
        return True, f"⚡【{cname} T{tier}】快捷研究（花费×{emult}）\n{txt}"

    slots = research.research_slots(lab_lv)
    active = research.active_research(conn, qq)
    if active >= slots:
        return False, (f"❌ 研究所 Lv{lab_lv} 只有 {slots} 个并行研究位，当前已满。"
                       f"可改用快捷（花费×{emult}）立即出货")
    miss = _short(p, normal)
    if miss:
        return False, "❌ 资源不足：缺 " + "、".join(miss)
    _pay(conn, qq, normal)
    now = int(time.time())
    conn.execute(
        "INSERT INTO research_queue(qq,origin,unit_type,tier,mode,start_ts,end_ts)"
        " VALUES(?,?,?,?, 'normal',?,?)",
        (qq, origin or f"web:{qq}", cls, tier, now, now + dur))
    conn.commit()
    return True, (f"🔬【{cname} T{tier}】已下单（普通），约 {fmt_mins(dur//60)} 出蓝图，"
                  f"完成自动推送。\n　想马上拿到？用快捷（花费×{emult}）立即出货。")


def design_options(conn, cfg, qq: str, cls: str = None) -> dict:
    """图形化「组装」所需数据：舰种 / 各槽位可选模块 / 已存设计。

    「组装」在 QQ 侧是「浏览目录拿编号 → 输编号」两步，网页上直接做成
    逐槽点选 + 实时预览。这里只提供数据，预览走 preview_design()。
    """
    mc = pools.mod_data()
    p = conn.execute("SELECT qq,name FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return {"ok": False, "reason": "尚未注册"}
    if cls not in mc["classes"]:
        cls = next(iter(mc["classes"]))
    cdef = mc["classes"][cls]
    owned = pools.owned_ids(conn, qq)
    slot_names = mc["slot_names"]

    slots = []
    for sk in cdef["slots"]:
        opts = []
        for tier in range(1, 6):
            for m in sorted((x for x in pools.pool_modules(cls, tier)
                             if x["slot"] == sk),
                            key=lambda x: (pools.RARITY_ORDER.index(x["rarity"]),
                                           x["tier"])):
                info = pools.module_info(m["id"])
                if not info:
                    continue
                opts.append({
                    "id": m["id"], "name": info["name"], "tier": info["tier"],
                    "rarity": info["rarity"],
                    "rarity_name": pools.rarity_label(info["rarity"]),
                    "owned": m["id"] in owned,
                    "stats": info["stats"], "cost": info["cost"],
                })
        # 已拥有的排前面，方便直接点
        opts.sort(key=lambda o: (not o["owned"],
                                 pools.RARITY_ORDER.index(o["rarity"]), o["tier"]))
        slots.append({
            "key": sk, "name": slot_names.get(sk, sk),
            "required": sk in (cdef.get("required") or []),
            "slot_cost": (cdef.get("slot_cost") or {}).get(sk, {}),
            "options": opts,
            "owned_count": sum(1 for o in opts if o["owned"]),
        })

    designs = []
    for d in conn.execute(
            "SELECT id,name,ship_class,tier,stats_json,cost_json,work_ticks"
            " FROM designs WHERE qq=? ORDER BY id", (qq,)).fetchall():
        designs.append({
            "id": d["id"], "name": d["name"], "cls": d["ship_class"],
            "cls_name": mc["classes"].get(d["ship_class"], {}).get("name", ""),
            "tier": d["tier"],
            "stats": json.loads(d["stats_json"] or "{}"),
            "cost": json.loads(d["cost_json"] or "{}"),
            "work_ticks": d["work_ticks"],
            "work_text": fmt_mins(int(d["work_ticks"] or 0)
                                  * cfg["tick"]["economic_min"]),
        })
    yard = research.building_level(conn, *p_xy(conn, qq), "shipyard")
    return {"ok": True, "cls": cls, "cls_name": cdef["name"],
            "base_hp": cdef.get("base_hp"),
            "classes": [{"id": k, "name": v["name"]}
                        for k, v in mc["classes"].items()],
            "slots": slots, "designs": designs,
            "shipyard_lv": yard,
            "max_qty": int(mc.get("max_produce_qty", 10)),
            "tier_hp_mult": mc.get("tier_hp_mult")}


def p_xy(conn, qq: str):
    r = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?",
                     (qq,)).fetchone()
    return ((r["capital_x"], r["capital_y"]) if r else (None, None))


def preview_design(cfg, cls: str, chosen: dict) -> dict:
    """实时预览（与保存时的计算完全同源，调 pools.ship_preview）。"""
    mc = pools.mod_data()
    if cls not in mc["classes"]:
        return {"ok": False, "reason": "不认识该舰种"}
    clean = {k: v for k, v in (chosen or {}).items() if v}
    pv = pools.ship_preview(cls, clean)
    dur = int(pv["work_ticks"]) * cfg["tick"]["economic_min"]
    return {"ok": True, "tier": pv["tier"], "stats": pv["stats"],
            "cost": pv["cost"], "work_ticks": pv["work_ticks"],
            "work_text": fmt_mins(dur),
            "missing_required": [mc["slot_names"].get(s, s)
                                 for s in pv["missing_required"]],
            "stats_text": pools.stats_text(pv["stats"])}


def save_design(conn, cfg, qq: str, name: str, cls: str, chosen: dict):
    """保存设计（与 QQ 侧 /nw组装 同一套校验）。返回 (ok, text)。"""
    mc = pools.mod_data()
    p = conn.execute("SELECT qq FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return False, "❌ 先 /nw注册"
    if cls not in mc["classes"]:
        return False, "❌ 不认识该舰种"
    name = (name or "").strip()[:8]
    if not name:
        return False, "❌ 请给方案起个名字（最多 8 字）"
    cname = mc["classes"][cls]["name"]
    owned = pools.owned_ids(conn, qq)
    chosen = {k: v for k, v in (chosen or {}).items() if v}
    # 槽位/拥有校验（Web 传什么都不能绕过）
    for sk, mid in chosen.items():
        info = pools.module_info(mid)
        if not info or info["cls"] != cls:
            return False, f"❌ 模块 {mid} 不属于【{cname}】"
        if sk not in mc["classes"][cls]["slots"]:
            return False, f"❌【{mc['slot_names'].get(sk, sk)}】不是该舰种的槽位"
        if mid not in owned:
            return False, f"❌【{info['name']}】还未拥有，先研究抽蓝图"
    pv = pools.ship_preview(cls, chosen)
    if pv["missing_required"]:
        miss = "、".join(mc["slot_names"].get(s, s) for s in pv["missing_required"])
        return False, f"❌ 还缺少必选槽：{miss}"
    if conn.execute("SELECT 1 FROM designs WHERE qq=? AND name=?",
                    (qq, name)).fetchone():
        return False, f"❌ 你已有设计【{name}】，换个名字"
    conn.execute(
        "INSERT INTO designs(qq,name,ship_class,tier,modules,stats_json,cost_json,"
        "work_ticks) VALUES(?,?,?,?,?,?,?,?)",
        (qq, name, cls, pv["tier"], json.dumps(chosen, ensure_ascii=False),
         json.dumps(pv["stats"], ensure_ascii=False),
         json.dumps(pv["cost"], ensure_ascii=False), pv["work_ticks"]))
    conn.commit()
    did = conn.execute("SELECT MAX(id) m FROM designs WHERE qq=?",
                       (qq,)).fetchone()["m"]
    return True, (f"✅ 设计【{name}】已保存（{cname} T{pv['tier']}，#{did}）。"
                  f"去「🏗 建造」或 /nw生产 {name} <数量> 下船台。")


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
