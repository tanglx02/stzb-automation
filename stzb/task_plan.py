# -*- coding: utf-8 -*-
"""角色「执行任务模式」的客户端侧解析。

后端 `server/app/plan.py` 是同一套定义的服务端实现。两边各自独立部署，
所以代码是**刻意重复**的 —— 但 `tests/test_role_plan.py` 里有一条断言
专门盯着这两份常量与行为别漂移，改了一边不改另一边会立刻红。

一个角色可以被指定：
  · **跑不跑**（paused）
  · **跑哪几个任务**（custom 模式下的勾选）
  · **在哪几个档位跑**（00:00 / 12:00）

客户端拿到的是心跳响应 `assignment.task_plan` 里那个 dict。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

# ⚠ 必须与 server/app/plan.py 的 TASK_KEYS / SLOTS 保持一致
TASK_KEYS: tuple = ("gongpin", "shuishou", "shijing", "yanwu", "texing", "recruit")
SLOTS: tuple = ("00:00", "12:00")

MODE_INHERIT = "inherit"
MODE_CUSTOM = "custom"
MODE_PAUSED = "paused"
MODES = (MODE_INHERIT, MODE_CUSTOM, MODE_PAUSED)


def normalize(raw: Any) -> Dict[str, Any]:
    """把任意输入整成合法 plan。**绝不抛异常** —— 它来自网络，不能连累跑任务。"""
    if isinstance(raw, (str, bytes)) and raw:
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return {"mode": MODE_INHERIT,
                "tasks": {k: True for k in TASK_KEYS},
                "slots": list(SLOTS), "note": ""}

    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in MODES:
        mode = MODE_INHERIT

    tasks = {k: True for k in TASK_KEYS}
    raw_tasks = raw.get("tasks")
    if isinstance(raw_tasks, dict):
        for k in TASK_KEYS:
            if k in raw_tasks:
                tasks[k] = bool(raw_tasks[k])
    elif isinstance(raw_tasks, (list, tuple)):
        want = {str(x).strip() for x in raw_tasks}
        tasks = {k: (k in want) for k in TASK_KEYS}

    slots: List[str] = []
    raw_slots = raw.get("slots")
    if isinstance(raw_slots, (list, tuple)):
        slots = [s for s in (str(x).strip() for x in raw_slots) if s in SLOTS]
    if not slots:
        # 空列表等于「哪个档都不跑」，几乎肯定是配错了 → 退回全档位
        slots = list(SLOTS)

    note = raw.get("note")
    return {"mode": mode, "tasks": tasks, "slots": slots,
            "note": str(note)[:400] if isinstance(note, (str, bytes)) else ""}


def is_paused(plan: Any) -> bool:
    return normalize(plan)["mode"] == MODE_PAUSED


def selected_tasks(plan: Any,
                   global_tasks: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
    """这个角色本轮该跑哪些任务。

    None  → 不做限制（跟随全局配置 / 按档位跑全部）
    [...] → 只跑这几个（空列表 = 什么都不跑）
    """
    p = normalize(plan)
    if p["mode"] == MODE_PAUSED:
        return []
    if p["mode"] != MODE_CUSTOM:
        return None
    picked = [k for k in TASK_KEYS if p["tasks"].get(k)]
    if isinstance(global_tasks, dict):
        picked = [k for k in picked if global_tasks.get(k, True)]
    return picked


def allows_slot(plan: Any, slot: str) -> bool:
    if not slot:
        return True
    return slot in normalize(plan)["slots"]


def summary_text(plan: Any) -> str:
    p = normalize(plan)
    if p["mode"] == MODE_PAUSED:
        return "暂停执行"
    if p["mode"] == MODE_INHERIT:
        return "跟随后端全局配置"
    picked = [k for k in TASK_KEYS if p["tasks"].get(k)]
    if not picked:
        return "自定义：无任务"
    return "自定义：%d/%d 个任务" % (len(picked), len(TASK_KEYS))
