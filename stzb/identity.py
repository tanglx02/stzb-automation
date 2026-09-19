# -*- coding: utf-8 -*-
"""客户端稳定标识。

后端要能区分「这台机器」和「那台机器」，光靠机器名不够 ——
改个机器名、重装系统、或者一个人两台机器同名，都会让后台记录错乱。

所以客户端第一次运行时就生成一个随机 uid 存到 state/client.json，
之后一直用它。文件丢了也没关系，会生成新的，后台那边会多出一条记录，
人工在「客户端」页把旧记录删掉即可（比强行复用 host 安全）。

为什么不把 uid 写进 config.json：那份文件用户会手改（要填后端地址和令牌），
很容易在复制粘贴时把 uid 也一起复制到另一台机器上，导致两台机器抢同一个身份。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import platform
from typing import Any, Dict, Optional

DEFAULT_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "client.json")


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return platform.node() or "unknown"


def machine_fingerprint() -> str:
    """轻量指纹，仅用于「uid 丢了但明显还是同一台机器」时给个提示。"""
    parts = [platform.node() or "", platform.machine() or "",
             str(os.environ.get("PROCESSOR_IDENTIFIER") or "")]
    return "|".join(parts)


def load_identity(state_path: str = DEFAULT_STATE) -> Dict[str, Any]:
    """读身份。不存在就创建。返回 {uid, host, created_at, fingerprint}。"""
    import datetime as dt
    data: Dict[str, Any] = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}

    changed = False
    if not data.get("uid"):
        data["uid"] = "cli_" + secrets.token_hex(10)
        data["created_at"] = dt.datetime.now().isoformat(timespec="seconds")
        changed = True
    if not data.get("fingerprint"):
        data["fingerprint"] = machine_fingerprint()
        changed = True
    # 机器名会变（笔记本改 WiFi、域环境），每次都刷新一下，反正只是展示用
    if data.get("host") != hostname():
        data["host"] = hostname()
        changed = True

    if changed:
        save_identity(data, state_path)
    return data


def save_identity(data: Dict[str, Any], state_path: str = DEFAULT_STATE) -> None:
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class Identity:
    """给主流程用的薄封装。`Identity.load()` 之后 .uid / .host 随时可读。"""

    def __init__(self, data: Dict[str, Any], path: str):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, state_path: str = DEFAULT_STATE) -> "Identity":
        return cls(load_identity(state_path), state_path)

    @property
    def uid(self) -> str:
        return str(self._data.get("uid") or "")

    @property
    def host(self) -> str:
        return str(self._data.get("host") or hostname())

    @property
    def created_at(self) -> str:
        return str(self._data.get("created_at") or "")

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


def read_assignment_cache(state_path: Optional[str] = None) -> Dict[str, Any]:
    """读上一次从后端拿到的账号/角色指派（缓存）。

    用途：后端连不上的时候，客户端也能知道自己上次该用哪个账号 ——
    否则一旦断网，跑任务时就不知道要不要切、切到谁。
    """
    p = state_path or os.path.join(os.path.dirname(DEFAULT_STATE), "assignment.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_assignment_cache(data: Dict[str, Any], state_path: Optional[str] = None) -> None:
    p = state_path or os.path.join(os.path.dirname(DEFAULT_STATE), "assignment.json")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass