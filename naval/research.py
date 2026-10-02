"""蓝图研究（抽卡）核心逻辑 —— §19.17

规则：研究所 Lv 开 T 池；首抽白船体保底；10/50/100 连保底；
未拥有 50% 重 roll；重复转碎片；碎片可定向兑换。
DB 写操作不内部 commit，由调用方统一提交。
"""
import random
import time

from . import pools


def building_level(conn, x: int, y: int, def_id: str) -> int:
    row = conn.execute("SELECT level FROM buildings WHERE x=? AND y=? AND def_id=?",
                       (x, y, def_id)).fetchone()
    return row["level"] if row else 0


def lab_info(conn, qq: str):
    """返回 (lab_lv, proving_lv, x, y)；未注册返回 None。"""
    p = conn.execute("SELECT capital_x,capital_y FROM players WHERE qq=?", (qq,)).fetchone()
    if not p:
        return None
    x, y = p["capital_x"], p["capital_y"]
    return (building_level(conn, x, y, "lab"),
            building_level(conn, x, y, "proving_ground"), x, y)


def research_slots(lab_lv: int) -> int:
    if lab_lv <= 0:
        return 0
    return pools.mod_data()["lab_slots"][lab_lv - 1]


def research_duration(cfg: dict, mod_cfg: dict, proving_lv: int) -> int:
    base = mod_cfg["research_cost"]["1"]["sec"]  # 各 T 时长统一 5 分钟
    ratio = max(mod_cfg["min_time_ratio"],
                1 - mod_cfg["time_discount_per_proving"] * proving_lv)
    return int(base * ratio)


def active_research(conn, qq: str) -> int:
    return conn.execute("SELECT COUNT(*) c FROM research_queue WHERE qq=?", (qq,)).fetchone()["c"]


def _pity_row(conn, qq: str, pool: str):
    row = conn.execute("SELECT * FROM research_pity WHERE qq=? AND pool=?",
                       (qq, pool)).fetchone()
    if not row:
        conn.execute("INSERT INTO research_pity(qq,pool) VALUES(?,?)", (qq, pool))
        row = conn.execute("SELECT * FROM research_pity WHERE qq=? AND pool=?",
                           (qq, pool)).fetchone()
    return row


def _bump_pity(conn, qq: str, pool: str, rarity: str):
    """按出货稀有度推进保底计数（达到/超过档线即清零）。"""
    row = _pity_row(conn, qq, pool)
    idx = pools.RARITY_ORDER.index(rarity)
    sb, sp, sg = row["since_blue"], row["since_purple"], row["since_gold"]
    if idx >= 3:      # 金
        sb = sp = sg = 0
    elif idx >= 2:    # 紫
        sb, sp, sg = 0, 0, sg + 1
    elif idx >= 1:    # 蓝
        sb, sp, sg = 0, sp + 1, sg + 1
    else:             # 白
        sb, sp, sg = sb + 1, sp + 1, sg + 1
    conn.execute("UPDATE research_pity SET since_blue=?,since_purple=?,since_gold=? "
                 "WHERE qq=? AND pool=?", (sb, sp, sg, qq, pool))


def _choose_rarity(mod_cfg: dict, row) -> str:
    pity = mod_cfg["pity"]
    rc = mod_cfg["rarity"]
    if row["since_gold"] + 1 >= pity["gold"]:
        return "gold"
    if row["since_purple"] + 1 >= pity["purple"]:
        cands = ["purple", "gold"]
    elif row["since_blue"] + 1 >= pity["blue"]:
        cands = ["blue", "purple", "gold"]
    else:
        cands = list(pools.RARITY_ORDER)
    weights = [rc[r]["weight"] for r in cands]
    return random.choices(cands, weights=weights, k=1)[0]


def _add_fragments(conn, qq: str, tier: int, rarity: str, amount: int):
    conn.execute(
        "INSERT INTO fragments(qq,tier,rarity,amount) VALUES(?,?,?,?) "
        "ON CONFLICT(qq,tier,rarity) DO UPDATE SET amount=amount+excluded.amount",
        (qq, tier, rarity, amount))


def roll_gacha(conn, mod_cfg: dict, qq: str, cls: str, tier: int) -> dict:
    """抽一次卡并落库（blueprints / fragments / research_pity）。
    返回 {info,is_new,frag,forced}。调用方负责 commit。"""
    pool = pools.pool_id(cls, tier)
    all_mods = pools.pool_modules(cls, tier)
    owned = pools.owned_ids(conn, qq)
    pool_owned = {m["id"] for m in all_mods} & owned
    row = _pity_row(conn, qq, pool)

    forced = None
    # ① 首抽白船体保底
    if not pool_owned:
        cands = [m for m in all_mods if m["rarity"] == "white" and m["slot"] == "hull"]
        chosen = random.choice(cands)
        forced = "first_hull"
    else:
        rarity = _choose_rarity(mod_cfg, row)
        if rarity != "white":
            forced = rarity
        cands = [m for m in all_mods if m["rarity"] == rarity]
        chosen = random.choice(cands)
        # ② 未拥有保护：重复且池内还有未拥有 → 50% 同稀有度重 roll 一次
        if chosen["id"] in owned:
            unowned_same = [m for m in cands if m["id"] not in owned]
            if unowned_same and random.random() < mod_cfg["reroll_dup_chance"]:
                chosen = random.choice(unowned_same)

    is_new = chosen["id"] not in owned
    if is_new:
        conn.execute("INSERT OR IGNORE INTO blueprints(qq,module_id,obtained_at) VALUES(?,?,?)",
                     (qq, chosen["id"], int(time.time())))
        frag = 0
    else:
        frag = mod_cfg["rarity"][chosen["rarity"]]["frag"]
        _add_fragments(conn, qq, tier, chosen["rarity"], frag)
    _bump_pity(conn, qq, pool, chosen["rarity"])
    return {"info": chosen, "is_new": is_new, "frag": frag, "forced": forced}


RARITY_MARK = {"white": "白", "blue": "蓝", "purple": "紫", "gold": "金"}
RARITY_EMOJI = {"white": "⚪", "blue": "🔵", "purple": "🟣", "gold": "🟡"}
_FORCED_TEXT = {"first_hull": "（新池首抽·保底白船体）", "blue": "（10连保底·蓝+）",
                "purple": "（50连保底·紫+）", "gold": "（🎉100连保底·金！）"}


def gacha_text(mod_cfg: dict, cls: str, tier: int, r: dict) -> str:
    """抽卡结果推送/回复文本（handler 与 engine 共用，避免循环导入）。"""
    m = r["info"]
    cname = mod_cfg["classes"][cls]["name"]
    forced = _FORCED_TEXT.get(r["forced"], "")
    head = f"🔬【{cname} T{tier}】研究完成\n"
    if r["is_new"]:
        return (head + f"🎉 新蓝图：{RARITY_EMOJI[m['rarity']]} {RARITY_MARK[m['rarity']]}·"
                       f"{m['name']}（{mod_cfg['slot_names'][m['slot']]}槽）{forced}")
    return (head + f"♻️ 重复：{RARITY_MARK[m['rarity']]}·{m['name']} "
                   f"→ {r['frag']} 碎片{forced}")


def exchange(conn, mod_cfg: dict, qq: str, cls: str, tier: int, name: str):
    """碎片定向兑换。返回 (True, 模块info) 或 (False, 原因)。不 commit。"""
    target = next((m for m in pools.pool_modules(cls, tier) if m["name"] == name), None)
    if not target:
        return False, f"池中没有模块【{name}】，先 /nw蓝图 {mod_cfg['classes'][cls]['name']} 查看"
    if pools.module_info(target["id"])["id"] in pools.owned_ids(conn, qq):
        return False, "你已拥有该模块，无需兑换"
    rdef = mod_cfg["rarity"][target["rarity"]]
    frow = conn.execute("SELECT amount FROM fragments WHERE qq=? AND tier=? AND rarity=?",
                        (qq, tier, target["rarity"])).fetchone()
    have_frag = frow["amount"] if frow else 0
    need_frag = rdef["exchange"]
    if have_frag < need_frag:
        return False, (f"碎片不足：{rdef['name']}T{tier} 碎片 {have_frag}/{need_frag}"
                       f"（/nw碎片 查看库存）")
    need_sci = mod_cfg["research_cost"][str(tier)]["science"]
    p = conn.execute("SELECT science FROM players WHERE qq=?", (qq,)).fetchone()
    if p["science"] < need_sci:
        return False, f"科研点不足：需 {need_sci}，现有 {p['science']:.0f}"
    conn.execute("UPDATE fragments SET amount=amount-? WHERE qq=? AND tier=? AND rarity=?",
                 (need_frag, qq, tier, target["rarity"]))
    conn.execute("UPDATE players SET science=science-? WHERE qq=?", (need_sci, qq))
    conn.execute("INSERT OR IGNORE INTO blueprints(qq,module_id,obtained_at) VALUES(?,?,?)",
                 (qq, target["id"], int(time.time())))
    return True, target


def grant_starter(conn, qq: str, cls: str):
    """新手包：所选舰种全套 T1 + 运输船全套 T1。不 commit。"""
    now = int(time.time())
    ids = [m["id"] for m in pools.pool_modules(cls, 1)]
    ids += [m["id"] for m in pools.pool_modules("transport", 1)]
    for mid in ids:
        conn.execute("INSERT OR IGNORE INTO blueprints(qq,module_id,obtained_at) VALUES(?,?,?)",
                     (qq, mid, now))
