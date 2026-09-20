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

    ★ 多用户版（2026-09-20）：管理员凭据现在落在 `users` 表里（role='admin'）。
      kv 里的 `admin_user` / `admin_pwd_hash` 只在**老库**里还有值，由
      `db._migrate_users` 搬进 users 表；这里再兜一次底，保证任何情况下
      「一个可登录的管理员」一定存在 —— 否则会出现「库是新的但没人能登进去」
      这种要拿命令行救的死局。
    """
    out: Dict[str, Optional[str]] = {}
    if not db.user_count_admins():
        user = settings.BOOTSTRAP_ADMIN_USER or "admin"
        pwd = settings.BOOTSTRAP_ADMIN_PASSWORD or new_token(12)
        # 同名用户已存在（比如老库搬过来的不是 admin 名，而现在要建 admin）
        # 就换一个能用的名字，别让 bootstrap 自己撞唯一索引
        if db.user_by_name(user):
            user = "admin"
            if db.user_by_name(user):
                user = "admin%d" % (int(time.time()) % 10000)
        db.user_create(user, hash_secret(pwd), role=db.ROLE_ADMIN,
                       display_name="系统管理员", created_by="bootstrap",
                       must_change=False)
        out["admin_user"] = user
        out["admin_password"] = pwd
    if not db.kv_get("agent_token_hash"):
        tok = settings.BOOTSTRAP_AGENT_TOKEN or ("stzb_" + new_token(24))
        db.kv_set("agent_token_hash", hash_secret(tok))
        db.kv_set("agent_token_hint", agent_token_hint(tok))
        out["agent_token"] = tok
    return out


# ------------------------------------------------------------------ 校验

# 用户名不存在时拿来做计时对比的假哈希：**模块加载时算一次就固定下来**。
# 不能每次现算 —— scrypt 一次约 60ms，现算等于给每次失败登录白加一倍耗时，
# 反而把「用户不存在」和「口令错」的耗时差拉得更大，起不到拉平的作用。
_DUMMY_HASH = hash_secret("stzb-dummy-hash-for-timing-equalization")


def check_user(user: str, password: str) -> Optional[Dict]:
    """多用户登录校验。返回用户字典（不含哈希）表示通过，否则 None。

    ★ 三条硬规矩：
      1. **停用的用户直接拒**（enabled=0）。停用是管理员的即时止损手段，
         不能留着「口令对了就还能进」的后门。
      2. 用户名不存在时**照样跑一次哈希**，避免通过响应时间区分
         「用户名错」和「口令错」—— 单管理员版本就有这条，多用户下更重要，
         否则可以拿它枚举出系统里有哪些用户名。
      3. 用户不存在时用作对比的是**固定的假哈希**。不能拿别的用户的真哈希去比，
         那会让「A 的口令碰巧能登录 B」这种荒唐事在计时上露出差异。
    """
    u = (user or "").strip()
    pwd = password or ""
    if not u or not pwd:
        return None
    row = db.user_by_name(u, with_hash=True)
    if not row:
        verify_secret(pwd, _DUMMY_HASH)      # 拉平耗时，防止用户名枚举
        return None
    if not row.get("enabled"):
        verify_secret(pwd, row.get("pwd_hash") or "")
        return None
    if not verify_secret(pwd, row.get("pwd_hash") or ""):
        return None
    return db.user_get(int(row["id"]))


def check_admin(user: str, password: str) -> bool:
    """兼容老调用（CLI / 测试）：口令对**且**是管理员才算通过。"""
    u = check_user(user, password)
    return bool(u and u.get("is_admin"))


def check_agent_token(token: str) -> bool:
    if not token:
        return False
    return verify_secret(token, db.kv_get("agent_token_hash") or "")


def rotate_agent_token() -> str:
    tok = "stzb_" + new_token(24)
    db.kv_set("agent_token_hash", hash_secret(tok))
    db.kv_set("agent_token_hint", agent_token_hint(tok))
    return tok


def set_user_password(uid: int, password: str, must_change: bool = False) -> None:
    db.user_update(uid, pwd_hash=hash_secret(password),
                   must_change=1 if must_change else 0)


def set_admin_password(user: str, password: str) -> None:
    """兼容老调用：按用户名改口令。用户不存在时**顺手建出来**。

    「顺手建」是刻意的：老版本的这个函数就是「改管理员口令」，
    在只有一个管理员的时代这样最省事。现在保留这个语义，让
    `python -m app.cli reset-password` 在「管理员被误删」时还能救回来。
    """
    row = db.user_by_name(user)
    if row:
        set_user_password(int(row["id"]), password)
        return
    db.user_create(user or "admin", hash_secret(password), role=db.ROLE_ADMIN,
                   display_name="系统管理员", created_by="cli", must_change=False)


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


# ------------------------------------------------------------------ 口令强度

# 弱口令黑名单。都是**长度够但一猜就中**的那种，纯按长度卡是拦不住的。
_WEAK_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1", "passw0rd",
    "qwertyui", "qwerty123", "abc12345", "11111111", "00000000", "88888888",
    "admin123", "admin888", "root1234", "letmein1", "iloveyou", "welcome1",
    "a1234567", "1qaz2wsx", "qazwsxed", "zxcvbnm1", "asdfghjk", "stzb1234",
}


def password_problem(password: str, username: str = "") -> str:
    """检查口令强度，返回**问题描述**；没问题返回空串。

    只做「明显不合格」的拦截，不搞复杂度强制（大小写数字符号全要那种规则
    逼得用户把口令写在便签上，反而更不安全）：
      · 至少 8 位
      · 不能整串等于用户名、也不能是用户名 + 几个数字这种一眼看穿的组合
      · 不在常见弱口令表里
    """
    p = password or ""
    if len(p) < 8:
        return "口令至少 8 位"
    low = p.lower()
    u = (username or "").strip().lower()
    if low in _WEAK_PASSWORDS:
        return "这是最常见的弱口令之一，换一个"
    if u and low == u:
        return "口令不能和用户名一样"
    if u and len(u) >= 3 and low.startswith(u) and len(p) - len(u) <= 3:
        return "口令不能是「用户名 + 几个数字」这种一眼看穿的组合"
    if len(set(p)) <= 2:
        return "口令字符太单一（比如全是同一个字母），换一个"
    return ""
