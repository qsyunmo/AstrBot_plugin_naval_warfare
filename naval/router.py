"""命令路由（不依赖 AstrBot）：前缀判定 → 确认拦截 → 频率控制 → handler 分发。

QQ 侧（main.py）与 Web 侧（webapp.py）共用本模块，保证两边语义完全一致，
不会出现「网页能用的指令 QQ 不能用」这类漂移。

用法：
    replies, consumed = await router.handle(origin, qq, nick, text)
    consumed=False 表示这条消息不是指令，调用方应放行（QQ 侧交给 AI 闲聊）。
"""
import difflib
import re

from . import pools
from .game import ALIASES, COMMANDS, Ctx

PREFIXES_SLASH = ("/nw", "/海战")   # 斜杠前缀：强制认领（未知指令给提示）
PREFIXES_BARE = ("nw", "海战")      # 裸前缀：仅当代号是已知指令才认领
CONFIRM_WORDS = {"1", "2", "3", "确认", "取消"}
AT_RE = re.compile(r"@\S+\s*")

ERROR_TEXT = "💥 指令执行出错，已记录日志，请联系管理员。"


class Router:
    def __init__(self, conn, cfg, confirm, design, ratelimit=None, on_error=None):
        self.conn, self.cfg, self.confirm, self.design = conn, cfg, confirm, design
        self.ratelimit = ratelimit
        self.on_error = on_error          # 异常回调(标签, exc)，用于写日志
        self.cmds = None                  # 由调用方注入 GameCommands

    # ---------- 文本处理 ----------
    @staticmethod
    def normalize(text: str) -> str:
        return AT_RE.sub("", text or "").strip().replace("　", " ")

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

    def parse(self, t: str):
        """返回 (cmd, args[], force_claim) 或 None。"""
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

    def _fail(self, label: str, exc: Exception) -> str:
        if self.on_error:
            self.on_error(label, exc)
        return ERROR_TEXT

    def _ctx(self, origin, qq, nick, args, raw) -> Ctx:
        return Ctx(self.conn, self.cfg, self.confirm, origin, qq, nick, args, raw, self.design)

    # ---------- 主入口 ----------
    async def handle(self, origin: str, qq: str, nick: str, text: str):
        """返回 (replies: list[str], consumed: bool)。

        consumed=False 表示不是指令，应放行；True 表示已认领并需发送 replies。
        """
        t = self.normalize(text)
        if not t:
            return [], False

        # 0) 确认会话拦截：报价后限时回复 1/2/3（无需前缀）
        pending = self.confirm.take(origin, qq)
        if pending and t in CONFIRM_WORDS:
            choice = "1" if t == "确认" else ("2" if t == "取消" else t)
            try:
                reply = await self.cmds.execute_action(
                    self._ctx(origin, qq, nick, [], text), pending, choice)
            except Exception as e:
                reply = self._fail(f"确认操作异常 qq={qq}", e)
            return [reply], True

        parsed = self.parse(t)
        if parsed is None:
            return [], False
        cmd_raw, args, force = parsed

        cmd = ALIASES.get(cmd_raw, cmd_raw)
        # 设计模式内 /nw<舰种>：浏览该舰种模块编号目录
        cls = pools.class_id(cmd_raw)
        in_design = cls is not None and self.design.get(origin, qq) is not None
        # 裸前缀：已知指令或设计模式内的舰种名才认领，其余交给调用方（QQ 侧给 AI 闲聊）
        if not force and cmd not in COMMANDS and not in_design:
            return [], False

        # 频率控制
        if self.ratelimit is not None:
            ok, wait = self.ratelimit.allow(qq)
            if not ok:
                return [f"⏳ 指令太快啦，还剩 {wait:.0f} 秒，提督喝口茶。"], True

        ctx = self._ctx(origin, qq, nick, args, text)

        if cmd not in COMMANDS:
            if cls is not None:
                if in_design:
                    try:
                        return [await self.cmds.browse_class(ctx, cls)], True
                    except Exception as e:
                        return [self._fail(f"模块目录浏览异常 qq={qq}", e)], True
                return ([f"📚 想看【{pools.mod_data()['classes'][cls]['name']}】模块目录？"
                         f"先发 /nw设计 进入设计模式，再发 /nw{cmd_raw} 浏览。"], True)
            guess = difflib.get_close_matches(cmd_raw, list(COMMANDS) + list(ALIASES), n=1)
            hint = f"你是不是想找：/nw{guess[0]}？" if guess else "发 /nw帮助 查看全部指令。"
            return [f"❓ 未知指令【{cmd_raw}】。{hint}"], True

        method_name = COMMANDS[cmd][0]
        try:
            result = await getattr(self.cmds, method_name)(ctx)
        except Exception as e:
            result = self._fail(f"指令执行异常 {cmd} {args}", e)
        return [result], True
