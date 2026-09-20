# -*- coding: utf-8 -*-
"""纯标准库的 WebSocket 客户端（RFC 6455）。

**为什么不用现成的库**：客户端脚本的原则是「拷贝到任何一台 Windows 机器、
不装任何额外依赖就能跑」（见 `stzb/cloud.py` 的模块说明）。`websocket-client`
之类虽然是纯 Python，但仍然要求先 `pip install`；而这个文件的全部依赖是
`socket / ssl / select / hashlib / base64 / secrets` —— 标准库，一定在。

**实现范围**：只做我们真正用得到的部分，但需要的地方都不含糊：
  · 握手（`Sec-WebSocket-Accept` 是**逐字节核对**，不是「有这个头就行」）
  · 文本 / 二进制 / 关闭 / ping / pong 五种操作码
  · **分片**（continuation 帧）与长度三档（7 位 / 16 位 / 64 位）
  · **客户端必须掩码**（RFC 硬性要求）
  · 控制帧插队（读到 ping 就地回 pong，不打扰上层的分片组装）
  · 超大帧拦截（默认 8MB 上限，防止对端塞一个畸形长度把我们撑爆）
  · 握手后残留字节的缓冲（服务端很可能把第一帧和 101 响应一起发过来）

**不做**：扩展协商（`permessage-deflate` 等我们没开）、`Sec-WebSocket-Protocol`
子协议、代理（CONNECT 隧道）。都没用到，加了只会多出没人测的代码路径。

------------------------------------------------------------------
★ 一条必须遵守的规矩：**帧没读全，绝不消费缓冲区**
------------------------------------------------------------------

长连接上我们要一边收消息、一边做别的事（发保活、推状态），所以 `recv()`
必须能「等一小会儿没消息就先返回」。于是就有了这个陷阱：

    假设一帧是这样到达的 —— 第一次 recv 只拿到前 4 个字节（帧头还没读完），
    如果此时把已读的字节从缓冲区里拿走，下次就会从**帧的中间**开始解析，
    把负载当帧头、把长度字段当操作码 —— 结果是一串完全莫名其妙的错误，
    而且**偶发**：取决于操作系统这次把多少字节塞进同一个 TCP 段。

所以本模块的接收一律遵循：

    缓冲区 `_buf` 只增不减，直到**完整的一帧**躺在里面；
    `_try_parse()` 在帧不完整时返回 None 且**一个字节都不动**，
    超时/中断后下次重新从头解析 —— 天然幂等，不存在错位。

这条规矩是本模块最容易被"优化掉"的地方（看起来多读了几个字节很浪费），
**请务必保留**。`_try_parse()` 上面有对应的注释。

**这条连接上的业务协议**（与 `server/app/realtime.py` 对齐）见 `stzb/realtime.py`。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# 五种操作码
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_TIMEOUT = 15.0
# 单帧上限：服务端推的「完整状态」含角色任务计划也只有几 KB。
# 8MB 是防畸形/恶意长度用的天花板，不是容量目标。
DEFAULT_MAX_PAYLOAD = 8 * 1024 * 1024
_RECV_CHUNK = 65536


class WSError(RuntimeError):
    """握手失败、协议被破坏、连接不可用。"""


class WSClosed(WSError):
    """对端发来了关闭帧。上层据此决定重连。"""

    def __init__(self, code: int = 0, reason: str = ""):
        super().__init__("WebSocket 已关闭（code=%s%s）"
                         % (code, ("，原因：%s" % reason) if reason else ""))
        self.code = code
        self.reason = reason


def _ssl_ctx() -> ssl.SSLContext:
    """复用 cloud 模块那套证书逻辑：优先 certifi，其次系统信任库。

    刻意**不**降级成「不校验证书」—— 长连接上会跑任务指令，
    中间人插进来等于把机器交出去。
    """
    from .cloud import _ssl_context
    return _ssl_context()


class WebSocket:
    """一条 WebSocket 连接。**非线程安全**：一个实例只能被一个线程用。

    典型用法（见 `stzb/realtime.py`）：

        ws = WebSocket(url, headers={"X-Agent-Token": tok}).connect()
        ws.send_json({"type": "ping"})
        msg = ws.recv_json(timeout=0.5)      # 超时会抛 TimeoutError，可反复调用
    """

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_payload: int = DEFAULT_MAX_PAYLOAD,
                 ssl_ctx: Optional[ssl.SSLContext] = None) -> None:
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WSError("只支持 ws:// 与 wss://，收到的是 %r" % url)
        if not u.hostname:
            raise WSError("URL 里没有主机名：%r" % url)
        self.secure = (u.scheme == "wss")
        self.host = u.hostname
        self.port = int(u.port or (443 if self.secure else 80))
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.path = path
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.max_payload = int(max_payload)
        self._ssl_ctx = ssl_ctx

        self._sock: Optional[socket.socket] = None
        # 只增不减，直到完整的一帧躺进来（见模块文档里那条规矩）
        self._buf = bytearray()
        self._closed = False
        self._close_sent = False
        # 发送锁。本模块整体是「一个实例一个线程」用的，但上层有两种情况会
        # 从别的线程发起发送（收尾时抢着推最后一帧状态）。没有这把锁，
        # 两次 sendall 的字节会交错，对端解析出一堆乱码 —— 偶发，极难查。
        self._send_lock = threading.Lock()
        # 分片组装的中间态：只在一帧**完整**解析出来之后才会被改动
        self._frag_op: Optional[int] = None
        self._frag: List[bytes] = []
        self.bytes_recv = 0
        self.frames_recv = 0

    # ------------------------------------------------------------ 建立 / 关闭

    def connect(self) -> "WebSocket":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = self._ssl_ctx or _ssl_ctx()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(self.timeout)
        self._sock = sock

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_hdr = self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port)
        lines = [
            "GET %s HTTP/1.1" % self.path,
            "Host: %s" % host_hdr,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            lines.append("%s: %s" % (k, v))
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            head = self._read_handshake_head()
        except Exception:
            self._hard_close()
            raise
        self._verify_handshake(head, key)
        return self

    def _read_handshake_head(self) -> bytes:
        """读到 `\\r\\n\\r\\n`。多读进来的字节留在 _buf（那是第一帧的开头）。"""
        while True:
            i = self._buf.find(b"\r\n\r\n")
            if i >= 0:
                head = bytes(self._buf[:i + 4])
                del self._buf[:i + 4]
                return head
            if len(self._buf) > 64 * 1024:
                raise WSError("握手响应头异常大（>64KB），多半不是 WebSocket 服务")
            self._fill(self.timeout)

    def _verify_handshake(self, head: bytes, key: str) -> None:
        text = head.decode("latin-1")
        first = text.split("\r\n", 1)[0]
        if " 101" not in first:
            # 401/403/404 都会走到这里。把首行原样带出去，方便一眼看出
            # 是反代没配 Upgrade、还是令牌不对、还是路径写错了。
            raise WSError("服务端拒绝了 WebSocket 升级：%s" % first.strip())
        got = ""
        for line in text.split("\r\n")[1:]:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.strip().lower() == "sec-websocket-accept":
                got = v.strip()
                break
        # ★ 逐字节核对，不是「有这个头就行」：这一条能挡住「其实升级到了别的
        #   协议上」和「中间人替换了响应」两种情况。
        expect = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if got != expect:
            raise WSError("Sec-WebSocket-Accept 校验失败（期望 %s，收到 %r）" % (expect, got))

    @property
    def connected(self) -> bool:
        return bool(self._sock) and not self._closed

    def close(self, code: int = 1000, reason: str = "") -> None:
        """发关闭帧并关掉 socket。**失败不抛异常** —— 它总会出现在 finally 里。"""
        if self._sock and not self._close_sent and not self._closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
                self._close_sent = True
            except Exception:
                pass
        self._hard_close()

    def _hard_close(self) -> None:
        self._closed = True
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------ 底层收发

    def _fill(self, timeout: Optional[float]) -> None:
        """再读一块数据进缓冲区。超时抛 `TimeoutError`，断连抛 `WSError`。"""
        if self._sock is None:
            raise WSError("连接已关闭")
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(_RECV_CHUNK)
        except socket.timeout:
            raise                                     # 交给上层判断（多半是"先干别的"）
        except OSError as e:
            self._hard_close()
            raise WSError("接收失败：%r" % (e,))
        if not chunk:
            self._hard_close()
            raise WSError("连接被对端关闭")
        self._buf += chunk
        self.bytes_recv += len(chunk)

    def _try_parse(self) -> Optional[Tuple[bool, int, bytes]]:
        """从缓冲区里试解析**一整帧**。不完整 → 返回 None，且**一个字节都不动**。

        ★★ 就是这里，「不完整不动缓冲区」是刻意的。详见模块文档顶部那条规矩：
           一旦在帧没读全时就 `del self._buf[:n]`，下次会从帧的中间开始解析，
           症状是偶发的乱码/未知操作码，且随操作系统的分包行为变化 ——
           极难复现。返回 None 让上层「再等等」，代价只是把已有的字节重解析一遍，
           一帧最多几百字节，完全不值得为这点开销冒上面的风险。
        """
        b = self._buf
        if len(b) < 2:
            return None
        b0, b1 = b[0], b[1]
        fin = bool(b0 & 0x80)
        rsv = b0 & 0x70
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if rsv:
            raise WSError("对端使用了未协商的扩展位（RSV=0x%x）" % rsv)

        off = 2
        if ln == 126:
            if len(b) < off + 2:
                return None
            ln = struct.unpack("!H", bytes(b[off:off + 2]))[0]
            off += 2
        elif ln == 127:
            if len(b) < off + 8:
                return None
            ln = struct.unpack("!Q", bytes(b[off:off + 8]))[0]
            off += 8
        # ★ 长度上限要在「等后续字节」**之前**判：否则对端报一个 2^63 的长度，
        #   我们就会傻等到天荒地老。
        if ln > self.max_payload:
            raise WSError("单帧过大（%d 字节 > 上限 %d）" % (ln, self.max_payload))

        mask = b""
        if masked:
            if len(b) < off + 4:
                return None
            mask = bytes(b[off:off + 4])
            off += 4
        if len(b) < off + ln:
            return None                              # ← 不完整：原样返回，不动 _buf
        data = bytes(b[off:off + ln])
        if masked:
            data = bytes(x ^ mask[i & 3] for i, x in enumerate(data))
        del self._buf[:off + ln]                     # 到这里才消费
        self.frames_recv += 1
        return fin, opcode, data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WSError("连接已关闭，发不出去")
        n = len(payload)
        head = bytearray([0x80 | opcode])            # FIN=1（我们不分片发送）
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        # ★ 掩码是**客户端必须做**的（RFC 6455 §5.3）。不掩码的客户端在规范
        #   服务器上会被直接断开，而且报错信息很难懂。
        mask = secrets.token_bytes(4)
        head += mask
        masked = bytes(x ^ mask[i & 3] for i, x in enumerate(payload))
        try:
            with self._send_lock:                 # 见 __init__ 里的说明
                self._sock.sendall(bytes(head) + masked)
        except OSError as e:
            self._hard_close()
            raise WSError("发送失败：%r" % (e,))

    # ------------------------------------------------------------ 收发

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(OP_BIN, bytes(data))

    def send_json(self, obj: Any) -> None:
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def recv(self, timeout: Optional[float] = None) -> Tuple[int, bytes]:
        """收一条**完整**消息，返回 (操作码, 负载)。

        · `timeout` 秒内没有完整消息 → 抛 `TimeoutError`（**可以安全地反复调用**，
          缓冲区里的半帧不会丢，下次接着解析）
        · 自动回 pong（收到 ping 时）—— 调用方不用管保活
        · 自动组装分片，返回拼好的整条消息
        · 收到关闭帧 → 回一个关闭帧并抛 `WSClosed`
        """
        deadline = None if timeout is None else time.time() + float(timeout)
        while True:
            parsed = self._try_parse()
            if parsed is None:
                if deadline is None:
                    self._fill(None)
                else:
                    remain = deadline - time.time()
                    if remain <= 0:
                        raise TimeoutError("等待服务端消息超时（%.1fs 内没有完整消息）"
                                           % timeout)
                    self._fill(remain)
                continue

            fin, opcode, data = parsed

            # ---- 控制帧可以插在分片中间，就地处理，不打扰分片组装 ----
            if opcode == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code, reason = 0, ""
                if len(data) >= 2:
                    code = struct.unpack("!H", data[:2])[0]
                    reason = data[2:].decode("utf-8", "replace")
                if not self._close_sent:
                    try:
                        self._send_frame(OP_CLOSE, data[:2])
                        self._close_sent = True
                    except Exception:
                        pass
                self._hard_close()
                raise WSClosed(code, reason)

            # ---- 数据帧 ----
            if opcode == OP_CONT:
                if self._frag_op is None:
                    raise WSError("收到 continuation 帧，但前面没有未完成的分片")
                self._frag.append(data)
            elif opcode in (OP_TEXT, OP_BIN):
                if self._frag_op is not None:
                    raise WSError("上一条分片消息还没结束，就来了新的数据帧")
                self._frag_op = opcode
                self._frag = [data]
            else:
                raise WSError("不认识的操作码 0x%x" % opcode)

            if not fin:
                continue                                 # 还有后续分片，继续攒
            op, parts = self._frag_op, self._frag
            self._frag_op, self._frag = None, []
            return int(op or OP_TEXT), b"".join(parts)

    def recv_text(self, timeout: Optional[float] = None) -> str:
        op, data = self.recv(timeout=timeout)
        return data.decode("utf-8", "replace")

    def recv_json(self, timeout: Optional[float] = None) -> Any:
        txt = self.recv_text(timeout=timeout)
        try:
            return json.loads(txt)
        except Exception as e:
            raise WSError("收到的不是合法 JSON：%r（原文前 200 字：%s）" % (e, txt[:200]))


# ------------------------------------------------------------------ 小工具

def parse_ws_url(base: str) -> str:
    """把后端 base_url（http/https）换成对应的 WebSocket 地址。

    http://127.0.0.1:8000  →  ws://127.0.0.1:8000/api/agent/ws
    https://example.com    →  wss://example.com/api/agent/ws

    已经是 ws/wss 的原样保留（方便临时指向别的路径调试）。
    """
    u = urlsplit((base or "").strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(u.scheme, "")
    if not scheme:
        raise WSError("看不懂的 base_url：%r" % base)
    prefix = (u.path or "").rstrip("/")
    return "%s://%s%s/api/agent/ws" % (scheme, u.netloc, prefix)
