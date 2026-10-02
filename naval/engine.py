"""世界 tick 引擎：经济 tick 20 分钟（产出+建造完工），战争 tick 2 分钟（P0 仅计数）"""
import asyncio
import json
import logging
import time

try:  # 容器外无头测试时 astrbot 不存在
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    logger = logging.getLogger("naval")

from . import pools, research, fleet, aiworld, combat
from .db import meta_get, meta_set

TICKS_PER_DAY = 24 * 3  # 经济 tick 20 分钟 → 每天 72 tick

# Web 版会话的 origin 前缀：这类推送没有 QQ 会话可发，改投缓冲等网页轮询
WEB_ORIGIN_PREFIX = "web:"


def _building_output(conn, cfg: dict, b, island_row):
    """建筑在当前等级的日产 dict（已乘矿床富度系数）。"""
    bdef = cfg["buildings"].get(b["def_id"])
    prod = bdef.get("production") if bdef else None
    if not prod:
        return {}
    ore = json.loads(island_row["ore_json"] or "{}")
    mult_table = cfg["ore_rich_mult"]
    out = {}
    for res, per_day in prod.items():
        mult = 1.0
        if res == "steel":
            mult = mult_table[ore.get("iron", 1) - 1]
        elif res == "oil":
            mult = mult_table[ore.get("oil", 1) - 1]
        elif res == "food":
            mult = mult_table[ore.get("fish", 1) - 1]
        out[res] = per_day * b["level"] * mult
    return out


def settle_economic(conn, cfg: dict) -> list[str]:
    """推进一个经济 tick：产出结算 + 建造完工。返回推送消息列表(origin,text)。"""
    pushes = []
    tick = meta_get(conn, "econ_tick", int, 0) + 1
    meta_set(conn, "econ_tick", tick)

    # 1) 产出（按建筑日产 /72）
    islands = {(r["x"], r["y"]): r
               for r in conn.execute("SELECT * FROM islands WHERE owner_qq IS NOT NULL")}
    acc: dict[str, dict[str, float]] = {}
    for b in conn.execute("SELECT * FROM buildings").fetchall():
        isl = islands.get((b["x"], b["y"]))
        if not isl or not isl["owner_qq"]:
            continue
        out = _building_output(conn, cfg, b, isl)
        acc.setdefault(isl["owner_qq"], {}).update(
            {k: acc.setdefault(isl["owner_qq"], {}).get(k, 0) + v for k, v in out.items()})
    for qq, gains in acc.items():
        if not gains:
            continue
        sets, vals = [], []
        for res, amount in gains.items():
            sets.append(f"{res}=COALESCE({res},0)+?")
            vals.append(round(amount / TICKS_PER_DAY, 2))
        vals.append(qq)
        conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)

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


def settle_war(conn, cfg: dict) -> list:
    """推进一个战争 tick：舰队移动 → 海盗游荡/补队 → 接敌 → 战斗结算。返回推送列表。"""
    pushes = []
    war_tick = meta_get(conn, "war_tick", int, 0) + 1
    meta_set(conn, "war_tick", war_tick)
    try:
        fleet.war_tick_move(conn, cfg)
        aiworld.war_tick_ai(conn, cfg, war_tick)
        combat.maybe_start_battles(conn, cfg, war_tick)
        pushes = combat.settle_battles(conn, cfg, war_tick)
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
