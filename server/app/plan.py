# -*- coding: utf-8 -*-
"""角色「执行任务模式」的定义与解析。

一个角色可以独立指定：
  · **跑不跑**（paused）
  · **跑哪几个任务**（custom 模式下的勾选）
  · **在哪几个档位跑**（00:00 / 12:00）

存储形态（`game_roles.task_plan` 一列，JSON 文本）::

    {
      "mode": "inherit" | "custom" | "paused",
      "tasks": {"gongpin": true, "shuishou": false, ...},
      "slots": ["00:00", "12:00"],
      "note": ""
    }

三种模式的含义：

| mode      | 含义                                                       |
|-----------|------------------------------------------------------------|
| `inherit` | **默认**。完全跟随后端「任务配置」页里的全局开关（= 改造前的行为） |
| `custom`  | 只用这里勾选的任务；全局开关里被关掉的任务，这里勾了也不跑     |
| `paused`  | 该角色**整个不跑**（客户端心跳拿到后直接跳过，不切换、不执行）  |

> 为什么要有 `inherit`：让「多角色分别定制」是**可选的**。
> 只用一个角色的用户不用配任何东西，行为与以前完全一致。
> 用多个角色（比如大号要全套、小号只要招募）时才切到 `custom`。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

# 任务 key —— 必须与采集端 stzb/tasks.py 的 TASKS 保持一致。
# tests/test_role_plan.py 里有一条断言专门盯着这两边别漂移。
TASK_KEYS: tuple = ("gongpin", "shuishou", "shijing", "yanwu", "texing", "recruit")

# 档位：游戏每天 00:00 与 12:00 各刷新一次
SLOTS: tuple = ("00:00", "12:00")

MODE_INHERIT = "inherit"
MODE_CUSTOM = "custom"
MODE_PAUSED = "paused"
MODES = (MODE_INHERIT, MODE_CUSTOM, MODE_PAUSED)

MODE_LABEL = {
    MODE_INHERIT: "跟随后端全局配置",
    MODE_CUSTOM: "单独指定（自定义）",
    MODE_PAUSED: "暂停执行",
}


def default_plan() -> Dict[str, Any]:
    """出厂默认：不改变任何行为。"""
    return {
        "mode": MODE_INHERIT,
        "tasks": {k: True for k in TASK_KEYS},
        "slots": list(SLOTS),
        "note": "",
    }


def normalize_plan(raw: Any) -> Dict[str, Any]:
    """把任意输入（JSON 文本 / dict / None / 脏数据）整成合法 plan。

    宁可用默认值兜底，也不要抛异常 —— 这个值会在心跳响应里往返，
    解析失败绝不能连累客户端。
    """
    if isinstance(raw, (str, bytes)) and raw:
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return default_plan()

    d = default_plan()
    mode = str(raw.get("mode") or "").strip().lower()
    d["mode"] = mode if mode in MODES else MODE_INHERIT

    tasks = raw.get("tasks")
    if isinstance(tasks, dict):
        for k in TASK_KEYS:
            if k in tasks:
                d["tasks"][k] = bool(tasks[k])
    elif isinstance(tasks, (list, tuple)):
        # 宽容处理：也接受 ["recruit", "shuishou"] 这种写法
        want = {str(x).strip() for x in tasks}
        d["tasks"] = {k: (k in want) for k in TASK_KEYS}

    slots = raw.get("slots")
    if isinstance(slots, (list, tuple)):
        keep = [s for s in (str(x).strip() for x in slots) if s in SLOTS]
        # 空列表没有意义（等于什么都不跑）→ 退回默认全档位，避免误配置成"静默不跑"
        d["slots"] = keep or list(SLOTS)

    note = raw.get("note")
    if isinstance(note, (str, bytes)):
        d["note"] = str(note)[:400]
    return d


def dumps_plan(plan: Any) -> str:
    return json.dumps(normalize_plan(plan), ensure_ascii=False)


def loads_plan(raw: Any) -> Dict[str, Any]:
    return normalize_plan(raw)


def is_paused(plan: Any) -> bool:
    return normalize_plan(plan)["mode"] == MODE_PAUSED


def selected_tasks(plan: Any, global_tasks: Optional[Dict[str, Any]] = None
                   ) -> Optional[List[str]]:
    """算出这个角色本轮该跑哪些任务。

    返回 None 表示「不做限制」（跟随全局/按档位跑全部），
    返回列表表示「只跑这几个」。

    · mode=inherit → None（交给全局 tasks 开关与档位去决定）
    · mode=custom  → 勾选的任务 ∩ 全局开启的任务（全局关掉的一律不跑）
    · mode=paused  → []（不跑）
    """
    p = normalize_plan(plan)
    if p["mode"] == MODE_PAUSED:
        return []
    if p["mode"] != MODE_CUSTOM:
        return None
    picked = [k for k in TASK_KEYS if p["tasks"].get(k)]
    if isinstance(global_tasks, dict):
        # 全局被关掉的任务，角色这里勾了也不算 —— 避免「后端关了某个任务，
        # 客户端却因为角色勾选又把它跑起来」这种「两边打架」的困惑。
        picked = [k for k in picked if global_tasks.get(k, True)]
    return picked


def allows_slot(plan: Any, slot: str) -> bool:
    p = normalize_plan(plan)
    if not slot:
        return True
    return slot in p["slots"]


def summary(plan: Any) -> Dict[str, Any]:
    """给界面用的一句话摘要。"""
    p = normalize_plan(plan)
    picked = [k for k in TASK_KEYS if p["tasks"].get(k)]
    return {
        "mode": p["mode"],
        "mode_label": MODE_LABEL.get(p["mode"], p["mode"]),
        "tasks": picked,
        "tasks_count": len(picked),
        "slots": list(p["slots"]),
        "note": p["note"],
    }


def summary_text(plan: Any) -> str:
    """给列表/日志用的一行文字。"""
    p = normalize_plan(plan)
    if p["mode"] == MODE_PAUSED:
        return "暂停执行"
    if p["mode"] == MODE_INHERIT:
        return "跟随后端全局配置"
    picked = [k for k in TASK_KEYS if p["tasks"].get(k)]
    if not picked:
        return "自定义：无任务（不会执行任何任务）"
    return "自定义：%d/%d 个任务（%s）" % (
        len(picked), len(TASK_KEYS), "、".join(picked))
