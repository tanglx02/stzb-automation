# -*- coding: utf-8 -*-
"""角色「执行任务模式」（task_plan）的单元测试。

同一套定义有两份实现，分别部署在两端：
  · 服务端 `server/app/plan.py`      —— 后端界面读它、心跳响应里下发它
  · 客户端 `stzb/task_plan.py`       —— 采集端拿到 assignment 后解析它

两份代码是**刻意重复**的（各端独立部署，不能互相 import）。所以这份测试的
第一要务就是**防漂移**：常量、默认值、解析行为、selected_tasks 语义，
两边必须完全一致。以后谁改了一边忘了另一边，这里立刻红。

同时盯住 TASK_KEYS 与采集端真正执行的任务表 `stzb/tasks.py:TASKS` 别对不上 ——
否则后端勾选里会多/少一个任务，客户端要么无视它，要么永远跑不到它。
"""
import copy
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

from stzb import task_plan as C                                  # noqa: E402
from stzb.tasks import TASKS as CLIENT_TASKS                     # noqa: E402
from app import plan as S                                        # noqa: E402

KEYS = ("gongpin", "shuishou", "shijing", "yanwu", "texing", "recruit")


# ------------------------------------------------------------------ 防漂移

def test_constants_in_sync():
    """两端的 TASK_KEYS / SLOTS / MODES 必须一字不差。"""
    print("\n== 常量一致性（server/app/plan.py  vs  stzb/task_plan.py） ==")
    rows = [
        ("TASK_KEYS", S.TASK_KEYS, C.TASK_KEYS),
        ("SLOTS", S.SLOTS, C.SLOTS),
        ("MODES", tuple(S.MODES), tuple(C.MODES)),
        ("MODE_INHERIT", S.MODE_INHERIT, C.MODE_INHERIT),
        ("MODE_CUSTOM", S.MODE_CUSTOM, C.MODE_CUSTOM),
        ("MODE_PAUSED", S.MODE_PAUSED, C.MODE_PAUSED),
    ]
    good = True
    for name, a, b in rows:
        same = tuple(a) == tuple(b)
        good = good and same
        print("   %-14s server=%-46s client=%-46s %s"
              % (name, a, b, "✓" if same else "✗"))
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_task_keys_match_real_tasks():
    """后端勾选的任务 key，必须就是采集端真正会跑的任务。"""
    print("\n== TASK_KEYS 对应采集端真实任务表 stzb/tasks.py:TASKS ==")
    real = tuple(CLIENT_TASKS.keys())
    for k in KEYS:
        ok = k in CLIENT_TASKS
        print("   %-10s %-28s %s" % (k, CLIENT_TASKS.get(k, {}).get("name", "?"),
                                     "✓" if ok else "✗ 采集端没有这个任务！"))
    # 反过来：采集端有的，后端也必须能控 —— 否则后端永远关不掉它
    extra = [k for k in real if k not in KEYS]
    if extra:
        print("   !! 采集端多出后端管不到的任务: %s" % extra)
    good = all(k in CLIENT_TASKS for k in KEYS) and not extra
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_normalize_agrees():
    """同一批（含脏数据的）输入，两端 normalize 出来的 plan 必须一致。"""
    print("\n== normalize 行为一致性（含脏数据） ==")
    cases = [
        ("None", None),
        ("空 dict", {}),
        ("非法 mode", {"mode": "wat"}),
        ("大写 MODE", {"mode": "CUSTOM"}),
        ("合法 custom", {"mode": "custom",
                        "tasks": {"gongpin": True, "shuishou": False},
                        "slots": ["12:00"], "note": "小号"}),
        ("paused", {"mode": "paused"}),
        ("tasks 为列表", {"mode": "custom", "tasks": ["recruit", "shuishou"]}),
        ("slots 空列表→退回全档", {"mode": "custom", "slots": []}),
        ("slots 全非法→退回全档", {"mode": "custom", "slots": ["99:99"]}),
        ("slots 半非法", {"mode": "custom", "slots": ["12:00", "xx"]}),
        ("note 超长", {"note": "n" * 999}),
        ("note 非字符串", {"note": 123}),
        ("plan 是 JSON 文本", json.dumps({"mode": "custom", "slots": ["00:00"]})),
        ("plan 是坏 JSON 文本", "{not json"),
        ("tasks 混合脏值", {"mode": "custom",
                          "tasks": {"gongpin": 1, "recruit": 0, "bogus": True}}),
    ]
    good = True
    for label, raw in cases:
        a = S.normalize_plan(copy.deepcopy(raw))
        b = C.normalize(copy.deepcopy(raw))
        same = a == b
        good = good and same
        print("   %-24s %s" % (label, "✓ 一致" if same else "✗ 不一致"))
        if not same:
            print("      server=%s" % a)
            print("      client=%s" % b)
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_default_plan_is_inert():
    """出厂默认必须是「不改变任何行为」：inherit + 全任务 + 全档位。"""
    print("\n== 默认 plan 必须无副作用 ==")
    d = S.default_plan()
    print("   default=%s" % d)
    good = (d["mode"] == S.MODE_INHERIT
            and all(d["tasks"].values())
            and tuple(d["slots"]) == tuple(S.SLOTS))
    # 默认 → 不限任务、不暂停、两个档位都放行
    good = good and S.selected_tasks(d) is None
    good = good and not S.is_paused(d)
    good = good and all(S.allows_slot(d, s) for s in S.SLOTS)
    # 而且「什么都不填」等价于默认
    good = good and S.normalize_plan(None) == d
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


# ------------------------------------------------------------------ 语义

def test_selected_tasks_semantics():
    """inherit=None / custom=交集勾选 / paused=[]。"""
    print("\n== selected_tasks 语义 ==")
    good = True

    r = S.selected_tasks({"mode": "inherit"})
    good = good and r is None
    print("   inherit                       -> %-24s %s" % (r, "✓" if r is None else "✗"))

    plan = {"mode": "custom",
            "tasks": {k: (k in ("recruit", "shuishou")) for k in KEYS}}
    r = S.selected_tasks(plan)
    good = good and r == ["shuishou", "recruit"]
    print("   custom              无全局限制 -> %-24s %s" % (r, "✓" if r == ["shuishou", "recruit"] else "✗"))

    # 全局把 recruit 关了 → 角色这里勾了也不跑（避免两端打架）
    r = S.selected_tasks(plan, {"recruit": False})
    good = good and r == ["shuishou"]
    print("   同上 + 全局关 recruit        -> %-24s %s" % (r, "✓" if r == ["shuishou"] else "✗"))

    r = S.selected_tasks({"mode": "paused"})
    good = good and r == []
    print("   paused                       -> %-24s %s" % (r, "✓" if r == [] else "✗"))

    empty = {"mode": "custom", "tasks": {k: False for k in KEYS}}
    r = S.selected_tasks(empty)
    good = good and r == []
    print("   custom 一个都没勾            -> %-24s %s" % (r, "✓" if r == [] else "✗"))

    # 客户端侧必须给出一模一样的答案
    r2 = C.selected_tasks(copy.deepcopy(plan), {"recruit": False})
    good = good and r2 == ["shuishou"]
    print("   客户端同一输入               -> %-24s %s" % (r2, "✓" if r2 == ["shuishou"] else "✗"))

    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_allows_slot():
    print("\n== allows_slot 档位过滤 ==")
    good = True
    plan = {"mode": "custom", "slots": ["12:00"]}
    checks = [("00:00", False), ("12:00", True), ("", True)]
    for slot, want in checks:
        got = S.allows_slot(plan, slot)
        gotc = C.allows_slot(copy.deepcopy(plan), slot)
        ok = got == want and gotc == want
        good = good and ok
        print("   slot=%-8r 期望=%-6s server=%-6s client=%-6s %s"
              % (slot, want, got, gotc, "✓" if ok else "✗"))
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_paused():
    print("\n== paused 识别 ==")
    good = True
    for raw, want in [({"mode": "paused"}, True),
                      ({"mode": "custom"}, False),
                      (None, False),
                      ("{bad", False),
                      ({"mode": "PAUSED"}, True)]:
        a, b = S.is_paused(raw), C.is_paused(copy.deepcopy(raw))
        ok = a == want and b == want
        good = good and ok
        print("   %-18s 期望=%-6s server=%-6s client=%-6s %s"
              % (raw, want, a, b, "✓" if ok else "✗"))
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_roundtrip():
    """dumps_plan → loads_plan 必须无损（后端存库用的就是这条链路）。"""
    print("\n== 存库往返（dumps_plan / loads_plan 无损） ==")
    plan = {"mode": "custom",
            "tasks": {k: (k in ("recruit",)) for k in KEYS},
            "slots": ["00:00"], "note": "只招募"}
    text = S.dumps_plan(plan)
    back = S.loads_plan(text)
    ok = back == S.normalize_plan(plan)
    # 客户端也得能解析后端存下来的那段文本
    okc = C.normalize(text) == back
    good = ok and okc
    print("   存储文本: %s" % text)
    print("   读回:     %s" % back)
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def test_bad_input_never_raises():
    """网络来的脏数据绝不能抛异常连累跑任务。"""
    print("\n== 脏数据永不抛异常 ==")
    nasty = [None, 0, "", "[]", "[1,2]", 3.14, True, {"tasks": "x"},
             {"slots": 5}, {"mode": None}, {"tasks": {"gongpin": "yes"}},
             b"\xff\xfe", object()]
    good = True
    for raw in nasty:
        try:
            S.normalize_plan(raw)
            C.normalize(raw)
            S.summary(raw)
            S.summary_text(raw)
            C.summary_text(raw)
        except Exception as e:
            good = False
            print("   !! %r 抛了 %s" % (raw, e))
    print("   %d 组脏输入全部安全返回 %s" % (len(nasty), "✓" if good else "✗"))
    return good


def test_summary_text_agrees():
    """一两行摘要两端措辞可以不同，但档位/任务数必须一致。"""
    print("\n== summary 语义一致（不看措辞，看结论） ==")
    plans = [
        {"mode": "inherit"},
        {"mode": "paused"},
        {"mode": "custom", "tasks": {k: False for k in KEYS}},
        {"mode": "custom", "tasks": {k: (k in ("recruit", "yanwu")) for k in KEYS}},
    ]
    good = True
    for p in plans:
        s = S.summary(p)
        picked_c = [k for k in C.TASK_KEYS if C.normalize(p)["tasks"].get(k)]
        ok = (sorted(s["tasks"]) == sorted(picked_c)
              and s["tasks_count"] == len(picked_c)
              and s["mode"] == C.normalize(p)["mode"])
        good = good and ok
        print("   mode=%-8s server=%d 个 %s | client=%d 个 %s  %s"
              % (s["mode"], s["tasks_count"], s["tasks"],
                 len(picked_c), picked_c, "✓" if ok else "✗"))
    print("   %s" % ("✓ 通过了" if good else "✗ 失败"))
    return good


def main():
    results = []
    for fn in (test_constants_in_sync, test_task_keys_match_real_tasks,
               test_normalize_agrees, test_default_plan_is_inert,
               test_selected_tasks_semantics, test_allows_slot, test_paused,
               test_roundtrip, test_bad_input_never_raises,
               test_summary_text_agrees):
        try:
            results.append((fn.__name__, fn()))
        except Exception:
            import traceback
            traceback.print_exc()
            results.append((fn.__name__, False))

    print("\n" + "=" * 64)
    for name, ok in results:
        print("  %-44s %s" % (name, "✓" if ok else "✗ 失败"))
    bad = [n for n, ok in results if not ok]
    print("=" * 64)
    print("结果：%d/%d 通过" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
