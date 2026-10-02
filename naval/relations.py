"""§26.1/§27.1 阵营关系权威表。

工程红线（§27.1）：**敌对阵营/战争/合同状态只在服务端存一份权威表**，
所有"能否攻击、谁自动参战、能否撤退"的判定只读这张表，禁止从战报或历史消息二次推导。

state 语义：
  peace   —— 和平：不可攻击，需先 /nw宣战
  neutral —— 中立：可攻击，但有恶名等后果（§26.1 关系矩阵）
  war     —— 敌对：可自由攻击
  allied  —— 同盟：不可攻击

默认关系来自 config.factions[*].default，玩家没有显式记录时按默认值判定；
一旦玩家宣战/媾和，就在 relations 表落一条显式记录，之后一律以记录为准。
"""

PEACE, NEUTRAL, WAR, ALLIED = "peace", "neutral", "war", "allied"
# §6.2：互不侵犯条约（NAP）。ATTACKABLE 不含它，所以 can_attack 天然拒绝。
NAP = "non_aggression"
# §6.2：附庸/保护国——非对称关系，记在 treaties 表（a=宗主，b=附庸）
VASSAL = "vassal"

ATTACKABLE = (NEUTRAL, WAR)
STATE_ZH = {PEACE: "和平", NEUTRAL: "中立", WAR: "敌对", ALLIED: "同盟",
            NAP: "互不侵犯"}


def faction_cfg(cfg: dict) -> dict:
    return cfg.get("factions") or {}


def faction_name(cfg: dict, fid: str) -> str:
    rec = faction_cfg(cfg).get(fid) or {}
    return rec.get("name") or fid


def all_factions(cfg: dict) -> list:
    return [k for k in faction_cfg(cfg) if not k.startswith("_")]


def get_state(conn, cfg: dict, qq: str, faction: str) -> str:
    """读权威状态：有显式记录用记录，否则用配置默认值。

    特殊规则（§26.1）：对 empire，通缉热度达阈值即视同敌对。
    """
    row = conn.execute("SELECT state FROM relations WHERE qq=? AND faction=?",
                       (qq, faction)).fetchone()
    if row and row["state"]:
        state = row["state"]
    else:
        state = (faction_cfg(cfg).get(faction) or {}).get("default", NEUTRAL)

    dcfg = cfg.get("diplomacy") or {}
    if faction == "empire" and state == PEACE:
        heat = conn.execute("SELECT wanted_heat FROM players WHERE qq=?",
                            (qq,)).fetchone()
        if heat and float(heat["wanted_heat"] or 0) >= float(
                dcfg.get("heat_hostile_threshold", 20)):
            return WAR
    # 恶名达执法阈值时，执法者对所有人生效（§25.3）
    if faction == "enforcer":
        return WAR
    return state


def set_state(conn, qq: str, faction: str, state: str, tick: int = 0,
              note: str = "") -> None:
    conn.execute(
        "INSERT INTO relations(qq,faction,state,since_tick,note) VALUES(?,?,?,?,?)"
        " ON CONFLICT(qq,faction) DO UPDATE SET state=excluded.state,"
        " since_tick=excluded.since_tick, note=excluded.note",
        (qq, faction, state, tick, note))
    conn.commit()


def can_attack(conn, cfg: dict, qq: str, faction: str) -> bool:
    """§27.1 唯一的"能否攻击"判定入口。"""
    return get_state(conn, cfg, qq, faction) in ATTACKABLE


def hostile_factions(conn, cfg: dict, qq: str) -> set:
    """该玩家当前可自由攻击的阵营集合（供战斗目标选择）。"""
    return {f for f in all_factions(cfg) if can_attack(conn, cfg, qq, f)}


def reputation(conn, qq: str, faction: str) -> float:
    r = conn.execute("SELECT value FROM reputation WHERE qq=? AND faction=?",
                     (qq, faction)).fetchone()
    return float(r["value"]) if r else 0.0


def add_reputation(conn, qq: str, faction: str, delta: float,
                   lo: float = -100, hi: float = 100) -> float:
    cur = reputation(conn, qq, faction)
    new = max(lo, min(hi, cur + delta))
    conn.execute(
        "INSERT INTO reputation(qq,faction,value) VALUES(?,?,?)"
        " ON CONFLICT(qq,faction) DO UPDATE SET value=excluded.value",
        (qq, faction, new))
    conn.commit()
    return new


def add_heat(conn, qq: str, delta: float) -> float:
    """通缉热度（§26.1，对 empire），0~100 夹紧。"""
    row = conn.execute("SELECT wanted_heat FROM players WHERE qq=?", (qq,)).fetchone()
    cur = float((row["wanted_heat"] if row else 0) or 0)
    new = max(0.0, min(100.0, cur + delta))
    conn.execute("UPDATE players SET wanted_heat=? WHERE qq=?", (new, qq))
    conn.commit()
    return new


def kill_consequences(conn, cfg, qq: str, faction: str, tick: int = 0) -> dict:
    """§26.1 关系矩阵：击沉某阵营单位的后果。返回 {'infamy','heat','reputation'}。"""
    rec = faction_cfg(cfg).get(faction) or {}
    return {"infamy": float(rec.get("infamy_on_kill", 0) or 0),
            "heat": float(rec.get("heat_on_kill", 0) or 0),
            "reputation": -float(rec.get("infamy_on_kill", 0) or 0) / 2.0}


def apply_kill_consequences(conn, cfg, qq: str, faction: str,
                            ships: int = 1, tick: int = 0) -> dict:
    """按击沉数把 §26.1 的后果落到权威表上。返回实际变化量。"""
    c = kill_consequences(conn, cfg, qq, faction, tick)
    inf = c["infamy"] * ships
    heat = c["heat"] * ships
    rep = c["reputation"] * ships
    if inf:
        conn.execute("UPDATE players SET infamy=COALESCE(infamy,0)+? WHERE qq=?",
                     (inf, qq))
    if heat:
        add_heat(conn, qq, heat)
    if rep and faction in ("empire", "guild", "merc"):
        add_reputation(conn, qq, faction, rep)
    conn.commit()
    return {"infamy": inf, "heat": heat, "reputation": rep}


# ---------- §6.2 玩家间外交（PvP） ----------
# 复用同一张 relations 表：把"另一个玩家"看成一个伪阵营 `player:<qq>`，
# 这样 can_attack / set_state 的全部判定逻辑不用重写，也符合 §27.1
# 「敌对关系只在服务端存一份权威表」的工程红线。

PLAYER_PREFIX = "player:"


def player_key(qq: str) -> str:
    return f"{PLAYER_PREFIX}{qq}"


def is_player_key(fid: str) -> bool:
    return bool(fid) and str(fid).startswith(PLAYER_PREFIX)


def key_to_qq(fid: str) -> str:
    return str(fid)[len(PLAYER_PREFIX):]


def get_pvp_state(conn, cfg: dict, a_qq: str, b_qq: str) -> str:
    """玩家 A 眼中的玩家 B：有显式记录用记录，否则默认和平（即「无战国」）。"""
    if not a_qq or not b_qq or a_qq == b_qq:
        return PEACE
    row = conn.execute("SELECT state FROM relations WHERE qq=? AND faction=?",
                       (a_qq, player_key(b_qq))).fetchone()
    return row["state"] if row and row["state"] else PEACE


def set_pvp_state(conn, a_qq: str, b_qq: str, state: str, tick: int = 0,
                  note: str = "") -> None:
    """单向设定 A→B 的关系；宣战/媾和用 declare_pvp_war / make_pvp_peace 双向写。"""
    _set_player_state(conn, a_qq, b_qq, state, tick, note)


def _set_player_state(conn, a_qq, b_qq, state, tick, note) -> None:
    conn.execute(
        "INSERT INTO relations(qq,faction,state,since_tick,note) VALUES(?,?,?,?,?)"
        " ON CONFLICT(qq,faction) DO UPDATE SET state=excluded.state,"
        " since_tick=excluded.since_tick, note=excluded.note",
        (a_qq, player_key(b_qq), state, tick, note))
    conn.commit()


def declare_pvp_war(conn, a_qq: str, b_qq: str, tick: int = 0) -> None:
    """双向宣战。"""
    _set_player_state(conn, a_qq, b_qq, WAR, tick, "宣战")
    _set_player_state(conn, b_qq, a_qq, WAR, tick, "被宣战")


def make_pvp_peace(conn, a_qq: str, b_qq: str, tick: int = 0) -> None:
    """双向媾和。"""
    _set_player_state(conn, a_qq, b_qq, PEACE, tick, "媾和")
    _set_player_state(conn, b_qq, a_qq, PEACE, tick, "媾和")


def is_neutral(conn, qq: str) -> bool:
    """§6.2 中立观察国：该玩家是否处于中立保护状态。

    放在 relations 而不是 diplomacy，是因为"能否攻击"一律以本模块为准
    （§27.1 红线：敌对关系只在服务端存一份权威表），避免反向依赖。
    """
    if not qq:
        return False
    try:
        r = conn.execute("SELECT is_neutral FROM players WHERE qq=?",
                         (qq,)).fetchone()
    except Exception:
        return False
    return bool(r and r["is_neutral"]) if r else False


def can_attack_player(conn, cfg: dict, a_qq: str, b_qq: str) -> bool:
    """能否攻击该玩家：处于战争状态，且对方不是中立观察国（§6.2）。

    中立国受保护——即便宣战也不行，必须先等对方退出中立。
    """
    if not a_qq or not b_qq or a_qq == b_qq:
        return False
    if is_neutral(conn, b_qq):
        return False
    return get_pvp_state(conn, cfg, a_qq, b_qq) == WAR


def players_snapshot(conn, cfg: dict, qq: str) -> list:
    """外交面板里的"其他玩家"一栏。"""
    out = []
    for r in conn.execute("SELECT qq,name FROM players WHERE qq!=? ORDER BY qq",
                          (qq,)).fetchall():
        st = get_pvp_state(conn, cfg, qq, r["qq"])
        out.append({"qq": r["qq"], "name": r["name"] or r["qq"], "state": st})
    return out


# ---------- §6.2 同盟 / 联军作战（共享视野、协防） ----------

def are_allied(conn, cfg: dict, a_qq: str, b_qq: str) -> bool:
    """双向同盟才算成立（任一方解盟即失效）。"""
    if not a_qq or not b_qq or a_qq == b_qq:
        return False
    return (get_pvp_state(conn, cfg, a_qq, b_qq) == ALLIED
            and get_pvp_state(conn, cfg, b_qq, a_qq) == ALLIED)


def declare_alliance(conn, a_qq: str, b_qq: str, tick: int = 0) -> None:
    _set_player_state(conn, a_qq, b_qq, ALLIED, tick, "缔结同盟")
    _set_player_state(conn, b_qq, a_qq, ALLIED, tick, "缔结同盟")


def break_alliance(conn, a_qq: str, b_qq: str, tick: int = 0) -> None:
    """解盟即回到和平（§6.2；不自动转为敌对）。"""
    _set_player_state(conn, a_qq, b_qq, PEACE, tick, "解除同盟")
    _set_player_state(conn, b_qq, a_qq, PEACE, tick, "解除同盟")


def allies_of(conn, cfg: dict, qq: str) -> list:
    """当前盟友的 qq 列表（需双向同盟）。"""
    out = []
    for r in conn.execute("SELECT qq FROM players WHERE qq!=?", (qq,)).fetchall():
        if are_allied(conn, cfg, qq, r["qq"]):
            out.append(r["qq"])
    return out


def snapshot(conn, cfg: dict, qq: str) -> list:
    """外交总览：每个阵营的当前状态（含默认值与通缉热度）。"""
    out = []
    heat = conn.execute("SELECT wanted_heat FROM players WHERE qq=?", (qq,)).fetchone()
    heat_v = float((heat["wanted_heat"] if heat else 0) or 0)
    for fid in all_factions(cfg):
        state = get_state(conn, cfg, qq, fid)
        rec = conn.execute("SELECT state FROM relations WHERE qq=? AND faction=?",
                           (qq, fid)).fetchone()
        out.append({
            "faction": fid, "name": faction_name(cfg, fid), "state": state,
            "explicit": bool(rec), "desc": (faction_cfg(cfg).get(fid) or {}).get("desc", ""),
            "reputation": reputation(conn, qq, fid), "heat": heat_v,
        })
    return out
