"""§19.15 航空母舰 + 舰载机四型（中队制）。

关键规则（照抄设计文档）：
  1. 每战争 tick 可出击中队数 = 单波上限；返航/整备占甲板
  2. CAP：来袭机群先与防守方战斗机打空战，`对空总和 ÷ 敌机HP → 击落架数`
  3. 突防：CAP 后存活的攻击机面对编队防空，再削一轮
  4. 投弹：俯冲轰炸机点甲板，雷击机走鱼雷结算
  5. 断甲板：甲板 HP 归零 → 无法起降
  6. 成本：每次出击每架耗铝 2
"""
import json
import random

from . import aiworld

FIGHTER, DIVE, TORPEDO, INTERCEPTOR = "fighter", "dive", "torpedo", "interceptor"
ATTACKERS = (DIVE, TORPEDO)


def acfg(cfg: dict) -> dict:
    return cfg.get("air") or {}


def cv_spec(cfg: dict, tier: int) -> dict:
    """§19.15 航母 T1~T5 的机库/单波/甲板 HP。"""
    table = acfg(cfg).get("cv") or {}
    return table.get(str(max(1, min(5, int(tier))))) or table.get("2") or {}


def kind_stats(cfg: dict, kind: str, tier: int) -> dict:
    """某代际某机型的属性。"""
    t = acfg(cfg).get("tiers") or {}
    row = t.get(str(max(1, min(5, int(tier))))) or t.get("2") or {}
    return row.get(kind) or {}


def fcfg(cfg: dict) -> dict:
    return (cfg.get("air") or {}).get("fatigue") or {}


def fatigue_factor(cfg: dict, fatigue: int) -> float:
    """§19.15 规则 1：疲劳对出击威力的折扣。

    低于 penalty_start 无影响；之后线性衰减，拉满时到 min_factor。
    """
    f = fcfg(cfg)
    start = float(f.get("penalty_start", 30))
    mx = float(f.get("max", 100)) or 100.0
    floor = float(f.get("min_factor", 0.40))
    v = float(fatigue or 0)
    if v <= start or mx <= start:
        return 1.0
    t = min(1.0, (v - start) / (mx - start))
    return 1.0 - (1.0 - floor) * t


def fatigue_label(cfg: dict, fatigue: int) -> str:
    v = int(fatigue or 0)
    if v >= 80:
        return "🟥 极度疲劳"
    if v >= 50:
        return "🟠 疲劳"
    if v >= 30:
        return "🟡 略疲劳"
    return "🟢 整备良好"


def add_fatigue(conn, cfg, squads: list) -> None:
    """出击后给参战中队加疲劳。"""
    per = int(fcfg(cfg).get("per_sortie", 25))
    mx = int(fcfg(cfg).get("max", 100))
    for s in squads:
        new = min(mx, int(s["fatigue"] or 0) + per) if "fatigue" in s.keys() else per
        conn.execute("UPDATE squadrons SET fatigue=? WHERE id=?", (new, s["id"]))
    conn.commit()


def recover_fatigue(conn, cfg, war_tick: int) -> None:
    """§19.15 规则 1：每战争 tick 整备恢复疲劳（甲板趴窝的航母不恢复）。"""
    rec = int(fcfg(cfg).get("recover_per_tick", 7))
    if rec <= 0:
        return
    down_cache: dict = {}
    for sq in conn.execute("SELECT * FROM squadrons WHERE fatigue>0").fetchall():
        fid = sq["fleet_id"]
        if fid not in down_cache:
            down_cache[fid] = fleet_deck_down(conn, cfg, fid, war_tick)
        if down_cache[fid]:
            continue          # 甲板全断，无法整备
        conn.execute("UPDATE squadrons SET fatigue=MAX(0,fatigue-?) WHERE id=?",
                     (rec, sq["id"]))
    conn.commit()


def kind_name(cfg: dict, kind: str) -> str:    return ((acfg(cfg).get("kinds") or {}).get(kind) or {}).get("name", kind)


def squadron_size(cfg: dict) -> int:
    return int(acfg(cfg).get("squadron_size", 12))


# ---------- §19.15 规则 5：飞行甲板（独立于舰体 HP） ----------

def deck_hp_of(cfg: dict, tier: int) -> float:
    return float((acfg(cfg).get("deck_hp") or {}).get(str(
        max(1, min(5, int(tier)))), 100))


def ensure_deck(conn, cfg: dict, ship_id: int, tier: int) -> None:
    """给航母补一条甲板状态（首次访问时按代际建满）。"""
    r = conn.execute("SELECT * FROM deck_state WHERE ship_id=?", (ship_id,)).fetchone()
    if r:
        return
    hp = deck_hp_of(cfg, tier)
    conn.execute("INSERT INTO deck_state(ship_id,deck_hp,deck_max,down_until)"
                 " VALUES(?,?,?,0)", (ship_id, hp, hp))
    conn.commit()


def deck_row(conn, cfg: dict, ship_id: int, tier: int):
    ensure_deck(conn, cfg, ship_id, tier)
    return conn.execute("SELECT * FROM deck_state WHERE ship_id=?", (ship_id,)).fetchone()


def fleet_deck_down(conn, cfg: dict, fleet_id: int, war_tick: int) -> bool:
    """舰队里是否所有航母的甲板都趴了（全部无法起降）。

    注意：趴窝 = 甲板 HP 归零 **且** 停飞期未过。
    不能写成「甲板 HP>0 才算可用」——那样 down_until 到期后永远起不来。
    """
    carriers = fleet_carriers(conn, fleet_id)
    if not carriers:
        return False
    up = 0
    for r, tier in carriers:
        d = deck_row(conn, cfg, r["id"], tier)
        if not d:
            continue
        is_down = (float(d["deck_hp"]) <= 0
                   and int(d["down_until"] or 0) > war_tick)
        if not is_down:
            up += 1
    return up == 0


def damage_deck(conn, cfg: dict, fleet_id: int, dmg: float, war_tick: int,
                dive: bool = False) -> str:
    """§19.15 规则 5：航弹打甲板。T3 起装甲甲板 −40%；甲板归零则停飞 N tick。

    返回可读文本（无甲板则空串）。
    """
    a = acfg(cfg)
    armor_tbl = a.get("deck_armor") or {}
    down_ticks = int(a.get("deck_down_ticks", 6))
    lines = []
    for r, tier in fleet_carriers(conn, fleet_id):
        d = deck_row(conn, cfg, r["id"], tier)
        if not d:
            continue
        mult = 1.0
        if dive:
            mult = 1.0
        armor = float(armor_tbl.get(str(tier), 0.0))
        take = dmg * mult * (1.0 - armor)
        cur = max(0.0, float(d["deck_hp"]) - take)
        if cur <= 0:
            conn.execute("UPDATE deck_state SET deck_hp=0,down_until=? WHERE ship_id=?",
                         (war_tick + down_ticks, r["id"]))
            lines.append(f"　🛑【{r['name']}】飞行甲板被打穿！停飞 {down_ticks} 个战争 tick"
                         + (f"（装甲甲板已减伤 {armor*100:.0f}%）" if armor else ""))
        else:
            conn.execute("UPDATE deck_state SET deck_hp=? WHERE ship_id=?",
                         (round(cur, 1), r["id"]))
            lines.append(f"　🛩【{r['name']}】甲板受损 {take:.0f}，剩余 {cur:.0f}"
                         + (f"（装甲甲板 −{armor*100:.0f}%）" if armor else ""))
    conn.commit()
    return "\n".join(lines)


def repair_decks(conn, cfg: dict, war_tick: int) -> None:
    """每战争 tick 自动修补甲板（§19.15 未给数值，这里配置化）。"""
    per = float(acfg(cfg).get("deck_repair_per_tick", 18))
    if per <= 0:
        return
    for r in conn.execute("SELECT * FROM deck_state").fetchall():
        mx = float(r["deck_max"] or 0)
        if float(r["deck_hp"] or 0) >= mx:
            continue
        if int(r["down_until"] or 0) > war_tick:
            continue        # 趴窝期间不修
        conn.execute("UPDATE deck_state SET deck_hp=MIN(?,deck_hp+?) WHERE ship_id=?",
                     (mx, per, r["ship_id"]))
    conn.commit()


# ---------- §19.15 规则 8：殉爆 ----------

def magazine_roll(conn, cfg: dict, fleet_id: int, hit_ship, war_tick: int) -> str:
    """航母被击中时按所携航弹/燃油 roll 殉爆。返回文本（未发生则空串）。"""
    m = acfg(cfg).get("magazine") or {}
    carriers = fleet_carriers(conn, fleet_id)
    if not carriers:
        return ""
    out = []
    for r, tier in carriers:
        n_sq = conn.execute(
            "SELECT COUNT(*) c FROM squadrons WHERE fleet_id=? AND planes>0",
            (fleet_id,)).fetchone()["c"]
        chance = float(m.get("base_chance", 0.04)) + \
            float(m.get("per_squadron", 0.012)) * int(n_sq or 0)
        # §19.15：弹药升降/消防模块（机库槽紫卡）殉爆概率 −50%
        mods = {}
        try:
            mods = json.loads(r["data_json"] or "{}")
        except Exception:
            mods = {}
        modsel = (mods.get("modules") or {}) if isinstance(mods, dict) else {}
        if str(modsel.get("hangar", "")).find("exp") >= 0 or \
                str(modsel.get("hangar", "")).find("fire") >= 0:
            chance *= float(m.get("fire_suppression", 0.5))
        if random.random() >= chance:
            continue
        maxhp = float(r["max_hp"] or r["hp"] or 1)
        dmg = maxhp * float(m.get("damage_ratio", 0.6))
        hp = max(0.0, float(r["hp"]) - dmg)
        if hp <= 0:
            conn.execute("DELETE FROM ships WHERE id=?", (r["id"],))
            out.append(f"　💥💥【{r['name']}】弹药库殉爆！舰体当场解体"
                       f"（{dmg:.0f} 伤害）——史实大凤/信浓式结局。")
        else:
            conn.execute("UPDATE ships SET hp=? WHERE id=?", (round(hp, 1), r["id"]))
            out.append(f"　💥💥【{r['name']}】弹药库殉爆！受到 {dmg:.0f} 巨额伤害"
                       f"（剩 {hp:.0f}）。")
        if m.get("wipe_deck", True):
            conn.execute("UPDATE deck_state SET deck_hp=0,down_until=? WHERE ship_id=?",
                         (war_tick + int(acfg(cfg).get("deck_down_ticks", 6)), r["id"]))
            out.append("　　（殉爆导致飞行甲板全断）")
    conn.commit()
    return "\n".join(out)


# ---------- §19.15 规则 7：环境（风暴 / 夜间） ----------

def roll_weather(conn, cfg: dict, war_tick: int) -> str:
    """每战争 tick 掷一次天气/昼夜，写入 meta.weather。"""
    states = (acfg(cfg).get("weather") or {}).get("states") or {}
    if not states:
        return "clear"
    names = [k for k in states if not k.startswith("_")]
    weights = [float((states[k] or {}).get("weight", 1)) for k in names]
    pick = random.choices(names, weights=weights, k=1)[0]
    conn.execute("INSERT INTO meta(key,value) VALUES('weather',?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (pick,))
    conn.commit()
    return pick


def weather_of(conn) -> str:
    r = conn.execute("SELECT value FROM meta WHERE key='weather'").fetchone()
    return (r["value"] if r and r["value"] else "clear")


def weather_name(cfg: dict, key: str) -> str:
    st = (acfg(cfg).get("weather") or {}).get("states") or {}
    return (st.get(key) or {}).get("name", key)


def launch_factor(cfg: dict, tier: int, weather: str) -> float:
    """§19.15 规则 7：当前环境下该代际航母的可出击比例。"""
    w = acfg(cfg).get("weather") or {}
    t = str(max(1, min(5, int(tier))))
    if weather == "storm":
        tbl = w.get("storm_launch") or {}
        return float(tbl.get(t, 0.0))
    if weather == "night":
        tbl = w.get("night_launch") or {}
        return float(tbl.get(t, 1.0))
    return 1.0


def fleet_launch_factor(conn, cfg, fleet_id: int) -> tuple:
    """舰队整体的可出击比例（取各航母最好的一艘）与说明。"""
    w = weather_of(conn)
    best = 0.0
    for r, tier in fleet_carriers(conn, fleet_id):
        best = max(best, launch_factor(cfg, tier, w))
    return best, w



# ---------- 舰载机队 ----------

def fleet_carriers(conn, fleet_id: int):
    """舰队里的航母（返回 [(ship_row, tier, spec)]）。"""
    out = []
    for r in conn.execute("SELECT * FROM ships WHERE fleet_id=?", (fleet_id,)).fetchall():
        cls = None
        if str(r["def_id"]).isdigit():
            d = conn.execute("SELECT ship_class FROM designs WHERE id=?",
                             (int(r["def_id"]),)).fetchone()
            cls = d["ship_class"] if d else None
        if cls == "cv":
            out.append((r, int(r["tier"] or 1)))
    return out


def fleet_hangar(conn, cfg: dict, fleet_id: int) -> dict:
    """舰队总机库容量与单波上限（多艘航母累加，单波取最大）。"""
    cap = wave = 0
    for r, tier in fleet_carriers(conn, fleet_id):
        spec = cv_spec(cfg, tier)
        cap += int(spec.get("hangar", 4))
        wave = max(wave, int(spec.get("wave", 1)))
    return {"hangar": cap, "wave": wave}


def ensure_squadrons(conn, cfg: dict, fleet_id: int, qq: str,
                     tier: int = 2) -> int:
    """给航母舰队补满默认联队（战斗机 1 + 俯冲 1 + 鱼雷 1，其余补战斗机）。

    编队比例参照 §19.15 的均衡配置（战斗/轰炸/攻击/拦截）。
    """
    hg = fleet_hangar(conn, cfg, fleet_id)
    want = int(hg.get("hangar", 0))
    have = conn.execute("SELECT COUNT(*) c FROM squadrons WHERE fleet_id=?",
                        (fleet_id,)).fetchone()["c"]
    if want <= have:
        return 0
    plan = [FIGHTER, DIVE, TORPEDO] + [FIGHTER] * max(0, want - 3)
    size = squadron_size(cfg)
    made = 0
    for kind in plan[have:want]:
        conn.execute(
            "INSERT INTO squadrons(qq,fleet_id,kind,tier,planes,ready) VALUES(?,?,?,?,?,1)",
            (qq, fleet_id, kind, tier, size))
        made += 1
    if made:
        conn.commit()
    return made


def fleet_squadrons(conn, fleet_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM squadrons WHERE fleet_id=? AND planes>0 ORDER BY kind",
        (fleet_id,)).fetchall()]


def fleet_cap(conn, cfg: dict, fleet_id: int) -> float:
    """防守方 CAP 对空总和（战斗机 + 拦截机）。"""
    total = 0.0
    for s in fleet_squadrons(conn, fleet_id):
        if s["kind"] not in (FIGHTER, INTERCEPTOR):
            continue
        st = kind_stats(cfg, s["kind"], s["tier"])
        total += float(st.get("aa", 0)) * int(s["planes"])
    return total


def fleet_aa(conn, fleet_id: int) -> float:
    """舰队编队防空（各舰 aa 值合计）。"""
    from . import fleet
    total = 0.0
    for r in fleet.fleet_ships(conn, fleet_id):
        st = fleet.ship_stats(r)
        total += float(st.get("aa", 0) or 0)
    return total


# ---------- 空袭 ----------

def strike(conn, cfg: dict, qq: str, fleet_id: int, tx: int, ty: int,
           kinds: list = None):
    """对 (tx,ty) 发动空袭。返回 (ok, text)。

    目标可以是同格的 AI 舰队，或该格的岛屿。
    """
    a = acfg(cfg)
    hg = fleet_hangar(conn, cfg, fleet_id)
    if hg["hangar"] <= 0:
        return False, "❌ 该舰队没有航母（机库容量 0），无法出击"
    squads = fleet_squadrons(conn, fleet_id)
    if not squads:
        return False, "❌ 航母上没有可用中队（机库为空或全部备机耗尽）"

    f = conn.execute("SELECT * FROM fleets WHERE id=?", (fleet_id,)).fetchone()
    dist = max(abs(f["x"] - tx), abs(f["y"] - ty))
    rng_max = int(a.get("strike_range", 12))
    if dist > rng_max:
        return False, f"❌ 目标距 {dist} 格，超出空袭半径 {rng_max} 格"

    war_tick = 0
    mr = conn.execute("SELECT value FROM meta WHERE key='war_tick'").fetchone()
    if mr:
        try:
            war_tick = int(mr["value"])
        except (TypeError, ValueError):
            war_tick = 0

    # §19.15 规则 5：甲板被打穿的航母不能起降
    if fleet_deck_down(conn, cfg, fleet_id, war_tick):
        return False, ("❌ 舰队所有航母的飞行甲板都在趴窝（甲板 HP 归零），"
                       "无法起降。等待修补或换一艘航母。")

    # §19.15 规则 7：风暴/夜间限制出击规模
    wfactor, weather = fleet_launch_factor(conn, cfg, fleet_id)
    if wfactor <= 0:
        return False, (f"❌ 当前【{weather_name(cfg, weather)}】，"
                       f"该代际航母无法出击（§19.15 规则 7）。")
    weather_note = ""
    if wfactor < 1.0:
        weather_note = (f"　🌦 当前【{weather_name(cfg, weather)}】，"
                        f"出击规模 ×{wfactor:.0%}")

    # 波次：一次最多出动 单波上限 个中队（§19.15 规则 1）
    wave = max(1, int(hg["wave"]))
    # §19.15 规则 7：恶劣环境按比例压缩出击规模
    wave = max(1, int(round(wave * wfactor)))
    pool = [s for s in squads if s["kind"] in ATTACKERS] or squads
    pool.sort(key=lambda s: (s["kind"] not in ATTACKERS, s["kind"]))
    launched = pool[:wave]

    planes_out = sum(int(s["planes"]) for s in launched)
    al_cost = planes_out * int(a.get("aluminium_per_plane", 2))
    p = conn.execute("SELECT aluminium,money FROM players WHERE qq=?", (qq,)).fetchone()
    if p and float(p["aluminium"] or 0) < al_cost:
        return False, (f"❌ 铝材不足：出动 {planes_out} 架需铝 {al_cost}，"
                       f"现有 {float(p['aluminium'] or 0):.0f}")

    # 找目标：同格 AI 舰队优先，否则该格岛屿
    tgt_fleet = conn.execute(
        "SELECT * FROM ai_fleets WHERE x=? AND y=? AND comp_json!='[]' LIMIT 1",
        (tx, ty)).fetchone()

    lines = [f"✈️ 空袭 ({tx},{ty})　距 {dist} 格　出动 {len(launched)} 中队 / {planes_out} 架"]
    if weather_note:
        lines.append(weather_note)

    atk_total = 0.0
    deck_atk = 0.0
    fatigue_notes = []
    for s in launched:
        st = kind_stats(cfg, s["kind"], s["tier"])
        n = int(s["planes"])
        # §19.15 规则 1：疲劳削减出击威力
        ff = fatigue_factor(cfg, s["fatigue"] if "fatigue" in s.keys() else 0)
        atk_total += float(st.get("atk", 0)) * n * ff
        if st.get("deck"):
            deck_atk += float(st.get("atk", 0)) * n * ff * float(
                a.get("deck_damage_ratio", 0.35))
        if ff < 1.0:
            fatigue_notes.append(f"{kind_name(cfg, s['kind'])}×{ff:.0%}")
    # §19.15 规则 1：本波次出击计入疲劳（返航后需要整备）
    add_fatigue(conn, cfg, launched)
    _new_fats = [int(conn.execute("SELECT fatigue FROM squadrons WHERE id=?",
                                  (s["id"],)).fetchone()["fatigue"] or 0)
                 for s in launched]
    launched_fat = sum(_new_fats) / max(1, len(_new_fats))
    if fatigue_notes:
        lines.append("　😮‍💨 疲劳折扣：" + "　".join(fatigue_notes)
                     + f"（本波出击后平均疲劳 {launched_fat:.0f}）")

    # ① CAP：防守方战斗机先打空战（§19.15 规则 2）
    cap_power = 0.0
    cap_name = ""
    if tgt_fleet is not None:
        cap_power = float(aiworld._comp_power(
            [c for c in json.loads(tgt_fleet["comp_json"] or "[]")]))
        cap_name = tgt_fleet["name"]
        # AI 舰队的"战斗机"抽象为它自身对空占比
        cap_power *= 0.02
    cap_div = float(a.get("cap_divisor", 16))
    shot_down = 0
    if cap_power > 0:
        shot_down = int(cap_power / max(1.0, cap_div) * random.uniform(0.8, 1.2))
        shot_down = max(0, min(planes_out, shot_down))
        lines.append(f"　① CAP：【{cap_name}】对空拦截，击落 {shot_down} 架")

    # ② 编队防空（§19.15 规则 3）
    aa_kill = 0
    if tgt_fleet is not None:
        aa_val = 0.0
        for c in json.loads(tgt_fleet["comp_json"] or "[]"):
            aa_val += float(c.get("aa", 0) or 0) * int(c.get("qty", 0) or 0)
        aa_kill = int(aa_val * float(a.get("aa_per_defense", 1.2))
                      * random.uniform(0.8, 1.2))
        aa_kill = max(0, min(planes_out - shot_down, aa_kill))
        if aa_val > 0:
            lines.append(f"　② 突防：编队防空再击落 {aa_kill} 架")

    survivors = max(0, planes_out - shot_down - aa_kill)
    ratio = survivors / max(1, planes_out)
    lines.append(f"　③ 突防：{survivors} 架突防成功（{ratio * 100:.0f}%）")

    # ④ 防雷带/装甲减伤（§19.15：再吃防雷带 -40%~55%，按目标舰代际折算）
    armor = 0.0
    if tgt_fleet is not None:
        armor = _armor_of(cfg, json.loads(tgt_fleet["comp_json"] or "[]"))
        if armor > 0:
            lines.append(f"　④ 防雷带/装甲：目标减伤 {armor * 100:.0f}%")

    dmg = (atk_total + deck_atk) * ratio * (1.0 - armor)
    result_txt = ""
    if survivors <= 0:
        lines.append("　→ 全部被拦截，无战果。")
    elif tgt_fleet is not None:
        comp = json.loads(tgt_fleet["comp_json"] or "[]")
        killed = _apply_air_damage(comp, dmg)
        if any(int(c.get("qty", 0) or 0) > 0 for c in comp):
            conn.execute("UPDATE ai_fleets SET comp_json=? WHERE id=?",
                         (json.dumps(comp, ensure_ascii=False), tgt_fleet["id"]))
        else:
            conn.execute(
                "UPDATE ai_fleets SET comp_json='[]',respawn_tick=? WHERE id=?",
                (0, tgt_fleet["id"]))
        conn.commit()
        result_txt = f"　→ 对【{tgt_fleet['name']}】造成 {dmg:.0f} 伤害，击沉 {killed} 艘。"
        lines.append(result_txt)
    else:
        isl = conn.execute("SELECT * FROM islands WHERE x=? AND y=?",
                           (tx, ty)).fetchone()
        if isl:
            hp = max(0.0, float(isl["hp"] or 0) - dmg)
            conn.execute("UPDATE islands SET hp=?, morale=MAX(0,COALESCE(morale,50)-2)"
                         " WHERE x=? AND y=?", (hp, tx, ty))
            conn.commit()
            result_txt = f"　→ 对岛屿 ({tx},{ty}) 造成 {dmg:.0f} 伤害，剩余耐久 {hp:.0f}。"
            lines.append(result_txt)
        else:
            lines.append("　→ 目标海域没有可打击目标。")

    # 成本与损耗
    if p is not None:
        conn.execute("UPDATE players SET aluminium=aluminium-? WHERE qq=?",
                     (al_cost, qq))
    _consume_planes(conn, cfg, launched, shot_down + aa_kill)
    conn.commit()

    lines.append(f"　耗铝 {al_cost}　战损 {shot_down + aa_kill} 架（由备机补充，"
                 f"备机耗尽则中队缩编）")
    return True, "\n".join(lines)


def _armor_of(cfg: dict, comp: list) -> float:
    """§19.15 防雷带/装甲减伤：按目标编队各舰代际加权平均。"""
    table = acfg(cfg).get("armor_reduction") or {}
    num = den = 0.0
    for c in comp:
        n = int(c.get("qty", 0) or 0)
        if n <= 0:
            continue
        t = str(max(1, min(5, int(c.get("tier", 1) or 1))))
        num += float(table.get(t, 0)) * n
        den += n
    return (num / den) if den > 0 else 0.0


def _apply_air_damage(comp: list, dmg: float) -> int:
    """把空袭伤害摊到目标编队上，返回击沉艘数。"""
    total = sum(int(c.get("qty", 0) or 0) for c in comp)
    if total <= 0 or dmg <= 0:
        return 0
    left = dmg
    killed = 0
    for c in comp:
        while int(c.get("qty", 0) or 0) > 0 and left > 0:
            hp = float(c.get("hp", 1) or 1)
            if left >= hp:
                left -= hp
                c["qty"] = int(c["qty"]) - 1
                c["hp"] = c.get("maxhp", hp)
                killed += 1
            else:
                c["hp"] = hp - left
                left = 0
    return killed


def _consume_planes(conn, cfg: dict, launched: list, lost: int) -> None:
    """战损按各中队均摊；架数归零则该中队消失（备机补充已抽象进扣减）。

    §19.15 规则 1：疲劳的机组更容易在空战中吃亏。
    """
    if lost <= 0 or not launched:
        return
    per = lost / len(launched)
    bonus = float(fcfg(cfg).get("loss_bonus", 0.6))
    for s in launched:
        fat = int(s["fatigue"] or 0) if "fatigue" in s.keys() else 0
        extra = 1.0 + (fat / 100.0) * bonus
        left = max(0, int(int(s["planes"]) - round(per * extra)))
        conn.execute("UPDATE squadrons SET planes=? WHERE id=?", (left, s["id"]))
