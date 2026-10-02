"""世界 tick 引擎：经济 tick 20 分钟（产出+建造完工），战争 tick 2 分钟（P0 仅计数）"""
import asyncio
import json
import logging
import time

try:  # 容器外无头测试时 astrbot 不存在
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = logging.getLogger("naval")

from . import (pools, research, fleet, aiworld, combat, mines, air, relations,
               salvage, events)
from .db import meta_get, meta_set

TICKS_PER_DAY = 24 * 3  # 经济 tick 20 分钟 → 每天 72 tick

# Web 版会话的 origin 前缀：这类推送没有 QQ 会话可发，改投缓冲等网页轮询
WEB_ORIGIN_PREFIX = "web:"


def morale_factor(morale) -> float:
    """§18.3 民心系数 mF = 0.6 + 0.4·(民心/100)。民心 0 仍有 60% 产出。"""
    try:
        m = max(0.0, min(100.0, float(morale if morale is not None else 60)))
    except (TypeError, ValueError):
        m = 60.0
    return 0.6 + 0.4 * (m / 100.0)


def control_factor(control) -> float:
    """§18.3 控制系数 cF = 0.2 + 0.8·(控制度/100)。刚占岛（20）时 0.36。"""
    try:
        c = max(0.0, min(100.0, float(control if control is not None else 100)))
    except (TypeError, ValueError):
        c = 100.0
    return 0.2 + 0.8 * (c / 100.0)


def dev_factor(dev_level) -> float:
    """§18.3 发展系数 dF = 0.5 + 0.05·D。D1=0.55，D10=1.0。"""
    try:
        d = max(1, min(10, int(dev_level or 1)))
    except (TypeError, ValueError):
        d = 1
    return 0.5 + 0.05 * d


def tax_table(cfg: dict) -> dict:
    return (cfg.get("tax") or {}).get("bills") or {}


def tax_bill(cfg: dict, rate) -> dict:
    """取最接近的税率法案档（§18.6）。"""
    bills = tax_table(cfg)
    if not bills:
        return {"money_mult": 1.0, "morale_per_day": 0.0, "name": "5%"}
    try:
        r = int(rate)
    except (TypeError, ValueError):
        r = 5
    keys = sorted((int(k) for k in bills), key=lambda v: abs(v - r))
    return bills.get(str(keys[0])) or bills[str(keys[0])]


def connected_islands(conn, cfg: dict, qq: str) -> set:
    """§11 航线连通：从首都出发，经"己方 + 盟友"岛屿构成的港链可达的岛。

    返回 {(x,y), ...}；不在其中即为"飞地"，产出 ×0.5（§18.3 的 rtF）。
    """
    rcfg = cfg.get("route") or {}
    link = int(rcfg.get("link_distance", 12))
    owners = [qq]
    if rcfg.get("ally_counts", True):
        try:
            owners += relations.allies_of(conn, cfg, qq)
        except Exception:
            logger.exception("[海战模拟器] 求同盟列表异常（港链回退为只算自己）")
    p = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return set()
    pts = set()
    for r in conn.execute("SELECT x,y,owner_qq FROM islands WHERE owner_qq IS NOT NULL"):
        if r["owner_qq"] in owners:
            pts.add((r["x"], r["y"]))
    connected = set()
    frontier = [(p["capital_x"], p["capital_y"])]
    while frontier:
        cx, cy = frontier.pop()
        for (x, y) in pts:
            if (x, y) in connected:
                continue
            if max(abs(x - cx), abs(y - cy)) <= link:
                connected.add((x, y))
                frontier.append((x, y))
    return connected


def route_factor(conn, cfg: dict, qq: str, x: int, y: int, cache: dict = None) -> float:
    """§18.3 航线系数 rtF：连通 1.0，飞地 0.5。"""
    rcfg = cfg.get("route") or {}
    if cache is not None and qq in cache:
        conn_set = cache[qq]
    else:
        conn_set = connected_islands(conn, cfg, qq)
        if cache is not None:
            cache[qq] = conn_set
    if not conn_set:
        return 1.0        # 没有任何己方岛时不做惩罚（例如刚注册）
    return 1.0 if (x, y) in conn_set else float(rcfg.get("disconnected_mult", 0.5))


def island_power(conn, cfg: dict, x: int, y: int) -> dict:
    """§18.5 电力网（每岛一张网）。

    发电站每级供 10 电、自耗 3 油/日；耗电建筑按建筑表的 power 吃电。
    供电不足时按 §18.5 的拉闸优先级依次停：
        装饰/银行(1) → 工厂(2，半产) → 雷达/船厂(3) → 炼铝/研究(4，最后停)
    返回 {'supply','demand','shed': set(building_id), 'half': set(building_id), 'oil'}
    """
    supply = demand = oil = 0.0
    items = []
    for b in conn.execute("SELECT * FROM buildings WHERE x=? AND y=?", (x, y)).fetchall():
        bdef = cfg["buildings"].get(b["def_id"]) or {}
        out = float(bdef.get("power_out", 0) or 0)
        if out:
            supply += out * int(b["level"] or 1)
            oil += float(bdef.get("oil_cost", 0) or 0) * int(b["level"] or 1)
        p = float(bdef.get("power", 0) or 0)
        if p > 0:
            use = p * int(b["level"] or 1)
            demand += use
            items.append({"id": b["id"], "use": use,
                          "shed": int(bdef.get("shed", 3)),
                          "half": bool(bdef.get("half_when_shed"))})
    shed, half = set(), set()
    if demand > supply:
        short = demand - supply
        # 先停 shed 值小的（优先被拉闸）
        for it in sorted(items, key=lambda v: v["shed"]):
            if short <= 0:
                break
            if it["half"]:
                # 工厂：半产，等效只吃一半电
                saved = it["use"] * 0.5
                half.add(it["id"])
            else:
                saved = it["use"]
                shed.add(it["id"])
            short -= saved
        if short > 0:
            # 还差电：把半产的也彻底拉掉
            for it in sorted(items, key=lambda v: v["shed"]):
                if short <= 0:
                    break
                if it["id"] in half:
                    half.discard(it["id"])
                    shed.add(it["id"])
                    short -= it["use"] * 0.5
    return {"supply": supply, "demand": demand, "shed": shed, "half": half,
            "oil": oil}


def power_factor(conn, cfg: dict, building_id: int, pw: dict) -> float:
    """§18.3 电力系数 pF：通电=1；断电=0；工厂断电=0.5。"""
    if building_id in pw.get("shed", ()):
        return 0.0
    if building_id in pw.get("half", ()):
        return 0.5
    return 1.0


def _building_output(conn, cfg: dict, b, island_row, rt: float = 1.0,
                     pf: float = 1.0):
    """建筑在当前等级的日产 dict。

    §18.3：Y = 基础日产 × 建筑等级 L × 矿床系数 r × 发展系数 dF
              × 控制系数 cF × 民心系数 mF
    （航线/电力/事件系数尚未实现）
    """
    bdef = cfg["buildings"].get(b["def_id"])
    prod = bdef.get("production") if bdef else None
    if not prod:
        return {}
    ore = json.loads(island_row["ore_json"] or "{}")
    mult_table = cfg["ore_rich_mult"]
    # §18.3 的公共系数（含航线系数 rtF 与电力系数 pF）
    common = (dev_factor(island_row["dev_level"])
              * control_factor(island_row["control"])
              * morale_factor(island_row["morale"])
              * float(rt) * float(pf))
    out = {}
    for res, per_day in prod.items():
        mult = 1.0
        if res == "steel":
            mult = mult_table[ore.get("iron", 1) - 1]
        elif res == "oil":
            mult = mult_table[ore.get("oil", 1) - 1]
        elif res == "food":
            mult = mult_table[ore.get("fish", 1) - 1]
        out[res] = per_day * b["level"] * mult * common
    return out


def admin_capacity(conn, cfg: dict, qq: str) -> float:
    """§14.1 行政容量 = 基础容量 + Σ总督府等级。"""
    base = float((cfg.get("admin") or {}).get("base_capacity", 4))
    r = conn.execute(
        "SELECT COALESCE(SUM(b.level),0) s FROM buildings b"
        " JOIN islands i ON i.x=b.x AND i.y=b.y"
        " WHERE i.owner_qq=? AND b.def_id='governor'", (qq,)).fetchone()
    return base + float(r["s"] or 0)


def admin_usage(conn, cfg: dict, qq: str) -> float:
    """§14.1 行政占用 = Σ(岛发展等级 D + 2)。"""
    base = float((cfg.get("admin") or {}).get("island_cost_base", 2))
    r = conn.execute(
        "SELECT COALESCE(SUM(dev_level),0) s, COUNT(*) c FROM islands"
        " WHERE owner_qq=?", (qq,)).fetchone()
    return float(r["s"] or 0) + base * float(r["c"] or 0)


def corruption_rate(conn, cfg: dict, qq: str) -> float:
    """§18.3 腐败率 k = clamp((U−C)/C, 0, 0.9)。资金/食物按 (1−k) 折损。"""
    acfg = cfg.get("admin") or {}
    cap = admin_capacity(conn, cfg, qq)
    use = admin_usage(conn, cfg, qq)
    if use <= 0:
        return 0.0
    if cap <= 0:
        return float(acfg.get("max_corruption", 0.9))
    return max(0.0, min(float(acfg.get("max_corruption", 0.9)), (use - cap) / cap))


def is_over_admin(conn, cfg: dict, qq: str) -> bool:
    return admin_usage(conn, cfg, qq) > admin_capacity(conn, cfg, qq)


def _player_tax(conn, qq: str) -> int:
    r = conn.execute("SELECT tax_rate FROM players WHERE qq=?", (qq,)).fetchone()
    try:
        return int(r["tax_rate"]) if r and r["tax_rate"] is not None else 5
    except (KeyError, IndexError):
        return 5


def settle_morale(conn, cfg: dict) -> list:
    """§18.6 税率法案的民心效果（每日一次）。

    民心没有免费的自然恢复——想回升只能降到 0% 休养生息（+1/日）。
    这是"压榨 vs 治理"取舍的全部来源。
    """
    bills = tax_table(cfg)
    if not bills:
        return []
    tick = meta_get(conn, "econ_tick", int, 0)
    if tick <= 0 or tick % TICKS_PER_DAY != 0:
        return []
    notes = []
    for p in conn.execute(
            "SELECT qq, name, tax_rate, morale FROM players").fetchall():
        bill = tax_bill(cfg, p["tax_rate"])
        delta = float(bill.get("morale_per_day", 0) or 0)
        if delta == 0:
            continue
        before = float(p["morale"] or 0)
        after = max(0.0, min(100.0, before + delta))
        conn.execute("UPDATE players SET morale=? WHERE qq=?", (after, p["qq"]))
        for isl in conn.execute("SELECT x,y FROM islands WHERE owner_qq=?",
                                (p["qq"],)).fetchall():
            conn.execute(
                "UPDATE islands SET morale=MAX(0,MIN(100,COALESCE(morale,60)+?))"
                " WHERE x=? AND y=?", (delta, isl["x"], isl["y"]))
        if delta < 0 and before >= 30 > after:
            notes.append((p["qq"],
                          f"⚠️ 民心动摇：{before:.0f} → {after:.0f}，已跌破 30！\n"
                          f"当前税率【{bill.get('name', '')}】（民心 {delta:+.0f}/日）。\n"
                          f"§14.2：民心/控制度低于 30 会触发叛乱检定，建议降到 0% 休养生息。"))
    conn.commit()
    return notes


def settle_economic(conn, cfg: dict) -> list[str]:
    """推进一个经济 tick：产出结算 + 建造完工。返回推送消息列表(origin,text)。"""
    pushes = []
    tick = meta_get(conn, "econ_tick", int, 0) + 1
    meta_set(conn, "econ_tick", tick)

    # 1) 产出（按建筑日产 /72）
    islands = {(r["x"], r["y"]): r
               for r in conn.execute("SELECT * FROM islands WHERE owner_qq IS NOT NULL")}
    acc: dict[str, dict[str, float]] = {}
    rt_cache: dict = {}          # §18.3 航线系数：按岛主缓存连通集
    power_cache: dict = {}       # §18.5 电力网：按岛屿缓存
    oil_burn: dict[str, float] = {}   # 发电站自耗油
    for b in conn.execute("SELECT * FROM buildings").fetchall():
        isl = islands.get((b["x"], b["y"]))
        if not isl or not isl["owner_qq"]:
            continue
        key = (b["x"], b["y"])
        if key not in power_cache:
            power_cache[key] = island_power(conn, cfg, b["x"], b["y"])
            # 发电站自耗油按岛累计一次——不能放在循环里，否则同一岛的
            # 油耗会被"岛上建筑数"重复计入
            if power_cache[key].get("oil"):
                oil_burn[isl["owner_qq"]] = (oil_burn.get(isl["owner_qq"], 0.0)
                                             + power_cache[key]["oil"])
        pw = power_cache[key]
        rt = route_factor(conn, cfg, isl["owner_qq"], b["x"], b["y"], rt_cache)
        pf = power_factor(conn, cfg, b["id"], pw)
        out = _building_output(conn, cfg, b, isl, rt, pf)
        acc.setdefault(isl["owner_qq"], {}).update(
            {k: acc.setdefault(isl["owner_qq"], {}).get(k, 0) + v for k, v in out.items()})
    for qq, gains in acc.items():
        if not gains:
            continue
        # §18.6 税率法案：只放大资金，民心按法案逐日变化
        bill = tax_bill(cfg, _player_tax(conn, qq))
        if "money" in gains:
            gains["money"] *= float(bill.get("money_mult", 1.0))
        # §18.3 腐败率：资金/食物再 ×(1−k)（§14.1 超行政力）
        k = corruption_rate(conn, cfg, qq)
        if k > 0:
            for res in ("money", "food"):
                if res in gains:
                    gains[res] *= (1.0 - k)
        # §18.3 事件系数 eF：叠加当前生效的随机事件（§8）
        for res in list(gains.keys()):
            ef = events.event_factor(conn, cfg, qq, res, tick)
            if ef != 1.0:
                gains[res] *= ef
        sets, vals = [], []
        for res, amount in gains.items():
            sets.append(f"{res}=COALESCE({res},0)+?")
            vals.append(round(amount / TICKS_PER_DAY, 2))
        vals.append(qq)
        conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)

    # §18.5 发电站自耗油（每级 3 油/日）
    for qq, burn in oil_burn.items():
        if burn <= 0:
            continue
        conn.execute("UPDATE players SET oil=MAX(0,COALESCE(oil,0)-?) WHERE qq=?",
                     (round(burn / TICKS_PER_DAY, 3), qq))

    # 2) 建造完工
    done = conn.execute("SELECT * FROM build_queue WHERE end_tick<=?", (tick,)).fetchall()
    for q in done:
        existing = conn.execute("SELECT id,level FROM buildings WHERE x=? AND y=? AND def_id=?",
                                (q["x"], q["y"], q["def_id"])).fetchone()
        if existing:
            conn.execute("UPDATE buildings SET level=? WHERE id=?",
                         (q["target_level"], existing["id"]))
        else:
            conn.execute("INSERT INTO buildings(x,y,def_id,level,hp,built_tick) VALUES(?,?,?,?,?,?)",
                         (q["x"], q["y"], q["def_id"], q["target_level"], None, tick))
        bname = cfg["buildings"][q["def_id"]]["name"]
        pushes.append((q["origin"], f"✅【{bname} Lv{q['target_level']}】建造完成！({q['x']},{q['y']})"))
        conn.execute("DELETE FROM build_queue WHERE id=?", (q["id"],))

    # 3) 船坞生产完工：按 qty 生成舰船实例
    done = conn.execute(
        "SELECT pq.*, d.name dn, d.tier dtier, d.stats_json, d.cost_json "
        "FROM production_queue pq JOIN designs d ON d.id=pq.design_id "
        "WHERE pq.end_tick<=?", (tick,)).fetchall()
    for q in done:
        stats = json.loads(q["stats_json"] or "{}")
        cost = json.loads(q["cost_json"] or "{}")
        hp = stats.get("hp", 0)
        snap = json.dumps({"stats": stats, "cost": cost}, ensure_ascii=False)
        for _ in range(max(1, q["qty"])):
            conn.execute(
                "INSERT INTO ships(qq,fleet_id,def_id,name,tier,hp,max_hp,data_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (q["qq"], None, str(q["design_id"]), q["dn"], q["dtier"], hp, hp, snap))
        pushes.append((q["origin"],
                       f"🚢【{q['dn']}】×{q['qty']} 建造完成！已入港 ({q['x']},{q['y']})，"
                       f"后续可编组舰队出战。"))
        conn.execute("DELETE FROM production_queue WHERE id=?", (q["id"],))

    # 4) §18.7 商港路线收入（受安全度影响，破交/封锁会压低）
    settle_trade(conn, cfg)
    # 5) §25.3 恶名自然衰减
    settle_infamy(conn, cfg)
    # 6) §14.2 岛屿控制度自然增长
    settle_control(conn, cfg)
    # 7) §26.1 通缉热度自然衰减（2/日）
    settle_heat(conn, cfg)
    # 8) §18.6 税率法案的民心效果（每日一次）
    settle_morale(conn, cfg)
    # 9) §6.2 条约：到期清理 + 附庸上贡
    try:
        from . import diplomacy as _dip
        _dip.expire_treaties(conn, cfg)
        _dip.tribute_tick(conn, cfg, pushes)
    except Exception:
        logger.exception("[海战模拟器] 条约结算异常")
    # 10) §8 随机事件：检定 + 过期清理（返回的推送由调用方发出）
    try:
        players = [dict(r) for r in conn.execute(
            "SELECT qq,capital_x,capital_y FROM players").fetchall()]
        war_tick_now = meta_get(conn, "war_tick", int, 0)
        events.expire_events(conn, tick, war_tick_now)
        for qq, text in events.roll_event(conn, cfg, tick, war_tick_now, players):
            if qq:
                pushes.append((resolve_origin(conn, qq), text))
            else:
                for p in players:
                    pushes.append((resolve_origin(conn, p["qq"]), text))
    except Exception:
        logger.exception("[海战模拟器] 随机事件结算异常")
    conn.commit()
    return pushes


def settle_research(conn) -> list:
    """扫描到期普通研究（真实时间戳，每 10 秒轮询），抽卡并返回推送列表。"""
    now = int(time.time())
    due = conn.execute("SELECT * FROM research_queue WHERE end_ts<=?", (now,)).fetchall()
    if not due:
        return []
    mc = pools.mod_data()
    pushes = []
    for q in due:
        r = research.roll_gacha(conn, mc, q["qq"], q["unit_type"], q["tier"])
        conn.execute("DELETE FROM research_queue WHERE id=?", (q["id"],))
        pushes.append((q["origin"], research.gacha_text(mc, q["unit_type"], q["tier"], r)))
    conn.commit()
    return pushes


def settle_infamy(conn, cfg: dict) -> None:
    """§25.3 恶名自然衰减 1/日。

    注意：不能用 `CAST(infamy - 1/72 AS INTEGER)` —— SQLite 的 CAST 是截断取整，
    会把每次衰减放大成整整 1 点（变成 72/日）。这里改为只在跨日的那个经济 tick 整点扣。
    """
    decay = int((cfg.get("enforcer") or {}).get("infamy_decay_per_day", 1))
    if decay <= 0:
        return
    tick = meta_get(conn, "econ_tick", int, 0)
    if tick <= 0 or tick % TICKS_PER_DAY != 0:
        return
    conn.execute(
        "UPDATE players SET infamy=MAX(0,COALESCE(infamy,0)-?) WHERE COALESCE(infamy,0)>0",
        (decay,))
    conn.commit()


def check_enforcers(conn, cfg: dict) -> list:
    """§25.3：恶名达标刷执法者；恶名跌回 clear 以下撤走追击。返回提示列表。"""
    ecfg = cfg.get("enforcer") or {}
    notes = []
    war_tick = meta_get(conn, "war_tick", int, 0)
    trigger = int(ecfg.get("trigger_infamy", 10))
    clear = int(ecfg.get("clear_infamy", 5))

    for p in conn.execute(
            "SELECT qq,name,infamy,capital_x,capital_y FROM players").fetchall():
        if p["capital_x"] is None:
            continue
        infamy = int(p["infamy"] or 0)
        batch = None
        for r in conn.execute("SELECT * FROM ai_fleets WHERE faction=?",
                              (aiworld.ENFORCER,)).fetchall():
            m = json.loads(r["mission"] or "{}")
            if m.get("target_qq") == p["qq"] and json.loads(r["comp_json"] or "[]"):
                batch = r
                break
        if batch is not None and infamy < clear:
            conn.execute("UPDATE ai_fleets SET comp_json='[]' WHERE id=?", (batch["id"],))
            notes.append((f"🕊️ 你的恶名已降至 {infamy}，跨国执法舰队撤离了海域。", p["qq"]))
            continue
        if infamy >= trigger and batch is None:
            lv = aiworld.ensure_enforcer(conn, cfg, p["qq"], p["capital_x"],
                                         p["capital_y"], war_tick)
            if lv:
                notes.append((f"🚨 恶名 {infamy} 触发了跨国联合执法舰队 L{lv}！\n"
                              f"它正从外海扑向你的舰队与港口。\n"
                              f"（§25.3：击沉其舰船可减恶名，全歼该批次则恶名清零；"
                              f"恶名自然衰减 {ecfg.get('infamy_decay_per_day', 1)}/日）",
                              p["qq"]))
    conn.commit()
    return notes


def settle_rebellion_tick(conn, cfg: dict, war_tick: int) -> list:
    """§26.3 叛乱检定只在跨日的经济 tick 触发（每日一次）。"""
    rcfg = cfg.get("rebellion") or {}
    per_day = int(rcfg.get("check_per_day", 1))
    if per_day <= 0:
        return []
    tick = meta_get(conn, "econ_tick", int, 0)
    step = max(1, TICKS_PER_DAY // per_day)
    if tick <= 0 or tick % step != 0:
        return []
    return aiworld.settle_rebellion(conn, cfg, war_tick)


def settle_contracts(conn, cfg: dict, war_tick: int) -> list:
    """§26.2 雇佣合同到期：舰队原地解散变中立。返回 [(qq, text)]。"""
    notes = []
    rows = conn.execute(
        "SELECT * FROM contracts WHERE status='active' AND expire_tick<=?",
        (war_tick,)).fetchall()
    for r in rows:
        left = conn.execute("SELECT COUNT(*) c FROM ships WHERE fleet_id=?",
                            (r["fleet_id"],)).fetchone()["c"]
        conn.execute("DELETE FROM ships WHERE fleet_id=?", (r["fleet_id"],))
        conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (r["fleet_id"],))
        conn.execute("UPDATE contracts SET status='expired' WHERE id=?", (r["id"],))
        notes.append((r["qq"],
                      f"⏳ 雇佣合同到期：【{r['company']}】L{r['level']} 已按约解散，"
                      f"{left} 艘舰船归还中立（§26.2 合同到期原地变中立）。\n"
                      f"想续约请靠近雇佣军团再 /nw雇佣。"))
    if rows:
        conn.commit()
    return notes


def settle_heat(conn, cfg: dict) -> None:
    """§26.1 通缉热度 2/日衰减（不袭击帝国资产时），只在跨日整点结算。"""
    d = cfg.get("diplomacy") or {}
    decay = int(d.get("heat_decay_per_day", 2))
    if decay <= 0:
        return
    tick = meta_get(conn, "econ_tick", int, 0)
    if tick <= 0 or tick % TICKS_PER_DAY != 0:
        return
    conn.execute("UPDATE players SET wanted_heat=MAX(0,COALESCE(wanted_heat,0)-?)"
                 " WHERE COALESCE(wanted_heat,0)>0", (decay,))
    conn.commit()


def settle_control(conn, cfg: dict) -> None:
    """§14.2 控制度：每日自然 +3（只在跨日的经济 tick 整点结算）。

    与恶名同理，不能用 CAST 按 tick 摊薄 —— CAST 截断会让每次涨整整 1 点（=72/日）。
    """
    gain = int((cfg.get("landing") or {}).get("control_gain_per_day", 3))
    if gain <= 0:
        return
    tick = meta_get(conn, "econ_tick", int, 0)
    if tick <= 0 or tick % TICKS_PER_DAY != 0:
        return
    # §14.1：超行政力时控制度增长减半（按岛主逐个判断）
    half_over = bool((cfg.get("admin") or {}).get("over_limit_control_half", True))
    owners = [r["owner_qq"] for r in conn.execute(
        "SELECT DISTINCT owner_qq FROM islands WHERE owner_qq IS NOT NULL").fetchall()]
    for owner in owners:
        g = gain / 2.0 if (half_over and is_over_admin(conn, cfg, owner)) else float(gain)
        conn.execute("UPDATE islands SET control=MIN(100,COALESCE(control,0)+?)"
                     " WHERE owner_qq=? AND COALESCE(control,0)<100", (g, owner))
    conn.commit()


def _is_player_owner(v) -> bool:
    """岛屿归属是否是玩家（正规军用 regular:N，中立为 NULL）。"""
    return bool(v) and not str(v).startswith("regular")


def resolve_origin(conn, qq: str) -> str:
    """给某玩家找一个可推送的会话 origin：优先他最近下单的会话，否则回落到 Web。"""
    o = fleet.latest_origin(conn, qq)
    return o or f"{WEB_ORIGIN_PREFIX}{qq}"


def detect_island_changes(conn, cfg: dict, prev: dict) -> list:
    """对比战前战后的岛屿归属，产出易主通知。返回 [(qq, text)]。

    §11/§14.2：岛屿易主必须有反馈，否则玩家不知道自己的岛被正规军抢了。
    """
    notes = []
    cur = {f"{r['x']},{r['y']}": r["owner_qq"] for r in conn.execute(
        "SELECT x,y,owner_qq FROM islands").fetchall()}
    for key, owner in cur.items():
        old = prev.get(key)
        if old == owner:
            continue
        x, y = key.split(",")
        if _is_player_owner(owner):
            notes.append((owner, f"🏴 你的舰队拿下了 ({x},{y})！控制度将从 20 起爬升，"
                                 f"记得留兵守过渡期（§14.2）。"))
        if _is_player_owner(old) and old != owner:
            who = "正规军" if str(owner or "").startswith("regular") else (owner or "中立势力")
            notes.append((old, f"⚠️ 你的岛屿 ({x},{y}) 被【{who}】夺走了！\n"
                               f"派舰队过去清海后 /nw登陆 {x},{y} 可以夺回。"))
    return notes


def settle_trade(conn, cfg: dict) -> float:
    """§18.7 商港路线收入：Σ(60·L·安全度)，按日计、经济 tick 入账 1/72。

    L 取岛屿发展等级；安全度 = players.route_security / 100（破交/封锁压低，护航恢复）。
    """
    tcfg = cfg.get("trade") or {}
    base = float(tcfg.get("base_income_per_day", 60))
    rows = conn.execute(
        "SELECT i.owner_qq AS qq, SUM(i.dev_level) AS lv,"
        "       COALESCE(p.route_security, 100) AS sec"
        " FROM islands i JOIN players p ON p.qq = i.owner_qq"
        " WHERE i.owner_qq IS NOT NULL GROUP BY i.owner_qq").fetchall()
    total = 0.0
    for r in rows:
        # 注意：安全度可能是 0，不能用 `or 100`（0 是 falsy，会被误当成未设置）
        raw_sec = r["sec"]
        sec = 100.0 if raw_sec is None else float(raw_sec)
        sec = max(0.0, min(100.0, sec))
        gain = base * float(r["lv"] or 1) * (sec / 100.0) / TICKS_PER_DAY
        # §18.6 税率法案的资金倍率：商港收入是本作主要的资金来源，必须吃这个倍率
        gain *= float(tax_bill(cfg, _player_tax(conn, r["qq"])).get("money_mult", 1.0))
        # §18.3 腐败率同样作用于资金
        gain *= (1.0 - corruption_rate(conn, cfg, r["qq"]))
        if gain <= 0:
            continue
        conn.execute("UPDATE players SET money=COALESCE(money,0)+? WHERE qq=?",
                     (round(gain, 2), r["qq"]))
        total += gain
    if rows:
        conn.commit()
    return total


def settle_stances(conn, cfg: dict) -> list:
    """§4 阵位的持续效果：护航恢复本方安全度、封锁压低目标岛主安全度。"""
    tcfg = cfg.get("trade") or {}
    gain = float(tcfg.get("escort_security_gain", 8))
    loss = float(tcfg.get("blockade_security_loss", 6))
    smax = float(tcfg.get("security_max", 100))
    for f in conn.execute("SELECT * FROM fleets").fetchall():
        m = json.loads(f["mission"] or "{}")
        mt = m.get("type")
        if mt == "escort":
            conn.execute(
                "UPDATE players SET route_security=MIN(?,COALESCE(route_security,100)+?)"
                " WHERE qq=?", (smax, gain, f["qq"]))
        elif mt == "blockade":
            tx, ty = m.get("target_x"), m.get("target_y")
            if tx is None:
                continue
            isl = conn.execute("SELECT owner_qq FROM islands WHERE x=? AND y=?",
                               (tx, ty)).fetchone()
            if isl and isl["owner_qq"]:
                conn.execute(
                    "UPDATE players SET route_security=MAX(0,COALESCE(route_security,100)-?)"
                    " WHERE qq=?", (loss, isl["owner_qq"]))
    conn.commit()
    return []


def settle_war(conn, cfg: dict) -> list:
    """推进一个战争 tick：舰队移动 → 商船航线 → 海盗游荡/补队 → 阵位效果 → 接敌 → 战斗。"""
    pushes = []
    war_tick = meta_get(conn, "war_tick", int, 0) + 1
    meta_set(conn, "war_tick", war_tick)
    try:
        # 战前快照：用于战后比对岛屿易主（§11/§14.2 反馈回路）
        prev_islands = {f"{r['x']},{r['y']}": r["owner_qq"] for r in conn.execute(
            "SELECT x,y,owner_qq FROM islands").fetchall()}

        fleet.war_tick_move(conn, cfg)
        # §4 水雷：玩家舰队走位后检查触发（己方雷场不触发）
        for f in conn.execute("SELECT * FROM fleets").fetchall():
            n = fleet.ship_count(conn, f["id"])
            if n <= 0:
                continue
            dmg, txt = mines.check_trigger(conn, cfg, f["qq"], f["x"], f["y"], n)
            if dmg <= 0:
                continue
            detail = mines.damage_player_fleet(conn, cfg, f["id"], dmg)
            pushes.append((resolve_origin(conn, f["qq"]),
                           f"💥 你的舰队【{f['name']}】在 ({f['x']},{f['y']}) 触雷！\n"
                           f"{txt}\n{detail}"))
        # §19.15 规则 7：每战争 tick 掷一次天气/昼夜；规则 5：修补甲板
        air.roll_weather(conn, cfg, war_tick)
        air.repair_decks(conn, cfg, war_tick)
        # §19.15 规则 1：舰载机整备（恢复疲劳）
        air.recover_fatigue(conn, cfg, war_tick)
        # §26.4/§11 残骸过期清理
        salvage.war_tick_wrecks(conn, cfg, war_tick)
        aiworld.war_tick_merchants(conn, cfg, war_tick)
        # AI 撞雷（布雷方的战果推送）
        for qq, text in mines.war_tick_ai_mines(conn, cfg):
            pushes.append((resolve_origin(conn, qq), text))
        aiworld.war_tick_patrols(conn, cfg, war_tick)
        aiworld.war_tick_merc(conn, cfg, war_tick)
        aiworld.war_tick_rebels(conn, cfg, war_tick)
        aiworld.war_tick_yunmo(conn, cfg, war_tick)
        aiworld.war_tick_enforcer(conn, cfg, war_tick)
        aiworld.war_tick_regular(conn, cfg, war_tick)
        aiworld.war_tick_ai(conn, cfg, war_tick)
        settle_stances(conn, cfg)
        combat.maybe_start_battles(conn, cfg, war_tick)
        pushes = combat.settle_battles(conn, cfg, war_tick)
        # §25.3 恶名达标则刷执法者；跌回阈值以下则撤走
        for text, qq in check_enforcers(conn, cfg):
            pushes.append((resolve_origin(conn, qq), text))
        # §11/§14.2 岛屿易主通知（丢岛 / 得岛）
        for qq, text in detect_island_changes(conn, cfg, prev_islands):
            pushes.append((resolve_origin(conn, qq), text))
        # §26.2 雇佣合同到期解散
        for qq, text in settle_contracts(conn, cfg, war_tick):
            pushes.append((resolve_origin(conn, qq), text))
        # §26.3 叛乱检定（每日一次，控制度<30 的岛）
        for qq, text in settle_rebellion_tick(conn, cfg, war_tick):
            pushes.append((resolve_origin(conn, qq), text))
        conn.commit()
    except Exception:
        logger.exception("[海战模拟器] 战争 tick 结算异常")
    return pushes


class GameEngine:
    def __init__(self, conn, cfg: dict, context, push_buffer=None):
        self.conn, self.cfg, self.context = conn, cfg, context
        self.push_buffer = push_buffer   # Web 用户推送缓冲（可为 None）
        self._task = None

    def start(self):
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[海战模拟器] tick 引擎已启动")

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _push(self, origin: str, text: str):
        if not origin:
            return
        # Web 会话没有 QQ 可发：投进缓冲，等网页轮询取走
        if str(origin).startswith(WEB_ORIGIN_PREFIX):
            if self.push_buffer is not None:
                self.push_buffer.add(origin, text)
            return
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain
            await self.context.send_message(origin, MessageChain([Plain(text)]))
        except Exception:
            logger.exception("[海战模拟器] 推送失败")

    async def _loop(self):
        econ_min = self.cfg["tick"]["economic_min"]
        war_min = self.cfg["tick"]["war_min"]
        interval = self.cfg["tick"]["loop_seconds"]
        while True:
            try:
                # 研究队列按真实时间到期（与经济 tick 解耦，约 3.5~5 分钟）
                for origin, text in settle_research(self.conn):
                    await self._push(origin, text)
                now = time.time()
                last_econ = meta_get(self.conn, "last_econ_ts", float, 0)
                last_war = meta_get(self.conn, "last_war_ts", float, 0)
                if last_econ == 0:
                    meta_set(self.conn, "last_econ_ts", now)
                elif now - last_econ >= econ_min * 60:
                    meta_set(self.conn, "last_econ_ts", now)
                    for origin, text in settle_economic(self.conn, self.cfg):
                        await self._push(origin, text)
                # 战争 tick：舰队移动/海盗游荡/接敌/战斗结算
                if last_war == 0:
                    meta_set(self.conn, "last_war_ts", now)
                elif now - last_war >= war_min * 60:
                    meta_set(self.conn, "last_war_ts", now)
                    for origin, text in settle_war(self.conn, self.cfg):
                        await self._push(origin, text)
            except Exception:
                logger.exception("[海战模拟器] tick 结算异常")
            await asyncio.sleep(interval)
