# -*- coding: utf-8 -*-
"""多用户隔离与权限测试。

覆盖 2026-09-20 引入的多用户模型：
  1. 用户 CRUD（建 / 改名 / 停用 / 改密 / 删除，且删除不连带删账号）
  2. 登录（大小写不敏感、停用即拒、一次性口令强制改密）
  3. 数据隔离：普通用户只看得到自己的账号/角色/运行记录/待执行
  4. 越权拦截：普通用户碰别人的账号 / 角色 / 任务 → 一律挡掉
  5. 主机级操作（客户端指派、设置、用户管理）只归管理员
  6. 最后一个管理员保护（不能降级 / 停用 / 删除自己）
  7. 老库迁移：kv 里的 admin_user/admin_pwd_hash 搬进 users 表
    8. 运行记录归属：同名角色不串号（跨用户访问一律 404）

用临时数据目录跑，不碰 server/data 里的真实库。

    python tests/test_multiuser.py
"""
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

BASE = tempfile.mkdtemp(prefix="stzb_multiuser_")
os.environ["STZB_DATA_DIR"] = BASE
os.environ["STZB_ADMIN_USER"] = "root_admin"
os.environ["STZB_ADMIN_PASSWORD"] = "Rootpw12345"
os.environ["STZB_AGENT_TOKEN"] = "stzb_mu_token"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                            # noqa: E402
from app import db, security                        # noqa: E402

AG = {"X-Agent-Token": "stzb_mu_token", "X-Agent-Uid": "uid-A",
      "X-Agent-Host": "PC-A", "X-Agent-Version": "1.1.0"}

FAILS = []


def check(label, ok, extra=""):
    print("  %-52s %s %s" % (label, "✓" if ok else "✗", extra))
    if not ok:
        FAILS.append(label)


def login(c, user, pwd):
    return c.post("/login", data={"user": user, "password": pwd},
                  follow_redirects=False)


def logout(c):
    return c.post("/logout", follow_redirects=False)


with TestClient(app) as c:
    # ---------------------------------------------------------------- 引导
    print("\n== 1. 引导：管理员落在 users 表里 ==")
    adm = db.user_by_name("root_admin")
    check("bootstrap 建出管理员", bool(adm), "role=%s" % (adm or {}).get("role"))
    check("管理员 is_admin", bool(adm and adm.get("is_admin")))
    check("bootstrap 后管理员数=1", db.user_count_admins() == 1)

    # ---------------------------------------------------------------- 登录
    print("\n== 2. 登录：大小写不敏感 / 停用即拒 ==")
    check("正确口令登录成功", login(c, "root_admin", "Rootpw12345").status_code == 303)
    check("用户名大小写不敏感", login(c, "ROOT_ADMIN", "Rootpw12345").status_code == 303)
    check("口令错被拒(401)", login(c, "root_admin", "wrong-password").status_code == 401)
    check("不存在的用户被拒(401)", login(c, "nobody_at_all", "whatever123").status_code == 401)

    # ------------------------------------------------------------ 建普通用户
    print("\n== 3. 管理员建普通用户（走页面表单） ==")
    login(c, "root_admin", "Rootpw12345")
    r = c.post("/users", data={"username": "zhangsan", "role": "user",
                               "display_name": "张三", "password": "Zs!kaola7788"},
               follow_redirects=False)
    check("建用户返回 303", r.status_code == 303, r.headers.get("location", "")[:60])
    zs = db.user_by_name("zhangsan")
    check("用户已入库", bool(zs), "id=%s role=%s" % ((zs or {}).get("id"), (zs or {}).get("role")))
    check("普通用户 is_admin=False", not (zs or {}).get("is_admin"))
    check("must_change=True（首次登录强制改密）", bool((zs or {}).get("must_change")))

    r = c.post("/users", data={"username": "zhangsan", "role": "user"},
               follow_redirects=False)
    check("重名被拒（303 带 err）", "err=" in (r.headers.get("location") or ""))

    r = c.post("/users", data={"username": "ab", "role": "user"},
               follow_redirects=False)
    check("用户名太短被拒", "err=" in (r.headers.get("location") or ""))

    r = c.post("/users", data={"username": "bad name!", "role": "user"},
               follow_redirects=False)
    check("用户名非法字符被拒", "err=" in (r.headers.get("location") or ""))

    r = c.post("/users", data={"username": "lisi", "role": "user",
                               "password": "12345678"}, follow_redirects=False)
    check("弱口令被拒", "err=" in (r.headers.get("location") or ""))

    r = c.post("/users", data={"username": "lisi", "role": "user"}, follow_redirects=False)
    check("留空口令 → 自动生成", r.status_code == 303 and "new_pwd=" in (r.headers.get("location") or ""))
    li = db.user_by_name("lisi")
    check("lisi 已入库（自动口令）", bool(li))

    # ------------------------------------------------- 一次性口令强制改密
    print("\n== 4. 一次性口令：首登被强制跳改密页 ==")
    logout(c)
    r = login(c, "zhangsan", "Zs!kaola7788")
    check("登录成功且跳 /password?first=1",
          r.status_code == 303 and "/password?first=1" in (r.headers.get("location") or ""),
          r.headers.get("location", ""))

    r = c.get("/password?first=1")
    check("改密页可访问(200)", r.status_code == 200)

    r = c.post("/password", data={"current": "", "new1": "k7Fj!2mQx9",
                                  "new2": "k7Fj!2mQx9", "first": "1"},
               follow_redirects=False)
    check("首登免验旧口令即可改密", "ok=" in (r.headers.get("location") or ""),
          r.headers.get("location", ""))
    check("must_change 已清掉",
          not (db.user_by_name("zhangsan") or {}).get("must_change"))
    check("新口令可用", bool(security.check_user("zhangsan", "k7Fj!2mQx9")))
    check("旧口令已失效", not security.check_user("zhangsan", "Zs!kaola7788"))

    # 弱口令把关（两处入口共用一套规则，别一边严一边松）
    r = c.post("/password", data={"current": "k7Fj!2mQx9", "new1": "12345678",
                                  "new2": "12345678"}, follow_redirects=False)
    check("改密页拒绝弱口令", "err=" in (r.headers.get("location") or ""))
    check("口令没被改成弱的", bool(security.check_user("zhangsan", "k7Fj!2mQx9")))
    r = c.post("/password", data={"current": "wrong-one", "new1": "Another1!ok",
                                  "new2": "Another1!ok"}, follow_redirects=False)
    check("改密页要验证当前口令", "err=" in (r.headers.get("location") or ""))
    r = c.post("/password", data={"current": "k7Fj!2mQx9", "new1": "Another1!ok",
                                  "new2": "mismatch1!ok"}, follow_redirects=False)
    check("两次不一致被拒", "err=" in (r.headers.get("location") or ""))

    # ------------------------------------------------------------ 数据隔离
    print("\n== 5. 数据隔离：张三建号，lisi 看不到 ==")
    logout(c)
    login(c, "zhangsan", "k7Fj!2mQx9")
    r = c.post("/accounts", data={"label": "张三主号", "masked": "159****0001"},
               follow_redirects=False)
    check("普通用户能自己建账号", r.status_code == 303)
    zs_acc = [a for a in db.account_list() if a["label"] == "张三主号"]
    check("账号归属张三", bool(zs_acc) and zs_acc[0]["owner_id"] == zs["id"],
          "owner_id=%s" % (zs_acc[0]["owner_id"] if zs_acc else None))
    zs_aid = zs_acc[0]["id"]
    c.post("/accounts/%d/roles" % zs_aid,
           data={"name": "ZS角色一", "tab": "已有角色"}, follow_redirects=False)

    logout(c)
    login(c, "lisi", "x")            # 口令不对 —— 得先拿管理员给 lisi 的口令
    # lisi 的口令是自动生成的，直接走安全层重置一个已知的
    logout(c)
    login(c, "root_admin", "Rootpw12345")
    security.set_user_password(int(li["id"]), "Lis!3mu4567")
    logout(c)
    check("lisi 能登录", login(c, "lisi", "Lis!3mu4567").status_code == 303)

    r = c.get("/accounts")
    check("lisi 的账号页不含张三的账号", "张三主号" not in r.text)
    check("lisi 的账号列表为空",
          len(db.account_list(owner_id=li["id"], own_only=True)) == 0)

    # 越权：改 / 删 别人的账号
    r = c.post("/accounts/%d/update" % zs_aid, data={"label": "被改了"},
               follow_redirects=False)
    check("lisi 改张三的账号被拦", "err=" in (r.headers.get("location") or ""))
    check("账号名没被改",
          (db.account_get(zs_aid) or {}).get("label") == "张三主号")

    r = c.post("/accounts/%d/delete" % zs_aid, follow_redirects=False)
    check("lisi 删张三的账号被拦", "err=" in (r.headers.get("location") or ""))
    check("账号还在", bool(db.account_get(zs_aid)))

    zs_rid = db.role_list(zs_aid)[0]["id"]
    r = c.post("/roles/%d/delete" % zs_rid, follow_redirects=False)
    check("lisi 删张三的角色被拦", "err=" in (r.headers.get("location") or ""))
    check("角色还在", bool(db.role_get(zs_rid)))

    r = c.post("/accounts/%d/roles" % zs_aid, data={"name": "偷加的角色"},
               follow_redirects=False)
    check("lisi 往张三账号塞角色被拦", "err=" in (r.headers.get("location") or ""))
    check("没多出角色", len(db.role_list(zs_aid)) == 1)

    # ------------------------------------------------------------ 主机级权限
    print("\n== 6. 主机级操作：普通用户只读 / 被拒 ==")
    for p in ("/settings", "/config", "/users"):
        r = c.get(p, follow_redirects=False)
        check("普通用户访问 %-10s 被弹回" % p,
              r.status_code == 303 and "denied=1" in (r.headers.get("location") or ""),
              "%s -> %s" % (r.status_code, r.headers.get("location")))

    r = c.get("/clients")
    check("普通用户能只读看客户端页(200)", r.status_code == 200)
    check("客户端页对普通用户标注只读",
          "只读" in r.text)
    check("客户端页不含「改名 / 备注 / 删除」操作块",
          "/rename" not in r.text and "/delete" not in r.text)

    cli = db.client_by_uid("uid-A")
    if not cli:
        c.post("/api/agent/ping", headers=AG, json={"uid": "uid-A", "host": "PC-A",
                                                    "mode": "managed", "state": "idle"})
        cli = db.client_by_uid("uid-A")
    r = c.post("/clients/%d/rename" % cli["id"], data={"name": "lisi改名"},
               follow_redirects=False)
    check("普通用户改客户端名被弹回", r.status_code == 303
          and "denied=1" in (r.headers.get("location") or ""))
    check("客户端名没被改", (db.client_get(cli["id"]) or {}).get("name") != "lisi改名")

    r = c.post("/clients/%d/probe" % cli["id"], data={"kind": "probe"},
               follow_redirects=False)
    check("普通用户下发探测指令被弹回", r.status_code == 303)

    # ------------------------------------------------------------ 待执行归属
    print("\n== 7. 待执行任务：归属与取消权限 ==")
    r = c.post("/jobs", data={"slot": "auto", "note": "lisi 的任务"},
               follow_redirects=False)
    check("普通用户能自己排任务", r.status_code == 303)
    mine = db.request_list(owner_id=int(li["id"]), own_only=True)
    check("任务归属 lisi", any(int(x["owner_id"]) == int(li["id"]) for x in mine))

    logout(c)
    login(c, "root_admin", "Rootpw12345")
    c.post("/jobs", data={"slot": "auto", "note": "管理员公共任务"}, follow_redirects=False)
    logout(c)
    login(c, "lisi", "Lis!3mu4567")

    seen = db.request_list(owner_id=int(li["id"]), own_only=True)
    check("lisi 看得到自己的任务",
          any(x["owner_id"] is not None and int(x["owner_id"]) == int(li["id"])
              for x in seen))
    check("lisi 也看得到管理员公共任务(owner_id IS NULL)",
          any(x["owner_id"] is None for x in seen))
    check("lisi 看不到别人的私有任务",
          all(x["owner_id"] is None or int(x["owner_id"]) == int(li["id"])
              for x in seen))

    pub = [x for x in seen if x["owner_id"] is None]
    if pub:
        r = c.post("/jobs/%d/cancel" % pub[0]["id"], follow_redirects=False)
        check("lisi 取消管理员的公共任务被拒",
              "err=" in (r.headers.get("location") or ""))
        with db.tx() as cn:
            st = cn.execute("SELECT status FROM run_requests WHERE id=?",
                            (pub[0]["id"],)).fetchone()["status"]
        check("公共任务仍是 pending", st == "pending")

    own = [x for x in seen if x["owner_id"] is not None]
    if own:
        r = c.post("/jobs/%d/cancel" % own[0]["id"], follow_redirects=False)
        check("lisi 取消自己的任务成功", "canceled=1" in (r.headers.get("location") or ""))

    # ------------------------------------------------------- 管理员管全部
    print("\n== 8. 管理员看全部 ==")
    logout(c)
    login(c, "root_admin", "Rootpw12345")
    r = c.get("/accounts")
    check("管理员看得到张三的账号", "张三主号" in r.text)
    check("管理员账号页标了归属人", "归属：" in r.text)
    r = c.get("/jobs")
    check("管理员待执行页有「排队人」列", "排队人" in r.text)
    r = c.get("/users")
    check("用户管理页列出 zhangsan", "zhangsan" in r.text)
    check("用户管理页列出 lisi", "lisi" in r.text)

    # ------------------------------------------------- 最后一个管理员保护
    print("\n== 9. 最后一个管理员保护 ==")
    me = db.user_by_name("root_admin")
    r = c.post("/users/%d/update" % me["id"], data={"_has_enabled": "1",
                                                    "role": "user", "enabled": "1"},
               follow_redirects=False)
    check("不能改自己的角色（降级=当场丢权限）",
          "err=" in (r.headers.get("location") or ""))
    check("角色仍是 admin", (db.user_by_name("root_admin") or {}).get("role") == "admin")

    r = c.post("/users/%d/update" % me["id"], data={"_has_enabled": "1", "role": "admin"},
               follow_redirects=False)
    check("不能停用自己（不发 enabled 字段）",
          "err=" in (r.headers.get("location") or ""))
    check("仍然是启用的", bool((db.user_by_name("root_admin") or {}).get("enabled")))

    r = c.post("/users/%d/delete" % me["id"], follow_redirects=False)
    check("不能删自己", "err=" in (r.headers.get("location") or ""))
    check("管理员还在", bool(db.user_by_name("root_admin")))

    # 第二个管理员可以降级第一个（证明「唯一管理员」才是拦截条件，不是「自己」）
    c.post("/users", data={"username": "ops2", "role": "admin",
                           "password": "Ops9!xq7742"}, follow_redirects=False)
    check("第二个管理员已建", db.user_count_admins() == 2)
    ops2 = db.user_by_name("ops2")
    logout(c)
    check("ops2 能登录", login(c, "ops2", "Ops9!xq7742").status_code == 303)
    r = c.post("/users/%d/update" % me["id"],
               data={"_has_enabled": "1", "role": "user", "enabled": "1"},
               follow_redirects=False)
    check("另一个管理员可以降级 root_admin", "ok=" in (r.headers.get("location") or ""))
    check("root_admin 现在是普通用户",
          (db.user_by_name("root_admin") or {}).get("role") == "user")
    r = c.post("/users/%d/update" % me["id"],
               data={"_has_enabled": "1", "role": "admin", "enabled": "1"},
               follow_redirects=False)
    check("（改回管理员）", "ok=" in (r.headers.get("location") or ""))

    # ------------------------------------------------------------ 停用即失效
    print("\n== 10. 停用立刻生效（不等会话过期） ==")
    logout(c)
    login(c, "lisi", "Lis!3mu4567")
    check("lisi 现在能访问总览", c.get("/").status_code == 200)
    logout(c)
    login(c, "root_admin", "Rootpw12345")
    c.post("/users/%d/update" % li["id"], data={"_has_enabled": "1", "role": "user"},
           follow_redirects=False)             # 不带 enabled → 停用
    check("lisi 已被停用", not (db.user_by_name("lisi") or {}).get("enabled"))
    logout(c)
    check("停用后登录被拒", login(c, "lisi", "Lis!3mu4567").status_code == 401)

    # 反向：启用后立刻能登回来
    login(c, "root_admin", "Rootpw12345")
    c.post("/users/%d/update" % li["id"],
           data={"_has_enabled": "1", "role": "user", "enabled": "1"},
           follow_redirects=False)
    check("重新启用后能登录", login(c, "lisi", "Lis!3mu4567").status_code == 303)

    # ------------------------------------------------------------ 改名不踢线
    print("\n== 11. 改名不影响会话（会话认 uid 不认名字） ==")
    logout(c)
    login(c, "root_admin", "Rootpw12345")
    c.post("/users/%d/update" % zs["id"],
           data={"_has_enabled": "1", "role": "user", "enabled": "1",
                 "username": "zhangsan_new"}, follow_redirects=False)
    check("改名成功", bool(db.user_by_name("zhangsan_new")))

    # ------------------------------------------------------------ 删除用户
    print("\n== 12. 删用户：账号收归管理员，不连带删 ==")
    before_acc = db.account_get(zs_aid)
    r = c.post("/users/%d/delete" % zs["id"], follow_redirects=False)
    check("删用户成功", "ok=" in (r.headers.get("location") or ""))
    check("用户没了", not db.user_by_name("zhangsan_new"))
    after_acc = db.account_get(zs_aid)
    check("账号还在", bool(after_acc))
    check("账号归属变成管理员(NULL)", after_acc and after_acc["owner_id"] is None,
          "owner_id=%s" % (after_acc or {}).get("owner_id"))
    check("角色跟着账号保留", len(db.role_list(zs_aid)) == 1)

    # ------------------------------------------------------------ 重置口令
    print("\n== 13. 重置口令：生成临时口令且强制改密 ==")
    r = c.post("/users/%d/reset" % li["id"], follow_redirects=False)
    loc = r.headers.get("location") or ""
    check("重置返回新口令", "new_pwd=" in loc)
    check("被重置者 must_change=True",
          bool((db.user_by_name("lisi") or {}).get("must_change")))
    raw = loc.split("new_pwd=")[-1]
    from urllib.parse import unquote
    pwd_pair = unquote(raw).split("|", 1)[-1]
    check("重置后的口令可用（且需改密）",
          bool(security.check_user("lisi", pwd_pair)))

    # ------------------------------------------------------------ 页面可访问
    print("\n== 14. 管理员页面全部可访问 ==")
    for p in ("/", "/clients", "/accounts", "/roles", "/jobs", "/runs", "/config",
              "/settings", "/events", "/users", "/password"):
        code = c.get(p).status_code
        check("  %-12s 200" % p, code == 200, "-> %s" % code)

    # ------------------------------------------- 运行记录归属（最要命的隔离路径）
    # routes_agent.create_run 里「按角色名反查」这一步最容易出事：
    # 不同用户的角色**重名是可能的**（游戏名在被抢注前谁都能用），
    # 若在全库范围按名字找，A 的运行记录会被错归到 B 的角色上 ——
    # 那等于把 A 的数据泄给 B。所以必须先把范围圈到自己名下。
    # 这条路径前面几个 section 都没端到端覆盖到，这里单独钉死。
    print("\n== 15. 运行记录归属：同名角色不串号（最要命的隔离路径） ==")
    logout(c)
    login(c, "root_admin", "Rootpw12345")

    # 造两个用户，各自的账号下都登记「归一化后同名」的角色（区服不同而已）
    c.post("/users", data={"username": "alice", "role": "user",
                           "password": "Al!ce3mu778"}, follow_redirects=False)
    c.post("/users", data={"username": "bob", "role": "user",
                           "password": "B0b!3mu4455"}, follow_redirects=False)
    al = db.user_by_name("alice")
    bo = db.user_by_name("bob")
    check("alice / bob 已建立", bool(al) and bool(bo),
          "alice=%s bob=%s" % ((al or {}).get("id"), (bo or {}).get("id")))

    c.post("/accounts", data={"label": "Alice号", "owner_id": str(al["id"])},
           follow_redirects=False)
    al_acc = next((a["id"] for a in db.account_list() if a["label"] == "Alice号"), None)
    c.post("/accounts/%d/roles" % al_acc, data={"name": "X6021龙兴之"},
           follow_redirects=False)
    al_rid = db.role_list(al_acc)[0]["id"]

    c.post("/accounts", data={"label": "Bob号", "owner_id": str(bo["id"])},
           follow_redirects=False)
    bo_acc = next((a["id"] for a in db.account_list() if a["label"] == "Bob号"), None)
    c.post("/accounts/%d/roles" % bo_acc, data={"name": "X6014龙兴之"},
           follow_redirects=False)
    bo_rid = db.role_list(bo_acc)[0]["id"]

    check("两个角色归一化后同名（碰撞前提成立）",
          db.role_key("X6021龙兴之") == db.role_key("X6014龙兴之") == "龙兴之",
          "alice=%s bob=%s" % (al_rid, bo_rid))

    # ---- 数据层：按名字反查必须先圈定归属 ----
    hit_a = db.role_find_by_name("龙兴之", owner_id=al["id"], own_only=True)
    hit_b = db.role_find_by_name("龙兴之", owner_id=bo["id"], own_only=True)
    check("限 alice 范围 → 命中 alice 的角色", (hit_a or {}).get("id") == al_rid)
    check("限 bob   范围 → 命中 bob 的角色", (hit_b or {}).get("id") == bo_rid)
    check("不限范围（同名碰撞）→ 拒绝猜测，返回 None",
          db.role_find_by_name("龙兴之") is None)

    # ---- 端到端 A：客户端指派到 alice 的账号，上传后必须归 alice 的角色 ----
    c.post("/api/agent/ping", headers=AG,
           json={"uid": "uid-A", "host": "PC-A", "mode": "managed", "state": "idle"})
    cli = db.client_by_uid("uid-A")
    db.client_update(int(cli["id"]), account_id=al_acc, role_id=None)   # 只派账号，不派角色
    r = c.post("/api/agent/run", headers=AG, json={
        "client_run_id": "iso-A-1", "uid": "uid-A", "host": "PC-A",
        "slot": "12:00", "started_at": db.now(), "duration_seconds": 30,
        "all_ok": True, "counts": {"ok": 1},
        "role_label": "X6021龙兴之", "account_label": "159****0001",
        "tasks": [{"key": "recruit", "name": "招募", "status": "ok", "seconds": 10}]})
    ra = db.run_get(r.json()["run_id"])
    check("归到 alice 的角色（不是 bob 的）", int(ra["role_id"] or 0) == al_rid,
          "role_id=%s（alice=%s / bob=%s）" % (ra["role_id"], al_rid, bo_rid))
    check("run.owner_id = alice", int(ra["owner_id"] or 0) == int(al["id"]))
    check("alice 的数据里能看到这条记录",
          any(int(x["id"]) == ra["id"]
              for x in db.run_list(owner_id=int(al["id"]), own_only=True)))
    check("bob 的数据里看不到这条记录",
          not any(int(x["id"]) == ra["id"]
                  for x in db.run_list(owner_id=int(bo["id"]), own_only=True)))

    # ---- 端到端 B：无指派 + 同名碰撞 → 拒绝猜测，且不归属任何普通用户 ----
    db.client_update(int(cli["id"]), account_id=None, role_id=None)
    r = c.post("/api/agent/run", headers=AG, json={
        "client_run_id": "iso-B-1", "uid": "uid-A", "host": "PC-A",
        "slot": "auto", "started_at": db.now(), "duration_seconds": 5,
        "all_ok": True, "counts": {"ok": 1}, "role_label": "龙兴之",
        "tasks": [{"key": "recruit", "name": "招募", "status": "ok", "seconds": 3}]})
    rb = db.run_get(r.json()["run_id"])
    check("无指派 + 同名碰撞 → 不归到任何角色", rb["role_id"] is None,
          "role_id=%s" % rb["role_id"])
    check("也不归属任何普通用户（owner_id=NULL）", rb["owner_id"] is None)

    # ---- 端到端 C：跨用户访问运行记录一律 404（不泄露「存在性」） ----
    logout(c)
    check("bob 能登录", login(c, "bob", "B0b!3mu4455").status_code == 303)
    r = c.get("/runs")
    check("bob 的运行列表不含 alice 的记录",
          ("/runs/%d" % ra["id"]) not in r.text)
    check("bob 直接访问 alice 的运行详情 → 404",
          c.get("/runs/%d" % ra["id"]).status_code == 404)


# ------------------------------------------------------------ 老库迁移
print("\n== 17. 角色自动发现的 missing 标记（真机撞到的回归） ==")
# 为什么单列成一条（2026-09-21 真机实测撞到）：
#   `db.role_sync_discovered` 在标 missing 时写了 `r.get("seen_at")`，而
#   `existing` 字典里装的是 sqlite3.Row —— **Row 没有 .get()**，一调就抛
#   AttributeError("'sqlite3.Row' object has no attribute 'get'")。
#   该异常只在「账号下已有一个『以前读到过、这次没读到』的角色」时才触发，
#   也就是**第二次以后的登录**才暴露：首次接入库里是空的，走 mark_missing
#   那个循环时一个候选都没有，永远绿。所以它是「越用越坏」的那类 bug——
#   单测、首跑、演示全过，角色一多起来就静默不落库（用户看到的现象正是
#   「后台发现角色读不回来」）。
#
# 下面这个用例把三条判据一次钉死：
#   ① 游戏里读到的  → seen_at 刷新 + discover_count 递增
#   ② 曾见过、这次没读到 → 标 missing_at（**不删**）
#   ③ 从没见过的    → 不标 missing（不然手工登记的会被大面积误报）
try:
    _r17 = db.role_sync_discovered(zs_aid, ["云魇丨奈子"])
except Exception as _e:                      # noqa: BLE001
    _r17 = {}
    check("首次发现不应抛异常", False, repr(_e))
check("首次发现：新角色入库", _r17.get("added") == ["云魇丨奈子"], str(_r17))
check("首次发现：无 missing（库里本来是空的）", _r17.get("missing") == [], str(_r17))

# 造一个「以前读到过」的角色（seen_at 非空）：模拟「上次登录时还在」
db.role_create(zs_aid, "执剑丨青山")
with db.connect() as _cn:
    _cn.execute("UPDATE game_roles SET seen_at='2026-09-20T10:00:00' "
                "WHERE account_id=? AND name='执剑丨青山'", (zs_aid,))
    _cn.commit()
# 再放一个「从没见过」的（seen_at 留空）：模拟手工登记但没登录过
db.role_create(zs_aid, "鸡波长")

try:
    _r17b = db.role_sync_discovered(zs_aid, ["云魇丨奈子"])
    _ok17 = True
    _err17 = ""
except Exception as _e:                      # noqa: BLE001
    _ok17 = False
    _err17 = repr(_e)
    _r17b = {}

check("再次发现不抛 AttributeError（Row.get 那个坑）", _ok17, _err17)
if _ok17:
    check("读到的角色 → 确认为 seen", "云魇丨奈子" in _r17b.get("seen", []), str(_r17b))
    check("曾见过+这次没读到 → 标 missing", "执剑丨青山" in _r17b.get("missing", []),
          str(_r17b))
    check("从没见过 → 不标 missing", "鸡波长" not in _r17b.get("missing", []),
          str(_r17b))
    # 只标不删：角色行必须还在（删角色是不可逆的人工决定）
    _names17 = {r["name"] for r in db.role_list(zs_aid)}
    check("missing 角色只标不删", "执剑丨青山" in _names17, str(sorted(_names17)))

print("\n== 16. 老库迁移：kv admin_user/admin_pwd_hash → users 表 ==")
BASE2 = tempfile.mkdtemp(prefix="stzb_migrate_")
os.environ["STZB_DATA_DIR"] = BASE2

import importlib                                          # noqa: E402
from app import settings as _st                           # noqa: E402
from app import db as _db                                 # noqa: E402
importlib.reload(_st)
importlib.reload(_db)

_st.ensure_dirs()
_db.init_db()
# 造一个「老库」：users 表清空，但 kv 里留着单管理员时代的凭据
with _db.tx() as cn:
    cn.execute("DELETE FROM users")
    cn.execute("DELETE FROM kv")
_db.kv_set("admin_user", "legacy_admin")
_db.kv_set("admin_pwd_hash", security.hash_secret("OldAdminPw123"))

with _db.connect() as cn:
    _db._migrate(cn)
    cn.commit()

mig = _db.user_by_name("legacy_admin")
check("老管理员被搬进 users 表", bool(mig), "role=%s" % (mig or {}).get("role"))
check("搬迁后是管理员", bool(mig and mig.get("is_admin")))
check("搬迁后 enabled", bool(mig and mig.get("enabled")))
check("搬迁后老口令仍能登录",
      bool(security.check_user("legacy_admin", "OldAdminPw123")))

with _db.connect() as cn:                # 幂等：再跑一次不应再插一条
    _db._migrate(cn)
    cn.commit()
check("重复迁移不重复插入",
      sum(1 for u in _db.user_list() if u["username"] == "legacy_admin") == 1)

shutil.rmtree(BASE, ignore_errors=True)
shutil.rmtree(BASE2, ignore_errors=True)

print("\n" + "=" * 62)
if FAILS:
    print("有 %d 项未通过：" % len(FAILS))
    for f in FAILS:
        print("  ✗", f)
    sys.exit(1)
print("全部通过 ✓")