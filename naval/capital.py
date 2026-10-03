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


def quota_for(conn, cfg: dict) -> int:
    c = ccfg(cfg)
    if active_treaty(conn):
        return int(c.get("quota_with_treaty", c.get("quota", 4)))
    return int(c.get("quota", 4))


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
    q = quota_for(conn, cfg)
    n = in_service(conn, cfg, qq)
    return {"ok": True, "has_permit": has_permit(conn, qq),
            "quota": q, "in_service": n, "remaining": max(0, q - n),
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
    q = quota_for(conn, cfg)
    n = in_service(conn, cfg, qq)
    if n >= q:
        tip = "（全服《海军条约》生效中，配额已收紧）" if active_treaty(conn) else ""
        return False, (f"❌ 主力舰已达**服役配额**上限 {n}/{q}{tip}。"
                       f"退役或损失后才能再造")
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
    q = quota_for(conn, cfg)
    from . import pools as _pools
    zh = "、".join(_pools.mod_data()["classes"].get(c, {}).get("name", c)
                   for c in sorted(capital_classes(cfg)))
    return True, (f"🏛 法案通过！【{p['name']}】获得主力舰建造许可"
                  f"（耗资 {need_money}）。\n"
                  f"　服役配额 {q} 艘，当前在役 {in_service(conn, cfg, qq)} 艘。\n"
                  f"　可造：{zh}")


def toggle_treaty(conn, cfg: dict, on: bool):
    """开关全服《海军条约》（收紧配额）——沙盒/GM 用。"""
    meta_set(conn, TREATY_META_KEY, "1" if on else "0")
    conn.commit()
    return quota_for(conn, cfg)
