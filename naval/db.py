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
