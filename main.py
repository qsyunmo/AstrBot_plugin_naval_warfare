"""海战模拟器 AstrBot 插件 —— 唯一消息入口（§24.1 路由契约）

入口内完成：归一化 → 交给 Router（前缀判定/确认拦截/频率控制/分发）→ 发送回复。
非命令消息一律不消费（放行给 AI 聊天）。

命令语义在 naval/router.py，QQ 侧与本插件的 Web 版共用同一份实现。
"""
import json
from pathlib import Path

from astrbot.api import star, logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.message_components import Plain

from .naval.db import connect, init_db
from .naval.session import ConfirmStore, RateLimiter, DesignModeStore
from .naval.engine import GameEngine
from .naval.game import GameCommands
from .naval.router import Router
from .naval.webapp import NavalWeb, WebPushBuffer

CHUNK = 1500


class NavalWarfare(star.Star):
    def __init__(self, context: star.Context, config: dict = None):
        self.context = context
        plugin_cfg = config or {}
        self.admins = set(str(q) for q in plugin_cfg.get("admins", []))
        self.respond_private = plugin_cfg.get("respond_private", True)
        self.web_cfg = {
            "enable": plugin_cfg.get("web_enable", True),
            "port": int(plugin_cfg.get("web_port", 8090) or 8090),
            "host": plugin_cfg.get("web_host", "0.0.0.0"),
        }

        data_dir = Path(__file__).parent / "naval" / "data"
        self.cfg = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))

        self.conn = connect()
        init_db(self.conn)
        self.confirm = ConfirmStore(self.cfg["confirm"]["timeout_sec"])
        self.design = DesignModeStore()
        self.ratelimit = RateLimiter(self.cfg["ratelimit"]["window_sec"],
                                     self.cfg["ratelimit"]["max_cmds"])
        self.cmds = GameCommands(self.conn, self.cfg, self.confirm, self.design)

        # Web 用户的到期推送没有 QQ 会话可发，先缓冲，等网页轮询取走
        self.push_buffer = WebPushBuffer()
        self.engine = GameEngine(self.conn, self.cfg, context, push_buffer=self.push_buffer)
        self.engine.start()

        # 命令路由（QQ 与 Web 共用）
        self.router = Router(self.conn, self.cfg, self.confirm, self.design,
                             self.ratelimit, on_error=self._on_router_error)
        self.router.cmds = self.cmds

        self.web = NavalWeb(self) if self.web_cfg["enable"] else None
        if self.web:
            self.web.start(self.web_cfg["host"], self.web_cfg["port"])

        logger.info("[海战模拟器] 插件加载成功")

    @staticmethod
    def _on_router_error(label: str, exc: Exception):
        logger.exception(f"[海战模拟器] {label}: {exc}")

    async def terminate(self):
        if self.web:
            await self.web.stop()
        await self.engine.stop()
        self.conn.close()
        logger.info("[海战模拟器] 插件已卸载")

    # ---------- 工具 ----------
    @staticmethod
    def _chunks(text: str):
        text = text.strip()
        while len(text) > CHUNK:
            cut = text.rfind("\n", 0, CHUNK)
            if cut <= 0:
                cut = CHUNK
            yield text[:cut].strip()
            text = text[cut:]
        if text.strip():
            yield text.strip()

    async def _reply(self, event, text: str):
        for part in self._chunks(text):
            await event.send(MessageChain([Plain(part)]))

    # ---------- 唯一入口 ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=2147484660)
    async def on_message(self, event):
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        qq = str(event.get_sender_id())
        nick = event.get_sender_name() or qq
        origin = event.unified_msg_origin
        is_private = str(origin).startswith("private") if origin else False
        if is_private and not self.respond_private:
            return

        replies, consumed = await self.router.handle(origin, qq, nick, text)
        if not consumed:
            return  # 非命令：放行给 AI 聊天
        event.stop_event()
        for reply in replies:
            await self._reply(event, reply)
