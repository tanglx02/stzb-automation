# -*- coding: utf-8 -*-
"""口令 / 令牌 / 会话 / 限流。

不引入 passlib / bcrypt 这类额外依赖，直接用标准库的 scrypt ——
它本身就是抗 GPU 的口令哈希函数，参数按 OWASP 建议取（n=2^15, r=8, p=1）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import db, settings

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
# OpenSSL 默认 maxmem 只有 32MB，而 n=2^15,r=8 需要 128*n*r ≈ 33.5MB，
# 不显式放开就会报 "memory limit exceeded" —— 实测踩过，第一次启动直接起不来。
SCRYPT_MAXMEM = 128 * 1024 * 1024


# ------------------------------------------------------------------ 哈希

def hash_secret(secret: str) -> str:
    """返回 `scrypt$n$r$p$salt_hex$hash_hex`，盐随机。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
                        maxmem=SCRYPT_MAXMEM)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                      salt.hex(), dk.hex())


def verify_secret(secret: str, stored: str) -> bool:
    """常数时间比较。任何格式异常一律判失败，不抛异常。"""
    if not secret or not stored:
        return False
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(secret.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p),
                            dklen=len(bytes.fromhex(hash_hex)),
                            maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def agent_token_hint(token: str) -> str:
    """只给人看头尾，方便对账又不泄露完整令牌。"""
    if len(token) <= 12:
        return token[:4] + "…"
    return "%s…%s" % (token[:8], token[-4:])


# ------------------------------------------------------------------ 会话密钥

def session_secret() -> str:
    """会话签名密钥：优先环境变量，否则生成一次存库（重启后会话不失效）。

    注意这个函数在**模块导入时**就会被调用（挂中间件要用），那时 lifespan 还没跑，
    所以这里必须自己先把库准备好，否则会报 unable to open database file。
    """
    if settings.SECRET_KEY_ENV:
        return settings.SECRET_KEY_ENV
    settings.ensure_dirs()
    db.init_db()
    v = db.kv_get("session_secret")
    if not v:
        v = new_token(48)
        db.kv_set("session_secret", v)
    return v


# ------------------------------------------------------------------ 首次引导

def bootstrap() -> Dict[str, Optional[str]]:
    """首次启动时建立管理员口令与 agent 令牌，返回**只在这一次**明文的凭据。

    已经初始化过就返回空字典，绝不再打印。
    """
    out: Dict[str, Optional[str]] = {}
    if not db.kv_get("admin_pwd_hash"):
        user = settings.BOOTSTRAP_ADMIN_USER or "admin"
        pwd = settings.BOOTSTRAP_ADMIN_PASSWORD or new_token(12)
        db.kv_set("admin_user", user)
        db.kv_set("admin_pwd_hash", hash_secret(pwd))
        out["admin_user"] = user
        out["admin_password"] = pwd
    if not db.kv_get("agent_token_hash"):
        tok = settings.BOOTSTRAP_AGENT_TOKEN or ("stzb_" + new_token(24))
        db.kv_set("agent_token_hash", hash_secret(tok))
        db.kv_set("agent_token_hint", agent_token_hint(tok))
        out["agent_token"] = tok
    return out


# ------------------------------------------------------------------ 校验

def check_admin(user: str, password: str) -> bool:
    if not user or not password:
        return False
    stored_user = db.kv_get("admin_user") or "admin"
    stored_hash = db.kv_get("admin_pwd_hash") or ""
    if not hmac.compare_digest(user.strip(), stored_user):
        # 仍然跑一次哈希，避免通过响应时间区分「用户名错」和「口令错」
        verify_secret(password, stored_hash)
        return False
    return verify_secret(password, stored_hash)


def check_agent_token(token: str) -> bool:
    if not token:
        return False
    return verify_secret(token, db.kv_get("agent_token_hash") or "")


def rotate_agent_token() -> str:
    tok = "stzb_" + new_token(24)
    db.kv_set("agent_token_hash", hash_secret(tok))
    db.kv_set("agent_token_hint", agent_token_hint(tok))
    return tok


def set_admin_password(user: str, password: str) -> None:
    db.kv_set("admin_user", user or "admin")
    db.kv_set("admin_pwd_hash", hash_secret(password))


# ------------------------------------------------------------------ 登录限流

_lock = threading.Lock()
_fails: Dict[str, List[float]] = {}


def login_blocked(ip: str) -> Tuple[bool, int]:
    """返回 (是否已被拦, 还要等多少秒)。"""
    now = time.time()
    with _lock:
        arr = [t for t in _fails.get(ip, []) if now - t < settings.LOGIN_WINDOW_SECONDS]
        _fails[ip] = arr
        if len(arr) >= settings.LOGIN_MAX_FAILS:
            wait = int(settings.LOGIN_WINDOW_SECONDS - (now - min(arr))) + 1
            return True, max(wait, 1)
    return False, 0


def login_record_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def login_reset(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)
