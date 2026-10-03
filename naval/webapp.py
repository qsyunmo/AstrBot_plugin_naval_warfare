"""海战模拟器 Web 版：在插件进程内起一个 FastAPI/uvicorn 服务。

复用同一套命令路由（naval/router.py）、同一个 SQLite 库和同一个 GameCommands，
游戏逻辑零改动；Web 只是换了一层传输。

鉴权：QQ 号 + 口令 → 内存态随机 token（Authorization: Bearer <token>）。
口令为空的老账号允许登录，但会被强制引导设置口令后才能操作。
"""
import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

try:
    from astrbot.api import logger
except ImportError:  # 无头测试
    import logging

    logger = logging.getLogger("naval.web")

# 必须在模块级导入：FastAPI 用 get_type_hints 解析路由签名，
# 只能看到模块全局，看不到工厂函数内的局部名（否则 request 会被当成查询参数）。
try:
    from fastapi import Body, FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:  # pragma: no cover - 无 fastapi 时仅不可用 Web
    Body = FastAPI = Request = HTMLResponse = JSONResponse = None

from . import webauth
from .engine import WEB_ORIGIN_PREFIX
from .router import CONFIRM_WORDS, Router
from . import pools

STATIC_DIR = Path(__file__).parent / "webstatic"
PUSH_KEEP = 200          # 每个玩家最多保留多少条待取推送
BODY_LIMIT = 64 * 1024

# §19.1 舰员档位的中文名（前端直接显示）
CREW_ZH = {"recruit": "新兵", "veteran": "老练", "ace": "王牌"}

# 设计图鉴要展示的舰船属性（顺序即展示顺序）
STAT_ZH = {"hp": "耐久", "fire": "火力", "torpedo": "鱼雷", "asw": "反潜",
           "aa": "防空", "speed": "航速", "detect": "探测", "hit": "命中",
           "stealth": "隐蔽", "range": "射程", "cargo": "载货", "troop": "运兵",
           "deck_hp": "甲板", "hangar": "机库", "fuel_save": "节油", "spd": "增速"}

# 图鉴里的舰种展示顺序（由小到大）
CLASS_ORDER = ("frigate", "destroyer", "light_cruiser", "ss_attack", "ss_escort",
               "cv", "transport")

logger = logging.getLogger("naval")


def _ai_name(conn, side: dict) -> str:
    """AI 方显示名：优先用舰队名（如「海盗编队」），退回阵营名。"""
    afid = side.get("ai_fleet_id")
    if afid is not None:
        r = conn.execute("SELECT name FROM ai_fleets WHERE id=?", (afid,)).fetchone()
        if r and r["name"]:
            return r["name"]
    if side.get("qq"):
        r = conn.execute("SELECT name FROM players WHERE qq=?",
                         (side["qq"],)).fetchone()
        if r and r["name"]:
            return r["name"]
        return str(side["qq"])
    return "【" + str(side.get("faction") or "AI") + "】"


class WebPushBuffer:
    """把发给 Web 用户的推送攒起来，等网页轮询取走（进程内存态）。"""

    def __init__(self, maxlen: int = PUSH_KEEP):
        self.maxlen = maxlen
        self._buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._lock = asyncio.Lock()

    @staticmethod
    def _qq_of(origin: str) -> str:
        return str(origin).split(":", 1)[1] if ":" in str(origin) else str(origin)

    def add(self, origin: str, text: str) -> None:
        self._buf[self._qq_of(origin)].append({"ts": int(time.time()), "text": text})

    def drain(self, qq: str) -> list:
        q = self._buf.get(str(qq))
        if not q:
            return []
        out = list(q)
        q.clear()
        return out


def _json_default(o):
    return str(o)


class NavalWeb:
    """Web 服务宿主。start() 在当前事件循环里跑 uvicorn，不额外开线程，
    这样 SQLite 连接与内存态会话（confirm/design）只在单线程里被访问，避免竞态。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.conn = plugin.conn
        self.cfg = plugin.cfg
        self.confirm = plugin.confirm
        self.design = plugin.design
        self.cmds = plugin.cmds
        self.push_buffer = plugin.push_buffer
        self.sessions = webauth.SessionStore()

        # Web 侧独立路由：不套用 QQ 的频率限制（网页是点击驱动，不是刷屏）
        self.router = Router(self.conn, self.cfg, self.confirm, self.design,
                             ratelimit=None, on_error=self._on_error)
        self.router.cmds = self.cmds

        self.app = self._build_app()
        self._server = None
        self._task = None
        self._thread = None

    @staticmethod
    def _on_error(label: str, exc: Exception):
        logger.exception(f"[海战模拟器][Web] {label}: {exc}")

    # ---------- 数据 ----------
    def _player(self, qq: str):
        return self.conn.execute("SELECT * FROM players WHERE qq=?", (str(qq),)).fetchone()

    def _player_public(self, qq: str) -> dict:
        p = self._player(qq)
        if not p:
            return {}
        keys = ("qq", "name", "capital_x", "capital_y", "infamy", "morale",
                "steel", "oil", "aluminium", "rare_earth", "chips", "food",
                "supply", "manpower", "money", "science", "intel",
                # 后续版本新增：通缉热度/税率/航线安全/中立状态
                "wanted_heat", "tax_rate", "route_security",
                "is_neutral", "neutral_since")
        return {k: p[k] for k in keys if k in p.keys()}

    def _islands(self, qq: str) -> list:
        rows = self.conn.execute(
            "SELECT x,y,itype,dev_level,ore_json,control,morale,hp FROM islands"
            " WHERE owner_qq=? ORDER BY x,y", (str(qq),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["ore"] = json.loads(d.pop("ore_json") or "{}")
            except Exception:
                d["ore"] = {}
            out.append(d)
        return out

    def _counts(self, qq: str) -> dict:
        q = (str(qq),)
        one = lambda sql: self.conn.execute(sql, q).fetchone()[0]  # noqa: E731
        return {
            "buildings": one("SELECT COUNT(*) FROM buildings b JOIN islands i"
                             " ON i.x=b.x AND i.y=b.y WHERE i.owner_qq=?"),
            "designs": one("SELECT COUNT(*) FROM designs WHERE qq=?"),
            "blueprints": one("SELECT COUNT(*) FROM blueprints WHERE qq=?"),
            "ships": one("SELECT COUNT(*) FROM ships WHERE qq=?"),
            "fleets": one("SELECT COUNT(*) FROM fleets WHERE qq=?"),
            "build_queue": one("SELECT COUNT(*) FROM build_queue WHERE qq=?"),
            "research_queue": one("SELECT COUNT(*) FROM research_queue WHERE qq=?"),
            "production_queue": one("SELECT COUNT(*) FROM production_queue WHERE qq=?"),
        }

    # ---------- 鉴权 ----------
    def _qq_from_request(self, request):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        if not token:
            token = request.cookies.get("nw_token")
        return self.sessions.get(token)

    # ---------- 路由 ----------
    def _build_app(self):
        app = FastAPI(title="海战模拟器 Web", docs_url=None, redoc_url=None)

        def err(msg: str, code: int = 400):
            return JSONResponse({"ok": False, "error": msg}, status_code=code)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse((STATIC_DIR / "index.html").read_text("utf-8"))

        @app.get("/healthz")
        async def healthz():
            return {"ok": True, "players": self.conn.execute(
                "SELECT COUNT(*) FROM players").fetchone()[0]}

        @app.post("/api/login")
        async def login(payload: dict = Body(...)):
            qq = str(payload.get("qq", "")).strip()
            password = str(payload.get("password", ""))
            if not webauth.is_valid_qq(qq):
                return err("QQ 号格式不对（5~12 位数字）")
            p = self._player(qq)
            if not p:
                return err("该 QQ 尚未注册。请先在 QQ 里发：/nw注册 <名称> <密码>")
            stored = p["web_pass"] or ""
            if stored:
                if not webauth.verify_password(password, stored):
                    return err("口令不正确", 401)
                need_set = False
            else:
                # 老账号没有 Web 口令：放行，但强制引导设置
                need_set = True
            token = self.sessions.create(qq)
            resp = JSONResponse({
                "ok": True, "token": token, "qq": qq,
                "need_set_password": need_set,
                "player": self._player_public(qq),
            })
            resp.set_cookie("nw_token", token, httponly=True, samesite="lax",
                            max_age=self.sessions.ttl)
            return resp

        @app.post("/api/logout")
        async def logout(request: Request):
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
            self.sessions.drop(token or request.cookies.get("nw_token"))
            resp = JSONResponse({"ok": True})
            resp.delete_cookie("nw_token")
            return resp

        @app.get("/api/me")
        async def me(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            p = self._player(qq)
            if not p:
                return err("账号不存在", 404)
            return {
                "ok": True, "qq": qq,
                "need_set_password": not (p["web_pass"] or ""),
                "player": self._player_public(qq),
                "islands": self._islands(qq),
                "counts": self._counts(qq),
            }

        @app.post("/api/password")
        async def set_password(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            p = self._player(qq)
            if not p:
                return err("账号不存在", 404)
            old = str(payload.get("old_password", ""))
            new = str(payload.get("new_password", ""))
            stored = p["web_pass"] or ""
            # 已设过口令的账号改口令必须先验证旧口令
            if stored and not webauth.verify_password(old, stored):
                return err("原口令不正确", 401)
            strength = webauth.check_password_strength(new)
            if strength:
                return err(strength)
            self.conn.execute("UPDATE players SET web_pass=?, web_pass_at=? WHERE qq=?",
                              (webauth.hash_password(new), int(time.time()), qq))
            self.conn.commit()
            self.sessions.drop_qq(qq)          # 改口令后其它会话失效
            token = self.sessions.create(qq)   # 当前会话续上
            resp = JSONResponse({"ok": True, "token": token, "message": "口令已设置"})
            resp.set_cookie("nw_token", token, httponly=True, samesite="lax",
                            max_age=self.sessions.ttl)
            return resp

        @app.post("/api/cmd")
        async def cmd(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            p = self._player(qq)
            if not p:
                return err("账号不存在", 404)
            text = str(payload.get("text", "")).strip()
            if not text:
                return err("命令为空")
            if not (p["web_pass"] or ""):
                return err("请先设置 Web 口令", 403)

            # 网页是点击/短输入驱动：没写前缀就自动补，确认词原样放行
            if text not in CONFIRM_WORDS and not self._has_prefix(text):
                text = "/nw" + text
            nick = p["name"] or qq
            origin = f"{WEB_ORIGIN_PREFIX}{qq}"
            replies, consumed = await self.router.handle(origin, qq, nick, text)
            if not consumed:
                return {"ok": True, "replies": ["❓ 没听懂这条指令，发「帮助」看看能做什么。"],
                        "player": self._player_public(qq)}
            return {"ok": True, "replies": replies, "player": self._player_public(qq),
                    "counts": self._counts(qq)}

        @app.get("/api/events")
        async def events(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            return {"ok": True, "events": self.push_buffer.drain(qq)}

        # ---------- 图形化内政：建造 / 加急（不敲指令也能玩） ----------
        @app.get("/api/build_options")
        async def build_options(request: Request, x: int = None, y: int = None):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            d = _gov.build_options(self.conn, self.cfg, qq, x, y)
            if not d.get("ok"):
                return err(d.get("reason", "查询失败"), 404)
            return d

        @app.get("/api/queues")
        async def queues(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            return _gov.queue_snapshot(self.conn, self.cfg, qq)

        @app.post("/api/build")
        async def build(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            def_id = str(payload.get("def_id", ""))
            x, y = payload.get("x"), payload.get("y")
            rush = bool(payload.get("rush"))
            try:
                ok, text = _gov.do_build(self.conn, self.cfg, qq,
                                         f"web:{qq}", def_id, x, y, rush)
            except Exception as e:
                logger.exception("[海战模拟器][Web] 建造异常")
                return err(f"建造失败：{type(e).__name__}", 500)
            if not ok:
                return err(text.lstrip("❌ "))
            return {"ok": True, "text": text,
                    "options": _gov.build_options(self.conn, self.cfg, qq, x, y),
                    "queues": _gov.queue_snapshot(self.conn, self.cfg, qq)}

        @app.post("/api/rush")
        async def rush(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            kind = str(payload.get("kind", ""))
            try:
                qid = int(payload.get("id"))
            except (TypeError, ValueError):
                return err("id 应为数字")
            try:
                ok, text = _gov.rush_queue(self.conn, self.cfg, qq, kind, qid)
            except Exception as e:
                logger.exception("[海战模拟器][Web] 加急异常")
                return err(f"加急失败：{type(e).__name__}", 500)
            if not ok:
                return err(text.lstrip("❌ "))
            return {"ok": True, "text": text,
                    "queues": _gov.queue_snapshot(self.conn, self.cfg, qq)}

        @app.get("/api/research_options")
        async def research_options(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            d = _gov.research_options(self.conn, self.cfg, qq)
            if not d.get("ok"):
                return err(d.get("reason", "查询失败"), 404)
            return d

        @app.post("/api/research")
        async def research_start(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            cls = str(payload.get("cls", ""))
            mode = str(payload.get("mode", "normal"))
            try:
                tier = int(payload.get("tier"))
            except (TypeError, ValueError):
                return err("tier 应为数字")
            if mode not in ("normal", "express"):
                return err("mode 应为 normal 或 express")
            try:
                ok, text = _gov.do_research(self.conn, self.cfg, qq,
                                            f"web:{qq}", cls, tier, mode)
            except Exception as e:
                logger.exception("[海战模拟器][Web] 研究异常")
                return err(f"研究失败：{type(e).__name__}", 500)
            if not ok:
                return err(text.lstrip("❌ "))
            return {"ok": True, "text": text,
                    "options": _gov.research_options(self.conn, self.cfg, qq),
                    "queues": _gov.queue_snapshot(self.conn, self.cfg, qq)}

        @app.get("/api/design_options")
        async def design_options(request: Request, cls: str = None):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            d = _gov.design_options(self.conn, self.cfg, qq, cls)
            if not d.get("ok"):
                return err(d.get("reason", "查询失败"), 404)
            return d

        @app.post("/api/design_preview")
        async def design_preview(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            return _gov.preview_design(self.cfg, str(payload.get("cls", "")),
                                       payload.get("modules") or {})

        @app.post("/api/design_save")
        async def design_save(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import gov as _gov

            try:
                ok, text = _gov.save_design(
                    self.conn, self.cfg, qq, str(payload.get("name", "")),
                    str(payload.get("cls", "")), payload.get("modules") or {})
            except Exception as e:
                logger.exception("[海战模拟器][Web] 保存设计异常")
                return err(f"保存失败：{type(e).__name__}", 500)
            if not ok:
                return err(text.lstrip("❌ "))
            return {"ok": True, "text": text,
                    "options": _gov.design_options(
                        self.conn, self.cfg, qq, str(payload.get("cls", "")))}

        @app.get("/api/sandbox")
        async def sandbox_status(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import sandbox as _sb

            st = _sb.status(self.conn, self.cfg, qq)
            if not st.get("ok"):
                return err(st.get("reason", "查询失败"), 404)
            return st

        @app.post("/api/sandbox")
        async def sandbox_apply(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import sandbox as _sb

            what = str(payload.get("what", "all"))
            try:
                ok, text = _sb.apply(self.conn, self.cfg, qq, what)
            except Exception as e:
                logger.exception("[海战模拟器][Web] 沙盒操作异常")
                return err(f"沙盒操作失败：{type(e).__name__}", 500)
            if not ok:
                return err(text.lstrip("❌ "), 403)
            return {"ok": True, "text": text,
                    "status": _sb.status(self.conn, self.cfg, qq)}

        @app.get("/api/capital")
        async def capital_status(request: Request):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import capital as _cap

            return _cap.status(self.conn, self.cfg, qq)

        @app.post("/api/capital")
        async def capital_act(request: Request, payload: dict = Body(...)):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            from . import capital as _cap
            from . import research as _rs

            what = str(payload.get("what", "")).lower()
            if what in ("upgrade", "升级", "扩编"):
                ok, text = _cap.upgrade_quota(self.conn, self.cfg, qq)
            elif what in ("apply", "申请"):
                academy = max([_rs.building_level(self.conn, r["x"], r["y"],
                                                  "naval_academy")
                               for r in self.conn.execute(
                                   "SELECT x,y FROM islands WHERE owner_qq=?",
                                   (qq,)).fetchall()] or [0])
                ok, text = _cap.apply_permit(self.conn, self.cfg, qq, academy)
            else:
                return err("what 应为 apply 或 upgrade")
            if not ok:
                return err(text.lstrip("❌ "))
            return {"ok": True, "text": text,
                    "status": _cap.status(self.conn, self.cfg, qq)}

        # ---------- 图形化数据 ----------
        @app.get("/api/map")
        async def map_data(request: Request, radius: int = 10,
                           cx: int = None, cy: int = None):
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            d = self._chart_data(qq, max(3, min(30, radius)), (cx, cy))
            if not d:
                return err("尚未注册势力", 404)
            return {"ok": True, **d}

        @app.get("/api/assets")
        async def assets(request: Request):
            """舰队 / 舰船 / 设计 / 蓝图 / 队列 —— 给图形化面板用的一次性快照。"""
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            return {"ok": True,
                    "fleets": self._fleets(qq),
                    "ships": self._ships(qq),
                    "designs": self._designs(qq),
                    "buildings": self._buildings(qq),
                    "queues": self._queues(qq),
                    # 后续版本新增
                    "captains": self._captains(qq),
                    "power": self._power(qq),
                    "events": self._events(qq),
                    "research": self._research(qq),
                    "diplomacy": self._diplomacy(qq)}

        @app.get("/api/battles")
        async def battles(request: Request, limit: int = 20):
            """§27.6 与我有关的战斗列表（供战报回放选场）。"""
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            return {"ok": True, "battles": self._my_battles(qq, limit)}

        @app.get("/api/battle/{bid}")
        async def battle_detail(request: Request, bid: int):
            """§27.6 一场战斗的完整回放数据（逐轮流水 + 功勋表）。"""
            qq = self._qq_from_request(request)
            if not qq:
                return err("未登录", 401)
            d = self._battle_detail(qq, bid)
            if not d:
                return err("找不到该战斗，或你不是参战方", 404)
            return {"ok": True, **d}

        return app

    # ---------- 图形化数据组装 ----------
    def _chart_data(self, qq: str, radius: int, center: tuple = None):
        from .game import chart_data

        return chart_data(self.conn, self.cfg, qq, radius, center)

    def _fleets(self, qq: str) -> list:
        from . import fleet as fleetm

        out = []
        for f in fleetm.list_fleets(self.conn, qq):
            try:
                mission = json.loads(f["mission"] or "{}")
            except Exception:
                mission = {}
            rows = self.conn.execute(
                "SELECT id,name,tier,hp,max_hp FROM ships WHERE fleet_id=?", (f["id"],)).fetchall()
            out.append({
                "id": f["id"], "name": f["name"], "x": f["x"], "y": f["y"],
                "mission": mission,
                "speed": fleetm.fleet_speed(self.conn, f["id"]) if hasattr(fleetm, "fleet_speed") else None,
                "ships": [dict(r) for r in rows],
            })
        return out

    def _ships(self, qq: str) -> list:
        rows = self.conn.execute(
            "SELECT id,name,tier,hp,max_hp,fleet_id,sub_state,sub_batt,def_id,"
            "crew_exp,crew_tier FROM ships WHERE qq=? ORDER BY id", (qq,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # 潜艇才需要展示三态；顺带把舰级带给前端做图标
            cls = None
            if str(d.get("def_id") or "").isdigit():
                row = self.conn.execute("SELECT ship_class FROM designs WHERE id=?",
                                        (int(d["def_id"]),)).fetchone()
                cls = row["ship_class"] if row else None
            d["cls"] = cls
            # §19.1 舰员档位中文名，前端直接用
            d["crew_label"] = CREW_ZH.get(str(d.get("crew_tier") or "recruit"), "")
            if not str(cls or "").startswith("ss"):
                d.pop("sub_state", None)
                d.pop("sub_batt", None)
            d.pop("def_id", None)
            out.append(d)
        return out

    # ---------- §6.2 外交 / §19.1 指挥官 / §8 事件 / §18.5 电力 ----------
    def _captains(self, qq: str) -> list:
        rows = self.conn.execute(
            "SELECT id,name,skill,level,exp,ship_id FROM captains WHERE qq=?"
            " ORDER BY level DESC, id", (qq,)).fetchall()
        skills = (self.cfg.get("captains") or {}).get("skills") or {}
        out = []
        for r in rows:
            d = dict(r)
            spec = skills.get(d["skill"]) or {}
            d["skill_name"] = spec.get("name", d["skill"])
            d["stat"] = spec.get("stat")
            d["per_level"] = spec.get("per_level", 0)
            if d.get("ship_id"):
                s = self.conn.execute("SELECT name FROM ships WHERE id=?",
                                      (d["ship_id"],)).fetchone()
                d["ship_name"] = s["name"] if s else None
            out.append(d)
        return out

    def _diplomacy(self, qq: str) -> dict:
        """§6.2 外交面板数据：阵营关系 + 玩家关系 + 条约 + 中立 + 租借港。"""
        from . import diplomacy as dip
        from . import relations as rel
        out = {"factions": [], "players": [], "treaties": [], "leases": [],
               "neutral": False, "liege": None, "vassals": [], "market": []}
        try:
            for r in rel.snapshot(self.conn, self.cfg, qq):
                out["factions"].append({
                    "name": r["name"], "state": r["state"],
                    "state_zh": rel.STATE_ZH.get(r["state"], r["state"]),
                    "explicit": bool(r.get("explicit")),
                    "reputation": r.get("reputation"),
                })
        except Exception:
            logger.exception("[海战模拟器] web 外交快照异常")
        for v in rel.players_snapshot(self.conn, self.cfg, qq):
            out["players"].append({
                "qq": v["qq"], "name": v["name"], "state": v["state"],
                "state_zh": rel.STATE_ZH.get(v["state"], v["state"]),
                "neutral": rel.is_neutral(self.conn, v["qq"]),
            })
        try:
            now = int(time.time())
            for t in self.conn.execute(
                    "SELECT * FROM treaties WHERE expire_at>? AND (a_qq=? OR b_qq=?)",
                    (now, qq, qq)).fetchall():
                other = t["b_qq"] if t["a_qq"] == qq else t["a_qq"]
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (other,)).fetchone()
                out["treaties"].append({
                    "kind": t["kind"], "other": other,
                    "other_name": nm["name"] if nm else other,
                    "days_left": max(0, (t["expire_at"] - now) // 86400),
                    "role": ("宗主" if t["a_qq"] == qq else "附庸")
                            if t["kind"] == rel.VASSAL else "",
                })
            for l in dip.active_leases(self.conn, qq):
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (l["owner_qq"],)).fetchone()
                out["leases"].append({"x": l["x"], "y": l["y"],
                                      "owner": nm["name"] if nm else l["owner_qq"],
                                      "days_left": max(0, (l["expire_at"] - now) // 86400)})
            out["neutral"] = rel.is_neutral(self.conn, qq)
            out["liege"] = dip.liege_of(self.conn, self.cfg, qq)
            out["vassals"] = dip.vassals_of(self.conn, self.cfg, qq)
            # §6.2 市场公开挂单（别人的 + 自己挂的都列，标出归属）
            now2 = int(time.time())
            for m in self.conn.execute(
                    "SELECT * FROM trade_offers WHERE to_qq='' AND status='pending'"
                    " AND expire_at>? ORDER BY id DESC LIMIT 20",
                    (now2,)).fetchall():
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (m["from_qq"],)).fetchone()
                out["market"].append({
                    "id": m["id"], "from_qq": m["from_qq"],
                    "from_name": nm["name"] if nm else m["from_qq"],
                    "mine": m["from_qq"] == qq,
                    "give_res": m["give_res"], "give_amt": m["give_amt"],
                    "want_res": m["want_res"], "want_amt": m["want_amt"],
                    "at_war": rel.get_pvp_state(self.conn, self.cfg, qq,
                                                m["from_qq"]) == rel.WAR,
                })
        except Exception:
            logger.exception("[海战模拟器] web 条约/租借快照异常")
        return out

    def _events(self, qq: str) -> list:
        """§8 当前生效的随机事件（个人事件只显示自己的）。"""
        from . import events as ev
        tick = self.conn.execute(
            "SELECT value FROM meta WHERE key='econ_tick'").fetchone()
        try:
            tick_v = int(tick["value"]) if tick else 0
        except (TypeError, ValueError):
            tick_v = 0
        out = []
        for r in ev.active_events(self.conn, tick_v):
            if r["scope"] == "player" and r["qq"] and r["qq"] != qq:
                continue
            spec = (ev.catalog(self.cfg).get(r["event"]) or {})
            out.append({"event": r["event"], "name": spec.get("name", r["event"]),
                        "desc": spec.get("desc", ""), "scope": r["scope"],
                        "eF": spec.get("eF") or {}})
        return out

    def _power(self, qq: str) -> list:
        """§18.5 每座己方岛屿的电网供需。"""
        from .engine import island_power
        rows = self.conn.execute(
            "SELECT x,y FROM islands WHERE owner_qq=? ORDER BY x,y", (qq,)).fetchall()
        out = []
        for r in rows:
            pw = island_power(self.conn, self.cfg, r["x"], r["y"])
            out.append({"x": r["x"], "y": r["y"], "supply": pw["supply"],
                        "demand": pw["demand"], "shed": len(pw["shed"]),
                        "half": len(pw["half"]), "oil": pw["oil"]})
        return out

    def _designs(self, qq: str) -> list:
        rows = self.conn.execute(
            "SELECT id,name,ship_class,tier,modules,stats_json,cost_json,work_ticks"
            " FROM designs WHERE qq=? ORDER BY ship_class,tier,id", (qq,)).fetchall()
        try:
            mod_data = pools.mod_data()
        except Exception:
            mod_data = {}
        classes = mod_data.get("classes") or {}
        slot_names = mod_data.get("slot_names") or {}
        # 属性展示顺序与中文名（图鉴用）
        stat_zh = STAT_ZH
        out = []
        for r in rows:
            d = dict(r)
            mods = {}
            try:
                mods = json.loads(d.pop("modules") or "{}")
            except (TypeError, ValueError):
                mods = {}
            for k in ("stats_json", "cost_json"):
                try:
                    d[k[:-5]] = json.loads(d.pop(k) or "{}")
                except (TypeError, ValueError):
                    d[k[:-5]] = {}
            d["class_name"] = (classes.get(d["ship_class"]) or {}).get(
                "name", d["ship_class"])
            # 装了什么模块（图鉴展开时显示）
            parts = []
            for slot, mid in (mods or {}).items():
                if not mid:
                    continue
                try:
                    info = pools.module_info(mid) or {}
                except Exception:
                    info = {}
                parts.append({
                    "slot": slot,
                    "slot_name": slot_names.get(slot, slot),
                    "name": info.get("name") or mid,
                })
            d["modules"] = parts
            # 航母额外带机库/甲板/单波（§19.15）
            if str(d["ship_class"]) == "cv":
                try:
                    from . import air
                    d["cv"] = air.cv_spec(self.cfg, int(d["tier"] or 1))
                except Exception:
                    d["cv"] = None
            # 简单的综合战力评分：便于图鉴里横向比较
            st = d.get("stats") or {}
            d["power"] = round(
                float(st.get("hp", 0) or 0) * 0.10
                + float(st.get("fire", 0) or 0) * 1.0
                + float(st.get("torpedo", 0) or 0) * 1.2
                + float(st.get("asw", 0) or 0) * 1.0
                + float(st.get("aa", 0) or 0) * 0.8
                + float(st.get("speed", 0) or 0) * 2.0
                + float(st.get("detect", 0) or 0) * 1.5, 1)
            d["stats_zh"] = {stat_zh.get(k, k): v
                             for k, v in (d.get("stats") or {}).items()
                             if v and k in stat_zh}
            out.append(d)
        # 图鉴按「舰种顺序 → 代际」排列（字母序会把航母排到最前，不直观）
        order = {c: i for i, c in enumerate(CLASS_ORDER)}
        out.sort(key=lambda x: (order.get(x["ship_class"], 99),
                                int(x["tier"] or 1), x["id"]))
        return out

    def _buildings(self, qq: str) -> list:
        rows = self.conn.execute(
            "SELECT b.x,b.y,b.def_id,b.level FROM buildings b JOIN islands i"
            " ON i.x=b.x AND i.y=b.y WHERE i.owner_qq=? ORDER BY b.x,b.y", (qq,)).fetchall()
        names = (self.cfg.get("buildings") or {})
        out = []
        for r in rows:
            d = dict(r)
            d["name"] = (names.get(d["def_id"]) or {}).get("name", d["def_id"])
            out.append(d)
        return out

    # ---------- §27.6 战报回放 ----------
    def _my_battles(self, qq: str, limit: int = 20) -> list:
        """与我有关的战斗列表（A 方，或 PvP 里的 B 方）。"""
        q = str(qq)
        rows = self.conn.execute(
            "SELECT id,tick,end_tick,x,y,status,summary,detail,sides_json,qq,"
            "ai_fleet_id,rng_seed FROM battles ORDER BY id DESC LIMIT ?",
            (max(1, min(100, limit)),)).fetchall()
        out = []
        for r in rows:
            try:
                sides = json.loads(r["sides_json"] or "{}")
            except (TypeError, ValueError):
                continue
            a, b = sides.get("A") or {}, sides.get("B") or {}
            mine = q in (a.get("qq"), b.get("qq"))
            if not mine:
                continue
            opp = b if a.get("qq") == q else a
            opp_name = _ai_name(self.conn, opp)
            if opp.get("qq"):
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (opp["qq"],)).fetchone()
                if nm:
                    opp_name = nm["name"]
            nround = self.conn.execute(
                "SELECT COUNT(DISTINCT round_no) c FROM battle_events WHERE battle_id=?",
                (r["id"],)).fetchone()["c"]
            out.append({
                "id": r["id"], "tick": r["tick"], "x": r["x"], "y": r["y"],
                "status": r["status"], "opponent": opp_name,
                "rounds": nround, "detail": r["detail"],
                "won": "胜利" in (r["summary"] or ""),
                "lost": "全军覆没" in (r["summary"] or ""),
            })
        return out

    def _battle_detail(self, qq: str, bid: int) -> dict:
        """一场战斗的完整回放数据：逐轮事件 + 功勋表。"""
        q = str(qq)
        r = self.conn.execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
        if not r:
            return {}
        try:
            sides = json.loads(r["sides_json"] or "{}")
        except (TypeError, ValueError):
            sides = {}
        a, b = sides.get("A") or {}, sides.get("B") or {}
        if q not in (a.get("qq"), b.get("qq")):
            return {}          # 只允许当事人回放自己的战斗

        def _side_name(sd):
            if sd.get("qq"):
                nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                       (sd["qq"],)).fetchone()
                return nm["name"] if nm else str(sd["qq"])
            return _ai_name(self.conn, sd)

        evs = self.conn.execute(
            "SELECT round_no,text FROM battle_events WHERE battle_id=? ORDER BY id",
            (bid,)).fetchall()
        rounds = {}
        for e in evs:
            rounds.setdefault(int(e["round_no"] or 0), []).append(e["text"])
        # §27.6 每轮兵力快照（HP 条用）
        snaps = {}
        for s in self.conn.execute(
                "SELECT * FROM battle_rounds WHERE battle_id=? ORDER BY round_no,id",
                (bid,)).fetchall():
            snaps[int(s["round_no"] or 0)] = {
                "a_hp": s["a_hp"], "a_max": s["a_max"], "a_units": s["a_units"],
                "b_hp": s["b_hp"], "b_max": s["b_max"], "b_units": s["b_units"],
            }
        round_list = []
        for k in sorted(rounds):
            round_list.append({"no": k, "lines": rounds[k],
                               "snap": snaps.get(k)})
        dmg = []
        for d in self.conn.execute(
                "SELECT qq,damage,kills,lost FROM battle_damage WHERE battle_id=?"
                " ORDER BY damage DESC", (bid,)).fetchall():
            nm = self.conn.execute("SELECT name FROM players WHERE qq=?",
                                   (d["qq"],)).fetchone()
            dmg.append({"qq": d["qq"], "name": nm["name"] if nm else d["qq"],
                        "damage": d["damage"], "kills": d["kills"], "lost": d["lost"]})
        total = sum(x["damage"] or 0 for x in dmg) or 0
        for x in dmg:
            x["share"] = (x["damage"] / total) if total else 0
        return {
            "id": r["id"], "x": r["x"], "y": r["y"], "status": r["status"],
            "detail": r["detail"], "summary": r["summary"] or "",
            "my_side": "A" if a.get("qq") == q else "B",
            "a_name": _side_name(a), "b_name": _side_name(b),
            "rounds": round_list,
            "damage": dmg,
        }

    # ---------- 科技树（§19.17 蓝图抽卡 + 研究所等级） ----------
    def _research(self, qq: str) -> dict:
        from . import research as rs
        try:
            md = pools.mod_data()
        except Exception:
            logger.exception("[海战模拟器] 读取模块数据失败")
            md = {}
        classes = md.get("classes") or {}
        arche = md.get("archetypes") or {}
        rarity_cfg = md.get("rarity") or {}
        tier_mults = md.get("tier_stat_mult") or []
        pity_cfg = md.get("pity") or {}
        info = rs.lab_info(self.conn, qq)
        lab_lv = info[0] if info else 0
        proving_lv = info[1] if info else 0
        slots = rs.research_slots(lab_lv)

        owned = {r["module_id"] for r in self.conn.execute(
            "SELECT module_id FROM blueprints WHERE qq=?", (qq,)).fetchall()}
        # 每个 module_id 形如 cls_t{tier}_{key}
        owned_by = {}
        for mid in owned:
            mi = pools.module_info(mid)
            if not mi:
                continue
            k = (mi.get("cls"), int(mi.get("tier") or 1))
            owned_by[k] = owned_by.get(k, 0) + 1

        # 代际总览：研究所等级 = 可研究池上限（Lv1=T1 … Lv5=T5）
        tiers = []
        for t in range(1, 6):
            total = sum(len(arche.get(c) or []) for c in classes)
            got = sum(v for (c, tt), v in owned_by.items() if tt == t)
            tiers.append({
                "tier": t,
                "unlocked": lab_lv >= t,
                "mult": tier_mults[t - 1] if t - 1 < len(tier_mults) else None,
                "owned": got,
                "total": total,
            })

        # 按舰种 × 代际的收集度
        grid = []
        for c, spec in classes.items():
            cells = []
            for t in range(1, 6):
                tot = len(arche.get(c) or [])
                cells.append({"tier": t, "owned": owned_by.get((c, t), 0),
                              "total": tot})
            grid.append({"cls": c, "name": spec.get("name", c), "cells": cells,
                         "slots": spec.get("slots") or []})

        frags = []
        for r in self.conn.execute(
                "SELECT tier,rarity,amount FROM fragments WHERE qq=? ORDER BY tier",
                (qq,)).fetchall():
            rc = rarity_cfg.get(r["rarity"]) or {}
            frags.append({"tier": r["tier"], "rarity": r["rarity"],
                          "rarity_name": rc.get("name", r["rarity"]),
                          "amount": r["amount"],
                          "exchange": rc.get("exchange"),
                          "enough": int(r["amount"] or 0) >= int(rc.get("exchange") or 0)})

        pity = []
        for r in self.conn.execute(
                "SELECT * FROM research_pity WHERE qq=?", (qq,)).fetchall():
            pity.append({"pool": r["pool"], "since_blue": r["since_blue"],
                         "since_purple": r["since_purple"],
                         "since_gold": r["since_gold"],
                         "need_blue": pity_cfg.get("blue"),
                         "need_purple": pity_cfg.get("purple"),
                         "need_gold": pity_cfg.get("gold")})

        queue = []
        now = int(time.time())
        for r in self.conn.execute(
                "SELECT * FROM research_queue WHERE qq=? ORDER BY end_ts", (qq,)).fetchall():
            queue.append({"unit_type": r["unit_type"], "tier": r["tier"],
                          "mode": r["mode"],
                          "left": max(0, int(r["end_ts"] or 0) - now)})

        recent = []
        for r in self.conn.execute(
                "SELECT module_id,obtained_at FROM blueprints WHERE qq=?"
                " ORDER BY obtained_at DESC LIMIT 12", (qq,)).fetchall():
            mi = pools.module_info(r["module_id"]) or {}
            recent.append({"module_id": r["module_id"],
                           "name": mi.get("name") or r["module_id"],
                           "cls": mi.get("cls"), "tier": mi.get("tier"),
                           "rarity": mi.get("rarity")})
        return {
            "lab_level": lab_lv, "proving_level": proving_lv, "slots": slots,
            "max_tier": lab_lv, "owned_total": len(owned),
            "pool_total": sum(len(v or []) for v in arche.values()),
            "tiers": tiers, "grid": grid, "fragments": frags,
            "pity": pity, "queue": queue, "recent": recent,
            "rarity": {k: {"name": v.get("name"), "weight": v.get("weight"),
                           "exchange": v.get("exchange")}
                       for k, v in rarity_cfg.items()},
        }

    def _queues(self, qq: str) -> dict:
        def rows(sql):
            return json.loads(json.dumps(
                [dict(r) for r in self.conn.execute(sql, (qq,)).fetchall()],
                ensure_ascii=False, default=str))

        return {
            "build": rows("SELECT id,x,y,def_id,target_level,end_tick FROM build_queue WHERE qq=?"),
            "research": rows("SELECT id,unit_type,tier,mode,end_ts FROM research_queue WHERE qq=?"),
            "production": rows("SELECT id,x,y,design_id,qty,end_tick FROM production_queue WHERE qq=?"),
        }

    @staticmethod
    def _has_prefix(text: str) -> bool:
        low = text.lower()
        return any(low.startswith(p) for p in ("/nw", "/海战", "nw", "海战"))

    # ---------- 生命周期 ----------
    def start(self, host: str = "0.0.0.0", port: int = 8090) -> None:
        import uvicorn

        config = uvicorn.Config(self.app, host=host, port=port,
                                log_level="warning", access_log=False,
                                timeout_keep_alive=30)
        self._server = uvicorn.Server(config)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._task = loop.create_task(self._serve())
            logger.info(f"[海战模拟器][Web] 已启动 http://{host}:{port}")
        else:  # 没有运行中的事件循环时兜底：退到后台线程
            import threading
            self._thread = threading.Thread(
                target=self._server.run, name="naval-web", daemon=True)
            self._thread.start()
            logger.info(f"[海战模拟器][Web] 已启动(线程) http://{host}:{port}")

    async def _serve(self):
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[海战模拟器][Web] 服务异常退出")

    async def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
        logger.info("[海战模拟器][Web] 已停止")
