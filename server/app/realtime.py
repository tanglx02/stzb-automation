# -*- coding: utf-8 -*-
"""实时通道（WebSocket）—— 让后端**主动**把东西推给采集端，而不是等它来问。

**为什么要有它**：原来的心跳是「客户端每 30 秒来问一次」，于是管理端点下
「立刻跑一轮」之后，最坏要等 30 秒客户端才知道。实时通道把这段延迟压到毫秒级。

------------------------------------------------------------------
两条硬规矩（改动这个模块前请先读完）
------------------------------------------------------------------

**规矩一：数据库始终是唯一事实来源。**

实时通道只负责「通知」，绝不负责「存东西」。待领任务仍在 `run_requests` 表，
一次性指令仍在 `clients.probe_json`，配置仍在 `config` 表。推送的消息只带一句
「有新东西了，你来看看」，客户端收到后照常走原来的 HTTP 接口来拉。

这条规矩换来两个好处：
  · **连接断了不会丢任务**，只会慢一点（退回心跳节奏）；
  · 以后想换传输层（长轮询、gRPC、消息队列）**一行业务代码都不用动**。

**规矩二：心跳必须保留。**

WS 会被很多东西挡掉：企业防火墙掐非标准 Upgrade、反代没配 `Upgrade`/`Connection`
透传、杀软拦长连接、NAT 网关掐空闲连接……所以**绝不能**把「推不到」变成「跑不了」。
客户端侧的降级逻辑见 `stzb/realtime.py`：WS 连不上就退回 HTTP 心跳，
连上之后也还有心跳兜底。

------------------------------------------------------------------
跨线程推送是怎么做的
------------------------------------------------------------------

管理端的路由（`routes_ui.py`）是**同步函数**（FastAPI 把它们丢在线程池里跑），
而 WebSocket 是**协程**。所以推送不能直接 `await`，也不能直接往 `asyncio.Queue`
里 `put`（`asyncio.Queue` 不是线程安全的）。

做法是：在 lifespan 里记住事件循环对象，推送时用
`loop.call_soon_threadsafe(queue.put_nowait, msg)` —— 这是官方指定的
「从别的线程往事件循环里塞回调」的姿势。每条连接自带一个队列，由它自己的
写协程消费。这样既不会阻塞管理端的请求，也不会踩线程安全的坑。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import db, security, settings

log = logging.getLogger("stzb.realtime")

# 客户端多久没发任何帧就认为这条连接死了。
# 客户端每 WS_PING_INTERVAL 秒发一次 ping，取 4~5 倍做阈值：
# 容得下几次网络抖动，又不至于让死连接一直挂着占位。
WS_IDLE_TIMEOUT = 120
WS_PING_INTERVAL = 25

# 每条连接的待发队列长度。队列满说明对端要么堵了、要么根本没在读 ——
# 这时丢最旧的（数据库才是事实来源，丢通知最多晚一拍）。
QUEUE_MAX = 64

router = APIRouter(prefix="/api/agent", tags=["realtime"])


# ------------------------------------------------------------------ 连接登记

class Conn:
    """一条实时连接。"""

    __slots__ = ("uid", "host", "queue", "peer", "opened_at", "sent", "recv")

    def __init__(self, uid: str, host: str = "", peer: str = "") -> None:
        self.uid = uid
        self.host = host
        self.peer = peer
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.opened_at = time.time()
        self.sent = 0
        self.recv = 0


_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_by_uid: Dict[str, List[Conn]] = {}
_conns: List[Conn] = []


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在 lifespan 里把事件循环记下来 —— 同步代码靠它往协程里塞消息。"""
    global _loop
    _loop = loop
    log.info("实时通道已就绪（事件循环已绑定）")


def unbind_loop() -> None:
    global _loop
    _loop = None


def _register(conn: Conn) -> None:
    with _lock:
        _conns.append(conn)
        _by_uid.setdefault(conn.uid, []).append(conn)


def _unregister(conn: Conn) -> None:
    with _lock:
        try:
            _conns.remove(conn)
        except ValueError:
            pass
        lst = _by_uid.get(conn.uid)
        if lst:
            try:
                lst.remove(conn)
            except ValueError:
                pass
            if not lst:
                _by_uid.pop(conn.uid, None)


def _safe_put(q: "asyncio.Queue", msg: Dict[str, Any]) -> None:
    """往队列里塞一条消息；满了就丢最旧的腾位置。

    ★ 这里**必须**吞掉 QueueFull：这段代码是通过 call_soon_threadsafe 在
      事件循环线程里跑的，异常会冒到 loop 的默认异常处理器里，变成噪音日志，
      而它其实只是「客户端堵住了」这一件正常的事。
    """
    try:
        q.put_nowait(msg)
        return
    except asyncio.QueueFull:
        pass
    try:
        q.get_nowait()
        q.put_nowait(msg)
    except Exception:
        pass


# ------------------------------------------------------------------ 推送接口

def _deliver(uid: str, msg: Dict[str, Any]) -> int:
    """把消息交给该 uid 的所有连接。返回送出去几条。

    ★ 返回 0 是**正常情况**（客户端没连实时通道 → 它会从心跳拿到这份状态），
      所以调用方**不要**把 0 当成错误去记 warn 日志，否则离线客户端会让日志刷屏。
    """
    uid = (uid or "").strip()
    loop = _loop
    if not uid or loop is None or not loop.is_running():
        return 0
    with _lock:
        targets = list(_by_uid.get(uid) or ())
    if not targets:
        return 0
    ok = 0
    for conn in targets:
        try:
            loop.call_soon_threadsafe(_safe_put, conn.queue, msg)
            ok += 1
        except RuntimeError:
            # 事件循环正在关闭 —— 正常退出路径，不是错误
            break
    return ok


def notify_uid(uid: str, what: str, **extra: Any) -> int:
    """通知某台客户端「有新东西了，来拉一下」。

    what 取值：
      · "jobs"     —— 有待领任务（客户端收到后立刻 GET /api/agent/jobs）
      · "command"  —— 有待执行的一次性指令（探测 / 强制切换）
      · "config"   —— 配置变了（客户端按需重拉 /api/agent/config）
      · "state"    —— 服务端认为客户端该重新同步一次状态（附 state 负载）
      · "refresh"  —— 泛化的「来刷新一下」（队列拥堵时的兜底）
    """
    msg = {"type": "notify", "what": what, "at": db.now()}
    msg.update(extra)
    return _deliver(uid, msg)


def notify_client(client_id: int, what: str, **extra: Any) -> int:
    """按客户端主键推送（管理端手里通常只有 id）。"""
    try:
        cli = db.client_get(int(client_id))
    except Exception:
        return 0
    if not cli:
        return 0
    return notify_uid(cli.get("uid") or "", what, **extra)


def notify_all(what: str, **extra: Any) -> int:
    """给所有在线客户端推（改配置、改任务模式这类全局动作）。"""
    with _lock:
        uids = list(_by_uid.keys())
    if not uids:
        return 0
    # ★ 消息体只拼一次再复制给各条连接 —— 别把一个 dict 对象共享出去，
    #   将来谁改了它就会影响所有人。
    base = {"type": "notify", "what": what, "at": db.now()}
    base.update(extra)
    return sum(_deliver(u, dict(base)) for u in uids)


def push_state(cli: Dict[str, Any], status: Optional[Dict[str, Any]] = None) -> int:
    """把「这台客户端现在该收到什么」**主动**推给它。

    ★ 这是把「指派」从拉模式变成推模式的关键：管理端一改客户端的账号/角色，
      这里立刻算一份新状态推过去，客户端无需等下一拍心跳。

    负载形状与 HTTP 心跳的响应**完全一致**（同一个 build_client_state），
    所以客户端两条通道可以用同一段代码处理。
    """
    from .routes_agent import build_client_state   # 就地导入：避免模块级循环
    if not cli:
        return 0
    if status is None:
        status = cli.get("status") or {}
    try:
        state = build_client_state(cli, status)
    except Exception as e:                          # 推送失败绝不能影响管理端操作
        log.warning("算客户端状态失败（uid=%s）：%r", cli.get("uid"), e)
        return 0
    return _deliver(cli.get("uid") or "", {"type": "state", "state": state})


def push_state_to_client(client_id: int) -> int:
    try:
        cli = db.client_get(int(client_id))
    except Exception:
        return 0
    return push_state(cli) if cli else 0


def push_state_for_role(role_id: int, account_id: Optional[int] = None) -> int:
    """给「被指派到这个角色（或这个账号）」的客户端推新状态。

    角色改了任务模式 / 启停之后，客户端该立刻知道 —— 否则它要等到下一拍心跳
    才拿到新的 task_plan：管理端刚点了「暂停」，那边还在跑，很容易被当成没生效。

    匹配规则（保守，宁多推勿漏推）：
      · 客户端的 role_id 正好是这个角色 → 推；
      · 客户端只指派了账号、没指定角色（即「这个账号下所有角色都归它」）
        且账号就是这个角色的父账号 → 也推。
    多推一份状态是无害的（同一份内容重复下发），漏推才会让操作看起来失灵。
    """
    hits: List[Dict[str, Any]] = []
    try:
        rows = db.client_list()
    except Exception:
        return 0
    for cli in rows:
        rid = cli.get("role_id")
        aid = cli.get("account_id")
        if rid and int(rid) == int(role_id):
            hits.append(cli)
        elif account_id and aid and not rid and int(aid) == int(account_id):
            hits.append(cli)
    return sum(push_state(c) for c in hits)


# ------------------------------------------------------------------ 状态查询

def stats() -> Dict[str, Any]:
    """给控制台看的一点点统计。"""
    with _lock:
        conns = list(_conns)
        uids = sorted(_by_uid.keys())
    now = time.time()
    return {
        "loop_bound": bool(_loop and _loop.is_running()),
        "conns": len(conns),
        "uids": uids,
        "detail": [{"uid": c.uid, "host": c.host, "peer": c.peer,
                    "age_seconds": int(now - c.opened_at),
                    "sent": c.sent, "recv": c.recv} for c in conns],
        "idle_timeout": WS_IDLE_TIMEOUT,
        "ping_interval": WS_PING_INTERVAL,
    }


def is_connected(uid: str) -> bool:
    """某台客户端此刻有没有活着的实时连接。

    ⚠️ 这是**瞬时**状态，只能用来提示「推送会很快」或「要等下一拍心跳」，
    **绝不能**拿它当「在线/离线」判据 —— 在线与否始终看 last_seen 的年龄
    （见 db._row_client 里的 online 字段），否则防火墙一掐 Upgrade，
    满屏客户端就会被误报成离线。
    """
    with _lock:
        return bool(_by_uid.get((uid or "").strip()))


# ------------------------------------------------------------------ 端点

def _token_of(ws: WebSocket) -> str:
    tok = ws.headers.get("x-agent-token") or ""
    if not tok:
        auth = ws.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return tok


@router.websocket("/ws")
async def agent_ws(websocket: WebSocket) -> None:
    """采集端的长连接。

    **鉴权**：只用请求头（`X-Agent-Token` 或 `Authorization: Bearer`）。
    刻意**不支持** `?token=` —— 查询串会进 uvicorn 的访问日志，
    等于把令牌定期抄进日志文件。

    **握手协议**（客户端侧实现在 stzb/ws.py，帧编解码见 stzb/realtime.py）：
      连接建立 → 服务端立刻推一条 `hello`（含完整状态）
      之后：客户端发 `state`（状态变了才发）→ 服务端回 `state`
            客户端发 `ping`（每 25 秒保活）→ 服务端回 `pong`
            服务端有东西 → 主动推 `notify` / `state`
    """
    if not security.check_agent_token(_token_of(websocket)):
        # 4401 是自定义码：客户端据此知道「令牌不对」，别去无脑重连
        await websocket.close(code=4401, reason="invalid agent token")
        db.event("warn", "realtime", "实时通道令牌校验失败")
        return

    uid = (websocket.headers.get("x-agent-uid") or "").strip()
    host = (websocket.headers.get("x-agent-host") or "").strip()
    version = (websocket.headers.get("x-agent-version") or "").strip()
    if not uid:
        # 没有稳定标识就不让占着连接：否则「谁连上来了」根本无从归属
        await websocket.close(code=4400, reason="missing X-Agent-Uid")
        return

    await websocket.accept()
    peer = ""
    try:
        peer = "%s:%s" % (websocket.client.host, websocket.client.port) \
            if websocket.client else ""
    except Exception:
        pass

    # 连上就算「报到」：刷新 last_seen，并登记客户端（新机器第一条就是这条通道）
    try:
        cli = db.client_upsert(uid, host=host, agent_version=version, ip=peer.split(":")[0])
    except Exception as e:
        log.warning("实时通道登记客户端失败（uid=%s）：%r", uid, e)
        cli = None
    if not cli:
        await websocket.close(code=4400, reason="cannot register client")
        return

    conn = Conn(uid=uid, host=host, peer=peer)
    _register(conn)
    db.event("info", "realtime", "实时通道已连接：%s（%s）" % (cli.get("name") or uid, peer))
    log.info("WS 连接建立 uid=%s peer=%s（当前 %d 条）", uid, peer, stats()["conns"])

    try:
        # ---- 1. 先把完整状态推过去，客户端不必再发一次心跳来「拉」----
        await _send_state(websocket, conn, cli, note="hello")

        # ---- 2. 读 / 写两条协程并行 ----
        writer = asyncio.create_task(_writer(websocket, conn))
        try:
            await _reader(websocket, conn, cli)
        finally:
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.info("WS 连接异常结束 uid=%s：%r", uid, e)
    finally:
        _unregister(conn)
        db.event("info", "realtime",
                 "实时通道已断开：%s（收 %d / 发 %d）" % (uid, conn.recv, conn.sent))
        log.info("WS 连接关闭 uid=%s（剩 %d 条）", uid, stats()["conns"])


async def _send(ws: WebSocket, conn: Conn, msg: Dict[str, Any]) -> None:
    await ws.send_json(msg)
    conn.sent += 1


async def _send_state(ws: WebSocket, conn: Conn, cli: Dict[str, Any],
                      *, note: str = "") -> None:
    """把当前状态发给客户端。载荷与 HTTP 心跳响应同形。

    注意 cli 可能是「刚登记、还没上报过状态」的新行 —— 那时 status 为空字典，
    build_client_state 会给出「状态未知、先不用切」的保守结论，正合适。
    """
    from .routes_agent import build_client_state
    try:
        state = build_client_state(cli, cli.get("status") or {})
    except Exception as e:
        state = {"ok": False, "error": repr(e)}
    await _send(ws, conn, {"type": "state", "note": note, "state": state,
                           "ws_ping_interval": WS_PING_INTERVAL})


async def _writer(ws: WebSocket, conn: Conn) -> None:
    """把队列里的消息发出去。"""
    while True:
        msg = await conn.queue.get()
        await _send(ws, conn, msg)


async def _reader(ws: WebSocket, conn: Conn, cli: Dict[str, Any]) -> None:
    """收客户端发来的帧。

    超时策略：`WS_IDLE_TIMEOUT` 秒内一条帧都没有就断开。
    客户端每 25 秒发一次 ping，正常情况下永远不会触发这个超时；
    一旦触发，说明这条 TCP 已经死了（拔网线、NAT 掉表），
    早点关掉让客户端重连，比挂着一个「看起来在线」的死连接好。
    """
    while True:
        try:
            raw = await asyncio.wait_for(ws.receive_json(), timeout=WS_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("WS 空闲超时，关闭 uid=%s", conn.uid)
            await ws.close(code=4408, reason="idle timeout")
            return

        conn.recv += 1
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type") or ""

        if kind == "ping":
            await _send(ws, conn, {"type": "pong", "t": raw.get("t"),
                                   "server_time": db.now()})
            _touch(conn, cli)
            continue

        if kind == "state":
            # 客户端把状态报上来了（原本走 POST /api/agent/ping 的那份载荷）
            status = {k: raw.get(k) for k in
                      ("mode", "state", "busy", "note", "current", "device",
                       "last_run_at", "extra")}
            status["note"] = str(status.get("note") or "")[:300]
            try:
                cli = db.client_upsert(conn.uid, host=conn.host,
                                       agent_version=raw.get("version") or "",
                                       status=status) or cli
            except Exception as e:
                log.warning("WS 状态落库失败 uid=%s：%r", conn.uid, e)
            await _send_state(ws, conn, cli, note="ack")
            continue

        # 不认识的类型：不回错，免得老客户端因为一条未知消息就断开
        log.info("WS 收到未知消息类型 uid=%s type=%r", conn.uid, kind)


def _touch(conn: Conn, cli: Dict[str, Any]) -> None:
    """收到保活 ping 也要刷新 last_seen。

    ★ 只连不断是不够的 —— 「在线」的判据始终是 last_seen 的年龄
      （db._row_client 的 online 字段）。如果 ping 不刷新 last_seen，
      一个连着实时通道的客户端会被算成离线。
    """
    try:
        db.client_upsert(conn.uid, host=conn.host)
    except Exception as e:
        log.warning("WS 保活刷新 last_seen 失败 uid=%s：%r", conn.uid, e)
