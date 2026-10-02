# 海战模拟器 naval_warfare

**中文** ｜ [English](#english) ｜ [Русский](#русский)

AstrBot（OneBot/NapCat）内置插件：QQ 群/私聊里的纯文字海战策略游戏。
1000×1000 环面海图、岛屿占领与发展、T1~T5 模块抽卡造舰、护航/封锁/破交、多势力 AI 与多舰队交战。

- 版本：0.1.0（P1 阶段：研究抽卡 / 舰船设计器 / 船坞排产）
- 平台：aiocqhttp（NapCat 等 OneBot v11）
- 依赖：**零第三方依赖**，SQLite 使用 Python 标准库
- 完整规则与数值：见上级目录 `../海战模拟器设计文档.md`（§0~§27）

## 玩法概览

1. `/nw注册 <名称> [Web口令]` 出生在距现有首都 15~30 格的环带，roll 岛型与矿床
2. 发展岛屿：建造矿场/油井/渔场/仓库/造船厂……经济 tick 每 20 分钟自动产出
3. P1：研究所抽蓝图（白/蓝/紫/金 + 保底 + 碎片兑换）→ 设计舰船 → 船坞排产
4. P2：舰队移动与多舰队交战（六阶段结算），遭遇海盗、正规军、帝国商船、雇佣船队……
5. 终局：恶名过高招来执法者 T5 讨伐军；全服协讨 L80「雲墨の实验型舰队」搏 0.1% T10 旗舰蓝图

## 指令

前缀：`/nw`、`/海战`（也支持不带斜杠的 `nw`、`海战`，大小写不敏感，中文可紧贴如 `/nw帮助`）。
花钱类操作先给报价，60 秒内回复 `1` 确认 / `2` 取消。

| 指令 | 状态 | 说明 |
|---|---|---|
| `/nw注册 <名称> [Web口令]` | P0 | 开局注册；口令用于网页版登录（出生后回复 1 引导 / 2 开始 / 3 重掷，重掷仅 1 次） |
| `/nw我` `/nw资源` `/nw岛` | P0 | 势力状态卡、资源库存、岛屿详情 |
| `/nw建造 <建筑名>` | P0 | 建造/升级报价（校验资源/槽位/矿床/岛级/前置） |
| `/nw队列` | P0 | 建造/研究/船坞队列与剩余时间 |
| `/nw排行` | P0 | 势力排行榜 |
| `/nw帮助` | P0 | 指令总表 |
| `/nw研究 <舰种> <T1~T5>` | P1 | 蓝图抽卡：1 普通（5分钟，试验场每级-10%）/ 2 快捷（花费×10立即出货） |
| `/nw研究状态` | P1 | 研究位占用、倒计时、保底进度 |
| `/nw蓝图 [舰种]` | P1 | 蓝图图鉴（按槽位/稀有度/T 分组） |
| `/nw碎片` `/nw碎片兑换 <舰种> <T> <模块名>` | P1 | 重复蓝图转碎片，碎片+科研点定向兑换 |
| `/nw设计` `/nw退出` | P1 | 进入/退出舰船设计模式（30分钟无指令自动退出） |
| `/nw<舰种>` | P1 | 设计模式内列出该舰种 T1~T5 全模块编号目录（✅已拥有/🔒未研究，兼容空格与中文紧贴） |
| `/nw组装 <方案名> <模块编号…>` | P1 | 按目录编号一行配槽（兼容 `模块3`/`#3` 写法），每槽限1个，必选槽校验后报价保存 |
| `/nw设计列表` `/nw设计查看 <名>` | P1 | 设计图鉴与属性/造价/工时 |
| `/nw生产 <设计名> [数量]` | P1 | 船坞排产（船台数=造船厂等级），完工自动入列 ships 并推送 |
| `/nw船坞` | P1 | 船台占用与在造批次 |
| `/nw舰队 [名]` `/nw编队 <队> 加入/移出 <舰…>` | P2a | 无参舰队列表；有名=创建（首都港）/详情；调编在港同名舰 |
| `/nw移动 <队> <x,y>` `/nw攻击 <队> <x,y>` `/nw撤退 <队>` | P2a | 按航速自动算航程/油费报价，确认后出征；同格海盗自动开战，撤退1个战争tick脱离 |
| `/nw战报 [id]` `/nw海图` | P2a | 战报列表/逐轮详情；首都周边21×21文本海图（H首都/P海盗据点/x巡逻队/F己方舰队） |
| `/nw驻防` `/nw护航` `/nw封锁` `/nw登陆` `/nw空袭` 等 | P2b+ | 已注册指令，当前返回开放提示 |

非命令消息一律不拦截，正常走 AI 聊天；裸前缀只有后面跟已知指令时才会被认领。

## Web 版

同一套引擎、同一个数据库，另开一个浏览器入口。**游戏逻辑零改动**——Web 只是把命令从 QQ 消息换成 HTTP 请求。

### 登录方式

- **QQ 号 + Web 口令**，口令在注册时设定：`/nw注册 <名称> <口令>`
- 口令规则：至少 4 位、不能全是数字
- **老账号（注册时没写口令）**：仍可用 QQ 号登录，但会**被强制引导设置口令**，设好之前不能操作
- 账号必须已在 QQ 侧注册过，未注册的 QQ 登录会被拒

### 部署

`docker-compose.yml` 里把端口映射出去：

```yaml
  astrbot:
    ports:
      - "6185:6185"
      - "8090:8090"   # Web 版
```

插件配置（WebUI 插件页或 `_conf_schema.json`）：

| 项 | 默认 | 说明 |
|---|---|---|
| `web_enable` | `true` | 是否在插件进程内起 HTTP 服务 |
| `web_port` | `8090` | 监听端口，需与 compose 映射一致 |
| `web_host` | `0.0.0.0` | 允许外部访问；改 `127.0.0.1` 则仅本机 |

启动后在容器日志里应看到：

```
[海战模拟器][Web] 已启动 http://0.0.0.0:8090
```

> Web 服务跑在 **AstrBot 插件进程内**（同一个事件循环），因此与 QQ 侧共用
> SQLite 连接和确认/设计模式会话，不存在跨进程竞态。

### 公网访问（内网穿透）

本机没有公网 IP 时，用 cloudflared 快速隧道（免费、免账号）：

```powershell
docker run -d --name naval-tunnel --restart unless-stopped `
  cloudflare/cloudflared:latest tunnel --no-autoupdate `
  --url http://host.docker.internal:8090
docker logs naval-tunnel 2>&1 | Select-String trycloudflare
```

日志里会打印形如 `https://xxx-yyy-zzz.trycloudflare.com` 的地址，手机浏览器直接打开即可。

> ⚠️ 快速隧道的域名**每次重启都会变**；需要固定域名要改用 Cloudflare 命名隧道（需账号 + 域名）。
> ⚠️ 公网暴露前请先确认账号**已设置口令**，否则任何知道 QQ 号的人都能登录。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 网页界面（单文件，无构建步骤） |
| `POST` | `/api/login` | `{qq, password}` → `{token, need_set_password, player}` |
| `POST` | `/api/password` | 设置/修改口令（已设过需验旧口令） |
| `GET` | `/api/me` | 势力概况 + 领地 + 各类计数 |
| `POST` | `/api/cmd` | `{text}` → `{replies}`；不带前缀会自动补 `/nw` |
| `GET` | `/api/events` | 取走缓存的到期推送（建造/研究完工），取后即清 |
| `GET` | `/healthz` | 存活探针 |

鉴权用 `Authorization: Bearer <token>`（也支持 `nw_token` Cookie）。会话是内存态，进程重启后需重新登录。

### 为什么 QQ 和 Web 不会行为漂移

命令解析（前缀判定 / 确认拦截 / 频率控制 / 未知指令提示）统一在 `naval/router.py`，
QQ 侧 `main.py` 与 Web 侧 `naval/webapp.py` 调的是同一个 `Router`。
两边只有"传输层"不同：QQ 发消息、Web 返回 JSON。

## 安装与部署

把整个目录放到 AstrBot 持久化插件目录，重启即可：

```powershell
Copy-Item -Recurse "d:\代码\海战模拟器\naval_warfare" "d:\代码\astrbot\_data_snapshot\plugins\"
cd d:\代码\astrbot; docker compose restart astrbot
```

> 注意：插件目录必须在容器 `/AstrBot/data` 的实际挂载源下（本机为 `_data_snapshot/plugins/`），
> 放进未挂载的目录容器里看不到。

日志中出现以下内容即加载成功：

```
Plugin naval_warfare (0.1.0) ...
[海战模拟器] tick 引擎已启动
[海战模拟器] 插件加载成功
```

## 目录结构

```
naval_warfare/
├── main.py                 # AstrBot 入口：归一化 → Router → 发送（非命令放行给 AI）
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 可配置项（管理员/私聊开关/免打扰/Web 端口）
├── requirements.txt        # 无第三方依赖（Web 版用 AstrBot 自带的 FastAPI）
└── naval/
    ├── router.py           # 命令路由（前缀/确认/限流/分发），QQ 与 Web 共用
    ├── webapp.py           # Web 版：FastAPI 应用 + 推送缓冲 + 会话
    ├── webauth.py          # Web 口令散列(pbkdf2) 与内存态会话
    ├── webstatic/
    │   └── index.html      # 网页界面（单文件，无构建步骤）
    ├── db.py               # SQLite 连接(WAL)、meta 键值、建库与迁移
    ├── session.py          # 60 秒报价确认 / 设计模式会话(30分钟) / 频率限制（内存态）
    ├── spawn.py            # 出生岛 roll 点（环面 15~30 环带锚点采样）
    ├── pools.py            # 模块池加载、舰种/T 解析、整舰属性/造价/工时计算
    ├── research.py         # 研究所判定、抽卡（首抽/10/50/100保底/重roll）、碎片兑换、新手蓝图
    ├── engine.py           # GameEngine：研究到期扫描(10s) + 经济 tick 20min + 战争 tick 2min
    ├── game.py             # 指令表 COMMANDS/ALIASES 与全部 handler
    └── data/
        ├── config.json     # 全部平衡旋钮（tick/岛型/建筑/成本档/新手包）
        ├── modules.json    # P1 抽卡数值：研究成本/稀有度/保底/6舰种/槽位/模块原型
        ├── schema.sql      # 全部数据表
        └── naval.db        # 运行时自动生成（WAL，含 -wal/-shm）
```

## 架构约定（开发红线）

- **单消息入口**：全插件只有一个 `@filter.event_message_type(ALL, priority=2147484660)`，
  在入口内统一完成前缀判定、确认会话、频率控制、分发；非命令直接 return 不消费。
- handler 签名统一 `async def h(ctx: Ctx) -> str`，Ctx 携带 conn/cfg/confirm/origin/qq/nick/args/raw/dstore。
- 设计模式只拦截带前缀指令（`/nw<舰种>` 与 `/nw组装`），不吞普通聊天；模式状态保存在 DesignModeStore（舰种+模块编号目录快照，重新浏览即刷新）。
- 注意 GameCommands 的设计模式会话属性名为 `self.dstore`（不能用 `self.design`，会遮蔽同名 handler 方法 `design`）。
- 花钱操作 = 返回报价文本 + `confirm.put()`，由 `execute_action()` 二次确认后落地，禁止直接扣款。
- **权威状态单一来源**：阵营/通缉/合同/战争状态以数据库为准，战报文本不是状态来源。
- SQLite 同步调用（标准库），WAL 模式；tick 由 asyncio 循环驱动（默认 10 秒巡检一次时间戳）。

### Tick 规则

| tick | 周期 | P0 行为 |
|---|---|---|
| 研究扫描 | 10 秒轮询真实时间戳 | P1：普通研究到期（统一5分钟，试验场最低约3.5分钟）自动抽卡并推送 |
| 经济 tick | 20 分钟（每天 72 tick） | 建筑产出 = 日产 / 72 入账；建造完工、船坞批次完工（生成 ships）推送 |
| 战争 tick | 2 分钟 | P2a：舰队移动 → 海盗游荡/补队 → 同格接敌 → 战斗一轮结算（命中/炮/鱼雷/深弹/沉没/撤退） |

## 配置

`naval/data/config.json`（游戏数值，重启生效）：世界尺寸与种子、tick 周期、出生环带、
新手包、6 种岛型（槽位/矿床/渔场/耐久/权重）、10 种建筑（产出/工时/前置/特殊成本）、
T1~T5 成本档、矿床富度系数。

`_conf_schema.json`（WebUI 插件配置）：管理员 QQ 列表、私聊响应开关、免打扰时段。

## 开发备忘（踩过的坑）

- AstrBot 以 `data.plugins.naval_warfare` 为包根加载：main.py 导入子包要写 `from .naval.db import ...`。
- handler priority **数值越大越先执行**（框架按 -priority 排序）。
- WakingStage 会剥掉消息前导 `/`：插件收到 `/nw帮助` 时文本是 `nw帮助`，解析必须支持中文紧贴。
- 容器外无头测试：`engine.py` 对 `astrbot.api.logger` 做了 try/except 回退标准 logging。

## 路线图

- [x] P0：注册/状态/岛屿/建造报价确认/队列/排行/经济 tick（已 QQ 实测）
- [x] P1：研究抽卡（5 分钟 / 快捷×10，10/50/100 保底，碎片兑换）+ 舰船设计器向导 + 船坞排产（54 项无头冒烟通过，待 QQ 实测）
- [x] P2a：舰队编组/移动报价/海盗L1据点+巡逻队（游荡+60tick重生）/简化战斗引擎/战报/文本海图（49 项无头冒烟通过，待 QQ 实测）
- [ ] P2b+：迷雾、PvP/外交、潜艇三态、护航/封锁/登陆/空袭、正规军（§25.2）、通缉/声望/雇佣/贸易

---

# English

**naval_warfare** is an AstrBot (OneBot/NapCat) plugin: a text-only naval strategy game played in QQ group chats and direct messages.

A 1000×1000 toroidal sea map, island capture and development, T1–T5 module gacha shipbuilding, escort / blockade / commerce-raiding, multiple AI factions and multi-fleet combat.

- Version: 0.1.0 (P1 stage: research gacha / ship designer / dock scheduling)
- Platform: aiocqhttp (NapCat and other OneBot v11 implementations)
- Dependencies: **none** — SQLite comes from the Python standard library
- Full rules and numbers: `../海战模拟器设计文档.md` (Chinese only, §0–§27)

> ⚠️ **All in-game commands are Chinese.** The prefix `/nw` is Latin, but every subcommand is a Chinese word (e.g. `/nw注册`, `/nw建造`). There are no English aliases — you have to type the Chinese subcommand.
> The prefix `/海战` is the Chinese word for "naval battle"; it works the same way.

## Gameplay overview

1. `/nw注册 <name> [web password]` — spawn on a ring 15–30 tiles away from existing capitals; island type and ore deposits are rolled
2. Develop islands: mines / oil wells / fisheries / warehouses / shipyards… the economy tick produces automatically every 20 minutes
3. P1: the research lab draws blueprints (white / blue / purple / gold + pity + shard exchange) → design ships → dock production
4. P2: fleet movement and multi-fleet combat (six-phase resolution); encounters with pirates, regulars, imperial merchantmen, mercenary flotillas…
5. Endgame: high notoriety summons T5 enforcer punitive fleets; a server-wide raid on the L80 "雲墨の実験型艦隊" for a 0.1% T10 flagship blueprint

## Commands

Prefix: `/nw` or `/海战` (also accepted without the slash: `nw`, `海战`; case-insensitive; Chinese may be attached directly, e.g. `/nw帮助`).
Paid actions return a quote first — reply `1` within 60 seconds to confirm, `2` to cancel.

| Command | Stage | Description |
|---|---|---|
| `/nw注册 <name> [web password]` | P0 | Register to start. The password is used to sign in to the **web version** (after spawning: 1 guide / 2 start / 3 reroll; only 1 reroll) |
| `/nw我` `/nw资源` `/nw岛` | P0 | Faction status card, resource stockpile, island details |
| `/nw建造 <building name>` | P0 | Build / upgrade quote (validates resources, slots, ore deposit, island tier, prerequisites) |
| `/nw队列` | P0 | Build / research / dock queues and remaining time |
| `/nw排行` | P0 | Faction leaderboard |
| `/nw帮助` | P0 | Full command list |
| `/nw研究 <hull class> <T1~T5>` | P1 | Blueprint gacha: 1 normal (5 min; −10% per test-range level) / 2 instant (cost ×10, ships immediately) |
| `/nw研究状态` | P1 | Research slots in use, countdown, pity progress |
| `/nw蓝图 [hull class]` | P1 | Blueprint codex (grouped by slot / rarity / tier) |
| `/nw碎片` `/nw碎片兑换 <hull class> <T> <module name>` | P1 | Duplicate blueprints convert to shards; shards + research points for targeted exchange |
| `/nw设计` `/nw退出` | P1 | Enter / leave ship design mode (auto-exits after 30 minutes without a command) |
| `/nw<hull class>` | P1 | Inside design mode: list the T1–T5 module index for that hull class (✅ owned / 🔒 not researched; spaces or attached Chinese both work) |
| `/nw组装 <design name> <module numbers…>` | P1 | Assign modules to slots by index number, one line (accepts `模块3` / `#3`); max 1 per slot, mandatory slots validated, then quoted and saved |
| `/nw设计列表` `/nw设计查看 <name>` | P1 | Design codex and stats / cost / build time |
| `/nw生产 <design name> [qty]` | P1 | Dock production (slipways = shipyard level); on completion ships are added to `ships` automatically and pushed |
| `/nw船坞` | P1 | Slipway occupancy and batches under construction |
| `/nw舰队 [name]` `/nw编队 <fleet> 加入/移出 <ships…>` | P2a | No argument = fleet list; with a name = create (capital port) / details; assign ships of the same name in port |
| `/nw移动 <fleet> <x,y>` `/nw攻击 <fleet> <x,y>` `/nw撤退 <fleet>` | P2a | Range and fuel cost are quoted from speed; confirm to set sail. Pirates in the same tile trigger combat automatically; retreat disengages after 1 war tick |
| `/nw战报 [id]` `/nw海图` | P2a | Battle report list / round-by-round details; 21×21 text sea chart around the capital (H capital / P pirate base / x patrol / F own fleet) |
| `/nw驻防` `/nw护航` `/nw封锁` `/nw登陆` `/nw空袭` etc. | P2b+ | Registered commands; currently return a "not yet open" notice |

Non-command messages are never intercepted — they go to normal AI chat. A bare prefix is only claimed when a known command follows it.

## Web version

The same engine and the same database, exposed through a browser. **Game logic is unchanged** — the web layer only swaps the transport from QQ messages to HTTP.

- Sign in with **QQ number + web password** (set at registration: `/nw注册 <name> <password>`)
- Password rules: at least 4 characters, not all digits
- **Legacy accounts** (registered without a password) can still sign in with just the QQ number, but are **forced to set a password** before doing anything
- The account must already be registered on the QQ side

Deployment: map port `8090` in `docker-compose.yml` (see `_conf_schema.json` for `web_enable` / `web_port` / `web_host`). The HTTP service runs **inside the AstrBot plugin process**, so it shares the SQLite connection and the confirmation / design-mode sessions with the QQ side — no cross-process races.

For public access without a public IP, a cloudflared quick tunnel works with no account:

```powershell
docker run -d --name naval-tunnel --restart unless-stopped `
  cloudflare/cloudflared:latest tunnel --no-autoupdate `
  --url http://host.docker.internal:8090
```

> ⚠️ A quick-tunnel hostname changes on every restart. A stable domain needs a named Cloudflare tunnel (account + domain).
> ⚠️ Make sure the account has a password set **before** exposing it publicly.

Command parsing lives in `naval/router.py` and is shared by `main.py` (QQ) and `naval/webapp.py` (web), so the two can never drift apart.

## Installation

Drop the whole directory into AstrBot's persistent plugin directory and restart:

```powershell
Copy-Item -Recurse "d:\代码\海战模拟器\naval_warfare" "d:\代码\astrbot\_data_snapshot\plugins\"
cd d:\代码\astrbot; docker compose restart astrbot
```

> Note: the plugin directory must live under the real mount source of the container's `/AstrBot/data`
> (on this machine: `_data_snapshot/plugins/`). Anything placed in an unmounted directory is invisible to the container.

The plugin loaded successfully when the log shows:

```
Plugin naval_warfare (0.1.0) ...
[海战模拟器] tick 引擎已启动
[海战模拟器] 插件加载成功
```

---

# Русский

**naval_warfare** — плагин для AstrBot (OneBot/NapCat): текстовая морская стратегия, в которую играют в групповых чатах QQ и в личных сообщениях.

Тороидальная морская карта 1000×1000, захват и развитие островов, гача-кораблестроение из модулей T1–T5, сопровождение / блокада / рейдерство, несколько ИИ-фракций и сражения нескольких флотов.

- Версия: 0.1.0 (этап P1: гача исследований / конструктор кораблей / планирование в доке)
- Платформа: aiocqhttp (NapCat и другие реализации OneBot v11)
- Зависимости: **отсутствуют** — SQLite из стандартной библиотеки Python
- Полные правила и числа: `../海战模拟器设计文档.md` (только на китайском, §0–§27)

> ⚠️ **Все игровые команды — на китайском.** Префикс `/nw` латинский, но каждая подкоманда — китайское слово (например, `/nw注册`, `/nw建造`). Английских псевдонимов нет: подкоманду нужно вводить по-китайски.
> Префикс `/海战` — китайское слово «морское сражение»; работает так же.

## Обзор игры

1. `/nw注册 <имя> [пароль для веба]` — старт: вы появляетесь на кольце в 15–30 клетках от существующих столиц; тип острова и залежи руды определяются случайно
2. Развитие островов: шахты / нефтяные вышки / рыбные промыслы / склады / верфи… экономический тик производит ресурсы автоматически каждые 20 минут
3. P1: исследовательская лаборатория тянет чертежи (белый / синий / фиолетовый / золотой + гарантия + обмен осколков) → проектирование кораблей → производство в доке
4. P2: перемещение флотов и сражения нескольких флотов (разрешение в шесть фаз); встречи с пиратами, регулярными силами, имперскими торговыми судами, наёмными флотилиями…
5. Финал: высокая дурная слава приводит карательные флоты T5; общемировой рейд на L80 «雲墨の実験型艦隊» ради 0,1% шанса на чертёж флагмана T10

## Команды

Префикс: `/nw` или `/海战` (можно и без слэша: `nw`, `海战`; регистр не важен; китайский можно писать вплотную, например `/nw帮助`).
Платные действия сначала возвращают смету — ответьте `1` в течение 60 секунд для подтверждения, `2` для отмены.

| Команда | Этап | Описание |
|---|---|---|
| `/nw注册 <имя> [пароль для веба]` | P0 | Регистрация. Пароль используется для входа в **веб-версию** (затем: 1 — подсказка / 2 — начать / 3 — переброс; переброс только один раз) |
| `/nw我` `/nw资源` `/nw岛` | P0 | Карточка фракции, запасы ресурсов, подробности об острове |
| `/nw建造 <название постройки>` | P0 | Смета на постройку / улучшение (проверяются ресурсы, слоты, залежь, уровень острова, предварительные условия) |
| `/nw队列` | P0 | Очереди постройки / исследований / дока и оставшееся время |
| `/nw排行` | P0 | Таблица лидеров фракций |
| `/nw帮助` | P0 | Полный список команд |
| `/nw研究 <класс корабля> <T1~T5>` | P1 | Гача чертежей: 1 — обычная (5 минут; −10% за каждый уровень испытательного полигона) / 2 — быстрая (цена ×10, выдача сразу) |
| `/nw研究状态` | P1 | Занятые слоты исследований, таймер, прогресс гарантии |
| `/nw蓝图 [класс корабля]` | P1 | Каталог чертежей (с группировкой по слотам / редкости / уровню) |
| `/nw碎片` `/nw碎片兑换 <класс> <T> <модуль>` | P1 | Дубликаты чертежей превращаются в осколки; осколки + очки исследований дают адресный обмен |
| `/nw设计` `/nw退出` | P1 | Вход / выход из режима проектирования корабля (автовыход через 30 минут без команд) |
| `/nw<класс корабля>` | P1 | Внутри режима проектирования: каталог всех модулей T1–T5 для этого класса (✅ есть / 🔒 не исследовано; пробелы и слитный китайский одинаково работают) |
| `/nw组装 <имя проекта> <номера модулей…>` | P1 | Распределение модулей по слотам одной строкой по номерам (принимаются `模块3` / `#3`); не более 1 на слот, обязательные слоты проверяются, затем смета и сохранение |
| `/nw设计列表` `/nw设计查看 <имя>` | P1 | Каталог проектов и их характеристики / стоимость / время постройки |
| `/nw生产 <имя проекта> [кол-во]` | P1 | Постановка в док (число стапелей = уровень верфи); по завершении корабли автоматически попадают в `ships` и приходит уведомление |
| `/nw船坞` | P1 | Занятость стапелей и строящиеся партии |
| `/nw舰队 [имя]` `/nw编队 <флот> 加入/移出 <корабли…>` | P2a | Без аргумента — список флотов; с именем — создание (порт столицы) / подробности; назначение одноимённых кораблей в порту |
| `/nw移动 <флот> <x,y>` `/nw攻击 <флот> <x,y>` `/nw撤退 <флот>` | P2a | Дальность и расход топлива считаются по скорости, затем смета; после подтверждения — выход. Пираты в той же клетке начинают бой автоматически; отход выводит из боя через 1 военный тик |
| `/nw战报 [id]` `/nw海图` | P2a | Список боёв / подробности по раундам; текстовая карта моря 21×21 вокруг столицы (H — столица / P — база пиратов / x — патруль / F — свой флот) |
| `/nw驻防` `/nw护航` `/nw封锁` `/nw登陆` `/nw空袭` и др. | P2b+ | Команды зарегистрированы; сейчас возвращают уведомление «ещё не открыто» |

Сообщения, не являющиеся командами, никогда не перехватываются — они уходят в обычный чат с ИИ. Голый префикс распознаётся только тогда, когда за ним следует известная команда.

## Веб-версия

Тот же движок и та же база данных, но вход через браузер. **Игровая логика не менялась** — веб-слой лишь заменяет транспорт с сообщений QQ на HTTP.

- Вход по **номеру QQ + веб-паролю** (задаётся при регистрации: `/nw注册 <имя> <пароль>`)
- Правила пароля: не менее 4 символов, не только цифры
- **Старые аккаунты** (зарегистрированные без пароля) могут войти по одному номеру QQ, но **обязаны задать пароль** до начала игры
- Аккаунт должен быть заранее зарегистрирован на стороне QQ

Развёртывание: пробросьте порт `8090` в `docker-compose.yml` (настройки `web_enable` / `web_port` / `web_host` — в `_conf_schema.json`). HTTP-сервис работает **внутри процесса плагина AstrBot**, поэтому использует то же соединение SQLite и те же сессии подтверждения и режима проектирования, что и сторона QQ — гонок между процессами нет.

Для доступа извне без публичного IP подойдёт быстрый туннель cloudflared (без аккаунта):

```powershell
docker run -d --name naval-tunnel --restart unless-stopped `
  cloudflare/cloudflared:latest tunnel --no-autoupdate `
  --url http://host.docker.internal:8090
```

> ⚠️ Адрес быстрого туннеля меняется при каждом перезапуске. Для постоянного домена нужен именованный туннель Cloudflare (аккаунт + домен).
> ⚠️ Перед публикацией убедитесь, что у аккаунта уже задан пароль.

Разбор команд находится в `naval/router.py` и используется и `main.py` (QQ), и `naval/webapp.py` (веб), поэтому поведение двух сторон не может разойтись.

## Установка

Поместите каталог целиком в постоянный каталог плагинов AstrBot и перезапустите:

```powershell
Copy-Item -Recurse "d:\代码\海战模拟器\naval_warfare" "d:\代码\astrbot\_data_snapshot\plugins\"
cd d:\代码\astrbot; docker compose restart astrbot
```

> Внимание: каталог плагина должен находиться под реальным источником монтирования `/AstrBot/data`
> (на этой машине — `_data_snapshot/plugins/`). Всё, что лежит в несмонтированном каталоге, контейнеру не видно.

Плагин загружен успешно, если в логе есть:

```
Plugin naval_warfare (0.1.0) ...
[海战模拟器] tick 引擎已启动
[海战模拟器] 插件加载成功
```
