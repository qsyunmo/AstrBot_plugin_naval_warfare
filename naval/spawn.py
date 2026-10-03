"""出生岛生成：随机岛型+矿床，环面距离满足 15~30 格环带（§12 出生规则）"""
import json
import math
import random
import time


def _torus_dist(x1, y1, x2, y2, size):
    dx, dy = abs(x1 - x2), abs(y1 - y2)
    dx = min(dx, size - dx)
    dy = min(dy, size - dy)
    return (dx * dx + dy * dy) ** 0.5


def roll_island(cfg: dict, rng: random.Random, require_capital: bool = False) -> dict:
    """roll 岛型+矿床。require_capital=True 时只从can_spawn池出，且铁矿富度≥3。"""
    types = cfg["island_types"]
    pool = [(tid, t) for tid, t in types.items() if t.get("can_spawn")] \
        if require_capital else list(types.items())
    weights = [t["spawn_weight"] for _, t in pool]
    tid, tdef = rng.choices(pool, weights=weights, k=1)[0]

    ore = {}
    for ore_id, (lo, hi) in tdef.get("ore_table", {}).items():
        if hi > 0 and rng.random() < 0.75:
            ore[ore_id] = rng.randint(lo, hi)
    if require_capital:  # 出生岛必有铁矿且富度≥3
        ore["iron"] = max(ore.get("iron", 0), cfg["spawn"]["capital_ore_min"], rng.randint(3, 5))
    if tdef.get("fishery"):
        lo, hi = tdef.get("fishery_rich", [1, 3])
        ore["fish"] = rng.randint(lo, hi)

    return {"itype": tid, "ore": ore, "hp": tdef["hp"]}


def find_spawn_coord(conn, cfg: dict, rng: random.Random):
    """返回(x,y)。有人时距最近首都15~30格；无人时全图随机。避开已存在岛。"""
    size = cfg["world"]["map_size"]
    s = cfg["spawn"]
    capitals = [(r["capital_x"], r["capital_y"])
                for r in conn.execute("SELECT capital_x,capital_y FROM players "
                                      "WHERE capital_x IS NOT NULL").fetchall()]
    occupied = {(r["x"], r["y"]) for r in conn.execute("SELECT x,y FROM islands").fetchall()}
    try:
        from . import worldgen as _wg
    except Exception:
        _wg = None

    def free(x, y):
        """既没被占，也不能正好压在野生岛上（否则世界生成时会撞岛）。"""
        if (x, y) in occupied:
            return False
        return _wg is None or _wg.peek(cfg, x, y) is None

    if not capitals:  # 全服第一人：全图随机
        for _ in range(s["max_tries"]):
            x, y = rng.randint(0, size - 1), rng.randint(0, size - 1)
            if free(x, y):
                return x, y
        return None

    for _ in range(s["max_tries"]):
        # 直接以随机一个现有首都为锚点，在环带内采样（保证至少一人在30格内）
        ax, ay = rng.choice(capitals)
        r = rng.uniform(s["min_dist"], s["max_dist"])
        theta = rng.uniform(0, 2 * math.pi)
        x = (ax + int(r * math.cos(theta))) % size
        y = (ay + int(r * math.sin(theta))) % size
        if not free(x, y):
            continue
        # 仍需与所有首都保持 ≥15
        if all(_torus_dist(x, y, cx, cy, size) >= s["min_dist"] for cx, cy in capitals):
            return x, y
    return None


def create_capital(conn, cfg: dict, qq: str, name: str):
    """完整注册：坐标+岛+玩家行+初始建筑+新手包。返回((player,island), None) 或 (None, 原因)。"""
    if conn.execute("SELECT 1 FROM players WHERE qq=?", (qq,)).fetchone():
        return None, "你已注册，发 /nw我 查看势力状态"
    rng = random.Random(f"{qq}-{time.time_ns()}")
    coord = find_spawn_coord(conn, cfg, rng)
    if coord is None:
        return None, "海图拥挤，出生失败，请联系管理员"
    x, y = coord
    island = roll_island(cfg, rng, require_capital=True)
    now = int(time.time())
    tick_row = conn.execute("SELECT value FROM meta WHERE key='econ_tick'").fetchone()
    econ_tick = int(tick_row[0]) if tick_row else 0

    res = cfg["start_package"]["resources"]
    conn.execute(
        "INSERT INTO players(qq,name,created_at,capital_x,capital_y,"
        "steel,oil,aluminium,rare_earth,chips,food,supply,manpower,money,science,intel,last_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qq, name, now, x, y, res["steel"], res["oil"], res["aluminium"], res["rare_earth"],
         res["chips"], res["food"], res["supply"], res["manpower"], res["money"],
         res["science"], res["intel"], now))
    conn.execute(
        "INSERT INTO islands(x,y,itype,ore_json,dev_level,owner_qq,owner_kind,control,morale,hp,created_at)"
        " VALUES(?,?,?,?,1,?,'player',100,80,?,?)",
        (x, y, island["itype"], json.dumps(island["ore"], ensure_ascii=False), qq, island["hp"], now))
    for bid, lv in cfg["start_package"]["buildings"]:
        conn.execute("INSERT INTO buildings(x,y,def_id,level,hp,built_tick) VALUES(?,?,?,?,?,?)",
                     (x, y, bid, lv, island["hp"], econ_tick))
    conn.commit()
    player = conn.execute("SELECT * FROM players WHERE qq=?", (qq,)).fetchone()
    island_row = conn.execute("SELECT * FROM islands WHERE x=? AND y=?", (x, y)).fetchone()
    return (player, island_row), None
