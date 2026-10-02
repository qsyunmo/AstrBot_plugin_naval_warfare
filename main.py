"""海战模拟器 AstrBot 插件 —— 唯一消息入口（§24.1 路由契约）

入口内完成：前缀判定 → 确认会话拦截 → 命令提取 → 频率控制 → handler 分发。
非命令消息一律不消费（放行给 AI 聊天）。
"""
import difflib
import json
import re
from pathlib import Path

from astrbot.api import star, logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.message_components import Plain

from .naval.db import connect, init_db
from .naval.session import ConfirmStore, RateLimiter, DesignModeStore
from .naval.engine import GameEngine
from .naval.game import GameCommands, Ctx, COMMANDS, ALIASES
from .naval import pools

PREFIXES_SLASH = ("/nw", "/海战")   # 斜杠前缀：强制认领（未知指令给提示）
PREFIXES_BARE = ("nw", "海战")      # 裸前缀：仅当代号是已知指令才认领，避免吃掉"nw什么意思"这类闲聊
CONFIRM_WORDS = {"1", "2", "3", "确认", "取消"}
AT_RE = re.compile(r"@\S+\s*")
CHUNK = 1500


class NavalWarfare(star.Star):
    def __init__(self, context: star.Context, config: dict = None):
        self.context = context
        plugin_cfg = config or {}
        self.admins = set(str(q) for q in plugin_cfg.get("admins", []))
        self.respond_private = plugin_cfg.get("respond_private", True)

        data_dir = Path(__file__).parent / "naval" / "data"
        self.cfg = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))

        self.conn = connect()
        init_db(self.conn)
        self.confirm = ConfirmStore(self.cfg["confirm"]["timeout_sec"])
        self.design = DesignModeStore()
        self.ratelimit = RateLimiter(self.cfg["ratelimit"]["window_sec"],
                                     self.cfg["ratelimit"]["max_cmds"])
        self.cmds = GameCommands(self.conn, self.cfg, self.confirm, self.design)
        self.engine = GameEngine(self.conn, self.cfg, context)
        self.engine.start()
        logger.info("[海战模拟器] 插件加载成功")

    async def terminate(self):
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

    @staticmethod
    def _normalize(text: str) -> str:
        return AT_RE.sub("", text).strip().replace("　", " ")

    @staticmethod
    def _match_prefix(t: str, low: str, prefixes):
        """命中前缀返回 body（支持中文紧贴，如 /nw帮助）；未命中返回 None。"""
        for pfx in sorted(prefixes, key=len, reverse=True):
            p = pfx.lower()
            if low == p:
                return ""
            if low.startswith(p):
                return t[len(pfx):].strip()
        return None

    def _parse(self, t: str):
        """返回 (cmd, args[], force_claim) 或 None。
        force_claim=True：斜杠开头明确是命令；False：裸前缀仅已知指令认领。"""
        low = t.lower()
        body = self._match_prefix(t, low, PREFIXES_SLASH)
        force = True
        if body is None:
            body = self._match_prefix(t, low, PREFIXES_BARE)
            force = False
        if body is None:
            return None
        parts = body.split()
        return (parts[0], parts[1:], force) if parts else ("帮助", [], force)

    # ---------- 唯一入口 ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=2147484660)
    async def on_message(self, event):
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        t = self._normalize(text)
        if not t:
            return
        qq = str(event.get_sender_id())
        nick = event.get_sender_name() or qq
        origin = event.unified_msg_origin
        is_private = str(origin).startswith("private") if origin else False
        if is_private and not self.respond_private:
            return

        # 0) 确认会话拦截：报价后 60 秒内回复 1/2/3（无需前缀）
        pending = self.confirm.take(origin, qq)
        if pending and t in CONFIRM_WORDS:
            event.stop_event()
            choice = "1" if t == "确认" else ("2" if t == "取消" else t)
            await self._reply(event, await self.cmds.execute_action(
                Ctx(self.conn, self.cfg, self.confirm, origin, qq, nick, [], text,
                    self.design),
                pending, choice))
            return

        parsed = self._parse(t)
        if parsed is None:
            return  # 非命令：放行给 AI 聊天
        cmd_raw, args, force = parsed

        cmd = ALIASES.get(cmd_raw, cmd_raw)
        # 设计模式内 /nw<舰种>：浏览该舰种模块编号目录
        cls = pools.class_id(cmd_raw)
        in_design = cls is not None and self.design.get(origin, qq) is not None
        # 裸前缀（nw/海战）：已知指令或设计模式内的舰种名才认领，其余交给 AI 闲聊
        if not force and cmd not in COMMANDS and not in_design:
            return

        # 频率控制
        ok, wait = self.ratelimit.allow(qq)
        if not ok:
            event.stop_event()
            await self._reply(event, f"⏳ 指令太快啦，还剩 {wait:.0f} 秒，提督喝口茶。")
            return

        event.stop_event()
        ctx = Ctx(self.conn, self.cfg, self.confirm, origin, qq, nick, args, text,
                  self.design)
        if cmd not in COMMANDS:
            if cls is not None:
                if in_design:
                    try:
                        result = await self.cmds.browse_class(ctx, cls)
                    except Exception:
                        logger.exception("[海战模拟器] 模块目录浏览异常")
                        result = "💥 浏览出错，已记录日志，请联系管理员。"
                    await self._reply(event, result)
                    return
                await self._reply(event,
                    f"📚 想看【{pools.mod_data()['classes'][cls]['name']}】模块目录？"
                    f"先发 /nw设计 进入设计模式，再发 /nw{cmd_raw} 浏览。")
                return
            guess = difflib.get_close_matches(cmd_raw, list(COMMANDS) + list(ALIASES), n=1)
            hint = f"你是不是想找：/nw{guess[0]}？" if guess else "发 /nw帮助 查看全部指令。"
            await self._reply(event, f"❓ 未知指令【{cmd_raw}】。{hint}")
            return

        method_name = COMMANDS[cmd][0]
        try:
            result = await getattr(self.cmds, method_name)(ctx)
        except Exception:
            logger.exception(f"[海战模拟器] 指令执行异常: {cmd} {args}")
            result = "💥 指令执行出错，已记录日志，请联系管理员。"
        await self._reply(event, result)
