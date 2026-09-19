# -*- coding: utf-8 -*-
"""客户端心跳。

**为什么必须有这个东西：**
服务端在公网、客户端在内网。内网客户端能主动连服务端，服务端连不回来 ——
这是 NAT 的硬限制，不是配置问题。所以「后台能不能看到客户端在线」这件事，
唯一可行的做法就是客户端**自己定时上报**。

于是这个模块干三件事：
  1. 每 N 秒 POST 一次 /api/agent/ping，把「我是谁 / 我在干什么 / 我现在是哪个账号」报上去
  2. 把服务端在响应里捎回来的指令取出来执行：
        · assignment / switch_needed → 告诉主流程该切到哪个账号、哪个角色
        · command（人工点的「探测界面」等）→ 就地执行并回报结果
  3. 全程不影响主流程：线程崩了、网络断了，任务照跑

心跳线程是 daemon，主进程退出它就跟着没了，不需要额外清理。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Callable, Dict, Optional

# 服务端没告诉节奏时的兜底：30 秒一次，判掉线 90 秒
FALLBACK_INTERVAL = 30


class Heartbeat:
    """后台心跳线程。

    参数：
      client      : CloudClient 实例（已配好 base_url / token）
      identity    : stzb.identity.Identity
      state_fn    : 无参回调，返回要上报的状态字典（在线程里被调用，
                    实现里读写共享状态务必加锁或用简单不可变对象）
      on_assignment : 收到指派时回调 (assignment: dict, switch_needed: bool, reason: str)
      on_command    : 收到一次性指令时回调 (command: dict) -> (ok, message, data)
      logger      : 日志函数
    """

    def __init__(self, client, identity,
                 state_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                 on_assignment: Optional[Callable[[Dict[str, Any], bool, str], None]] = None,
                 on_command: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 logger: Callable[[str], None] = print,
                 interval: Optional[int] = None):
        self.client = client
        self.identity = identity
        self.state_fn = state_fn or (lambda: {})
        self.on_assignment = on_assignment
        self.on_command = on_command
        self.log = logger
        self.interval = int(interval or FALLBACK_INTERVAL)

        self._stop = threading.Event()
        self._poke = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 最新一次心跳的成果，主流程随时可读
        self.assignment: Optional[Dict[str, Any]] = None
        self.switch_needed = False
        self.switch_reason = ""
        self.task_paused = False
        self.last_ok = False
        self.last_error = ""
        self.last_at: Optional[dt.datetime] = None
        self.beats = 0

    # -------------------------------------------------------------- 生命周期

    def start(self) -> "Heartbeat":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stzb-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._poke.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def poke(self) -> None:
        """让心跳线程立刻打一次（比如状态刚变了、任务刚要开始）。"""
        self._poke.set()

    # -------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        # 启动时不立刻打：主流程自己会先 ping 一次拿到指派，避免重复请求打乱日志
        while not self._stop.is_set():
            self._poke.wait(timeout=self.interval)
            self._poke.clear()
            if self._stop.is_set():
                break
            try:
                self.beat()
            except Exception as e:                      # 心跳线程绝不允许把主流程带走
                self.last_error = repr(e)
                self.last_ok = False
                try:
                    self.log("  ! 心跳异常：%r（不影响任务）" % (e,))
                except Exception:
                    pass

    def beat(self) -> Dict[str, Any]:
        """打一次心跳并处理服务端捎回来的东西。返回响应字典（失败时为空）。"""
        status = {}
        try:
            status = self.state_fn() or {}
        except Exception:
            status = {}

        payload = {
            "uid": self.identity.uid,
            "host": self.identity.host,
            **{k: v for k, v in status.items() if k in
               ("mode", "state", "busy", "note", "current", "device",
                "last_run_at", "extra")},
        }
        ok, data = self.client.heartbeat(payload)
        self.beats += 1
        self.last_at = dt.datetime.now()
        if not ok or not isinstance(data, dict):
            self.last_ok = False
            self.last_error = str(data)
            return {}
        self.last_ok = True
        self.last_error = ""

        # 服务端可以借心跳告诉我们心跳该多快（统一改的话不用动客户端）
        iv = data.get("heartbeat_interval")
        try:
            if iv and 5 <= int(iv) <= 3600:
                self.interval = int(iv)
        except Exception:
            pass

        self.task_paused = bool(data.get("task_paused"))

        with self._lock:
            self.assignment = data.get("assignment") or None
            self.switch_needed = bool(data.get("switch_needed"))
            self.switch_reason = str(data.get("switch_reason") or "")

        if self.on_assignment and (self.assignment or self.switch_needed):
            try:
                self.on_assignment(self.assignment or {}, self.switch_needed,
                                   self.switch_reason)
            except Exception as e:
                self.log("  ! 处理指派回调异常：%r" % (e,))

        cmd = data.get("command")
        if cmd and self.on_command:
            threading.Thread(target=self._run_command, args=(cmd,),
                             name="stzb-cmd", daemon=True).start()
        return data

    # -------------------------------------------------------------- 指令执行

    def _run_command(self, cmd: Dict[str, Any]) -> None:
        ok, message, payload = False, "", {}
        try:
            res = self.on_command(cmd)
            if isinstance(res, tuple) and len(res) == 3:
                ok, message, payload = res
            elif isinstance(res, dict):
                ok = bool(res.get("ok", True))
                message = str(res.get("message") or "")
                payload = res.get("data") or {}
            else:
                ok, message = True, ""
        except Exception as e:
            ok, message = False, "执行指令异常：%r" % (e,)
        try:
            self.log("  · 指令 %s → %s%s"
                     % (cmd.get("kind"), "成功" if ok else "失败",
                        ("：" + message) if message else ""))
        except Exception:
            pass
        try:
            self.client.probe_result(self.identity.uid, ok, message, payload)
        except Exception as e:
            self.log("  ! 回报探测结果失败：%r" % (e,))
        # 探测完立刻把状态刷上去，让后台马上看到新界面
        self.poke()

    # -------------------------------------------------------------- 查询

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "beats": self.beats,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "last_at": self.last_at.isoformat(timespec="seconds") if self.last_at else None,
                "interval": self.interval,
                "assignment": self.assignment,
                "switch_needed": self.switch_needed,
                "switch_reason": self.switch_reason,
                "task_paused": self.task_paused,
            }


# ------------------------------------------------------------------ 心跳用的状态盒

class StatusBox:
    """给心跳线程读的共享状态。主流程改、心跳线程读，用锁保护一下。

    刻意做得很小：心跳要报的东西就那么几个 —— 在跑什么、当前哪个账号、模拟器状态。
    """

    def __init__(self, mode: str = "", uid: str = "", host: str = ""):
        self._lock = threading.Lock()
        self._d: Dict[str, Any] = {
            "mode": mode,
            "state": "idle",
            "busy": False,
            "note": "",
            "current": {},
            "device": {},
            "last_run_at": "",
            "extra": {},
        }

    def set(self, **kw: Any) -> None:
        with self._lock:
            self._d.update({k: v for k, v in kw.items() if k in self._d
                            or k in ("current", "device", "extra")})

    def patch_current(self, **kw: Any) -> None:
        with self._lock:
            cur = dict(self._d.get("current") or {})
            cur.update(kw)
            self._d["current"] = cur

    def patch_device(self, **kw: Any) -> None:
        with self._lock:
            dev = dict(self._d.get("device") or {})
            dev.update(kw)
            self._d["device"] = dev

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._d)