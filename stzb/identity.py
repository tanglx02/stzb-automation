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


# ================================================================== 进程存活判断
#
# 下面这几个东西是「同一台机器上有没有一轮任务正在跑」的**唯一判据**。
# 常驻代理（agent.py）靠它决定「后台派的活现在能不能接」，run_daily 靠它
# 决定「要不要接管上一个进程留下的锁」。两处各写一遍必然分叉 ——
# 分叉的后果是「代理以为空闲、其实有任务在跑」，两边一起点模拟器，
# 点击全部错位（这个坑真踩过，见 RunLock 的说明）。

# 单实例锁的最长有效期（秒）。正常一轮含冷启动模拟器在 10 分钟以内，
# 超过就当成上次异常退出（关窗口 / 蓝屏 / 任务计划强杀）留下的残留锁。
LOCK_STALE_SECONDS = 2400


def pid_alive(pid: int) -> bool:
    """判断进程是否还活着（跨平台，任何异常都当「不活着」）。

    ⚠️ Windows 上**不能**用 `os.kill(pid, 0)` 探活：Windows 的 os.kill 会把
    非 CTRL_* 的信号实现成 `TerminateProcess` —— 探活探成杀人，直接把那台
    正在跑任务的进程干掉。所以这里走 Win32 API 查退出码。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def run_lock_path(project_root: str) -> str:
    """单实例锁的位置（run_daily 与 agent 必须用同一个）。"""
    return os.path.join(project_root, "state", "run.lock")


def run_in_progress(project_root: str, now: Optional[float] = None) -> bool:
    """本机此刻是否**真有一轮任务在跑**。

    ★ 判据是「锁文件里的 pid 还活着 **且** 锁没超龄」，不是「锁文件在不在」——
      被强杀的进程会留下残留锁文件，拿「文件存在」当判据会把其实空闲的机器
      判成「忙」，于是后台派的活永远接不了。run_daily 自己也是这么判的
      （它会识别并接管残留锁），两边口径必须一致。
    """
    import time as _t
    import datetime as _dt
    p = run_lock_path(project_root)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return False
    try:
        pid = int(d.get("pid") or 0)
    except Exception:
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        age = (_t.time() if now is None else now) - \
            _dt.datetime.fromisoformat(str(d.get("started"))).timestamp()
    except Exception:
        age = LOCK_STALE_SECONDS + 1          # 读不出开始时间 → 当过期
    return age < LOCK_STALE_SECONDS and pid_alive(pid)