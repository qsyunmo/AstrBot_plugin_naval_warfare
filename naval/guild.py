"""§26.2 协会剿匪任务。

设计文档原文：「协会舰队 …… 声望≥友好：发护航/剿匪**任务**（预付资金，完成再奖）；
攻击=被悬赏」。

本模块只做剿匪一类：接单（预付资金）→ 击沉 N 艘目标阵营 → /nw交差 领赏并涨声望。
声望门槛由 config.guild_missions.tiers[*].min_rep 决定，声望越高能接的单越大。
"""
import json

from . import relations

ACTIVE, DONE, EXPIRED = "active", "done", "expired"


def _cfg(cfg: dict) -> dict:
    return cfg.get("guild_missions") or {}


def tiers(cfg: dict) -> dict:
    return _cfg(cfg).get("tiers") or {}


def tier_of(cfg: dict, tier: str) -> dict:
    return tiers(cfg).get(str(tier)) or {}


def ensure_missions(conn, cfg: dict, qq: str, war_tick: int) -> int:
    """按声望补齐可接的单子。已有未完成/未过期的单则不补。返回新建条数。"""
    mcfg = _cfg(cfg)
    rep = relations.reputation(conn, qq, "guild")
    have = conn.execute(
        "SELECT COUNT(*) c FROM guild_missions WHERE qq=? AND status=?",
        (qq, ACTIVE)).fetchone()["c"]
    if have:
        return 0
    target = mcfg.get("target_faction", "pirate")
    made = 0
    for key in sorted(tiers(cfg), key=lambda k: int(k)):
        t = tier_of(cfg, key)
        if rep < float(t.get("min_rep", 0)):
            continue
        conn.execute(
            "INSERT INTO guild_missions(qq,tier,name,target_faction,need,progress,"
            "prepay,reward,rep_reward,status,created_tick,expire_tick)"
            " VALUES(?,?,?,?,?,0,?,?,?,?,?,?)",
            (qq, key, t.get("name", f"任务{key}"), target, int(t.get("need", 3)),
             int(t.get("prepay", 0)), int(t.get("reward", 0)), int(t.get("rep", 0)),
             ACTIVE, war_tick, war_tick + int(mcfg.get("refresh_ticks", 144)) * 3))
        made += 1
    if made:
        conn.commit()
    return made


def list_missions(conn, qq: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM guild_missions WHERE qq=? AND status=? ORDER BY tier",
        (qq, ACTIVE)).fetchall()]


def accept(conn, cfg: dict, qq: str, mid: int):
    """接单：立刻预付资金。返回 (ok, text)。"""
    m = conn.execute("SELECT * FROM guild_missions WHERE id=? AND qq=?",
                     (mid, qq)).fetchone()
    if not m:
        return False, f"❌ 找不到任务 #{mid}"
    if m["status"] != ACTIVE:
        return False, f"❌ 任务 #{mid} 已不可接（{m['status']}）"
    if m["accepted"]:
        return False, f"❌ 任务 #{mid} 已经接过了，去 /nw交差 结算"
    conn.execute("UPDATE guild_missions SET accepted=1 WHERE id=?", (mid,))
    conn.commit()
    return True, ""


def progress(conn, qq: str, faction: str, ships: int) -> list:
    """击沉敌舰时推进对应任务进度。返回完成的 (mid, name, reward, rep) 列表。"""
    out = []
    if ships <= 0:
        return out
    rows = conn.execute(
        "SELECT * FROM guild_missions WHERE qq=? AND status=? AND target_faction=?",
        (qq, ACTIVE, faction)).fetchall()
    for m in rows:
        newp = min(int(m["need"]), int(m["progress"]) + ships)
        conn.execute("UPDATE guild_missions SET progress=? WHERE id=?", (newp, m["id"]))
        out.append((m["id"], m["name"], int(m["need"]), newp))
    if rows:
        conn.commit()
    return out


def claim(conn, cfg: dict, qq: str, mid: int):
    """交差：进度达标则发奖 + 涨声望。返回 (ok, text)。"""
    m = conn.execute("SELECT * FROM guild_missions WHERE id=? AND qq=?",
                     (mid, qq)).fetchone()
    if not m:
        return False, f"❌ 找不到任务 #{mid}"
    if m["status"] != ACTIVE:
        return False, f"❌ 任务 #{mid} 已结算"
    if not m["accepted"]:
        return False, (f"❌ 任务 #{mid} 还没接单。先 /nw接单 {mid} "
                       f"（需预付资金 {m['prepay']}）")
    if int(m["progress"]) < int(m["need"]):
        return False, (f"❌ 任务 #{mid}【{m['name']}】尚未完成："
                       f"进度 {m['progress']}/{m['need']} 艘")
    conn.execute(
        "UPDATE players SET money=COALESCE(money,0)+? WHERE qq=?",
        (int(m["reward"]), qq))
    rep = relations.add_reputation(conn, qq, "guild", float(m["rep_reward"]))
    conn.execute("UPDATE guild_missions SET status=? WHERE id=?", (DONE, mid))
    conn.commit()
    return True, (f"✅ 任务 #{mid}【{m['name']}】完成！\n"
                  f"协会支付赏金 资金{m['reward']}（预付款 {m['prepay']} 已先行到账）。\n"
                  f"协会声望 +{m['rep_reward']}，现为 {rep:+.0f}。")


def on_guild_attacked(conn, cfg: dict, qq: str, ships: int, war_tick: int) -> str:
    """§26.2「攻击=被悬赏」：击沉协会舰船 → 声望大跌 + 自动敌对。"""
    if not _cfg(cfg).get("hostile_on_attack", True):
        return ""
    pen = -12.0 * max(1, ships)
    rep = relations.add_reputation(conn, qq, "guild", pen)
    relations.set_state(conn, qq, "guild", relations.WAR, war_tick, "攻击协会被悬赏")
    return (f"\n⚠️ 你袭击了商人协会——协会已悬赏你：声望 {pen:+.0f}（现 {rep:+.0f}），"
            f"关系转为敌对。协会巡逻队见到你会直接开火。")
