"""指令 handler（P0 经济建造 + P1 研究抽卡/设计器/船坞）

handler 统一签名：async def h(ctx) -> str
ctx 字段：conn, cfg, confirm(ConfirmStore), origin, qq, nick, args(list[str]), raw(str)
所有花钱/不可逆操作 → 返回报价文本并 confirm.put(action)，回复数字后经 execute_action 落地。
"""
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass

from . import spawn, pools, research, fleet as fleetm, aiworld, combat, webauth, vision
from .db import meta_get

logger = logging.getLogger("naval")

PREFIX_HINT = "前缀 /nw（或 /海战）"

# 指令表：命令名 → (方法名, 是否开放, 别名, 简介)
COMMANDS = {
    "注册": ("register", True, [], "开局注册势力 <名称> [Web口令]"),
    "帮助": ("help", True, ["菜单", "help"], "查看指令表"),
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
    # —— P2b §4：攻防任务 ——
    "驻防": ("garrison", True, [], "驻防当前格（受击减免+自动迎击）"),
    "巡逻": ("patrol", True, [], "巡逻阵位 <舰队> <中心> <半径>（揭雾）"),
    "反潜": ("asw_patrol", True, [], "反潜巡逻 <舰队> <中心> <半径>"),
    "伏击": ("ambush", True, [], "潜艇静默伏击阵位 <舰队> [坐标]"),
    "破交": ("raid", True, [], "破交 <舰队> <中心> <半径>（劫掠商船，涨恶名）"),
    "封锁": ("blockade", True, [], "封锁 <舰队> <敌岛x,y>（压港断商，不占岛）"),
    "护航": ("escort", True, [], "护航 <舰队> <中心x,y> [半径]（拦截袭击者）"),
    # —— P2b+ 占位 ——
    "登陆": ("landing", True, [], "登陆夺岛 <运兵舰队> <敌岛x,y>（需同格无敌舰）"),
    "支援": ("support", True, [], "支援 <battle_id> [舰队]（§27.4 援军入列）"),
    # —— P2b §26.1：外交 ——
    "外交": ("diplomacy", True, [], "外交 [势力]（关系权威表总览）"),
    "宣战": ("declare_war", True, [], "宣战 <势力>（改为敌对，可攻击）"),
    "媾和": ("make_peace", True, [], "媾和 <势力|玩家>（改回和平）"),
    # —— P2b §6.2：同盟 ——
    "同盟": ("alliance", True, [], "同盟 <玩家>（共享视野 + 自动协防，双向）"),
    "解盟": ("break_alliance_cmd", True, [], "解盟 <玩家>（解除同盟回到和平）"),
    # —— P2b §26.4/§11：打捞 ——
    "打捞": ("salvage_cmd", True, [], "打捞 [舰队] [坐标]（雲墨残骸 / 沉船遗迹）"),
    # —— P2b §19.1：指挥官/舰长 ——
    "指挥官": ("captains_cmd", True, [], "指挥官（舰长名册与任职情况）"),
    "招募舰长": ("recruit_captain", True, [], "招募舰长（§15.3 海军学院）"),
    "任命": ("appoint_captain", True, [], "任命 <舰长> <舰名>（提供技能加成）"),
    "免职": ("dismiss_captain", True, [], "免职 <舰长>（转为待命）"),
    # —— P2b §18.5：电力 ——
    "电力": ("power_cmd", True, [], "电力 [坐标]（岛屿电网供需与拉闸）"),
    # —— P2b §27.4：旁观 ——
    "观战": ("spectate", True, [], "观战 [战斗编号]（§27.4 中立旁观，不参战无收益）"),
    # —— P2b §6.2：外交扩展 ——
    "贸易": ("trade_cmd", True, [], "贸易 [挂单|市场|接受|拒绝|撤单] ...（§6.2 资源贸易与市场）"),
    "加急": ("rush_cmd", True, ["加速", "催工"], "加急：花额外资源立即完成已排队的建造/生产/研究"),
    "租港": ("lease_cmd", True, [], "租港 <盟友> <坐标>（§6.2 军港租借）"),
    "间谍": ("spy_cmd", True, [], "间谍 <玩家> <行动>（§6.2 破坏/窃取/煽动）"),
    "索赔": ("reparations_cmd", True, [], "索赔 <玩家> <金额> [坐标...]（§6.2 赔款割岛）"),
    "互不侵犯": ("nap_cmd", True, [], "互不侵犯 [玩家] [天数]（§6.2 条约，也可查看现有）"),
    "附庸": ("vassalize_cmd", True, [], "附庸 <玩家>（§6.2 需实力达对方 2 倍）"),
    "解约": ("break_treaty_cmd", True, [], "解约 <玩家>（§6.2 撕毁条约，承担违约恶名）"),
    "中立": ("neutral_cmd", True, [], "中立 [退出]（§6.2 中立观察国：不可侵犯但也不能打人）"),
    # —— P2b §26.2：雇佣 ——
    "雇佣": ("hire", True, [], "雇佣 [天数]（与附近的雇佣军团签约）"),
    "合同": ("contracts", True, [], "合同（查看在役雇佣合同）"),
    # —— P2b §26.2：协会任务 ——
    "任务": ("missions", True, [], "任务（协会剿匪合同列表）"),
    "接单": ("accept_mission", True, [], "接单 <编号>（预付资金接任务）"),
    "交差": ("claim_mission", True, [], "交差 <编号>（领赏并涨声望）"),
    # —— P2b §18.6：税率 ——
    "税率": ("tax", True, [], "税率 [0|5|10|15|20]（资金倍率 ↔ 民心/日）"),
    # —— P2b §19.15：航母 ——
    "机库": ("hangar", True, [], "机库 [舰队]（航母机库与舰载机中队）"),
    "空袭": ("airstrike", True, [], "空袭 <航母舰队> <目标x,y>（舰载机波次出击）"),
    # —— P2b §27.4：协讨 ——
    "协讨": ("co_belligerent", True, [], "协讨 <battle_id> <舰队>（第三方参战，按伤害分赏）"),
    # —— P2b §4：水雷 ——
    "布雷": ("lay_mines", True, [], "布雷 <舰队> <坐标> [锚雷/音响雷/磁雷]"),
    "扫雷": ("sweep_mines", True, [], "扫雷 <舰队> <坐标>（清除敌方雷场）"),
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


def chart_data(conn, cfg: dict, qq: str, radius: int = 10, center: tuple = None):
    """结构化海图数据。QQ 侧 chart 指令与 Web 图形化海图共用，避免两套标记语义漂移。

    返回 None 表示未注册。坐标范围裁剪到地图边界内；
    islands.kind ∈ capital/own/other，pirates.kind ∈ base/patrol。
    战争迷雾（§1）：视野外的他方岛屿/海盗不出现在结果里；己方目标始终可见。

    center 可选：以指定坐标为中心（战报回放联动用），默认以首都为中心。
    注意迷雾仍按玩家真实视野计算——中心挪到远处时看到的确实是被迷雾遮住的区域。
    """
    p = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or p["capital_x"] is None:
        return None
    if center and center[0] is not None and center[1] is not None:
        cx, cy = int(center[0]), int(center[1])
    else:
        cx, cy = p["capital_x"], p["capital_y"]
    # 首都坐标与"视图中心"是两回事——中心可以被战报联动挪到别处，
    # 但 kind='capital' 必须只标自己真正的首都。
    capx, capy = p["capital_x"], p["capital_y"]
    x0, x1 = max(0, cx - radius), min(fleetm.MAP_SIZE - 1, cx + radius)
    y0, y1 = max(0, cy - radius), min(fleetm.MAP_SIZE - 1, cy + radius)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    sources = vision.vision_sources(conn, cfg, qq)

    d = {"center": [cx, cy], "x0": x0, "y0": y0, "w": w, "h": h,
         "map_size": fleetm.MAP_SIZE,
         "fog": vision.is_enabled(cfg),
         "vision": vision.visible_grid(sources, x0, y0, w, h),
         "islands": [], "pirates": [], "fleets": []}

    # ---- 势力图层：他方岛屿标注外交关系，己方岛屿标注是否飞地（§6.2 / §18.3）----
    from . import relations as _rel
    _rel_cache = {}

    def _rel_of(owner):
        if owner is None:
            return ""
        if owner not in _rel_cache:
            try:
                if owner == qq:
                    r = "self"
                elif _rel.is_neutral(conn, owner):
                    r = "protected"        # 中立观察国：打不得
                elif _rel.are_allied(conn, cfg, qq, owner):
                    r = "ally"
                else:
                    st = _rel.get_pvp_state(conn, cfg, qq, owner)
                    r = ("war" if st == _rel.WAR
                         else ("nap" if st == _rel.NAP else ""))
            except Exception:
                logger.exception("[海战模拟器] 海图求外交关系异常")
                r = ""
            _rel_cache[owner] = r
        return _rel_cache[owner]

    try:
        from .engine import connected_islands
        _conn_set = connected_islands(conn, cfg, qq)
    except Exception:
        logger.exception("[海战模拟器] 海图求航线连通异常")
        _conn_set = None

    for isl in conn.execute(
            "SELECT x,y,owner_qq,owner_kind FROM islands"
            " WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (x0, x1, y0, y1)).fetchall():
        if isl["x"] == capx and isl["y"] == capy:
            kind = "capital"
        elif isl["owner_qq"] == qq:
            kind = "own"          # 己方目标不受迷雾影响
        elif isl["owner_kind"] == "regular":
            kind = "regular"      # 正规军领土：已探明即可见
            if not vision.is_visible(sources, isl["x"], isl["y"]):
                continue
        else:
            kind = "other"
            if not vision.is_visible(sources, isl["x"], isl["y"]):
                continue
        item = {"x": isl["x"], "y": isl["y"], "kind": kind,
                "owner": isl["owner_qq"], "rel": _rel_of(isl["owner_qq"])}
        # 己方岛屿：是否飞地（无港链回首都 → 产出 ×0.5，§18.3 rtF）
        # 只标自己的岛——盟友的岛是否连通取决于他们的港链，与我的产出无关
        if _conn_set is not None and isl["owner_qq"] == qq:
            item["feat"] = (isl["x"], isl["y"]) not in _conn_set
        d["islands"].append(item)

    for pr in conn.execute(
            "SELECT x,y,comp_json,name,faction FROM ai_fleets"
            " WHERE faction IN ('pirate','neutral_merchant','enforcer','regular',"
            "'empire','guild','rebel','merc')"
            " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (x0, x1, y0, y1)).fetchall():
        if pr["faction"] == "enforcer":
            kind = "enforcer"        # §25.3 全服可见，不受迷雾遮蔽
        elif pr["faction"] == "empire":
            kind = "empire"          # §26.2 帝国航线「全图航标可见」
        elif not vision.is_visible(sources, pr["x"], pr["y"]):
            continue
        elif pr["faction"] == "neutral_merchant":
            kind = "merchant"
        elif pr["faction"] in ("regular", "guild", "rebel", "merc", "yunmo"):
            kind = pr["faction"]
        else:
            kind = "patrol" if json.loads(pr["comp_json"] or "[]") else "base"
        d["pirates"].append({"x": pr["x"], "y": pr["y"], "kind": kind,
                             "name": pr["name"], "faction": pr["faction"]})

    for f in fleetm.list_fleets(conn, qq):
        if f["x"] is not None and x0 <= f["x"] <= x1 and y0 <= f["y"] <= y1:
            d["fleets"].append({"id": f["id"], "name": f["name"], "x": f["x"], "y": f["y"]})
    # §26.4/§11 残骸（全服可见——残骸区是公开可抢的）
    try:
        from . import salvage as _sv
        d["wrecks"] = _sv.wrecks_near(conn, x0, x1, y0, y1)
    except Exception:
        logger.exception("[海战模拟器] 海图求残骸异常")
        d["wrecks"] = []
    # §6.2 租借军港（自己租来的）
    try:
        from . import diplomacy as _dip
        d["leases"] = [l for l in _dip.active_leases(conn, qq)
                       if x0 <= l["x"] <= x1 and y0 <= l["y"] <= y1]
    except Exception:
        logger.exception("[海战模拟器] 海图求租借港异常")
        d["leases"] = []
    return d


class GameCommands:
    def __init__(self, conn, cfg, confirm, design_store=None):
        self.conn, self.cfg, self.confirm, self.dstore = conn, cfg, confirm, design_store
        self.mc = pools.mod_data()  # 模块池配置

    def _player(self, qq):
        return self.conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()

    # ---------- 账号 ----------
    async def register(self, ctx: Ctx) -> str:
        """注册：/nw注册 <名称> [密码]

        密码可选。留空则账号没有 Web 口令，首次进 Web 时会被引导设置。
        """
        args = [a for a in ctx.args if a and a.strip()]
        name = (args[0].strip() if args else "") or ctx.nick or ctx.qq
        name = name[:12]
        password = args[1].strip() if len(args) > 1 else ""
        if password:
            pw_err = webauth.check_password_strength(password)
            if pw_err:
                return f"❌ Web 口令不合格：{pw_err}"

        result, err = spawn.create_capital(self.conn, self.cfg, ctx.qq, name)
        if err:
            return f"❌ {err}"
        player, isl = result

        if password:
            self.conn.execute(
                "UPDATE players SET web_pass=?, web_pass_at=? WHERE qq=?",
                (webauth.hash_password(password), int(time.time()), ctx.qq))
            self.conn.commit()

        ore = json.loads(isl["ore_json"])
        action = {"type": "register", "can_reroll": self.cfg["spawn"]["allow_reroll"]}
        self.confirm.put(ctx.origin, ctx.qq, action)
        others = self.conn.execute("SELECT COUNT(*) c FROM players WHERE qq!=?", (ctx.qq,)).fetchone()["c"]
        web_line = ("🔑 Web 口令已设置，可用 QQ 号登录网页版"
                    if password else
                    "🔑 未设置 Web 口令，首次登录网页版时会引导你设置")
        return (f"🌊 欢迎，提督【{name}】！\n"
                f"出生岛：({isl['x']},{isl['y']}) {_island_name(self.cfg, isl['itype'])}"
                f"｜{_ore_text(ore)}｜槽位 {self.cfg['island_types'][isl['itype']]['slots']}+D\n"
                f"🎁 新手包：钢800 油200 食物500 资金1000 + 总督府Lv1\n"
                f"{web_line}\n"
                f"🌍 当前海域已有 {others} 股势力\n"
                f"回复：1 看新手引导　2 直接开始　3 重掷出生点（仅1次）")

    async def help(self, ctx: Ctx) -> str:
        p0 = [f"/nw{c}" for c, (_, ok, _, _) in COMMANDS.items() if ok]
        later = [c for c, (_, ok, _, _) in COMMANDS.items() if not ok]
        web = (ctx.cfg or {}).get("web") or {}
        return ("⚓ 海战模拟器指令\n" + "　".join(p0) +
                "\n\n📎 斜杠可以省：`nw资源` 与 `/nw资源` 等效（`海战资源` 也行）\n"
                "例：nw研究 驱逐舰 T3｜nw建造 研究所｜nw设计 → nw护卫舰 → nw组装 海圻号 1 5 8" +
                self._web_pitch(web) +
                "\n\n后续开放：" + "、".join(later))

    @staticmethod
    def _web_pitch(web: dict) -> str:
        """nw帮助 里推荐网页版。地址由插件配置 web_public_url 提供。"""
        if not web or not web.get("enable", True):
            return ""
        url = (web.get("public_url") or "").strip()
        port = web.get("port", 8090)
        head = "\n\n🖥️ 网页版（推荐）：鼠标点选即可，不用记指令，手机也能玩"
        if url:
            return f"{head}\n{url}\n用 QQ 号 + 注册时设的口令登录"
        return f"{head}\n本机 http://127.0.0.1:{port}（未配置对外地址）\n用 QQ 号 + 注册时设的口令登录"

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
        infamy = int(p["infamy"] or 0)
        ecfg = self.cfg.get("enforcer") or {}
        from .engine import tax_bill, morale_factor, admin_capacity, admin_usage, corruption_rate
        from . import events as _ev
        bill = tax_bill(self.cfg, p["tax_rate"])
        tax_line = (f"  税率 {bill.get('name', '')}　民心系数 ×{morale_factor(p['morale']):.2f}")
        ev_txt = _ev.summary(self.conn, self.cfg, meta_get(self.conn, "econ_tick", int, 0))
        if ev_txt:
            tax_line += f"\n📢 生效事件：{ev_txt}"
        # §14.1 行政力与腐败
        cap_u = admin_capacity(self.conn, self.cfg, ctx.qq)
        use_u = admin_usage(self.conn, self.cfg, ctx.qq)
        k = corruption_rate(self.conn, self.cfg, ctx.qq)
        admin_line = (f"🏛 行政 {use_u:.0f}/{cap_u:.0f}"
                      + (f"　🔥 腐败 {k * 100:.0f}%（资金/食物 −{k * 100:.0f}%，"
                         f"控制度增长减半）" if k > 0 else "　腐败 0%"))
        # §25.3 执法者状态行
        if infamy >= int(ecfg.get("trigger_infamy", 10)):
            lv = aiworld.enforcer_level(self.cfg, infamy)
            hunt = self.conn.execute(
                "SELECT name,x,y FROM ai_fleets WHERE faction='enforcer'"
                " AND mission LIKE ?", (f'%"{ctx.qq}"%',)).fetchone()
            if hunt:
                threat = (f"\n🚨 跨国执法舰队 L{lv} 正在追击你："
                          f"【{hunt['name']}】({hunt['x']},{hunt['y']})")
            else:
                threat = f"\n🚨 恶名 {infamy} 已达执法阈值（L{lv}），随时可能被找上门"
        else:
            threat = (f"\n🕊 距执法者触发还差 {max(0, int(ecfg.get('trigger_infamy', 10)) - infamy)}"
                      f" 点恶名")
        return (f"⚓ {p['name']}　排名 #{rank}/{total}　恶名 {infamy}\n"
                f"🏝 岛屿 {isl_count}　❤ 民心 {p['morale']:.0f}　🚢 舰船 {ship_count}\n"
                + admin_line + "\n"
                f"📦 钢{p['steel']:.0f} 油{p['oil']:.0f} 食物{p['food']:.0f} "
                f"补给{p['supply']:.0f} 人力{p['manpower']:.0f}\n"
                f"💰 资金{p['money']:.0f}　🔬 科研{p['science']:.0f}　"
                f"铝{p['aluminium']:.0f} 稀土{p['rare_earth']:.0f}\n"
                f"🛠 建造 {bq}　🧪 研究 {rq}　首都 ({p['capital_x']},{p['capital_y']})"
                + threat + tax_line)

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
        """返回 (costs{steel,oil,money}, work_ticks)。逻辑已抽到 gov.py 供 Web 共用。"""
        from . import gov as _gov
        return _gov.building_cost(self.cfg, def_id, target_lv)

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
        from . import gov as _gov
        rmul = _gov.rush_mult(self.cfg)
        rcost = _gov._rush_cost(self.cfg, costs)
        rextra = "、".join(f"{RES_NAME[k]}{rcost[k] - costs[k]}"
                           for k in costs if rcost[k] > costs[k]) or "无"
        hint = (f"\n回复：1 确认　2 取消"
                + (f"　3 ⚡加急（多付 {rextra}，立即完成）"
                   if _gov.rush_enabled(self.cfg) else ""))
        return (f"🔧 准备{'升级' if existing else '建造'}【{bname} Lv{target_lv}】({x},{y})\n"
                f"消耗：钢{costs['steel']} 油{costs['oil']} 资金{costs['money']}\n"
                f"工时 {work} tick（约 {_gov.fmt_mins(mins)}）\n{bdef['desc']}"
                f"{hint}")

    async def rush_cmd(self, ctx: Ctx) -> str:
        """加急：花额外资源立即完成已排队的建造/生产/研究（给时间不多的玩家）。"""
        from . import gov as _gov
        if not self._player(ctx.qq):
            return "❌ 先 /nw注册"
        snap = _gov.queue_snapshot(self.conn, self.cfg, ctx.qq)
        if not snap.get("ok"):
            return "❌ 查询失败"
        if not _gov.rush_enabled(self.cfg):
            return "❌ 本服未开启加急"
        allq = ([("建造", b) for b in snap["builds"]]
                + [("生产", p) for p in snap["productions"]]
                + [("研究", r) for r in snap["researches"]])
        if not ctx.args:
            if not allq:
                return "📭 没有正在排队的项目，无需加急。"
            out = [f"⚡ 可加急的项目（多付 {int((_gov.rush_mult(self.cfg)-1)*100)}% 资源立即完成）："]
            for kind, q in allq:
                if kind == "研究":
                    out.append(f"　[{kind}] #{q['id']} {q['cls_name']} T{q['tier']}"
                               f"　剩 {q['left_text']}　加急费 科研{q['extra']['science']}")
                else:
                    nm = q["name"] + (f" Lv{q['target_lv']}" if "target_lv" in q else
                                      f" ×{q.get('qty',1)}")
                    out.append(f"　[{kind}] #{q['id']} {nm}　剩 {q['left_text']}"
                               f"　加急费 {q['extra_text']}")
            out.append("")
            out.append("加急：/nw加急 <编号>　或 /nw加急 建造|生产|研究 <编号>")
            return "\n".join(out)
        kind, qid = None, None
        if len(ctx.args) == 1:
            try:
                qid = int(ctx.args[0].lstrip("#"))
            except (TypeError, ValueError):
                return "❌ 编号应为数字。用法：/nw加急 <编号>"
            for k, q in allq:
                if q["id"] == qid:
                    kind = {"建造": "build", "生产": "production",
                            "研究": "research"}[k]
                    break
            if kind is None:
                return f"❌ 队列里没有 #{qid}"
        else:
            kmap = {"建造": "build", "生产": "production", "研究": "research"}
            kind = kmap.get(ctx.args[0])
            if not kind:
                return "❌ 类型应为 建造/生产/研究。用法：/nw加急 建造 <编号>"
            try:
                qid = int(ctx.args[1].lstrip("#"))
            except (TypeError, ValueError):
                return "❌ 编号应为数字"
        ok, text = _gov.rush_queue(self.conn, self.cfg, ctx.qq, kind, qid)
        return text

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
        from . import gov as _gov
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
        # 拥有状态必须现查——catalog 是「浏览目录那一刻」的快照，
        # 玩家很可能看完目录又去抽了几发蓝图，用快照会误报「还未拥有」。
        owned_now = pools.owned_ids(self.conn, ctx.qq)
        chosen, seen_slot = {}, set()
        for n in picks:
            e = catalog[n - 1]
            if e["id"] not in owned_now:
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
                f"单舰工时：约 {_gov.fmt_mins(mins)}\n"
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
        from . import gov as _gov
        hint = "\n回复：1 确认　2 取消"
        if _gov.rush_enabled(self.cfg):
            rc = _gov._rush_cost(self.cfg, cost)
            extra = "、".join(f"{RES_NAME[k]}{rc[k] - cost[k]}"
                              for k in cost if rc.get(k, 0) > cost[k]) or "无"
            hint += f"　3 ⚡加急（多付 {extra}，立即下水）"
        return (f"🚢 准备在 ({x},{y}) 船坞建造【{d['name']}】×{qty}\n"
                f"消耗：{_cost_text(cost)}\n工时 {work} tick（约 {_gov.fmt_mins(mins)}）"
                f"{hint}")

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
        # §19.1 舰员经验：把已有舰船的舰员状态一并列出
        from . import crew as _crew
        ships = self.conn.execute(
            "SELECT * FROM ships WHERE qq=? ORDER BY crew_exp DESC, id LIMIT 12",
            (ctx.qq,)).fetchall()
        if ships:
            lines.append("")
            lines.append("🎗 舰员状态（§19.1 新兵/老练/王牌）：")
            for s in ships:
                lines.append(f"· {s['name']}　{_crew.describe(self.conn, self.cfg, s)}")
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
        """§26.4 对雲墨的攻击需显式确认（战力评估 + 防误触短语）。"""
        ycfg = self.cfg.get("yunmo") or {}
        phrase = ycfg.get("confirm_phrase", "确认攻击雲墨")
        # 先剥离防误触短语，否则坐标解析会被它带歪
        args = [a for a in ctx.args if a != phrase]
        raw_ok = phrase in (ctx.raw or "")
        if len(args) >= 2:
            xy = fleetm.parse_xy(" ".join(args[1:]))
            if xy:
                tx, ty = xy
                y = self.conn.execute(
                    "SELECT * FROM ai_fleets WHERE faction='yunmo' AND comp_json!='[]'"
                    " AND x=? AND y=?", (tx, ty)).fetchone()
                if y and not raw_ok:
                    return self._yunmo_pitch(ctx, y, tx, ty)
                if y and raw_ok:
                    # §26.4「打了就是全面战争」：确认短语即等同宣战
                    from . import relations
                    relations.set_state(
                        self.conn, ctx.qq, "yunmo", relations.WAR,
                        meta_get(self.conn, "war_tick", int, 0), "确认攻击雲墨")
                    ctx.args = args
                    head = await self._move_like(ctx, "attack")
                    return ("☢️ 已向雲墨宣战——全面战争开始。\n"
                            "（§26.4：它不先打你，但打了就没有退路）\n" + head)
        ctx.args = args
        return await self._move_like(ctx, "attack")

    def _yunmo_pitch(self, ctx: Ctx, y, tx: int, ty: int) -> str:
        """§26.4 交战门槛：战力评估 + 需输入「确认攻击雲墨」。"""
        from . import pools
        ycfg = self.cfg.get("yunmo") or {}
        phrase = ycfg.get("confirm_phrase", "确认攻击雲墨")
        mine = 0.0
        for f in self.conn.execute("SELECT * FROM fleets WHERE qq=?", (ctx.qq,)).fetchall():
            for u in combat._player_units(self.conn, f["id"]):
                mine += float(u.get("fire", 0) or 0) + float(u.get("torpedo", 0) or 0)
        theirs = aiworld._comp_power(json.loads(y["comp_json"] or "[]"))
        ratio = (theirs / mine) if mine > 0 else 999.0
        # 未确认时不下单
        warn = ""
        if ratio >= float(ycfg.get("power_ratio_warn", 30)):
            warn = (f"\n【极度不推荐】敌方战力评估 ≈ 你方当前舰队的 "
                    f"{ratio:.0f} 倍以上。")
        else:
            warn = f"\n⚠️ 敌方战力评估 ≈ 你方的 {ratio:.0f} 倍。"
        return (f"☢️ 目标：{y['name']}（({tx},{ty})）\n"
                f"雲墨为赛季级世界事件，**绝不主动攻击**——它不先打你，"
                f"但打了就是全面战争。\n"
                + warn +
                f"\n§26.4 交战门槛：两道 L50 实验护航队会优先接战，"
                f"不打掉屏护，火力 80% 打在护航身上。\n"
                f"若你确定要打，请把指令写成：\n"
                f"　　/nw攻击 <舰队> {tx},{ty} {phrase}\n"
                f"（必须带这句防误触短语；带短语即视为宣战）")

    # ---------- P2b §4 攻防任务（驻防/巡逻/反潜/伏击） ----------
    def _station_fleet(self, f, mtype: str, x: int, y: int) -> str:
        """把舰队切到原地驻留姿态（驻防/伏击），返回给玩家的文案。"""
        if mtype == "ambush" and not fleetm.is_subs_only(self.conn, f["id"]):
            return ("❌ 伏击阵位只能由潜艇舰队执行（当前编队含水面舰）。"
                    "先把水面舰移出：/nw编队 <舰队> 移出 <舰名…>")
        tick = meta_get(self.conn, "war_tick", int, 0)
        m = {"type": mtype, "x": x, "y": y, "since_tick": tick}
        self.conn.execute("UPDATE fleets SET x=?,y=?,mission=? WHERE id=?",
                          (x, y, json.dumps(m, ensure_ascii=False), f["id"]))
        self.conn.commit()
        task = self.cfg.get("task") or {}
        if mtype == "garrison":
            pct = int(float(task.get("garrison_defense", 0.25)) * 100)
            return (f"🛡【{f['name']}】已在({x},{y})驻防。\n"
                    f"受击伤害 −{pct}%；同格遇敌自动迎击。\n"
                    f"解除：发 /nw移动 <舰队> <坐标>")
        pct = int(float((self.cfg.get("submarine") or {}).get("ambush_bonus", 0.2)) * 100)
        return (f"🦈【{f['name']}】已在({x},{y})进入静默伏击阵位。\n"
                f"潜艇保持深潜待机，敌方声呐很难发现；接敌首波鱼雷 +{pct}% 命中。\n"
                f"解除：发 /nw移动 <舰队> <坐标>")

    async def garrison(self, ctx: Ctx) -> str:
        """§4 驻防 <舰队> [坐标]：默认当前格。"""
        if not ctx.args:
            return "用法：/nw驻防 <舰队> [x,y]（省略坐标=当前所在格）"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能驻防"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，先 /nw撤退"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:])) if len(ctx.args) > 1 else None
        x, y = xy if xy else (f["x"], f["y"])
        return self._station_fleet(f, "garrison", x, y)

    async def ambush(self, ctx: Ctx) -> str:
        """§4 伏击 <潜艇舰队> [坐标]：静默阵位，首波 +20%。"""
        if not ctx.args:
            return "用法：/nw伏击 <潜艇舰队> [x,y]（省略坐标=当前所在格）"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能设伏"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，先 /nw撤退"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:])) if len(ctx.args) > 1 else None
        x, y = xy if xy else (f["x"], f["y"])
        return self._station_fleet(f, "ambush", x, y)

    def _patrol_like(self, ctx: Ctx, mtype: str) -> str:
        """巡逻 / 反潜：<舰队> <中心x,y> <半径>。"""
        label = "巡逻" if mtype == "patrol" else "反潜巡逻"
        if len(ctx.args) < 3:
            return f"用法：/nw{label} <舰队> <中心x,y> <半径>\n例：/nw{label} 一游 234,567 5"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能出动"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，要脱离请 /nw撤退"
        ms = fleetm.mission_of(f)
        if ms.get("type") in ("move", "attack"):
            return "❌ 舰队正在机动中，到点后再下新命令"
        xy = fleetm.parse_xy(ctx.args[1])
        if xy is None:
            return "❌ 中心坐标格式不对，应如 234,567（0~999）"
        try:
            radius = int(ctx.args[2])
        except ValueError:
            return "❌ 半径要填整数（建议 2~10）"
        if not 1 <= radius <= 20:
            return "❌ 半径范围 1~20"
        cx, cy = xy
        tick = meta_get(self.conn, "war_tick", int, 0)
        m = {"type": mtype, "cx": cx, "cy": cy, "radius": radius,
             "phase": "approach", "since_tick": tick}
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), f["id"]))
        self.conn.commit()
        dist = max(abs(f["x"] - cx), abs(f["y"] - cy))
        task = self.cfg.get("task") or {}
        if mtype == "patrol":
            base = int((self.cfg.get("fog") or {}).get("fleet_vision", 6))
            eff = max(base, radius + int(task.get("patrol_vision_bonus", 2)))
            extra = (f"阵位视野约 {eff} 格（巡航舰基础视野 {base}，"
                     f"阵位半径越大揭雾越广）")
        else:
            extra = (f"对潜探测 +{int(float(task.get('asw_detect_bonus', 0.5)) * 100)}%"
                     f"（更容易抓到潜艇）")
        if dist <= radius:
            head = f"🔍【{f['name']}】已在({cx},{cy})半径 {radius} 格内就位。"
        else:
            head = (f"🔍【{f['name']}】驶向({cx},{cy})，抵达后转入半径 "
                    f"{radius} 格{label}阵位。")
        return f"{head}\n{extra}\n解除：/nw移动 <舰队> <坐标>"

    async def patrol(self, ctx: Ctx) -> str:
        return self._patrol_like(ctx, "patrol")

    async def asw_patrol(self, ctx: Ctx) -> str:
        return self._patrol_like(ctx, "asw")

    # ---------- P2b §4：破交 / 封锁 / 护航 ----------
    async def raid(self, ctx: Ctx) -> str:
        """破交 <舰队> <中心> <半径>：在指定海域主动寻歼中立商船。"""
        if len(ctx.args) < 3:
            return "用法：/nw破交 <舰队> <中心x,y> <半径>\n例：/nw破交 一游 234,567 6"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能出动"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，要脱离请 /nw撤退"
        if fleetm.mission_of(f).get("type") in ("move", "attack"):
            return "❌ 舰队正在机动中，到点后再下新命令"
        xy = fleetm.parse_xy(ctx.args[1])
        if xy is None:
            return "❌ 中心坐标格式不对，应如 234,567（0~999）"
        try:
            radius = int(ctx.args[2])
        except ValueError:
            return "❌ 半径要填整数（建议 3~10）"
        if not 1 <= radius <= 20:
            return "❌ 半径范围 1~20"
        cx, cy = xy
        tick = meta_get(self.conn, "war_tick", int, 0)
        m = {"type": "raid", "cx": cx, "cy": cy, "radius": radius,
             "phase": "approach", "since_tick": tick}
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), f["id"]))
        self.conn.commit()
        inf = int((self.cfg.get("merchant") or {}).get("infamy_per_ship", 3))
        return (f"🏴‍☠️【{f['name']}】已受命在({cx},{cy})半径 {radius} 格海域破交。\n"
                f"航行途中持续揭雾；进入半径后主动拦截中立商船。\n"
                f"⚠️ 每次洗劫 恶名 +{inf}，恶名过高会招来执法者。\n"
                f"解除：/nw移动 <舰队> <坐标>")

    async def blockade(self, ctx: Ctx) -> str:
        """封锁 <舰队> <敌岛x,y>：压港断商，不占岛。"""
        if len(ctx.args) < 2:
            return "用法：/nw封锁 <舰队> <敌岛x,y>\n例：/nw封锁 一游 234,567"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能执行封锁"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，要脱离请 /nw撤退"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:]))
        if xy is None:
            return "❌ 坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        isl = self.conn.execute("SELECT x,y,owner_qq FROM islands WHERE x=? AND y=?",
                                (tx, ty)).fetchone()
        if not isl:
            return f"❌ ({tx},{ty}) 没有岛屿"
        if isl["owner_qq"] == ctx.qq:
            return "❌ 不能封锁自己的岛"
        tick = meta_get(self.conn, "war_tick", int, 0)
        radius = max(1, int((self.cfg.get("merchant") or {}).get("blockade_radius", 3)))
        m = {"type": "blockade", "cx": tx, "cy": ty, "radius": radius,
             "target_x": tx, "target_y": ty, "phase": "approach", "since_tick": tick}
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), f["id"]))
        self.conn.commit()
        loss = int((self.cfg.get("trade") or {}).get("blockade_security_loss", 6))
        owner = isl["owner_qq"] or "中立"
        return (f"🚧【{f['name']}】已受命封锁 ({tx},{ty})。\n"
                f"舰队将驶往该岛周边并压制出入商船；\n"
                f"目标岛主（{owner}）航线安全度每战争 tick −{loss}，收入随之下降。\n"
                f"封锁不占岛，撤离即解除。\n"
                f"解除：/nw移动 <舰队> <坐标>")

    async def escort(self, ctx: Ctx) -> str:
        """护航 <舰队> <x,y> [半径]：挂接运输线，拦截袭击者并恢复航线安全度。"""
        if len(ctx.args) < 2:
            return "用法：/nw护航 <舰队> <航线中心x,y> [半径]\n例：/nw护航 一游 234,567 5"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能执行护航"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，要脱离请 /nw撤退"
        xy = fleetm.parse_xy(ctx.args[1])
        if xy is None:
            return "❌ 航线中心坐标格式不对，应如 234,567（0~999）"
        radius = 5
        if len(ctx.args) > 2:
            try:
                radius = int(ctx.args[2])
            except ValueError:
                return "❌ 半径要填整数（建议 3~10）"
        if not 1 <= radius <= 20:
            return "❌ 半径范围 1~20"
        cx, cy = xy
        tick = meta_get(self.conn, "war_tick", int, 0)
        m = {"type": "escort", "cx": cx, "cy": cy, "radius": radius,
             "phase": "approach", "since_tick": tick}
        self.conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                          (json.dumps(m, ensure_ascii=False), f["id"]))
        self.conn.commit()
        gain = int((self.cfg.get("trade") or {}).get("escort_security_gain", 8))
        return (f"🛡【{f['name']}】已受命护航 ({cx},{cy}) 半径 {radius} 格航线。\n"
                f"拦截半径内的袭击者（海盗等）；每战争 tick 航线安全度 +{gain}。\n"
                f"解除：/nw移动 <舰队> <坐标>")

    # ---------- P2b §4/§24.2：登陆夺岛 ----------
    def _fleet_transports(self, fleet_id: int) -> int:
        """舰队里的运兵船数量（transport 舰种）。"""
        n = 0
        for r in self.conn.execute("SELECT def_id FROM ships WHERE fleet_id=?", (fleet_id,)):
            if str(r["def_id"]).isdigit():
                d = self.conn.execute("SELECT ship_class FROM designs WHERE id=?",
                                      (int(r["def_id"]),)).fetchone()
                if d and d["ship_class"] == "transport":
                    n += 1
        return n

    def _enemy_fleet_on(self, qq: str, x: int, y: int):
        """同格的敌对 AI 舰队（§24.2「需同格无敌舰」）。"""
        return self.conn.execute(
            "SELECT id,name FROM ai_fleets WHERE x=? AND y=? AND faction IN"
            " ('pirate','enforcer','regular') AND comp_json!='[]' LIMIT 1",
            (x, y)).fetchone()

    def _island_defense(self, isl) -> int:
        lcfg = self.cfg.get("landing") or {}
        return (int(lcfg.get("defense_base", 60))
                + int(lcfg.get("defense_per_dev", 50)) * int(isl["dev_level"] or 1))

    async def landing(self, ctx: Ctx) -> str:
        """登陆 <运兵舰队> <敌岛x,y>：需同格无敌舰，二次确认（高损失警告）。"""
        if len(ctx.args) < 2:
            return "用法：/nw登陆 <运兵舰队> <敌岛x,y>\n例：/nw登陆 登陆队 234,567"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.in_battle(self.conn, f["id"]):
            return "❌ 舰队正在交战，无法组织登陆"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:]))
        if xy is None:
            return "❌ 坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        isl = self.conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (tx, ty)).fetchone()
        if not isl:
            return f"❌ ({tx},{ty}) 没有岛屿"
        if isl["owner_qq"] == ctx.qq:
            return "❌ 那是你自己的岛"
        if (f["x"], f["y"]) != (tx, ty):
            return (f"❌ 舰队在({f['x']},{f['y']})，登陆必须与目标岛同格。\n"
                    f"先 /nw移动 {f['name']} {tx},{ty}")
        n_tr = self._fleet_transports(f["id"])
        if n_tr <= 0:
            return "❌ 舰队里没有运兵船（transport），无法投送陆战队。"
        foe = self._enemy_fleet_on(ctx.qq, tx, ty)
        if foe:
            return (f"❌ 同格还有敌方舰队【{foe['name']}】，必须先清海才能登陆。\n"
                    f"（§24.2：需同格无敌舰）")
        p = self._player(ctx.qq)
        lcfg = self.cfg.get("landing") or {}
        need = int(lcfg.get("manpower_per_transport", 80)) * n_tr
        if p["manpower"] < need:
            return f"❌ 人力不够：需 {need}，现有 {p['manpower']:.0f}"
        defense = self._island_defense(isl)
        power = int(lcfg.get("assault_per_transport", 140)) * n_tr
        hp = float(isl["hp"] or 0)
        eff = max(1, power - defense // 2)
        dmg = min(eff, hp)
        will_capture = dmg >= hp
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "landing", "fid": f["id"], "tx": tx, "ty": ty,
                          "transports": n_tr, "manpower": need})
        owner = isl["owner_qq"] or "中立"
        head = "🎯 预计本次即可占领" if will_capture else f"🎯 预计削耐久 {int(dmg)}"
        return (f"⚔️ 登陆 ({tx},{ty}) 岛　守方：{owner}\n"
                f"运兵船 {n_tr} 艘｜突击力 {power}｜岛屿防御 {defense}\n"
                f"岛屿耐久 {hp:.0f}｜{head}\n"
                f"消耗 人力{need}\n"
                f"⚠️ 高损失警告：每艘运兵船约 "
                f"{int(float(lcfg.get('transport_loss_chance', 0.25)) * 100)}% 概率战损；"
                f"守方防御越高伤亡越大。\n"
                f"回复 1 强行登陆　2 取消")

    async def _action_landing(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice != "1":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消登陆。"
        lcfg = self.cfg.get("landing") or {}
        self.confirm.drop(ctx.origin, ctx.qq)
        f = self.conn.execute("SELECT * FROM fleets WHERE id=?", (action["fid"],)).fetchone()
        tx, ty = action["tx"], action["ty"]
        if not f:
            return "❌ 舰队已不存在"
        isl = self.conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (tx, ty)).fetchone()
        if not isl or isl["owner_qq"] == ctx.qq:
            return "❌ 目标岛状态已变化，登陆取消"
        if (f["x"], f["y"]) != (tx, ty):
            return "❌ 舰队已离开目标海域，登陆取消"
        foe = self._enemy_fleet_on(ctx.qq, tx, ty)
        if foe:
            return f"❌ 敌方舰队【{foe['name']}】已进入海域，登陆取消"

        n_tr = action["transports"]
        p = self._player(ctx.qq)
        need = int(action["manpower"])
        if p["manpower"] < need:
            return f"❌ 人力不足（需 {need}，现有 {p['manpower']:.0f}），登陆取消"
        self.conn.execute("UPDATE players SET manpower=COALESCE(manpower,0)-? WHERE qq=?",
                          (need, ctx.qq))

        defense = self._island_defense(isl)
        power = int(lcfg.get("assault_per_transport", 140)) * n_tr
        dmg = max(1, power - defense // 2)
        hp = float(isl["hp"] or 0) - dmg

        # 运兵船战损（§24.2 高损失）
        lost = 0
        chance = float(lcfg.get("transport_loss_chance", 0.25))
        trs = []
        for r in self.conn.execute("SELECT id,def_id FROM ships WHERE fleet_id=?", (f["id"],)):
            if str(r["def_id"]).isdigit():
                d = self.conn.execute("SELECT ship_class FROM designs WHERE id=?",
                                      (int(r["def_id"]),)).fetchone()
                if d and d["ship_class"] == "transport":
                    trs.append(r["id"])
        for sid in trs:
            if random.random() < chance:
                self.conn.execute("DELETE FROM ships WHERE id=?", (sid,))
                lost += 1

        if hp <= 0:
            ratio = float(lcfg.get("capture_hp_ratio", 0.35))
            max_hp = max(100, int((hp + dmg) * ratio))
            conn_result = self.conn.execute(
                "UPDATE islands SET owner_qq=?,owner_kind='player',control=?,morale=?,hp=?"
                " WHERE x=? AND y=?",
                (ctx.qq, int(lcfg.get("capture_control", 20)), 60, max_hp, tx, ty))
            self.conn.commit()
            if not conn_result.rowcount:
                return "❌ 占领失败（岛屿状态变化）"
            control = int(lcfg.get("capture_control", 20))
            gain = int(lcfg.get("control_gain_per_day", 3))
            return (f"🏴 登陆成功！({tx},{ty}) 已被你占领。\n"
                    f"歼灭守军，运兵船战损 {lost}/{n_tr} 艘，消耗人力 {need}。\n"
                    f"⚠️ 控制度仅 {control}（每日自然 +{gain}）——§14.2：\n"
                    f"控制度低于 30 有叛乱风险，低于 10 会直接易帜。\n"
                    f"夺岛不瞬间收益，请留下驻军或舰队守过渡期。")
        self.conn.execute("UPDATE islands SET hp=?, morale=MAX(0,COALESCE(morale,80)-5)"
                          " WHERE x=? AND y=?", (round(hp, 1), tx, ty))
        self.conn.commit()
        return (f"⚔️ 登陆战报 ({tx},{ty})：\n"
                f"突击力 {power} vs 防御 {defense}，削减岛屿耐久 {int(dmg)}。\n"
                f"剩余耐久 {hp:.0f}（打空才能易主）。\n"
                f"运兵船战损 {lost}/{n_tr} 艘，消耗人力 {need}，民心 −5。\n"
                f"可再次 /nw登陆 继续消耗；注意补充运兵船与人力。")

    # ---------- P2b §27：支援（援军加入进行中的战斗）----------
    async def support(self, ctx: Ctx) -> str:
        """支援 <battle_id> [舰队]：§27.4 同阵营舰在反应半径内可加入战斗。"""
        if not ctx.args:
            return ("用法：/nw支援 <battle_id> [舰队]\n"
                    "不带舰队名时自动选一支位于反应半径内的空闲舰队。")
        try:
            bid = int(str(ctx.args[0]).lstrip("#"))
        except ValueError:
            return "❌ battle_id 要填数字，例如 /nw支援 12"
        b = self.conn.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if not b:
            return f"❌ 找不到战斗 #{bid}"
        if b["status"] != "active":
            return f"❌ 战斗 #{bid} 已结束"
        sides = json.loads(b["sides_json"] or "{}")
        a = sides.get("A", {})
        if a.get("qq") != ctx.qq:
            return "❌ 这不是你的战斗（§27.4 协讨需先建立共战关系，暂未开放）"
        ids = combat.side_fleet_ids(a)
        radius = int((self.cfg.get("fleet") or {}).get("support_radius", 15))

        if len(ctx.args) > 1:
            f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[1])
            if not f:
                return f"❌ 找不到舰队【{ctx.args[1]}】"
        else:
            f = None
            for cand in self.conn.execute("SELECT * FROM fleets WHERE qq=?", (ctx.qq,)).fetchall():
                if cand["id"] in ids or fleetm.ship_count(self.conn, cand["id"]) <= 0:
                    continue
                if fleetm.in_battle(self.conn, cand["id"]):
                    continue
                if max(abs(cand["x"] - b["x"]), abs(cand["y"] - b["y"])) <= radius:
                    f = cand
                    break
            if f is None:
                return (f"❌ 反应半径 {radius} 格内没有可用的空闲舰队。\n"
                        f"先把舰队开到 ({b['x']},{b['y']}) 附近再支援。")
        if f["id"] in ids:
            return f"❌ 【{f['name']}】已经在这场战斗里了"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能参战"
        if fleetm.in_battle(self.conn, f["id"]):
            return f"❌ 【{f['name']}】正在另一场战斗中"
        dist = max(abs(f["x"] - b["x"]), abs(f["y"] - b["y"]))
        if dist > radius:
            return (f"❌ 【{f['name']}】距战场 {dist} 格，超出反应半径 {radius} 格（§27.4）。\n"
                    f"先 /nw移动 {f['name']} {b['x']},{b['y']}")
        # 到位参战：把舰队拉到战场格并入列
        ids.append(f["id"])
        a["fleet_ids"] = ids
        sides["A"] = a
        self.conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                          (b["x"], b["y"], f["id"]))
        self.conn.execute("UPDATE battles SET sides_json=? WHERE id=?",
                          (json.dumps(sides, ensure_ascii=False), bid))
        self.conn.execute(
            "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,?,?)",
            (bid, b["tick"], 0, f"🤝 援军【{f['name']}】自 {dist} 格外赶到，加入我方阵线！"))
        self.conn.commit()
        return (f"✅ 援军【{f['name']}】已加入战斗 #{bid}。\n"
                f"我方现有 {len(ids)} 支舰队："
                + "、".join(self._fleet_name(i) for i in ids) +
                f"\n（本次简化为即时到位；§27.4 的按航速计到达时间尚未建模）")

    def _fleet_name(self, fid: int) -> str:
        r = self.conn.execute("SELECT name FROM fleets WHERE id=?", (fid,)).fetchone()
        return r["name"] if r else f"#{fid}"

    # ---------- P2b §26.1：外交 ----------
    def _faction_arg(self, raw: str):
        """把玩家输入的势力名/英文 id 解析成阵营 id。"""
        from . import relations
        if not raw:
            return None
        key = raw.strip()
        if key in relations.faction_cfg(self.cfg):
            return key
        for fid in relations.all_factions(self.cfg):
            if relations.faction_name(self.cfg, fid) == key:
                return fid
        for fid in relations.all_factions(self.cfg):
            if key and (key in relations.faction_name(self.cfg, fid)
                        or key in fid):
                return fid
        return None

    def _player_arg(self, raw: str):
        """§6.2 把输入解析成另一个玩家（支持 QQ 号或昵称）。"""
        if not raw:
            return None
        key = raw.strip().lstrip("@")
        r = self.conn.execute("SELECT qq,name FROM players WHERE qq=?", (key,)).fetchone()
        if r:
            return r
        return self.conn.execute("SELECT qq,name FROM players WHERE name=?",
                                 (key,)).fetchone()

    async def diplomacy(self, ctx: Ctx) -> str:
        """外交 [势力]：§26.1 查看与各阵营的关系权威状态。"""
        from . import relations
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if ctx.args:
            fid = self._faction_arg(ctx.args[0])
            if not fid:
                return (f"❌ 未知势力【{ctx.args[0]}】。可用："
                        + "、".join(relations.faction_name(self.cfg, f)
                                    for f in relations.all_factions(self.cfg)))
            state = relations.get_state(self.conn, self.cfg, ctx.qq, fid)
            rec = self.conn.execute(
                "SELECT state,note FROM relations WHERE qq=? AND faction=?",
                (ctx.qq, fid)).fetchone()
            rep = relations.reputation(self.conn, ctx.qq, fid)
            heat = float(p["wanted_heat"] or 0)
            lines = [f"🤝 与【{relations.faction_name(self.cfg, fid)}】的关系："
                     f"{relations.STATE_ZH.get(state, state)}",
                     f"来源：{'玩家显式设定' if rec else '默认规则'}"
                     + (f"（{rec['note']}）" if rec and rec["note"] else ""),
                     f"声望 {rep:+.0f}" if fid in ("empire", "guild", "merc") else "",
                     f"说明：{(relations.faction_cfg(self.cfg).get(fid) or {}).get('desc','')}"]
            if fid == "empire":
                d = self.cfg.get("diplomacy") or {}
                lines.append(f"通缉热度 {heat:.0f}/100（≥{d.get('heat_wanted_threshold',20)} "
                             f"上通缉榜；≥{d.get('heat_hostile_threshold',20)} 视为敌对）")
            if state == relations.PEACE:
                lines.append(f"→ 和平状态不可攻击。要打请先 /nw宣战 "
                             f"{relations.faction_name(self.cfg, fid)}")
            return "\n".join(l for l in lines if l)

        rows = relations.snapshot(self.conn, self.cfg, ctx.qq)
        heat = float(p["wanted_heat"] or 0)
        out = [f"🌐 {p['name']} 的外交态势（§26.1 关系权威表）",
               f"通缉热度 {heat:.0f}/100　恶名 {int(p['infamy'] or 0)}", ""]
        for r in rows:
            mark = "⚔️" if r["state"] == relations.WAR else (
                "🤝" if r["state"] == relations.PEACE else "➖")
            src = "★" if r["explicit"] else " "
            out.append(f"{mark}{src} {r['name']:<10} {relations.STATE_ZH.get(r['state'], r['state'])}"
                       + (f"　声望 {r['reputation']:+.0f}"
                          if r["faction"] in ("empire", "guild", "merc") else ""))
        # §6.2 其他玩家
        pvps = relations.players_snapshot(self.conn, self.cfg, ctx.qq)
        if pvps:
            out.append("")
            out.append("🧑‍✈️ 其他提督（§6.2 玩家间外交）：")
            for v in pvps:
                mark = "⚔️" if v["state"] == relations.WAR else "🤝"
                out.append(f"{mark} {v['name']:<10} "
                           f"{relations.STATE_ZH.get(v['state'], v['state'])}"
                           f"　({v['qq']})")
            out.append("　对玩家默认和平（无战国）；要打请先 /nw宣战 <昵称>")
        out.append("")
        out.append("⚔️=敌对可打　🤝=和平(需宣战)　➖=中立可打但涨恶名　★=已显式设定")
        return "\n".join(out)

    async def declare_war(self, ctx: Ctx) -> str:
        """宣战 <势力>：§26.1 把关系权威表改为敌对。"""
        from . import relations
        if not ctx.args:
            return "用法：/nw宣战 <势力 或 玩家昵称>（用 /nw外交 看全部目标）"
        # §6.2 先试玩家，再试阵营
        pl = self._player_arg(ctx.args[0])
        if pl and pl["qq"] != ctx.qq:
            from . import diplomacy as _dip
            from . import relations as _rel
            if _dip.ncfg(self.cfg).get("block_war", True) and \
                    _rel.is_neutral(self.conn, ctx.qq):
                return ("❌ 你是中立观察国，不能向玩家宣战（§6.2）。\n"
                        "先 /nw中立 退出中立（未满最短期限会涨恶名）。")
            if _rel.is_neutral(self.conn, pl["qq"]):
                return (f"❌ 【{pl['name']}】是中立观察国，受不可侵犯保护（§6.2）。\n"
                        f"你必须等对方自行退出中立。")
            cur = relations.get_pvp_state(self.conn, self.cfg, ctx.qq, pl["qq"])
            if cur == relations.WAR:
                return f"❌ 你已经在对【{pl['name']}】作战了"
            tick = meta_get(self.conn, "war_tick", int, 0)
            relations.declare_pvp_war(self.conn, ctx.qq, pl["qq"], tick)
            return (f"⚔️ 已向提督【{pl['name']}】宣战（§6.2）。\n"
                    f"双方的舰队现在可以在同格/相邻处交火；"
                    f"击沉无战国玩家运输船会涨恶名（§26.1），宣战后则不会。\n"
                    f"罢兵：/nw媾和 {pl['name']}")
        fid = self._faction_arg(ctx.args[0])
        if not fid:
            return (f"❌ 未知势力【{ctx.args[0]}】。可用："
                    + "、".join(relations.faction_name(self.cfg, f)
                                for f in relations.all_factions(self.cfg)))
        name = relations.faction_name(self.cfg, fid)
        cur = relations.get_state(self.conn, self.cfg, ctx.qq, fid)
        if cur == relations.WAR:
            return f"❌ 你与【{name}】已经处于敌对状态"
        tick = meta_get(self.conn, "war_tick", int, 0)
        relations.set_state(self.conn, ctx.qq, fid, relations.WAR, tick, "玩家宣战")
        extra = ""
        if fid == "empire":
            relations.add_heat(self.conn, ctx.qq, 10)
            extra = "\n⚠️ 对帝国宣战：通缉热度 +10，帝国军将按热度分级猎杀。"
        return (f"⚔️ 已向【{name}】宣战。\n"
                f"你的舰队现在可以攻击其单位；同格自动接敌，破交/攻击阵位会主动寻歼。{extra}")
    async def make_peace(self, ctx: Ctx) -> str:
        """媾和 <势力>：§26.1 把关系权威表改回和平。"""
        from . import relations
        if not ctx.args:
            return "用法：/nw媾和 <势力 或 玩家昵称>"
        pl = self._player_arg(ctx.args[0])
        if pl and pl["qq"] != ctx.qq:
            cur = relations.get_pvp_state(self.conn, self.cfg, ctx.qq, pl["qq"])
            if cur != relations.WAR:
                return f"❌ 你与【{pl['name']}】本来就处于和平状态"
            tick = meta_get(self.conn, "war_tick", int, 0)
            relations.make_pvp_peace(self.conn, ctx.qq, pl["qq"], tick)
            return (f"🕊️ 已与提督【{pl['name']}】媾和，双方停战。\n"
                    f"（§6.2：临时共战不等于同盟；这是白和平，不涉及赔款割岛）")
        fid = self._faction_arg(ctx.args[0])
        if not fid:
            return f"❌ 未知势力【{ctx.args[0]}】"
        name = relations.faction_name(self.cfg, fid)
        cur = relations.get_state(self.conn, self.cfg, ctx.qq, fid)
        rec = self.conn.execute("SELECT state FROM relations WHERE qq=? AND faction=?",
                                (ctx.qq, fid)).fetchone()
        stored = rec["state"] if rec and rec["state"] else \
            (relations.faction_cfg(self.cfg).get(fid) or {}).get(
                "default", relations.NEUTRAL)
        if stored == relations.PEACE:
            # 外交记录本来就是和平；此时若仍判定为敌对，只可能是热度派生出来的
            if cur == relations.WAR:
                d = self.cfg.get("diplomacy") or {}
                heat = float(self._player(ctx.qq)["wanted_heat"] or 0)
                return (f"❌ 【{name}】对你的敌对不是外交状态，而是通缉热度 "
                        f"{heat:.0f} 派生出来的。\n"
                        f"热度每日衰减 {d.get('heat_decay_per_day', 2)}，"
                        f"降到 {d.get('heat_hostile_threshold', 20)} 以下会自动恢复和平，"
                        f"无需媾和。")
            return f"❌ 你与【{name}】本来就是和平关系"
        d = self.cfg.get("diplomacy") or {}
        heat = float(self._player(ctx.qq)["wanted_heat"] or 0)
        if fid == "empire" and heat >= float(d.get("heat_hostile_threshold", 20)):
            return (f"❌ 帝国拒绝媾和：你的通缉热度 {heat:.0f} 仍高于 "
                    f"{d.get('heat_hostile_threshold', 20)}。\n"
                    f"热度每日自然衰减 {d.get('heat_decay_per_day', 2)}，"
                    f"或全歼执法者批次可清零恶名——先降温再来。")
        tick = meta_get(self.conn, "war_tick", int, 0)
        relations.set_state(self.conn, ctx.qq, fid, relations.PEACE, tick, "玩家媾和")
        return (f"🕊️ 已与【{name}】媾和，关系恢复和平，双方停止互相攻击。")

    # ---------- P2b §26.2：雇佣军团 ----------
    def _merc_near(self, qq: str):
        """找 5 格内可谈的雇佣船队（§26.2「接近到 5 格内可谈」）。"""
        mcfg = self.cfg.get("merc") or {}
        talk = int(mcfg.get("talk_radius", 5))
        best = None
        for f in self.conn.execute("SELECT * FROM fleets WHERE qq=?", (qq,)).fetchall():
            for m in self.conn.execute(
                    "SELECT * FROM ai_fleets WHERE faction='merc' AND comp_json!='[]'").fetchall():
                d = max(abs(m["x"] - f["x"]), abs(m["y"] - f["y"]))
                if d <= talk and (best is None or d < best[0]):
                    best = (d, m, f)
        if best:
            return best[1], best[2], best[0]
        # 没有舰队在附近时，退而看首都附近
        p = self._player(qq)
        if not p or p["capital_x"] is None:
            return None, None, 0
        for m in self.conn.execute(
                "SELECT * FROM ai_fleets WHERE faction='merc' AND comp_json!='[]'").fetchall():
            d = max(abs(m["x"] - p["capital_x"]), abs(m["y"] - p["capital_y"]))
            if d <= talk and (best is None or d < best[0]):
                best = (d, m, None)
        return (best[1], best[2], best[0]) if best else (None, None, 0)

    async def hire(self, ctx: Ctx) -> str:
        """雇佣 [天数]：§26.2 花钱雇佣附近的雇佣军团。"""
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        merc, near, dist = self._merc_near(ctx.qq)
        if not merc:
            talk = int((self.cfg.get("merc") or {}).get("talk_radius", 5))
            return (f"❌ {talk} 格内没有可谈的雇佣船队。\n"
                    f"雇佣军团在海图上以 m 标记，全图漂游；派舰队靠近再谈。")
        m = json.loads(merc["mission"] or "{}")
        lv = int(m.get("level") or merc["level"] or 8)
        company = m.get("company") or merc["name"]
        spec = aiworld.merc_spec(self.cfg, lv)
        per_day = int(spec.get("price_per_day", 300))
        need_oil = int(spec.get("oil", 0))
        need_supply = int(spec.get("supply", 0))
        mcfg = self.cfg.get("merc") or {}
        days = int(mcfg.get("min_days", 3))
        if ctx.args:
            try:
                days = int(ctx.args[0])
            except ValueError:
                return "❌ 天数要填整数，例如 /nw雇佣 7"
        if not int(mcfg.get("min_days", 3)) <= days <= int(mcfg.get("max_days", 30)):
            return f"❌ 合同天数范围 {mcfg.get('min_days', 3)}~{mcfg.get('max_days', 30)} 天"
        total = per_day * days
        if p["money"] < total:
            return f"❌ 资金不够：需 {total}，现有 {p['money']:.0f}"
        if p["oil"] < need_oil or p["supply"] < need_supply:
            return (f"❌ 预付物资不够：需 油{need_oil} 补给{need_supply}，"
                    f"现有 油{p['oil']:.0f} 补给{p['supply']:.0f}")
        comp = json.loads(merc["comp_json"] or "[]")
        self.confirm.put(ctx.origin, ctx.qq,
                         {"type": "hire", "merc_id": merc["id"], "days": days,
                          "price": total, "oil": need_oil, "supply": need_supply})
        troops = "、".join(f"{aiworld.CLASS_ZH_BASE.get(c['cls'], c['cls'])}×{c['qty']}"
                           for c in comp)
        return (f"🤝 雇佣谈判　【{company}】L{lv}\n"
                f"距你 {dist} 格\n"
                f"编制：{troops}\n"
                f"报价：资金 {per_day}/日 × {days} 日 = 资金{total}"
                f"　预付 油{need_oil} 补给{need_supply}\n"
                f"合同期内该舰队归你指挥（可用 驻防/护卫/移动/支援 等指令）；\n"
                f"⚠️ 到期后原地解散变中立，不再听令。\n"
                f"回复 1 签约　2 拒绝")

    async def _action_hire(self, ctx: Ctx, action: dict, choice: str) -> str:
        if choice != "1":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已婉拒雇佣军团。"
        self.confirm.drop(ctx.origin, ctx.qq)
        merc = self.conn.execute("SELECT * FROM ai_fleets WHERE id=?",
                                 (action["merc_id"],)).fetchone()
        if not merc or not json.loads(merc["comp_json"] or "[]"):
            return "❌ 该雇佣船队已不在（可能已被歼灭）"
        p = self._player(ctx.qq)
        if p["money"] < action["price"] or p["oil"] < action["oil"] \
                or p["supply"] < action["supply"]:
            return "❌ 资金/物资不足，签约取消"
        self.conn.execute(
            "UPDATE players SET money=money-?, oil=oil-?, supply=supply-? WHERE qq=?",
            (action["price"], action["oil"], action["supply"], ctx.qq))

        m = json.loads(merc["mission"] or "{}")
        lv = int(m.get("level") or merc["level"] or 8)
        company = m.get("company") or merc["name"]
        comp = json.loads(merc["comp_json"] or "[]")
        war_tick = meta_get(self.conn, "war_tick", int, 0)
        days = int(action["days"])
        expire = war_tick + days * 72 * 2      # 1 日 = 72 经济 tick = 144 战争 tick

        # 把雇佣编制转成玩家真实舰船（复用 designs/ships，玩家就能用全部既有指令）
        cur = self.conn.execute(
            "INSERT INTO fleets(qq,name,x,y,mission,created_tick) VALUES(?,?,?,?, '{}',0)",
            (ctx.qq, f"雇佣·{company}"[:8], merc["x"], merc["y"]))
        fid = cur.lastrowid
        made = 0
        for c in comp:
            cls, tier, qty = c["cls"], int(c.get("tier", 2)), int(c.get("qty", 1))
            dn = f"雇佣-{cls}-T{tier}"
            d = self.conn.execute("SELECT id FROM designs WHERE qq=? AND name=?",
                                  (ctx.qq, dn)).fetchone()
            if d:
                did = d["id"]
            else:
                st = aiworld.standard_stats(cls, tier)
                c2 = self.conn.execute(
                    "INSERT INTO designs(qq,name,ship_class,tier,modules,stats_json,"
                    "cost_json,work_ticks) VALUES(?,?,?,?,'{}',?,'{}',1)",
                    (ctx.qq, dn, cls, tier, json.dumps(st, ensure_ascii=False)))
                did = c2.lastrowid
            for _ in range(qty):
                r = self.conn.execute("SELECT MAX(id) m FROM ships").fetchone()
                nm = f"{aiworld.CLASS_ZH_BASE.get(cls, cls)}{(r['m'] or 0) + 1}"
                self.conn.execute(
                    "INSERT INTO ships(qq,fleet_id,def_id,name,tier,hp,max_hp,data_json)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (ctx.qq, fid, str(did), nm, tier, c["hp"], c["maxhp"],
                     json.dumps({"stats": json.loads(self.conn.execute(
                         "SELECT stats_json FROM designs WHERE id=?", (did,)).fetchone()[0]),
                         "cost": {}})))
                made += 1
        self.conn.execute(
            "INSERT INTO contracts(qq,fleet_id,merc_id,company,level,start_tick,"
            "expire_tick,price,status) VALUES(?,?,?,?,?,?,?,?,'active')",
            (ctx.qq, fid, merc["id"], company, lv, war_tick, expire, action["price"]))
        # 雇佣兵离开 AI 列表（人跟着合同走）
        self.conn.execute("UPDATE ai_fleets SET comp_json='[]',respawn_tick=NULL WHERE id=?",
                          (merc["id"],))
        self.conn.commit()
        return (f"✅ 已与【{company}】签约 {days} 天。\n"
                f"支付 资金{action['price']} + 油{action['oil']} + 补给{action['supply']}。\n"
                f"舰队【雇佣·{company}】已就位（{made} 艘），坐标 "
                f"({merc['x']},{merc['y']})。\n"
                f"你有完全指挥权：编队/移动/驻防/护航/支援 都可用。\n"
                f"⏳ 合同到期（约 {days} 日后）原地解散变中立——到期前记得把仗打完。")

    async def contracts(self, ctx: Ctx) -> str:
        """合同：查看在役雇佣合同与剩余时间。"""
        rows = self.conn.execute(
            "SELECT * FROM contracts WHERE qq=? AND status='active'", (ctx.qq,)).fetchall()
        if not rows:
            return "📄 当前没有在役雇佣合同。靠近雇佣军团（海图 m）后 /nw雇佣 可谈。"
        war_tick = meta_get(self.conn, "war_tick", int, 0)
        out = ["📄 在役雇佣合同："]
        for r in rows:
            left = max(0, int(r["expire_tick"]) - war_tick)
            n = self.conn.execute("SELECT COUNT(*) c FROM ships WHERE fleet_id=?",
                                  (r["fleet_id"],)).fetchone()["c"]
            days = left / 144.0
            out.append(f"　#{r['id']} 【{r['company']}】L{r['level']}　"
                       f"剩 {days:.1f} 日（{left} 战争tick）　现存 {n} 艘　"
                       f"已付资金{r['price']}")
        return "\n".join(out)

    # ---------- P2b §26.2：协会剿匪任务 ----------
    async def missions(self, ctx: Ctx) -> str:
        """任务：查看协会发布的剿匪合同（声望≥门槛才有更好的单）。"""
        from . import guild, relations
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        war_tick = meta_get(self.conn, "war_tick", int, 0)
        guild.ensure_missions(self.conn, self.cfg, ctx.qq, war_tick)
        rows = guild.list_missions(self.conn, ctx.qq)
        rep = relations.reputation(self.conn, ctx.qq, "guild")
        if not rows:
            return (f"📋 协会暂时没有给你的单子。\n"
                    f"当前协会声望 {rep:+.0f}，接单需声望 ≥0。\n"
                    f"（§26.2：声望≥友好才会发护航/剿匪任务）")
        out = [f"📋 商人协会任务栏　协会声望 {rep:+.0f}", ""]
        for r in rows:
            tag = "已接单" if r["accepted"] else "未接单"
            bar = "█" * min(10, int(r["progress"] * 10 / max(1, r["need"])))
            tname = relations.faction_name(self.cfg, r["target_faction"])
            out.append(f"　#{r['id']}【{r['name']}】{tag}")
            out.append(f"　　目标：击沉 {tname} 舰船 {r['need']} 艘")
            out.append(f"　　进度：{r['progress']}/{r['need']} {bar}")
            out.append(f"　　预付 资金{r['prepay']}　赏金 资金{r['reward']}　"
                       f"声望 +{r['rep_reward']}")
        out.append("")
        out.append("接单：/nw接单 <编号>　交差：/nw交差 <编号>")
        return "\n".join(out)

    async def accept_mission(self, ctx: Ctx) -> str:
        """接单 <编号>：预付资金接下一单协会任务。"""
        from . import guild
        if not ctx.args:
            return "用法：/nw接单 <编号>（用 /nw任务 看列表）"
        try:
            mid = int(ctx.args[0])
        except ValueError:
            return "❌ 编号要填数字"
        m = self.conn.execute("SELECT * FROM guild_missions WHERE id=? AND qq=?",
                              (mid, ctx.qq)).fetchone()
        if not m:
            return f"❌ 找不到任务 #{mid}"
        p = self._player(ctx.qq)
        if p["money"] < int(m["prepay"]):
            return f"❌ 资金不够：接单需预付 {m['prepay']}，现有 {p['money']:.0f}"
        ok, err = guild.accept(self.conn, self.cfg, ctx.qq, mid)
        if not ok:
            return err
        self.conn.execute("UPDATE players SET money=money-? WHERE qq=?",
                          (int(m["prepay"]), ctx.qq))
        self.conn.commit()
        from . import relations as _rel
        tname = _rel.faction_name(self.cfg, m["target_faction"])
        return (f"📝 已接下任务 #{mid}【{m['name']}】。\n"
                f"预付款 资金{m['prepay']} 已到账（先行支付，§26.2）。\n"
                f"目标：击沉 {tname} 舰船 {m['need']} 艘"
                f"（当前 {m['progress']}）。\n"
                f"完成后 /nw交差 {mid} 领赏 资金{m['reward']} 并涨协会声望。")

    async def claim_mission(self, ctx: Ctx) -> str:
        """交差 <编号>：进度达标则领赏并涨声望。"""
        from . import guild
        if not ctx.args:
            return "用法：/nw交差 <编号>"
        try:
            mid = int(ctx.args[0])
        except ValueError:
            return "❌ 编号要填数字"
        ok, text = guild.claim(self.conn, self.cfg, ctx.qq, mid)
        return text

    # ---------- P2b §18.6：税率法案 ----------
    async def tax(self, ctx: Ctx) -> str:
        """税率 [档位]：查看或调整税率法案（资金倍率 ↔ 民心/日）。"""
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        from .engine import tax_table, tax_bill, morale_factor
        bills = tax_table(self.cfg)
        cur = int(p["tax_rate"] if p["tax_rate"] is not None else 5)
        if not ctx.args:
            lines = [f"📊 税率法案（§18.6）　当前：{tax_bill(self.cfg, cur).get('name')}",
                     f"岛屿民心 {p['morale']:.0f}　民心系数 ×{morale_factor(p['morale']):.2f}",
                     ""]
            for k in sorted(bills, key=int):
                b = bills[k]
                mark = "▶" if int(k) == cur else "　"
                lines.append(f"{mark} {b['name']:<12} 资金 ×{b['money_mult']}　"
                             f"民心 {b['morale_per_day']:+d}/日")
            lines.append("")
            lines.append("民心系数 mF = 0.6 + 0.4×(民心/100)：民心 0 时产出只剩 60%。")
            lines.append("切换：/nw税率 <0|5|10|15|20>")
            return "\n".join(lines)
        try:
            rate = int(ctx.args[0])
        except ValueError:
            return "❌ 税率要填数字：0 / 5 / 10 / 15 / 20"
        if str(rate) not in bills:
            return f"❌ 没有该档位。可选：{'、'.join(sorted(bills, key=int))}"
        if rate == cur:
            return f"❌ 当前已经是【{bills[str(rate)]['name']}】"
        self.conn.execute("UPDATE players SET tax_rate=? WHERE qq=?", (rate, ctx.qq))
        self.conn.commit()
        b = bills[str(rate)]
        warn = ""
        if b["morale_per_day"] < 0:
            warn = (f"\n⚠️ 该档位会让民心 {b['morale_per_day']:+d}/日，"
                    f"长期维持会掉到 30 以下触发叛乱检定（§14.2）。")
        elif b["morale_per_day"] > 0:
            warn = "\n✅ 休养生息：民心和产出都在恢复，适合战后调整。"
        return (f"📊 税率已调为【{b['name']}】\n"
                f"资金产出 ×{b['money_mult']}　民心 {b['morale_per_day']:+d}/日{warn}")

    # ---------- P2b §19.15：航母与空袭 ----------
    async def hangar(self, ctx: Ctx) -> str:
        """机库 [舰队]：查看航母机库、单波上限与舰载机中队。"""
        from . import air
        if ctx.args:
            f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
            if not f:
                return f"❌ 找不到舰队【{ctx.args[0]}】"
        else:
            f = self.conn.execute(
                "SELECT * FROM fleets WHERE qq=? ORDER BY id LIMIT 1", (ctx.qq,)).fetchone()
            if not f:
                return "❌ 你还没有舰队"
        carriers = air.fleet_carriers(self.conn, f["id"])
        if not carriers:
            return (f"❌【{f['name']}】里没有航母。\n"
                    f"航母需要在设计器里以「航空母舰」舰种建造"
                    f"（槽位：船体/动力/飞行甲板/机库/防空/电子）。")
        hg = air.fleet_hangar(self.conn, self.cfg, f["id"])
        wname = air.weather_name(self.cfg, air.weather_of(self.conn))
        lines = [f"🛩【{f['name']}】机库：{hg['hangar']} 个中队位"
                 f"　单波上限 {hg['wave']} 中队　当前天气【{wname}】", ""]
        war_tick = meta_get(self.conn, "war_tick", int, 0)
        for r, tier in carriers:
            spec = air.cv_spec(self.cfg, tier)
            d = air.deck_row(self.conn, self.cfg, r["id"], tier)
            deck = ""
            if d:
                pct = (float(d["deck_hp"]) / max(1.0, float(d["deck_max"]))) * 100
                down = int(d["down_until"] or 0) > war_tick
                deck = (f"　甲板 {d['deck_hp']:.0f}/{d['deck_max']:.0f}（{pct:.0f}%）"
                        + ("　🛑停飞中" if down else ""))
            lines.append(f"　🚢 {r['name']} T{tier} {spec.get('name', '')}"
                         f"　机库{spec.get('hangar', 0)} 单波{spec.get('wave', 0)}"
                         f"{deck}")
        squads = air.fleet_squadrons(self.conn, f["id"])
        lines.append("")
        if not squads:
            lines.append("　（机库为空——建造航母后会自动配属联队）")
        else:
            size = air.squadron_size(self.cfg)
            for s in squads:
                fat = int(s["fatigue"] or 0) if "fatigue" in s.keys() else 0
                ff = air.fatigue_factor(self.cfg, fat)
                lines.append(f"　✈️ {air.kind_name(self.cfg, s['kind'])} T{s['tier']}"
                             f"　{s['planes']}/{size} 架"
                             f"　疲劳 {fat} {air.fatigue_label(self.cfg, fat)}"
                             + (f"（威力 ×{ff:.0%}）" if ff < 1.0 else ""))
            rec = int((self.cfg.get("air", {}).get("fatigue") or {})
                      .get("recover_per_tick", 7))
            lines.append(f"　（§19.15 规则 1：每战争 tick 整备恢复 {rec} 点疲劳，"
                         f"甲板趴窝时不恢复）")
        lines.append("")
        lines.append(f"出击：/nw空袭 {f['name']} <目标x,y>　"
                     f"（半径 {int((self.cfg.get('air') or {}).get('strike_range', 12))} 格，"
                     f"每架耗铝 {int((self.cfg.get('air') or {}).get('aluminium_per_plane', 2))}）")
        return "\n".join(lines)

    async def airstrike(self, ctx: Ctx) -> str:
        """空袭 <航母舰队> <目标x,y>：§19.15 舰载机波次出击。"""
        from . import air
        if len(ctx.args) < 2:
            return ("用法：/nw空袭 <航母舰队> <目标x,y>\n"
                    "例：/nw空袭 航1 234,567\n"
                    "（§19.15：每战争 tick 可出击中队数 = 航母单波上限；"
                    "先吃对方 CAP，再吃编队防空，幸存者投弹）")
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:]))
        if xy is None:
            return "❌ 目标坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        ok, text = air.strike(self.conn, self.cfg, ctx.qq, f["id"], tx, ty)
        return text

    async def co_belligerent(self, ctx: Ctx) -> str:
        """协讨 <battle_id> <舰队>：§27.4 第三方玩家申请加入进行中的战斗，按伤害分赏。"""
        if len(ctx.args) < 2:
            return ("用法：/nw协讨 <battle_id> <舰队>\n"
                    "对通缉犯/公敌/执法者的战斗，任何人都可申请加入，"
                    "战利品按伤害贡献分配（§27.4/§27.6）。")
        try:
            bid = int(str(ctx.args[0]).lstrip("#"))
        except ValueError:
            return "❌ battle_id 要填数字"
        b = self.conn.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if not b:
            return f"❌ 找不到战斗 #{bid}"
        if b["status"] != "active":
            return f"❌ 战斗 #{bid} 已结束"
        sides = json.loads(b["sides_json"] or "{}")
        a = sides.get("A", {})
        if a.get("qq") == ctx.qq:
            return f"❌ 这本来就是你的战斗，用 /nw支援 {bid} <舰队> 增援即可"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[1])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[1]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能参战"
        if fleetm.in_battle(self.conn, f["id"]):
            return f"❌ 【{f['name']}】正在另一场战斗中"
        # §27.4 协讨限于"打公敌"的战斗：对方须是执法者/通缉犯这类
        b_side = sides.get("B", {})
        faction = b_side.get("faction", "pirate")
        if faction not in ("enforcer", "rebel", "pirate"):
            return (f"❌ 该战斗的对手是【{faction}】，不属于可协讨目标。\n"
                    f"（§27.4：协讨仅对通缉犯/公敌/执法者这类目标开放）")
        dist = max(abs(f["x"] - b["x"]), abs(f["y"] - b["y"]))
        radius = int((self.cfg.get("fleet") or {}).get("support_radius", 15))
        if dist > radius:
            return (f"❌ 【{f['name']}】距战场 {dist} 格，超出反应半径 {radius} 格。\n"
                    f"先 /nw移动 {f['name']} {b['x']},{b['y']}")
        ids = combat.side_fleet_ids(a)
        if f["id"] in ids:
            return f"❌ 【{f['name']}】已经在这场战斗里了"
        # 协讨方记入 co 名单：战利品按 §27.6 伤害贡献分
        co = list(a.get("co") or [])
        if ctx.qq not in co:
            co.append(ctx.qq)
        a["co"] = co
        ids.append(f["id"])
        a["fleet_ids"] = ids
        sides["A"] = a
        self.conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                          (b["x"], b["y"], f["id"]))
        self.conn.execute("UPDATE battles SET sides_json=? WHERE id=?",
                          (json.dumps(sides, ensure_ascii=False), bid))
        self.conn.execute(
            "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,?,?)",
            (bid, b["tick"], 0,
             f"🤝 第三方【{f['name']}】自 {dist} 格外赶来协讨！"))
        self.conn.commit()
        return (f"✅ 已加入战斗 #{bid} 协讨（§27.4）。\n"
                f"我方现有 {len(ids)} 支舰队。\n"
                f"⚠️ 临时共战不等于同盟，战斗结束后关系恢复原状；\n"
                f"战利品将按 §27.6 伤害贡献表分给各参与方。")

    # ---------- P2b §4：布雷 / 扫雷 ----------
    async def lay_mines(self, ctx: Ctx) -> str:
        """布雷 <舰队> <坐标> [雷种]：§4 区域水雷场。"""
        from . import mines as minem
        if len(ctx.args) < 2:
            kinds = "、".join(f"{k}={minem.kind_name(self.cfg, k)}"
                              for k in minem.KINDS)
            return (f"用法：/nw布雷 <舰队> <坐标> [雷种]\n"
                    f"例：/nw布雷 潜1 234,567 magnetic\n"
                    f"雷种：{kinds}（默认 anchor）")
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        if fleetm.ship_count(self.conn, f["id"]) <= 0:
            return "❌ 空舰队不能布雷"
        xy = fleetm.parse_xy(ctx.args[1])
        if xy is None:
            return "❌ 坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        dist = max(abs(f["x"] - tx), abs(f["y"] - ty))
        if dist > 1:
            return (f"❌ 舰队在({f['x']},{f['y']})，距目标 {dist} 格。\n"
                    f"布雷需在目标格或相邻格（先 /nw移动 {f['name']} {tx},{ty}）")
        kind = ctx.args[2] if len(ctx.args) > 2 else "anchor"
        ok, text = minem.lay(self.conn, self.cfg, ctx.qq, f["id"], tx, ty, kind)
        return text

    async def sweep_mines(self, ctx: Ctx) -> str:
        """扫雷 <舰队> <坐标>：清除敌方雷场。"""
        from . import mines as minem
        if len(ctx.args) < 2:
            return "用法：/nw扫雷 <舰队> <坐标>"
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        xy = fleetm.parse_xy(ctx.args[1])
        if xy is None:
            return "❌ 坐标格式不对，应如 234,567（0~999）"
        tx, ty = xy
        dist = max(abs(f["x"] - tx), abs(f["y"] - ty))
        if dist > 1:
            return (f"❌ 舰队距目标 {dist} 格，扫雷需在目标格或相邻格")
        ok, text = minem.sweep(self.conn, self.cfg, ctx.qq, f["id"], tx, ty)
        return text

    async def alliance(self, ctx: Ctx) -> str:
        """同盟 <玩家>：§6.2 缔结同盟（双向），共享视野、自动协防。"""
        from . import relations
        if not ctx.args:
            return ("用法：/nw同盟 <玩家昵称或QQ>\n"
                    "§6.2 同盟效果：共享视野（盟友的海图为你点灯）、"
                    "同盟协防（盟友挨打时你 15 格内的舰队自动参战）、互相不可攻击。")
        pl = self._player_arg(ctx.args[0])
        if not pl or pl["qq"] == ctx.qq:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        if relations.are_allied(self.conn, self.cfg, ctx.qq, pl["qq"]):
            return f"❌ 你与【{pl['name']}】已经是同盟"
        cur = relations.get_pvp_state(self.conn, self.cfg, ctx.qq, pl["qq"])
        tick = meta_get(self.conn, "war_tick", int, 0)
        relations.declare_alliance(self.conn, ctx.qq, pl["qq"], tick)
        warn = ""
        if cur == relations.WAR:
            warn = "\n⚠️ 你们此前处于战争状态，缔结同盟已自动停战。"
        return (f"🤝 已与提督【{pl['name']}】缔结同盟（§6.2）。\n"
                f"✅ 共享视野：对方的首都/岛屿/舰队为你点灯\n"
                f"✅ 同盟协防：对方挨打时，你 15 格内的空闲舰队会自动参战（§27.1）\n"
                f"✅ 互相不可攻击{warn}\n"
                f"解盟：/nw解盟 {pl['name']}")

    async def break_alliance_cmd(self, ctx: Ctx) -> str:
        """解盟 <玩家>：§6.2 解除同盟（回到和平）。"""
        from . import relations
        if not ctx.args:
            return "用法：/nw解盟 <玩家昵称或QQ>"
        pl = self._player_arg(ctx.args[0])
        if not pl or pl["qq"] == ctx.qq:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        if not relations.are_allied(self.conn, self.cfg, ctx.qq, pl["qq"]):
            return f"❌ 你与【{pl['name']}】没有同盟关系"
        tick = meta_get(self.conn, "war_tick", int, 0)
        relations.break_alliance(self.conn, ctx.qq, pl["qq"], tick)
        return (f"💔 已解除与【{pl['name']}】的同盟，双方回到和平状态。\n"
                f"（共享视野与自动协防同时失效；不会自动转为敌对）")

    async def salvage_cmd(self, ctx: Ctx) -> str:
        """打捞 [坐标]：§26.4 雲墨残骸 / §11 沉船遗迹。"""
        from . import salvage as sv
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if not ctx.args:
            # 列出附近残骸
            cx, cy = p["capital_x"], p["capital_y"]
            rows = sv.wrecks_near(self.conn, max(0, cx - 60), max(0, cy - 60),
                                  cx + 60, cy + 60)
            if not rows:
                return ("⚓ 附近 120 格内没有可打捞的残骸。\n"
                        "（§26.4 雲墨被歼灭后会留下 14 日残骸区；"
                        "§11 沉船遗迹随机分布在海图上，海图标记 w）")
            out = ["⚓ 附近可打捞目标："]
            for r in rows:
                kn = "雲墨残骸区" if r["kind"] == "yunmo" else "沉船遗迹"
                out.append(f"　({r['x']},{r['y']})【{kn}】剩余 {r['remaining']} 次")
            out.append("")
            out.append("用法：/nw打捞 <舰队> <坐标>（或舰队已在残骸格时省略坐标）")
            return "\n".join(out)
        # 解析：/nw打捞 <舰队> [坐标]
        f = fleetm.get_fleet(self.conn, ctx.qq, ctx.args[0])
        if not f:
            return f"❌ 找不到舰队【{ctx.args[0]}】"
        xy = fleetm.parse_xy(" ".join(ctx.args[1:])) if len(ctx.args) > 1 else None
        x, y = xy if xy else (f["x"], f["y"])
        ok, text = sv.salvage(self.conn, self.cfg, ctx.qq, f["id"], x, y)
        return text

    async def captains_cmd(self, ctx: Ctx) -> str:
        """指挥官：§19.1 查看舰长名册与任职情况。"""
        from . import captains as cap
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        rows = cap.list_captains(self.conn, ctx.qq)
        if not rows:
            return (f"🎖 你还没有舰长。\n"
                    f"招募：/nw招募舰长（资金{cap.ccfg(self.cfg).get('recruit_money', 800)}"
                    f" + 人力{cap.ccfg(self.cfg).get('recruit_manpower', 50)}）\n"
                    f"§19.1：舰长带技能等级，任命上舰后提供加成，战沉则一并损失。")
        out = ["🎖 舰长名册（§19.1）", ""]
        for r in rows:
            ship = "待命"
            if r["ship_id"]:
                s = self.conn.execute("SELECT name FROM ships WHERE id=?",
                                      (r["ship_id"],)).fetchone()
                ship = s["name"] if s else "（舰已损失）"
            need = int(cap.ccfg(self.cfg).get("exp_per_level", 100))
            out.append(f"　【{r['name']}】{cap.skill_name(self.cfg, r['skill'])} "
                       f"Lv{r['level']}　经验 {r['exp']}/{need}　任职：{ship}")
        out.append("")
        out.append("招募：/nw招募舰长　任命：/nw任命 <舰长> <舰名>　免职：/nw免职 <舰长>")
        return "\n".join(out)

    async def recruit_captain(self, ctx: Ctx) -> str:
        """招募舰长：§15.3 海军学院负责指挥官获取。"""
        from . import captains as cap
        ok, text = cap.recruit(self.conn, self.cfg, ctx.qq)
        return text

    async def appoint_captain(self, ctx: Ctx) -> str:
        """任命 <舰长> <舰名>：§19.1 把舰长派上舰。"""
        from . import captains as cap
        if len(ctx.args) < 2:
            return ("用法：/nw任命 <舰长名> <舰名>\n"
                    "例：/nw任命 陈绍宽 海圻号")
        ok, text = cap.assign(self.conn, self.cfg, ctx.qq, ctx.args[0], ctx.args[1])
        return text

    async def dismiss_captain(self, ctx: Ctx) -> str:
        """免职 <舰长>：解除任职，舰长转为待命。"""
        from . import captains as cap
        if not ctx.args:
            return "用法：/nw免职 <舰长名>"
        ok, text = cap.unassign(self.conn, self.cfg, ctx.qq, ctx.args[0])
        return text

    async def power_cmd(self, ctx: Ctx) -> str:
        """电力 [坐标]：§18.5 查看岛屿电网供需与拉闸情况。"""
        from .engine import island_power
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        x, y = p["capital_x"], p["capital_y"]
        if ctx.args:
            xy = fleetm.parse_xy(ctx.args[0])
            if not xy:
                return "❌ 坐标格式示例：/nw电力 234,567"
            x, y = xy
        isl = self.conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
        if not isl or isl["owner_qq"] != ctx.qq:
            return f"❌ ({x},{y}) 不是你的岛"
        pw = island_power(self.conn, self.cfg, x, y)
        lines = [f"⚡ ({x},{y}) 电网（§18.5）",
                 f"供电 {pw['supply']:.0f}　用电 {pw['demand']:.0f}"
                 f"　{'✅ 供需平衡' if pw['demand'] <= pw['supply'] else '⚠️ 供电不足，已拉闸'}"]
        if pw["oil"]:
            lines.append(f"发电站自耗 油{pw['oil']:.0f}/日")
        bs = self.conn.execute("SELECT * FROM buildings WHERE x=? AND y=?",
                               (x, y)).fetchall()
        if bs:
            lines.append("")
            for b in bs:
                bdef = self.cfg["buildings"].get(b["def_id"]) or {}
                tag = ""
                if b["id"] in pw["shed"]:
                    tag = "　🔌 已断电（停产）"
                elif b["id"] in pw["half"]:
                    tag = "　🔌 电力不足（半产）"
                parts = []
                if bdef.get("power_out"):
                    parts.append(f"+{bdef['power_out'] * b['level']}电")
                if bdef.get("power"):
                    parts.append(f"-{bdef['power'] * b['level']}电")
                lines.append(f"　{bdef.get('name', b['def_id'])} Lv{b['level']}"
                             f"　{' '.join(parts)}{tag}")
        return "\n".join(lines)

    async def spectate(self, ctx: Ctx) -> str:
        """观战 <battle_id>：§27.4 中立旁观——可看战报但无收益。"""
        from . import combat as _cb
        if not ctx.args:
            rows = self.conn.execute(
                "SELECT * FROM battles WHERE status='active' ORDER BY id DESC LIMIT 5"
            ).fetchall()
            if not rows:
                return "📭 当前没有正在进行的战斗。"
            out = ["👁 正在进行的战斗（§27.4 旁观不参战、无收益）："]
            for r in rows:
                sides = json.loads(r["sides_json"] or "{}")
                aa, bb = sides.get("A", {}), sides.get("B", {})
                out.append(f"　#{r['id']} ({r['x']},{r['y']})　"
                           f"{_cb._player_name(self.conn, aa.get('qq'))} vs "
                           + (_cb._player_name(self.conn, bb.get("qq"))
                              if _cb.is_pvp(bb)
                              else f"【{bb.get('faction', 'AI')}】"))
            out.append("")
            out.append("用法：/nw观战 <battle_id>")
            return "\n".join(out)
        try:
            bid = int(ctx.args[0].lstrip("#"))
        except (TypeError, ValueError):
            return "❌ 战斗编号应为数字，例：/nw观战 12"
        b = self.conn.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if not b:
            return f"❌ 找不到战斗 #{bid}"
        sides = json.loads(b["sides_json"] or "{}")
        aa, bb = sides.get("A", {}), sides.get("B", {})
        mine = ctx.qq in (aa.get("qq"), bb.get("qq"))
        evs = self.conn.execute(
            "SELECT round_no,text FROM battle_events WHERE battle_id=? ORDER BY id",
            (bid,)).fetchall()
        head = (f"👁 战斗 #{bid} ({b['x']},{b['y']})　状态 {b['status']}\n"
                f"{_cb._player_name(self.conn, aa.get('qq'))} vs "
                + (_cb._player_name(self.conn, bb.get("qq"))
                   if _cb.is_pvp(bb) else f"【{bb.get('faction', 'AI')}】"))
        if not mine and b["status"] == "active":
            head += "\n（§27.4 中立旁观：可看战报但无收益，也不会被冻结）"
        body = "\n".join(e["text"] for e in evs[-24:])
        tail = ""
        if b["status"] == "over" and b["summary"]:
            tail = "\n" + (b["summary"] or "")
        return head + "\n\n" + body + tail

    async def trade_cmd(self, ctx: Ctx) -> str:
        """贸易：§6.2 玩家间资源贸易（定向报价 / 公开挂单 / 市场 / 接受 / 拒绝 / 撤单）。"""
        from . import diplomacy as dip
        if not ctx.args:
            return dip.list_trades(self.conn, self.cfg, ctx.qq)
        sub = ctx.args[0]
        # —— 市场相关子命令 ——
        if sub in ("市场", "集市", "行情"):
            return dip.list_market(self.conn, self.cfg, ctx.qq)
        if sub in ("挂单", "出售", "卖"):
            # /nw贸易 挂单 <给出资源> <数量> <索要资源> <数量>
            if len(ctx.args) < 5:
                return ("用法：/nw贸易 挂单 <给出资源> <数量> <索要资源> <数量>\n"
                        "例：/nw贸易 挂单 钢材 5000 石油 2000\n"
                        "挂出的单任何人（非交战国）都能接；看别人的单：/nw贸易 市场")
            gr = dip._res_key(self.cfg, ctx.args[1])
            wr = dip._res_key(self.cfg, ctx.args[3])
            if not gr or not wr:
                return ("❌ 资源名无法识别。可交易：" +
                        "、".join(dip.res_name(self.cfg, r) for r in dip.TRADABLE))
            try:
                ga, wa = float(ctx.args[2]), float(ctx.args[4])
            except (TypeError, ValueError):
                return "❌ 数量应为数字"
            ok, text = dip.offer_order(self.conn, self.cfg, ctx.qq, gr, ga, wr, wa)
            return text
        if sub in ("撤单", "撤销"):
            if len(ctx.args) < 2:
                return "用法：/nw贸易 撤单 <编号>"
            try:
                oid = int(ctx.args[1].lstrip("#"))
            except (TypeError, ValueError):
                return "❌ 编号应为数字"
            ok, text = dip.cancel_order(self.conn, self.cfg, ctx.qq, oid)
            return text
        if sub in ("接受", "拒绝"):
            if len(ctx.args) < 2:
                return f"用法：/nw贸易 {sub} <编号>"
            try:
                oid = int(ctx.args[1].lstrip("#"))
            except (TypeError, ValueError):
                return "❌ 编号应为数字"
            if sub == "接受":
                ok, text = dip.accept_trade(self.conn, self.cfg, ctx.qq, oid)
            else:
                ok, text = dip.reject_trade(self.conn, self.cfg, ctx.qq, oid)
            return text
        if sub in ("列表", "查看"):
            return dip.list_trades(self.conn, self.cfg, ctx.qq)
        # /nw贸易 <玩家> <给出资源> <数量> <索要资源> <数量>
        if len(ctx.args) < 5:
            return ("用法：/nw贸易 <玩家> <给出资源> <数量> <索要资源> <数量>\n"
                    "例：/nw贸易 乙方 钢材 5000 石油 2000\n"
                    "可交易资源：" + "、".join(dip.res_name(self.cfg, r)
                                               for r in dip.TRADABLE)
                    + "\n查看：/nw贸易　接受：/nw贸易接受 <编号>")
        pl = self._player_arg(sub)
        if not pl:
            return f"❌ 找不到玩家【{sub}】"
        gr = dip._res_key(self.cfg, ctx.args[1])
        wr = dip._res_key(self.cfg, ctx.args[3])
        if not gr or not wr:
            return ("❌ 资源名无法识别。可交易：" +
                    "、".join(dip.res_name(self.cfg, r) for r in dip.TRADABLE))
        try:
            ga, wa = float(ctx.args[2]), float(ctx.args[4])
        except (TypeError, ValueError):
            return "❌ 数量应为数字"
        ok, text = dip.offer_trade(self.conn, self.cfg, ctx.qq, pl["qq"], gr, ga,
                                   wr, wa)
        return text

    async def lease_cmd(self, ctx: Ctx) -> str:
        """租港 <玩家> <坐标>：§6.2 军港租借。"""
        from . import diplomacy as dip
        if len(ctx.args) >= 1 and ctx.args[0] in ("列表", "查看"):
            ls = dip.active_leases(self.conn, ctx.qq)
            if not ls:
                return ("⚓ 你没有租借中的军港。\n"
                        "用法：/nw租港 <盟友> <坐标>（需先 /nw同盟）")
            out = ["⚓ 你的租借军港："]
            for l in ls:
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (l["owner_qq"],)).fetchone()
                left = max(0, (l["expire_at"] - int(__import__("time").time())) // 86400)
                out.append(f"　({l['x']},{l['y']})　东家【{nm['name'] if nm else '?'}】"
                           f"　剩约 {left} 天")
            return "\n".join(out)
        if len(ctx.args) < 2:
            return "用法：/nw租港 <盟友> <坐标>　查看：/nw租港 列表"
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        xy = fleetm.parse_xy(ctx.args[1])
        if not xy:
            return "❌ 坐标格式示例：/nw租港 乙方 234,567"
        ok, text = dip.lease_port(self.conn, self.cfg, ctx.qq, pl["qq"], xy[0], xy[1])
        return text

    async def spy_cmd(self, ctx: Ctx) -> str:
        """间谍 <玩家> <行动>：§6.2 破坏建筑/窃取蓝图/窃取情报/煽动叛乱。"""
        from . import diplomacy as dip
        from . import relations as _rel
        # §6.2 中立观察国不能使用间谍
        if dip.ncfg(self.cfg).get("block_spy", True) and \
                _rel.is_neutral(self.conn, ctx.qq):
            return ("❌ 你是中立观察国，不能对他国使用间谍（§6.2）。\n"
                    "先 /nw中立 退出中立。")
        ops = dip.spy_ops(self.cfg)
        if not ctx.args:
            sc = dip.dcfg(self.cfg).get("spy") or {}
            lines = ["🕵️ 间谍行动（§6.2）",
                     f"　经费：资金{sc.get('cost_money', 1200)} + "
                     f"芯片{sc.get('cost_chips', 20)}　冷却 {sc.get('cooldown_hours', 6)}h",
                     ""]
            for k, v in ops.items():
                lines.append(f"　{v['name']}({k})　成功率 {v['success']:.0%}　{v['desc']}")
            lines.append("")
            lines.append("用法：/nw间谍 <玩家> <行动>")
            return "\n".join(lines)
        if len(ctx.args) < 2:
            return "用法：/nw间谍 <玩家> <行动>（不带参数看行动列表）"
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        op = ctx.args[1]
        for k, v in ops.items():
            if v["name"] == op:
                op = k
                break
        ok, text = dip.run_spy(self.conn, self.cfg, ctx.qq, pl["qq"], op)
        return text

    async def reparations_cmd(self, ctx: Ctx) -> str:
        """索赔 <玩家> [赔款金额] [坐标...]：§6.2 赔款割岛。"""
        from . import diplomacy as dip
        if len(ctx.args) < 2:
            return ("用法：/nw索赔 <玩家> <赔款金额> [割让坐标...]\n"
                    "例：/nw索赔 乙方 5000 234,567 240,570\n"
                    "§6.2：需先在对该玩家的会战中获胜才有权索取。")
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        try:
            money = float(ctx.args[1])
        except (TypeError, ValueError):
            return "❌ 赔款金额应为数字（不要可填 0）"
        islands = []
        for a in ctx.args[2:]:
            xy = fleetm.parse_xy(a)
            if xy:
                islands.append(xy)
        tick = meta_get(self.conn, "war_tick", int, 0)
        ok, text = dip.demand_reparations(self.conn, self.cfg, ctx.qq, pl["qq"],
                                          money, islands, tick)
        return text

    async def nap_cmd(self, ctx: Ctx) -> str:
        """互不侵犯 [玩家] [天数]：§6.2 缔结互不侵犯条约。"""
        from . import diplomacy as dip
        if not ctx.args:
            return dip.list_treaties(self.conn, self.cfg, ctx.qq)
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        days = None
        if len(ctx.args) > 1:
            try:
                days = int(ctx.args[1])
            except (TypeError, ValueError):
                return "❌ 天数应为整数"
        ok, text = dip.declare_nap(self.conn, self.cfg, ctx.qq, pl["qq"], days)
        return text

    async def vassalize_cmd(self, ctx: Ctx) -> str:
        """附庸 <玩家>：§6.2 使对方成为自己的附庸（需实力 2 倍）。"""
        from . import diplomacy as dip
        if not ctx.args:
            return dip.list_treaties(self.conn, self.cfg, ctx.qq)
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        ok, text = dip.declare_vassal(self.conn, self.cfg, ctx.qq, pl["qq"])
        return text

    async def break_treaty_cmd(self, ctx: Ctx) -> str:
        """解约 <玩家>：§6.2 撕毁条约（承担违约恶名，§26.1）。"""
        from . import diplomacy as dip
        if not ctx.args:
            return ("用法：/nw解约 <玩家>\n"
                    "§6.2：单方面撕毁条约/脱离保护关系会涨违约恶名。\n"
                    "查看现有条约：/nw互不侵犯")
        pl = self._player_arg(ctx.args[0])
        if not pl:
            return f"❌ 找不到玩家【{ctx.args[0]}】"
        ok, text = dip.break_treaty(self.conn, self.cfg, ctx.qq, pl["qq"])
        return text

    async def neutral_cmd(self, ctx: Ctx) -> str:
        """中立 [退出]：§6.2 宣布/退出中立观察国。"""
        from . import diplomacy as dip
        p = self._player(ctx.qq)
        if not p:
            return "❌ 先 /nw注册"
        if ctx.args and ctx.args[0] in ("退出", "取消", "结束"):
            ok, text = dip.withdraw_neutral(self.conn, self.cfg, ctx.qq)
            return text
        if p["is_neutral"]:
            st = dip.neutral_status(self.conn, self.cfg, ctx.qq)
            return (f"{st}\n"
                    f"你要退出中立吗？/nw中立 退出\n"
                    f"（§6.2：中立期间任何玩家都无法攻击你，"
                    f"但你也无法宣战与使用间谍）")
        ok, text = dip.declare_neutral(self.conn, self.cfg, ctx.qq)
        return text

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
        d = chart_data(self.conn, ctx.cfg, ctx.qq)
        if not d:
            return "❌ 还未注册，先 /nw注册"
        cx, cy = d["center"]
        w, h, x0, y0 = d["w"], d["h"], d["x0"], d["y0"]
        vis = d.get("vision")
        # 迷雾：看不见的格子用 ~ 表示未探明
        grid = [["~" if (vis and not vis[ry][rx]) else "."
                 for rx in range(w)] for ry in range(h)]
        for isl in d["islands"]:
            ch = {"capital": "H", "own": "#", "other": "o", "regular": "R"}[isl["kind"]]
            grid[isl["y"] - y0][isl["x"] - x0] = ch
        marks = []
        for pr in d["pirates"]:
            ch = {"patrol": "x", "base": "P", "merchant": "M", "enforcer": "E",
                  "regular": "R", "empire": "$", "guild": "G", "rebel": "b",
                  "merc": "m"}[pr["kind"]]
            grid[pr["y"] - y0][pr["x"] - x0] = ch
            marks.append(f"{ch}=【{pr['name']}】({pr['x']},{pr['y']})")
        for f in d["fleets"]:
            grid[f["y"] - y0][f["x"] - x0] = "F"
            marks.append(f"F={f['name']}({f['x']},{f['y']})")
        # §26.4/§11 残骸
        for wk in d.get("wrecks", []):
            grid[wk["y"] - y0][wk["x"] - x0] = "w"
            kn = "雲墨残骸" if wk["kind"] == "yunmo" else "沉船遗迹"
            marks.append(f"w={kn}({wk['x']},{wk['y']}) 剩{wk['remaining']}次")
        head = f"🗺️ 以首都({cx},{cy})为中心（北↑，每格约20km）"
        body = "\n".join("".join(row) for row in grid)
        legend = ("图例：H首都　#己方岛　o其他岛　R正规军　P海盗据点　x海盗巡逻队"
                  "　M中立商船　$帝国商船　G协会　b叛军　m雇佣军团　E执法者"
                  "　w残骸　F己方舰队" + ("　~未探明" if d.get("fog") else ""))
        tail = "\n".join(marks[:8])
        if d.get("fog") and not marks:
            tail = "（视野内暂无可攻击目标；派舰队出航可扩大视野）"
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
        if atype == "landing":
            return await self._action_landing(ctx, action, choice)
        if atype == "hire":
            return await self._action_hire(ctx, action, choice)
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
        from . import gov as _gov
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消。"
        if choice not in ("1", "3"):
            return "回复 1 确认　2 取消　3 加急（多付资源立即完成）"
        self.confirm.drop(ctx.origin, ctx.qq)
        rush = (choice == "3")
        # 确认时按当前状态重新校验并执行（资源/队列/槽位可能已变）
        ok, text = _gov.do_build(self.conn, self.cfg, ctx.qq, ctx.origin,
                                 action["def_id"], action["x"], action["y"], rush)
        return text

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
        # §26.2 中立商船队：有商船可跑贸易线，也可破交掠夺（涨恶名）
        if aiworld.ensure_merchants(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                                    meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n🛳 附近出现中立商船队在跑航线（/nw海图 可见）。"
                    "可用 /nw破交 <舰队> <坐标> <半径> 劫掠，或避而远之。")
        # §26.2 正规军：占岛建国的领土 AI（海图 R 标记）
        if aiworld.ensure_regular(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                                  meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n🏛 外海出现了正规军势力（海图 R 标记）。他们会占岛扩张，"
                    "也会编队巡弋——可打可避，击沉不涨恶名。")
        # §26.2 帝国商船队（全图可见，暴富路线）+ 协会巡逻队（反海盗）
        if aiworld.ensure_empire_merchant(self.conn, self.cfg, p["capital_x"],
                                          p["capital_y"],
                                          meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n💎 帝国商船队正在跑航线（海图 $ 标记，全图可见）。"
                    "满载稀土铝材——洗劫可暴富，但恶名 +10、通缉热度 +35，"
                    "会招来帝国军与执法者。默认和平，需 /nw宣战 帝国 才能动手。")
        if aiworld.ensure_guild_patrol(self.conn, self.cfg, p["capital_x"],
                                       p["capital_y"],
                                       meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n🛡 商人协会派出了反海盗巡逻队（海图 G 标记）。"
                    "他们专打海盗/叛军，对你默认中立。")
        # §26.4 雲墨：远海固定停泊的终局事件（绝不主动攻击）
        if aiworld.ensure_yunmo(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                                meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n☢️ 远海侦测到【雲墨の实验型舰队 L80】——本作终局事件。\n"
                    "它绝不主动攻击，但主动打它需输入防误触短语（见 /nw攻击）。")
        # §26.2 雇佣军团：花钱买战斗力的唯一路径
        if aiworld.ensure_merc(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                               meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n💰 外海有雇佣军团在漂游找合同（海图 m 标记）。"
                    "派舰队靠近 5 格内即可 /nw雇佣 谈价，用资金+油+补给换一支现成舰队。")
        # §11 沉船遗迹
        from . import salvage as _sv
        if _sv.ensure_wrecks(self.conn, self.cfg, p["capital_x"], p["capital_y"],
                             meta_get(self.conn, "war_tick", int, 0)):
            tip += ("\n⚓ 附近海域发现沉船遗迹（§11，海图 w 标记）。"
                    "派舰队过去 /nw打捞 可得钢铁/资金/蓝图碎片——但有触雷伏击风险。")
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
        from . import gov as _gov
        if choice == "2":
            self.confirm.drop(ctx.origin, ctx.qq)
            return "已取消。"
        if choice not in ("1", "3"):
            return "回复 1 确认　2 取消" + (
                "　3 加急（多付资源立即下水）" if _gov.rush_enabled(self.cfg) else "")
        rush = (choice == "3")
        p = self._player(ctx.qq)
        cost = dict(action["cost"])
        if rush:
            if not _gov.rush_enabled(self.cfg):
                self.confirm.drop(ctx.origin, ctx.qq)
                return "❌ 本服未开启加急"
            cost = _gov._rush_cost(self.cfg, cost)
        miss = _missing_res(p, cost)
        if miss:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 资源已不足：缺 " + "、".join(miss)
        yard = research.building_level(self.conn, action["x"], action["y"], "shipyard")
        active = self.conn.execute(
            "SELECT COUNT(*) c FROM production_queue WHERE qq=?", (ctx.qq,)).fetchone()["c"]
        if active >= yard:
            self.confirm.drop(ctx.origin, ctx.qq)
            return "❌ 船台刚被占满，报价失效。"
        self._deduct(ctx.qq, cost)
        d = self.conn.execute("SELECT id,name,tier,cost_json,stats_json FROM designs"
                              " WHERE id=?", (action["design_id"],)).fetchone()
        tick = meta_get(self.conn, "econ_tick", int, 0)
        if rush:
            # 立即下水：直接生成舰船实例，不进队列
            stats = json.loads(d["stats_json"] or "{}")
            hp = stats.get("hp", 0)
            snap = json.dumps({"stats": stats,
                               "cost": json.loads(d["cost_json"] or "{}")},
                              ensure_ascii=False)
            for _ in range(max(1, action["qty"])):
                self.conn.execute(
                    "INSERT INTO ships(qq,fleet_id,def_id,name,tier,hp,max_hp,data_json)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (ctx.qq, None, str(d["id"]), d["name"], d["tier"], hp, hp, snap))
            self.conn.commit()
            self.confirm.drop(ctx.origin, ctx.qq)
            return f"⚡【{d['name']}】×{action['qty']} 加急下水！已入港，可编入舰队。"
        self.conn.execute(
            "INSERT INTO production_queue(qq,origin,x,y,design_id,qty,start_tick,end_tick)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ctx.qq, ctx.origin, action["x"], action["y"], action["design_id"],
             action["qty"], tick, action["end_tick"]))
        self.conn.commit()
        self.confirm.drop(ctx.origin, ctx.qq)
        return f"🚢【{d['name']}】×{action['qty']} 已上船台，完工自动推送。/nw船坞 查看"

    # ---------- 共用工具 ----------
    def _deduct(self, qq: str, cost: dict):
        for k, v in cost.items():
            if v:
                self.conn.execute(f"UPDATE players SET {k}=COALESCE({k},0)-? WHERE qq=?",
                                  (v, qq))
