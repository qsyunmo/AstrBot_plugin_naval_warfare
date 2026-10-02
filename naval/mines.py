"""§4/§24.2 布雷与扫雷：区域水雷场（锚雷/音响雷/磁雷）与清除。

设计文档依据：
  §4      「布雷/扫雷 | 区域水雷场（锚雷/音响雷/磁雷）与清除」
  §15.3   「建筑即指令钩子：水雷仓库→布雷」—— 建筑树与指令系统咬合
  §19.7/8 护航潜艇自带水雷 8、远洋雷击潜艇水雷 12
  §25.1 L7 海盗据点「+雷场」

因此布雷能力来自两处：舰船自带水雷（潜艇/驱逐），或己方岛屿有水雷仓库。
水雷对敌我识别：己方与同盟水雷不触发（IFF），敌方通过时按雷种 roll 触发。
"""
import json
import random

from . import fleet

ANCHOR, ACOUSTIC, MAGNETIC = "anchor", "acoustic", "magnetic"
KINDS = (ANCHOR, ACOUSTIC, MAGNETIC)


def mcfg(cfg: dict) -> dict:
    return cfg.get("mines") or {}


def kind_spec(cfg: dict, kind: str) -> dict:
    return (mcfg(cfg).get("kinds") or {}).get(kind) or {}


def kind_name(cfg: dict, kind: str) -> str:
    return kind_spec(cfg, kind).get("name", kind)


# ---------- 布雷能力 ----------

def fleet_mine_capacity(conn, cfg: dict, fleet_id: int) -> int:
    """舰队可携带的水雷总量（按舰种配置累加）。"""
    table = mcfg(cfg).get("sub_mines") or {}
    total = 0
    for r in conn.execute("SELECT def_id FROM ships WHERE fleet_id=?", (fleet_id,)).fetchall():
        if not str(r["def_id"]).isdigit():
            continue
        d = conn.execute("SELECT ship_class FROM designs WHERE id=?",
                         (int(r["def_id"]),)).fetchone()
        if d:
            total += int(table.get(d["ship_class"], 0))
    return total


def _island_has_depot(conn, qq: str, x: int, y: int, radius: int = 3) -> bool:
    """§15.3 岛上有水雷仓库则具备布雷能力（在岛屿周边半径内）。"""
    r = conn.execute(
        "SELECT COUNT(*) c FROM buildings b JOIN islands i ON i.x=b.x AND i.y=b.y"
        " WHERE i.owner_qq=? AND b.def_id='mine_depot'"
        " AND b.x BETWEEN ? AND ? AND b.y BETWEEN ? AND ?",
        (qq, x - radius, x + radius, y - radius, y + radius)).fetchone()
    return bool(r and r["c"])


def can_lay(conn, cfg: dict, qq: str, fleet_id: int, x: int, y: int):
    """返回 (能否布雷, 说明)。"""
    cap = fleet_mine_capacity(conn, cfg, fleet_id)
    if cap > 0:
        return True, f"舰队自带水雷 {cap} 枚"
    if _island_has_depot(conn, qq, x, y):
        return True, "借助己方水雷仓库补给"
    return False, ("该舰队没有布雷能力，且目标 3 格内没有己方水雷仓库。\n"
                   "（§19.7：护航/攻击潜艇自带水雷；§15.3：水雷仓库→布雷）")


# ---------- 水雷场读写 ----------

def get_field(conn, x: int, y: int):
    return conn.execute("SELECT * FROM minefields WHERE x=? AND y=?", (x, y)).fetchone()


def lay(conn, cfg: dict, qq: str, fleet_id: int, x: int, y: int, kind: str):
    """在 (x,y) 布雷。返回 (ok, text)。"""
    mc = mcfg(cfg)
    if kind not in KINDS:
        return False, f"❌ 雷种只能是 {'/'.join(KINDS)}"
    ok, why = can_lay(conn, cfg, qq, fleet_id, x, y)
    if not ok:
        return False, f"❌ {why}"
    amount = int(mc.get("lay_per_action", 8))
    cost = mc.get("cost") or {}
    p = conn.execute("SELECT steel,money FROM players WHERE qq=?", (qq,)).fetchone()
    if p:
        if float(p["steel"] or 0) < float(cost.get("steel", 0)):
            return False, f"❌ 钢材不足：布雷需 钢{cost.get('steel', 0)}"
        conn.execute("UPDATE players SET steel=steel-? WHERE qq=?",
                     (cost.get("steel", 0), qq))
    cap = int(mc.get("mine_capacity", 40))
    cur = get_field(conn, x, y)
    if cur and cur["owner_qq"] != qq:
        return False, (f"❌ ({x},{y}) 已被【{cur['owner_qq']}】布设雷场"
                       f"（{cur['count']} 枚），需先 /nw扫雷 清除")
    have = int(cur["count"]) if cur else 0
    if have >= cap:
        return False, f"❌ ({x},{y}) 雷场已达上限 {cap} 枚"
    new = min(cap, have + amount)
    if cur:
        conn.execute("UPDATE minefields SET count=?,kind=? WHERE id=?",
                     (new, kind, cur["id"]))
    else:
        conn.execute(
            "INSERT INTO minefields(x,y,owner_qq,kind,count,created_tick)"
            " VALUES(?,?,?,?,?,0)", (x, y, qq, kind, new))
    conn.commit()
    return True, (f"💣 已在 ({x},{y}) 布设【{kind_name(cfg, kind)}】雷场："
                  f"{have} → {new} 枚（上限 {cap}）。\n"
                  f"敌方舰船通过该格时将按雷种概率触发。己方与同盟不受影响。")


def sweep(conn, cfg: dict, qq: str, fleet_id: int, x: int, y: int):
    """在 (x,y) 扫雷。返回 (ok, text)。"""
    mc = mcfg(cfg)
    cur = get_field(conn, x, y)
    if not cur or int(cur["count"]) <= 0:
        return False, f"❌ ({x},{y}) 没有雷场需要清除"
    if cur["owner_qq"] == qq:
        return False, (f"❌ ({x},{y}) 是你自己的雷场（{cur['count']} 枚）。\n"
                       f"要撤除请先确认——目前不支持自撤，避免误清自家防线。")
    base = int(mc.get("sweep_per_action", 10))
    diff = float(kind_spec(cfg, cur["kind"]).get("sweep_diff", 1.0))
    cleared = max(1, int(base / max(0.1, diff) * random.uniform(0.85, 1.15)))
    cleared = min(cleared, int(cur["count"]))
    left = int(cur["count"]) - cleared
    conn.execute("UPDATE minefields SET count=? WHERE id=?", (left, cur["id"]))
    conn.commit()
    tail = "雷场已清空。" if left <= 0 else f"剩余 {left} 枚，需继续扫。"
    return True, (f"🧹 在 ({x},{y}) 扫除【{kind_name(cfg, cur['kind'])}】"
                  f"{cleared} 枚。{tail}")


# ---------- 触发 ----------

def check_trigger(conn, cfg: dict, qq: str, x: int, y: int, ships: int = 1):
    """舰队进入 (x,y) 时的水雷触发检定。返回 (伤害, 文本) 或 (0, None)。

    己方雷场不触发（IFF）；雷场被触发后按触发量消耗。
    """
    cur = get_field(conn, x, y)
    if not cur or int(cur["count"]) <= 0:
        return 0.0, None
    if cur["owner_qq"] == qq:
        return 0.0, None

    spec = kind_spec(cfg, cur["kind"])
    chance = float(spec.get("trigger", 0.45))
    # 舰队越大越容易撞雷
    chance = min(0.95, chance * (1.0 + 0.05 * max(0, ships - 1)))
    hits = 0
    for _ in range(min(int(cur["count"]), max(1, ships))):
        if random.random() < chance:
            hits += 1
    if hits <= 0:
        return 0.0, None
    dmg = hits * float(spec.get("damage", 120))
    left = max(0, int(cur["count"]) - hits)
    conn.execute("UPDATE minefields SET count=? WHERE id=?", (left, cur["id"]))
    conn.commit()
    return dmg, (f"💥 ({x},{y}) 触发【{kind_name(cfg, cur['kind'])}】雷场！"
                 f"命中 {hits} 枚，造成 {dmg:.0f} 伤害。"
                 + ("" if left else "该雷场已被消耗殆尽。"))


def damage_player_fleet(conn, cfg, fleet_id: int, dmg: float) -> str:
    """把水雷伤害摊到玩家舰队各舰上（逐舰扣血，沉没删除）。"""
    ships = fleet.fleet_ships(conn, fleet_id)
    if not ships or dmg <= 0:
        return ""
    per = dmg / len(ships)
    sunk = []
    for r in ships:
        hp = float(r["hp"] or 0) - per
        if hp <= 0:
            conn.execute("DELETE FROM ships WHERE id=?", (r["id"],))
            sunk.append(r["name"])
        else:
            conn.execute("UPDATE ships SET hp=? WHERE id=?", (round(hp, 1), r["id"]))
    conn.commit()
    txt = f"　舰队 {len(ships)} 艘均摊 {per:.0f} 伤害。"
    if sunk:
        txt += "\n　💥 沉没：" + "、".join(sunk)
    return txt


def damage_ai_fleet(conn, cfg, ai_row, dmg: float) -> str:
    """把水雷伤害摊到 AI 编队上（按 qty 削减）。"""
    comp = json.loads(ai_row["comp_json"] or "[]")
    if not comp or dmg <= 0:
        return ""
    left = dmg
    sunk = 0
    for s in comp:
        while int(s.get("qty", 0) or 0) > 0 and left > 0:
            hp = float(s.get("hp", 1) or 1)
            if left >= hp:
                left -= hp
                s["qty"] = int(s["qty"]) - 1
                s["hp"] = s.get("maxhp", hp)
                sunk += 1
            else:
                s["hp"] = hp - left
                left = 0
    alive = any(int(s.get("qty", 0) or 0) > 0 for s in comp)
    if alive:
        conn.execute("UPDATE ai_fleets SET comp_json=? WHERE id=?",
                     (json.dumps(comp, ensure_ascii=False), ai_row["id"]))
    else:
        conn.execute("UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
                     (0, ai_row["id"]))
    conn.commit()
    return f"　击沉 {sunk} 艘。" if sunk else ""


def war_tick_ai_mines(conn, cfg: dict) -> list:
    """AI 舰队走进雷场时的触发（商人/巡逻队/叛军都会撞雷）。

    返回 [(qq, text)]，用于给布雷方推送战果。
    """
    from . import aiworld
    notes = []
    for r in conn.execute("SELECT * FROM ai_fleets WHERE comp_json!='[]'").fetchall():
        cur = get_field(conn, r["x"], r["y"])
        if not cur or int(cur["count"]) <= 0:
            continue
        comp = json.loads(r["comp_json"] or "[]")
        n = sum(int(c.get("qty", 0) or 0) for c in comp)
        dmg, txt = check_trigger(conn, cfg, "__ai__", r["x"], r["y"], n)
        if dmg <= 0:
            continue
        extra = damage_ai_fleet(conn, cfg, r, dmg)
        notes.append((cur["owner_qq"],
                      f"💣 你的雷场在 ({r['x']},{r['y']}) 命中【{r['name']}】！\n{txt}{extra}"))
    return notes
