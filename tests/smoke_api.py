# -*- coding: utf-8 -*-
"""服务端 API 冒烟测试：心跳 / 指派 / 探测 / 定向任务 / 运行归属。

用临时数据目录跑，不碰 server/data 里的真实库。
"""
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

BASE = tempfile.mkdtemp(prefix="stzb_smoke_")
os.environ["STZB_DATA_DIR"] = BASE
os.environ["STZB_ADMIN_USER"] = "admin"
os.environ["STZB_ADMIN_PASSWORD"] = "pw12345678"
os.environ["STZB_AGENT_TOKEN"] = "stzb_smoke_token"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                            # noqa: E402
from app import db                                  # noqa: E402

AG = {"X-Agent-Token": "stzb_smoke_token", "X-Agent-Uid": "uid-A",
      "X-Agent-Host": "PC-A", "X-Agent-Version": "1.1.0"}

with TestClient(app) as c:
    print("== 老式 GET 探活（只带头，无负载） ==")
    r = c.get("/api/agent/ping", headers=AG)
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False)[:320])

    print("\n== 新式 POST 心跳（带负载） ==")
    hb = {"uid": "uid-A", "host": "PC-A", "mode": "managed", "state": "idle",
          "current": {"masked": "159****4508", "role": "X6014龙兴之"}}
    r = c.post("/api/agent/ping", headers=AG, json=hb)
    d = r.json()
    print(r.status_code, "registered=", d.get("registered"),
          "interval=", d.get("heartbeat_interval"),
          "assignment=", d.get("assignment"), "switch=", d.get("switch_needed"))

    print("\n== 登录管理端，建账号 + 角色 ==")
    r = c.post("/login", data={"user": "admin", "password": "pw12345678"},
               follow_redirects=False)
    print("login:", r.status_code)
    r = c.post("/accounts", data={"label": "主号", "login_name": "15900004508",
                                  "masked": "159****4508", "tag": "一组"},
               follow_redirects=False)
    print("create account:", r.status_code, r.headers.get("location"))
    aid = db.account_list()[0]["id"]
    r = c.post("/accounts/%d/roles" % aid,
               data={"name": "X6014龙兴之", "server": "X6014",
                     "season": "龙兴之", "tab": "已有角色"},
               follow_redirects=False)
    print("create role:", r.status_code)
    cli = db.client_by_uid("uid-A")
    rid = db.role_list(aid)[0]["id"]
    r = c.post("/clients/%d/assign" % cli["id"],
               data={"account_id": aid, "role_id": rid}, follow_redirects=False)
    print("assign:", r.status_code, r.headers.get("location"))

    print("\n== 指派后心跳：账号不一致 → 应要求切换 ==")
    hb["current"] = {"masked": "138****9999", "role": "别的角色"}
    d = c.post("/api/agent/ping", headers=AG, json=hb).json()
    print("assignment=", json.dumps(d["assignment"], ensure_ascii=False))
    print("switch_needed=", d["switch_needed"], "|", d["switch_reason"])

    print("\n== 管理端点「探测」→ 下一次心跳带回指令 ==")
    r = c.post("/clients/%d/probe" % cli["id"],
               data={"kind": "probe", "note": "看看界面"}, follow_redirects=False)
    print("probe req:", r.status_code)
    d = c.post("/api/agent/ping", headers=AG, json=hb).json()
    print("command=", json.dumps(d.get("command"), ensure_ascii=False))

    print("\n== 客户端回报探测结果 ==")
    r = c.post("/api/agent/probe", headers=AG,
               json={"uid": "uid-A", "ok": True, "message": "读到 12 行",
                     "data": {"lines": ["主城", "出征"]}})
    print(r.status_code, r.json())
    print("probe 状态:", db.client_get(cli["id"])["probe"]["status"])

    print("\n== 定向任务：只派给 uid-A ==")
    r = c.post("/jobs", data={"slot": "12:00", "only": "recruit",
                              "client_id": str(cli["id"]), "note": "指定给 A"},
               follow_redirects=False)
    print("create job:", r.status_code)
    AG_B = dict(AG)
    AG_B["X-Agent-Uid"] = "uid-B"
    AG_B["X-Agent-Host"] = "PC-B"
    c.post("/api/agent/ping", headers=AG_B, json={"uid": "uid-B", "host": "PC-B"})
    print("B 看到:", json.dumps(c.get("/api/agent/jobs", headers=AG_B,
                                   params={"uid": "uid-B"}).json(), ensure_ascii=False))
    ja = c.get("/api/agent/jobs", headers=AG, params={"uid": "uid-A"}).json()
    print("A 看到:", json.dumps(ja, ensure_ascii=False))
    jid = ja["jobs"][0]["id"]
    print("B 抢 A 的定向任务:",
          c.post("/api/agent/jobs/%d/take" % jid, headers=AG_B,
                 params={"uid": "uid-B", "host": "PC-B"}).status_code, "(应 403)")
    print("A 领取:", c.post("/api/agent/jobs/%d/take" % jid, headers=AG,
                          params={"uid": "uid-A", "host": "PC-A"}).status_code, "(应 200)")

    print("\n== 上传运行结果：应自动归属到客户端 / 账号 ==")
    r = c.post("/api/agent/run", headers=AG, json={
        "client_run_id": "uid-A-test-1", "uid": "uid-A", "host": "PC-A",
        "slot": "12:00", "started_at": "2026-09-19T12:00:00",
        "duration_seconds": 120, "all_ok": True, "counts": {"ok": 5},
        "tasks": [{"key": "recruit", "name": "招募", "status": "ok", "seconds": 30}]})
    print(r.status_code, r.json())
    run = db.run_get(r.json()["run_id"])
    print("run 归属: client_id=%s account_label=%s role_label=%s"
          % (run["client_id"], run["account_label"], run["role_label"]))

    print("\n== 页面可访问性 ==")
    for p in ("/clients", "/accounts", "/", "/jobs", "/runs"):
        print("  %-12s %s" % (p, c.get(p).status_code))
    print("\n== 在线统计 ==", db.client_online_count())

shutil.rmtree(BASE, ignore_errors=True)
print("\nALL DONE")