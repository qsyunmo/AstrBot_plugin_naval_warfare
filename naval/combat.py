"""P2a 简化战斗引擎（§19.2 最小版）。

同格接敌即开战；每个战争 tick 对 active battle 结算 1 轮。
- 命中 = clamp(0.75 + 攻方火控*0.004 − 目标航速*0.0015, 0.10, 0.95)
- 炮(fire)每轮；鱼雷(torpedo)每 3 轮；潜艇只能被 asw>0 的深弹攻击
- 伤害 = 威力 × uniform(0.8,1.2)；舰沉删除 ships 行，堆叠 qty-1 并把溢出伤害滚到下一艘
- 玩家撤退：mission.retreat_since 起 1 个战争 tick 后脱离，舰队被推远几格防秒重开
"""
import json
import logging
import random
import time

from . import aiworld, fleet, relations, guild, air, mines

logger = logging.getLogger("naval")

WEB_ORIGIN_PREFIX = "web:"


# ---------- 单位 ----------

def _player_units(conn, fid: int) -> list:
    out = []
    for r in fleet.fleet_ships(conn, fid):
        st = fleet.ship_stats(r)
        keys = r.keys()
        # 舰级：def_id 指向 designs.id；潜艇三态要用舰级判断
        cls = None
        if str(r["def_id"]).isdigit():
            d = conn.execute("SELECT ship_class FROM designs WHERE id=?",
                             (int(r["def_id"]),)).fetchone()
            cls = d["ship_class"] if d else None
        out.append({"side": "p", "id": r["id"], "name": r["name"],
                    "hp": r["hp"], "maxhp": r["max_hp"],
                    "fire": st.get("fire", 0), "torpedo": st.get("torpedo", 0),
                    "asw": st.get("asw", 0), "hit": st.get("hit", 0),
                    "detect": st.get("detect", 0),
                    "speed": st.get("speed", 0), "cls": cls, "tier": r["tier"],
                    # §19.1 舰员经验：必须带上，否则加成无从计算
                    "crew_exp": (int(r["crew_exp"] or 0)
                                 if "crew_exp" in keys else 0),
                    "state": (r["sub_state"] if "sub_state" in keys else None),
                    "batt": (r["sub_batt"] if "sub_batt" in keys else None)})
    return out


# 会被判定为「我方主动进攻」的任务类型（整顿「侵略者」用）
AGGRESSIVE_MISSIONS = ("attack", "raid", "blockade", "escort", "ambush")


def side_fleet_ids(a: dict) -> list:
    """§27 一方可以有多支舰队参战；fleet_id 是主队，fleet_ids 是全部（含主队）。"""
    ids = [int(v) for v in (a.get("fleet_ids") or [])]
    fid = a.get("fleet_id")
    if fid is not None and int(fid) not in ids:
        ids.insert(0, int(fid))
    return ids


def _apply_refit_combat(conn, units: list, role: str) -> int:
    """按整顿效果给单位加成。role: 'attack'（我方主动）| 'defend'（我方被袭）。

    - 侵略者   invader    → 主动进攻时 生命与攻击 ×2
    - 铜墙铁壁 iron_wall  → 被袭防守时 生命与攻击 ×2
    逐舰队判定（同一方可能有多支舰队，各自整顿不同）。
    返回受影响的单位数。
    """
    if role not in ("attack", "defend"):
        return 0
    want = "invader" if role == "attack" else "iron_wall"
    cache, pending, hit = {}, {}, 0
    for u in units:
        if u.get("side") != "p":
            continue
        fid = u.get("fleet_id")
        if fid is None:
            continue
        if fid not in cache:
            cache[fid] = fleet.has_refit(conn, fid, want)
        if not cache[fid]:
            continue
        hit += 1
        # 生命：当前值与上限一起翻倍（否则翻倍后立刻被"超过上限"裁掉）
        u["hp"] = float(u.get("hp") or 0) * 2
        u["maxhp"] = float(u.get("maxhp") or 0) * 2
        for k in ("fire", "torpedo", "asw"):
            u[k] = float(u.get(k) or 0) * 2
        pending.setdefault(fid, []).append(u)
    _ = pending
    return hit


def _role_of(sides: dict, side_key: str) -> str:
    """side_key 那一方在这张战斗里是进攻还是防守。"""
    agg = str(sides.get("aggressor") or "A")
    return "attack" if agg == side_key else "defend"


def _player_units_multi(conn, cfg, fids: list, role: str = None) -> list:
    """§27.3 多支舰队同一方：火力按编队聚合后结算。

    每个单位带上 qq/fleet_id，供 §27.6 伤害贡献表按玩家归属。
    role='attack'/'defend' 时套用舰队整顿（侵略者 / 铜墙铁壁）的 ×2。
    """
    out = []
    for fid in fids:
        fr = conn.execute("SELECT qq FROM fleets WHERE id=?", (fid,)).fetchone()
        qq = fr["qq"] if fr else None
        for u in _player_units(conn, fid):
            u["qq"] = qq
            u["fleet_id"] = fid
            out.append(u)
    # §19.1 指挥官/舰长：给挂有舰长的舰船加技能加成
    # （注意：这里不要用裸 except 吞异常——曾因此隐藏过一个 NameError）
    from . import captains as _cap
    _cap.attach_to_units(conn, cfg, out)
    # §19.1 舰员经验（新兵/老练/王牌）——作用于舰船本身
    from . import crew as _crew
    _crew.attach_bonus(conn, cfg, out)
    # 舰队整顿：放在舰长/舰员加成之后，final 值再翻倍
    if role:
        _apply_refit_combat(conn, out, role)
    return out


def _save_player_sub_state(conn, units: list) -> None:
    """把本轮推进后的潜艇状态落库。"""
    for u in units:
        if u["side"] == "p" and _is_ss(u):
            conn.execute("UPDATE ships SET sub_state=?,sub_batt=? WHERE id=?",
                         (u.get("state") or SURFACE, u.get("batt"), u["id"]))


def _unit_key(u: dict):
    return u["id"] if u["side"] == "p" else u["idx"]


def _carry_sub_state(before: list, after: list) -> None:
    """重新读存活单位后，把推进过的潜艇状态搬过去（否则状态会丢）。"""
    prev = {_unit_key(u): u for u in before if _is_ss(u)}
    for u in after:
        if _is_ss(u) and _unit_key(u) in prev:
            src = prev[_unit_key(u)]
            for f in ("state", "batt", "ambush"):
                u[f] = src.get(f)


def _ai_sub_to_comp(comp: list, units: list) -> None:
    """AI 单位是 comp 堆叠的副本，状态必须写回 comp 才会随 comp_json 落库。"""
    for u in units:
        if u["side"] == "a" and _is_ss(u) and u["idx"] < len(comp):
            comp[u["idx"]]["state"] = u.get("state")
            comp[u["idx"]]["batt"] = u.get("batt")


def _ai_units(comp: list) -> list:
    out = []
    for i, s in enumerate(comp):
        if s.get("qty", 0) > 0:
            out.append({"side": "a", "idx": i,
                        "name": aiworld.CLASS_ZH.get(s["cls"], s["cls"]),
                        "hp": s["hp"], "qty": s["qty"], **s})
    return out


# ---------- 接敌 ----------

# 注：攻击目标不再用硬编码阵营集合。§27.1 要求"能否攻击"一律读 relations 权威表，
# 具体见 _target_factions()。


def _target_factions(conn, cfg, qq: str, mtype: str) -> set:
    """§27.1：攻击目标不再硬编码，一律由关系权威表现算。

    阵位只决定"主动寻歼的意愿范围"，能否打仍然由 relations 决定：
      raid/attack → 所有可攻击阵营
      blockade    → 只打商船（压港断商，不主动挑正规军）
      escort/巡逻 → 只打袭击者（海盗/叛军这类敌对阵营）
    """
    attackable = relations.hostile_factions(conn, cfg, qq)
    if mtype in ("raid", "attack"):
        return set(attackable)
    if mtype == "blockade":
        return {f for f in attackable if f in ("neutral_merchant", "empire")}
    if mtype in ("escort", "patrol", "asw"):
        return {f for f in attackable if f in ("pirate", "rebel", "enforcer")}
    return {f for f in attackable if f in ("pirate", "rebel")}


def maybe_start_battles(conn, cfg: dict, war_tick: int) -> list:
    """判定开战：同格必战；破交/封锁/护航/攻击阵位则在半径内主动寻歼。

    能否开战读 relations 权威表（§27.1），合法关系不成立则不开战。
    """
    new_ids = []
    ais = [r for r in conn.execute("SELECT * FROM ai_fleets").fetchall()
           if json.loads(r["comp_json"] or "[]")]
    for f in conn.execute("SELECT * FROM fleets").fetchall():
        if fleet.ship_count(conn, f["id"]) <= 0 or fleet.in_battle(conn, f["id"]):
            continue
        m = fleet.mission_of(f)
        mtype = str(m.get("type") or "")
        targets = _target_factions(conn, cfg, f["qq"], mtype)
        radius = int(m.get("radius", 0)) if mtype in ("raid", "blockade", "escort") else 0

        for pr in ais:
            if not relations.can_attack(conn, cfg, f["qq"], pr["faction"]):
                continue        # §27.1 权威判定：关系不允许则完全不接敌
            same = (pr["x"], pr["y"]) == (f["x"], f["y"])
            if same:
                # 同格一律接敌（商船来不及跑）
                pass
            else:
                if not radius or pr["faction"] not in targets:
                    continue
                if max(abs(pr["x"] - f["x"]), abs(pr["y"] - f["y"])) > radius:
                    continue
            if any(1 for _ in conn.execute(
                    "SELECT 1 FROM battles WHERE status='active' AND ai_fleet_id=?",
                    (pr["id"],))):
                continue
            sides = {"A": {"side": "player", "qq": f["qq"], "fleet_id": f["id"]},
                     "B": {"side": "ai", "ai_fleet_id": pr["id"], "level": pr["level"],
                           "faction": pr["faction"]}}
            # 谁先动手 —— 整顿「侵略者/铜墙铁壁」要按这个判攻守。
            # A 方永远是我们这边的玩家，所以只要判断他当时在干什么：
            # 进攻类任务 = 我方主动；其余（移动经过/驻防/伏击/巡逻）算被袭。
            sides["aggressor"] = "A" if mtype in AGGRESSIVE_MISSIONS else "B"
            # §27.1 自动防御协议「同盟协防」：盟友在反应半径内的空闲舰队自动入列
            # （_ally_auto_defense 会就地改写 sides["A"] 的 fleet_ids / co）
            _ally_auto_defense(conn, cfg, sides["A"], pr["x"], pr["y"])
            cur = conn.execute(
                "INSERT INTO battles(tick,x,y,created_at,status,sides_json,rng_seed,"
                "qq,ai_fleet_id,summary) VALUES(?,?,?,?, 'active',?, ?,?,?, '')",
                (war_tick, pr["x"], pr["y"], int(time.time()),
                 json.dumps(sides, ensure_ascii=False),
                 random.randint(1, 10 ** 9), f["qq"], pr["id"]))
            bid = cur.lastrowid
            tag = "（破交拦截）" if mtype == "raid" else ("（封锁拦截）" if mtype == "blockade" else "")
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,0,?)",
                (bid, war_tick,
                 f"⚓ 坐标({pr['x']},{pr['y']}) 遭遇【{pr['name']}】，战斗开始！{tag}"))
            conn.commit()
            new_ids.append(bid)

    # 执法者主动寻歼（§25.3「对该玩家所有舰队无差别进攻」）：同格或相邻即开战
    for e in conn.execute("SELECT * FROM ai_fleets WHERE faction=?",
                          (aiworld.ENFORCER,)).fetchall():
        if not json.loads(e["comp_json"] or "[]"):
            continue
        if any(1 for _ in conn.execute(
                "SELECT 1 FROM battles WHERE status='active' AND ai_fleet_id=?", (e["id"],))):
            continue
        m = fleet.mission_of(e)
        target_qq = m.get("target_qq")
        if not target_qq:
            continue
        if not relations.can_attack(conn, cfg, target_qq, aiworld.ENFORCER):
            continue
        for f in conn.execute("SELECT * FROM fleets WHERE qq=?", (target_qq,)).fetchall():
            if fleet.ship_count(conn, f["id"]) <= 0 or fleet.in_battle(conn, f["id"]):
                continue
            if max(abs(f["x"] - e["x"]), abs(f["y"] - e["y"])) > 1:
                continue
            sides = {"A": {"side": "player", "qq": f["qq"], "fleet_id": f["id"]},
                     "B": {"side": "ai", "ai_fleet_id": e["id"], "level": e["level"],
                           "faction": aiworld.ENFORCER}}
            cur = conn.execute(
                "INSERT INTO battles(tick,x,y,created_at,status,sides_json,rng_seed,"
                "qq,ai_fleet_id,summary) VALUES(?,?,?,?, 'active',?, ?,?,?, '')",
                (war_tick, f["x"], f["y"], int(time.time()),
                 json.dumps(sides, ensure_ascii=False),
                 random.randint(1, 10 ** 9), f["qq"], e["id"]))
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,0,?)",
                (cur.lastrowid, war_tick,
                 f"🚨 【{e['name']}】在({f['x']},{f['y']})咬住了你的舰队！"))
            conn.commit()
            new_ids.append(cur.lastrowid)
            break
    # §6.2 玩家对玩家：已在战争状态且同格/相邻即开战
    seen = set()
    for f in conn.execute("SELECT * FROM fleets").fetchall():
        if fleet.ship_count(conn, f["id"]) <= 0 or fleet.in_battle(conn, f["id"]):
            continue
        for o in conn.execute("SELECT * FROM fleets WHERE qq!=?",
                              (f["qq"],)).fetchall():
            if o["qq"] in seen or fleet.ship_count(conn, o["id"]) <= 0:
                continue
            if fleet.in_battle(conn, o["id"]):
                continue
            if not relations.can_attack_player(conn, cfg, f["qq"], o["qq"]):
                continue
            if max(abs(f["x"] - o["x"]), abs(f["y"] - o["y"])) > 1:
                continue
            key = tuple(sorted((f["id"], o["id"])))
            if key in seen:
                continue
            seen.add(key)
            sides = {"A": {"side": "player", "qq": f["qq"], "fleet_id": f["id"],
                           "fleet_ids": [f["id"]]},
                     "B": {"side": "player", "qq": o["qq"], "fleet_id": o["id"],
                           "fleet_ids": [o["id"]]}}
            cur = conn.execute(
                "INSERT INTO battles(tick,x,y,created_at,status,sides_json,rng_seed,"
                "qq,ai_fleet_id,summary) VALUES(?,?,?,?, 'active',?, ?,?,NULL,'')",
                (war_tick, o["x"], o["y"], int(time.time()),
                 json.dumps(sides, ensure_ascii=False),
                 random.randint(1, 10 ** 9), f["qq"]))
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,0,?)",
                (cur.lastrowid, war_tick,
                 f"⚔️ 玩家会战爆发：{_player_name(conn, f['qq'])} vs "
                 f"{_player_name(conn, o['qq'])}（§6.2）"))
            conn.commit()
            new_ids.append(cur.lastrowid)
            seen.add(f["qq"])
            seen.add(o["qq"])
    return new_ids


# ---------- 单轮 ----------

def _hit_chance(cfg: dict, att: dict, tgt: dict) -> float:
    c = cfg["combat"]
    p = c["hit_base"] + att.get("hit", 0) * c["hit_per_fc"] \
        - tgt.get("speed", 0) * c["hit_speed_penalty"]
    return max(c["hit_min"], min(c["hit_max"], p))


def _is_ss(u: dict) -> bool:
    """潜艇判定：海盗方 cls 以 ss 开头；玩家方同样看舰级（def_id 解析出的 cls）。"""
    return str(u.get("cls") or "").startswith("ss")


def _alive(u: dict) -> bool:
    return u["qty"] > 0 if u["side"] == "a" else u["hp"] > 0


# ---------- 潜艇三态（§19.4）与潜航耐力（§19.5） ----------

SURFACE, PERISCOPE, DEEP = "surface", "periscope", "deep"


def _sub_cfg(cfg: dict) -> dict:
    return cfg.get("submarine") or {}


def _battery_cap(cfg: dict, tier) -> int:
    """潜航耐力上限（单位：战争 tick）；-1 = 无限（T4 核动力 / T5 AIP）。"""
    table = _sub_cfg(cfg).get("battery") or {}
    try:
        return int(table.get(str(int(tier or 1)), 6))
    except Exception:
        return 6


def _enemies_alive(units: list) -> list:
    return [u for u in units if _alive(u)]


def _enemy_has_asw(enemies: list) -> bool:
    return any(u.get("asw", 0) > 0 for u in _enemies_alive(enemies))


def _detected(cfg, sub: dict, enemies: list) -> bool:
    """按 §19.4 判定潜艇当前状态下是否被对方发现。

    水面=必然被探；潜望镜=对方有对海探测即可见，声呐再给 periscope_sonar_bonus 加成；
    深潜=仅声呐 P 检定（deep_sonar_chance）。
    """
    st = sub.get("state") or SURFACE
    if st == SURFACE:
        return True
    sc = _sub_cfg(cfg)
    alive = _enemies_alive(enemies)
    if not alive:
        return False
    if st == PERISCOPE:
        for e in alive:
            if e.get("detect", 0) > 0:
                return True
        p = 0.6 + float(sc.get("periscope_sonar_bonus", 0.20))
        for e in alive:
            if e.get("asw", 0) > 0 and _rnd() < p:
                return True
        return False
    p = float(sc.get("deep_sonar_chance", 0.45))
    for e in alive:
        if e.get("asw", 0) > 0 and _rnd() < p:
            return True
    return False


def _stance_bonus(cfg, stance: str, units: list) -> None:
    """把 §4 任务姿态的效果施加到本方单位上（每轮开打前调用）。

    - asw：对潜探测加成（更容易抓潜艇）
    - ambush：潜艇强制以深潜待机开局 → 首轮 hidden=True 即吃伏击加成
    - garrison：伤害减免在 _damage_scale 侧体现
    """
    task = cfg.get("task") or {}
    if stance == "asw":
        mult = 1.0 + float(task.get("asw_detect_bonus", 0.5))
        for u in units:
            if u.get("asw", 0) > 0:
                u["_asw_base"] = u["asw"]
                u["asw"] = u["asw"] * mult
    elif stance == "ambush":
        for u in units:
            if _is_ss(u):
                u["state"] = DEEP


def _clear_stance(units: list) -> None:
    for u in units:
        if u.get("_asw_base") is not None:
            u["asw"] = u["_asw_base"]
            u.pop("_asw_base", None)


def _damage_scale(cfg, stance: str) -> float:
    """受击方所在舰队的姿态带来的伤害缩放（驻防减伤）。"""
    if stance == "garrison":
        return 1.0 - float((cfg.get("task") or {}).get("garrison_defense", 0.25))
    return 1.0


def _update_sub_state(cfg, sub: dict, enemies: list) -> bool:
    """推进一艘潜艇的状态，返回本轮是否"隐蔽出击"（供伏击加成）。

    §19.4/§19.5 的循环：深潜待机 → 上浮到潜望镜深度攻击 → 被 ASW 盯上就压回深潜。
    电量耗尽则被迫上浮充电（本 tick 充满，避免长期趴窝）。
    """
    sc = _sub_cfg(cfg)
    if not sc.get("enabled", True):
        sub["state"] = SURFACE
        return False

    hidden = (sub.get("batt") is None) or (sub.get("state") == DEEP)
    cap = _battery_cap(cfg, sub.get("tier"))
    batt = sub.get("batt")

    if cap == -1:
        sub["batt"] = None
        if _enemy_has_asw(enemies) and _detected(cfg, sub, enemies):
            sub["state"] = DEEP
        else:
            sub["state"] = PERISCOPE
        return hidden

    if batt is None:
        batt = cap
    if batt <= 0:                            # 电量耗尽：被迫上浮，本 tick 充满
        sub["state"] = SURFACE
        sub["batt"] = cap
        return hidden

    if _enemy_has_asw(enemies) and _detected(cfg, sub, enemies):
        sub["state"] = DEEP
    else:
        sub["state"] = PERISCOPE
    sub["batt"] = batt - 1
    return hidden


def _sub_can_fire(sub: dict) -> bool:
    """深潜无法攻击（§19.4）。"""
    return (sub.get("state") or SURFACE) != DEEP


def _weapon_factor(cfg, tgt: dict, wpn: str) -> float:
    """潜艇受击修正：深潜只吃深弹；潜望镜吃炮但威力打折。"""
    if not _is_ss(tgt):
        return 1.0
    st = tgt.get("state") or SURFACE
    if st == DEEP:
        return 1.0 if wpn == "深弹" else 0.0
    if st == PERISCOPE:
        if wpn == "深弹":
            return 1.0
        return float(_sub_cfg(cfg).get("periscope_gun_penalty", 0.5))
    return 1.0


def _war_tick_of(conn) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key='war_tick'").fetchone()
    try:
        return int(r["value"]) if r and r["value"] is not None else 0
    except (TypeError, ValueError):
        return 0


def _carrier_side_effects(conn, cfg, tgt: dict, dmg: float, events: list,
                          att_name: str, wpn: str, war_tick: int) -> None:
    """§19.15 规则 5/8：航母被击中时的连带效果——打甲板 + roll 殉爆。

    甲板 HP 独立于舰体 HP；甲板归零则该航母停飞，殉爆则是巨额伤害 + 甲板全断。
    """
    if tgt.get("cls") != "cv" or tgt.get("side") != "p":
        return
    fid = tgt.get("fleet_id")
    try:
        deck_txt = air.damage_deck(conn, cfg, fid, dmg * 0.35, war_tick,
                                   dive=("航弹" in wpn or "轰炸" in wpn))
        if deck_txt:
            events.append(deck_txt)
        mag_txt = air.magazine_roll(conn, cfg, fid, tgt, war_tick)
        if mag_txt:
            events.append(mag_txt)
    except Exception:
        logger.exception("[海战模拟器] 航母甲板/殉爆结算异常")


def _apply_damage(conn, cfg, tgt: dict, dmg: float, events: list, att_name: str, wpn: str,
                  comp: list):
    """施加伤害。返回 (实际伤害, 击沉数)——§27.6 伤害贡献表需要。"""
    if tgt["side"] == "p":
        # §19.1 舰员经验：老练/王牌舰员受击伤害打折
        dt = float(tgt.get("_dmg_taken", 1.0) or 1.0)
        if dt != 1.0:
            dmg *= dt
        tgt["hp"] = max(0, tgt["hp"] - dmg)
        conn.execute("UPDATE ships SET hp=? WHERE id=?", (round(tgt["hp"], 1), tgt["id"]))
        # §19.15 规则 5/8：航母吃伤害时连带打甲板 + 殉爆判定
        _carrier_side_effects(conn, cfg, tgt, dmg, events, att_name, wpn,
                              _war_tick_of(conn))
        if tgt["hp"] <= 0:
            # §19.1 战沉有损失：舰长随舰阵亡
            lost_txt = ""
            try:
                from . import captains as _cap
                lost_txt = _cap.on_ship_lost(conn, cfg, tgt["id"], tgt["name"])
            except Exception:
                logger.exception("[海战模拟器] 舰长阵亡结算异常")
            conn.execute("DELETE FROM ships WHERE id=?", (tgt["id"],))
            events.append(f"💥 {att_name} {wpn}击沉【{tgt['name']}】！" + lost_txt)
            return dmg, 1
        events.append(f"  {att_name}{wpn}命中 {tgt['name']}，造成 {dmg:.0f} 伤害"
                      f"（剩 {tgt['hp']:.0f}）")
        return dmg, 0
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
    return dmg, sunk


def _fire_side(conn, cfg, attackers: list, targets: list, round_no: int,
               events: list, comp: list, target_stance: str = "", tally: dict = None):
    """§27.6：tally 按玩家累计伤害与击沉，用于战报功勋与协讨分赏。"""
    scale = _damage_scale(cfg, target_stance)
    for att in attackers:
        if not _alive(att):
            continue
        # 深潜潜艇无法攻击（§19.4）
        if _is_ss(att) and not _sub_can_fire(att):
            continue
        alive = [u for u in targets if _alive(u)]
        # 目标选择：水面舰优先；仅 asw 手能打潜艇
        if att.get("asw", 0) > 0:
            subs = [u for u in alive if _is_ss(u)]
            surf = [u for u in alive if not _is_ss(u)]
            tgt = _pick(surf) if surf else (_pick(subs) if subs else None)
        else:
            surf = [u for u in alive if not _is_ss(u)]
            tgt = _pick(surf) if surf else None
        if tgt is None:
            continue
        # 伏击：未被探测的潜艇首波齐射 +20% 命中（§19.5）
        ambush = 0.0
        if att.get("ambush"):
            ambush = float(_sub_cfg(cfg).get("ambush_bonus", 0.20))
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
            hit = _hit_chance(cfg, att, tgt) + ambush
            if _rnd() > min(0.95, hit):
                continue
            factor = _weapon_factor(cfg, tgt, wpn)
            if factor <= 0:
                events.append(f"  {att['name']}{wpn}打空——{tgt['name']}处于深潜，"
                              f"只有深弹够得着")
                continue
            tag = "（伏击）" if ambush else ""
            dealt, kills = _apply_damage(
                conn, cfg, tgt, power * factor * scale * _uf(0.8, 1.2),
                events, att["name"], f"{wpn}{tag}", comp)
            # §19.2 暴击表：模块损毁/火灾/进水/殉爆
            extra = _crit_check(cfg, tgt, dealt, events)
            if extra > 0:
                dealt2, kills2 = _apply_damage(conn, cfg, tgt, extra, events,
                                               att["name"], "暴击", comp)
                dealt += dealt2
                kills += kills2
            if tally is not None and att.get("qq"):
                t = tally.setdefault(att["qq"], {"damage": 0.0, "kills": 0})
                t["damage"] += dealt
                t["kills"] += kills
                # 被打的一方也记一笔"承受伤害"，用于战报里的己方损失
                if tgt.get("qq"):
                    d = tally.setdefault(tgt["qq"], {"damage": 0.0, "kills": 0})
                    d["lost"] = d.get("lost", 0.0) + dealt


# ---------- 结算 ----------

def _loot_for(cfg: dict, faction: str, level) -> dict:
    """战利品查表，**永不抛异常**。

    原来是 `cfg["pirate"]["loot"][str(b["level"])]` —— 海盗表只有 L1~L3，
    但 B 方可能是 L20/L30 的佣兵（merc 压根没有 loot 配置），于是 KeyError 把
    整个战争 tick 的结算打断，那场战斗就永远卡在 active、之后每个 tick 都失败。
    现在按「该阵营自己的表 → 海盗表 → 取不超过该等级的最高档 → 最低档」逐级回落。
    """
    for src in ((cfg.get(faction) or {}).get("loot"),
                (cfg.get("pirate") or {}).get("loot")):
        if not isinstance(src, dict) or not src:
            continue
        key = str(level)
        if key in src:
            return src[key]
        # 取不超过该等级的最高档（等级超出表格时不再崩）
        nums = sorted(int(k) for k in src if str(k).lstrip("-").isdigit())
        below = [n for n in nums if n <= int(level or 0)]
        pick = below[-1] if below else (nums[0] if nums else None)
        if pick is not None:
            return src[str(pick)]
    return {"steel": 0, "money": 0}


def _finish(conn, cfg, bid: int, war_tick: int, sides: dict, result: str,
            comp: list, detail: str):
    """result: victory/defeat/retreat。返回 (origin, summary) 或 None。"""
    a, b = sides["A"], sides["B"]
    if is_pvp(b):
        return _finish_pvp(conn, cfg, bid, war_tick, sides, result, detail)
    pr = conn.execute("SELECT * FROM ai_fleets WHERE id=?",
                      (b["ai_fleet_id"],)).fetchone()
    if result == "victory":
        bside_faction = b.get("faction") or (pr["faction"] if pr else "pirate")
        if bside_faction == aiworld.ENFORCER:
            summary = _clear_infamy(conn, cfg, a["qq"], a["fleet_id"], pr, war_tick)
        elif bside_faction == aiworld.YUNMO:
            summary = _yunmo_loot(conn, cfg, a["qq"], pr, war_tick)
            # §26.4 击杀后残骸区保留 14 日可打捞
            from . import salvage as _sv
            wx = pr["x"] if pr is not None else b.get("x", 0)
            wy = pr["y"] if pr is not None else b.get("y", 0)
            _sv.create_yunmo_wreck(conn, cfg, wx, wy, war_tick)
            summary += (f"\n⚓ 残骸区留在 ({wx},{wy})，14 日内可 /nw打捞"
                        f"（§26.4 离子核心唯一产出）。")
            for fx in side_fleet_ids(a):
                conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (fx,))
        elif bside_faction in aiworld.ROUTE_FACTIONS:
            summary = _plunder(conn, cfg, a["qq"], b["ai_fleet_id"], pr, war_tick,
                               bside_faction)
        else:
            loot = _loot_for(cfg, bside_faction, b.get("level"))
            co = a.get("co") or []
            if co:
                # §27.6 协讨：掠夺所得按伤害贡献分给各方
                parts = _split_loot(conn, cfg, bid, a, int(loot["money"]),
                                    {"steel": int(loot["steel"])}, a["qq"])
                reward_txt = ("\n💰 战利品按伤害贡献分配（§27.6）：\n"
                              + "\n".join(t for _, t in parts))
            else:
                conn.execute(
                    "UPDATE players SET steel=COALESCE(steel,0)+?,money=COALESCE(money,0)+?"
                    " WHERE qq=?", (loot["steel"], loot["money"], a["qq"]))
                reward_txt = ""
            # §26.1 关系矩阵：击沉该阵营的恶名/通缉热度/声望后果（海盗为 0）
            ships = sum(int(s.get("qty", 0) or 0) for s in comp) or 1
            cons = relations.apply_kill_consequences(
                conn, cfg, a["qq"], bside_faction, ships, war_tick)
            # §26.2 协会剿匪任务进度；攻击协会则被悬赏
            if bside_faction == "pirate":
                guild.progress(conn, a["qq"], "pirate", ships)
            elif bside_faction == "guild":
                cons["guild_wanted"] = guild.on_guild_attacked(
                    conn, cfg, a["qq"], ships, war_tick)
            conn.execute(
                "UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
                (war_tick + cfg["pirate"]["respawn_war_ticks"], b["ai_fleet_id"]))
            conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (a["fleet_id"],))
            for fx in side_fleet_ids(a):
                conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (fx,))
            # §27.1 同盟协防：战报里点出哪些盟友舰队来援
            if a.get("co"):
                summary += ("\n🤝 同盟协防参展：" +
                            "、".join(_player_name(conn, q) for q in a["co"]))
            summary = (f"🏆 海战胜利！坐标({pr['x']},{pr['y']}) 歼灭【{pr['name']}】，"
                       f"缴获 钢{loot['steel']} 资金{loot['money']}。"
                       f"残敌约 {_fmt_min(cfg['pirate']['respawn_war_ticks'] * cfg['tick']['war_min'])} 分钟后重整。"
                       + reward_txt
                       + _consequence_text(cons, bside_faction))
    elif result == "defeat":
        for fx in side_fleet_ids(a):
            conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (fx,))
        summary = f"☠️ 海战失利：你的舰队在({pr['x']},{pr['y']})全军覆没……"
    else:
        n = cfg["fleet"]["retreat_nudge_cells"]
        fr = conn.execute("SELECT * FROM fleets WHERE id=?", (a["fleet_id"],)).fetchone()
        nx = max(0, min(fleet.MAP_SIZE - 1, fr["x"] + n))
        ny = max(0, min(fleet.MAP_SIZE - 1, fr["y"] + n))
        conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                     (nx, ny, a["fleet_id"]))
        # §27.4 追击：激进战损/完全歼灭方不崩溃且可追击（执法者 10 格、普通 5 格）
        chase = _pursue_cells(cfg, b.get("faction") or (
            pr["faction"] if pr else "default"))
        chase_txt = ""
        if chase > 0 and pr is not None and json.loads(pr["comp_json"] or "[]"):
            px, py = pr["x"], pr["y"]
            cx2 = px + max(-chase, min(chase, nx - px))
            cy2 = py + max(-chase, min(chase, ny - py))
            cx2 = max(0, min(fleet.MAP_SIZE - 1, cx2))
            cy2 = max(0, min(fleet.MAP_SIZE - 1, cy2))
            conn.execute("UPDATE ai_fleets SET x=?,y=? WHERE id=?",
                         (cx2, cy2, pr["id"]))
            gap = max(abs(cx2 - nx), abs(cy2 - ny))
            if gap == 0:
                chase_txt = (f"\n🚨 但【{pr['name']}】紧咬不放（追击 {chase} 格），"
                             f"已追至同一海域——下一战争 tick 可能再次接敌！")
            else:
                chase_txt = (f"\n🚨【{pr['name']}】追击 {chase} 格，"
                             f"现距你 {gap} 格。")
        conn.execute("UPDATE battles SET status='over',end_tick=?,summary=?,detail=?"
                     " WHERE id=?", (war_tick, "", detail, bid))
        summary = (f"↩️ 舰队已撤离接触，退至({nx},{ny})，残存舰只保留。"
                   + chase_txt)
    conn.execute(
        "UPDATE battles SET status='over',end_tick=?,summary=?,detail=? WHERE id=?",
        (war_tick, summary, detail, bid))
    # §27.6 战报功勋明细
    merit = _merit_text(conn, cfg, bid, a, b)
    if merit:
        summary += merit
    # §19.1 指挥官经验（§15.3 海军学院 +50%）
    try:
        from . import captains as _cap
        ups = _cap.on_battle(conn, cfg, a["qq"], result == "victory")
        if ups:
            summary += "\n" + "\n".join(ups)
    except Exception:
        logger.exception("[海战模拟器] 舰长经验结算异常")
    # §19.1 舰员经验（新兵/老练/王牌）：按本场战功给幸存舰发经验
    try:
        from . import crew as _crew
        kills = sum(int(r["kills"] or 0) for r in _damage_rows(conn, bid)
                    if r["qq"] == a.get("qq"))
        cnotes = _crew.award(conn, cfg, a["qq"], side_fleet_ids(a), kills)
        if cnotes:
            summary += "\n" + "\n".join(cnotes)
    except Exception:
        logger.exception("[海战模拟器] 舰员经验结算异常")
    conn.execute("UPDATE battles SET summary=? WHERE id=?", (summary, bid))
    conn.commit()
    # 战报必达：优先玩家最近下单的会话，否则回落到 Web（原先只在有队列时才推送，
    # 导致破交/洗劫这种大收益战果静默丢失）
    origin = fleet.latest_origin(conn, a["qq"]) or f"{WEB_ORIGIN_PREFIX}{a['qq']}"
    return (origin, summary)


def _consequence_text(cons: dict, faction: str) -> str:
    """§26.1 关系矩阵在战报里的可读呈现。"""
    parts = []
    if cons.get("infamy"):
        parts.append(f"恶名 +{cons['infamy']:.0f}")
    if cons.get("heat"):
        parts.append(f"通缉热度 +{cons['heat']:.0f}")
    if cons.get("reputation"):
        parts.append(f"声望 {cons['reputation']:+.0f}")
    if not parts:
        return ""
    txt = "\n⚠️ 关系变化：" + "、".join(parts)
    if cons.get("guild_wanted"):
        txt += cons["guild_wanted"]
    return txt


def _plunder(conn, cfg, qq: str, afid: int, pr, war_tick: int,
             faction: str = "neutral_merchant") -> str:
    """破交胜利结算（§26.2）：掠货 + 资金 + 恶名/通缉热度，商船队若干 tick 后重整。"""
    mcfg = (cfg.get("empire_merchant") if faction == "empire"
            else cfg.get("merchant")) or {}
    mission = fleet.mission_of(pr) if pr else {}
    gold = int(mission.get("gold") or mcfg.get("loot_money", 300))
    cargo = mission.get("cargo") or dict(mcfg.get("loot_cargo") or {})
    sets = ["money=COALESCE(money,0)+?"]
    vals = [gold]
    for res, amount in cargo.items():
        sets.append(f"{res}=COALESCE({res},0)+?")
        vals.append(amount)
    vals.append(qq)
    conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)
    # §26.1 关系矩阵：恶名/通缉热度/声望由阵营配置决定（中立商船 +3/艘，帝国商船 +10 恶名/+35 热度）
    ships = sum(int(s.get("qty", 0) or 0) for s in
                json.loads(pr["comp_json"] or "[]")) if pr else 1
    cons = relations.apply_kill_consequences(conn, cfg, qq, faction, max(1, ships), war_tick)
    resp_ticks = int(mission.get("respawn") or mcfg.get("respawn_war_ticks", 40))
    conn.execute("UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
                 (war_tick + resp_ticks, afid))
    cargo_txt = " ".join(f"{k}{v}" for k, v in cargo.items() if v)
    who = "中立商船" if faction != "empire" else "帝国商船"
    return (f"🏴‍☠️ 破交得手！坐标({pr['x']},{pr['y']}) 洗劫【{pr['name']}】（{who}），"
            f"掠得 资金{gold}" + (f" + {cargo_txt}" if cargo_txt else "") +
            f"，该航线约 {_fmt_min(resp_ticks * cfg['tick']['war_min'])} 分钟后恢复。"
            + _consequence_text(cons, faction))


def _clear_infamy(conn, cfg, qq: str, fleet_id, pr, war_tick: int) -> str:
    """全歼执法者批次（§25.3）：恶名直接清零，批次进入冷却。"""
    ecfg = cfg.get("enforcer") or {}
    row = conn.execute("SELECT infamy FROM players WHERE qq=?", (qq,)).fetchone()
    was = int((row["infamy"] if row else 0) or 0)
    conn.execute("UPDATE players SET infamy=0 WHERE qq=?", (qq,))
    if fleet_id is not None:
        conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (fleet_id,))
    name = "执法舰队"
    if pr is not None:
        name = pr["name"]
        m = json.loads(pr["mission"] or "{}")
        m["cooldown_until"] = war_tick + int(ecfg.get("batch_cooldown_ticks", 5040))
        m["init_comp"] = []
        conn.execute("UPDATE ai_fleets SET comp_json='[]',mission=? WHERE id=?",
                     (json.dumps(m, ensure_ascii=False), pr["id"]))
    return (f"🎖️ 全歼【{name}】！\n"
            f"跨国联合舰队铩羽而归，你的恶名从 {was} 直接清零。\n"
            f"（§25.3：全歼当前批次即解除通缉；下次恶名回升才会再来。）")


def _bump_damage(conn, bid: int, tally: dict) -> None:
    """§27.6 把本轮伤害贡献累加进 battle_damage。"""
    for qq, t in tally.items():
        conn.execute(
            "INSERT INTO battle_damage(battle_id,qq,damage,kills,lost)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(battle_id,qq) DO UPDATE SET"
            " damage=damage+excluded.damage, kills=kills+excluded.kills,"
            " lost=lost+excluded.lost",
            (bid, qq, float(t.get("damage", 0)), int(t.get("kills", 0)),
             float(t.get("lost", 0))))
    if tally:
        conn.commit()


def _damage_rows(conn, bid: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM battle_damage WHERE battle_id=? ORDER BY damage DESC",
        (bid,)).fetchall()]


def _player_name(conn, qq: str) -> str:
    r = conn.execute("SELECT name FROM players WHERE qq=?", (qq,)).fetchone()
    return r["name"] if r and r["name"] else qq


def _merit_text(conn, cfg, bid: int, a: dict, b: dict) -> str:
    """§27.6 战报末尾的"击沉/伤害/损失/收益"明细。"""
    rows = _damage_rows(conn, bid)
    if not rows:
        return ""
    total = sum(r["damage"] for r in rows) or 1.0
    lines = ["", "📊 战功与伤害贡献（§27.6）："]
    for r in rows:
        share = r["damage"] / total * 100
        who = _player_name(conn, r["qq"])
        if r["qq"] == a.get("qq"):
            who += "（你）"
        lines.append(f"　{who}：伤害 {r['damage']:.0f}（{share:.0f}%）"
                     f"　击沉 {r['kills']}　承受 {r['lost']:.0f}")
    return "\n".join(lines)


def _split_loot(conn, cfg, bid: int, a: dict, gold: int, cargo: dict,
                base_qq: str) -> list:
    """§27.6 收益按伤害贡献权重分给协讨各方。返回 [(qq, text)]。"""
    rows = _damage_rows(conn, bid)
    total = sum(r["damage"] for r in rows) or 1.0
    out = []
    for r in rows:
        qq = r["qq"]
        share = r["damage"] / total
        g = int(round(gold * share))
        c = {k: int(round(v * share)) for k, v in (cargo or {}).items()}
        sets, vals = ["money=COALESCE(money,0)+?"], [g]
        for res, amount in c.items():
            if amount <= 0:
                continue
            sets.append(f"{res}=COALESCE({res},0)+?")
            vals.append(amount)
        vals.append(qq)
        conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)
        cargo_txt = " ".join(f"{k}{v}" for k, v in c.items() if v)
        out.append((qq, f"　{_player_name(conn, qq)}：资金{g}"
                        + (f" + {cargo_txt}" if cargo_txt else "")))
    conn.commit()
    return out


def _pursue_cells(cfg: dict, faction: str) -> int:
    """§27.4 追击格数：激进战损/完全歼灭方不崩溃且可追击。

    商船/雇佣兵/协会不在追击之列（§26.2 商船「见军舰就跑」）。
    """
    p = (cfg.get("combat") or {}).get("pursue") or {}
    if faction in (p.get("no_pursue") or []):
        return 0
    try:
        return int(p.get(faction, p.get("default", 5)))
    except (TypeError, ValueError):
        return 5


def _rebel_intervention(conn, cfg, b, a, bside, comp, round_no, events, tally):
    """§27.4 叛军乱入：同格有叛军时，自动攻击强度最低方的运输/商船。

    返回是否发生了乱入（用于战报提示）。
    """
    rcfg = (cfg.get("combat") or {}).get("rebel_intervention") or {}
    if not rcfg.get("enabled", True):
        return False
    rebels = [r for r in conn.execute(
        "SELECT * FROM ai_fleets WHERE faction='rebel' AND x=? AND y=? AND comp_json!='[]'",
        (b["x"], b["y"])).fetchall()
        if r["id"] != bside.get("ai_fleet_id")]
    if not rebels:
        return False
    # 判定"强度最低方"：比两方的火力总量
    p_power = sum(float(u.get("fire", 0) or 0) + float(u.get("torpedo", 0) or 0)
                  for u in _player_units_multi(conn, cfg, side_fleet_ids(a)))
    a_power = aiworld._comp_power(comp)
    weak_is_player = p_power <= a_power
    hit_any = False
    for rb in rebels:
        rc = json.loads(rb["comp_json"] or "[]")
        power = aiworld._comp_power(rc) * float(rcfg.get("damage_ratio", 0.03))
        if power <= 0:
            continue
        if weak_is_player:
            trs = [u for u in _player_units_multi(conn, cfg, side_fleet_ids(a))
                   if u.get("cls") == "transport" and _alive(u)]
            if not trs:
                trs = [u for u in _player_units_multi(conn, cfg, side_fleet_ids(a))
                       if _alive(u)]
            if not trs:
                continue
            tgt = min(trs, key=lambda u: u.get("hp", 0))
            _apply_damage(conn, cfg, tgt, power, events, f"叛军【{rb['name']}】",
                          "鱼雷", comp)
            hit_any = True
        else:
            cand = [c for c in comp if c.get("cls") == "transport"
                    and int(c.get("qty", 0) or 0) > 0]
            if not cand:
                cand = [c for c in comp if int(c.get("qty", 0) or 0) > 0]
            if not cand:
                continue
            i = comp.index(cand[0])
            _apply_damage(conn, cfg, {"side": "a", "idx": i, "name": "运输船",
                                      "qty": cand[0].get("qty", 0)},
                          power, events, f"叛军【{rb['name']}】", "鱼雷", comp)
            hit_any = True
    if hit_any:
        events.append(f"  ⚠️ 同格叛军趁乱打劫（§27.4），优先攻击"
                      f"{'我方' if weak_is_player else '敌方'}运输船")
    return hit_any


def _yunmo_loot(conn, cfg, qq: str, pr, war_tick: int) -> str:
    """§26.4 雲墨战利品：海量资源 + 0.1% 旗舰蓝图（两艘各独立 roll）+ 100 金碎片兜底。"""
    ycfg = cfg.get("yunmo") or {}
    loot = ycfg.get("loot") or {}
    sets, vals = [], []
    for res, amount in loot.items():
        sets.append(f"{res}=COALESCE({res},0)+?")
        vals.append(amount)
    if sets:
        vals.append(qq)
        conn.execute(f"UPDATE players SET {','.join(sets)} WHERE qq=?", vals)

    chance = float(ycfg.get("blueprint_chance", 0.001))
    got = []
    for name in ("实验型离子炮战列舰", "实验型无人机打击空天航母"):
        if _rnd() < chance:
            got.append(name)
    fallback = ""
    if got:
        for nm in got:
            conn.execute("UPDATE players SET intel=COALESCE(intel,0)+? WHERE qq=?",
                         (1, qq))
        fallback = ("\n🎖️🎖️ 掉落 T10 旗舰蓝图：" + "、".join(got) +
                    "！（0.1% 独立 roll 命中，全服公告）")
    else:
        frag = int(ycfg.get("fragment_fallback", 100))
        conn.execute("UPDATE players SET intel=COALESCE(intel,0)+? WHERE qq=?",
                     (frag, qq))
        fallback = (f"\n📦 未掉蓝图，按 §26.4 给 {frag} 金碎片兜底。")

    # §26.4 每赛季最多击杀 1 次：记入 meta，后续不再刷新
    if ycfg.get("kill_once_per_season", True):
        conn.execute("INSERT INTO meta(key,value) VALUES('yunmo_killed', '1')"
                     " ON CONFLICT(key) DO UPDATE SET value='1'")
    loot_txt = " ".join(f"{k}{v}" for k, v in loot.items())
    return (f"☢️🏆 【弑神者】全服公告：雲墨の实验型舰队已被歼灭！\n"
            f"缴获 {loot_txt}，另获 T5 金模块碎片若干。{fallback}\n"
            f"（§26.4：每赛季最多击杀 1 次，残骸区保留 14 日可打捞——打捞尚未实现）")


def _morale_cfg(cfg: dict) -> dict:
    return (cfg.get("combat") or {}).get("morale") or {}


def _side_strength(units: list) -> float:
    """一方的"兵力"：按舰数与血量加权，用于算损失比例。"""
    tot = 0.0
    for u in units:
        q = int(u.get("qty", 1) or 1) if u.get("side") == "a" else 1
        tot += q * max(0.0, float(u.get("hp", 0) or 0))
    return tot


def _no_break(cfg: dict, faction: str) -> bool:
    """§25.3/§26.4：执法者与雲墨激进战损，不崩溃不投降。"""
    return faction in (_morale_cfg(cfg).get("no_break_factions") or [])


def _morale_step(cfg: dict, cur: float, before: float, after: float,
                 recovered: bool) -> float:
    """按本轮兵力损失比例扣士气；无损失则缓慢回升。"""
    mc = _morale_cfg(cfg)
    if recovered:
        return min(float(mc.get("start", 100)),
                   cur + float(mc.get("recover_per_round", 1.5)))
    if before <= 0:
        return cur
    lost_pct = max(0.0, (before - after) / before) * 100.0
    return max(0.0, cur - lost_pct * float(mc.get("loss_per_percent", 2.2)))


def _fleet_speed_of(units: list) -> float:
    """编队航速取最慢舰（§19.1）。"""
    sp = [float(u.get("speed", 0) or 0) for u in units if _alive(u)]
    return min(sp) if sp else 0.0


def _can_disengage(cfg: dict, runner: list, chaser: list) -> bool:
    """§19.1 机动：撤退方航速 ≥ 对方 1.2 倍可脱离。"""
    ratio = float((cfg.get("combat") or {}).get("disengage", {}).get(
        "speed_ratio", 1.2))
    rs, cs = _fleet_speed_of(runner), _fleet_speed_of(chaser)
    if cs <= 0:
        return True
    return rs >= cs * ratio


def _crit_check(cfg: dict, tgt: dict, dmg: float, events: list) -> float:
    """§19.2 暴击表：模块损毁 5% / 火灾 2% / 进水 1% / 殉爆 0.5%(×10)。

    返回追加的伤害（火灾/殉爆会立刻体现一部分，其余靠后续 tick 持续掉血）。
    """
    cc = (cfg.get("combat") or {}).get("crit") or {}
    roll = _rnd()
    extra = 0.0
    if roll < float(cc.get("magazine", 0.005)):
        extra = dmg * (float(cc.get("magazine_mult", 10)) - 1)
        events.append(f"  💥💥 殉爆！{tgt['name']} 弹药库爆炸（×{int(cc.get('magazine_mult', 10))} 伤害）")
    elif roll < float(cc.get("magazine", 0.005)) + float(cc.get("flood", 0.01)):
        extra = dmg * float(cc.get("flood_slow", 0.30))
        events.append(f"  🌊 进水！{tgt['name']} 航速下降、持续掉血")
    elif roll < float(cc.get("magazine", 0.005)) + float(cc.get("flood", 0.01)) \
            + float(cc.get("fire", 0.02)):
        extra = float(cc.get("fire_damage", 8))
        events.append(f"  🔥 火灾！{tgt['name']} 持续掉血")
    elif roll < float(cc.get("magazine", 0.005)) + float(cc.get("flood", 0.01)) \
            + float(cc.get("fire", 0.02)) + float(cc.get("module", 0.05)):
        events.append(f"  🔧 模块损毁：{tgt['name']} 一处武器/引擎失效")
    return extra


def _damage_control(conn, cfg, units: list, events: list) -> None:
    """§19.2 损管：海上每 tick 自修 1 HP + 40% 概率灭一处暴击（这里体现为回血）。"""
    dc = (cfg.get("combat") or {}).get("damage_control") or {}
    heal = float(dc.get("self_repair_per_tick", 1))
    if heal <= 0:
        return
    for u in units:
        if u.get("side") != "p" or not _alive(u):
            continue
        mx = float(u.get("maxhp", 0) or 0)
        if mx <= 0 or float(u["hp"]) >= mx:
            continue
        u["hp"] = min(mx, float(u["hp"]) + heal)
        conn.execute("UPDATE ships SET hp=? WHERE id=?", (round(u["hp"], 1), u["id"]))


def _ally_auto_defense(conn, cfg, a: dict, tx: int, ty: int) -> list:
    """§27.1 自动防御协议「同盟协防」：盟友挨打时，其盟友在反应半径内的空闲舰队自动入列。

    返回实际参战的 (qq, fleet_name) 列表；同时把舰队并入 A 方 fleet_ids。
    """
    qq = a.get("qq")
    if not qq:
        return []
    try:
        allies = list(relations.allies_of(conn, cfg, qq))
    except Exception:
        logger.exception("[海战模拟器] 求同盟列表异常（协防名单可能不全）")
        allies = []
    # §6.2 附庸保护国：宗主协防附庸，附庸也随宗主参战（§27.1 自动防御协议）
    try:
        from . import diplomacy as _dip
        lord = _dip.liege_of(conn, cfg, qq)
        if lord and lord not in allies:
            allies.append(lord)
        for sub in _dip.vassals_of(conn, cfg, qq):
            if sub not in allies:
                allies.append(sub)
    except Exception:
        logger.exception("[海战模拟器] 求宗主/附庸关系异常（协防名单可能不全）")
    if not allies:
        return []
    radius = int((cfg.get("fleet") or {}).get("support_radius", 15))
    ids = side_fleet_ids(a)
    joined = []
    for al in allies:
        for f in conn.execute("SELECT * FROM fleets WHERE qq=?", (al,)).fetchall():
            if f["id"] in ids or fleet.ship_count(conn, f["id"]) <= 0:
                continue
            if fleet.in_battle(conn, f["id"]):
                continue
            if max(abs(f["x"] - tx), abs(f["y"] - ty)) > radius:
                continue
            ids.append(f["id"])
            conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                         (tx, ty, f["id"]))
            joined.append((al, f["name"]))
            break       # 每名盟友每次最多派一支
    if joined:
        a["fleet_ids"] = ids
        co = list(a.get("co") or [])
        for al, _n in joined:
            if al not in co:
                co.append(al)
        a["co"] = co
        conn.commit()
    return joined


def _spectator_fleets(conn, cfg, a: dict, bside: dict, x: int, y: int) -> list:
    """§27.1/§27.4 同格的中立第三方舰队（不冻结、可自由离开）。

    只算"既非本方、也非敌对方、且与该场战斗无关"的玩家舰队。
    """
    fids = set(side_fleet_ids(a)) | set(side_fleet_ids(bside))
    out = []
    for r in conn.execute("SELECT * FROM fleets WHERE x=? AND y=?", (x, y)).fetchall():
        if r["id"] in fids:
            continue
        if fleet.ship_count(conn, r["id"]) <= 0:
            continue
        out.append(dict(r))
    return out


def _stray_shots(conn, cfg, b, sides: dict, pu: list, au: list,
                 round_no: int, events: list) -> None:
    """§27.4 流弹误伤：同格旁观的第三方有极低概率被误伤。

    「误伤方按攻击中立处理」——涨恶名，并把被害方对误伤方的关系转为中立敌对。
    """
    sc = (cfg.get("combat") or {}).get("stray") or {}
    chance = float(sc.get("chance", 0.03))
    ratio = float(sc.get("damage_ratio", 0.05))
    if chance <= 0:
        return
    a, bside = sides["A"], sides["B"]
    specs = _spectator_fleets(conn, cfg, a, bside, b["x"], b["y"])
    if not specs:
        return
    for sp in specs:
        if _rnd() >= chance:
            continue
        # 随机决定误伤来自哪一方，并按该方火力造成伤害
        src_is_player = _rnd() < 0.5
        power = sum(float(u.get("fire", 0) or 0) for u in (pu if src_is_player else au))
        dmg = max(1.0, power * ratio)
        victim_qq = sp["qq"]
        shooter_qq = a.get("qq") if src_is_player else None
        # 不要吞异常（第 27 轮的教训：裸 except 会隐藏 NameError 之类的真 bug）
        detail = mines.damage_player_fleet(conn, cfg, sp["id"], dmg)
        if shooter_qq and shooter_qq != victim_qq:
            try:
                relations.set_state(conn, shooter_qq, relations.player_key(victim_qq),
                                    str(sc.get("hostility", "neutral")), 0,
                                    "流弹误伤")
                conn.execute(
                    "UPDATE players SET infamy=COALESCE(infamy,0)+?,"
                    " wanted_heat=MIN(100,COALESCE(wanted_heat,0)+?) WHERE qq=?",
                    (float(sc.get("infamy", 6)), float(sc.get("infamy", 6)) * 0.5,
                     shooter_qq))
                conn.commit()
            except Exception:
                logger.exception("[海战模拟器] 流弹误伤关系结算异常")
        src_name = _player_name(conn, shooter_qq) if shooter_qq else "敌方"
        events.append(f"  💢 流弹误伤：旁观中的【{sp['name']}】（{_player_name(conn, victim_qq)}）"
                      f"被{src_name}的火力波及！{detail}")
        conn.execute(
            "INSERT INTO battle_events(battle_id,war_tick,round_no,text)"
            " VALUES(?,?,?,?)",
            (b["id"], 0, round_no,
             f"💢 流弹误伤旁观者【{sp['name']}】：误伤方按攻击中立处理（§27.4），恶名 +"
             f"{float(sc.get('infamy', 6)):.0f}"))


def scfg(cfg: dict) -> dict:
    return (cfg.get("combat") or {}).get("scale") or {}


def split_stacks(comp: list, cfg: dict) -> list:
    """§27.5 分队堆叠：同型舰按 max_stack 艘编为一个堆叠分队。

    HP/火力=单舰×数量，整体吃一次结算；沉没按整艘数扣减。
    潜艇按 sub_stack（20 艘/群）聚合——§27.5「潜艇狼群」。

    这样 1674 艘也只产生 ~40 个堆叠，单 tick 结算是常数级而非逐舰级。
    """
    sc = scfg(cfg)
    mx = max(1, int(sc.get("max_stack", 50)))
    submx = max(1, int(sc.get("sub_stack", 20)))
    out = []
    for s in comp or []:
        try:
            qty = int(s.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        cls = str(s.get("cls", ""))
        cap = submx if cls.startswith("ss") else mx
        if qty <= cap:
            out.append(dict(s))
            continue
        n = (qty + cap - 1) // cap
        base, rem = qty // n, qty % n
        for i in range(n):
            d = dict(s)
            d["qty"] = base + (1 if i < rem else 0)
            out.append(d)
    return out


def stack_summary(comp: list, cfg: dict) -> str:
    """§27.5 给战报用的一句话编制摘要。"""
    total = sum(int(s.get("qty", 0) or 0) for s in comp or [])
    if not comp:
        return ""
    return f"　编制 {len(comp)} 个堆叠分队 / 合计 {total} 艘"


def battle_rng(seed, war_tick: int, round_no: int):
    """§27.5 确定性 RNG：同一场战斗的同一轮，掷骰结果可重放。

    用 (种子, 战争 tick, 轮次) 派生一个独立 Random，避免共享全局 random 的
    调用顺序依赖——只要种子与轮次相同，产物就一致。
    """
    try:
        s = int(seed or 0)
    except (TypeError, ValueError):
        s = 0
    return random.Random(f"{s}:{war_tick}:{round_no}")


# —— §27.5 确定性 RNG 的取值入口 ——
# 战斗结算一律走下面这几个助手，而不是直接调用全局 random。
# 这样每轮开始时把 _RNG 切到由战斗种子派生的流，事件流水就能重放。
_RNG = random          # 默认（战斗外）仍用全局 random


def _fmt_min(m) -> str:
    """把分钟数显示成人话：30.0 -> 30，7.5 -> 7.5，90 -> 1 小时 30 分，1440 -> 1 天。

    与 gov.fmt_mins 保持同一套口径（war_min 现在是 0.5 这样的小数，
    直接用 f-string 会显示成「30.0 分钟」）。
    """
    m = float(m)
    if m < 60:
        return f"{m:g}"
    h, mm = divmod(int(round(m)), 60)
    if h < 24:
        return (f"{h} 小时 " + f"{mm} 分") if mm else f"{h} 小时"
    d, hh = divmod(h, 24)
    return (f"{d} 天 " + f"{hh} 小时") if hh else f"{d} 天"


def _rnd() -> float:
    return _RNG.random()


def _ri(a: int, b: int) -> int:
    return _RNG.randint(a, b)


def _uf(a: float, b: float) -> float:
    return _RNG.uniform(a, b)


def _pick(seq):
    return _RNG.choice(seq)


def set_battle_rng(b, cfg: dict, war_tick: int, round_no: int) -> None:
    """把当前回合的随机流切到该战斗的确定性流（未开启则用全局 random）。"""
    global _RNG
    if not scfg(cfg).get("deterministic_rng", True):
        _RNG = random
        return
    _RNG = battle_rng(b["rng_seed"] if "rng_seed" in b.keys() else 0,
                      war_tick, round_no)


def _flush_events(conn, bid: int, war_tick: int, round_no: int, events: list) -> None:
    """把本轮事件落库。

    单独抽出来是因为有几条分支会提前 continue（士气崩溃等），
    必须在 continue 之前先落库，否则整轮战报会丢失。
    """
    for line in events or []:
        conn.execute(
            "INSERT INTO battle_events(battle_id,war_tick,round_no,text) VALUES(?,?,?,?)",
            (bid, war_tick, round_no, line))
    conn.commit()


def _strength_of(units: list) -> tuple:
    """把一方的单位列表汇总成 (当前总 HP, 满编总 HP, 单位数)。

    AI 方是堆叠（qty 艘共用一条），所以按 qty 乘算。
    """
    hp = mx = 0.0
    n = 0
    for u in units or []:
        q = int(u.get("qty", 1) or 1) if u.get("side") == "a" else 1
        if q <= 0:
            continue
        h = float(u.get("hp", 0) or 0)
        if h <= 0 and u.get("side") == "a":
            continue
        m = float(u.get("maxhp", 0) or 0) or h
        hp += h * q
        mx += m * q
        n += q
    return hp, mx, n


def _snapshot_round(conn, bid: int, round_no: int, pu: list, au: list) -> None:
    """§27.6 记录本轮双方兵力，供 Web 端画 HP 条。"""
    a_hp, a_mx, a_n = _strength_of(pu)
    b_hp, b_mx, b_n = _strength_of(au)
    conn.execute(
        "INSERT INTO battle_rounds(battle_id,round_no,a_hp,a_max,a_units,"
        "b_hp,b_max,b_units) VALUES(?,?,?,?,?,?,?,?)",
        (bid, round_no, round(a_hp, 1), round(a_mx, 1), a_n,
         round(b_hp, 1), round(b_mx, 1), b_n))
    conn.commit()


def is_pvp(bside: dict) -> bool:
    """§6.2 玩家对玩家：B 方也是玩家舰队（没有 ai_fleet / comp_json）。"""
    return str(bside.get("side") or "") == "player" or bside.get("qq") is not None


def _b_units(conn, bside: dict, comp: list, sides: dict = None) -> list:
    """取 B 方单位：PvP 时也是玩家舰船（side 仍为 'p'，伤害结算逻辑可直接复用）。

    sides 传进来时按攻守套用舰队整顿（PvP 里 B 方也是玩家）。
    """
    if is_pvp(bside):
        role = _role_of(sides, "B") if sides else None
        return _player_units_multi(conn, cfg, side_fleet_ids(bside), role)
    return _ai_units(comp)


def _finish_pvp(conn, cfg, bid: int, war_tick: int, sides: dict, result: str,
                detail: str):
    """§6.2 玩家间战斗收尾：无 AI 战利品，清任务 + 出功勋明细。"""
    a, b = sides["A"], sides["B"]
    a_qq, b_qq = a.get("qq"), b.get("qq")
    for side in (a, b):
        for fx in side_fleet_ids(side):
            conn.execute("UPDATE fleets SET mission='{}' WHERE id=?", (fx,))
    if result == "victory":
        summary = (f"🏆 玩家会战胜利！你击败了提督【{_player_name(conn, b_qq)}】"
                   f"（§6.2）。")
    elif result == "defeat":
        summary = f"☠️ 玩家会战失利：你在与【{_player_name(conn, b_qq)}】的交战中全军覆没。"
    else:
        summary = f"↩️ 你与【{_player_name(conn, b_qq)}】脱离接触（{detail}）。"
    conn.execute("UPDATE battles SET status='over',end_tick=?,summary=?,detail=?"
                 " WHERE id=?", (war_tick, summary, detail, bid))
    merit = _merit_text(conn, cfg, bid, a, b)
    if merit:
        summary += merit
        conn.execute("UPDATE battles SET summary=? WHERE id=?", (summary, bid))
    conn.commit()
    return (f"{WEB_ORIGIN_PREFIX}{a_qq}", summary)


def settle_battles(conn, cfg: dict, war_tick: int) -> list:
    """所有 active battle 各结算 1 轮（撤退判定优先）。返回推送列表。"""
    pushes = []
    for b in conn.execute(
            "SELECT * FROM battles WHERE status='active' ORDER BY id").fetchall():
        sides = json.loads(b["sides_json"] or "{}")
        a, bside = sides["A"], sides["B"]
        fid, afid = a["fleet_id"], bside.get("ai_fleet_id")
        pr = conn.execute("SELECT * FROM ai_fleets WHERE id=?", (afid,)).fetchone() \
            if afid is not None else None
        comp = json.loads(pr["comp_json"] or "[]") if pr is not None else []
        # §27.5 分队堆叠：AI 方按 50 艘（潜艇 20）编堆叠，玩家方逐舰保留
        if not is_pvp(bside):
            comp = split_stacks(comp, cfg)
        prev = conn.execute("SELECT MAX(round_no) m FROM battle_events WHERE battle_id=?",
                            (b["id"],)).fetchone()["m"] or 0

        # 撤退：标记满 retreat_rounds 即脱离（retreat_rounds=1 → 首个 tick 脱离）
        fr = conn.execute("SELECT * FROM fleets WHERE id=?", (fid,)).fetchone()
        fm = json.loads(fr["mission"] or "{}")
        if "retreat_since" in fm and \
                war_tick - fm["retreat_since"] >= cfg["combat"]["retreat_rounds"] - 1:
            # §19.1 机动：撤退方航速 ≥ 对方 1.2 倍才能真正脱离
            _pu = _player_units_multi(conn, cfg, side_fleet_ids(a),
                                     _role_of(sides, 'A'))
            _au = _b_units(conn, bside, comp)
            if _can_disengage(cfg, _pu, _au):
                conn.execute(
                    "INSERT INTO battle_events(battle_id,war_tick,round_no,text)"
                    " VALUES(?,?,?,?)",
                    (b["id"], war_tick, prev + 1,
                     "↩️ 本方舰队航速占优，成功脱离战斗（§19.1 机动）。"))
                conn.commit()
                p = _finish(conn, cfg, b["id"], war_tick, sides, "retreat", comp, "撤退")
                if p:
                    pushes.append(p)
                continue
            conn.execute(
                "INSERT INTO battle_events(battle_id,war_tick,round_no,text)"
                " VALUES(?,?,?,?)",
                (b["id"], war_tick, prev + 1,
                 "⚠️ 摆脱失败：航速不足对方 1.2 倍，仍被咬住（§19.1 机动）。"))
            conn.commit()

        pu = _player_units_multi(conn, cfg, side_fleet_ids(a),
                                 _role_of(sides, 'A'))
        au = _b_units(conn, bside, comp, sides)   # §6.2：B 方可能是另一个玩家
        if not pu or not au:
            conn.commit()
            p = _finish(conn, cfg, b["id"], war_tick, sides,
                        "defeat" if not pu else "victory", comp, "一方已无兵力")
            if p:
                pushes.append(p)
            continue

        round_no = prev + 1
        # §27.5 确定性 RNG：本回合的掷骰切到该战斗种子派生的流
        set_battle_rng(b, cfg, war_tick, round_no)
        events = [f"—— 第{round_no}轮 ——"]

        # §4 任务姿态：驻防减伤 / 反潜探测加成 / 伏击深潜待机
        p_stance = str(fleet.mission_of(fr).get("type") or "")
        a_stance = ""
        if not is_pvp(bside):
            a_stance = str(pr["mission"] and json.loads(pr["mission"] or "{}").get("type") or "")
        _stance_bonus(cfg, p_stance, pu)
        _stance_bonus(cfg, a_stance, au)
        if p_stance == "garrison":
            events.append("  🛡 本方处于驻防阵位，受击伤害减免")
        if a_stance == "garrison":
            events.append("  🛡 敌方处于驻防阵位，受击伤害减免")

        # 潜艇三态：先各自推进状态（潜望镜/深潜/被迫上浮），伏击标记写回单位
        for att, foes in ((pu, au), (au, pu)):
            for u in att:
                if _is_ss(u) and _alive(u):
                    u["ambush"] = _update_sub_state(cfg, u, foes)
                else:
                    u.pop("ambush", None)
        for u in pu + au:
            if _is_ss(u) and _alive(u):
                st = {"surface": "水面", "periscope": "潜望镜深度", "deep": "深潜"}.get(
                    u.get("state") or SURFACE, u.get("state"))
                batt = "" if u.get("batt") is None else f"｜电量 {u['batt']}"
                events.append(f"  🌊 {u['name']} 处于【{st}】{batt}")

        tally = {}
        # §27.5 分段消耗：单 tick 每方最多结算 N 个堆叠，超出部分本轮不参与射击
        cap = int(scfg(cfg).get("tick_stack_cap", 200))
        if len(au) > cap or len(pu) > cap:
            events.append(f"  ⏳ 编制过大，本轮仅结算前 {cap} 个堆叠"
                          f"（§27.5 分段消耗：我方 {len(pu)}／敌方 {len(au)}）")
        # §27.4 叛军乱入：同格叛军趁乱打劫最弱方的运输船
        _rebel_intervention(conn, cfg, b, a, bside, comp, round_no, events, tally)
        _fire_side(conn, cfg, pu[:cap], au, round_no, events, comp,
                   target_stance=a_stance, tally=tally)
        pu2 = _player_units_multi(conn, cfg, side_fleet_ids(a),
                                  _role_of(sides, 'A'))  # 重新读存活
        _carry_sub_state(pu, pu2)
        au2 = _b_units(conn, bside, comp)
        _carry_sub_state(au, au2)
        _stance_bonus(cfg, p_stance, pu2)
        _stance_bonus(cfg, a_stance, au2)
        _fire_side(conn, cfg, au2[:cap], pu2, round_no, events, comp,
                   target_stance=p_stance, tally=tally)
        _bump_damage(conn, b["id"], tally)

        # §27.4 流弹误伤：同格旁观的第三方可能被波及
        _stray_shots(conn, cfg, b, sides, pu2, au2, round_no, events)

        # ⑤ §19.1 损管/士气：自修、士气结算、崩溃检定
        mc = _morale_cfg(cfg)
        mor = sides.get("morale") or {"A": float(mc.get("start", 100)),
                                      "B": float(mc.get("start", 100))}
        p_lost = _side_strength(pu) > _side_strength(pu2)
        a_lost = _side_strength(au) > _side_strength(au2)
        mor["A"] = _morale_step(cfg, float(mor.get("A", 100)),
                                _side_strength(pu), _side_strength(pu2), not p_lost)
        mor["B"] = _morale_step(cfg, float(mor.get("B", 100)),
                                _side_strength(au), _side_strength(au2), not a_lost)
        sides["morale"] = mor
        conn.execute("UPDATE battles SET sides_json=? WHERE id=?",
                     (json.dumps(sides, ensure_ascii=False), b["id"]))
        events.append(f"  🎗 士气：我方 {mor['A']:.0f}　敌方 {mor['B']:.0f}")
        _damage_control(conn, cfg, pu2, events)

        thresh = float(mc.get("break_threshold", 30))
        broke = None
        if mor["A"] < thresh and not _no_break(cfg, "player"):
            broke = ("A", "我方")
        elif mor["B"] < thresh and not _no_break(cfg, bside.get("faction") or ""):
            broke = ("B", "敌方")
        if broke:
            side_key, label = broke
            if side_key == "A":
                # 我方崩溃：自动撤退（§27.4 普通方士气崩溃 → 脱离需 1~2 tick）
                events.append(f"  🏳 {label}士气崩溃（{mor['A']:.0f} < {thresh:.0f}），"
                              f"被迫脱离战斗！")
                _snapshot_round(conn, b["id"], round_no, pu2, au2)
                _flush_events(conn, b["id"], war_tick, round_no, events)
                p = _finish(conn, cfg, b["id"], war_tick, sides, "retreat", comp,
                            "士气崩溃")
                if p:
                    pushes.append(p)
                continue
            events.append(f"  🏳 {label}士气崩溃（{mor['B']:.0f} < {thresh:.0f}），"
                          f"残部溃散！")
            _snapshot_round(conn, b["id"], round_no, pu2, au2)
            _flush_events(conn, b["id"], war_tick, round_no, events)
            p = _finish(conn, cfg, b["id"], war_tick, sides, "victory", comp,
                        "敌方士气崩溃")
            if p:
                pushes.append(p)
            continue

        if not is_pvp(bside):
            _ai_sub_to_comp(comp, au2)
            conn.execute("UPDATE ai_fleets SET comp_json=? WHERE id=?",
                         (json.dumps(comp, ensure_ascii=False), afid))
        _save_player_sub_state(conn, pu)
        # §27.6 先记兵力快照，再落事件（回放时两者按轮号对齐）
        _snapshot_round(conn, b["id"], round_no, pu2, au2)
        _flush_events(conn, b["id"], war_tick, round_no, events)

        pu_left = fleet.ship_count(conn, fid)
        ai_left = sum(s.get("qty", 0) for s in comp)
        if pu_left == 0 or ai_left == 0:
            p = _finish(conn, cfg, b["id"], war_tick, sides,
                        "victory" if ai_left == 0 else "defeat", comp,
                        "\n".join(events))
            if p:
                pushes.append(p)
    return pushes
