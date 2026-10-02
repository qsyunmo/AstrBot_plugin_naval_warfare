"""确认会话（60秒）与指令频率控制——均为内存态，重启即清空（可接受）"""
import time


class ConfirmStore:
    def __init__(self, timeout_sec: int = 60):
        self.timeout = timeout_sec
        self._pending: dict[str, dict] = {}

    @staticmethod
    def _key(origin: str, qq: str) -> str:
        return f"{origin}|{qq}"

    def put(self, origin: str, qq: str, action: dict) -> None:
        self._pending[self._key(origin, qq)] = {"action": action, "ts": time.time()}

    def take(self, origin: str, qq: str):
        item = self._pending.get(self._key(origin, qq))
        if not item:
            return None
        if time.time() - item["ts"] > self.timeout:
            self._pending.pop(self._key(origin, qq), None)
            return None
        return item["action"]

    def drop(self, origin: str, qq: str) -> None:
        self._pending.pop(self._key(origin, qq), None)


class DesignModeStore:
    """舰船设计模式会话：记住当前浏览舰种与模块编号目录快照。
    滑动超时（默认30分钟无指令自动退出），仅内存态。"""
    def __init__(self, timeout_sec: int = 1800):
        self.timeout = timeout_sec
        self._state: dict[str, dict] = {}

    @staticmethod
    def _key(origin: str, qq: str) -> str:
        return f"{origin}|{qq}"

    def enter(self, origin: str, qq: str) -> dict:
        st = {"ts": time.time(), "cls": None, "catalog": []}
        self._state[self._key(origin, qq)] = st
        return st

    def get(self, origin: str, qq: str):
        item = self._state.get(self._key(origin, qq))
        if not item:
            return None
        if time.time() - item["ts"] > self.timeout:
            self._state.pop(self._key(origin, qq), None)
            return None
        item["ts"] = time.time()
        return item

    def drop(self, origin: str, qq: str) -> None:
        self._state.pop(self._key(origin, qq), None)


class RateLimiter:
    def __init__(self, window_sec: int = 5, max_cmds: int = 3):
        self.window, self.max = window_sec, max_cmds
        self._hits: dict[str, list[float]] = {}

    def allow(self, qq: str):
        """返回 (是否放行, 剩余冷却秒)"""
        now = time.time()
        hits = [t for t in self._hits.get(qq, []) if now - t < self.window]
        if len(hits) >= self.max:
            return False, round(self.window - (now - hits[0]), 1)
        hits.append(now)
        self._hits[qq] = hits
        return True, 0
