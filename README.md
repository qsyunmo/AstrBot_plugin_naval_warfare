# 海战模拟器 naval_warfare

**中文** ｜ [English](#english) ｜ [Русский](#русский)

AstrBot（OneBot/NapCat）内置插件：QQ 群/私聊里的纯文字海战策略游戏。
1000×1000 环面海图、岛屿占领与发展、T1~T5 模块抽卡造舰、护航/封锁/破交、多势力 AI 与多舰队交战。

- 版本：0.1.0（P1 阶段：研究抽卡 / 舰船设计器 / 船坞排产）
- 平台：aiocqhttp（NapCat 等 OneBot v11）
- 依赖：**零第三方依赖**，SQLite 使用 Python 标准库
- 完整规则与数值：见上级目录 `../海战模拟器设计文档.md`（§0~§27）

## 玩法概览

1. `/nw注册 <势力名>` 出生在距现有首都 15~30 格的环带，roll 岛型与矿床
2. 发展岛屿：建造矿场/油井/渔场/仓库/造船厂……经济 tick 每 20 分钟自动产出
3. P1：研究所抽蓝图（白/蓝/紫/金 + 保底 + 碎片兑换）→ 设计舰船 → 船坞排产
4. P2：舰队移动与多舰队交战（六阶段结算），遭遇海盗、正规军、帝国商船、雇佣船队……
5. 终局：恶名过高招来执法者 T5 讨伐军；全服协讨 L80「雲墨の实验型舰队」搏 0.1% T10 旗舰蓝图

## 指令

前缀：`/nw`、`/海战`（也支持不带斜杠的 `nw`、`海战`，大小写不敏感，中文可紧贴如 `/nw帮助`）。
花钱类操作先给报价，60 秒内回复 `1` 确认 / `2` 取消。

| 指令 | 状态 | 说明 |
|---|---|---|
| `/nw注册 <势力名>` | P0 | 开局注册（出生后回复 1 引导 / 2 开始 / 3 重掷，重掷仅 1 次） |
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
├── main.py                 # 唯一消息入口：前缀判定→确认拦截→频率限制→分发
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 可配置项（管理员/私聊开关/免打扰时段）
├── requirements.txt        # 无第三方依赖
└── naval/
    ├── db.py               # SQLite 连接(WAL)、meta 键值、建库
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

1. `/nw注册 <faction name>` — spawn on a ring 15–30 tiles away from existing capitals; island type and ore deposits are rolled
2. Develop islands: mines / oil wells / fisheries / warehouses / shipyards… the economy tick produces automatically every 20 minutes
3. P1: the research lab draws blueprints (white / blue / purple / gold + pity + shard exchange) → design ships → dock production
4. P2: fleet movement and multi-fleet combat (six-phase resolution); encounters with pirates, regulars, imperial merchantmen, mercenary flotillas…
5. Endgame: high notoriety summons T5 enforcer punitive fleets; a server-wide raid on the L80 "雲墨の実験型艦隊" for a 0.1% T10 flagship blueprint

## Commands

Prefix: `/nw` or `/海战` (also accepted without the slash: `nw`, `海战`; case-insensitive; Chinese may be attached directly, e.g. `/nw帮助`).
Paid actions return a quote first — reply `1` within 60 seconds to confirm, `2` to cancel.

| Command | Stage | Description |
|---|---|---|
| `/nw注册 <faction name>` | P0 | Register to start (after spawning, reply 1 to guide / 2 to start / 3 to reroll; only 1 reroll) |
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

1. `/nw注册 <название фракции>` — старт: вы появляетесь на кольце в 15–30 клетках от существующих столиц; тип острова и залежи руды определяются случайно
2. Развитие островов: шахты / нефтяные вышки / рыбные промыслы / склады / верфи… экономический тик производит ресурсы автоматически каждые 20 минут
3. P1: исследовательская лаборатория тянет чертежи (белый / синий / фиолетовый / золотой + гарантия + обмен осколков) → проектирование кораблей → производство в доке
4. P2: перемещение флотов и сражения нескольких флотов (разрешение в шесть фаз); встречи с пиратами, регулярными силами, имперскими торговыми судами, наёмными флотилиями…
5. Финал: высокая дурная слава приводит карательные флоты T5; общемировой рейд на L80 «雲墨の実験型艦隊» ради 0,1% шанса на чертёж флагмана T10

## Команды

Префикс: `/nw` или `/海战` (можно и без слэша: `nw`, `海战`; регистр не важен; китайский можно писать вплотную, например `/nw帮助`).
Платные действия сначала возвращают смету — ответьте `1` в течение 60 секунд для подтверждения, `2` для отмены.

| Команда | Этап | Описание |
|---|---|---|
| `/nw注册 <название фракции>` | P0 | Регистрация для начала игры (после появления: 1 — подсказка / 2 — начать / 3 — переброс; переброс только один раз) |
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
