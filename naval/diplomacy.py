"""§6.2 外交扩展：资源贸易、军港租借、间谍、赔款割岛。

设计文档 §6.2 的清单：
    宣战/媾和、白和平、赔款割岛、互不侵犯、同盟、联军作战（共享视野/基地）、
    军港租借、资源贸易、违约恶名、附庸/保护国、间谍（破坏建筑、策反岛屿、
    窃取蓝图、假情报）、中立观察国。

其中 宣战/媾和（第 23 轮）、同盟与联军作战（第 24 轮）已实现，本模块补上
资源贸易、军港租借、间谍、赔款割岛。
"""
import json
import logging
import random
import time

from . import relations


logger = logging.getLogger("naval")

PENDING, ACCEPTED, REJECTED, EXPIRED = "pending", "accepted", "rejected", "expired"
CANCELLED = "cancelled"
# to_qq 为空字符串表示「公开挂单」（§6.2 资源贸易·市场撮合），任何人可接
# 可用于贸易/赔偿的资源
TRADABLE = ("money", "steel", "oil", "food", "aluminium", "rare_earth", "chips",
            "manpower", "supply")
RES_ZH = {"money": "资金", "steel": "钢材", "oil": "石油", "food": "食物",
          "aluminium": "铝材", "rare_earth": "稀土", "chips": "芯片",
          "manpower": "人力", "supply": "补给", "science": "科研点"}



def _fmt_min(m) -> str:
    """分钟数 → 人话（与 combat/game 的 _fmt_min 同口径）。"""
    m = float(m)
    if m < 60:
        return f"{m:g} 分钟"
    h, mm = divmod(int(round(m)), 60)
    if h < 24:
        return (f"{h} 小时 " + f"{mm} 分") if mm else f"{h} 小时"
    d, hh = divmod(h, 24)
    return (f"{d} 天 " + f"{hh} 小时") if hh else f"{d} 天"

def dcfg(cfg: dict) -> dict:
    return cfg.get("diplomacy") or {}


def res_name(cfg: dict, key: str) -> str:
    return RES_ZH.get(key, key)


def _res_key(cfg: dict, text: str):
    """把用户输入（中文名或英文 id）解析成资源键。"""
    t = (text or "").strip()
    if t in TRADABLE:
        return t
    for k in TRADABLE:
        if RES_ZH.get(k) == t:
            return k
    return None


# ---------------- §6.2 资源贸易 ----------------

def _validate_trade(cfg, give_res, give_amt, want_res, want_amt):
    """报价的公共校验。返回 (ok, msg)。"""
    t = dcfg(cfg).get("trade") or {}
    mx = float(t.get("max_amount", 10 ** 6))
    if give_res not in TRADABLE or want_res not in TRADABLE:
        return False, ("❌ 可交易资源：" + "、".join(res_name(cfg, r) for r in TRADABLE))
    if give_amt <= 0 or want_amt <= 0:
        return False, "❌ 数量必须为正"
    if give_amt > mx or want_amt > mx:
        return False, f"❌ 单项数量不得超过 {mx:.0f}"
    if give_res == want_res:
        return False, "❌ 同类资源互换没有意义"
    return True, ""


def offer_order(conn, cfg, a_qq: str, give_res: str, give_amt: float,
                want_res: str, want_amt: float):
    """§6.2 资源贸易·公开挂单：挂到市场，任何非交战国都能接。返回 (ok, text)。"""
    t = dcfg(cfg).get("trade") or {}
    ok, msg = _validate_trade(cfg, give_res, give_amt, want_res, want_amt)
    if not ok:
        return False, msg
    mx_open = int(t.get("market_max_open", 5))
    cnt = conn.execute("SELECT COUNT(*) c FROM trade_offers WHERE from_qq=? AND"
                       " to_qq='' AND status=?", (a_qq, PENDING)).fetchone()["c"]
    if cnt >= mx_open:
        return False, (f"❌ 你最多同时挂 {mx_open} 张单，当前 {cnt} 张。"
                       f"用 /nw贸易 撤单 <编号> 撤掉一些")
    p = conn.execute(f"SELECT {give_res} FROM players WHERE qq=?", (a_qq,)).fetchone()
    have = float(p[give_res] or 0)
    if have < give_amt:
        return False, (f"❌ {res_name(cfg, give_res)}不足：需要 {give_amt:.0f}，"
                       f"现有 {have:.0f}")
    now = int(time.time())
    conn.execute(
        "INSERT INTO trade_offers(from_qq,to_qq,give_res,give_amt,want_res,want_amt,"
        "status,created_at,expire_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (a_qq, "", give_res, give_amt, want_res, want_amt, PENDING, now,
         now + int(t.get("expire_hours", 24)) * 3600))
    conn.commit()
    oid = conn.execute("SELECT MAX(id) m FROM trade_offers").fetchone()["m"]
    return True, (f"🏪 已挂单 #{oid}（公开）：\n"
                  f"　你给 {res_name(cfg, give_res)}{give_amt:.0f}　"
                  f"换 {res_name(cfg, want_res)}{want_amt:.0f}\n"
                  f"任何人可用 /nw贸易接受 {oid} 接单（"
                  f"{t.get('expire_hours', 24)} 小时内有效）\n"
                  f"撤单：/nw贸易 撤单 {oid}")


def list_market(conn, cfg, qq: str, limit: int = 15) -> str:
    """§6.2 市场：其他人挂出的公开单。"""
    now = int(time.time())
    conn.execute("UPDATE trade_offers SET status=? WHERE status=? AND expire_at<?",
                 (EXPIRED, PENDING, now))
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM trade_offers WHERE to_qq='' AND status=? AND from_qq!=?"
        " ORDER BY id DESC LIMIT ?", (PENDING, qq, max(1, min(50, limit)))).fetchall()
    mine = conn.execute(
        "SELECT COUNT(*) c FROM trade_offers WHERE to_qq='' AND status=? AND from_qq=?",
        (PENDING, qq)).fetchone()["c"]
    if not rows:
        return (f"🏪 市场上暂时没有别人挂的单。\n"
                f"（你自己有 {mine} 张在挂）\n"
                f"挂单：/nw贸易 挂单 <给出资源> <数量> <索要资源> <数量>\n"
                f"例：/nw贸易 挂单 钢材 5000 石油 2000")
    out = [f"🏪 市场公开挂单（你自己有 {mine} 张在挂）："]
    for r in rows:
        nm = conn.execute("SELECT name FROM players WHERE qq=?",
                          (r["from_qq"],)).fetchone()
        # 与挂单方处于战争状态就不能接
        at_war = relations.get_pvp_state(conn, cfg, qq, r["from_qq"]) == relations.WAR
        tag = "　⚔ 交战中，不可接单" if at_war else ""
        out.append(f"　#{r['id']}　【{nm['name'] if nm else '?'}】给 "
                   f"{res_name(cfg, r['give_res'])}{r['give_amt']:.0f} ↔ 要 "
                   f"{res_name(cfg, r['want_res'])}{r['want_amt']:.0f}{tag}")
    out.append("")
    out.append("接单：/nw贸易接受 <编号>")
    return "\n".join(out)


def cancel_order(conn, cfg, qq: str, oid: int):
    """撤掉自己挂的公开单。"""
    r = conn.execute("SELECT * FROM trade_offers WHERE id=?", (oid,)).fetchone()
    if not r:
        return False, f"❌ 找不到单号 #{oid}"
    if r["to_qq"] != "":
        return False, f"❌ #{oid} 不是公开挂单（定向报价请让对方拒绝）"
    if r["from_qq"] != qq:
        return False, "❌ 这不是你挂的单"
    if r["status"] != PENDING:
        return False, f"❌ 单 #{oid} 已是 {r['status']} 状态"
    conn.execute("UPDATE trade_offers SET status=? WHERE id=?", (CANCELLED, oid))
    conn.commit()
    return True, f"🗑 已撤下挂单 #{oid}"


def offer_trade(conn, cfg, a_qq: str, b_qq: str, give_res: str, give_amt: float,
                want_res: str, want_amt: float):
    """发起一笔定向报价。返回 (ok, text)。"""
    t = dcfg(cfg).get("trade") or {}
    if a_qq == b_qq:
        return False, "❌ 不能和自己交易"
    ok, msg = _validate_trade(cfg, give_res, give_amt, want_res, want_amt)
    if not ok:
        return False, msg
    b = conn.execute("SELECT qq,name FROM players WHERE qq=?", (b_qq,)).fetchone()
    if not b:
        return False, "❌ 找不到该玩家"
    if relations.get_pvp_state(conn, cfg, a_qq, b_qq) == relations.WAR:
        return False, "❌ 与交战国通商等同资敌（§6.2）；请先 /nw媾和"
    p = conn.execute(f"SELECT {give_res} FROM players WHERE qq=?", (a_qq,)).fetchone()
    have = float(p[give_res] or 0)
    if have < give_amt:
        return False, (f"❌ {res_name(cfg, give_res)}不足：需要 {give_amt:.0f}，"
                       f"现有 {have:.0f}")
    conn.execute(
        "INSERT INTO trade_offers(from_qq,to_qq,give_res,give_amt,want_res,want_amt,"
        "status,created_at,expire_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (a_qq, b_qq, give_res, give_amt, want_res, want_amt, PENDING, int(time.time()),
         int(time.time()) + int(t.get("expire_hours", 24)) * 3600))
    conn.commit()
    oid = conn.execute("SELECT MAX(id) m FROM trade_offers").fetchone()["m"]
    return True, (f"📜 已向【{b['name']}】发出贸易报价 #{oid}：\n"
                  f"　你给 {res_name(cfg, give_res)}{give_amt:.0f}　"
                  f"换 {res_name(cfg, want_res)}{want_amt:.0f}\n"
                  f"对方用 /nw贸易接受 {oid} 或 /nw贸易拒绝 {oid}（"
                  f"{t.get('expire_hours', 24)} 小时内有效）")


def list_trades(conn, cfg, qq: str) -> str:
    """列出与自己有关的报价（收到的在前）。"""
    now = int(time.time())
    conn.execute("UPDATE trade_offers SET status=? WHERE status=? AND expire_at<?",
                 (EXPIRED, PENDING, now))
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM trade_offers WHERE ((to_qq=? OR from_qq=?) AND to_qq!='')"
        " OR (to_qq='' AND from_qq=?) AND status=?"
        " ORDER BY id DESC LIMIT 15", (qq, qq, qq, PENDING)).fetchall()
    if not rows:
        return ("📭 没有待处理的贸易报价。\n"
                "定向：/nw贸易 <玩家> <给出资源> <数量> <索要资源> <数量>\n"
                "挂单：/nw贸易 挂单 <给出资源> <数量> <索要资源> <数量>\n"
                "看市场：/nw贸易 市场")
    out = ["📜 待处理贸易报价："]
    for r in rows:
        if (r["to_qq"] or "") == "":
            out.append(f"　#{r['id']}（我挂的公开单）　"
                       f"给 {res_name(cfg, r['give_res'])}{r['give_amt']:.0f} ↔ "
                       f"要 {res_name(cfg, r['want_res'])}{r['want_amt']:.0f}")
            continue
        who = "我发出" if r["from_qq"] == qq else "我收到"
        nm = conn.execute("SELECT name FROM players WHERE qq=?",
                          (r["to_qq"] if r["from_qq"] == qq else r["from_qq"],)
                          ).fetchone()
        out.append(f"　#{r['id']}（{who}）与【{nm['name'] if nm else '?'}】　"
                   f"给 {res_name(cfg, r['give_res'])}{r['give_amt']:.0f} ↔ "
                   f"要 {res_name(cfg, r['want_res'])}{r['want_amt']:.0f}")
    out.append("")
    out.append("接受：/nw贸易接受 <编号>　拒绝：/nw贸易拒绝 <编号>")
    return "\n".join(out)


def accept_trade(conn, cfg, qq: str, oid: int):
    """接受一笔报价：双向扣付与到账。"""
    r = conn.execute("SELECT * FROM trade_offers WHERE id=?", (oid,)).fetchone()
    if not r:
        return False, f"❌ 找不到报价 #{oid}"
    if r["status"] != PENDING:
        return False, f"❌ 报价 #{oid} 已是 {r['status']} 状态"
    is_public = (r["to_qq"] or "") == ""
    if is_public:
        if r["from_qq"] == qq:
            return False, "❌ 这是你自己挂的单，不能自己接"
        # 公开单在「接单时」再查一次战争状态（关系可能已变）
        if relations.get_pvp_state(conn, cfg, qq, r["from_qq"]) == relations.WAR:
            return False, "❌ 你与挂单方处于交战状态，不可接单（§6.2）"
    elif r["to_qq"] != qq:
        return False, "❌ 这笔报价不是发给你的"
    if int(r["expire_at"] or 0) < int(time.time()):
        conn.execute("UPDATE trade_offers SET status=? WHERE id=?", (EXPIRED, oid))
        conn.commit()
        return False, f"❌ 报价 #{oid} 已过期"
    # 复核双方库存（期间可能已变动）
    a = conn.execute(f"SELECT {r['give_res']} FROM players WHERE qq=?",
                     (r["from_qq"],)).fetchone()
    b = conn.execute(f"SELECT {r['want_res']} FROM players WHERE qq=?",
                     (qq,)).fetchone()
    if float(a[r["give_res"]] or 0) < float(r["give_amt"]):
        return False, f"❌ 对方已无足够 {res_name(cfg, r['give_res'])}，交易取消"
    if float(b[r["want_res"]] or 0) < float(r["want_amt"]):
        return False, (f"❌ 你的 {res_name(cfg, r['want_res'])} 不足：需要 "
                       f"{float(r['want_amt']):.0f}，现有 {float(b[r['want_res']] or 0):.0f}")
    tax = float(r["give_amt"]) * float((dcfg(cfg).get("trade") or {})
                                       .get("tax_rate", 0.02))
    conn.execute(f"UPDATE players SET {r['give_res']}={r['give_res']}-? WHERE qq=?",
                 (float(r["give_amt"]), r["from_qq"]))
    conn.execute(f"UPDATE players SET {r['give_res']}={r['give_res']}+? WHERE qq=?",
                 (float(r["give_amt"]) - tax, qq))
    conn.execute(f"UPDATE players SET {r['want_res']}={r['want_res']}-? WHERE qq=?",
                 (float(r["want_amt"]), qq))
    conn.execute(f"UPDATE players SET {r['want_res']}={r['want_res']}+? WHERE qq=?",
                 (float(r["want_amt"]), r["from_qq"]))
    conn.execute("UPDATE trade_offers SET status=?, to_qq=? WHERE id=?",
                 (ACCEPTED, qq, oid))
    conn.commit()
    seller = conn.execute("SELECT name FROM players WHERE qq=?",
                          (r["from_qq"],)).fetchone()
    head = (f"🤝 市场单 #{oid} 成交！（挂单方【{seller['name'] if seller else '?'}】）"
            if is_public else f"🤝 交易 #{oid} 成交！")
    return True, (f"{head}\n"
                  f"　对方获得 {res_name(cfg, r['want_res'])}{r['want_amt']:.0f}\n"
                  f"　你获得 {res_name(cfg, r['give_res'])}"
                  f"{float(r['give_amt']) - tax:.0f}（抽税 {tax:.0f}）")


def reject_trade(conn, cfg, qq: str, oid: int):
    r = conn.execute("SELECT * FROM trade_offers WHERE id=?", (oid,)).fetchone()
    if not r:
        return False, f"❌ 找不到报价 #{oid}"
    if r["to_qq"] != qq:
        return False, "❌ 这笔报价不是发给你的"
    if r["status"] != PENDING:
        return False, f"❌ 报价 #{oid} 已是 {r['status']} 状态"
    conn.execute("UPDATE trade_offers SET status=? WHERE id=?", (REJECTED, oid))
    conn.commit()
    return True, f"🚫 已拒绝报价 #{oid}"


# ---------------- §6.2 军港租借 ----------------

def active_leases(conn, qq: str, now: int = None) -> list:
    now = now or int(time.time())
    return [dict(r) for r in conn.execute(
        "SELECT * FROM port_leases WHERE tenant_qq=? AND expire_at>?", (qq, now)).fetchall()]


def lease_port(conn, cfg, a_qq: str, b_qq: str, x: int, y: int):
    """在盟友岛屿上租借军港。返回 (ok, text)。"""
    lc = dcfg(cfg).get("lease") or {}
    if a_qq == b_qq:
        return False, "❌ 不能租自己的港"
    if not relations.are_allied(conn, cfg, a_qq, b_qq):
        return False, "❌ 军港租借需要同盟关系（§6.2）。先 /nw同盟 <玩家>"
    isl = conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
    if not isl:
        return False, f"❌ ({x},{y}) 没有岛屿"
    if isl["owner_qq"] != b_qq:
        return False, f"❌ ({x},{y}) 不是对方的岛"
    if conn.execute("SELECT 1 FROM port_leases WHERE tenant_qq=? AND x=? AND y=?"
                    " AND expire_at>?", (a_qq, x, y, int(time.time()))).fetchone():
        return False, f"❌ 你已经租着 ({x},{y}) 了"
    cur = active_leases(conn, a_qq)
    mx = int(lc.get("max_active", 3))
    if len(cur) >= mx:
        return False, f"❌ 最多同时租 {mx} 座军港，你已有 {len(cur)} 座"
    cost = float(lc.get("cost_money", 500))
    days = int(lc.get("days", 7))
    per_day = float(lc.get("cost_per_day", 60))
    total = cost + per_day * days
    p = conn.execute("SELECT money FROM players WHERE qq=?", (a_qq,)).fetchone()
    if float(p["money"] or 0) < total:
        return False, (f"❌ 资金不足：租 {days} 天需 {total:.0f}"
                       f"（租金 {cost} + {per_day}/日），现有 {float(p['money'] or 0):.0f}")
    conn.execute("UPDATE players SET money=money-? WHERE qq=?", (total, a_qq))
    # 租金给对方（这是玩家间的资源转移，不凭空消失）
    conn.execute("UPDATE players SET money=money+? WHERE qq=?", (total, b_qq))
    conn.execute(
        "INSERT INTO port_leases(tenant_qq,owner_qq,x,y,start_at,expire_at)"
        " VALUES(?,?,?,?,?,?)",
        (a_qq, b_qq, x, y, int(time.time()),
         int(time.time()) + days * 86400))
    conn.commit()
    nm = conn.execute("SELECT name FROM players WHERE qq=?", (b_qq,)).fetchone()
    return True, (f"⚓ 已在【{nm['name']}】的岛 ({x},{y}) 租下军港 {days} 天。\n"
                  f"支付 {total:.0f} 资金（全额转给对方）。\n"
                  f"获得：该港视野 +{lc.get('vision_bonus', 4)} 格、可在此休整修理。")


def lease_vision_sources(conn, cfg, qq: str):
    """§6.2 租借军港带来的视野源。"""
    bonus = int((dcfg(cfg).get("lease") or {}).get("vision_bonus", 4))
    out = []
    for l in active_leases(conn, qq):
        out.append((l["x"], l["y"], bonus, "lease"))
    return out


# ---------------- §6.2 间谍 ----------------

def spy_ops(cfg: dict) -> dict:
    return (dcfg(cfg).get("spy") or {}).get("ops") or {}


def run_spy(conn, cfg, a_qq: str, b_qq: str, op: str):
    """执行一次间谍行动。返回 (ok, text)。"""
    sc = dcfg(cfg).get("spy") or {}
    ops = spy_ops(cfg)
    if op not in ops:
        return False, ("❌ 未知行动。可用：" +
                       "、".join(f"{v['name']}({k})" for k, v in ops.items()))
    if a_qq == b_qq:
        return False, "❌ 不能对自己下手"
    b = conn.execute("SELECT qq,name FROM players WHERE qq=?", (b_qq,)).fetchone()
    if not b:
        return False, "❌ 找不到该玩家"
    cost_m = float(sc.get("cost_money", 1200))
    cost_c = float(sc.get("cost_chips", 20))
    p = conn.execute("SELECT money,chips FROM players WHERE qq=?", (a_qq,)).fetchone()
    if float(p["money"] or 0) < cost_m or float(p["chips"] or 0) < cost_c:
        return False, (f"❌ 经费不足：需要 资金{cost_m:.0f} + 芯片{cost_c:.0f}，"
                       f"现有 {float(p['money'] or 0):.0f} / {float(p['chips'] or 0):.0f}")
    cd_h = int(sc.get("cooldown_hours", 6))
    last = conn.execute(
        "SELECT MAX(created_at) m FROM spy_ops WHERE from_qq=? AND to_qq=?",
        (a_qq, b_qq)).fetchone()["m"]
    if last and int(time.time()) - int(last) < cd_h * 3600:
        left = cd_h * 3600 - (int(time.time()) - int(last))
        return False, (f"⏳ 对该目标的行动还在冷却中（还需 "
                       f"{_fmt_min(left / 60)}）")
    conn.execute("UPDATE players SET money=money-?, chips=chips-? WHERE qq=?",
                 (cost_m, cost_c, a_qq))
    spec = ops[op]
    success = random.random() < float(spec.get("success", 0.5))
    detail = ""
    if success:
        detail = _apply_spy(conn, cfg, a_qq, b_qq, op)
    else:
        inf = float(sc.get("fail_infamy", 15))
        conn.execute("UPDATE players SET infamy=COALESCE(infamy,0)+?,"
                     " wanted_heat=MIN(100,COALESCE(wanted_heat,0)+?) WHERE qq=?",
                     (inf, inf * 0.5, a_qq))
        # 败露：对方知晓（关系转中立敌对，但不算开战）
        relations.set_pvp_state(conn, b_qq, a_qq, relations.NEUTRAL, 0, "间谍败露")
    conn.execute(
        "INSERT INTO spy_ops(from_qq,to_qq,op,success,detail,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (a_qq, b_qq, op, 1 if success else 0, detail, int(time.time())))
    conn.commit()
    nm = b["name"]
    if success:
        return True, (f"🕵️ 对【{nm}】的「{spec['name']}」行动成功。\n{detail}\n"
                      f"花费 资金{cost_m:.0f} + 芯片{cost_c:.0f}")
    return True, (f"🕵️ 对【{nm}】的「{spec['name']}」行动**败露**！\n"
                  f"对方已察觉你的敌意（关系转中立），恶名 +{inf:.0f}。\n"
                  f"花费 资金{cost_m:.0f} + 芯片{cost_c:.0f}（行动失败不退费）")


def _apply_spy(conn, cfg, a_qq: str, b_qq: str, op: str) -> str:
    """成功后的实际效果。"""
    if op == "sabotage":
        b = conn.execute(
            "SELECT * FROM buildings WHERE x IN (SELECT x FROM islands WHERE owner_qq=?)"
            " ORDER BY RANDOM() LIMIT 1", (b_qq,)).fetchone()
        if not b:
            return "　对方岛上没有可破坏的建筑。"
        newlv = max(0, int(b["level"]) - 1)
        if newlv <= 0:
            conn.execute("DELETE FROM buildings WHERE id=?", (b["id"],))
            return f"　💥 破坏了 ({b['x']},{b['y']}) 的 {b['def_id']}（夷平）"
        conn.execute("UPDATE buildings SET level=? WHERE id=?", (newlv, b["id"]))
        return (f"　💥 ({b['x']},{b['y']}) 的 {b['def_id']} 被降级至 Lv{newlv}")
    if op == "steal":
        # blueprints 的实际结构是 (qq, module_id, obtained_at)——没有 name/consumed
        bp = conn.execute(
            "SELECT * FROM blueprints WHERE qq=? ORDER BY RANDOM() LIMIT 1",
            (b_qq,)).fetchone()
        if not bp:
            return "　对方没有已获得的蓝图可偷。"
        conn.execute("UPDATE blueprints SET qq=? WHERE qq=? AND module_id=?",
                     (a_qq, b_qq, bp["module_id"]))
        name = bp["module_id"]
        try:
            from . import pools
            info = pools.module_info(bp["module_id"]) or {}
            name = info.get("name") or bp["module_id"]
        except Exception:
            logger.exception("[海战模拟器] 取蓝图名称失败（退回 module_id）")
        return f"　📐 窃得蓝图模块：{name}"
    if op == "recon":
        ps = conn.execute("SELECT money,steel,oil,food,manpower FROM players WHERE qq=?",
                          (b_qq,)).fetchone()
        fl = conn.execute("SELECT name,x,y FROM fleets WHERE qq=?", (b_qq,)).fetchall()
        lines = [f"　资金{float(ps['money'] or 0):.0f} 钢{float(ps['steel'] or 0):.0f} "
                 f"油{float(ps['oil'] or 0):.0f} 食物{float(ps['food'] or 0):.0f} "
                 f"人力{float(ps['manpower'] or 0):.0f}"]
        for f in fl[:8]:
            lines.append(f"　舰队【{f['name']}】在 ({f['x']},{f['y']})")
        return "\n".join(lines)
    if op == "unrest":
        isl = conn.execute("SELECT * FROM islands WHERE owner_qq=? ORDER BY RANDOM()"
                           " LIMIT 1", (b_qq,)).fetchone()
        if not isl:
            return "　对方没有可煽动的岛屿。"
        conn.execute("UPDATE islands SET morale=MAX(0,morale-18),"
                     " control=MAX(0,control-12) WHERE x=? AND y=?",
                     (isl["x"], isl["y"]))
        return (f"　🔥 ({isl['x']},{isl['y']}) 爆发骚乱：民心 −18、控制 −12")
    return "　（无效果）"


# ---------------- §6.2 互不侵犯条约 / 附庸保护国 ----------------

def tcfg(cfg: dict) -> dict:
    return dcfg(cfg).get("treaties") or {}


def player_power(conn, cfg, qq: str) -> float:
    """综合实力近似：岛屿 + 舰船 + 资金/1000。用于附庸门槛判定。"""
    isl = conn.execute("SELECT COUNT(*) c FROM islands WHERE owner_qq=?",
                       (qq,)).fetchone()["c"]
    shp = conn.execute("SELECT COUNT(*) c FROM ships WHERE qq=?", (qq,)).fetchone()["c"]
    p = conn.execute("SELECT money FROM players WHERE qq=?", (qq,)).fetchone()
    money = float(p["money"] or 0) if p else 0.0
    return isl * 3.0 + shp * 1.0 + money / 1000.0


def active_treaty(conn, a_qq: str, b_qq: str, kind: str = None, now: int = None):
    """查两人之间生效的条约（双向匹配）。"""
    now = now or int(time.time())
    sql = ("SELECT * FROM treaties WHERE expire_at>? AND"
           " ((a_qq=? AND b_qq=?) OR (a_qq=? AND b_qq=?))")
    args = [now, a_qq, b_qq, b_qq, a_qq]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def declare_nap(conn, cfg, a_qq: str, b_qq: str, days: int = None):
    """§6.2 互不侵犯条约：双方互不攻击，到期自动失效。"""
    t = tcfg(cfg).get("nap") or {}
    if a_qq == b_qq:
        return False, "❌ 不能和自己签约"
    b = conn.execute("SELECT name FROM players WHERE qq=?", (b_qq,)).fetchone()
    if not b:
        return False, "❌ 找不到该玩家"
    if relations.get_pvp_state(conn, cfg, a_qq, b_qq) == relations.WAR:
        return False, "❌ 交战中不能缔结互不侵犯条约，请先 /nw媾和"
    if active_treaty(conn, a_qq, b_qq, relations.NAP):
        return False, f"❌ 你与【{b['name']}】已有生效中的互不侵犯条约"
    d = int(days if days else t.get("days", 7))
    d = max(1, min(d, 90))
    now = int(time.time())
    conn.execute(
        "INSERT INTO treaties(a_qq,b_qq,kind,start_at,expire_at) VALUES(?,?,?,?,?)",
        (a_qq, b_qq, relations.NAP, now, now + d * 86400))
    relations.set_pvp_state(conn, a_qq, b_qq, relations.NAP, 0, "互不侵犯条约")
    relations.set_pvp_state(conn, b_qq, a_qq, relations.NAP, 0, "互不侵犯条约")
    conn.commit()
    return True, (f"📜 已与【{b['name']}】缔结互不侵犯条约（§6.2），有效期 {d} 天。\n"
                  f"期间双方无法互相攻击；单方面撕毁将承担违约恶名。\n"
                  f"撕毁：/nw解约 {b['name']}")


def declare_vassal(conn, cfg, a_qq: str, b_qq: str):
    """§6.2 附庸/保护国：a 成为 b 的宗主。需实力门槛 + 对方同意。"""
    v = tcfg(cfg).get("vassal") or {}
    if a_qq == b_qq:
        return False, "❌ 不能附庸自己"
    b = conn.execute("SELECT name FROM players WHERE qq=?", (b_qq,)).fetchone()
    if not b:
        return False, "❌ 找不到该玩家"
    if active_treaty(conn, a_qq, b_qq, relations.VASSAL):
        return False, f"❌ 【{b['name']}】已在你的保护之下"
    pa, pb = player_power(conn, cfg, a_qq), player_power(conn, cfg, b_qq)
    ratio = float(v.get("require_ratio", 2.0))
    if pb <= 0 or pa < pb * ratio:
        return False, (f"❌ 实力不足：你需要达到对方的 {ratio:.1f} 倍\n"
                       f"　你的实力 {pa:.1f}　对方 {pb:.1f}"
                       f"（需 ≥{pb * ratio:.1f}）\n"
                       f"实力相近的玩家请改用 /nw同盟")
    now = int(time.time())
    conn.execute(
        "INSERT INTO treaties(a_qq,b_qq,kind,start_at,expire_at) VALUES(?,?,?,?,?)",
        (a_qq, b_qq, relations.VASSAL, now, now + 365 * 86400))
    relations.set_pvp_state(conn, a_qq, b_qq, relations.PEACE, 0, "宗主")
    relations.set_pvp_state(conn, b_qq, a_qq, relations.PEACE, 0, "附庸")
    conn.commit()
    rate = float(v.get("tribute_rate", 0.08))
    return True, (f"🏳 已确立保护关系：【{b['name']}】成为你的附庸（§6.2）。\n"
                  f"✅ 宗主协防：附庸挨打时你 15 格内的舰队自动参战（§27.1）\n"
                  f"✅ 上贡：附庸每日向你上缴其资金产出的 {rate:.0%}\n"
                  f"✅ 双方互不攻击\n"
                  f"附庸脱离：对方可用 /nw解约 单方面脱身，但会承担违约恶名。")


def break_treaty(conn, cfg, qq: str, other_qq: str):
    """撕毁/退出条约：承担违约恶名（§26.1）。"""
    tr = active_treaty(conn, qq, other_qq)
    if not tr:
        return False, "❌ 你们之间没有生效中的条约"
    b = conn.execute("SELECT name FROM players WHERE qq=?", (other_qq,)).fetchone()
    kind = tr["kind"]
    if kind == relations.NAP:
        inf = float((tcfg(cfg).get("nap") or {}).get("break_infamy", 25))
        label = "互不侵犯条约"
    else:
        inf = float((tcfg(cfg).get("vassal") or {}).get("break_infamy", 40))
        label = "保护关系"
    conn.execute("UPDATE treaties SET expire_at=0 WHERE id=?", (tr["id"],))
    relations.set_pvp_state(conn, qq, other_qq, relations.NEUTRAL, 0, "违约")
    relations.set_pvp_state(conn, other_qq, qq, relations.NEUTRAL, 0, "对方违约")
    conn.execute("UPDATE players SET infamy=COALESCE(infamy,0)+?,"
                 " wanted_heat=MIN(100,COALESCE(wanted_heat,0)+?) WHERE qq=?",
                 (inf, inf * 0.5, qq))
    conn.commit()
    return True, (f"⚔️ 你单方面退出了与【{b['name'] if b else '?'}】的{label}（§6.2）。\n"
                  f"违约恶名 +{inf:.0f}、通缉热度 +{inf * 0.5:.0f}（§26.1）。\n"
                  f"双方关系退回中立。")


def expire_treaties(conn, cfg) -> list:
    """清理到期条约，并把关系退回和平。返回被清理的说明。"""
    now = int(time.time())
    out = []
    for r in conn.execute("SELECT * FROM treaties WHERE expire_at>0 AND expire_at<=?",
                          (now,)).fetchall():
        conn.execute("UPDATE treaties SET expire_at=0 WHERE id=?", (r["id"],))
        if r["kind"] == relations.NAP:
            for (x, y) in ((r["a_qq"], r["b_qq"]), (r["b_qq"], r["a_qq"])):
                if relations.get_pvp_state(conn, cfg, x, y) == relations.NAP:
                    relations.set_pvp_state(conn, x, y, relations.PEACE, 0, "条约到期")
        out.append(f"{r['kind']}:{r['a_qq']}-{r['b_qq']}")
    if out:
        conn.commit()
    return out


def tribute_tick(conn, cfg, pushes: list) -> None:
    """§6.2 附庸上贡：从附庸资金里抽一部分给宗主。"""
    v = tcfg(cfg).get("vassal") or {}
    if not v.get("tribute_rate"):
        return
    now = int(time.time())
    rate = float(v.get("tribute_rate", 0.08))
    floor = float(v.get("min_tribute", 20))
    for tr in conn.execute("SELECT * FROM treaties WHERE kind=? AND expire_at>?",
                           (relations.VASSAL, now)).fetchall():
        sub, lord = tr["b_qq"], tr["a_qq"]
        p = conn.execute("SELECT money,name FROM players WHERE qq=?", (sub,)).fetchone()
        if not p:
            continue
        amt = max(floor, float(p["money"] or 0) * rate)
        if float(p["money"] or 0) < amt:
            amt = float(p["money"] or 0)
        if amt <= 0:
            continue
        conn.execute("UPDATE players SET money=money-? WHERE qq=?", (amt, sub))
        conn.execute("UPDATE players SET money=money+? WHERE qq=?", (amt, lord))
        lname = conn.execute("SELECT name FROM players WHERE qq=?",
                             (lord,)).fetchone()
        pushes.append((sub, f"🏳 今日向宗主【{lname['name'] if lname else '?'}】"
                            f"上贡 资金{amt:.0f}（§6.2 附庸义务）"))
    conn.commit()


def vassals_of(conn, cfg, lord_qq: str) -> list:
    """该宗主的附庸 qq 列表。"""
    now = int(time.time())
    return [r["b_qq"] for r in conn.execute(
        "SELECT b_qq FROM treaties WHERE kind=? AND a_qq=? AND expire_at>?",
        (relations.VASSAL, lord_qq, now)).fetchall()]


def liege_of(conn, cfg, vassal_qq: str):
    """该附庸的宗主 qq（没有则 None）。"""
    now = int(time.time())
    r = conn.execute("SELECT a_qq FROM treaties WHERE kind=? AND b_qq=? AND expire_at>?",
                     (relations.VASSAL, vassal_qq, now)).fetchone()
    return r["a_qq"] if r else None


def list_treaties(conn, cfg, qq: str) -> str:
    now = int(time.time())
    rows = conn.execute(
        "SELECT * FROM treaties WHERE expire_at>? AND (a_qq=? OR b_qq=?) ORDER BY id",
        (now, qq, qq)).fetchall()
    if not rows:
        return ("📭 你没有生效中的条约。\n"
                "互不侵犯：/nw互不侵犯 <玩家> [天数]\n"
                "附庸：/nw附庸 <玩家>（需实力达对方 2 倍）")
    out = ["📜 你的条约："]
    for r in rows:
        other = r["b_qq"] if r["a_qq"] == qq else r["a_qq"]
        nm = conn.execute("SELECT name FROM players WHERE qq=?", (other,)).fetchone()
        left = max(0, (r["expire_at"] - now) // 86400)
        if r["kind"] == relations.VASSAL:
            role = "你是宗主" if r["a_qq"] == qq else "你是附庸"
            kind = "保护关系"
        else:
            role, kind = "", "互不侵犯"
        out.append(f"　{kind}（与【{nm['name'] if nm else '?'}】）"
                   f"　{role}　剩约 {left} 天")
    out.append("")
    out.append("撕毁：/nw解约 <玩家>（承担违约恶名）")
    return "\n".join(out)

# ---------------- §6.2 中立观察国 ----------------

def ncfg(cfg: dict) -> dict:
    return dcfg(cfg).get("neutral") or {}


def declare_neutral(conn, cfg, qq: str):
    """§6.2 宣布中立：换不可侵犯，代价是不能主动攻击任何玩家。"""
    n = ncfg(cfg)
    p = conn.execute("SELECT name,infamy,is_neutral FROM players WHERE qq=?",
                     (qq,)).fetchone()
    if not p:
        return False, "❌ 未注册"
    if p["is_neutral"]:
        return False, "❌ 你已经处于中立状态"
    for r in conn.execute("SELECT qq FROM players WHERE qq!=?", (qq,)).fetchall():
        if relations.get_pvp_state(conn, cfg, qq, r["qq"]) == relations.WAR:
            return False, "❌ 你正在交战中，不能宣布中立。请先 /nw媾和"
    lim = float(n.get("declare_infamy_limit", 30))
    if float(p["infamy"] or 0) > lim:
        return False, (f"❌ 恶名 {float(p['infamy'] or 0):.0f} 高于 {lim:.0f}，"
                       f"满身血债者不能宣称中立（§6.2）")
    conn.execute("UPDATE players SET is_neutral=1, neutral_since=? WHERE qq=?",
                 (int(time.time()), qq))
    conn.commit()
    return True, (f"🕊️ 你已宣布成为**中立观察国**（§6.2）。\n"
                  f"✅ 任何玩家都无法攻击你（即便宣战也不行）\n"
                  f"❌ 你不能向玩家宣战、不能使用间谍\n"
                  f"❌ 海盗/正规军/叛军等 NPC 仍会照常行动（中立不等于免于世界）\n"
                  f"最短保持 {n.get('min_days', 3)} 天；"
                  f"提前退出将涨恶名 {n.get('withdraw_infamy', 10)}。")


def withdraw_neutral(conn, cfg, qq: str):
    """退出中立。未满最短天数则涨恶名。"""
    n = ncfg(cfg)
    p = conn.execute("SELECT is_neutral,neutral_since FROM players WHERE qq=?",
                     (qq,)).fetchone()
    if not p or not p["is_neutral"]:
        return False, "❌ 你目前不是中立国"
    min_s = int(n.get("min_days", 3)) * 86400
    held = int(time.time()) - int(p["neutral_since"] or 0)
    conn.execute("UPDATE players SET is_neutral=0, neutral_since=0 WHERE qq=?", (qq,))
    inf = float(n.get("withdraw_infamy", 10))
    if held < min_s:
        conn.execute("UPDATE players SET infamy=COALESCE(infamy,0)+? WHERE qq=?",
                     (inf, qq))
        conn.commit()
        left = (min_s - held) // 86400
        return True, (f"⚔️ 你在未满 {n.get('min_days', 3)} 天时退出了中立"
                      f"（还差约 {left} 天）。\n"
                      f"反复横跳被识破：恶名 +{inf:.0f}。\n"
                      f"现在你可以正常宣战与交战了。")
    conn.commit()
    return True, "🕊️ 你已退出中立观察国状态（已满最短期限，无惩罚）。"


def neutral_status(conn, cfg, qq: str) -> str:
    p = conn.execute("SELECT is_neutral,neutral_since FROM players WHERE qq=?",
                     (qq,)).fetchone()
    if not p or not p["is_neutral"]:
        return ""
    n = ncfg(cfg)
    held = (int(time.time()) - int(p["neutral_since"] or 0)) // 86400
    need = int(n.get("min_days", 3))
    tail = "" if held >= need else f"（还需 {need - held} 天才可免费退出）"
    return f"🕊️ 中立观察国{tail}"


def _pvp_wins(conn, a_qq: str, b_qq: str, war_tick: int, window: int = 288) -> bool:
    """§6.2 赔款割岛的判定依据：索取方近期对目标赢过一场玩家会战。

    战报只以 A 方视角书写，故用文案判定胜负：
      「玩家会战胜利」 -> A 赢；「全军覆没」 -> B 赢。
    """
    for r in conn.execute(
            "SELECT sides_json,summary FROM battles WHERE status='over' AND end_tick>?",
            (war_tick - window,)).fetchall():
        try:
            sides = json.loads(r["sides_json"] or "{}")
        except (TypeError, ValueError):
            continue
        A, B = sides.get("A") or {}, sides.get("B") or {}
        if A.get("side") != "player" or B.get("side") != "player":
            continue
        summ = r["summary"] or ""
        a_won = "玩家会战胜利" in summ
        b_won = "全军覆没" in summ
        if A.get("qq") == a_qq and B.get("qq") == b_qq and a_won:
            return True
        if B.get("qq") == a_qq and A.get("qq") == b_qq and b_won:
            return True
    return False


def demand_reparations(conn, cfg, a_qq: str, b_qq: str, money: float = 0,
                       islands: list = None, war_tick: int = 0):
    """媾和时索要赔款/割岛。返回 (ok, text)。

    §6.2：这是战胜方的权利——必须先在对该玩家的会战中获胜，否则无权索取。
    """
    rc = dcfg(cfg).get("reparations") or {}
    islands = islands or []
    max_isl = int(rc.get("max_islands", 3))
    if len(islands) > max_isl:
        return False, f"❌ 一次最多索要 {max_isl} 座岛"
    if money < 0:
        return False, "❌ 赔款金额不能为负"
    b = conn.execute("SELECT qq,name,money FROM players WHERE qq=?", (b_qq,)).fetchone()
    if not b:
        return False, "❌ 找不到该玩家"
    if not _pvp_wins(conn, a_qq, b_qq, war_tick):
        return False, ("❌ 索取赔款/割岛需先在对该玩家的会战中获胜（§6.2）。\n"
                       "打赢一场玩家会战后再来谈判。")
    if money > float(b["money"] or 0):
        return False, (f"❌ 对方资金不足：索要 {money:.0f}，"
                       f"对方只有 {float(b['money'] or 0):.0f}")
    transferred = []
    for (x, y) in islands:
        isl = conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
        if not isl or isl["owner_qq"] != b_qq:
            return False, f"❌ ({x},{y}) 不是对方的岛，割让失败（整笔作废）"
        transferred.append((x, y))
    if money > 0:
        conn.execute("UPDATE players SET money=money-? WHERE qq=?", (money, b_qq))
        conn.execute("UPDATE players SET money=money+? WHERE qq=?", (money, a_qq))
    for (x, y) in transferred:
        conn.execute("UPDATE islands SET owner_qq=?, owner_kind='player',"
                     " control=40, morale=50 WHERE x=? AND y=?", (a_qq, x, y))
    conn.commit()
    parts = []
    if money > 0:
        parts.append(f"赔款 资金{money:.0f}")
    if transferred:
        parts.append("割让 " + "、".join(f"({x},{y})" for x, y in transferred))
    if not parts:
        return False, "❌ 没有提出任何条款"
    return True, (f"📜 已向【{b['name']}】索取：" + "、".join(parts) + "\n"
                  f"（§6.2 赔款割岛：割让岛屿控制度重置为 40、民心 50）")
