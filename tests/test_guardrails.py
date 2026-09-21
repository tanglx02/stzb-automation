# -*- coding: utf-8 -*-
"""安全边界与远端配置的回归测试。

这套测试专门盯住几条「一旦退回去就会出事、但平时看不出来」的规则：

  1. **两侧白名单必须一致** —— 服务端能下发的段，和客户端允许被覆盖的段，
     必须完全对应。任何一边多写一个段，就是一个隐藏的后门。
  2. **本机独占段一个都不能被下发** —— device / emulator / cloud / logging / account。
     尤其 `account`：服务端能指派「切到哪个账号」，但绝不能关掉「要不要切」，
     否则客户端会停在别人的账号上跑任务而本机毫无察觉。
  3. **未注册客户端拿不到定向任务**（这条是端到端联调抓出来的真实漏洞）。
  4. **心跳/掉线的判定是「按时间算」**，不依赖服务端主动连客户端。

不需要模拟器，也不需要起服务 —— 纯逻辑，随时可跑。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

from stzb import remote_config as rc                              # noqa: E402


def test_whitelist_matches():
    """服务端与客户端的白名单必须一一对应。"""
    print("== 两侧白名单一致性 ==")
    try:
        from app import managed as m
    except Exception as e:
        print("   ! 无法 import 服务端 managed 模块：%r" % (e,))
        print("     （可能没装 fastapi；这条测试需要 server 依赖）")
        return None          # 跳过，不算失败

    ok = True
    ms, cs = set(m.MANAGED_SECTIONS), set(rc.MANAGED_SECTIONS)
    if ms != cs:
        print("   ✗ 可下发段不一致！")
        print("     服务端多出: %s" % sorted(ms - cs))
        print("     客户端多出: %s" % sorted(cs - ms))
        ok = False
    else:
        print("   ✓ 可下发段一致：%s" % sorted(cs))

    ml, cl = set(m.LOCAL_ONLY_SECTIONS), set(rc.LOCAL_ONLY_SECTIONS)
    if ml != cl:
        print("   ✗ 本机独占段不一致！服务端=%s 客户端=%s" % (sorted(ml), sorted(cl)))
        ok = False
    else:
        print("   ✓ 本机独占段一致：%s" % sorted(cl))

    # 本机独占段绝不能被下发
    bad = ms & ml
    if bad:
        print("   ✗ 危险：这些段既在本机独占又在可下发里 → %s" % sorted(bad))
        ok = False
    else:
        print("   ✓ 本机独占段与可下发段无交集")

    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def test_account_section_is_local_only():
    """account 段必须下发不进去 —— 服务端不能关掉「要不要切账号」。"""
    print("\n== account 段必须是本机独占（回归） ==")
    ok = True
    if "account" not in rc.LOCAL_ONLY_SECTIONS:
        print("   ✗ 客户端 LOCAL_ONLY 里没有 account")
        ok = False
    if "account" in rc.MANAGED_SECTIONS:
        print("   ✗ 客户端可下发白名单里有 account —— 服务端能关掉切换了！")
        ok = False

    # 实际拿一段带 account 的载荷试一下，必须被过滤掉
    payload = {
        "tasks": {"recruit": False},
        "account": {"enabled": False, "switch_account": False},
        "device": {"adb": "/evil/adb"},
    }
    out = rc.filter_payload(payload)
    print("   过滤后剩下的段: %s" % sorted(out))
    if "account" in out:
        print("   ✗ account 段漏过去了！")
        ok = False
    if "device" in out:
        print("   ✗ device 段漏过去了！")
        ok = False
    if "tasks" not in out:
        print("   ✗ 该放行的 tasks 段被误拦了")
        ok = False
    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def test_unregistered_client_gets_no_directed_jobs():
    """未注册（无 uid）的客户端只能看到公共任务，拿不到任何定向任务。"""
    print("\n== 未注册客户端拿不到定向任务（回归） ==")
    try:
        from app import db
    except Exception as e:
        print("   ! 无法 import 服务端 db：%r（跳过）" % (e,))
        return None
    import inspect
    src = inspect.getsource(db.request_pending_for)
    # 断言：client_id 为 None 时必须只查 client_id IS NULL
    has_null_filter = "client_id IS NULL" in src or "client_id is null" in src.lower()
    print("   request_pending_for 里有「只取公共任务」的过滤: %s" % has_null_filter)
    if not has_null_filter:
        print("   ✗ 找不到过滤 —— 未注册客户端可能领走别人的定向任务！")
        return False
    print("   ✓ 通过了")
    return True


def test_offline_derived_from_last_seen():
    """在线状态必须是「按 last_seen 算出来的」，而不是存了个 online 字段。"""
    print("\n== 在线状态由 last_seen 推导（不依赖服务端连客户端） ==")
    try:
        from app import db, settings
    except Exception as e:
        print("   ! 无法 import 服务端模块：%r（跳过）" % (e,))
        return None
    ok = True
    src = inspect_source(db._row_client)
    for kw in ("last_seen", "age_seconds", "online"):
        if kw not in src:
            print("   ✗ _row_client 里看不到 %s —— 在线判定逻辑变了？" % kw)
            ok = False
    print("   heartbeat_interval=%s  offline_after=%s"
          % (settings.HEARTBEAT_INTERVAL, settings.CLIENT_OFFLINE_AFTER))
    if settings.CLIENT_OFFLINE_AFTER <= settings.HEARTBEAT_INTERVAL:
        print("   ✗ 掉线阈值不大于心跳间隔 —— 客户端会被误判掉线")
        ok = False
    else:
        print("   ✓ 掉线阈值 %ds > 心跳间隔 %ds（容得下至少一次丢包）"
              % (settings.CLIENT_OFFLINE_AFTER, settings.HEARTBEAT_INTERVAL))
    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def test_no_plaintext_passwords_anywhere():
    """账号相关代码里不能出现密码字段 —— 能力边界是免密切换。"""
    print("\n== 不存密码（账号表里不该有 password 字段） ==")
    try:
        from app import db
    except Exception as e:
        print("   ! 无法 import 服务端 db：%r（跳过）" % (e,))
        return None
    src = inspect_source(db)
    ok = True
    for bad in ("password_hash", "game_password", "login_password"):
        if bad in src:
            print("   ✗ 发现疑似密码字段：%s" % bad)
            ok = False
    if ok:
        print("   ✓ 没有游戏账号密码字段（只存脱敏串用于比对）")
    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


# ------------------------------------------------------------------ 小工具

def test_console_csp_allows_own_scripts():
    """控制台 CSP 必须放行同源脚本、但绝不放内联脚本。

    ★ 这条盯的是一个**静默失效**的真实事故：CSP 曾经写死 `script-src 'none'`，
      而页面里的交互（角色下拉联动、在线状态自动刷新、**删除/轮换令牌前的
      二次确认**）全是内联脚本和内联 onsubmit= —— 浏览器一律不执行，
      而且页面上不报任何错，只有开发者控制台里才看得到。
      结果就是几个不可逆操作没有确认框，还能跑很久没人发现。

    所以这里双向钉住：
      · CSP 的方向：script-src 必须允许 'self'，但**不能**出现 'none' 或
        'unsafe-inline'（前者静默废掉全部交互，后者等于放弃 XSS 防护）；
      · 模板的方向：不许再出现内联 <script> 或 on* 事件处理器
        —— 否则一旦有人把 CSP 收紧回去，又会静默失效。
    """
    import re

    print("\n== 控制台 CSP 放行同源脚本、禁止内联（回归） ==")
    ok = True

    main_py = os.path.join(ROOT, "server", "app", "main.py")
    src = open(main_py, encoding="utf-8").read()
    i = src.find('"Content-Security-Policy"')
    if i < 0:
        print("   ✗ main.py 里找不到 Content-Security-Policy")
        return False
    # CSP 是多个相邻字符串字面量拼起来的，这里把它们凑回一整条
    block = src[i:src.find(")", i)]
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    csp = "".join(parts[1:])
    m = re.search(r"script-src ([^;]*)", csp)
    val = (m.group(1) if m else "").strip()
    print("   script-src 指令：%r" % val)

    if "'self'" not in val:
        print("   ✗ script-src 没有 'self' —— 外部 console.js 会被挡掉")
        ok = False
    if "'none'" in val:
        print("   ✗ script-src 是 'none' —— 控制台交互会**静默**失效（曾经的事故）")
        ok = False
    if "'unsafe-inline'" in val:
        print("   ✗ script-src 放了 'unsafe-inline' —— 等于放弃 XSS 防护")
        ok = False
    if "connect-src 'self'" not in csp:
        print("   ✗ 没有 connect-src 'self' —— 自动刷新用的 fetch 会被挡")
        ok = False

    tpl_dir = os.path.join(ROOT, "server", "app", "templates")
    bad = []
    for name in sorted(os.listdir(tpl_dir)):
        if not name.endswith(".html"):
            continue
        html = open(os.path.join(tpl_dir, name), encoding="utf-8").read()
        html = re.sub(r"\{#.*?#\}", "", html, flags=re.S)      # 去掉 jinja 注释
        if re.search(r"<script(?![^>]*\bsrc=)", html, re.I):
            bad.append("%s: 内联 <script>" % name)
        if re.search(r"\son(click|submit|change|input|load|error)\s*=", html, re.I):
            bad.append("%s: 内联 on* 处理器" % name)
    if bad:
        print("   ✗ 模板里还有内联脚本/处理器（会被 CSP 静默拒掉）：%s" % bad)
        ok = False
    else:
        print("   ✓ 模板里零内联脚本、零内联事件处理器")

    js = os.path.join(ROOT, "server", "app", "static", "console.js")
    if not os.path.exists(js):
        print("   ✗ 缺少 /static/console.js（外部脚本没了，交互全废）")
        ok = False
    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def test_realtime_contract():
    """实时通道两侧的契约：BUSY 标记一致、两条通道共用同一份状态计算。

    这两条都属于「改错了平时看不出来、出事时很难查」的类型：
      · BUSY 前缀两边不一致 → 客户端回报的「正忙」服务端认不出 →
        指令被当成普通失败消费掉 → 管理端点了「探测」永远不执行；
      · 状态计算被复制成两份 → 同一台客户端走长连接和走心跳拿到的
        「该不该切换」可能不一样，且只在某条通道上复现。
    """
    print("\n== 实时通道两侧契约（回归） ==")
    ok = True
    try:
        from stzb.realtime import BUSY_PREFIX as CLIENT_BUSY, Realtime
        from stzb.heartbeat import Heartbeat
    except Exception as e:
        print("   ! 无法 import 客户端模块：%r（跳过）" % (e,))
        return None
    try:
        from app.routes_agent import BUSY_PREFIX as SERVER_BUSY, build_client_state
    except Exception as e:
        print("   ! 无法 import 服务端模块：%r（跳过）" % (e,))
        return None

    if CLIENT_BUSY != SERVER_BUSY:
        print("   ✗ BUSY 前缀不一致：客户端 %r / 服务端 %r" % (CLIENT_BUSY, SERVER_BUSY))
        ok = False
    else:
        print("   ✓ BUSY 前缀两侧一致：%r" % CLIENT_BUSY)

    # 状态计算必须只有一份：HTTP 心跳的处理里要能看到对它的调用
    from app import routes_agent as ra
    src = inspect_source(ra._heartbeat)
    if "build_client_state" not in src:
        print("   ✗ _heartbeat 没有调用 build_client_state —— 状态计算被复制了一份？")
        ok = False
    else:
        print("   ✓ HTTP 心跳与长连接共用 build_client_state（同一份状态计算）")

    # 两种会话对上层暴露的接口要一致，否则换一个类就会在运行期炸
    shared = ["start", "stop", "poke", "snapshot"]
    missing = [n for n in shared if not (hasattr(Realtime, n) and hasattr(Heartbeat, n))]
    if missing:
        print("   ✗ Realtime / Heartbeat 接口不一致，缺：%s" % missing)
        ok = False
    else:
        print("   ✓ Realtime 与 Heartbeat 的对外接口一致（%s）" % "、".join(shared))

    # 上层读的那些状态字段，两个类都得有（否则换类时会在运行期 AttributeError）
    class _FakeClient:
        base_url = "http://127.0.0.1:1"
        token = "t"
        version = ""

    class _FakeIdent:
        uid = "u"
        host = "h"

    fields = ("assignment", "switch_needed", "switch_reason", "task_paused",
              "last_ok", "last_error", "last_at", "beats", "interval")
    a = Realtime(_FakeClient(), _FakeIdent())
    b = Heartbeat(_FakeClient(), _FakeIdent())
    lost = [f for f in fields if not (hasattr(a, f) and hasattr(b, f))]
    if lost:
        print("   ✗ 会话状态字段缺失：%s" % lost)
        ok = False
    else:
        print("   ✓ 会话状态字段两边齐全：%s" % "、".join(fields))
    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def inspect_source(fn):
    import inspect
    try:
        return inspect.getsource(fn)
    except Exception:
        return ""


def test_role_name_vertical_normalized():
    """角色名的「竖线类字符」必须在前后端收敛到同一个形状。

    为什么这条要进护栏（2026-09-20 实机发现）：
      游戏角色名大量用中文竖线「丨」(U+4E28) 当中缀（「执剑丨青山」「云魇丨奈子」），
      而**人在后台手打时几乎必然打成 ASCII 竖线 `|`** —— 两个码点长得几乎一样，
      眼睛分不出来。若不做归一化，同一个角色在后端视角会变成两个人：
        · 后端认为是两个不同角色 → 自动发现重复建角色；
        · 指派比对 `role_key` 永远不等 → **天天白切一遍**（正是「只认角色名」
          这套设计要防的问题）。
    实测：不归一化时「执剑|青山」vs「执剑丨青山」只得 0.809 分，不是 1.0。
    """
    print("== 角色名竖线归一化（前后端一致）==")
    ok = True

    from stzb import account as A
    from app import db as _db

    # ① 两侧的「竖线字符表」必须一字不差
    cli = getattr(A, "_VERT_CHARS", None)
    srv = getattr(_db, "_VERT_CHARS", None)
    if not cli or not srv:
        print("   ✗ 有一侧没有 _VERT_CHARS：客户端=%r 服务端=%r" % (cli, srv))
        return False
    if set(cli) != set(srv):
        print("   ✗ 两侧竖线字符表不一致")
        print("      客户端：%s" % cli)
        print("      服务端：%s" % srv)
        ok = False
    else:
        print("   ✓ 两侧竖线字符表一致（%d 个字符）" % len(cli))

    # ② 统一后的形状必须相同
    if A._norm_vert("a|b丨c｜d") != _db.normalize_vert("a|b丨c｜d"):
        print("   ✗ 两侧归一化结果不同：%r vs %r"
              % (A._norm_vert("a|b丨c｜d"), _db.normalize_vert("a|b丨c｜d")))
        ok = False
    else:
        print("   ✓ 两侧归一化结果一致：%r" % _db.normalize_vert("a|b丨c｜d"))

    # ③ 手打 ASCII 竖线 与 OCR 读出的中文竖线，必须判为同一个角色
    pairs = [("执剑丨青山", "执剑|青山"), ("云魇丨奈子", "云魇|奈子"),
             ("执剑丨青山", "执剑｜青山")]
    for a, b in pairs:
        ka, kb = _db.role_key(a), _db.role_key(b)
        sc = A._one_score(a, b)
        if ka != kb or sc < 1.0:
            print("   ✗ 「%s」与「%s」没收敛：role_key %r vs %r，score=%.3f"
                  % (a, b, ka, kb, sc))
            ok = False
        else:
            print("   ✓ 「%s」≡「%s」（score=1.0）" % (a, b))

    # ④ ★ 反向：绝不能把真正的字母也归一化掉（I / l / 1 不在表里）
    for a, b in [("Iron", "lron"), ("lulu", "1u1u")]:
        if _db.role_key(a) == _db.role_key(b):
            print("   ✗ 误伤：把「%s」和「%s」合并成了同一个角色" % (a, b))
            ok = False
    for bad in "Il1":
        if bad in (cli or ""):
            print("   ✗ 危险：竖线字符表里混进了字母/数字 %r，会误伤真名字" % bad)
            ok = False
    if ok:
        print("   ✓ 未误伤字母名字（`I`/`l`/`1` 都不在表内）")

    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def test_password_rules_are_single_source():
    """改口令的强度校验必须只有**一处**实现。

    为什么进护栏（2026-09-20 真实分叉）：
      `/settings/password`（管理员）当初单独实现，只查了 `len(new1) >= 8`，
      没走 `security.password_problem()`；而 `/password`（普通用户）走了。
      后果是**同一条规则两个入口两套标准**：管理员能在设置页把口令改成
      `12345678`，普通用户却改不了。这种「A 能做的事 B 不能」不会报错，
      只会让人觉得规则随机，属于最难查的一类问题。

    现在两个入口都走 `routes_ui._apply_password_change`。这条测试盯两件事：
      ① 两处都调用了同一个 helper；
      ② `_apply_password_change` 里确实调了 `security.password_problem`。
    """
    print("== 改口令的强度校验只有一处实现 ==")
    ok = True
    import inspect
    from app import routes_ui as R

    src_all = inspect.getsource(R)
    helper = getattr(R, "_apply_password_change", None)
    if helper is None:
        print("   ✗ 找不到 _apply_password_change")
        return False
    if "password_problem" not in inspect.getsource(helper):
        print("   ✗ _apply_password_change 没走 password_problem（弱口令能过）")
        ok = False
    else:
        print("   ✓ _apply_password_change 走了 password_problem")

    # 两个改密路由都必须转手给同一个 helper，不能各写一套
    for fn_name, path in (("password_change", "/password"),
                          ("change_password", "/settings/password")):
        fn = getattr(R, fn_name, None)
        if fn is None:
            print("   ✗ 找不到路由函数 %s" % fn_name)
            ok = False
            continue
        body = inspect.getsource(fn)
        if "_apply_password_change" not in body:
            print("   ✗ %s（%s）没走统一 helper，可能又分叉了" % (fn_name, path))
            ok = False
        else:
            print("   ✓ %s（%s）走统一 helper" % (fn_name, path))

    # 反向：路由里不该再出现自己写的长度/弱口令判断
    for marker in ('len(new1) < 8', 'len(new2) < 8'):
        if marker in src_all:
            print("   ✗ 路由里还残留手写的长度校验 %r" % marker)
            ok = False

    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def _route_has_admin(src, path):
    """返回 POST <path> 那个路由函数体里是否出现 require_admin。

    用「装饰器到下一个装饰器」切块，避免把上一个函数的 require_admin
    误算给下一个函数（跨函数误判正是这类源码级检查最容易翻车的地方）。
    """
    import re as _re
    pat = '@router.post("%s"' % path
    idx = src.replace("'", '"').find(pat)
    if idx < 0:
        return False
    rest = src[idx:]
    nxt = rest.find("\n@router.", 1)
    body = rest if nxt < 0 else rest[:nxt]
    return "require_admin" in body


def test_admin_routes_are_guarded():
    """所有「主机级」POST 路由必须挂 require_admin。

    为什么进护栏：多用户下最容易犯的错是「加了权限判断，但漏了某个路由」。
    漏一个的后果是越权（普通用户能改客户端指派 / 轮换令牌 / 删别人），
    而它在页面上完全看不出来 —— 按钮藏了，但 POST 直接打过去照样成立。

    这条用**源码级**检查兜底：扫 `routes_ui.py`，凡路径属于主机级前缀的
    写操作，函数体里必须出现 require_admin。它不替代隔离测试，
    但能挡住「新加一个路由忘了加权限」这种最常见的回归。
    """
    print("== 主机级写操作都挂了 require_admin ==")
    ok = True
    import inspect
    import re as _re
    from app import routes_ui as R, db

    src = inspect.getsource(R)
    # 主机级前缀：客户端调度 / 配置 / 设置 / 用户管理 / 设备管理
    #
    # 为什么 /devices 必须在这里（2026-09-21 补）：
    #   设备管理是**主机级**能力——它对着某台电脑下发 ADB 指令（启停模拟器、
    #   覆盖分辨率、读角色），谁拿到谁就能操作别人的机器。但当初加设备路由时
    #   忘了把 /devices 写进这张表，于是这十几条路由处在**护栏盲区**里：
    #   全程靠人工核对权限，一旦哪天新增一条忘了挂 require_admin，
    #   本测试一声不吭地放行。设备路由已经全挂对了，这里只是把盲区补掉。
    #
    # 注意 /accounts/* **不**进这张表：账号是本用户私有资产，走 owner_id 隔离，
    #   普通用户改自己的账号属正常操作。唯一的例外是 /accounts/{aid}/discover
    #   （要下发到客户端去读角色，属主机级），它单独在下面 DEVICE_OPS 里钉住。
    GUARDED = ("/clients", "/config", "/settings", "/users", "/devices")
    # 拆出每个 @router.<verb>("<path>") 到下一个装饰器之间的函数体
    chunks = _re.split(r"\n@router\.", src)
    problems = []
    checked = 0
    for ch in chunks:
        m = _re.match(r'(get|post|delete|put|patch)\("([^"]+)"', ch)
        if not m:
            continue
        verb, path = m.group(1), m.group(2)
        if verb == "get":
            continue                      # 读操作另有口径（如 /clients 对普通用户只读开放）
        base = path.split("/{")[0] or "/"
        if not any(path.startswith(p) for p in GUARDED):
            continue
        checked += 1
        if "require_admin" not in ch:
            problems.append("%-6s %-28s 缺 require_admin" % (verb.upper(), path))

    for p in problems:
        print("   ✗ %s" % p)
        ok = False
    if ok:
        print("   ✓ %d 个主机级写操作都有 require_admin" % checked)

    # 反向：客户端的「指派 / 探测 / 改名 / 删除 / 暂停」一个都不能漏
    must = ["/clients/{cid}/assign", "/clients/{cid}/probe", "/clients/{cid}/rename",
            "/clients/{cid}/delete", "/clients/{cid}/toggle",
            "/settings/rotate-token", "/users"]
    for path in must:
        pat = '@router.%s("%s"' % ("post", path)
        if pat not in src.replace("'", '"'):
            print("   · 提示：没找到 %s（路径可能改了，请同步本测试）" % path)

    # 设备指令路由：一条漏挂 require_admin 就等于把「操作别人电脑」开放给所有人。
    # 前缀扫描（GUARDED）能覆盖大部分，但 /accounts/{aid}/discover 不在任何主机级
    # 前缀下，必须单独钉死；顺带把设备页所有写操作列成显式清单，
    # 这样以后新增设备路由时，测试会因为「清单里没有」而提醒同步。
    DEVICE_OPS = [
        "/devices/scan", "/devices/add",
        "/devices/{did}/update", "/devices/{did}/assign", "/devices/{did}/delete",
        "/devices/emulator/start", "/devices/emulator/stop",
        "/devices/{did}/force-size", "/devices/{did}/restore-size",
        "/devices/{did}/discover", "/accounts/{aid}/discover",
    ]
    for path in DEVICE_OPS:
        pat = '@router.post("%s"' % path
        if pat not in src.replace("'", '"'):
            print("   · 提示：没找到设备路由 %s（可能改了名，请同步本测试）" % path)
            continue
        if not _route_has_admin(src, path):
            print("   ✗ POST %-30s 缺 require_admin（可越权操作他人设备）" % path)
            ok = False

    print("   %s" % ("✓ 通过了" if ok else "✗ 失败"))
    return ok


def main():
    results = []
    for fn in (test_whitelist_matches, test_account_section_is_local_only,
               test_unregistered_client_gets_no_directed_jobs,
               test_offline_derived_from_last_seen,
               test_no_plaintext_passwords_anywhere,
               test_console_csp_allows_own_scripts,
               test_realtime_contract,
               test_role_name_vertical_normalized,
               test_password_rules_are_single_source,
               test_admin_routes_are_guarded):
        try:
            results.append((fn.__name__, fn()))
        except Exception:
            import traceback
            traceback.print_exc()
            results.append((fn.__name__, False))

    print("\n" + "=" * 62)
    skipped = 0
    for name, ok in results:
        if ok is None:
            print("  %-46s － 跳过" % name)
            skipped += 1
        else:
            print("  %-46s %s" % (name, "✓" if ok else "✗ 失败"))
    bad = [n for n, ok in results if ok is False]
    ran = len(results) - skipped
    print("=" * 62)
    print("结果：%d/%d 通过%s"
          % (ran - len(bad), ran, "（%d 项因缺 server 依赖跳过）" % skipped if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())