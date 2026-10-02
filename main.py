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


def is_private_origin(origin) -> bool:
    """判断 unified_msg_origin 是否私聊。

    AstrBot 的 origin 形如 `{platform_id}:{MessageType}:{session_id}`，
    其中 MessageType 是 `GroupMessage` / `FriendMessage` / `OtherMessage`
    （见 astrbot.core.platform.message_type.MessageType），**不是** `private`。
    早期这里写成 startswith("private") 导致 respond_private 配置完全失效。
    保留 private 前缀判断是为了兼容自造的 origin（如 Web 侧 web:<qq>）。
    """
    s = str(origin or "")
    if not s:
        return False
    parts = s.split(":", 2)
    if len(parts) >= 2 and parts[1] == "FriendMessage":
        return True
    return s.startswith("private")


# 看起来像在尝试用本插件指令（但没被认领）时，打一条日志便于排查
CMD_HEADS = ("nw", "海战", "/nw", "/海战")


def looks_like_command(text: str, normalized: str = None) -> bool:
    """是否"像是在用本插件指令"。只认开头，避免普通聊天误报。

    normalized 传 Router.normalize() 的结果（已剥掉 @某人 ）更准。
    """
    s = (normalized if normalized is not None else (text or "")).strip().lower()
    if not s or len(s) > 80:
        return False
    return s.startswith(CMD_HEADS)


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
            "public_url": str(plugin_cfg.get("web_public_url", "") or "").strip(),
        }

        data_dir = Path(__file__).parent / "naval" / "data"
        self.cfg = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
        # 把 Web 版信息并进游戏配置，供 game.py 的 nw帮助 推荐网页时读取
        self.cfg["web"] = self.web_cfg

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
        is_private = is_private_origin(origin)
        if is_private and not self.respond_private:
            return

        replies, consumed = await self.router.handle(origin, qq, nick, text)
        if not consumed:
            # 诊断：有人明显在尝试本插件指令却没被认领（打字错/前缀不对），
            # 打一条低噪声日志，便于从日志排查「我发了却没反应」。
            if looks_like_command(text, self.router.normalize(text)):
                logger.info(
                    f"[海战模拟器] 未认领（疑似误用）: qq={qq} origin={origin} "
                    f"text={text[:60]!r}")
            return  # 非命令：放行给 AI 聊天
        event.stop_event()
        # 诊断：成功认领时记录指令名（不记回复内容，避免刷屏）
        try:
            cmd = (self.router.parse(self.router.normalize(text)) or ("?",))[0]
        except Exception:
            cmd = "?"
        logger.info(f"[海战模拟器] QQ 指令: {cmd}　qq={qq}　origin={origin}")
        for reply in replies:
            await self._reply(event, reply)

    # ---------- 指令登记（供 AstrBot 指令管理识别） ----------
    # 说明：本插件用上面的 ALL 入口统一接管消息（优先级极高、会 stop_event），
    # 所以下面这个 handler 正常不会被执行。登记它的目的是让 `nw帮助` 出现在
    # AstrBot 的指令列表 / 指令管理里（AstrBot 只从 @filter.command 收集指令），
    # 从而满足「主 bot 里能找到 nw帮助」。
    # 万一分发顺序变化使它真的跑到，行为也与入口完全一致（同一份 Router）。
    @filter.command("nw帮助", alias={"海战帮助", "nwhelp"})
    async def nw_help(self, event):
        """海战模拟器：查看全部指令与网页版地址"""
        qq = str(event.get_sender_id())
        nick = event.get_sender_name() or qq
        origin = event.unified_msg_origin
        if is_private_origin(origin) and not self.respond_private:
            return
        replies, consumed = await self.router.handle(origin, qq, nick, "/nw帮助")
        if not consumed:
            return
        event.stop_event()
        for reply in replies:
            await self._reply(event, reply)
