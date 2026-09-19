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

def inspect_source(fn):
    import inspect
    try:
        return inspect.getsource(fn)
    except Exception:
        return ""


def main():
    results = []
    for fn in (test_whitelist_matches, test_account_section_is_local_only,
               test_unregistered_client_gets_no_directed_jobs,
               test_offline_derived_from_last_seen,
               test_no_plaintext_passwords_anywhere):
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