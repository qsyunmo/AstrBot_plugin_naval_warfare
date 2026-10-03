"""P2a 舰队：编组 CRUD、航速/油费报价、战争 tick 移动。

fleets.mission json：
  空 '{}'                     在海待命/驻留
  {"type":"move","tx","ty"}   向目标移动，到点清空
  {"type":"attack","tx","ty"} 接敌攻击（到点清空；同格自动开战）
  {"retreat_since":war_tick}  撤离接触（combat 读取）
"""
import json
import math

MAP_SIZE = 1000


def parse_xy(text: str):
    """'234,567' / '234，567' / '234 567' → (234,567)；非法 None。"""
    t = text.replace("，", ",").replace(" ", "")
    if "," not in t:
        return None
    a, _, b = t.partition(",")
    if not (a.lstrip("-").isdigit() and b.lstrip("-").isdigit()):
        return None
    x, y = int(a), int(b)
    if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
        return None
    return x, y


def ship_stats(row) -> dict:
    return json.loads(row["data_json"] or "{}").get("stats", {})


def list_fleets(conn, qq: str):
    return conn.execute("SELECT * FROM fleets WHERE qq=? ORDER BY id", (qq,)).fetchall()


def get_fleet(conn, qq: str, key: str):
    """按名字（或数字 id）取自己的舰队。"""
    if key.isdigit():
        r = conn.execute("SELECT * FROM fleets WHERE id=? AND qq=?",
                         (int(key), qq)).fetchone()
        if r:
            return r
    return conn.execute("SELECT * FROM fleets WHERE qq=? AND name=?",
                        (qq, key.strip())).fetchone()


def fleet_ships(conn, fleet_id: int):
    return conn.execute("SELECT * FROM ships WHERE fleet_id=? ORDER BY id",
                        (fleet_id,)).fetchall()


def ship_count(conn, fleet_id: int) -> int:
    return conn.execute("SELECT COUNT(*) c FROM ships WHERE fleet_id=?",
                        (fleet_id,)).fetchone()["c"]


def fleet_speed(conn, fleet_id: int) -> int:
    """编队航速取最慢舰；空编队 0。

    同一 def_id 的舰 stats 完全相同，所以只按 def_id 取一遍再算。
    关键是**不要 GROUP BY data_json** —— 那是很宽的文本列，
    一队 24 万艘时逐行哈希它要 0.5 秒。
    """
    ids = [r["def_id"] for r in conn.execute(
        "SELECT DISTINCT def_id FROM ships WHERE fleet_id=?",
        (fleet_id,)).fetchall()]
    if not ids:
        return 0
    spds = []
    for did in ids:
        r = conn.execute(
            "SELECT data_json FROM ships WHERE fleet_id=? AND def_id=? LIMIT 1",
            (fleet_id, did)).fetchone()
        if r is not None:
            spds.append(ship_stats(r).get("speed", 0))
    return min(spds) if spds else 0


def in_battle(conn, fleet_id: int):
    for r in conn.execute(
            "SELECT id,sides_json FROM battles WHERE status='active'").fetchall():
        sides = json.loads(r["sides_json"] or "{}")
        a, b = sides.get("A", {}), sides.get("B", {})
        if b.get("fleet_id") == fleet_id:
            return r["id"]
        # §27 一方可有多支舰队参战（fleet_ids 含主队）
        ids = [v for v in (a.get("fleet_ids") or [])]
        if fleet_id in ids:
            return r["id"]
        if a.get("fleet_id") == fleet_id:
            return r["id"]
    return None


def create_fleet(conn, qq: str, name: str, x: int, y: int, tick: int):
    name = name.strip()[:8]
    if not name:
        return None, "舰队名不能为空"
    if conn.execute("SELECT 1 FROM fleets WHERE qq=? AND name=?",
                    (qq, name)).fetchone():
        return None, f"已有同名舰队【{name}】"
    cur = conn.execute(
        "INSERT INTO fleets(qq,name,x,y,mission,created_tick) VALUES(?,?,?,?,'{}',?)",
        (qq, name, x, y, tick))
    conn.commit()
    return get_fleet(conn, qq, str(cur.lastrowid)), None


def add_ships(conn, qq: str, fleet_id: int, names: list):
    """把在港(fleet_id IS NULL)同名舰全部编入。返回 (编入数, 未找到名单)。"""
    added, missing = 0, []
    for nm in names:
        rows = conn.execute(
            "SELECT * FROM ships WHERE qq=? AND name=? AND fleet_id IS NULL",
            (qq, nm)).fetchall()
        if not rows:
            missing.append(nm)
            continue
        added += len(rows)
        conn.execute("UPDATE ships SET fleet_id=? WHERE qq=? AND name=? AND fleet_id IS NULL",
                     (fleet_id, qq, nm))
    conn.commit()
    return added, missing


def remove_ships(conn, qq: str, fleet_id: int, names: list):
    removed, missing = 0, []
    for nm in names:
        cur = conn.execute(
            "UPDATE ships SET fleet_id=NULL WHERE qq=? AND fleet_id=? AND name=?",
            (qq, fleet_id, nm))
        if cur.rowcount:
            removed += cur.rowcount
        else:
            missing.append(nm)
    conn.commit()
    return removed, missing


def move_quote(conn, cfg: dict, fleet_row) -> dict:
    """到目标格的距离/油费/战争 tick 数报价（mission 中须已有 tx,ty）。"""
    m = json.loads(fleet_row["mission"] or "{}")
    tx, ty = m["tx"], m["ty"]
    n = ship_count(conn, fleet_row["id"])
    dist = math.hypot(tx - fleet_row["x"], ty - fleet_row["y"])
    spd = fleet_speed(conn, fleet_row["id"])
    step = max(1, round(spd / cfg["fleet"]["cells_per_speed"]))
    ticks = max(1, math.ceil(dist / step))
    oil = math.ceil(dist) * n * cfg["fleet"]["move_oil_per_ship_cell"]
    return {"dist": math.ceil(dist), "oil": oil, "ticks": ticks,
            "speed": spd, "minutes": ticks * cfg["tick"]["war_min"]}


STATION_TYPES = ("patrol", "asw")   # 先驶向阵位中心、再驻留的任务（§4 巡逻/反潜）


def war_tick_move(conn, cfg: dict):
    """推进所有移动类任务一格。返回抵达事件列表 [(fleet_row, type, x, y)]。

    - move/attack：驶向 (tx,ty)，到点清空 mission
    - patrol/asw：驶向阵位中心 (cx,cy)，进入半径后转 on_station 驻留（不再移动）
    驻防/伏击是原地姿态，不在这里处理。
    """
    arrived = []
    cdiv = cfg["fleet"]["cells_per_speed"]
    for f in conn.execute("SELECT * FROM fleets").fetchall():
        if not f["mission"] or f["mission"] == "{}":
            continue
        m = json.loads(f["mission"] or "{}")
        mtype = m.get("type")

        if mtype in STATION_TYPES:
            cx, cy = m.get("cx"), m.get("cy")
            if cx is None or cy is None:
                continue
            radius = int(m.get("radius", 3))
            if max(abs(f["x"] - cx), abs(f["y"] - cy)) <= radius:
                if m.get("phase") != "on_station":
                    m["phase"] = "on_station"
                    conn.execute("UPDATE fleets SET mission=? WHERE id=?",
                                 (json.dumps(m, ensure_ascii=False), f["id"]))
                    arrived.append((f, mtype, f["x"], f["y"]))
                continue
            step = max(1, round(fleet_speed(conn, f["id"]) / cdiv))
            nx = f["x"] + max(-step, min(step, cx - f["x"]))
            ny = f["y"] + max(-step, min(step, cy - f["y"]))
            conn.execute("UPDATE fleets SET x=?,y=? WHERE id=?", (nx, ny, f["id"]))
            continue

        if mtype not in ("move", "attack"):
            continue
        tx, ty = m["tx"], m["ty"]
        step = max(1, round(fleet_speed(conn, f["id"]) / cdiv))
        nx = f["x"] + max(-step, min(step, tx - f["x"]))
        ny = f["y"] + max(-step, min(step, ty - f["y"]))
        if (nx, ny) == (tx, ty):
            conn.execute("UPDATE fleets SET x=?,y=?,mission='{}' WHERE id=?",
                         (nx, ny, f["id"]))
            arrived.append((f, mtype, nx, ny))
        else:
            conn.execute("UPDATE fleets SET x=?,y=? WHERE id=?", (nx, ny, f["id"]))
    conn.commit()
    return arrived


def mission_of(fleet_row) -> dict:
    """安全解析舰队任务 json。"""
    try:
        return json.loads(fleet_row["mission"] or "{}")
    except Exception:
        return {}


def is_subs_only(conn, fleet_id: int) -> bool:
    """舰队是否清一色潜艇（伏击阵位要求，§4「潜艇舰队」）。

    原来是「取全部 def_id 再逐艘查 designs」（N+1）——
    十万艘编队会做十万次查询。改成一次 JOIN 聚合。
    """
    n = conn.execute("SELECT COUNT(*) c FROM ships WHERE fleet_id=?",
                     (fleet_id,)).fetchone()["c"]
    if not n:
        return False
    bad = conn.execute(
        "SELECT COUNT(*) c FROM ships s LEFT JOIN designs d ON d.id=s.def_id"
        " WHERE s.fleet_id=? AND (d.ship_class IS NULL"
        " OR d.ship_class NOT LIKE 'ss%')", (fleet_id,)).fetchone()["c"]
    return int(bad or 0) == 0


def latest_origin(conn, qq: str):
    """战斗推送用：取该玩家最近一次下单留下的 origin（研究/生产/建造队列）。"""
    for tbl in ("production_queue", "research_queue", "build_queue"):
        try:
            r = conn.execute(
                f"SELECT origin FROM {tbl} WHERE qq=? AND origin IS NOT NULL "
                f"ORDER BY id DESC LIMIT 1", (qq,)).fetchone()
        except Exception:
            continue
        if r and r["origin"]:
            return r["origin"]
    return None


# ------------------------------------------------ 图形化编队（Web 用）
def _cls_of(conn, qq: str, design_name: str, cache: dict):
    """舰名 → (舰种代号, 代际)。ships.def_id 存的是 designs.id，而编队是按舰名分组的。"""
    key = (qq, design_name)
    if key in cache:
        return cache[key]
    r = conn.execute("SELECT ship_class,tier FROM designs WHERE qq=? AND name=?",
                     (qq, design_name)).fetchone()
    cache[key] = (r["ship_class"] if r else "", r["tier"] if r else 1)
    return cache[key]


def port_ships(conn, qq: str) -> list:
    """港区（未编入任何舰队）的舰船，按舰名分组。"""
    rows = conn.execute(
        "SELECT name, COUNT(*) c FROM ships WHERE qq=? AND fleet_id IS NULL"
        " GROUP BY name ORDER BY name", (qq,)).fetchall()
    cache = {}
    out = []
    for r in rows:
        cls, tier = _cls_of(conn, qq, r["name"], cache)
        out.append({"name": r["name"], "count": r["c"],
                    "cls": cls, "tier": tier})
    return out


def overview(conn, qq: str) -> dict:
    """舰队总览：每支舰队的编成（按舰名分组）+ 港区可用舰船。"""
    cache = {}
    fleets = []
    for f in list_fleets(conn, qq):
        comp = []
        # 按 def_id 分组（而不是 name）—— name 不在任何索引里，
        # 24 万艘时 GROUP BY name 每行都要回表，实测 2 秒以上。
        # 名字/代际从 designs 查得到。
        for r in conn.execute(
                "SELECT def_id, COUNT(*) c FROM ships WHERE qq=? AND fleet_id=?"
                " GROUP BY def_id", (qq, f["id"])).fetchall():
            d = conn.execute("SELECT name,tier FROM designs WHERE id=?",
                             (r["def_id"],)).fetchone() if str(r["def_id"]).isdigit() else None
            if d:
                comp.append({"name": d["name"], "count": r["c"],
                             "cls": _cls_of(conn, qq, d["name"], cache)[0],
                             "tier": d["tier"]})
            else:
                one = conn.execute(
                    "SELECT name,tier FROM ships WHERE fleet_id=? AND def_id=? LIMIT 1",
                    (f["id"], r["def_id"])).fetchone()
                nm = one["name"] if one else str(r["def_id"])
                cls, tier = _cls_of(conn, qq, nm, cache)
                comp.append({"name": nm, "count": r["c"], "cls": cls, "tier": tier})
        fleets.append({
            "id": f["id"], "name": f["name"], "x": f["x"], "y": f["y"],
            "in_battle": bool(in_battle(conn, f["id"])),
            "total": ship_count(conn, f["id"]),
            "speed": fleet_speed(conn, f["id"]),
            "subs_only": is_subs_only(conn, f["id"]),
            "comp": comp,
        })
    return {"ok": True, "fleets": fleets, "port": port_ships(conn, qq)}


def get_fleet_by_id(conn, qq: str, fid: int):
    return conn.execute("SELECT * FROM fleets WHERE id=? AND qq=?",
                        (fid, qq)).fetchone()


def set_composition(conn, qq: str, fleet_id: int, want: list):
    """把舰队编成设为目标（多退少补）。

    want: [{"name": 舰名, "qty": 数量}]。已在队里的优先留用，
    不足的从港区（fleet_id IS NULL）补，多余的退回港区。
    返回 (ok, text)。
    """
    f = get_fleet_by_id(conn, qq, fleet_id)
    if not f:
        return False, "找不到该舰队"
    if in_battle(conn, fleet_id):
        return False, "舰队正在交战，不能调编（可先 /nw撤退）"

    cur = {}
    for r in conn.execute("SELECT id,name FROM ships WHERE qq=? AND fleet_id=?",
                          (qq, fleet_id)).fetchall():
        cur.setdefault(r["name"], []).append(r["id"])

    target = {}
    for w in (want or []):
        nm = str((w or {}).get("name", "")).strip()
        try:
            q = int((w or {}).get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if not nm or q <= 0:
            continue
        target[nm] = target.get(nm, 0) + q

    moved_out = moved_in = 0
    # 多余的退回港区
    for nm, ids in cur.items():
        keep = min(len(ids), target.get(nm, 0))
        for sid in ids[keep:]:
            conn.execute("UPDATE ships SET fleet_id=NULL WHERE id=?", (sid,))
            moved_out += 1
    # 不足的从港区补
    short = {}
    for nm, q in target.items():
        have = min(len(cur.get(nm, [])), q)
        if q - have > 0:
            short[nm] = q - have
    for nm, need in short.items():
        rows = conn.execute(
            "SELECT id FROM ships WHERE qq=? AND name=? AND fleet_id IS NULL"
            " LIMIT ?", (qq, nm, need)).fetchall()
        for r in rows:
            conn.execute("UPDATE ships SET fleet_id=? WHERE id=?",
                         (fleet_id, r["id"]))
            moved_in += 1
        if len(rows) < need:
            # 港区不够，不计失败：把能编的先编上，提示差额
            pass
    conn.commit()
    total = ship_count(conn, fleet_id)
    return True, (f"✅【{f['name']}】编成已更新：入队 {moved_in} 艘、"
                  f"移出 {moved_out} 艘，现有 {total} 艘")


def disband(conn, qq: str, fleet_id: int):
    """解散舰队：舰船全部退回港区，删除舰队记录。"""
    f = get_fleet_by_id(conn, qq, fleet_id)
    if not f:
        return False, "找不到该舰队"
    if in_battle(conn, fleet_id):
        return False, "舰队正在交战，不能解散"
    n = ship_count(conn, fleet_id)
    conn.execute("UPDATE ships SET fleet_id=NULL WHERE qq=? AND fleet_id=?",
                 (qq, fleet_id))
    conn.execute("DELETE FROM fleets WHERE id=? AND qq=?", (fleet_id, qq))
    conn.commit()
    return True, f"🗑 已解散【{f['name']}】，{n} 艘舰船退回港区"
