"""§19.1 舰员经验（新兵/老练/王牌中队）。

与「指挥官/舰长」（captains.py，作用于个人）并列：舰员经验挂在**舰船本身**上，
反映"玩家侧逐舰保留舰名与战史"的设计取向——同一艘海圻号打得多了会从新兵变王牌。

三档（config.crew.tiers）：
    新兵 recruit  0 经验，无加成
    老练 veteran  120 经验，命中 +8%，受击 ×0.95
    王牌 ace      360 经验，命中 +18%，受击 ×0.88
"""
from . import fleet

RECRUIT, VETERAN, ACE = "recruit", "veteran", "ace"


def ccfg(cfg: dict) -> dict:
    return cfg.get("crew") or {}


def tiers(cfg: dict) -> dict:
    return ccfg(cfg).get("tiers") or {}


def tier_of(exp: int, cfg: dict) -> str:
    """按经验判定档位（取满足条件的最高档）。"""
    best, best_exp = RECRUIT, -1
    for k, v in tiers(cfg).items():
        need = int(v.get("exp", 0))
        if int(exp) >= need and need > best_exp:
            best, best_exp = k, need
    return best


def tier_name(cfg: dict, key: str) -> str:
    return (tiers(cfg).get(key) or {}).get("name", key)


def spec_of(cfg: dict, exp: int) -> dict:
    return tiers(cfg).get(tier_of(exp, cfg)) or {}


def attach_bonus(conn, cfg, units: list) -> None:
    """把舰员加成叠加到玩家单位上（在 _player_units_multi 里调用）。"""
    for u in units:
        if u.get("side") != "p":
            continue
        exp = int(u.get("crew_exp", 0) or 0)
        sp = spec_of(cfg, exp)
        u["crew_tier"] = tier_of(exp, cfg)
        u["crew_exp"] = exp
        hit = float(sp.get("hit", 0) or 0)
        if hit:
            u["hit"] = float(u.get("hit", 0) or 0) + hit
        dt = float(sp.get("dmg_taken", 1.0) or 1.0)
        if dt != 1.0:
            u["_dmg_taken"] = dt


def award(conn, cfg, qq: str, fids: list, kills: int = 0) -> list:
    """战后给幸存舰船发经验并处理晋升。返回晋升提示。"""
    c = ccfg(cfg)
    base = int(c.get("exp_per_battle", 12))
    per_kill = int(c.get("exp_per_kill", 8))
    gain = base + per_kill * max(0, int(kills))
    if gain <= 0:
        return []
    notes = []
    seen = set()
    for fid in fids:
        for r in conn.execute("SELECT * FROM ships WHERE fleet_id=?", (fid,)).fetchall():
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            keys = r.keys()
            if "crew_exp" not in keys:
                continue
            old = int(r["crew_exp"] or 0)
            new = old + gain
            old_t, new_t = tier_of(old, cfg), tier_of(new, cfg)
            conn.execute("UPDATE ships SET crew_exp=?, crew_tier=? WHERE id=?",
                         (new, new_t, r["id"]))
            if new_t != old_t:
                notes.append(f"　🎗【{r['name']}】舰员晋升"
                             f"{tier_name(cfg, new_t)}（{tier_name(cfg, old_t)} → "
                             f"{tier_name(cfg, new_t)}，经验 {new}）")
    conn.commit()
    return notes


def describe(conn, cfg, ship_row) -> str:
    """一行描述该舰的舰员状态（供船坞/舰队列表显示）。"""
    keys = ship_row.keys()
    exp = int(ship_row["crew_exp"] or 0) if "crew_exp" in keys else 0
    t = tier_of(exp, cfg)
    sp = spec_of(cfg, exp)
    nxt = None
    for k, v in sorted(tiers(cfg).items(), key=lambda kv: int(kv[1].get("exp", 0))):
        need = int(v.get("exp", 0))
        if need > exp:
            nxt = (k, need)
            break
    tail = f"（距{tier_name(cfg, nxt[0])}还差 {nxt[1]-exp}）" if nxt else "（已满级）"
    bonus = []
    if sp.get("hit"):
        bonus.append(f"命中+{float(sp['hit'])*100:.0f}%")
    if float(sp.get("dmg_taken", 1.0)) != 1.0:
        bonus.append(f"受击×{float(sp['dmg_taken']):.2f}")
    return (f"{tier_name(cfg, t)}　经验 {exp}{tail}"
            + ("　" + "、".join(bonus) if bonus else ""))
