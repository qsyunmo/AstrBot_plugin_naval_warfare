"""世界生成（§1「地图 1000×1000，每个坐标代表一座岛，稀疏存储，未占领海格运行时生成」）。

做法：把地图切成 `island_block` 见方的区块，每个区块最多放 1 座岛；
岛的位置只能落在区块**居中的窗口**里，窗口外留出 `island_block - window` 格余量。
相邻区块的窗口之间因此天然隔开，两岛最小间距 = block - window。

位置与岛型/矿床都由 `blake2b(seed:bx:by)` 推导 —— 纯函数、跨进程稳定、可重放。
岛只在**有人看到/需要**时才写进 islands 表（稀疏存储），
`materialize_rect` 幂等，重复调用不会重复插入，也不会覆盖玩家首都。

注意：单靠「每区块一座岛」**不保证**间距 —— 相邻区块各放一座时，
贴着区块边界的两座可以只差 1 格。必须靠居中的窗口留出余量。
"""
import hashlib
import json
import math
import random
import time

# 默认值；config.world 里可覆盖
DEF_BLOCK = 12         # 区块边长
DEF_GAP = 6            # 两岛最小间距（= block - window）
DEF_CHANCE = 0.72      # 每个区块出岛的概率


def _cfgv(cfg: dict):
    w = cfg.get("world") or {}
    block = max(4, int(w.get("island_block", DEF_BLOCK)))
    gap = int(w.get("island_gap", DEF_GAP))
    gap = max(1, min(block - 2, gap))       # 窗口至少 2 格宽
    chance = min(1.0, max(0.0, float(w.get("island_chance", DEF_CHANCE))))
    seed = w.get("seed", 20261001)
    size = int(w.get("map_size", 1000))
    return block, gap, chance, seed, size


def _block_rng(seed, bx: int, by: int) -> random.Random:
    """区块级确定性 RNG。用 blake2b 而不是内置 hash()——后者有随机盐，跨进程不稳定。"""
    h = hashlib.blake2b(f"{seed}:{bx}:{by}".encode("utf-8"), digest_size=16).digest()
    return random.Random(int.from_bytes(h, "big"))


def block_island(cfg: dict, bx: int, by: int):
    """区块 (bx,by) 的野生岛：返回 (x, y, {"itype","ore","hp"}) 或 None。纯函数。

    peek 与落地都用它，保证「看得见的」和「写进库的」完全一致。
    """
    block, gap, chance, seed, size = _cfgv(cfg)
    rng = _block_rng(seed, bx, by)
    if rng.random() >= chance:
        return None
    window = block - gap                      # 居中窗口宽度
    off = gap // 2
    ix = bx * block + off + rng.randrange(window)
    iy = by * block + off + rng.randrange(window)
    if not (0 <= ix < size and 0 <= iy < size):
        return None
    return ix, iy, _roll_island(cfg, rng)


def peek(cfg: dict, x: int, y: int):
    """(x,y) 上有什么野生岛？纯函数，不碰数据库。没有则返回 None。"""
    block, _gap, _chance, _seed, size = _cfgv(cfg)
    if not (0 <= x < size and 0 <= y < size):
        return None
    got = block_island(cfg, x // block, y // block)
    if not got:
        return None
    ix, iy, isl = got
    return isl if (ix, iy) == (x, y) else None


def _roll_island(cfg: dict, rng: random.Random) -> dict:
    """岛型 + 矿床。沿用 spawn.roll_island 的口径，但用区块 RNG 保证可重放。"""
    types = cfg["island_types"]
    pool = list(types.items())
    weights = [t["spawn_weight"] for _, t in pool]
    tid, tdef = rng.choices(pool, weights=weights, k=1)[0]

    ore = {}
    for ore_id, (lo, hi) in (tdef.get("ore_table") or {}).items():
        if hi > 0 and rng.random() < 0.75:
            ore[ore_id] = rng.randint(lo, hi)
    if tdef.get("fishery"):
        lo, hi = tdef.get("fishery_rich", [1, 3])
        ore["fish"] = rng.randint(lo, hi)
    return {"itype": tid, "ore": ore, "hp": tdef["hp"]}


def blocks_in(x0: int, y0: int, x1: int, y1: int, block: int):
    """矩形覆盖到的区块坐标（含边界）。"""
    for by in range(y0 // block, y1 // block + 1):
        for bx in range(x0 // block, x1 // block + 1):
            yield bx, by


def materialize_rect(conn, cfg: dict, x0: int, y0: int, x1: int, y1: int,
                     cap: int = 4000) -> int:
    """把矩形内该有的野生岛写进 islands 表。返回新建数量（幂等）。

    - 已存在的格子跳过 → 不会覆盖玩家首都 / 正规军占的岛
    - 超过 cap 就停手，避免一次请求写爆数据库
    """
    block, _gap, _chance, _seed, size = _cfgv(cfg)
    x0, x1 = max(0, x0), min(size - 1, x1)
    y0, y1 = max(0, y0), min(size - 1, y1)
    if x0 > x1 or y0 > y1:
        return 0

    existing = {(r["x"], r["y"]) for r in conn.execute(
        "SELECT x,y FROM islands WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
        (x0, x1, y0, y1)).fetchall()}

    now = int(time.time())
    made = 0
    for bx, by in blocks_in(x0, y0, x1, y1, block):
        got = block_island(cfg, bx, by)
        if not got:
            continue
        ix, iy, isl = got
        if not (x0 <= ix <= x1 and y0 <= iy <= y1):
            continue
        if (ix, iy) in existing:
            continue
        conn.execute(
            "INSERT INTO islands(x,y,itype,ore_json,dev_level,owner_qq,owner_kind,"
            "control,morale,hp,created_at) VALUES(?,?,?,?,0,NULL,NULL,0,0,?,?)",
            (ix, iy, isl["itype"],
             json.dumps(isl["ore"], ensure_ascii=False), isl["hp"], now))
        existing.add((ix, iy))
        made += 1
        if made >= cap:
            break
    if made:
        conn.commit()
    return made


def stats(cfg: dict) -> dict:
    """理论统计（用于自检/展示），不写库。"""
    block, gap, chance, seed, size = _cfgv(cfg)
    nb = math.ceil(size / block)
    return {"block": block, "gap": gap, "chance": chance,
            "blocks": nb * nb, "expected_islands": round(nb * nb * chance),
            "density_pct": round(100.0 * chance / (block * block), 3),
            "map_size": size, "seed": seed}


def is_wild_at(conn, x: int, y: int) -> bool:
    """该格是否有野生岛（已落库的无主岛）。"""
    r = conn.execute("SELECT owner_qq,owner_kind FROM islands WHERE x=? AND y=?",
                     (x, y)).fetchone()
    return bool(r) and r["owner_qq"] is None and r["owner_kind"] is None
