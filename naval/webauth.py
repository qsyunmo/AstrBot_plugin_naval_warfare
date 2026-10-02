"""Web 版账号口令与会话（纯标准库，无第三方依赖）

口令用 pbkdf2_hmac('sha256') 散列，存成 `pbkdf2_sha256$迭代$盐hex$散列hex`。
会话是内存态随机 token：进程重启即失效，玩家重新登录即可。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

ALGO = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128


def hash_password(password: str) -> str:
    """生成口令散列字符串。空口令返回空串（表示未设置）。"""
    if not password:
        return ""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ALGO}${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。stored 为空表示账号尚未设置口令。"""
    if not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def check_password_strength(password: str) -> str | None:
    """不合格返回错误文案，合格返回 None。"""
    if len(password) < MIN_PASSWORD_LEN:
        return f"口令至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > MAX_PASSWORD_LEN:
        return f"口令最长 {MAX_PASSWORD_LEN} 位"
    if password.isdigit():
        return "口令不能全是数字"
    return None


def is_valid_qq(qq: str) -> bool:
    """QQ 号：5~12 位纯数字（够宽松，兼容老号段）。"""
    return bool(qq) and qq.isdigit() and 5 <= len(qq) <= 12


class SessionStore:
    """内存态会话表。token -> {qq, exp}。"""

    def __init__(self, ttl_sec: int = 7 * 24 * 3600):
        self.ttl = ttl_sec
        self._sessions: dict[str, dict] = {}

    def _gc(self) -> None:
        now = time.time()
        for tok in [t for t, s in self._sessions.items() if s["exp"] < now]:
            self._sessions.pop(tok, None)

    def create(self, qq: str) -> str:
        self._gc()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"qq": str(qq), "exp": time.time() + self.ttl}
        return token

    def get(self, token: str | None) -> str | None:
        """返回 token 对应的 qq，无效/过期返回 None。"""
        if not token:
            return None
        s = self._sessions.get(token)
        if not s:
            return None
        if s["exp"] < time.time():
            self._sessions.pop(token, None)
            return None
        return s["qq"]

    def drop(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def drop_qq(self, qq: str) -> int:
        """踢掉某账号的全部会话（改口令后用）。"""
        hits = [t for t, s in self._sessions.items() if s["qq"] == str(qq)]
        for t in hits:
            self._sessions.pop(t, None)
        return len(hits)
