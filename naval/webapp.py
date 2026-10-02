"""海战模拟器 Web 版：在插件进程内起一个 FastAPI/uvicorn 服务。

复用同一套命令路由（naval/router.py）、同一个 SQLite 库和同一个 GameCommands，
游戏逻辑零改动；Web 只是换了一层传输。

鉴权：QQ 号 + 口令 → 内存态随机 token（Authorization: Bearer <token>）。
口令为空的老账号允许登录，但会被强制引导设置口令后才能操作。
"""
import asyncio
import json
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

STATIC_DIR = Path(__file__).parent / "webstatic"
PUSH_KEEP = 200          # 每个玩家最多保留多少条待取推送
BODY_LIMIT = 64 * 1024


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
                "supply", "manpower", "money", "science", "intel")
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

        return app

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
