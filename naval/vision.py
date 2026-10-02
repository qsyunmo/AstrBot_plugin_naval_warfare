"""战争迷雾（设计文档 §1，P2b）。

视野来源：己方岛屿、首都、己方舰队。每个来源有各自的切比雪夫半径
（与舰队移动一致），半径内的目标才进入玩家视野。

config.fog：
    enabled          总开关，关掉即"全图可见"（P2a 行为）
    island_vision    每个己方岛屿的视野半径
    capital_vision   首都视野半径（通常比普通岛屿大）
    fleet_vision     每支己方舰队的视野半径

本模块只做可见性判定，不含任何存储：岛屿/舰队坐标就是视野源，改坐标即改视野。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("naval")


def is_enabled(cfg: dict) -> bool:
    return bool((cfg.get("fog") or {}).get("enabled", True))


def vision_sources(conn, cfg: dict, qq: str):
    """返回 [(x, y, radius, kind)]；迷雾关闭时返回 None 表示"全图可见"。

    巡逻/反潜阵位上的舰队视野更大（§4「巡逻/侦察：揭雾」）：
    半径 = 阵位半径 + task.patrol_vision_bonus。
    """
    if not is_enabled(cfg):
        return None
    f = cfg.get("fog") or {}
    cap_r = int(f.get("capital_vision", 12))
    isl_r = int(f.get("island_vision", 6))
    flt_r = int(f.get("fleet_vision", 8))
    bonus = int((cfg.get("task") or {}).get("patrol_vision_bonus", 2))

    # §6.2 联军作战「共享视野」：自己 + 盟友一起点灯
    watchers = [qq]
    try:
        from . import relations
        watchers += relations.allies_of(conn, cfg, qq)
    except Exception:
        logger.exception("[海战模拟器] 共享视野求同盟列表异常（回退为只看自己）")

    # §6.2 军港租借：租来的港口也算视野源
    try:
        from . import diplomacy
        lease_src = diplomacy.lease_vision_sources(conn, cfg, qq)
    except Exception:
        logger.exception("[海战模拟器] 求租借军港视野异常")
        lease_src = []

    src = []
    for who in watchers:
        tag = "" if who == qq else "ally:"
        p = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?",
                         (who,)).fetchone()
        if p and p["capital_x"] is not None:
            src.append((p["capital_x"], p["capital_y"], cap_r, tag + "capital"))
        for r in conn.execute("SELECT x,y FROM islands WHERE owner_qq=?", (who,)).fetchall():
            src.append((r["x"], r["y"], isl_r, tag + "island"))
        for r in conn.execute("SELECT id,x,y,mission FROM fleets WHERE qq=?",
                              (who,)).fetchall():
            if r["x"] is None:
                continue
            rad = flt_r
            try:
                m = json.loads(r["mission"] or "{}")
                if m.get("type") in ("patrol", "asw") and m.get("phase") == "on_station":
                    rad = max(flt_r, int(m.get("radius", 3)) + bonus)
            except Exception:
                # 这里是"正当的"静默兜底：mission 是历史遗留 JSON，解析失败就退回
                # 默认视野半径，属于预期行为，不记日志。（其余 except 均已改为 logger）
                pass
            src.append((r["x"], r["y"], rad, tag + "fleet"))
    # §6.2 租借的军港
    src.extend(lease_src)
    return src


def is_visible(sources, x: int, y: int) -> bool:
    """切比雪夫距离判定（与舰队移动的步进方式一致）。"""
    if sources is None:
        return True
    for sx, sy, r, _kind in sources:
        if max(abs(x - sx), abs(y - sy)) <= r:
            return True
    return False


def visible_grid(sources, x0: int, y0: int, w: int, h: int) -> list:
    """返回 h×w 的布尔网格，供网页端渲染迷雾。"""
    if sources is None:
        return [[True] * w for _ in range(h)]
    grid = []
    for ry in range(h):
        row = []
        y = y0 + ry
        for rx in range(w):
            row.append(is_visible(sources, x0 + rx, y))
        grid.append(row)
    return grid
