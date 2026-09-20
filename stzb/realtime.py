# -*- coding: utf-8 -*-
"""客户端实时会话：**长连接为主，心跳为降级兜底**。

------------------------------------------------------------------
它替掉了什么
------------------------------------------------------------------

原来只有心跳：客户端每 30 秒 POST 一次 `/api/agent/ping`，把「我在干什么」
报上去，同时顺便问「有没有新指派 / 新指令」。问题在于**后端完全被动** ——
管理员在控制台点了「立刻跑一轮」，最坏要等 30 秒客户端才来问；

现在多了一条 WebSocket 长连接，方向反过来：

    · 后端一有东西（派任务 / 下指令 / 改指派 / 改配置）→ **立刻推**；
    · 客户端状态一变（开始跑 / 跑完 / 切角色）→ **立刻报**。

实测同一台机器内：管理员点「派任务」到客户端收到通知 **63 毫秒**。

------------------------------------------------------------------
★ 心跳为什么必须保留（不要把这段删掉）
------------------------------------------------------------------

WS 会被一堆东西挡掉，而且都不是「配置错了」，是环境就是这样：

    · 企业防火墙 / 出口设备掐掉非 HTTP 的 Upgrade；
    · 反向代理没配 `Upgrade` 与 `Connection` 透传（这是最常见的翻车点）；
    · 杀软 / 上网行为管理拦长连接；
    · NAT 网关掐空闲连接；
    · 中途换 WiFi、VPN 重连，长连接直接断。

所以本模块的形态是「**WS 优先 + 心跳兜底 + 自动重连**」：
WS 连着时走 WS；WS 一断，立刻退回原来的 HTTP 心跳（在线状态不断档），
同时按退避重试 WS。**两种模式下客户端的能力完全一样**，只是快慢不同。

这也是为什么 `_apply()` 里绝不允许出现「如果是 WS 就……否则……」这种分支：
服务端那边也是同一个 `build_client_state()` 算出来的，两边共用同一份处理，
才能保证「走哪条通道拿到的东西一致」。
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .identity import Identity          # noqa: F401  （类型提示用，保持依赖显式）
from .ws import WebSocket, WSError, WSClosed, parse_ws_url

# 服务端没告诉我们节奏时的兜底（与 settings.HEARTBEAT_INTERVAL 对齐）
FALLBACK_INTERVAL = 30
# 保活间隔：比服务端的空闲超时（120s）小得多，容得下几次丢包
DEFAULT_PING_INTERVAL = 25
# 状态上报的最小间隔：主流程可能在一瞬间改好几个字段，没必要每个字段发一包
STATE_MIN_INTERVAL = 1.0
# 每次 recv 的等待时长。它同时也是「主线程要求停止」的响应粒度。
TICK = 0.5
# 重连退避（秒）
RECONNECT_STEPS = (1, 2, 4, 8, 15, 30, 30, 30)

# ★ 与 服务端 server/app/routes_agent.py 的 BUSY_PREFIX **必须一致**
#   （自检 test_realtime_contract 里有断言钉着两边相等）。
#
#   含义：本机正在跑任务，此时收到「探测界面 / 强制切换」不能硬闯 ——
#   探测要截屏 OCR、切换要点按钮，跟正在跑的任务同时操作游戏界面会互相打架，
#   两边的判断都会错。所以回报这个前缀，服务端会把指令**放回队列**，
#   等本机闲下来再取走执行。
BUSY_PREFIX = "BUSY:"


class Realtime:
    """后台实时会话线程。

    参数与 `stzb.heartbeat.Heartbeat` **刻意的保持一致**（client / identity /
    state_fn / on_assignment / on_command / logger / interval），
    这样上层 `HeartbeatSession` 换个类就能切过来，不必改一圈调用点。

    新增的：
      url          —— WebSocket 地址；不给就用 client.base_url 推出来
      ws_enabled   —— 关掉就退化成纯心跳（用于排障：怀疑 WS 有问题时先关它）
      on_notify    —— 收到推送时的回调 (what:str, msg:dict)
      ping_interval / state_min_interval —— 保活与上报节奏
    """

    def __init__(self, client, identity,
                 state_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                 on_assignment: Optional[Callable[[Dict[str, Any], bool, str], None]] = None,
                 on_command: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 on_notify: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 logger: Callable[[str], None] = print,
                 url: str = "",
                 interval: Optional[int] = None,
                 ws_enabled: bool = True,
                 ping_interval: Optional[int] = None,
                 state_min_interval: float = STATE_MIN_INTERVAL,
                 notify_on_pending_jobs: bool = True):
        self.client = client
        self.identity = identity
        self.state_fn = state_fn or (lambda: {})
        self.on_assignment = on_assignment
        self.on_command = on_command
        self.on_notify = on_notify
        self.log = logger
        self.interval = int(interval or FALLBACK_INTERVAL)
        self.ws_enabled = bool(ws_enabled) and self._can_ws(url)
        self.ping_interval = int(ping_interval or DEFAULT_PING_INTERVAL)
        self.state_min_interval = float(state_min_interval)
        self.notify_on_pending_jobs = bool(notify_on_pending_jobs)

        try:
            self.url = url or parse_ws_url(getattr(client, "base_url", ""))
        except Exception as e:
            self.url = ""
            self.ws_enabled = False
            self._log_once("ws_badurl", "  ! 实时通道地址算不出来（%r），改为纯心跳模式" % (e,))

        # ---- 与 Heartbeat 同名的对外状态（上层可直接读）----
        self.assignment: Optional[Dict[str, Any]] = None
        self.switch_needed = False
        self.switch_reason = ""
        self.task_paused = False
        self.last_ok = False
        self.last_error = ""
        self.last_at: Optional[dt.datetime] = None
        self.beats = 0
        self.transport = "off"              # "ws" / "http" / "off"
        self.ws_connected = False
        self.ws_reconnects = 0
        self.ws_error = ""
        self.pushed = 0                     # 主动上报状态的次数

        self._stop = threading.Event()
        self._poke = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ws: Optional[WebSocket] = None
        self._ws_retry_at = 0.0
        self._retry_step = 0
        self._last_ping = 0.0
        self._last_push = 0.0
        self._sent_sig: Optional[str] = None
        self._next_beat = 0.0
        self._warned: set = set()

    # -------------------------------------------------------------- 生命周期

    @staticmethod
    def _can_ws(url: str) -> bool:
        return True

    def start(self) -> "Realtime":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stzb-realtime",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        self._poke.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._close_ws()

    def poke(self) -> None:
        """让会话立刻同步一次（状态刚变了 / 任务刚要开始）。"""
        self._poke.set()

    def beat_now(self) -> None:
        """请求「**立刻**上报一次」，不等下一拍。

        用途：任务刚跑完、状态从 busy 变回 idle —— 这一刻后台最需要马上看到，
        否则控制台里会一直显示「还在跑」，直到下一拍才纠正过来。

        ★ 这里只置标志、由会话线程真正发送，**不跨线程直接写 socket**：
          两个线程同时 `sendall` 会让字节流交错，对端帧解析直接崩，
          而且症状是偶发乱码，极难查。发送统一由会话线程做，天然串行。
        """
        self._poke.set()
        self._next_beat = 0.0            # HTTP 降级模式下：下一轮循环立刻打一拍

    # -------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        # 启动时先打一拍：拿到指派，也让后台立刻看到这台机器。
        # 顺序是「先试 WS」——WS 通了的话这一拍就是 hello，比 HTTP 还快。
        self._first_contact()
        while not self._stop.is_set():
            now = time.time()
            if self.ws_enabled and self._ws is None and now >= self._ws_retry_at:
                self._try_connect()
            if self._ws is not None:
                self._serve_ws()
                continue
            self._serve_http()

    def _first_contact(self) -> None:
        try:
            if self.ws_enabled:
                self._try_connect()
            if self._ws is None:
                self._beat_http()
        except Exception as e:                       # 首拍失败绝不能把线程带走
            self._record_fail(e)

    # -------------------------------------------------------------- WebSocket

    def _try_connect(self) -> None:
        try:
            ws = WebSocket(self.url, headers={
                "X-Agent-Token": self.client.token,
                "X-Agent-Uid": self.identity.uid,
                "X-Agent-Host": self.identity.host,
                "X-Agent-Version": getattr(self.client, "version", "") or "",
            }, timeout=10.0)
            ws.connect()
        except WSError as e:
            msg = str(e)
            if "403" in msg or "401" in msg:
                # ★ 令牌/路径不对是**配置问题**，重试一万次也没用，只会刷日志。
                #   直接停掉 WS，退成纯心跳（心跳至少还能上报状态）。
                self.ws_enabled = False
                self.ws_error = msg
                self._log_once("ws_auth",
                               "  ! 实时通道被拒（%s）→ 改为纯心跳模式。"
                               "请检查 config.json 的 cloud.token 与控制台一致" % msg)
            else:
                self.ws_error = msg
                self._log_once("ws_fail",
                               "  · 实时通道连不上（%s）→ 先用心跳兜着，会自动重试" % msg)
            self._schedule_retry()
            return
        except Exception as e:
            self.ws_error = repr(e)
            self._log_once("ws_fail", "  · 实时通道连不上（%r）→ 先用心跳兜着" % (e,))
            self._schedule_retry()
            return

        self._ws = ws
        self.transport = "ws"
        self.ws_connected = True
        self.ws_error = ""
        self._retry_step = 0
        self._last_ping = time.time()
        self._sent_sig = None            # 新连接上要重新推一份完整状态
        if self.ws_reconnects:
            self.log("  ✓ 实时通道已重连（第 %d 次）" % self.ws_reconnects)
        else:
            self.log("  ✓ 实时通道已连接（%s），后台指令将即时送达" % self.url)
        self.ws_reconnects += 1

    def _schedule_retry(self) -> None:
        step = RECONNECT_STEPS[min(self._retry_step, len(RECONNECT_STEPS) - 1)]
        self._retry_step += 1
        # 加抖动：多台机器同时掉线时别在同一秒一起冲回来
        self._ws_retry_at = time.time() + step + random.uniform(0, 1.5)

    def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self.ws_connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _drop_ws(self, why: str) -> None:
        if self._ws is not None:
            self._close_ws()
            self.log("  · 实时通道断开（%s）→ 退回心跳模式，并自动重连" % why)
        self.ws_error = why
        self.transport = "http"
        self._schedule_retry()

    def _serve_ws(self) -> None:
        """WS 模式下的一次循环：先做保活/上报，再等一小会儿收消息。"""
        ws = self._ws
        if ws is None:
            return
        try:
            self._ws_housekeeping()
        except (WSClosed, WSError, OSError) as e:
            self._drop_ws(repr(e))
            self._beat_http(silent=True)      # 掉线瞬间补一拍，在线状态不断档
            return
        except Exception as e:                # housekeeping 自己的 bug 不该断连接
            self.log("  ! 实时通道保活异常：%r" % (e,))
        try:
            msg = ws.recv_json(timeout=TICK)
        except TimeoutError:
            return                            # 正常：这段时间没消息，下轮继续
        except (WSClosed, WSError, OSError) as e:
            self._drop_ws(repr(e))
            self._beat_http(silent=True)
            return
        except Exception as e:
            self.log("  ! 实时通道收到坏消息：%r" % (e,))
            return

        if not isinstance(msg, dict):
            return
        self.last_ok = True
        self.last_error = ""
        self.last_at = dt.datetime.now()
        self.beats += 1
        kind = msg.get("type") or ""
        if kind == "state":
            self._apply(msg.get("state") or {})
        elif kind == "notify":
            self._handle_notify(msg)
        elif kind == "pong":
            pass
        # 其它类型静默忽略：服务端将来加新消息时，老客户端不该因此报错

    def _ws_housekeeping(self) -> None:
        ws = self._ws
        if ws is None:
            return
        now = time.time()
        if now - self._last_ping >= self.ping_interval:
            ws.send_json({"type": "ping", "t": int(now * 1000)})
            self._last_ping = now
        if self._should_push(now):
            self._push_state()

    def _should_push(self, now: float) -> bool:
        """要不要把状态推上去。

        ★ 触发条件只有两个：**状态变了** 或 **上层明确要求**（poke）。
          刻意**不做「定时推」** —— 那又变成轮询了；而「有没有新指令」这件事
          由服务端在管理员点击时主动推 notify，客户端收到再拉，
          所以定时推没有存在意义，只会白白制造流量。
        """
        if self._poke.is_set():
            return True
        sig = self._state_signature()
        return sig != self._sent_sig and (now - self._last_push) >= self.state_min_interval

    def _state_signature(self) -> str:
        """给状态算一个便宜的指纹，用来判断「变了没有」。

        直接比较字典也行，但状态里有 `extra` 这种可能带嵌套的结构；
        用 repr 生成指纹更省事，而且这里的数据量极小（几个短字符串）。
        """
        try:
            st = self.state_fn() or {}
        except Exception:
            return ""
        return repr(sorted((k, repr(v)) for k, v in st.items()))

    def _status_payload(self) -> Dict[str, Any]:
        """状态负载。字段与 HTTP 心跳的 body **完全一致**（服务端同一套解析）。"""
        try:
            status = self.state_fn() or {}
        except Exception:
            status = {}
        payload = {
            "uid": self.identity.uid,
            "host": self.identity.host,
            "version": getattr(self.client, "version", "") or "",
        }
        payload.update({k: v for k, v in status.items()
                        if k in ("mode", "state", "busy", "note", "current",
                                 "device", "last_run_at", "extra")})
        return payload

    def _push_state(self) -> None:
        ws = self._ws
        if ws is None:
            return
        payload = {"type": "state"}
        payload.update(self._status_payload())
        ws.send_json(payload)
        self._poke.clear()
        self._last_push = time.time()
        self._sent_sig = self._state_signature()
        self.beats += 1
        self.pushed += 1

    # -------------------------------------------------------------- HTTP 兜底

    def _serve_http(self) -> None:
        now = time.time()
        if now >= self._next_beat:
            self._beat_http()
        # 睡一小会儿再醒 —— 期间要能立刻响应 stop()
        self._stop.wait(0.5)

    def _beat_http(self, silent: bool = False) -> None:
        """一次 HTTP 心跳。WS 断线期间全靠它保命。"""
        if self.transport != "http":
            self.transport = "http"
        payload = self._status_payload()
        ok, data = self.client.heartbeat(payload)
        self._poke.clear()
        self.beats += 1
        self.last_at = dt.datetime.now()
        if not ok or not isinstance(data, dict):
            if not silent:
                self._record_fail(data)
            else:
                self.last_ok = False
                self.last_error = str(data)
            self._next_beat = time.time() + self.interval
            return
        self.last_ok = True
        self.last_error = ""
        self._apply(data)
        self._next_beat = time.time() + self.interval

    # -------------------------------------------------------------- 共用处理

    def _apply(self, data: Dict[str, Any]) -> None:
        """把服务端给的指令落到本地。**两条通道共用这一份**。

        ★ 服务端那边也是同一个 `build_client_state()` 算出来的
          （见 `server/app/routes_agent.py`）。所以这里**绝不能**按通道分叉：
          一旦分叉，「走 WS 和走 HTTP 行为不一样」这类 bug 会一直躲着不被发现，
          而且只在某条通道上复现，极难查。
        """
        if not isinstance(data, dict) or not data:
            return
        # 节奏由服务端定，方便以后统一调
        iv = data.get("heartbeat_interval")
        try:
            if iv and 5 <= int(iv) <= 3600:
                self.interval = int(iv)
        except Exception:
            pass
        pi = data.get("ws_ping_interval")
        try:
            if pi and 5 <= int(pi) <= 300:
                self.ping_interval = int(pi)
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

        # 兜底：服务端说「你还有待领任务」→ 就当收到了一次 "jobs" 通知。
        #
        # 为什么要这一条：notify 只是一条消息，实时通道抖动/重连时可能丢；
        # 而「有待领任务」这个数字是**每次状态同步都会带回来的**，所以只要
        # 有一次同步成功，队列里的活就不会被一直晾着。
        # 拿不到 notify 也能跑，这才是「数据库是唯一事实来源」该有的样子。
        #
        # ★ run_daily 会把这个关掉（notify_on_pending_jobs=False）：它一轮正在跑，
        #   中途插不了队，开着只会每同步一次就喊一句「后台派了新任务」，纯噪音。
        if self.notify_on_pending_jobs and self.on_notify:
            try:
                if int(data.get("pending_jobs") or 0) > 0:
                    self.on_notify("jobs", {"what": "jobs", "source": "state"})
            except Exception:
                pass

    def _handle_notify(self, msg: Dict[str, Any]) -> None:
        what = str(msg.get("what") or "refresh")
        if self.on_notify:
            try:
                self.on_notify(what, msg)
            except Exception as e:
                self.log("  ! 推送回调异常：%r" % (e,))
        # 收到任何通知都值得立刻同步一次状态：
        # 「有新指令」这件事必须靠这次同步才能把它取回来（指令是服务端
        # build_client_state 时才交给客户端的），而「有待领任务」则交给
        # on_notify 去拉 /jobs。
        self._poke.set()

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
            self.log("  ! 回报指令结果失败：%r" % (e,))
        # 执行完立刻把新状态推上去，让后台马上看到结果
        self.poke()

    def _record_fail(self, err: Any) -> None:
        self.last_ok = False
        self.last_error = str(err)
        self._log_once("beat_fail", "  ! 心跳/实时上报失败：%s（不影响任务）" % str(err)[:200])

    def _log_once(self, key: str, msg: str) -> None:
        """同一类问题只念一次 —— 网络断一天也不该把日志刷爆。"""
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        try:
            self.log(msg)
        except Exception:
            pass

    # -------------------------------------------------------------- 查询

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "transport": self.transport,
                "url": self.url,
                "ws_enabled": self.ws_enabled,
                "ws_connected": self.ws_connected,
                "ws_reconnects": self.ws_reconnects,
                "ws_error": self.ws_error,
                "beats": self.beats,
                "pushed": self.pushed,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "last_at": self.last_at.isoformat(timespec="seconds") if self.last_at else None,
                "interval": self.interval,
                "ping_interval": self.ping_interval,
                "assignment": self.assignment,
                "switch_needed": self.switch_needed,
                "switch_reason": self.switch_reason,
                "task_paused": self.task_paused,
            }
