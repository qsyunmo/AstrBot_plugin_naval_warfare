"""沙盒 / GM 功能：给服主自己的账号开挂用。

设计原则：
- **白名单制**：只有 `config.sandbox.qqs` 里列出的 QQ 能用；列表为空 = 全服禁用。
  这样即便插件被部署到公开服务器，也不会因为忘记关开关而人人可开挂。
- **每个入口都校验**：QQ 指令与 Web API 各自调用 `require_gm()`，不依赖调用方自觉。
- 不碰别人的数据：所有操作只作用于传入的 qq。
"""
import json
import time

from . import pools

# 资源字段（与 players 表列名一致）
RES_FIELDS = ("steel", "oil", "aluminium", "rare_earth", "chips", "food",
              "supply", "manpower", "money", "science", "intel")


def sandbox_cfg(cfg: dict) -> dict:
    return cfg.get("sandbox") or {}


def is_gm(cfg: dict, qq: str) -> bool:
    """是否白名单内的 GM。列表为空一律 False（宁可误关不可误开）。"""
    sc = sandbox_cfg(cfg)
    if not sc.get("enabled", True):
        return False
    qqs = [str(x) for x in (sc.get("qqs") or [])]
    return str(qq) in qqs


def require_gm(cfg: dict, qq: str):
    """入口统一校验。返回 (ok, msg)。"""
    if is_gm(cfg, qq):
        return True, ""
    sc = sandbox_cfg(cfg)
    if not (sc.get("qqs") or []):
        return False, "❌ 未配置沙盒白名单（config.sandbox.qqs 为空），本功能已禁用"
    return False, "❌ 只有服主可用该指令"


def all_module_ids() -> list:
    """全舰种 × T1~T5 的全部模块 id。"""
    mc = pools.mod_data()
    out = []
    for cls in mc["classes"]:
        for tier in range(1, 6):
            for m in pools.pool_modules(cls, tier):
                if m["id"] not in out:
                    out.append(m["id"])
    return out


def grant_all_blueprints(conn, qq: str) -> int:
    """解锁全部蓝图（全舰种全代际）。返回新增条数。"""
    owned = pools.owned_ids(conn, qq)
    now = int(time.time())
    n = 0
    for mid in all_module_ids():
        if mid in owned:
            continue
        conn.execute("INSERT INTO blueprints(qq,module_id,obtained_at) VALUES(?,?,?)",
                     (qq, mid, now))
        n += 1
    conn.commit()
    return n


def topup_resources(conn, cfg: dict, qq: str) -> dict:
    """把资源拉满（含资金/科研/情报）。返回设置后的值。"""
    amt = float(sandbox_cfg(cfg).get("resource_topup", 999999999))
    sets = ",".join(f"{k}=?" for k in RES_FIELDS)
    conn.execute(f"UPDATE players SET {sets} WHERE qq=?",
                 (*[amt] * len(RES_FIELDS), qq))
    conn.commit()
    return {k: amt for k in RES_FIELDS}


def max_all_buildings(conn, cfg, qq: str) -> int:
    """把自己所有岛上的建筑升到各自 max_lv；没有的建筑补一座 Lv1 再拉满。

    只动自己的岛（islands.owner_qq = qq）。
    """
    bdefs = cfg["buildings"]
    isls = conn.execute("SELECT x,y FROM islands WHERE owner_qq=?", (qq,)).fetchall()
    n = 0
    now = int(time.time())
    for isl in isls:
        x, y = isl["x"], isl["y"]
        cur = {r["def_id"]: r["id"] for r in conn.execute(
            "SELECT id,def_id FROM buildings WHERE x=? AND y=?", (x, y)).fetchall()}
        for def_id, bd in bdefs.items():
            lv = bd["max_lv"]
            if def_id in cur:
                conn.execute("UPDATE buildings SET level=? WHERE id=?",
                             (lv, cur[def_id]))
            else:
                conn.execute(
                    "INSERT INTO buildings(x,y,def_id,level,hp,built_tick)"
                    " VALUES(?,?,?,?,?,?)", (x, y, def_id, lv, None, now))
            n += 1
        # 岛屿开发等级也拉满（决定槽位/岛级 D）
        conn.execute("UPDATE islands SET dev_level=? WHERE x=? AND y=?", (10, x, y))
    conn.commit()
    return n


def status(conn, cfg, qq: str) -> dict:
    p = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return {"ok": False, "reason": "尚未注册"}
    total = len(all_module_ids())
    owned = len(set(all_module_ids()) & pools.owned_ids(conn, qq))
    return {"ok": True, "is_gm": is_gm(cfg, qq), "qq": qq,
            "name": p["name"],
            "resources": {k: (p[k] if k in p.keys() else 0) for k in RES_FIELDS},
            "topup": float(sandbox_cfg(cfg).get("resource_topup", 999999999)),
            "blueprints_owned": owned, "blueprints_total": total,
            "buildings": conn.execute(
                "SELECT COUNT(*) c FROM buildings WHERE x IN"
                " (SELECT x FROM islands WHERE owner_qq=?)", (qq,)).fetchone()["c"],
            "islands": conn.execute(
                "SELECT COUNT(*) c FROM islands WHERE owner_qq=?", (qq,)).fetchone()["c"],
            "ships": conn.execute(
                "SELECT COUNT(*) c FROM ships WHERE qq=?", (qq,)).fetchone()["c"],
            "designs": conn.execute(
                "SELECT COUNT(*) c FROM designs WHERE qq=?", (qq,)).fetchone()["c"]}


def apply(conn, cfg, qq: str, what: str):
    """执行沙盒操作。what: all | unlock | res | buildings

    返回 (ok, text)。
    """
    ok, msg = require_gm(cfg, qq)
    if not ok:
        return False, msg
    done = []
    if what in ("all", "unlock"):
        n = grant_all_blueprints(conn, qq)
        tot = len(all_module_ids())
        done.append(f"🔓 解锁全部蓝图：新增 {n} 个（现有 {tot}/{tot}）")
    if what in ("all", "res"):
        topup_resources(conn, cfg, qq)
        done.append(f"💰 资源已拉满为 {int(float(sandbox_cfg(cfg).get('resource_topup', 999999999)))}")
    if what in ("all", "buildings"):
        n = max_all_buildings(conn, cfg, qq)
        done.append(f"🏛 建筑与岛级已拉满（处理 {n} 项）")
    if not done:
        return False, "❌ 未知操作。可选：全部 / 解锁 / 资源 / 建筑"
    return True, "🛠 沙盒操作完成：\n　" + "\n　".join(done)
