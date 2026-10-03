"""主力舰法案许可 + 条约配额（设计文档 §19.17 第 50 行 / §14 第 358 行）。

文档要求：「战列巡洋舰、战列舰、航空母舰受服役配额限制」「战列/战巡/航母
另需法案许可 + 条约配额」。本模块把这条落地：

- **法案许可**：不是有蓝图就能造。要先「通过法案」——花钱 + 需要海军学院到一定
  等级（国会/海军部批准），通过后 `players.capital_permit` 置 1，永久有效。
- **服役配额**：同时在役的主力舰数量有上限（`config.capital.quota`）。
  若全服有生效的《海军条约》，配额降到 `quota_with_treaty`（条约配额）。

QQ 指令 `nw法案` / Web 均可查看与申请。
"""
import time

from .db import meta_get, meta_set

TREATY_META_KEY = "naval_treaty_active"
# 升满配额后的「无限」哨兵值（用 is_unlimited() 判断，别直接比大小）
UNLIMITED = 10 ** 9


def ccfg(cfg: dict) -> dict:
    return cfg.get("capital") or {}


def capital_classes(cfg: dict) -> set:
    return set(ccfg(cfg).get("classes") or [])


def is_capital(cfg: dict, cls: str) -> bool:
    return cls in capital_classes(cfg)


def active_treaty(conn) -> bool:
    """全服《海军条约》是否生效（由 meta 标记，可由管理/事件开关）。"""
    try:
        return bool(int(meta_get(conn, TREATY_META_KEY, int, 0)))
    except Exception:
        return False


# ------------------------------------------------ 服役配额升级
def up_cfg(cfg: dict) -> dict:
    return ccfg(cfg).get("quota_upgrade") or {}


def max_level(cfg: dict) -> int:
    return int(up_cfg(cfg).get("max_level", 8))


def per_upgrade(cfg: dict) -> int:
    return int(up_cfg(cfg).get("quota_per_upgrade", 2))


def quota_level(conn, qq: str) -> int:
    r = conn.execute("SELECT capital_quota_lv FROM players WHERE qq=?",
                     (qq,)).fetchone()
    return int((r["capital_quota_lv"] if r and r["capital_quota_lv"] else 0) or 0)


def is_unlimited(conn, cfg: dict, qq: str) -> bool:
    """升满后配额无限（也不受条约限制）。"""
    return quota_level(conn, qq) >= max_level(cfg)


def next_upgrade_cost(cfg: dict, lv: int):
    """第 lv+1 次升级的费用。lv >= max_level 时返回 None（已满）。"""
    if lv >= max_level(cfg):
        return None
    base = int(up_cfg(cfg).get("base_cost_money", 5000))
    mult = int(up_cfg(cfg).get("cost_mult", 10))
    return base * (mult ** lv)


def quota_for(conn, cfg: dict, qq: str = None) -> int:
    """当前服役配额。升满返回无限大哨兵值（配 is_unlimited 判断）。

    注意：配额现在是**按玩家**算的（升级只影响自己），不再只看服务端配置。
    """
    c = ccfg(cfg)
    if qq is None:
        base = int(c.get("quota", 4))
        return base
    lv = quota_level(conn, qq)
    if lv >= max_level(cfg):
        return UNLIMITED
    if active_treaty(conn):
        return int(c.get("quota_with_treaty", 3)) + lv * per_upgrade(cfg)
    return int(c.get("quota", 4)) + lv * per_upgrade(cfg)


def upgrade_quota(conn, cfg: dict, qq: str):
    """花钱提升服役配额。返回 (ok, text)。"""
    if not ccfg(cfg).get("enabled", True):
        return False, "❌ 本服未开启主力舰限制"
    if not has_permit(conn, qq):
        return False, "❌ 先通过法案（/nw法案 申请）才能扩编服役配额"
    lv = quota_level(conn, qq)
    mx = max_level(cfg)
    if lv >= mx:
        return False, f"❌ 服役配额已升满 {mx} 级，当前为**无限**"
    cost = next_upgrade_cost(cfg, lv)
    p = conn.execute("SELECT money,name FROM players WHERE qq=?", (qq,)).fetchone()
    if p is None:
        return False, "❌ 先 /nw注册"
    have = float(p["money"] or 0)
    if have < cost:
        return False, (f"❌ 资金不足：第 {lv+1} 次扩编需 {cost:,}，"
                       f"现有 {have:,.0f}")
    conn.execute("UPDATE players SET capital_quota_lv=capital_quota_lv+1,"
                 " money=COALESCE(money,0)-? WHERE qq=?", (cost, qq))
    conn.commit()
    newlv = lv + 1
    q = quota_for(conn, cfg, qq)
    nxt = next_upgrade_cost(cfg, newlv)
    tail = ("　已达上限，配额**无限**！" if q >= UNLIMITED
            else f"　配额 {q} 艘　下次扩编 {nxt:,} 资金")
    return True, (f"⚓ 服役配额已扩编至第 {newlv}/{mx} 级"
                  f"（耗资 {cost:,}）。{tail}")


def in_service(conn, cfg: dict, qq: str) -> int:
    """当前在役主力舰数（在港/在编 + 在建）。"""
    cls_list = list(capital_classes(cfg))
    if not cls_list:
        return 0
    # ships.def_id 存的是 design 的 id（字符串），要联表才能拿到 ship_class
    rows = conn.execute(
        "SELECT d.ship_class sc, COUNT(*) c FROM ships s"
        " JOIN designs d ON CAST(d.id AS TEXT)=s.def_id"
        " WHERE s.qq=? GROUP BY d.ship_class", (qq,)).fetchall()
    n = sum(r["c"] for r in rows if r["sc"] in cls_list)
    rows2 = conn.execute(
        "SELECT d.ship_class sc, SUM(pq.qty) c FROM production_queue pq"
        " JOIN designs d ON d.id=pq.design_id WHERE pq.qq=? GROUP BY d.ship_class",
        (qq,)).fetchall()
    n += sum(int(r["c"] or 0) for r in rows2 if r["sc"] in cls_list)
    return n


def has_permit(conn, qq: str) -> bool:
    r = conn.execute("SELECT capital_permit FROM players WHERE qq=?",
                     (qq,)).fetchone()
    return bool(r and (r["capital_permit"] or 0))


def status(conn, cfg: dict, qq: str) -> dict:
    q = quota_for(conn, cfg, qq)
    n = in_service(conn, cfg, qq)
    lv = quota_level(conn, qq)
    unlimited = q >= UNLIMITED
    return {"ok": True, "has_permit": has_permit(conn, qq),
            "quota": q, "in_service": n,
            "remaining": (UNLIMITED if unlimited else max(0, q - n)),
            "unlimited": unlimited, "level": lv, "max_level": max_level(cfg),
            "per_upgrade": per_upgrade(cfg),
            "next_cost": next_upgrade_cost(cfg, lv),
            "treaty": active_treaty(conn),
            "classes": sorted(capital_classes(cfg)),
            "permit_money": int(ccfg(cfg).get("permit_cost_money", 50000)),
            "permit_requires": ccfg(cfg).get("permit_requires") or {}}


def check_build(conn, cfg: dict, qq: str, cls: str):
    """造主力舰前的闸门。返回 (ok, msg)。非主力舰直接放行。"""
    if not is_capital(cfg, cls):
        return True, ""
    if not ccfg(cfg).get("enabled", True):
        return True, ""
    if not has_permit(conn, qq):
        return False, ("❌ 主力舰需**法案许可**：你先要通过法案"
                       "（/nw法案 申请，需资金与海军学院等级）")
    q = quota_for(conn, cfg, qq)
    n = in_service(conn, cfg, qq)
    if n >= q:
        tip = "（全服《海军条约》生效中，配额已收紧）" if active_treaty(conn) else ""
        return False, (f"❌ 主力舰已达**服役配额**上限 {n}/{q}{tip}。"
                       f"可 /nw法案 升级 花钱扩编，或等损失/退役")
    return True, ""


def apply_permit(conn, cfg: dict, qq: str, academy_lv: int):
    """通过法案（花资金 + 需海军学院等级）。返回 (ok, text)。"""
    c = ccfg(cfg)
    if not c.get("enabled", True):
        return False, "❌ 本服未开启主力舰限制"
    if has_permit(conn, qq):
        return False, "❌ 你已持有主力舰法案许可，无需重复申请"
    need_money = int(c.get("permit_cost_money", 50000))
    req = c.get("permit_requires") or {}
    need_academy = int(req.get("naval_academy", 0))
    if academy_lv < need_academy:
        return False, (f"❌ 需海军学院 Lv{need_academy}（当前 Lv{academy_lv}）——"
                       f"国会要看到成体系的海军教育才肯批")
    p = conn.execute("SELECT money,name FROM players WHERE qq=?", (qq,)).fetchone()
    if not p or float(p["money"] or 0) < need_money:
        return False, (f"❌ 资金不足：需 {need_money}，"
                       f"现有 {float(p['money'] or 0):.0f}")
    conn.execute("UPDATE players SET money=COALESCE(money,0)-?, capital_permit=1"
                 " WHERE qq=?", (need_money, qq))
    conn.commit()
    q = quota_for(conn, cfg, qq)
    from . import pools as _pools
    zh = "、".join(_pools.mod_data()["classes"].get(c, {}).get("name", c)
                   for c in sorted(capital_classes(cfg)))
    nxt = next_upgrade_cost(cfg, 0)
    return True, (f"🏛 法案通过！【{p['name']}】获得主力舰建造许可"
                  f"（耗资 {need_money}）。\n"
                  f"　服役配额 {q} 艘，当前在役 {in_service(conn, cfg, qq)} 艘。\n"
                  f"　可造：{zh}\n"
                  f"　配额不够可 /nw法案 升级 扩编（第 1 次 {nxt:,} 资金，"
                  f"每次 ×{int(up_cfg(cfg).get('cost_mult', 10))}，"
                  f"升满 {max_level(cfg)} 级后无限）")


def toggle_treaty(conn, cfg: dict, on: bool):
    """开关全服《海军条约》（收紧配额）——沙盒/GM 用。

    返回「未升级玩家的基础配额」，便于调用方观察条约影响。
    """
    meta_set(conn, TREATY_META_KEY, "1" if on else "0")
    conn.commit()
    c = ccfg(cfg)
    return (int(c.get("quota_with_treaty", 3)) if on else int(c.get("quota", 4)))
