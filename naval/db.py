"""SQLite 连接与建库（标准库 sqlite3，P0 低负载直接同步调用）"""
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "naval.db"
SCHEMA_PATH = DATA_DIR / "schema.sql"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """P0→P1：旧库 designs 表补列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）。"""
    # Web 版：players 补 Web 登录密码（pbkdf2 散列；NULL/空 = 尚未设置）
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if pcols:
        for name, decl in (("web_pass", "TEXT"),
                           ("web_pass_at", "INTEGER")):
            if name not in pcols:
                conn.execute(f"ALTER TABLE players ADD COLUMN {name} {decl}")

    # P2b 商港路线安全度（§18.7）：破交/封锁会压低，护航可恢复
    pcols2 = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if pcols2 and "route_security" not in pcols2:
        conn.execute("ALTER TABLE players ADD COLUMN route_security REAL DEFAULT 100")

    # 当前岛：占岛后 /nw建造、/nw电力 等默认作用在哪座岛上。
    # NULL = 首都（与旧行为一致，所以老账号不用迁移数据）。
    if pcols2:
        for name, decl in (("active_x", "INTEGER"), ("active_y", "INTEGER")):
            if name not in pcols2:
                conn.execute(f"ALTER TABLE players ADD COLUMN {name} {decl}")

    # §6.2 中立观察国：宣布中立换取不可侵犯（代价是不能主动攻击玩家）
    if pcols2:
        for name, decl in (("is_neutral", "INTEGER DEFAULT 0"),
                           ("neutral_since", "INTEGER DEFAULT 0")):
            if name not in pcols2:
                conn.execute(f"ALTER TABLE players ADD COLUMN {name} {decl}")

    # 主力舰法案许可（§19.17/§14）：0=未通过法案，1=已获许可
    pcols3 = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if pcols3:
        for name, decl in (("capital_permit", "INTEGER DEFAULT 0"),
                           ("capital_quota_lv", "INTEGER DEFAULT 0")):
            if name not in pcols3:
                conn.execute(f"ALTER TABLE players ADD COLUMN {name} {decl}")

    # P2b §25.3/§26.1 通缉热度（独立于恶名，对 empire）：0~100，2/日衰减
    pcols3 = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if pcols3 and "wanted_heat" not in pcols3:
        conn.execute("ALTER TABLE players ADD COLUMN wanted_heat REAL DEFAULT 0")

    # §18.6 税率法案：资金倍率 + 民心/日
    pcols4 = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if pcols4 and "tax_rate" not in pcols4:
        conn.execute("ALTER TABLE players ADD COLUMN tax_rate INTEGER DEFAULT 5")

    # §26.1/§27.1 阵营关系权威表 + 阵营声望（老库补建）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS relations("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, faction TEXT NOT NULL, state TEXT NOT NULL,"
        " since_tick INTEGER DEFAULT 0, note TEXT,"
        " UNIQUE(qq,faction))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reputation("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, faction TEXT NOT NULL, value REAL DEFAULT 0,"
        " UNIQUE(qq,faction))")

    # §26.2 雇佣合同：到期后雇佣舰队原地解散（变中立）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS contracts("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, fleet_id INTEGER NOT NULL, merc_id INTEGER,"
        " company TEXT, level INTEGER, start_tick INTEGER, expire_tick INTEGER,"
        " price INTEGER, status TEXT DEFAULT 'active')")

    # §26.2 协会剿匪任务
    conn.execute(
        "CREATE TABLE IF NOT EXISTS guild_missions("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, tier TEXT, name TEXT, target_faction TEXT,"
        " need INTEGER, progress INTEGER DEFAULT 0, prepay INTEGER, reward INTEGER,"
        " rep_reward INTEGER, accepted INTEGER DEFAULT 0, status TEXT DEFAULT 'active',"
        " created_tick INTEGER, expire_tick INTEGER)")

    # §19.15 舰载机中队：挂在航母舰队上（1 中队 = 12 架）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS squadrons("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, fleet_id INTEGER NOT NULL, kind TEXT NOT NULL,"
        " tier INTEGER DEFAULT 2, planes INTEGER DEFAULT 12, ready INTEGER DEFAULT 1,"
        " deck_hp REAL DEFAULT 0)")

    # §27.6 战斗伤害贡献表：战报功勋与协讨分赏都读它
    conn.execute(
        "CREATE TABLE IF NOT EXISTS battle_damage("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " battle_id INTEGER NOT NULL, qq TEXT NOT NULL,"
        " damage REAL DEFAULT 0, kills INTEGER DEFAULT 0, lost REAL DEFAULT 0,"
        " UNIQUE(battle_id,qq))")

    # §4 水雷场：一格一条，含雷种与剩余枚数
    conn.execute(
        "CREATE TABLE IF NOT EXISTS minefields("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " x INTEGER NOT NULL, y INTEGER NOT NULL, owner_qq TEXT NOT NULL,"
        " kind TEXT DEFAULT 'anchor', count INTEGER DEFAULT 0, created_tick INTEGER DEFAULT 0,"
        " UNIQUE(x,y))")

    # §19.15 规则 5：航母飞行甲板（独立于舰体 HP）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deck_state("
        " ship_id INTEGER PRIMARY KEY, deck_hp REAL DEFAULT 0, deck_max REAL DEFAULT 0,"
        " down_until INTEGER DEFAULT 0)")

    # §26.4 雲墨残骸 / §11 沉船遗迹
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wrecks("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " x INTEGER NOT NULL, y INTEGER NOT NULL, kind TEXT DEFAULT 'wreck',"
        " loot_json TEXT DEFAULT '{}', remaining INTEGER DEFAULT 1,"
        " expire_tick INTEGER DEFAULT 0, created_tick INTEGER DEFAULT 0)")

    # §19.15 规则 1：舰载机疲劳（返航/整备占甲板）
    qcols = {r[1] for r in conn.execute("PRAGMA table_info(squadrons)").fetchall()}
    if qcols and "fatigue" not in qcols:
        conn.execute("ALTER TABLE squadrons ADD COLUMN fatigue INTEGER DEFAULT 0")

    # §19.1 指挥官/舰长：任命带技能等级，战沉有损失
    conn.execute(
        "CREATE TABLE IF NOT EXISTS captains("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " qq TEXT NOT NULL, name TEXT NOT NULL, skill TEXT DEFAULT 'gunner',"
        " level INTEGER DEFAULT 1, exp INTEGER DEFAULT 0, ship_id INTEGER,"
        " created_tick INTEGER DEFAULT 0, UNIQUE(qq,name))")

    # §8 随机事件：生效中的事件（含事件系数 eF 的来源）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS active_events("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event TEXT NOT NULL, scope TEXT DEFAULT 'global', qq TEXT,"
        " start_tick INTEGER DEFAULT 0, end_tick INTEGER DEFAULT 0,"
        " data_json TEXT DEFAULT '{}')")

    # §6.2 外交扩展：资源贸易 / 军港租借 / 间谍行动
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trade_offers("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " from_qq TEXT NOT NULL, to_qq TEXT NOT NULL,"
        " give_res TEXT NOT NULL, give_amt REAL DEFAULT 0,"
        " want_res TEXT NOT NULL, want_amt REAL DEFAULT 0,"
        " status TEXT DEFAULT 'pending',"
        " created_at INTEGER DEFAULT 0, expire_at INTEGER DEFAULT 0)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS port_leases("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " tenant_qq TEXT NOT NULL, owner_qq TEXT NOT NULL,"
        " x INTEGER NOT NULL, y INTEGER NOT NULL,"
        " start_at INTEGER DEFAULT 0, expire_at INTEGER DEFAULT 0)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS spy_ops("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " from_qq TEXT NOT NULL, to_qq TEXT NOT NULL, op TEXT NOT NULL,"
        " success INTEGER DEFAULT 0, detail TEXT DEFAULT '',"
        " created_at INTEGER DEFAULT 0)")

    # §6.2 互不侵犯条约 / 附庸保护国（expire_at=0 表示已终止）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS treaties("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " a_qq TEXT NOT NULL, b_qq TEXT NOT NULL, kind TEXT NOT NULL,"
        " start_at INTEGER DEFAULT 0, expire_at INTEGER DEFAULT 0)")

    # §27.6 每轮双方兵力快照（Web 回放画 HP 条）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS battle_rounds("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " battle_id INTEGER NOT NULL, round_no INTEGER,"
        " a_hp REAL DEFAULT 0, a_max REAL DEFAULT 0, a_units INTEGER DEFAULT 0,"
        " b_hp REAL DEFAULT 0, b_max REAL DEFAULT 0, b_units INTEGER DEFAULT 0)")

    # P2b 潜艇三态（§19.4）：ships 补状态与蓄电池
    scols = {r[1] for r in conn.execute("PRAGMA table_info(ships)").fetchall()}
    if scols:
        for name, decl in (("sub_state", "TEXT DEFAULT 'surface'"),
                           ("sub_batt", "INTEGER"),
                           # §19.1 舰员经验（新兵/老练/王牌）
                           ("crew_exp", "INTEGER DEFAULT 0"),
                           ("crew_tier", "TEXT DEFAULT 'recruit'")):
            if name not in scols:
                conn.execute(f"ALTER TABLE ships ADD COLUMN {name} {decl}")

    cols = {r[1] for r in conn.execute("PRAGMA table_info(designs)").fetchall()}
    if not cols:
        return
    for name, decl in (("tier", "INTEGER DEFAULT 1"),
                       ("stats_json", "TEXT DEFAULT '{}'"),
                       ("cost_json", "TEXT DEFAULT '{}'"),
                       ("work_ticks", "INTEGER DEFAULT 1")):
        if name not in cols:
            conn.execute(f"ALTER TABLE designs ADD COLUMN {name} {decl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_designs_qq_name ON designs(qq,name)")
    # P2a：battles 旧表补战斗实例字段
    bcols = {r[1] for r in conn.execute("PRAGMA table_info(battles)").fetchall()}
    if bcols:
        for name, decl in (("status", "TEXT DEFAULT 'active'"),
                           ("sides_json", "TEXT DEFAULT '{}'"),
                           ("rng_seed", "INTEGER"),
                           ("end_tick", "INTEGER"),
                           ("qq", "TEXT"),
                           ("ai_fleet_id", "INTEGER")):
            if name not in bcols:
                conn.execute(f"ALTER TABLE battles ADD COLUMN {name} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    if conn.execute("SELECT 1 FROM meta WHERE key='econ_tick'").fetchone() is None:
        conn.execute("INSERT INTO meta(key,value) VALUES('econ_tick','0')")
        conn.execute("INSERT INTO meta(key,value) VALUES('war_tick','0')")
        conn.execute("INSERT INTO meta(key,value) VALUES('last_econ_ts','0')")
        conn.execute("INSERT INTO meta(key,value) VALUES('last_war_ts','0')")
    conn.commit()


def meta_get(conn, key, cast=str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return cast(row["value"]) if row else default


def meta_set(conn, key, value) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
