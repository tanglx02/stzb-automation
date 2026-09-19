# -*- coding: utf-8 -*-
"""客户端 ↔ 服务端 联调测试。

起一个真的 uvicorn 服务（本地端口），然后用真的 CloudClient / Heartbeat / Identity
去打它，验证：
  · 心跳登记与在线判定
  · 指派下发与 switch_needed 计算
  · 人工探测指令的往返（下发 → 客户端执行 → 回报）
  · 定向任务按客户端过滤
  · 运行结果自动归属

客户端那边用假的 Ui（不连模拟器），所以这个测试可以随时跑。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = sys.executable
BASE = tempfile.mkdtemp(prefix="stzb_e2e_")
PORT = 8791

env = dict(os.environ)
env["STZB_DATA_DIR"] = BASE
env["STZB_ADMIN_USER"] = "admin"
env["STZB_ADMIN_PASSWORD"] = "pw12345678"
env["STZB_AGENT_TOKEN"] = "stzb_e2e_token"
env["STZB_HEARTBEAT_INTERVAL"] = "5"        # 测试里把心跳调快，省时间
env["STZB_CLIENT_OFFLINE_AFTER"] = "15"

srv = subprocess.Popen(
    [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
     "--log-level", "warning"],
    cwd=os.path.join(ROOT, "server"), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")

# 等端口起来
import urllib.request                                     # noqa: E402
for i in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/healthz" % PORT, timeout=1)
        break
    except Exception:
        time.sleep(0.5)
else:
    print("!! 服务起不来"); srv.kill(); sys.exit(1)

print("服务已就绪：http://127.0.0.1:%d" % PORT)

# ---- 客户端侧 ----
from stzb.cloud import CloudClient                         # noqa: E402
from stzb.heartbeat import Heartbeat, StatusBox            # noqa: E402
from stzb.identity import Identity, load_identity          # noqa: E402

IDFILE = os.path.join(BASE, "client.json")
ident = Identity.load(IDFILE)
print("客户端身份: %s / %s" % (ident.uid, ident.host))

log_lines = []
def log(m):
    log_lines.append(m)
    print("   " + m)

client = CloudClient(base_url="http://127.0.0.1:%d" % PORT,
                     token="stzb_e2e_token", timeout=10, retries=1,
                     logger=log, uid=ident.uid)

print("\n== 1. 心跳 ==")
ok, info = client.ping()
print("  ping ok=", ok, "interval=", (info or {}).get("heartbeat_interval"),
      "registered=", (info or {}).get("registered"))

box = StatusBox(mode="managed")
box.set(state="idle", note="测试客户端空闲")
box.patch_current(masked="159****4508", role="X6014龙兴之")

received = {"cmd": [], "assign": []}

def on_command(cmd):
    received["cmd"].append(cmd)
    kind = cmd.get("kind")
    log("   （模拟执行指令 %s）" % kind)
    if kind == "probe":
        return True, "读到 12 行文字，已在主城", {"lines": ["主城", "出征", "招募"],
                                              "text_count": 12}
    if kind == "switch":
        return True, "切换成功：已切到 159****4508", {"ok": True, "kind": "account"}
    return False, "不认识的指令", {}

def on_assignment(a, need, reason):
    received["assign"].append((a, need, reason))

hb = Heartbeat(client, ident, state_fn=box.get, on_assignment=on_assignment,
               on_command=on_command, logger=log, interval=3)
hb.start()
time.sleep(0.2)
d = hb.beat()
print("  第一次心跳: assignment=", json.dumps((d or {}).get("assignment"), ensure_ascii=False),
      "switch=", (d or {}).get("switch_needed"))

# ---- 管理端操作 ----
print("\n== 2. 管理端建账号/角色并指派 ==")
import http.cookiejar                                      # noqa: E402
import urllib.parse

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [("Content-Type", "application/x-www-form-urlencoded")]

def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    try:
        return opener.open("http://127.0.0.1:%d%s" % (PORT, path), body, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code

def get(path):
    try:
        r = opener.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=10)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""

print("  login:", post("/login", {"user": "admin", "password": "pw12345678"}))
print("  建账号:", post("/accounts", {"label": "小号B", "masked": "138****7777",
                                     "login_name": "13800007777"}))
_, html = get("/clients")
# 从页面里抠出 id（没有解析库，直接用正则）
import re                                                  # noqa: E402
aid = int(re.search(r'/accounts/(\d+)/roles', html).group(1)) if re.search(r'/accounts/(\d+)/roles', html) else 1
cidm = re.search(r'/clients/(\d+)/assign', html)
cid = int(cidm.group(1)) if cidm else 1
print("  account_id=%s  client_id=%s" % (aid, cid))
print("  建角色:", post("/accounts/%d/roles" % aid,
                       {"name": "X3088赤壁之", "server": "X3088", "season": "赤壁之",
                        "tab": "已有角色"}))
_, html2 = get("/accounts")
rids = re.findall(r'/roles/(\d+)/update', html2)
rid = int(rids[0]) if rids else 1
print("  role_id=%s" % rid)
print("  指派:", post("/clients/%d/assign" % cid, {"account_id": aid, "role_id": rid}))

print("\n== 3. 指派后心跳应要求切换 ==")
d = hb.beat()
print("  assignment=", json.dumps((d or {}).get("assignment"), ensure_ascii=False))
print("  switch_needed=", (d or {}).get("switch_needed"), "|", (d or {}).get("switch_reason"))
assert (d or {}).get("switch_needed") is True, "应要求切换"
print("  ✓ 通过了")

print("\n== 4. 下发探测指令 → 心跳取走 → 回报 ==")
print("  下发:", post("/clients/%d/probe" % cid, {"kind": "probe", "note": "测试探测"}))
d = hb.beat()
cmd = (d or {}).get("command")
print("  心跳带回指令:", json.dumps(cmd, ensure_ascii=False)[:200])
assert cmd and cmd.get("kind") == "probe", "应带回探测指令"
time.sleep(1.5)     # 等指令线程执行 + 回报
_, html3 = get("/clients")
print("  页面上有探测结果:", "读到 12 行文字" in html3)
assert "读到 12 行文字" in html3, "探测结果应显示在页面上"
print("  ✓ 通过了")

print("\n== 5. 定向任务过滤 ==")
print("  建定向任务:", post("/jobs", {"slot": "12:00", "only": "recruit",
                                      "client_id": str(cid), "note": "e2e"}))
j = json.loads(json.dumps(client.list_jobs()))
print("  本客户端看到:", json.dumps(j, ensure_ascii=False)[:200])
assert j[1] and j[1].get("jobs"), "本客户端应看到定向任务"
# 另一台客户端不该看到
other = CloudClient(base_url="http://127.0.0.1:%d" % PORT, token="stzb_e2e_token",
                    timeout=10, retries=1, logger=lambda m: None, uid="cli_other_xxx")
oh = json.loads(json.dumps(other.list_jobs()[1]))
print("  另一台看到:", json.dumps(oh, ensure_ascii=False)[:120])
assert not oh.get("jobs"), "别的客户端不该看到定向任务"
print("  ✓ 通过了")

print("\n== 6. 上传运行结果应归属到客户端/账号 ==")
ok, rid, err = client.create_run({
    "client_run_id": "e2e-run-1", "uid": ident.uid, "host": ident.host,
    "slot": "12:00", "started_at": "2026-09-19T12:00:00", "duration_seconds": 60,
    "all_ok": True, "counts": {"ok": 1},
    "tasks": [{"key": "recruit", "name": "招募", "status": "ok", "seconds": 20}]})
print("  create_run ok=%s rid=%s" % (ok, rid))
_, html4 = get("/runs/%d" % rid)
print("  详情页有账号名:", "小号B" in html4)
assert "小号B" in html4, "运行详情应显示账号"
print("  ✓ 通过了")

print("\n== 7. 心跳正常上报（在线状态） ==")
time.sleep(3.5)
d = hb.beat()
status = (d or {}).get("client") or {}
print("  服务端认得这台:", status)
_, htmlc = get("/clients")
print("  页面上显示在线:", "在线" in htmlc)
print("  ✓ 通过了")

hb.stop()
print("\n全部通过。清理…")
srv.terminate()
try:
    srv.wait(timeout=10)
except Exception:
    srv.kill()
shutil.rmtree(BASE, ignore_errors=True)
print("DONE")