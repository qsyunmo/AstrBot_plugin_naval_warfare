"""指令 handler（P0 经济建造 + P1 研究抽卡/设计器/船坞）

handler 统一签名：async def h(ctx) -> str
ctx 字段：conn, cfg, confirm(ConfirmStore), origin, qq, nick, args(list[str]), raw(str)
所有花钱/不可逆操作 → 返回报价文本并 confirm.put(action)，回复数字后经 execute_action 落地。
"""
import json
import math
import re
import time
from dataclasses import dataclass

from . import spawn, pools, research, fleet as fleetm, aiworld, combat
from .db import meta_get

PREFIX_HINT = "前缀 /nw（或 /海战）"

# 指令表：命令名 → (方法名, 是否开放, 别名, 简介)
COMMANDS = {
    "注册": ("register", True, [], "开局注册势力"),
    "帮助": ("help", True, ["菜单"], "查看指令表"),
    "我": ("status", True, ["状态", "状态卡"], "势力状态卡"),
    "资源": ("resources", True, ["仓"], "查看资源库存"),
    "岛": ("island", True, ["岛屿"], "查看岛屿详情"),
    "建造": ("build", True, ["建"], "建造/升级建筑（报价确认）"),
    "升级": ("build", True, [], "建造 指令的别名（已存在则升级）"),
    "队列": ("queue", True, ["建造队列"], "建造/研究/生产队列"),
    "排行": ("ranking", True, ["排行榜"], "势力排行榜"),
    # —— P1：研究抽卡 ——
    "研究": ("research", True, ["研"], "蓝图抽卡 <舰种> <T1~T5>"),
    "研究状态": ("research_status", True, ["研状态"], "研究队列与剩余时间"),
    "蓝图": ("blueprints", True, [], "已拥有蓝图图鉴 [舰种]"),
    "碎片": ("fragments", True, [], "碎片库存与兑换说明"),
    "碎片兑换": ("fragment_exchange", True, ["兑碎片"], "碎片定向兑换 <舰种> <T> <模块>"),
    # —— P1：设计与造舰 ——
    "设计": ("design", True, [], "进入舰船设计模式（浏览模块目录/组装方案）"),
    "退出": ("exit_design", True, [], "退出舰船设计模式"),
    "组装": ("assemble", True, [], "设计模式内组装方案 <名> <模块编号…>"),
    "设计列表": ("design_list", True, ["设计ls"], "我的舰船设计"),
    "设计查看": ("design_view", True, ["设计看"], "查看设计属性 <名>"),
    "生产": ("produce", True, ["造舰"], "船坞排产 <设计> [数量]"),
    "船坞": ("shipyard", True, [], "船台占用/在造"),
    # —— P2a：舰队与海战 ——
    "舰队": ("fleet_cmd", True, [], "舰队列表/创建/详情"),
    "编队": ("formation", True, [], "调编 <舰队> 加入/移出 <舰名…>"),
    "移动": ("move_cmd", True, ["走"], "舰队移动 <舰队> <x,y>"),
    "攻击": ("attack_cmd", True, ["进攻"], "接敌攻击 <舰队> <x,y>"),
    "撤退": ("retreat_cmd", True, [], "撤离接触 <舰队>"),
    "战报": ("battle_report", True, [], "战报列表/详情 [id]"),
    "海图": ("chart", True, [], "周边海图"),
    # —— P2b+ 占位 ——
    "驻防": ("stub", False, [], "驻防岛屿"),
    "巡逻": ("stub", False, [], "侦察巡逻"),
    "反潜": ("stub", False, [], "反潜巡逻"),
    "护航": ("stub", False, [], "护航航线"),
    "封锁": ("stub", False, [], "封锁敌港"),
    "伏击": ("stub", False, [], "潜艇伏击"),
    "破交": ("stub", False, [], "袭击商船"),
    "登陆": ("stub", False, [], "登陆夺岛"),
    "空袭": ("stub", False, [], "舰载机出击"),
}
ALIASES = {alias: cmd for cmd, (_, _, aliases, _) in COMMANDS.items() for alias in aliases}

RES_NAME = {"steel": "钢", "oil": "油", "aluminium": "铝", "rare_earth": "稀土",
            "chips": "芯片", "food": "食物", "supply": "补给", "manpower": "人力",
            "money": "资金", "science": "科研", "intel": "情报"}
CHECK_RES = ("steel", "oil", "money", "rare_earth", "chips", "science", "manpower")
RARITY_MARK = {"white": "白", "blue": "蓝", "purple": "紫", "gold": "金"}
RARITY_EMOJI = {"white": "⚪", "blue": "🔵", "purple": "🟣", "gold": "🟡"}


@dataclass
class Ctx:
    conn: object
    cfg: dict
    confirm: object
    origin: str
    qq: str
    nick: str
    args: list
    raw: str
    dstore: object = None


def _island_name(cfg, itype):
    return cfg["island_types"].get(itype, {}).get("name", itype)


def _ore_text(ore: dict) -> str:
    names = {"iron": "铁", "oil": "石油", "aluminium": "铝土", "fish": "渔场"}
    return " ".join(f"{names.get(k,k)}★{v}" for k, v in ore.items()) or "无矿床"


def _cost_text(cost: dict) -> str:
    return " ".join(f"{RES_NAME.get(k,k)}{v}" for k, v in cost.items()
                    if k in RES_NAME and v)


def _missing_res(p, cost: dict):
    return [f"{RES_NAME.get(k,k)}{v - p[k]:.0f}" for k, v in cost.items()
            if k in p.keys() and p[k] < v]


class GameCommands:
    def __init__(self, conn, cfg, confirm, design_store=None):
        self.conn, self.cfg, self.confirm, self.dstore = conn, cfg, confirm, design_store
        self.mc = pools.mod_data()  # 模块池配置

    def _player(self, qq):
        return self.conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()

    # ---------- 账号 ----------
    async def register(self, ctx: Ctx) -> str:
        name = " ".join(ctx.args).strip() or ctx.nick or ctx.qq
        name = name[:12]
        result, err = spawn.create_capital(self.conn, self.cfg, ctx.qq, name)
        if err:
            return f"❌ {err}"
        player, isl = result
        ore = json.loads(isl["ore_json"])
        action = {"type": "register", "can_reroll": self.cfg["spawn"]["allow_reroll"]}
        self.confirm.put(ctx.origin, ctx.qq, action)
        others = self.conn.execute("SELECT COUNT(*) c FROM players WHERE qq!=?", (ctx.qq,)).fetchone()["c"]
        return (f"🌊 欢迎，提督【{name}】！\n"
                f"出生岛：({isl['x']},{isl['y']}) {_island_name(self.cfg, isl['itype'])}"
                f"｜{_ore_text(ore)}｜槽位 {self.cfg['island_types'][isl['itype']]['slots']}+D\n"
                f"🎁 新手包：钢800 油200 食物500 资金1000 + 总督府Lv1\n"
                f"🌍 当前海域已有 {others} 股势力\n"
                f"回复：1 看新手引导　2 直接开始　3 重掷出生点（仅1次）")

    async def help(self, ctx: Ctx) -> str:
        p0 = [f"/nw{c}" for c, (_, ok, _, _) in COMMANDS.items() if ok]
        later = [c for c, (_, ok, _, _) in COMMANDS.items() if not ok]
        return ("⚓ 海战模拟器指令\n" + "　".join(p0) +
                "\n\n后续开放：" + "、".join(later) +
                "\n例：/nw研究 驱逐舰 T3｜/nw建造 研究所｜/nw设计 → /nw护卫舰 → /nw组装 海圻号 1 5 8")

    async def status(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return f"❌ 尚未注册，发 /nw注册 势力名 开府（{PREFIX_HINT}）"
        isl_count = self.conn.execute("SELECT COUNT(*) c FROM islands WHERE owner_qq=?", (ctx.qq,)).fetchone()["c"]
        ship_count = self.conn.execute("SELECT COUNT(*) c FROM ships WHERE qq=?", (ctx.qq,)).fetchone()["c"]
        bq = self.conn.execute("SELECT COUNT(*) c FROM build_queue WHERE qq=?", (ctx.qq,)).fetchone()["c"]
        rq = research.active_research(self.conn, ctx.qq)
        rank = self.conn.execute("SELECT COUNT(*) c FROM players WHERE money>?", (p["money"],)).fetchone()["c"] + 1
        total = self.conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
        return (f"⚓ {p['name']}　排名 #{rank}/{total}　恶名 {p['infamy']}\n"
                f"🏝 岛屿 {isl_count}　❤ 民心 {p['morale']:.0f}　🚢 舰船 {ship_count}\n"
                f"📦 钢{p['steel']:.0f} 油{p['oil']:.0f} 食物{p['food']:.0f} "
                f"补给{p['supply']:.0f} 人力{p['manpower']:.0f}\n"
                f"💰 资金{p['money']:.0f}　🔬 科研{p['science']:.0f}　"
                f"铝{p['aluminium']:.0f} 稀土{p['rare_earth']:.0f}\n"
                f"🛠 建造 {bq}　🧪 研究 {rq}　首都 ({p['capital_x']},{p['capital_y']})")

    async def resources(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        lines = [f"📦 {p['name']} 的仓库："]
        for k, zh in RES_NAME.items():
            lines.append(f"{zh} {p[k]:.0f}")
        return "\n".join(lines[:5]) + "\n" + "　".join(lines[5:])

    # ---------- 岛屿 ----------
    async def island(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        x, y = p["capital_x"], p["capital_y"]
        if ctx.args:
            try:
                x, y = [int(v) for v in ctx.args[0].replace("，", ",").split(",")[:2]]
            except ValueError:
                return "❌ 坐标格式示例：/nw岛 234,567"
        isl = self.conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
        if not isl:
            return f"🌊 ({x},{y}) 是未占领海域（未侦察，无建筑）"
        tdef = self.cfg["island_types"][isl["itype"]]
        ore = json.loads(isl["ore_json"])
        bs = self.conn.execute("SELECT * FROM buildings WHERE x=? AND y=?", (x, y)).fetchall()
        used = len(bs)
        owner = isl["owner_qq"] or "中立"
        lines = [f"🏝 ({x},{y}) {_island_name(self.cfg, isl['itype'])}　归属:{owner}",
                 f"D{isl['dev_level']}/{tdef['d_max']}　槽位 {used}/{tdef['slots']+isl['dev_level']}"
                 f"　控制 {isl['control']:.0f}%　耐久 {isl['hp']:.0f}",
                 f"资源：{_ore_text(ore)}",
                 "建筑：" + ("、".join(f"{self.cfg['buildings'][b['def_id']]['name']}Lv{b['level']}"
                                      for b in bs if b["def_id"] in self.cfg["buildings"]) or "无")]
        return "\n".join(lines)

    # ---------- 建造 ----------
    def _building_cost(self, def_id: str, target_lv: int):
        """返回 (costs{steel,oil,money}, work_ticks)。"""
        bdef = self.cfg["buildings"][def_id]
        if bdef.get("cost_special") == "governor":
            base = self.cfg["cost_tiers"]["2"]
            mult = target_lv ** 1.3
            return ({k: int(base[k] * mult) for k in ("steel", "oil", "money")},
                    max(2, int(base["work"] * mult / 2)))
        tier = min(5, target_lv)
        base = self.cfg["cost_tiers"][str(tier)]
        extra = 1.6 ** max(0, target_lv - 5)
        return ({k: int(base[k] * extra) for k in ("steel", "oil", "money")},
                int(base["work"] * extra))

    async def build(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if not ctx.args:
            return "用法：/nw建造 建筑名（默认在首都）。可建：" + "、".join(
                d["name"] for d in self.cfg["buildings"].values())
        bname = ctx.args[0]
        def_id = next((k for k, d in self.cfg["buildings"].items() if d["name"] == bname), None)
        if not def_id:
            return f"❌ 没有【{bname}】。可建：" + "、".join(
                d["name"] for d in self.cfg["buildings"].values())
        bdef = self.cfg["buildings"][def_id]
        x, y = p["capital_x"], p["capital_y"]
        isl = self.conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
        tdef = self.cfg["island_types"][isl["itype"]]
        ore = json.loads(isl["ore_json"])

        existing = self.conn.execute(
            "SELECT level FROM buildings WHERE x=? AND y=? AND def_id=?", (x, y, def_id)).fetchone()
        target_lv = (existing["level"] + 1) if existing else 1
        if target_lv > bdef["max_lv"]:
            return f"❌ {bname} 已满级 Lv{bdef['max_lv']}"
        if not existing and bdef.get("unique") and self.conn.execute(
                "SELECT 1 FROM buildings WHERE x=? AND y=? AND def_id=?", (x, y, def_id)).fetchone():
            return f"❌ {bname} 每岛限 1 座"
        if not existing:  # 槽位
            used = self.conn.execute(
                "SELECT COUNT(*) c FROM buildings WHERE x=? AND y=?", (x, y)).fetchone()["c"]
            if used >= tdef["slots"] + isl["dev_level"]:
                return f"❌ 槽位不足（{used}/{tdef['slots']+isl['dev_level']}），先升级总督府提升岛级D"
        if bdef.get("min_dev") and isl["dev_level"] < bdef["min_dev"]:
            return f"❌ {bname} 需要岛级 D≥{bdef['min_dev']}（先升总督府）"
        if bdef.get("need_ore") and ore.get(bdef["need_ore"], 0) < 1:
            zh = {"iron": "铁矿", "oil": "油田"}[bdef["need_ore"]]
            return f"❌ 本岛无{zh}矿床，无法建造{bname}"
        if bdef.get("need_fishery") and ore.get("fish", 0) < 1:
            return "❌ 本岛周边无渔场，无法建造渔场码头"

        costs, work = self._building_cost(def_id, target_lv)
        missing = [f"{RES_NAME[k]}{v - p[k]:.0f}" for k, v in costs.items() if p[k] < v]
        if missing:
            return "❌ 资源不足：缺 " + "、".join(missing)
        econ_tick = meta_get(self.conn, "econ_tick", int, 0)
        mins = work * self.cfg["tick"]["economic_min"]
        action = {"type": "build", "def_id": def_id, "target_lv": target_lv,
                  "x": x, "y": y, "costs": costs, "end_tick": econ_tick + work}
        self.confirm.put(ctx.origin, ctx.qq, action)
        return (f"🔧 准备{'升级' if existing else '建造'}【{bname} Lv{target_lv}】({x},{y})\n"
                f"消耗：钢{costs['steel']} 油{costs['oil']} 资金{costs['money']}\n"
                f"工时 {work} tick（约 {mins//60}小时{mins%60}分）\n{bdef['desc']}\n"
                f"回复：1 确认　2 取消")

    async def queue(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        econ_tick = meta_get(self.conn, "econ_tick", int, 0)
        step_min = self.cfg["tick"]["economic_min"]
        lines = []
        # 建造
        for i, q in enumerate(self.conn.execute(
                "SELECT * FROM build_queue WHERE qq=? ORDER BY end_tick", (ctx.qq,)).fetchall(), 1):
            bname = self.cfg["buildings"][q["def_id"]]["name"]
            wait = max(0, q["end_tick"] - econ_tick) * step_min
            lines.append(f"🛠 {bname}Lv{q['target_level']} ({q['x']},{q['y']}) "
                         f"剩约 {wait//60}h{wait%60}m")
        # 研究
        now = time.time()
        for q in self.conn.execute(
                "SELECT * FROM research_queue WHERE qq=? ORDER BY end_ts", (ctx.qq,)).fetchall():
            wait = max(0, int(q["end_ts"] - now))
            cname = self.mc["classes"][q["unit_type"]]["name"]
            lines.append(f"🧪 {cname} T{q['tier']}　剩约 {wait//60}分{wait%60}秒（完成自动推送）")
        # 船坞
        for i, q in enumerate(self.conn.execute(
                "SELECT pq.*,d.name dn,d.ship_class FROM production_queue pq "
                "JOIN designs d ON d.id=pq.design_id WHERE pq.qq=? ORDER BY end_tick",
                (ctx.qq,)).fetchall(), 1):
            wait = max(0, q["end_tick"] - econ_tick) * step_min
            lines.append(f"🚢 {q['dn']}×{q['qty']} ({q['x']},{q['y']}) "
                         f"剩约 {wait//60}h{wait%60}m")
        return ("📋 进行中：\n" + "\n".join(lines)) if lines else "📭 所有队列都空着。/nw建造、/nw研究、/nw生产 走起。"

    async def ranking(self, ctx: Ctx) -> str:
        rows = self.conn.execute(
            "SELECT p.qq,p.name,p.money,COUNT(i.x) c FROM players p "
            "LEFT JOIN islands i ON i.owner_qq=p.qq GROUP BY p.qq "
            "ORDER BY p.money DESC LIMIT 10").fetchall()
        lines = ["🏆 资金榜（P0，更多榜单开发中）"]
        for i, r in enumerate(rows, 1):
            tag = " ←你" if r["qq"] == ctx.qq else ""
            lines.append(f"{i}. {r['name']}　资金{r['money']:.0f}　岛{r['c']}{tag}")
        return "\n".join(lines)

    async def stub(self, ctx: Ctx) -> str:
        return "🛠 该指令在后续版本开放（P2：舰队移动/交战指令）。当前可先研究蓝图、造船发育。"

    # ---------- P1：研究抽卡 ----------
    async def research(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if len(ctx.args) < 2:
            return ("用法：/nw研究 <舰种> <T等级>\n"
                    "可选舰种：" + "、".join(c["name"] for c in self.mc["classes"].values()) +
                    "\n例：/nw研究 驱逐舰 T3（先建研究所，Lv1~5 开 T1~T5 池）")
        cls = pools.class_id(ctx.args[0])
        tier = pools.parse_tier(ctx.args[1])
        if not cls:
            return f"❌ 不认识舰种【{ctx.args[0]}】。可选：" + "、".join(
                c["name"] for c in self.mc["classes"].values())
        if not tier:
            return "❌ 等级格式：T1~T5，如 /nw研究 护卫舰 T1"

        lab_lv, proving_lv, lx, ly = research.lab_info(self.conn, ctx.qq)
        if lab_lv < 1:
            return "❌ 首都还没有【研究所】，先 /nw建造 研究所"
        if lab_lv < tier:
            return f"❌ T{tier} 池需要研究所 Lv{tier}（当前 Lv{lab_lv}）"
        slots = research.research_slots(lab_lv)
        active = research.active_research(self.conn, ctx.qq)
        rc = self.mc["research_cost"][str(tier)]
        cname = self.mc["classes"][cls]["name"]
        pool_size = len(pools.pool_modules(cls, tier))
        owned_here = len({m["id"] for m in pools.pool_modules(cls, tier)}
                         & pools.owned_ids(self.conn, ctx.qq))
        # 报价（两种模式都展示，校验按较高的快捷花费提示缺口）
        dur = research.research_duration(self.cfg, self.mc, proving_lv)
        normal_cost = {k: rc[k] for k in ("science", "money", "rare_earth", "chips")}
        expr_cost = {k: v * self.mc["express_mult"] for k, v in normal_cost.items()}
        lines = [f"🔬 准备研究【{cname} T{tier}】蓝图（池内 {owned_here}/{pool_size} 已收集）"]
        if active >= slots:
            lines.append(f"❌ 研究所 Lv{lab_lv} 只有 {slots} 个并行研究位，当前已满。"
                         f"/nw研究状态 查看，或选快捷立即完成")
        lines.append(f"1️⃣ 普通：{_cost_text(normal_cost)}｜约 {dur//60} 分钟"
                     + ("" if active < slots else "（研究位满，暂不可选）"))
        lines.append(f"2️⃣ 快捷：{_cost_text(expr_cost)}｜立即出货（花费×{self.mc['express_mult']}）")
        miss_n = _missing_res(p, normal_cost)
        miss_e = _missing_res(p, expr_cost)
        if miss_n:
            lines.append("普通档缺：" + "、".join(miss_n))
        if miss_e:
            lines.append("快捷档缺：" + "、".join(miss_e))
        lines.append("白60% 蓝30% 紫9% 金1%｜10连必蓝/50必紫/100必金｜首抽必出白船体")
        lines.append("回复：1 普通　2 快捷　3 取消")
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "research", "cls": cls, "tier": tier,
                          "normal": normal_cost, "express": expr_cost, "dur": dur,
                          "slots": slots})
        return "\n".join(lines)

    async def research_status(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        lab_lv, proving_lv, _, _ = research.lab_info(self.conn, ctx.qq)
        if lab_lv < 1:
            return "🔬 尚无研究所。/nw建造 研究所"
        slots = research.research_slots(lab_lv)
        rows = self.conn.execute(
            "SELECT * FROM research_queue WHERE qq=? ORDER BY end_ts", (ctx.qq,)).fetchall()
        now = time.time()
        lines = [f"🔬 研究所 Lv{lab_lv}（T1~T{lab_lv} 池，{len(rows)}/{slots} 研究位）"]
        for q in rows:
            wait = max(0, int(q["end_ts"] - now))
            cname = self.mc["classes"][q["unit_type"]]["name"]
            lines.append(f"· {cname} T{q['tier']}　剩 {wait//60}分{wait%60}秒")
        # 保底进度
        prs = self.conn.execute(
            "SELECT * FROM research_pity WHERE qq=?", (ctx.qq,)).fetchall()
        active_pools = {pools.pool_id(r["unit_type"], r["tier"]) for r in rows}
        near = [r for r in prs if r["pool"] in active_pools]
        for r in near:
            if r["since_blue"] >= 5 or r["since_purple"] >= 30 or r["since_gold"] >= 80:
                lines.append(f"· {r['pool']} 保底：{r['since_blue']}/10 蓝 "
                             f"{r['since_purple']}/50 紫 {r['since_gold']}/100 金")
        if len(rows) < slots:
            lines.append("💡 有空位：/nw研究 <舰种> <T>")
        return "\n".join(lines)

    async def blueprints(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        if not ctx.args:
            lines = ["📘 蓝图图鉴（/nw蓝图 <舰种> 查看明细）"]
            for cid, cdef in self.mc["classes"].items():
                n = len(pools.owned_modules(self.conn, ctx.qq, cid))
                if n:
                    lines.append(f"· {cdef['name']}：{n} 张")
            bp = self.conn.execute("SELECT COUNT(*) c FROM blueprints WHERE qq=?",
                                   (ctx.qq,)).fetchone()["c"]
            if not bp:
                return "📘 还没有蓝图。先 /nw建造 研究所，再 /nw研究 护卫舰 T1（新手注册时可选送护卫/驱逐全套T1）"
            return "\n".join(lines)
        cls = pools.class_id(ctx.args[0])
        if not cls:
            return f"❌ 不认识舰种【{ctx.args[0]}】"
        mods = pools.owned_modules(self.conn, ctx.qq, cls)
        if not mods:
            return f"📘 {self.mc['classes'][cls]['name']}：暂无蓝图。/nw研究 {self.mc['classes'][cls]['name']} T1"
        lines = [f"📘 {self.mc['classes'][cls]['name']} 蓝图（{len(mods)}张）"]
        by_slot = {}
        for m in mods:
            by_slot.setdefault(m["slot"], []).append(m)
        for slot in self.mc["classes"][cls]["slots"]:
            ms = by_slot.get(slot)
            if not ms:
                continue
            zh = self.mc["slot_names"][slot]
            desc = "、".join(f"{RARITY_EMOJI[m['rarity']]}T{m['tier']}{m['name']}" for m in ms)
            lines.append(f"【{zh}】{desc}")
        return "\n".join(lines)

    async def fragments(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        rows = self.conn.execute(
            "SELECT * FROM fragments WHERE qq=? AND amount>0 ORDER BY tier,rarity",
            (ctx.qq,)).fetchall()
        lines = ["🧩 碎片库存："]
        if not rows:
            lines.append("（暂无。抽到重复蓝图会自动转成碎片）")
        for r in rows:
            lines.append(f"· T{r['tier']} {RARITY_MARK[r['rarity']]}碎片 ×{r['amount']}")
        lines.append("兑换价（另付该T科研点）：白50 蓝150 紫400 金1000")
        lines.append("用法：/nw碎片兑换 <舰种> <T> <模块名>，如 /nw碎片兑换 驱逐舰 T3 远洋船体")
        return "\n".join(lines)

    async def fragment_exchange(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if len(ctx.args) < 3:
            return "用法：/nw碎片兑换 <舰种> <T> <模块名>，如 /nw碎片兑换 驱逐舰 T3 远洋船体"
        cls = pools.class_id(ctx.args[0])
        tier = pools.parse_tier(ctx.args[1])
        name = "".join(ctx.args[2:])
        if not cls or not tier:
            return "❌ 格式：/nw碎片兑换 <舰种> <T1~T5> <模块名>"
        target = next((m for m in pools.pool_modules(cls, tier) if m["name"] == name), None)
        if not target:
            return f"❌【{self.mc['classes'][cls]['name']} T{tier}】池中没有模块【{name}】。/nw蓝图 {self.mc['classes'][cls]['name']} 查看"
        rdef = self.mc["rarity"][target["rarity"]]
        frow = self.conn.execute("SELECT amount FROM fragments WHERE qq=? AND tier=? AND rarity=?",
                                 (ctx.qq, tier, target["rarity"])).fetchone()
        have = frow["amount"] if frow else 0
        need_sci = self.mc["research_cost"][str(tier)]["science"]
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "frag_exchange", "cls": cls, "tier": tier, "name": name})
        return (f"🧩 定向兑换【{RARITY_MARK[target['rarity']]}·{target['name']} T{tier}】"
                f"（{self.mc['slot_names'][target['slot']]}槽）\n"
                f"消耗：{RARITY_MARK[target['rarity']]}T{tier}碎片 {have}/{rdef['exchange']}"
                f"　科研 {p['science']:.0f}/{need_sci}\n"
                f"回复：1 确认　2 取消")

    # ---------- P1：设计模式（浏览编号目录 → 一行组装） ----------
    DESIGN_HELP = (
        "✏️ 舰船设计模式\n"
        "① /nw<舰种> 浏览该舰种全部模块目录（✅已拥有/🔒未研究），如 /nw护卫舰\n"
        "② /nw组装 <方案名> <模块编号…> 选编号配槽，如 /nw组装 海圻号 1 5 8\n"
        "③ /nw退出 离开设计模式\n"
        "可选舰种：护卫舰、驱逐舰、轻巡洋舰、攻击潜艇、护航潜艇、运输船")

    async def design(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        if not self.dstore:
            return "❌ 设计器会话未初始化，请联系管理员"
        if ctx.args:
            return ("新设计流程：直接 /nw设计 进入设计模式，再发 /nw<舰种> 浏览模块、"
                    "/nw组装 <方案名> <编号…> 组装。\n" + self.DESIGN_HELP)
        st = self.dstore.enter(ctx.origin, ctx.qq)
        cur = ""
        if st.get("cls"):
            cur = f"\n当前浏览：{self.mc['classes'][st['cls']]['name']}"
        return f"🎨 已进入设计模式！{cur}\n" + self.DESIGN_HELP

    async def exit_design(self, ctx: Ctx) -> str:
        if self.dstore:
            self.dstore.drop(ctx.origin, ctx.qq)
        return "🚪 已退出舰船设计模式。"

    @staticmethod
    def _mod_brief(info: dict) -> str:
        """模块目录单行属性摘要（T1 基础值）。"""
        zh = {"spd": "速", "fire": "火", "torpedo": "雷", "asw": "潜", "aa": "空",
              "detect": "探", "stealth": "隐", "cargo": "舱", "hit": "控",
              "speed": "速", "troop": "兵", "range": "程", "fuel_save": "省"}
        parts = []
        for k, v in info["stats"].items():
            if k == "hp":
                parts.append(f"耐×{v:g}")
            elif k in zh:
                if k in ("spd",):
                    parts.append(f"{zh[k]}{v:+g}")
                else:
                    parts.append(f"{zh[k]}{v:g}")
        return " ".join(parts)

    async def browse_class(self, ctx: Ctx, cls: str) -> str:
        """设计模式内 /nw<舰种>：列出该舰种 T1~T5 全部模块的编号目录。"""
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        st = self.dstore.get(ctx.origin, ctx.qq)
        if not st:
            return "❌ 请先发 /nw设计 进入设计模式"
        clsdef = self.mc["classes"][cls]
        owned = pools.owned_ids(self.conn, ctx.qq)
        catalog, lines = [], [f"📚 {clsdef['name']} 模块目录（✅可装 / 🔒需先研究）",
                              f"/nw组装 <方案名> <编号…> 配槽，每槽限选1个；"
                              f"必选槽：{'、'.join(self.mc['slot_names'][s] for s in clsdef['required'])}"]
        idx = 0
        for slot in clsdef["slots"]:
            req = "★" if slot in clsdef["required"] else "　"
            lines.append(f"【{req}{self.mc['slot_names'][slot]}】")
            for tier in range(1, 6):
                mods = sorted((m for m in pools.pool_modules(cls, tier) if m["slot"] == slot),
                              key=lambda m: pools.RARITY_ORDER.index(m["rarity"]))
                for m in mods:
                    idx += 1
                    have = m["id"] in owned
                    catalog.append({"id": m["id"], "slot": slot, "owned": have})
                    mark = "✅" if have else "🔒"
                    brief = self._mod_brief(m)
                    lines.append(f"{idx}. {mark}{RARITY_EMOJI[m['rarity']]}T{tier} "
                                 f"{m['name']} {brief}".rstrip())
        st["cls"] = cls
        st["catalog"] = catalog
        n_have = sum(1 for e in catalog if e["owned"])
        lines.append(f"共 {len(catalog)} 个模块，已拥有 {n_have} 个。"
                     f"编号仅对本次浏览有效，重新研究后再发 /nw{clsdef['name']} 刷新。")
        return "\n".join(lines)

    async def assemble(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        st = self.dstore.get(ctx.origin, ctx.qq) if self.dstore else None
        if not st:
            return "❌ 请先发 /nw设计 进入设计模式，再 /nw<舰种> 浏览模块编号"
        cls = st.get("cls")
        catalog = st.get("catalog") or []
        if not cls or not catalog:
            return "❌ 还没选择舰种，先发 /nw<舰种> 浏览模块目录，如 /nw护卫舰"
        if len(ctx.args) < 1:
            return "用法：/nw组装 <方案名> <模块编号…>，如 /nw组装 海圻号 1 5 8"
        name = ctx.args[0][:8]
        # 编号兼容：1 / 模块1 / #1 / No.1
        picks, bad = [], []
        for tok in ctx.args[1:]:
            mt = re.search(r"\d+", tok)
            if not mt:
                bad.append(tok)
                continue
            n = int(mt.group())
            if n < 1 or n > len(catalog):
                bad.append(tok)
            elif n not in picks:
                picks.append(n)
        if bad:
            return f"❌ 无法识别或超出目录范围的编号：{'、'.join(bad)}（当前目录 1~{len(catalog)}）"
        if not picks:
            return "❌ 请至少填 1 个模块编号（船体必选）"
        chosen, seen_slot = {}, set()
        for n in picks:
            e = catalog[n - 1]
            if not e["owned"]:
                info = pools.module_info(e["id"])
                return (f"❌ 编号{n}【{info['name']}】还未拥有（🔒），"
                        f"先 /nw研究 {self.mc['classes'][cls]['name']} T{info['tier']} 抽蓝图")
            if e["slot"] in seen_slot:
                zh = self.mc["slot_names"][e["slot"]]
                return f"❌【{zh}】槽只能选 1 个模块，编号{n} 与同槽先选的模块冲突"
            seen_slot.add(e["slot"])
            chosen[e["slot"]] = e["id"]
        preview = pools.ship_preview(cls, chosen)
        if preview["missing_required"]:
            miss = "、".join(self.mc["slot_names"][s] for s in preview["missing_required"])
            return f"❌ 还缺少必选槽：{miss}（目录中★号槽位）"
        if self.conn.execute("SELECT 1 FROM designs WHERE qq=? AND name=?",
                             (ctx.qq, name)).fetchone():
            return f"❌ 你已有设计【{name}】，换个名字或 /nw设计查看 {name}"
        action = {"type": "save_design", "name": name, "cls": cls,
                  "modules": chosen, "tier": preview["tier"],
                  "stats": preview["stats"], "cost": preview["cost"],
                  "work_ticks": preview["work_ticks"]}
        self.confirm.put(ctx.origin, ctx.qq, action)
        mods_text = "、".join(
            f"{self.mc['slot_names'][s]}:{pools.module_info(mid)['name']}"
            for s, mid in chosen.items())
        mins = preview["work_ticks"] * self.cfg["tick"]["economic_min"]
        return (f"📐 方案预览【{name}】{self.mc['classes'][cls]['name']} T{preview['tier']}\n"
                f"配槽：{mods_text}\n{pools.stats_text(preview['stats'])}\n"
                f"单舰造价：{_cost_text(preview['cost'])}\n"
                f"单舰工时：约 {mins//60}h{mins%60}m\n"
                f"回复：1 保存设计　2 取消（仍在设计模式内，可继续组装其他方案）")

    async def design_list(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        rows = self.conn.execute("SELECT * FROM designs WHERE qq=? ORDER BY id",
                                 (ctx.qq,)).fetchall()
        if not rows:
            return "📐 还没有设计。/nw设计 <舰名> <舰种>"
        lines = ["📐 我的舰船设计："]
        for r in rows:
            cname = self.mc["classes"].get(r["ship_class"], {}).get("name", r["ship_class"])
            lines.append(f"· {r['name']}　{cname} T{r['tier']}（/nw设计查看 {r['name']}）")
        return "\n".join(lines)

    async def design_view(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        if not ctx.args:
            return "用法：/nw设计查看 <设计名>"
        name = ctx.args[0]
        r = self.conn.execute("SELECT * FROM designs WHERE qq=? AND name=?",
                              (ctx.qq, name)).fetchone()
        if not r:
            return f"❌ 没有设计【{name}】，/nw设计列表"
        modules = json.loads(r["modules"])
        stats = json.loads(r["stats_json"])
        cost = json.loads(r["cost_json"])
        mods_text = "、".join(
            f"{self.mc['slot_names'][s]}:{pools.module_info(mid)['name']}"
            for s, mid in modules.items())
        mins = r["work_ticks"] * self.cfg["tick"]["economic_min"]
        cname = self.mc["classes"][r["ship_class"]]["name"]
        return (f"📐 {r['name']}　{cname} T{r['tier']}\n配槽：{mods_text}\n"
                f"{pools.stats_text(stats)}\n单舰造价：{_cost_text(cost)}\n"
                f"工时约 {mins//60}h{mins%60}m｜/nw生产 {r['name']} <数量>")

    # ---------- P1：船坞排产 ----------
    async def produce(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if not ctx.args:
            return "用法：/nw生产 <设计名> [数量]。先 /nw设计列表 看已有设计"
        name = ctx.args[0]
        qty = 1
        if len(ctx.args) > 1 and ctx.args[1].isdigit():
            qty = max(1, min(self.mc["max_produce_qty"], int(ctx.args[1])))
        d = self.conn.execute("SELECT * FROM designs WHERE qq=? AND name=?",
                              (ctx.qq, name)).fetchone()
        if not d:
            return f"❌ 没有设计【{name}】，先 /nw设计 或 /nw设计列表"
        lab_lv, _, x, y = research.lab_info(self.conn, ctx.qq)
        yard = research.building_level(self.conn, x, y, "shipyard")
        if yard < 1:
            return "❌ 首都没有【造船厂】，先 /nw建造 造船厂"
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM production_queue WHERE qq=?", (ctx.qq,)).fetchone()["c"]
        if active >= yard:
            return f"❌ 造船厂 Lv{yard} 的 {yard} 个船台全满（在造 {active} 批），升级造船厂解锁更多船台"
        unit_cost = json.loads(d["cost_json"])
        cost = {k: v * qty for k, v in unit_cost.items() if v}
        missing = _missing_res(p, cost)
        if missing:
            return "❌ 资源不足：缺 " + "、".join(missing)
        work = d["work_ticks"] * qty
        econ_tick = meta_get(self.conn, "econ_tick", int, 0)
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "produce", "design_id": d["id"], "qty": qty,
                          "x": x, "y": y, "cost": cost, "end_tick": econ_tick + work})
        mins = work * self.cfg["tick"]["economic_min"]
        return (f"🚢 准备在 ({x},{y}) 船坞建造【{d['name']}】×{qty}\n"
                f"消耗：{_cost_text(cost)}\n工时 {work} tick（约 {mins//60}小时{mins%60}分）\n"
                f"回复：1 确认　2 取消")

    async def shipyard(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        x, y = p["capital_x"], p["capital_y"]
        yard = research.building_level(self.conn, x, y, "shipyard")
        if yard < 1:
            return "⚓ 首都没有造船厂。/nw建造 造船厂（船台数=造船厂等级）"
        rows = self.conn.execute(
            "SELECT pq.*,d.name dn FROM production_queue pq JOIN designs d ON d.id=pq.design_id "
            "WHERE pq.qq=? ORDER BY end_tick", (ctx.qq,)).fetchall()
        econ_tick = meta_get(self.conn, "econ_tick", int, 0)
        lines = [f"⚓ 造船厂 Lv{yard} ({x},{y})　船台 {len(rows)}/{yard}"]
        if not rows:
            lines.append("船台空闲。/nw生产 <设计名> <数量>")
        for q in rows:
            wait = max(0, q["end_tick"] - econ_tick) * self.cfg["tick"]["economic_min"]
            lines.append(f"· {q['dn']}×{q['qty']}　剩约 {wait//60}h{wait%60}m")
        return "\n".join(lines)

    # ---------- P2a：舰队与海战 ----------
    @staticmethod
    def _mission_brief(ms: dict) -> str:
        t = ms.get("type")
        if t in ("move", "attack"):
            return ("机动" if t == "move" else "攻击") + f"→({ms['tx']},{ms['ty']})"
        if "retreat_since" in ms:
            return "撤离中"
        return "待命"

    async def fleet_cmd(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 还未注册，先 /nw注册"
        rows = fleetm.list_fleets(self.conn, ctx.qq)
        if not ctx.args:
            if not rows:
                return ("⛵ 你还没有舰队。/nw舰队 <名字> 在首都港组建，"
                        "再用 /nw编队 <舰队> 加入 <舰名…> 编入已下水舰船。")
            lines = ["⛵ 我的舰队："]
            for f in rows:
                n = fleetm.ship_count(self.conn, f["id"])
                bid = fleetm.in_battle(self.conn, f["id"])
                tag = "⚔️交战中 " if bid else ""
                lines.append(f"· {f['name']}({f['id']}) ({f['x']},{f['y']}) "
                             f"{n}舰 {tag}{self._mission_brief(json.loads(f['mission'] or '{}'))}")
            lines.append("/nw舰队 <名> 查看详情/组建新队")
            return "\n".join(lines)
        name = " ".join(ctx.args).strip()
        f = fleetm.get_fleet(self.conn, ctx.qq, name)
        if f:
            ships = fleetm.fleet_ships(self.conn, f["id"])
            head = (f"⛵ 舰队【{f['name']}】坐标({f['x']},{f['y']})　"
                    f"{self._mission_brief(json.loads(f['mission'] or '{}'))}")
            if not ships:
                return head + "\n（空编队）/nw编队 " + f["name"] + " 加入 <舰名…>"
            lines = [head, f"编队航速 {fleetm.fleet_speed(self.conn, f['id'])}节，编制："]
            for s in ships:
                st = fleetm.ship_stats(s)
                lines.append(f"· {s['name']} T{s['tier']} 耐久{s['hp']:.0f}/{s['max_hp']:.0f}"
                             f"　{pools.stats_text(st)}")
            return "\n".join(lines)
        tick = meta_get(self.conn, "war_tick", int, 0)
        row, err = fleetm.create_fleet(self.conn, ctx.qq, name,
                                       p["capital_x"], p["capital_y"], tick)
        if err:
            return f"❌ {err}"
        return (f"✅ 舰队【{row['name']}】已在首都港({row['x']},{row['y']})组建。\n"
                f"编入舰船：/nw编队 {row['name']} 加入 <舰名…>")

    async def formation(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 还未注册，先 /nw注册"
        if len(ctx.args) < 3:
            return "用法：/nw编队 <舰队> 加入/移出 <舰名1> [舰名2…]"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】，先 /nw舰队 {ctx.args[0]} 组建"
        op = ctx.args[1]
        if op not in ("加入", "移出"):
            return "❌ 第二个词必须是 加入 或 移出"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，不能调编（可 /nw撤退 脱离后再编）"
        names = ctx.args[2:]
        if op == "加入":
            added, missing = fleetm.add_ships(self.conn, ctx.qq, f["id"], names)
        else:
            added, missing = fleetm.remove_ships(self.conn, ctx.qq, f["id"], names)
        out = f"✅ {op} {added} 艘"
        if missing:
            cond = "在港同名舰" if op == "加入" else "队中同名舰"
            out += "；未找到" + cond + "：" + "、".join(missing)
        return out

    def _move_quote(self, f, tx: int, ty: int) -> dict:
        dist = math.hypot(tx - f["x"], ty - f["y"])
        spd = fleetm.fleet_speed(self.conn, f["id"])
        step = max(1, round(spd / self.cfg["fleet"]["cells_per_speed"]))
        ticks = max(1, math.ceil(dist / step))
        n = fleetm.ship_count(self.conn, f["id"])
        oil = math.ceil(dist) * n * self.cfg["fleet"]["move_oil_per_ship_cell"]
        return {"dist": math.ceil(dist), "oil": oil, "ticks": ticks,
                "minutes": ticks * self.cfg["tick"]["war_min"]}

    async def _move_like(self, ctx: Ctx, kind: str) -> str:
        if not self._player(ctx.qq):
            return "❌ 还未注册，先 /nw注册"
        if len(ctx.args) < 2:
            return f"用法：/nw{'移动' if kind == 'move' else '攻击'} <舰队> <x,y>"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:]))
        if xy is None:
            return "❌ 坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能出动，先 /nw编队 加入舰船"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战；要脱离请 /nw撤退 <舰队>"
        ms = json.loads(f["mission"] or "{}")
        if ms.get("type") in ("move", "attack"):
            return "❌ 舰队正在机动中，到点后再下新命令"
        q = self._move_quote(f, tx, ty)
        p = self._player(ctx.qq)
        if p["oil"] < q["oil"]:
            return f"❌ 油不够：需 {q['oil']}，现有 {p['oil']:.0f}"
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "fleet_move", "fid": f["id"], "tx": tx, "ty": ty,
                          "oil": q["oil"], "kind": kind})
        verb = "移动至" if kind == "move" else "前往攻击"
        return (f"⚓【{f['name']}】{verb}({tx},{ty})\n"
                f"航程 {q['dist']}格｜航速 {fleetm.fleet_speed(self.conn, f['id'])}节｜"
                f"约 {q['minutes']} 分钟（{q['ticks']} 个战争tick）\n"
                f"耗 油{q['oil']}\n"
                f"同格遇敌将自动开战。回复 1 出航　2 取消")

    async def move_cmd(self, ctx: Ctx) -> str:
        return await self._move_like(ctx, "move")

    async def attack_cmd(self, ctx: Ctx) -> str:
        return await self._move_like(ctx, "attack")

    async def retreat_cmd(self, ctx: Ctx) -> str:
        if not ctx.args:
            return "用法：/nw撤退 <舰队>"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if not fleetm.in_battle(self.conn, f["id"]):
            return "❌ 该舰队当前没有接敌，无需撤退"
        war_tick = meta_get(self.conn, "war_tick", int, 0)
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps({"retreat_since": war_tick}), f["id"]))
        self.conn.commit()
        return (f"↩️【{f['name']}】已下令撤退，将在下一个战争 tick（约 "
                f"{self.cfg['tick']['war_min']} 分钟）脱离接触。")

    async def battle_report(self, ctx: Ctx) -> str:
        if not self._player(ctx.qq):
            return "❌ 还未注册，先 /nw注册"
        if not ctx.args:
            rows = self.conn.execute(
                "SELECT id,tick,x,y,status,summary FROM battles WHERE qq=? "
                "ORDER BY id DESC LIMIT 5", (ctx.qq,)).fetchall()
            if not rows:
                return "📜 尚无战报。找海盗打一场：/nw海图 查看 ⚓ 据点。"
            lines = ["📜 最近战报（/nw战报 <id> 看详情）："]
            for r in rows:
                tag = "进行中" if r["status"] == "active" else "已结束"
                head = (r["summary"].split("。")[0] if r["summary"] and r["status"] != "active"
                        else f"({r['x']},{r['y']}) 交战中")
                lines.append(f"#{r['id']} [{tag}] {head}")
            return "\n".join(lines)
        if not ctx.args[0].isdigit():
            return "用法：/nw战报 [战报id]"
        b = self.conn.execute("SELECT * FROM battles WHERE id=? AND qq=?",
                              (int(ctx.args[0]), ctx.qq)).fetchone()
        if not b:
            return "❌ 没有这份战报"
        evs = self.conn.execute(
            "SELECT round_no,text FROM battle_events WHERE battle_id=? ORDER BY id",
            (b["id"],)).fetchall()
        lines = [f"📜 战报 #{b['id']}　坐标({b['x']},{b['y']})"]
        lines += [e["text"] for e in evs]
        if b["status"] == "over" and b["summary"]:
            lines.append("【结果】" + b["summary"])
        return "\n".join(lines)

    async def chart(self, ctx: Ctx) -> str:
        p = self._player(ctx.qq)
        if not p:
            return "❌ 还未注册，先 /nw注册"
        cx, cy = p["capital_x"], p["capital_y"]
        R = 10
        x0, x1 = max(0, cx - R), min(fleetm.MAP_SIZE - 1, cx + R)
        y0, y1 = max(0, cy - R), min(fleetm.MAP_SIZE - 1, cy + R)
        grid = [["." for _ in range(x1 - x0 + 1)] for _ in range(y1 - y0 + 1)]
        for isl in self.conn.execute(
                "SELECT x,y,owner_qq FROM islands WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                (x0, x1, y0, y1)).fetchall():
            ch = "H" if (isl["x"], isl["y"]) == (cx, cy) else \
                 ("#" if isl["owner_qq"] == ctx.qq else "o")
            grid[isl["y"] - y0][isl["x"] - x0] = ch
        marks = []
        for pr in self.conn.execute(
                "SELECT x,y,comp_json,name FROM ai_fleets WHERE faction='pirate'"
                " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                (x0, x1, y0, y1)).fetchall():
            ch = "x" if json.loads(pr["comp_json"] or "[]") else "P"
            grid[pr["y"] - y0][pr["x"] - x0] = ch
            marks.append(f"{ch}=【{pr['name']}】({pr['x']},{pr['y']})")
        for f in fleetm.list_fleets(self.conn, ctx.qq):
            if x0 <= f["x"] <= x1 and y0 <= f["y"] <= y1:
                grid[f["y"] - y0][f["x"] - x0] = "F"
                marks.append(f"F={f['name']}({f['x']},{f['y']})")
        head = f"🗺️ 以首都({cx},{cy})为中心（北↑，每格约20km）"
        body = "\n".join("".join(row) for row in grid)
        legend = "图例：H首都　#己方岛　o其他岛　P海盗据点　x海盗巡逻队　F己方舰队"
        tail = "\n".join(marks[:8])
        return f"{head}\n{body}\n{legend}\n{tail}".rstrip()

    def _action_fleet_move(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消出航。"
        if choice != "1":
            return "回复 1 出航　2 取消"
        p = self._player(ctx.qq)
        if p["oil"] < action["oil"]:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 油已不足，报价失效。"
        f = self.conn.execute("SELECT * FROM fleets WHERE id=? AND qq=?",
                              (action["fid"], ctx.qq)).fetchone()
        if not f or fleetm.in_battle(self.conn, f["id"]):
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 舰队状态已变化（交战/不存在），命令失效。"
        ms = json.loads(f["mission"] or "{}")
        if ms.get("type") in ("move", "attack"):
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 舰队已在机动中。"
        self._deduct(ctx.qq, {"oil": action["oil"]})
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps({"type": action["kind"], "tx": action["tx"],
                                       "ty": action["ty"]}, ensure_ascii=False),
                           f["id"]))
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        verb = "出击" if action["kind"] == "attack" else "起航"
        return (f"✅【{f['name']}】已{verb}→({action['tx']},{action['ty']})，"
                f"到点/接敌后自动推送战报。")

    # ---------- 确认会话落地 ----------
    async def execute_action(self, ctx: Ctx, action: dict, choice: str) -> str:
        atype = action["type"]
        if atype == "register":
            return await self._action_register(ctx, action, choice)
        if atype == "build":
            return self._action_build(ctx, action, choice)
        if atype == "starter_pick":
            return self._action_starter(ctx, choice)
        if atype == "research":
            return await self._action_research(ctx, action, choice)
        if atype == "frag_exchange":
            return self._action_frag_exchange(ctx, action, choice)
        if atype == "save_design":
            return self._action_save_design(ctx, action, choice)
        if atype == "produce":
            return self._action_produce(ctx, action, choice)
        if atype == "fleet_move":
            return self._action_fleet_move(ctx, action, choice)
        return "❓ 会话已失效，请重新下达指令。"

    async def _action_register(self, ctx: Ctx, action: dict, choice: str) -> str:
        p = self._player(ctx.qq)
        if choice == "1":
            self.confirm.drop(ctx.origin, ctx.qq)
            return ("📜 新手引导：\n1) /nw建造 矿场 提升钢铁日产\n"
                    "2) /nw建造 研究所，然后 /nw研究 护卫舰 T1 抽蓝图\n"
                    "3) 攒齐蓝图后 /nw设计 海圻号 护卫舰 → /nw生产 海圻号\n"
                    "4) 经济每20分钟结算一次，安心下线，完成会推送\n"
                    "随时 /nw我 看状态、/nw帮助 看全部指令。")
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            # 第二步：新手蓝图二选一
            self.confirm.put(ctx.origin, ctx.qq, {"type": "starter_pick"})
            return ("✅ 开府完成！新手蓝图礼包二选一：\n"
                    "1 护卫舰全套 T1（含各槽位全部模块）\n"
                    "2 驱逐舰全套 T1\n"
                    "两种选择都加赠：运输船全套 T1\n"
                    "回复 1 或 2")
        if choice == "3" and action.get("can_reroll"):
            self.conn.execute("DELETE FROM buildings WHERE x=? AND y=?",
                              (p["capital_x"], p["capital_y"]))
            self.conn.execute("DELETE FROM islands WHERE owner_qq=?", (ctx.qq,))
            self.conn.execute("DELETE FROM players WHERE qq=?", (ctx.qq,))
            self.conn.commit()
            self.confirm.drop(ctx.origin, ctx.qq)
            return await self.register(ctx)
        return "回复 1 / 2 / 3 选择"

    def _action_build(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消。"
        if choice != "1":
            return "回复 1 确认　2 取消"
        p = self._player(ctx.qq)
        c = action["costs"]
        if p["steel"] < c["steel"] or p["oil"] < c["oil"] or p["money"] < c["money"]:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 资源已不足，报价失效。"
        self.conn.execute(
            "UPDATE players SET steel=steel-?,oil=oil-?,money=money-? WHERE qq=?",
            (c["steel"], c["oil"], c["money"], ctx.qq))
        self.conn.execute(
            "INSERT INTO build_queue(qq,origin,x,y,def_id,target_level,start_tick,end_tick)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ctx.qq, ctx.origin, action["x"], action["y"], action["def_id"],
             action["target_lv"], meta_get(self.conn, "econ_tick", int, 0), action["end_tick"]))
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        bname = self.cfg["buildings"][action["def_id"]]["name"]
        return f"✅【{bname} Lv{action['target_lv']}】已入队，完成后自动推送。/nw队列 查看"

    def _action_starter(self, ctx: Ctx, choice: str) -> str:
        if choice not in ("1", "2"):
            return "回复 1 护卫舰　2 驱逐舰"
        cls = "frigate" if choice == "1" else "destroyer"
        research.grant_starter(self.conn, ctx.qq, cls)
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        cname = self.mc["classes"][cls]["name"]
        tip = ""
        p = self._player(ctx.qq)
        if aiworld.ensure_pirate(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                                meta_get(self.conn, "war_tick", int, 0)):
            anchor = self.conn.execute(
                "SELECT x,y,level FROM ai_fleets WHERE faction='pirate'"
                " AND comp_json='[]' ORDER BY id LIMIT 1").fetchone()
            if anchor:
                tip = (f"\n⚓ 雷达发现附近海盗据点 L{anchor['level']}（{anchor['x']},"
                       f"{anchor['y']}），造船成军后可前去征讨（/nw海图）。")
        return (f"🎁 已发放：{cname}全套 T1 + 运输船全套 T1！\n"
                f"查看：/nw蓝图 {cname}\n"
                f"下一步：/nw建造 研究所 与 /nw建造 造船厂，"
                f"然后 /nw设计 <舰名> {cname} 配槽造船。{tip}")

    async def _action_research(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "3":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消研究。"
        if choice not in ("1", "2"):
            return "回复 1 普通　2 快捷　3 取消"
        cls, tier = action["cls"], action["tier"]
        cname = self.mc["classes"][cls]["name"]
        p = self._player(ctx.qq)
        if choice == "1":
            active = research.active_research(self.conn, ctx.qq)
            if active >= action["slots"]:
                self.confirm.drop(ctx.origin, ctx.qq)
                return "❌ 研究位刚被占满，改选快捷或稍后再试。"
            cost = action["normal"]
            miss = _missing_res(p, cost)
            if miss:
                self.confirm.drop(ctx.origin, ctx.qq)
                return "❌ 资源已不足：缺 " + "、".join(miss)
            self._deduct(ctx.qq, cost)
            now = int(time.time())
            self.conn.execute(
                "INSERT INTO research_queue(qq,origin,unit_type,tier,mode,start_ts,end_ts)"
                " VALUES(?,?,?,?, 'normal',?,?)",
                (ctx.qq, ctx.origin, cls, tier, now, now + action["dur"]))
            self.conn.commit()
            self.confirm.drop(ctx.origin, ctx.qq)
            dur = action["dur"]
            return (f"✅【{cname} T{tier}】已下单（普通），约 {dur//60} 分钟出蓝图，"
                    f"完成自动推送。/nw研究状态")
        # 快捷：立即抽
        cost = action["express"]
        miss = _missing_res(p, cost)
        if miss:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 资源已不足：缺 " + "、".join(miss)
        self._deduct(ctx.qq, cost)
        result = research.roll_gacha(self.conn, self.mc, ctx.qq, cls, tier)
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        return research.gacha_text(self.mc, cls, tier, result)

    def _action_frag_exchange(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消兑换。"
        if choice != "1":
            return "回复 1 确认　2 取消"
        ok, payload = research.exchange(self.conn, self.mc, ctx.qq,
                                        action["cls"], action["tier"], action["name"])
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        if not ok:
            return f"❌ {payload}"
        m = payload
        return f"🧩 兑换成功：获得新蓝图【{RARITY_MARK[m['rarity']]}·{m['name']} T{m['tier']}】！"

    def _action_save_design(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消，设计未保存。"
        if choice != "1":
            return "回复 1 保存　2 取消"
        if self.conn.execute("SELECT 1 FROM designs WHERE qq=? AND name=?",
                             (ctx.qq, action["name"])).fetchone():
            self.confirm.drop(ctx.origin, ctx.qq)
            return f"❌ 设计名【{action['name']}】已存在"
        self.conn.execute(
            "INSERT INTO designs(qq,name,ship_class,tier,modules,stats_json,cost_json,work_ticks)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ctx.qq, action["name"], action["cls"], action["tier"],
             json.dumps(action["modules"], ensure_ascii=False),
             json.dumps(action["stats"], ensure_ascii=False),
             json.dumps(action["cost"], ensure_ascii=False), action["work_ticks"]))
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        return f"✅ 设计【{action['name']}】已保存！/nw生产 {action['name']} <数量> 下船台"

    def _action_produce(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消。"
        if choice != "1":
            return "回复 1 确认　2 取消"
        p = self._player(ctx.qq)
        miss = _missing_res(p, action["cost"])
        if miss:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 资源已不足：缺 " + "、".join(miss)
        yard = research.building_level(self.conn, action["x"], action["y"], "shipyard")
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM production_queue WHERE qq=?", (ctx.qq,)).fetchone()["c"]
        if active >= yard:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 船台刚被占满，报价失效。"
        self._deduct(ctx.qq, action["cost"])
        self.conn.execute(
            "INSERT INTO production_queue(qq,origin,x,y,design_id,qty,start_tick,end_tick)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ctx.qq, ctx.origin, action["x"], action["y"], action["design_id"],
             action["qty"], meta_get(self.conn, "econ_tick", int, 0), action["end_tick"]))
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        d = self.conn.execute("SELECT name FROM designs WHERE id=?",
                              (action["design_id"],)).fetchone()
        return f"🚢【{d['name']}】×{action['qty']} 已上船台，完工自动推送。/nw船坞 查看"

    # ---------- 共用工具 ----------
    def _deduct(self, qq: str, cost: dict):
        for k, v in cost.items():
            if v:
                self.conn.execute(f"UPDATE players SET {k}=COALESCE({k},0)-? WHERE qq=?",
                                  (v, qq))
