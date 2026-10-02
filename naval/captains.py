"""§19.1 指挥官/舰长系统。

设计文档依据：
  §19.1 「指挥官/舰长系统（任命带技能等级，战沉有损失）」
  §15.3 「海军学院 | 指挥官获取/经验；全局唯一」

机制：
  - 招募：花资金+人力得到一名 Lv1 舰长，自带一项技能
  - 任命：挂到某艘舰船上，该舰获得技能加成（per_level × 等级）
  - 经验：每打满一场战斗涨经验，够则升级（有海军学院时 +50%）
  - 战沉：船没了，舰长一并损失，推送讣告
"""
import json
import random

from . import fleet


def ccfg(cfg: dict) -> dict:
    return cfg.get("captains") or {}


def skills(cfg: dict) -> dict:
    return ccfg(cfg).get("skills") or {}


def skill_name(cfg: dict, key: str) -> str:
    return (skills(cfg).get(key) or {}).get("name", key)


def random_name(cfg: dict, used: set) -> str:
    """随机起一个不重名的舰长姓名。"""
    c = ccfg(cfg)
    sur = c.get("surnames") or ["陈"]
    giv = c.get("given") or ["绍宽"]
    for _ in range(60):
        nm = random.choice(sur) + random.choice(giv)
        if nm not in used:
            return nm
    return random.choice(sur) + random.choice(giv) + str(random.randint(1, 99))


def recruit(conn, cfg, qq: str):
    """§15.3 招募一名舰长。返回 (ok, text)。"""
    c = ccfg(cfg)
    p = conn.execute("SELECT money,manpower FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return False, "❌ 未注册"
    cost_m = int(c.get("recruit_money", 800))
    cost_h = int(c.get("recruit_manpower", 50))
    if float(p["money"] or 0) < cost_m:
        return False, f"❌ 资金不足：招募需 {cost_m}，现有 {float(p['money'] or 0):.0f}"
    if float(p["manpower"] or 0) < cost_h:
        return False, f"❌ 人力不足：招募需 {cost_h}，现有 {float(p['manpower'] or 0):.0f}"
    used = {r["name"] for r in conn.execute(
        "SELECT name FROM captains WHERE qq=?", (qq,)).fetchall()}
    nm = random_name(cfg, used)
    sk = random.choice(list(skills(cfg).keys()) or ["gunner"])
    conn.execute("UPDATE players SET money=money-?, manpower=manpower-? WHERE qq=?",
                 (cost_m, cost_h, qq))
    cur = conn.execute(
        "INSERT INTO captains(qq,name,skill,level,exp,ship_id) VALUES(?,?,?,?,0,NULL)",
        (qq, nm, sk, int(c.get("start_skill", 1))))
    conn.commit()
    return True, (f"🎖 招募到舰长【{nm}】（{skill_name(cfg, sk)} 专精，"
                  f"Lv{int(c.get('start_skill', 1))}）\n"
                  f"花费 资金{cost_m} + 人力{cost_h}。\n"
                  f"用 /nw任命 {nm} <舰名> 把他派上舰。\n"
                  f"§19.1：舰在人在，舰沉人亡。")


def list_captains(conn, qq: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM captains WHERE qq=? ORDER BY level DESC, id", (qq,)).fetchall()]


def assign(conn, cfg, qq: str, captain_name: str, ship_name: str):
    """把舰长任命到某艘舰船。返回 (ok, text)。"""
    cap = conn.execute("SELECT * FROM captains WHERE qq=? AND name=?",
                       (qq, captain_name)).fetchone()
    if not cap:
        return False, f"❌ 你没有名叫【{captain_name}】的舰长"
    ship = conn.execute("SELECT * FROM ships WHERE qq=? AND name=?",
                        (qq, ship_name)).fetchone()
    if not ship:
        return False, f"❌ 你没有名叫【{ship_name}】的舰船"
    other = conn.execute("SELECT name FROM captains WHERE ship_id=? AND id!=?",
                         (ship["id"], cap["id"])).fetchone()
    if other:
        return False, f"❌ 【{ship_name}】上已经有舰长【{other['name']}】了"
    conn.execute("UPDATE captains SET ship_id=? WHERE id=?", (ship["id"], cap["id"]))
    conn.commit()
    return True, (f"🎖 已任命【{cap['name']}】为【{ship_name}】舰长"
                  f"（{skill_name(cfg, cap['skill'])} Lv{cap['level']}）。\n"
                  f"该舰会获得对应属性加成；§19.1：此舰战沉则舰长一并损失。")


def unassign(conn, cfg, qq: str, captain_name: str):
    cap = conn.execute("SELECT * FROM captains WHERE qq=? AND name=?",
                       (qq, captain_name)).fetchone()
    if not cap:
        return False, f"❌ 你没有名叫【{captain_name}】的舰长"
    if not cap["ship_id"]:
        return False, f"❌ 【{captain_name}】当前未任职"
    conn.execute("UPDATE captains SET ship_id=NULL WHERE id=?", (cap["id"],))
    conn.commit()
    return True, f"🎖 已解除【{cap['name']}】的舰长职务（转为待命）。"


def bonus_for(conn, cfg, ship_id: int) -> dict:
    """该舰船因舰长获得的属性倍率 {stat: multiplier}。"""
    cap = conn.execute("SELECT * FROM captains WHERE ship_id=?", (ship_id,)).fetchone()
    if not cap:
        return {}
    spec = skills(cfg).get(cap["skill"]) or {}
    stat = spec.get("stat")
    if not stat:
        return {}
    per = float(spec.get("per_level", 0.0))
    lv = int(cap["level"] or 1)
    return {stat: 1.0 + per * lv}


def on_battle(conn, cfg, qq: str, won: bool) -> list:
    """战后给参战舰长加经验（§15.3 海军学院 +50%）。返回升级/阵亡提示。"""
    c = ccfg(cfg)
    base = int(c.get("exp_per_battle", 25))
    # 首都有海军学院则经验加成
    r = conn.execute(
        "SELECT COUNT(*) c FROM buildings b JOIN islands i ON i.x=b.x AND i.y=b.y"
        " WHERE i.owner_qq=? AND b.def_id='naval_academy'", (qq,)).fetchone()
    if r and r["c"]:
        base = int(base * (1.0 + float(c.get("academy_exp_bonus", 0.5))))
    need = int(c.get("exp_per_level", 100))
    maxlv = int(c.get("max_skill", 5))
    notes = []
    for cap in conn.execute("SELECT * FROM captains WHERE qq=?", (qq,)).fetchall():
        exp = int(cap["exp"] or 0) + base
        lv = int(cap["level"] or 1)
        while exp >= need and lv < maxlv:
            exp -= need
            lv += 1
            notes.append(f"　🎖 舰长【{cap['name']}】晋升 Lv{lv}"
                         f"（{skill_name(cfg, cap['skill'])}）")
        conn.execute("UPDATE captains SET exp=?, level=? WHERE id=?", (exp, lv, cap["id"]))
    conn.commit()
    return notes


def on_ship_lost(conn, cfg, ship_id: int, ship_name: str) -> str:
    """§19.1 战沉有损失：舰长随舰损失。返回讣告文本（无舰长则空串）。"""
    cap = conn.execute("SELECT * FROM captains WHERE ship_id=?", (ship_id,)).fetchone()
    if not cap:
        return ""
    conn.execute("DELETE FROM captains WHERE id=?", (cap["id"],))
    conn.commit()
    return (f"\n⚰️ 随舰阵亡：【{cap['name']}】"
            f"（{skill_name(cfg, cap['skill'])} Lv{cap['level']}）与"
            f"【{ship_name}】一同沉没（§19.1）。")


def attach_to_units(conn, cfg, units: list) -> None:
    """把舰长加成叠加到玩家单位上（在 _player_units 之后调用）。"""
    for u in units:
        if u.get("side") != "p":
            continue
        b = bonus_for(conn, cfg, u.get("id"))
        if not b:
            continue
        u["captain"] = True
        for stat, mult in b.items():
            base = float(u.get(stat, 0) or 0)
            if stat == "hp":
                u["maxhp"] = float(u.get("maxhp", base) or base) * mult
                u["hp"] = min(u["maxhp"], float(u.get("hp", 0) or 0) + base * (mult - 1))
            elif stat == "speed":
                u["speed"] = base * mult
            else:
                u[stat] = base * mult
