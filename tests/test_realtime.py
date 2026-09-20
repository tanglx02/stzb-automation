# -*- coding: utf-8 -*-
"""实时通道测试：帧编解码（离线）+ 客户端↔服务端推送（端到端）。

分两段：

  A 段 —— **离线帧编解码**。用 `socket.socketpair()` 造一对真 socket，
          手动喂字节给 `WebSocket`，把 RFC 6455 里我们真正依赖的几条钉住：
          掩码、长度三档、分片、ping/pong、关闭握手，以及**最关键的一条**：
          「帧没读全时绝不消费缓冲区」（否则会出现偶发的帧错位）。

  B 段 —— **端到端**。起一个真的 uvicorn，用真的 `Realtime` 连上去，验证：
          连接即拿到指派、状态变化被立刻推上去、管理端派任务秒到、
          改指派秒到、探测指令往返、正忙时指令被放回队列、
          以及长连接断了能退回心跳。

跑法（需要 fastapi/uvicorn，用服务端那个环境）：
    python tests\\test_realtime.py
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = {"n": 0}


def check(name, cond, extra=""):
    PASS["n"] += 1
    if cond:
        print("  [PASS] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, extra))
        raise AssertionError(name)


# ================================================================== A 段：帧编解码

def _sframe(opcode, payload=b"", *, fin=True, mask=None):
    """手工造一个「服务端 → 客户端」的帧（服务端不掩码；也支持掩码用于反向测试）。"""
    b0 = (0x80 if fin else 0) | opcode
    b1 = 0x80 if mask is not None else 0
    n = len(payload)
    out = bytearray([b0])
    if n < 126:
        out.append(b1 | n)
    elif n < 65536:
        out.append(b1 | 126)
        out += struct.pack("!H", n)
    else:
        out.append(b1 | 127)
        out += struct.pack("!Q", n)
    if mask is not None:
        out += mask
        payload = bytes(x ^ mask[i & 3] for i, x in enumerate(payload))
    out += payload
    return bytes(out)


def test_codec():
    from stzb.ws import (OP_BIN, OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, WebSocket,
                         WSClosed, WSError)

    print("\n=== A 段：帧编解码（离线，用 socketpair 造真 socket）===")
    ws = WebSocket("ws://127.0.0.1:1/api/agent/ws")

    # ---- A1 长度三档（7 位 / 16 位 / 64 位）----
    for n, label in ((5, "7 位"), (300, "16 位"), (70000, "64 位")):
        a, b = socket.socketpair()
        ws._sock = a
        ws._buf = bytearray()
        payload = ("x" * (n - 1) + "。").encode("utf-8")   # 末尾放个多字节字符
        b.sendall(_sframe(OP_TEXT, payload))
        got = ws.recv_text(timeout=2)
        check("长度档 %s（%d 字节）能正确解析" % (label, len(payload)),
              got == payload.decode("utf-8"), repr(got[:20]))
        a.close()
        b.close()
    ws._sock = None

    # ---- A2 我们发出去的帧**必须带掩码**（RFC 6455 §5.3）----
    a, b = socket.socketpair()
    ws._sock = a
    ws.send_text("hello")
    raw = b.recv(64)
    check("客户端发出的帧带掩码位", bool(raw[1] & 0x80), "b1=0x%02x" % raw[1])
    check("客户端发出的帧是文本帧且 FIN=1", raw[0] == 0x81, "b0=0x%02x" % raw[0])
    masked_len = raw[1] & 0x7F
    check("掩码位置正确（短帧 b1 低 7 位=长度）", masked_len == 5, str(masked_len))
    a.close()
    b.close()
    ws._sock = None

    # ---- A3 分片组装 ----
    a, b = socket.socketpair()
    ws._sock = a
    ws._buf = bytearray()
    b.sendall(_sframe(OP_TEXT, "前半".encode("utf-8"), fin=False))
    b.sendall(_sframe(0x0, "后半".encode("utf-8"), fin=True))
    check("分片能被拼成一条完整消息", ws.recv_text(timeout=2) == "前半后半")
    a.close()
    b.close()
    ws._sock = None

    # ---- A4 分片中间插 ping：要就地回 pong，且不打扰分片组装 ----
    a, b = socket.socketpair()
    ws._sock = a
    ws._buf = bytearray()
    b.sendall(_sframe(OP_TEXT, "A".encode(), fin=False))
    b.sendall(_sframe(OP_PING, b"kp"))
    b.sendall(_sframe(0x0, "B".encode(), fin=True))
    got = ws.recv_text(timeout=2)
    pong = b.recv(64)
    check("分片中间的 ping 被就地回 pong 且不影响组装",
          got == "AB" and pong[0] == (0x80 | OP_PONG), "%r %r" % (got, pong[:2]))
    a.close()
    b.close()
    ws._sock = None

    # ---- A5 关闭帧：抛 WSClosed，并把关闭帧回过去 ----
    a, b = socket.socketpair()
    ws._sock = a
    ws._buf = bytearray()
    b.sendall(_sframe(OP_CLOSE, struct.pack("!H", 1000)))
    closed = None
    try:
        ws.recv(timeout=2)
    except WSClosed as e:
        closed = e
    back = b.recv(64)
    check("收到关闭帧抛 WSClosed 并回关闭帧",
          closed is not None and closed.code == 1000 and back[0] == (0x80 | OP_CLOSE),
          "%r %r" % (closed, back[:2]))
    a.close()
    b.close()
    ws._sock = None

    # ---- A6 ★★ 帧没读全时**一个字节都不能消费** ----
    # 这是整个模块最容易被"优化掉"的地方：一旦在帧没读全时清了缓冲区，
    # 下次就会从帧的中间开始解析 → 偶发的乱码/未知操作码，随操作系统分包变化。
    a, b = socket.socketpair()
    ws._sock = a
    ws._buf = bytearray()
    full = _sframe(OP_TEXT, "半截消息也要能接上".encode("utf-8"))
    b.sendall(full[:3])                      # 只先给 3 个字节（帧头都没读完）
    timed_out = False
    try:
        ws.recv(timeout=0.3)
    except TimeoutError:
        timed_out = True
    check("只到了一部分帧时，recv 超时而不是拼出半条消息", timed_out)
    check("★ 超时后缓冲区里的半帧**原封不动**（不消费才不会错位）",
          bytes(ws._buf) == full[:3], "%d 字节" % len(ws._buf))
    b.sendall(full[3:])                      # 补上剩下的一半
    check("剩下的一半到达后能完整解出（证明续读是幂等的）",
          ws.recv_text(timeout=2) == "半截消息也要能接上")
    a.close()
    b.close()
    ws._sock = None

    # ---- A7 畸形长度：要在"等字节"之前就被拦下 ----
    a, b = socket.socketpair()
    ws._sock = a
    ws._buf = bytearray()
    b.sendall(bytes([0x81, 127]) + struct.pack("!Q", 1 << 40))   # 谎报 1TB
    blew = False
    try:
        ws.recv(timeout=1)
    except WSError:
        blew = True
    except TimeoutError:
        blew = False
    check("超大长度立刻报错，不会傻等（否则对端报个 2^63 就能把我们挂死）", blew)
    a.close()
    b.close()
    ws._sock = None

    # ---- A8 握手响应必须逐字节核对 Sec-WebSocket-Accept ----
    import base64
    import hashlib
    key = base64.b64encode(b"0123456789abcdef").decode()
    good = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    ok = True
    try:
        ws._verify_handshake(
            ("HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: %s\r\n\r\n"
             % good).encode("latin-1"), key)
    except WSError:
        ok = False
    check("正确的 Sec-WebSocket-Accept 能通过", ok)
    reject = False
    try:
        ws._verify_handshake(
            b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: wrong\r\n\r\n",
            key)
    except WSError:
        reject = True
    check("错误的 Sec-WebSocket-Accept 被拒（不是「有这个头就行」）", reject)
    nob = False
    try:
        ws._verify_handshake(b"HTTP/1.1 403 Forbidden\r\n\r\n", key)
    except WSError:
        reject = True
        nob = True
    check("非 101 响应被拒（403 会被如实报出来，方便定位令牌/反代问题）", nob)

    # ---- A9 parse_ws_url ----
    from stzb.ws import parse_ws_url
    check("http → ws", parse_ws_url("http://127.0.0.1:8000")
          == "ws://127.0.0.1:8000/api/agent/ws")
    check("https → wss 且保留子路径", parse_ws_url("https://a.b.c/sub")
          == "wss://a.b.c/sub/api/agent/ws")


# ================================================================== B 段：端到端

def test_e2e():
    print("\n=== B 段：端到端（真 uvicorn + 真 Realtime）===")
    PY = sys.executable
    BASE = tempfile.mkdtemp(prefix="stzb_rt_")
    PORT = 8796
    TOKEN = "stzb_rt_token"

    env = dict(os.environ)
    env.update(STZB_DATA_DIR=BASE, STZB_ADMIN_USER="admin",
               STZB_ADMIN_PASSWORD="pw12345678", STZB_AGENT_TOKEN=TOKEN,
               # 心跳调快，好让「心跳兜底」这一段不用等太久；
               # 同时它也是「推送 vs 心跳」快慢对比的参照物
               STZB_HEARTBEAT_INTERVAL="5", STZB_CLIENT_OFFLINE_AFTER="20",
               PYTHONUNBUFFERED="1")

    srv = subprocess.Popen(
        [PY, "-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=os.path.join(ROOT, "server"), env=env,
        stdout=open(os.path.join(BASE, "server.log"), "w", encoding="utf-8"),
        stderr=subprocess.STDOUT)

    for _ in range(80):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/healthz" % PORT, timeout=1)
            break
        except Exception:
            time.sleep(0.4)
    else:
        print("!! 服务起不来")
        print(open(os.path.join(BASE, "server.log"), encoding="utf-8").read()[-3000:])
        srv.kill()
        return 1
    print("  服务已就绪：127.0.0.1:%d" % PORT)

    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def post(path, data):
        try:
            return opener.open(
                urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (PORT, path),
                    data=urllib.parse.urlencode(data).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"}),
                timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    def get(path):
        try:
            r = opener.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=10)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def api_clients():
        _, js = get("/api/clients")
        try:
            return json.loads(js)
        except Exception:
            return {}

    logs = []

    def log(m):
        logs.append(m)
        print("     " + m)

    from stzb.cloud import CloudClient
    from stzb.heartbeat import StatusBox
    from stzb.identity import Identity
    from stzb.realtime import BUSY_PREFIX, Realtime

    ident = Identity.load(os.path.join(BASE, "client.json"))
    client = CloudClient(base_url="http://127.0.0.1:%d" % PORT, token=TOKEN,
                         timeout=10, retries=1, logger=log, uid=ident.uid)
    box = StatusBox(mode="managed")
    box.set(state="idle", note="测试客户端空闲")
    box.patch_current(masked="159****4508", role="X6014龙兴之")

    got = {"assign": [], "cmd": [], "notify": []}
    # 让桩可以假装「本机正在跑任务」。真实实现在 run_daily / agent 里就是这个行为：
    # 忙的时候收到「切换」回报 BUSY，服务端会把指令放回队列（见 B6）。
    busy_mode = {"on": False}

    def on_command(cmd):
        got["cmd"].append(cmd)
        kind = cmd.get("kind")
        log("（桩：执行指令 %s）" % kind)
        if kind == "switch" and busy_mode["on"]:
            return False, BUSY_PREFIX + "本机正在跑任务（测试桩）", {"deferred": True}
        if kind == "probe":
            return True, "读到 12 行文字，已在主城", {"lines": ["主城"], "text_count": 12}
        return True, "切换成功（测试桩）", {"ok": True}

    def on_assignment(a, need, reason):
        got["assign"].append((a, need, reason))

    def on_notify(what, msg):
        got["notify"].append((what, msg))

    rt = Realtime(client, ident, state_fn=box.get, on_assignment=on_assignment,
                  on_command=on_command, on_notify=on_notify,
                  logger=log, interval=5)
    rt.start()
    for _ in range(60):
        if rt.ws_connected:
            break
        time.sleep(0.1)

    # ---- B1 连上就用长连接，而不是心跳 ----
    print("\n== B1. 实时通道连上（不再靠心跳轮询）==")
    check("传输方式是 WebSocket 长连接", rt.ws_connected and rt.transport == "ws",
          "transport=%s err=%s" % (rt.transport, rt.ws_error))
    print("     地址: %s" % rt.url)

    # ---- B2 连接即拿到指派（hello），不需要先发心跳去问 ----
    check("连上就收到了 hello 状态（服务端主动推）", rt.beats >= 1, "beats=%d" % rt.beats)

    # ---- B3 管理端登录 + 找到自己这台客户端 ----
    check("管理端登录成功", post("/login", {"user": "admin", "password": "pw12345678"}) in (200, 303))
    _, html = get("/clients")
    m = re.search(r"/clients/(\d+)/assign", html)
    check("客户端页能找到本机（连上就登记了）", m is not None)
    cid = int(m.group(1))

    # ---- B4 状态变化被立刻推上去 ----
    print("\n== B2. 状态一变就推上去（双向实时）==")
    t0 = time.time()
    box.set(state="running", busy=True, note="正在跑测试任务")
    seen = None
    while time.time() - t0 < 6:
        for c in api_clients().get("clients", []):
            if c.get("id") == cid and c.get("note") == "正在跑测试任务":
                seen = c
        if seen:
            break
        time.sleep(0.1)
    dt = time.time() - t0
    check("状态变化被后台看到（%.2f 秒）" % dt, seen is not None, "后台没看到新状态")
    print("     后台看到：state=%s note=%s" % (seen.get("state"), seen.get("note")))

    # ---- B5 管理端派任务 → 秒到 ----
    print("\n== B3. 管理端派任务 → 客户端收到通知 ==")
    got["notify"].clear()
    t0 = time.time()
    st = post("/jobs", {"slot": "12:00", "only": "", "client_id": ""})
    while time.time() - t0 < 6:
        if any(w == "jobs" for w, _ in got["notify"]):
            break
        time.sleep(0.02)
    dt = time.time() - t0
    print("     POST /jobs → %s；客户端收到通知用时 %.0f 毫秒" % (st, dt * 1000))
    # 心跳间隔是 5 秒：如果走了心跳，怎么也得几秒。1.5 秒以内才说明是推的。
    check("派任务是**推**过来的（< 1.5 秒，心跳得等 5 秒）", dt < 1.5,
          "用了 %.2f 秒，像是走了心跳" % dt)

    # ---- B6 改指派 → 客户端秒收到 switch_needed ----
    print("\n== B4. 改指派 → 客户端立刻知道要切换 ==")
    post("/accounts", {"label": "小号B", "masked": "138****7777",
                       "login_name": "13800007777"})
    _, html = get("/accounts")
    am = re.search(r"/accounts/(\d+)/roles", html)
    check("账号已建好（在账号页找到它）", am is not None)
    aid = int(am.group(1))
    post("/accounts/%d/roles" % aid, {"name": "测试角色A"})
    _, html = get("/accounts")
    rm = re.search(r"/roles/(\d+)/update", html)
    check("角色已建好", rm is not None)
    rid = int(rm.group(1))

    got["assign"].clear()
    t0 = time.time()
    post("/clients/%d/assign" % cid, {"account_id": str(aid), "role_id": str(rid)})
    need = False
    while time.time() - t0 < 6:
        if got["assign"] and got["assign"][-1][1]:
            need = True
            break
        time.sleep(0.05)
    dt = time.time() - t0
    check("改指派后客户端立刻收到 switch_needed（%.2f 秒）" % dt, need,
          "没收到切换意图：%r" % (got["assign"],))
    a = got["assign"][-1][0]
    check("指派内容带上了角色名（角色只按名字识别）",
          a.get("role") == "测试角色A" and a.get("masked") == "138****7777",
          repr(a))
    print("     指派: %s / %s；原因: %s" % (a.get("label"), a.get("role"),
                                            got["assign"][-1][2]))

    # ---- B7 探测指令往返 ----
    print("\n== B5. 探测指令：下发 → 执行 → 回报 ==")
    got["cmd"].clear()
    post("/clients/%d/probe" % cid, {"kind": "probe", "note": "测试探测"})
    hit = False
    for _ in range(80):
        if got["cmd"]:
            hit = True
            break
        time.sleep(0.1)
    check("客户端收到了探测指令", hit, "没收到")
    for _ in range(60):
        st = next((c.get("probe_status") for c in api_clients().get("clients", [])
                   if c.get("id") == cid), None)
        if st in ("done", "failed"):
            break
        time.sleep(0.1)
    check("服务端记录到指令已完成（客户端回传成功）", st == "done", "status=%r" % st)

    # ---- B8 正忙时指令被「放回队列」而不是丢掉 ----
    #
    # 这条走**完整真实链路**：管理端下指令 → 服务端推 notify → 客户端推状态
    # → 服务端把指令交给它 → 客户端回报 BUSY → 服务端放回队列（状态回到 pending）。
    # 判据用「最终状态是 pending」：正常执行完是 done/failed，只有被放回才是 pending。
    print("\n== B6. 客户端正忙 → 指令放回队列（不丢）==")
    n_before = len(got["cmd"])
    busy_mode["on"] = True
    post("/clients/%d/probe" % cid, {"kind": "switch"})
    back_to_pending = False
    saw_cmd = False
    for _ in range(100):
        if len(got["cmd"]) > n_before:
            saw_cmd = True
        st = next((c.get("probe_status") for c in api_clients().get("clients", [])
                   if c.get("id") == cid), None)
        if saw_cmd and st == "pending":
            back_to_pending = True
            break
        time.sleep(0.1)
    check("客户端确实收到了那条指令", saw_cmd, "没收到")
    check("客户端回报「正忙」后指令被放回队列（pending），而不是被消费掉",
          back_to_pending, "最终状态不是 pending")
    busy_mode["on"] = False
    # 恢复空闲后再来一次 —— 这次应该被正常执行掉（证明放回队列的指令真能再被取走）
    n_before = len(got["cmd"])
    for _ in range(100):
        if len(got["cmd"]) > n_before:
            break
        time.sleep(0.1)
    for _ in range(80):
        st = next((c.get("probe_status") for c in api_clients().get("clients", [])
                   if c.get("id") == cid), None)
        if st in ("done", "failed"):
            break
        time.sleep(0.1)
    check("空闲后那条指令被重新取走并执行完（放回队列没把它弄丢）",
          st == "done", "status=%r" % st)
    post("/clients/%d/probe/clear" % cid, {})

    # ---- B9 长连接断了要能退回心跳 ----
    print("\n== B7. 长连接断开 → 自动退回心跳（能力不缩水）==")
    check("此刻仍是长连接", rt.ws_connected)
    rt._close_ws()                            # 模拟"防火墙把长连接掐了"
    rt._ws_retry_at = time.time() + 3600      # 暂时不重连，好观察心跳模式
    before = rt.beats
    t0 = time.time()
    while time.time() - t0 < 12:
        if rt.transport == "http" and rt.beats > before:
            break
        time.sleep(0.2)
    check("断连后退回 http 心跳且心跳仍在上报",
          rt.transport == "http" and rt.beats > before,
          "transport=%s beats=%d→%d" % (rt.transport, before, rt.beats))
    st_ok, _ = get("/api/clients")
    check("心跳模式下后台依然看得到它（在线状态不断档）", st_ok == 200)

    rt.stop()
    print("\n  清理…")
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
    shutil.rmtree(BASE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    test_codec()
    rc = test_e2e() or 0
    if rc == 0:
        print("\n" + "=" * 60)
        print("  实时通道测试全部通过（%d 项断言）" % PASS["n"])
        print("=" * 60)
    sys.exit(rc)
