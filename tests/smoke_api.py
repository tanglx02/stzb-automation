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
    r = c.post("/accounts", data={"label": "主号", "login_name": "demo-account-01",
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

    print("\n== 角色「执行任务模式」：默认应是 inherit（不改变任何行为） ==")
    role = db.role_get(rid)
    from app import plan as PLAN
    p0 = PLAN.loads_plan(role["task_plan"])
    print("  默认 plan:", json.dumps(p0, ensure_ascii=False))
    d = c.post("/api/agent/ping", headers=AG,
               json={"uid": "uid-A", "host": "PC-A",
                     "current": {"masked": "159****4508", "role": "X6014龙兴之"}}).json()
    print("  心跳下发 task_plan:", json.dumps(d["assignment"].get("task_plan"),
                                              ensure_ascii=False))
    print("  心跳下发 paused:", d["assignment"].get("paused"))

    print("\n== 后端为角色单独指定任务（custom：只跑 招募 + 税收，只跑 12:00 档） ==")
    r = c.post("/roles/%d/save" % rid, data={
        "name": "X6014龙兴之", "tab": "已有角色", "server": "X6014",
        "season": "龙兴之", "enabled": "1",
        "plan_mode": "custom",
        "task_recruit": "1", "task_shuishou": "1",
        "slot_12": "1", "plan_note": "小号只跑两个"},
        follow_redirects=False)
    print("  save:", r.status_code, r.headers.get("location"))
    role = db.role_get(rid)
    saved = PLAN.loads_plan(role["task_plan"])
    print("  已保存:", json.dumps(saved, ensure_ascii=False))
    good = (saved["mode"] == "custom"
            and sorted(PLAN.selected_tasks(saved)) == ["recruit", "shuishou"]
            and PLAN.allows_slot(saved, "12:00") and not PLAN.allows_slot(saved, "00:00"))
    print("  解析正确:", "✓" if good else "✗ 失败", "(应 True)")

    d = c.post("/api/agent/ping", headers=AG,
               json={"uid": "uid-A", "host": "PC-A",
                     "current": {"masked": "159****4508", "role": "X6014龙兴之"}}).json()
    tp = d["assignment"].get("task_plan")
    print("  心跳下发 tasks:", tp["tasks"], "slots:", tp["slots"])
    good = good and tp["slots"] == ["12:00"]
    print("  下发正确:", "✓" if tp["slots"] == ["12:00"] else "✗ 失败")
    if not good:
        print("  !! 角色任务模式链路有问题")

    print("\n== 把角色切成「暂停执行」 ==")
    c.post("/roles/%d/save" % rid, data={
        "name": "X6014龙兴之", "tab": "已有角色", "enabled": "1",
        "plan_mode": "paused"}, follow_redirects=False)
    d = c.post("/api/agent/ping", headers=AG,
               json={"uid": "uid-A", "host": "PC-A",
                     "current": {"masked": "159****4508", "role": "X6014龙兴之"}}).json()
    print("  paused=", d["assignment"].get("paused"),
          "| switch_needed=", d["switch_needed"],
          "| reason=", d["switch_reason"])
    good = good and d["assignment"].get("paused") is True and d["switch_needed"] is False
    print("  暂停生效（不切换、不执行）:", "✓" if d["switch_needed"] is False else "✗ 失败")

    print("\n== 角色识别只认名字：服务端 X6014 改成 X6021（合服），不应再要求切换 ==")
    c.post("/roles/%d/save" % rid, data={
        "name": "X6014龙兴之", "enabled": "1", "plan_mode": "inherit",
        "server": "X6014", "season": "龙兴之"}, follow_redirects=False)
    # 客户端当前已经在「X6014龙兴之」这个角色上，只是区服号变了
    d = c.post("/api/agent/ping", headers=AG,
               json={"uid": "uid-A", "host": "PC-A",
                     "current": {"masked": "159****4508", "role": "X6021龙兴之"}}).json()
    print("  switch_needed=", d["switch_needed"],
          "| reason=", d.get("switch_reason"), "(应 False)")
    good = good and d["switch_needed"] is False
    print("  合服后不误判:", "✓" if d["switch_needed"] is False else "✗ 失败")

    print("\n== 运行归属：未指派时按角色名自动匹配已登记角色 ==")
    db.client_update(cli["id"], account_id=None, role_id=None)   # 先解除指派
    r = c.post("/api/agent/run", headers=AG, json={
        "client_run_id": "uid-A-nameonly-1", "uid": "uid-A", "host": "PC-A",
        "slot": "12:00", "started_at": "2026-09-19T12:10:00",
        "duration_seconds": 60, "all_ok": True, "counts": {"ok": 2},
        "role_label": "X6021龙兴之", "account_label": "159****4508",
        "tasks": [{"key": "recruit", "name": "招募", "status": "ok", "seconds": 20}]})
    run2 = db.run_get(r.json()["run_id"])
    print("  归属: role_id=%s role_label=%s" % (run2["role_id"], run2["role_label"]))
    print("  按名字命中已登记角色:", "✓" if run2["role_id"] == rid
          else "(未命中，role_id=%s)" % run2["role_id"])
    # 恢复指派，供后面页面检查用
    c.post("/clients/%d/assign" % cli["id"],
           data={"account_id": aid, "role_id": rid}, follow_redirects=False)

    print("\n== 角色每日执行总览（数据层 role_daily_overview） ==")
    ov = db.role_daily_overview(days=3)
    print("  days=%s dates=%s" % (ov["days"], ov["dates"]))
    for rr in ov["roles"]:
        done = sum(len(d["slots"]) for d in rr["days"])
        empty = [d["date"] for d in rr["days"] if d["empty"]]
        print("   · %-14s registered=%-5s 近%d天档位=%d 空白天=%d plan=%s"
              % (rr["name"], rr["registered"], ov["days"], done, len(empty),
                 rr["plan_text"]))
    print("  需关注角色 ids:", ov["attention"])

    print("\n== 回归：已指派但还没上报过 current 的客户端，页面不能 500 ==")
    # 客户端刚被指派、下一次心跳还没来时，status 里没有 current 字段。
    # 模板里若直接写 c.status.current.masked 会抛 UndefinedError → 整页 500。
    c.post("/api/agent/ping", headers=AG,
           json={"uid": "uid-A", "host": "PC-A", "mode": "managed", "state": "idle"})
    st = c.get("/clients")
    print("  /clients ->", st.status_code, "(应 200)")
    print("  已指派:", db.client_by_uid('uid-A').get('account_id') is not None,
          "| status 有 current:", 'current' in (db.client_by_uid('uid-A')['status'] or {}))
    if st.status_code != 200:
        print("  !! 页面 500 了")

    print("\n== 页面可访问性（有任一非 200 就报错） ==")
    # /devices 是设备管理页（管理员）；它是主机级页面，漏测会等到线上才发现白屏
    pages = ("/clients", "/accounts", "/", "/jobs", "/runs", "/roles", "/settings",
             "/events", "/config", "/users", "/password", "/devices")
    bad_pages = []
    for p in pages:
        code = c.get(p).status_code
        print("  %-12s %s" % (p, code))
        if code != 200:
            bad_pages.append((p, code))
    print("  页面全部 200:" if not bad_pages else "  !! 非 200 页面：%s" % bad_pages)

    print("\n== 任务页必须能指定设备 ==")
    # ★ 2026-09-21：后端曾经**整条链路都没有「设备」这个维度**，于是
    #   「在手机上跑还是在模拟器上跑」根本选不出来，用户看到的现象是
    #   「从后端发不起设备任务」。这里**硬断言**钉住入口别再被删掉 ——
    #   少了下拉就等于这个功能又没了，不能只在屏幕上写一行提示。
    jobs_html = c.get("/jobs").text
    devs_html = c.get("/devices").text
    # 设备行是**有登记设备时**才渲染的，而冒烟测试用的是空临时库 →
    # 按钮不会出现在 HTML 里。所以这条查**模板文件**（跟护栏测试一个路子）。
    _tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "server", "app", "templates")
    with open(os.path.join(_tpl, "_device_rows.html"), encoding="utf-8") as f:
        dev_rows_tpl = f.read()
    must_have = [
        ('/jobs 的设备下拉', 'name="device_id"' in jobs_html),
        ('/jobs 的「执行设备」表单标签', '执行设备（模拟器 / 实体手机）' in jobs_html),
        ('/jobs 列表的「执行设备」表头', "<th>执行设备</th>" in jobs_html),
        ('设备行模板的「跑一轮」按钮', "在这台设备上跑一轮" in dev_rows_tpl),
    ]
    for name, ok in must_have:
        print("  %-34s %s" % (name, "✓" if ok else "✗ 缺失"))
    missing = [n for n, ok in must_have if not ok]
    if missing:
        raise SystemExit("!! 设备任务入口缺失：%s" % missing)

    print("\n== 新增：设备管理接口与账号密码 ==")
    dev = c.get("/api/devices")
    print("  GET /api/devices ->", dev.status_code,
          "| 键:", sorted(dev.json().keys()) if dev.status_code == 200 else "-")
    # 没连设备时也应该返回合法的空结构，而不是 500（页面靠它渲染）
    if dev.status_code == 200:
        j = dev.json()
        print("  结构合法:", "✓" if ("devices" in j and "clients" in j) else j)

    # 账号密码：设 → 校验 → 弱口令拒绝 → 清空，全链路走一遍
    # （只存哈希、不回显明文；弱口令必须「不生效」，不能只看状态码）
    ok_pw = c.post("/accounts/%d/password" % aid,
                   data={"password": "smoke_pw_123"}, follow_redirects=False)
    print("  设密码 ->", ok_pw.status_code)
    a = db.account_get(aid)
    print("  库里标记 has_password:", a.get("has_password"),
          "| 无明文泄露:", "pwd_hash" not in a)
    print("  哈希可校验（对）:", db.account_check_password(aid, "smoke_pw_123"))
    print("  哈希可校验（错）:", db.account_check_password(aid, "smoke_pw_124"))

    # 弱口令：请求后必须仍是「原来那个密码」，不能把已设的密码冲掉
    c.post("/accounts/%d/password" % aid,
           data={"password": "123"}, follow_redirects=False)
    print("  弱口令尝试后旧密码仍有效:", db.account_check_password(aid, "smoke_pw_123"),
          "| 弱口令未生效:", not db.account_check_password(aid, "123"))

    clr = c.post("/accounts/%d/password" % aid,
                 data={"clear": "1"}, follow_redirects=False)
    print("  清密码 ->", clr.status_code, "| has_password:",
          db.account_get(aid).get("has_password"),
          "| 旧密码已失效:", not db.account_check_password(aid, "smoke_pw_123"))

    print("\n== 在线统计 ==", db.client_online_count())

shutil.rmtree(BASE, ignore_errors=True)
print("\nALL DONE")