-- 海战模拟器 P0 建表（SQLite WAL + 稀疏地图）
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 势力（一 QQ 一全服账号）
CREATE TABLE IF NOT EXISTS players (
  qq            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  created_at    INTEGER,
  capital_x     INTEGER,
  capital_y     INTEGER,
  -- 资源
  steel         REAL DEFAULT 0,   -- 钢
  oil           REAL DEFAULT 0,   -- 油
  aluminium     REAL DEFAULT 0,   -- 铝
  rare_earth    REAL DEFAULT 0,   -- 稀土
  chips         REAL DEFAULT 0,   -- 芯片
  food          REAL DEFAULT 0,   -- 食物
  supply        REAL DEFAULT 0,   -- 补给
  manpower      REAL DEFAULT 0,   -- 人力
  money         REAL DEFAULT 0,   -- 资金
  science       REAL DEFAULT 0,   -- 科研点
  intel         REAL DEFAULT 0,   -- 情报点
  -- 状态
  infamy        INTEGER DEFAULT 0,   -- 恶名（执法者触发，§25.3）
  morale        REAL DEFAULT 80,     -- 民心
  treaty_signed INTEGER DEFAULT 1,   -- 海军条约是否生效
  treaty_used   REAL DEFAULT 0,      -- 已占用条约吨位
  settings      TEXT DEFAULT '{}',   -- 推送等偏好
  last_seen     INTEGER
);

-- 岛屿（稀疏：仅存被占领/特殊岛；key=x*1000+y，环面地图）
CREATE TABLE IF NOT EXISTS islands (
  x          INTEGER NOT NULL,
  y          INTEGER NOT NULL,
  itype      TEXT NOT NULL,           -- 岛型 id（config.island_types）
  terrain    TEXT,
  ore_json   TEXT DEFAULT '{}',       -- {"iron":4,"oil":2} 富度1~5
  dev_level  INTEGER DEFAULT 1,       -- 发展等级 D
  owner_qq   TEXT,                    -- NULL=中立
  owner_kind TEXT DEFAULT 'player',   -- player/pirate/regular/enforcer/neutral
  ai_level   INTEGER,                 -- AI 据点等级
  control    REAL DEFAULT 100,        -- 控制度
  morale     REAL DEFAULT 80,
  hp         REAL,                    -- 岛屿耐久
  created_at INTEGER,
  PRIMARY KEY (x, y)
);
CREATE INDEX IF NOT EXISTS idx_islands_owner ON islands(owner_qq);

CREATE TABLE IF NOT EXISTS buildings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  x          INTEGER NOT NULL,
  y          INTEGER NOT NULL,
  def_id     TEXT NOT NULL,
  level      INTEGER DEFAULT 1,
  hp         REAL,
  built_tick INTEGER
);
CREATE INDEX IF NOT EXISTS idx_build_island ON buildings(x, y);

-- 建造队列（工时按经济 tick 计）
CREATE TABLE IF NOT EXISTS build_queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  qq           TEXT NOT NULL,
  origin       TEXT,                  -- 完成后推送目标 unified_msg_origin
  x            INTEGER NOT NULL,
  y            INTEGER NOT NULL,
  def_id       TEXT NOT NULL,
  target_level INTEGER NOT NULL,
  start_tick   INTEGER,
  end_tick     INTEGER
);

-- 研究队列（抽卡，§19.17）
CREATE TABLE IF NOT EXISTS research_queue (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  qq         TEXT NOT NULL,
  origin     TEXT,
  unit_type  TEXT NOT NULL,           -- 舰种池
  tier       INTEGER NOT NULL,
  mode       TEXT DEFAULT 'normal',   -- normal/express
  start_ts   INTEGER,                 -- epoch 秒（研究固定5分钟）
  end_ts     INTEGER,
  pity       INTEGER DEFAULT 0        -- 该池保底计数
);
CREATE TABLE IF NOT EXISTS blueprints (
  qq          TEXT NOT NULL,
  module_id   TEXT NOT NULL,
  obtained_at INTEGER,
  PRIMARY KEY (qq, module_id)
);
CREATE TABLE IF NOT EXISTS fragments (
  qq     TEXT NOT NULL,
  tier   INTEGER NOT NULL,
  rarity TEXT NOT NULL,               -- white/blue/purple/gold
  amount INTEGER DEFAULT 0,
  PRIMARY KEY (qq, tier, rarity)
);

-- 造舰设计与实例
CREATE TABLE IF NOT EXISTS designs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  qq         TEXT NOT NULL,
  name       TEXT NOT NULL,
  ship_class TEXT NOT NULL,
  tier       INTEGER DEFAULT 1,     -- 船体 T（决定整舰代次）
  modules    TEXT DEFAULT '{}',     -- {槽位: 模块id}
  stats_json TEXT DEFAULT '{}',     -- 落定属性快照
  cost_json  TEXT DEFAULT '{}',     -- 单舰造价快照
  work_ticks INTEGER DEFAULT 1,
  UNIQUE (qq, name)
);
-- 船坞排产队列（工时按经济 tick）
CREATE TABLE IF NOT EXISTS production_queue (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  qq         TEXT NOT NULL,
  origin     TEXT,
  x          INTEGER NOT NULL,
  y          INTEGER NOT NULL,
  design_id  INTEGER NOT NULL,
  qty        INTEGER DEFAULT 1,
  start_tick INTEGER,
  end_tick   INTEGER
);
-- 各【舰种×T】池保底计数（§19.17：10连必蓝/50必紫/100必金）
CREATE TABLE IF NOT EXISTS research_pity (
  qq           TEXT NOT NULL,
  pool         TEXT NOT NULL,       -- {舰种}_t{tier}
  since_blue   INTEGER DEFAULT 0,
  since_purple INTEGER DEFAULT 0,
  since_gold   INTEGER DEFAULT 0,
  PRIMARY KEY (qq, pool)
);
CREATE TABLE IF NOT EXISTS fleets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  qq           TEXT NOT NULL,
  name         TEXT NOT NULL,
  x            INTEGER,
  y            INTEGER,
  mission      TEXT DEFAULT '{}',     -- {type,target,...}
  created_tick INTEGER
);
CREATE TABLE IF NOT EXISTS ships (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  qq        TEXT NOT NULL,
  fleet_id  INTEGER,
  def_id    TEXT NOT NULL,            -- 设计 id 或标准舰 id
  name      TEXT,
  tier      INTEGER,
  hp        REAL,
  max_hp    REAL,
  data_json TEXT DEFAULT '{}',
  FOREIGN KEY (fleet_id) REFERENCES fleets(id)
);
CREATE INDEX IF NOT EXISTS idx_ships_fleet ON ships(fleet_id);

-- 战报（P2a：status active/over，sides_json 冻结双方快照，rng_seed 可重放）
CREATE TABLE IF NOT EXISTS battles (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tick       INTEGER,
  x          INTEGER,
  y          INTEGER,
  summary    TEXT,
  detail     TEXT,
  created_at INTEGER,
  status     TEXT DEFAULT 'active',
  sides_json TEXT DEFAULT '{}',
  rng_seed   INTEGER,
  end_tick   INTEGER,
  qq         TEXT,
  ai_fleet_id INTEGER
);
-- 战斗每轮流水（简报在 battles.summary，逐轮在本表）
CREATE TABLE IF NOT EXISTS battle_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  battle_id INTEGER NOT NULL,
  war_tick  INTEGER,
  round_no  INTEGER,
  text      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bevents_battle ON battle_events(battle_id);

-- AI 舰队（海盗/正规军/中立阵营，P2a 仅海盗；编制堆叠存 comp_json）
CREATE TABLE IF NOT EXISTS ai_fleets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  faction       TEXT NOT NULL,            -- pirate/regular/...
  name          TEXT,
  level         INTEGER DEFAULT 1,
  x             INTEGER NOT NULL,
  y             INTEGER NOT NULL,
  hx            INTEGER,                  -- 据点坐标（游荡锚点/补队点）
  hy            INTEGER,
  comp_json     TEXT DEFAULT '[]',        -- [{cls,tier,qty,hp,maxhp,kind}]
  mission       TEXT DEFAULT '{}',        -- {type,target_x,target_y}
  respawn_tick  INTEGER,                  -- 被歼后何时补队（war_tick）
  created_tick  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aifleet_pos ON ai_fleets(x, y);

-- 商船/运输航线（P1 起用，P0 预留）
CREATE TABLE IF NOT EXISTS routes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  qq        TEXT NOT NULL,
  ship_id   INTEGER,
  from_x    INTEGER, from_y INTEGER,
  to_x      INTEGER, to_y   INTEGER,
  cargo     TEXT DEFAULT '{}',
  kind      TEXT DEFAULT 'resource'   -- merchant/resource
);
