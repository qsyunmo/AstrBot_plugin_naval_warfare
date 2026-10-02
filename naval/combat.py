"""P2a 简化战斗引擎（§19.2 最小版）。

同格接敌即开战；每个战争 tick 对 active battle 结算 1 轮。
- 命中 = clamp(0.75 + 攻方火控*0.004 − 目标航速*0.0015, 0.10, 0.95)
- 炮(fire)每轮；鱼雷(torpedo)每 3 轮；潜艇只能被 asw>0 的深弹攻击
- 伤害 = 威力 × uniform(0.8,1.2)；舰沉删除 ships 行，堆叠 qty-1 并把溢出伤害滚到下一艘
- 玩家撤退：mission.retreat_since 起 1 个战争 tick 后脱离，舰队被推远几格防秒重开
"""
import json
import random
import time

from . import aiworld, fleet


# ---------- 单位 ----------

def _player_units(conn, fid: int) -> list:
    out = []
    for r in fleet.fleet_ships(conn, fid):
        st = fleet.ship_stats(r)
        out.append({"side": "p", "id": r["id"], "name": r["name"],
                    "hp": r["hp"], "maxhp": r["max_hp"],
                    "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                    "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                    "speed": st.get("speed", 0), "cls": None})
    return out


def _ai_units(comp: list) -> list:
    out = []
    for i, s in enumerate(comp):
        if s.get("qty", 0) > 0:
            out.append({"side": "a", "idx": i,
                        "name": aiworld.CLASS_ZH.get(s["cls"], s["cls"]),
                        "hp": s["hp"], "qty": s["qty"], **s})
    return out


# ---------- 接敌 ----------

def maybe_start_battles(conn, cfg: dict, war_tick: int) -> list:
    """同格玩家舰队 × 海盗巡逻队自动开战。返回新建战斗 id。"""
    new_ids = []
    pirates = [r for r in conn.execute(
        "SELECT * FROM ai_fleets WHERE faction='pirate'").fetchall()
        if json.loads(r["comp_json"] or "[]")]
    for f in conn.execute("SELECT * FROM fleets").fetchall():
        if fleet.ship_count(conn, f["id"]) <= 0 or fleet.in_battle(conn, f["id"]):
            continue
        for pr in pirates:
            if (pr["x"], pr["y"]) != (f["x"], f["y"]):
                continue
            if any(1 for _ in conn.execute(
                    "SELECT 1 FROM battles WHERE status='active' AND ai_fleet_id=?",
                    (pr["id"],))):
                continue
            sides = {"A": {"side": "player", "qq": f["qq"], "fleet_id": f["id"]},
                     "B": {"side": "ai", "ai_fleet_id": pr["id"], "level": pr["level"]}}
            cur = conn.execute(
                "INSERT INTO battles(tick,x,y,created_at,status,sides_json,rng_seed,"
                "qq,ai_fleet_id,summary) VALUES(?,?,?,?, 'active',?, ?,?,?, '')",
                (war_tick, f["x"], f["y"], int(time.time()),
                 json.dumps(sides, ensure_ascii=False),
                 random.randint(1, 10 ** 9), f["qq"], pr["id"]))
            bid = cur.lastrowid
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,0,?)",
                (bid, war_tick,
                 f"⚓ 坐标({f['x']},{f['y']}) 遭遇【{pr['name']}】，战斗开始！"))
            conn.commit()
            new_ids.append(bid)
    return new_ids


# ---------- 单轮 ----------

def _hit_chance(cfg: dict, att: dict, tgt: dict) -> float:
    c = cfg["combat"]
    p = c["hit_base"] + att.get("hit", 0) * c["hit_per_fc"] \
        - tgt.get("speed", 0) * c["hit_speed_penalty"]
    return max(c["hit_min"], min(c["hit_max"], p))


def _is_ss(u: dict) -> bool:
    """P2a 仅海盗方有潜艇（堆叠 cls 以 ss 开头）。"""
    return u["side"] == "a" and str(u.get("cls", "")).startswith("ss")


def _alive(u: dict) -> bool:
    return u["qty"] > 0 if u["side"] == "a" else u["hp"] > 0


def _apply_damage(conn, cfg, tgt: dict, dmg: float, events: list, att_name: str, wpn: str,
                  comp: list):
    if tgt["side"] == "p":
        tgt["hp"] = max(0, tgt["hp"] - dmg)
        conn.execute("UPDATE ships SET hp=? WHERE id=?", (round(tgt["hp"], 1), tgt["id"]))
        if tgt["hp"] <= 0:
            conn.execute("DELETE FROM ships WHERE id=?", (tgt["id"],))
            events.append(f"💥 {att_name} {wpn}击沉【{tgt['name']}】！")
        else:
            events.append(f"  {att_name}{wpn}命中 {tgt['name']}，造成 {dmg:.0f} 伤害"
                          f"（剩 {tgt['hp']:.0f}）")
    else:
        s = comp[tgt["idx"]]
        s["hp"] -= dmg
        sunk = 0
        while s["qty"] > 0 and s["hp"] <= 0:
            s["hp"] += s["maxhp"]
            s["qty"] -= 1
            sunk += 1
        if s["qty"] == 0:
            s["hp"] = 0
        if sunk:
            events.append(f"💥 {att_name}{wpn}击沉 {tgt['name']} ×{sunk}"
                          + (f"，余 {s['qty']} 艘" if s["qty"] else "，该编队全灭！"))
        else:
            events.append(f"  {att_name}{wpn}命中 {tgt['name']}×{s['qty']}，"
                          f"造成 {dmg:.0f} 伤害")


def _fire_side(conn, cfg, attackers: list, targets: list, round_no: int,
               events: list, comp: list):
    for att in attackers:
        if not _alive(att):
            continue
        alive = [u for u in targets if _alive(u)]
        # 目标选择：水面舰优先；仅 asw 手能打潜艇
        if att.get("asw", 0) > 0:
            subs = [u for u in alive if _is_ss(u)]
            surf = [u for u in alive if not _is_ss(u)]
            tgt = random.choice(surf) if surf else (random.choice(subs) if subs else None)
        else:
            surf = [u for u in alive if not _is_ss(u)]
            tgt = random.choice(surf) if surf else None
        if tgt is None:
            continue
        shots = []
        if _is_ss(tgt):
            if att.get("asw", 0) > 0:
                shots.append(("深弹", att["asw"]))
        else:
            if att.get("fire", 0) > 0:
                shots.append(("炮击", att["fire"]))
            if att.get("torpedo", 0) > 0 and round_no % 3 == 1:
                shots.append(("鱼雷", att["torpedo"]))
        for wpn, power in shots:
            if random.random() > _hit_chance(cfg, att, tgt):
                continue
            _apply_damage(conn, cfg, tgt, power * random.uniform(0.8, 1.2),
                          events, att["name"], wpn, comp)


# ---------- 结算 ----------

def _finish(conn, cfg, bid: int, war_tick: int, sides: dict, result: str,
            comp: list, detail: str):
    """result: victory/defeat/retreat。返回 (origin, summary) 或 None。"""
    a, b = sides["A"], sides["B"]
    pr = conn.execute("SELECT * FROM ai_fleets WHERE id=?",
                      (b["ai_fleet_id"],)).fetchone()
    if result == "victory":
        loot = cfg["pirate"]["loot"][str(b["level"])]
        conn.execute("UPDATE players SET steel=COALESCE(steel,0)+?,money=COALESCE(money,0)+?"
                     " WHERE qq=?", (loot["steel"], loot["money"], a["qq"]))
        conn.execute(
            "UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
            (war_tick + cfg["pirate"]["respawn_war_ticks"], b["ai_fleet_id"]))
        conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (a["fleet_id"],))
        summary = (f"🏆 海战胜利！坐标({pr['x']},{pr['y']}) 歼灭【{pr['name']}】，"
                   f"缴获 钢{loot['steel']} 资金{loot['money']}。"
                   f"残敌约 {cfg['pirate']['respawn_war_ticks'] * cfg['tick']['war_min']} 分钟后重整。")
    elif result == "defeat":
        conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (a["fleet_id"],))
        summary = f"☠️ 海战失利：你的舰队在({pr['x']},{pr['y']})全军覆没……"
    else:
        n = cfg["fleet"]["retreat_nudge_cells"]
        fr = conn.execute("SELECT * FROM fleets WHERE id=?", (a["fleet_id"],)).fetchone()
        nx = max(0, min(fleet.MAP_SIZE - 1, fr["x"] + n))
        ny = max(0, min(fleet.MAP_SIZE - 1, fr["y"] + n))
        conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                     (nx, ny, a["fleet_id"]))
        summary = f"↩️ 舰队已撤离接触，退至({nx},{ny})，残存舰只保留。"
    conn.execute(
        "UPDATE battles SET status='over',end_tick=?,summary=?,detail=? WHERE id=?",
        (war_tick, summary, detail, bid))
    origin = fleet.latest_origin(conn, a["qq"])
    return (origin, summary) if origin else None


def settle_battles(conn, cfg: dict, war_tick: int) -> list:
    """所有 active battle 各结算 1 轮（撤退判定优先）。返回推送列表。"""
    pushes = []
    for b in conn.execute(
            "SELECT * FROM battles WHERE status='active' ORDER BY id").fetchall():
        sides = json.loads(b["sides_json"] or "{}")
        a, bside = sides["A"], sides["B"]
        fid, afid = a["fleet_id"], bside["ai_fleet_id"]
        pr = conn.execute("SELECT * FROM ai_fleets WHERE id=?", (afid,)).fetchone()
        comp = json.loads(pr["comp_json"] or "[]")
        prev = conn.execute("SELECT MAX(round_no) m FROM battle_events WHERE battle_id=?",
                            (b["id"],)).fetchone()["m"] or 0

        # 撤退：标记满 retreat_rounds 即脱离（retreat_rounds=1 → 首个 tick 脱离）
        fr = conn.execute("SELECT * FROM fleets WHERE id=?", (fid,)).fetchone()
        fm = json.loads(fr["mission"] or "{}")
        if "retreat_since" in fm and \
                war_tick - fm["retreat_since"] >= cfg["combat"]["retreat_rounds"] - 1:
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,?,?)",
                (b["id"], war_tick, prev + 1, "↩️ 本方舰队成功脱离战斗。"))
            conn.commit()
            p = _finish(conn, cfg, b["id"], war_tick, sides, "retreat", comp, "撤退")
            if p:
                pushes.append(p)
            continue

        pu = _player_units(conn, fid)
        au = _ai_units(comp)
        if not pu or not au:
            conn.commit()
            p = _finish(conn, cfg, b["id"], war_tick, sides,
                        "defeat" if not pu else "victory", comp, "一方已无兵力")
            if p:
                pushes.append(p)
            continue

        round_no = prev + 1
        events = [f"—— 第{round_no}轮 ——"]
        _fire_side(conn, cfg, pu, au, round_no, events, comp)
        pu2 = _player_units(conn, fid)  # 重新读存活
        au2 = _ai_units(comp)
        _fire_side(conn, cfg, au2, pu2, round_no, events, comp)

        conn.execute("UPDATE ai_fleets SET comp_json=? WHERE id=?",
                     (json.dumps(comp, ensure_ascii=False), afid))
        for line in events:
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,?,?)",
                (b["id"], war_tick, round_no, line))
        conn.commit()

        pu_left = fleet.ship_count(conn, fid)
        ai_left = sum(s.get("qty", 0) for s in comp)
        if pu_left == 0 or ai_left == 0:
            p = _finish(conn, cfg, b["id"], war_tick, sides,
                        "victory" if ai_left == 0 else "defeat", comp,
                        "\n".join(events))
            if p:
                pushes.append(p)
    return pushes
