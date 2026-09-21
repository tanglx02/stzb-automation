# -*- coding: utf-8 -*-
"""从后端拉远程配置，合并进本地 config.json（只在内存里合并，不写回磁盘）。

**安全边界（和 server/app/managed.py 一一对应）：**
只允许覆盖下面白名单里的「业务段」。下面这些段永远以本机为准：
    device / emulator / cloud / account
理由：
  · device   改错了 → 连不上模拟器，且你人在外面根本没法修
  · emulator 同上，MuMuManager 路径、虚拟机索引
  · cloud    后端地址与令牌是本机凭据，不能由服务端下发（否则服务器一挂全都失联）
  · account  账号/角色切换开关。后端能「指派切到哪个账号」，但**不能把切换功能本身
             关掉** —— 否则服务端一旦被误改，客户端就会停在别人的账号上跑任务，
             而你在本机还没有任何办法发现。开关属于本机控制权。

`logging`（日志/截图的保留与清理策略）**可以由服务端下发** —— 它属于「运维策略」，
服务端统一调配很合理，且改错了最多是磁盘占用不合预期，**不会让脚本跑不起来**
（对比 device/emulator 改错就直接连不上）。本机配置工具里改也依然有效：
本机 config.json 永远是底，远端只是覆盖。
两边各有一份同样的白名单，任何一边写错都不会把本地配置搞坏。

合并方式是**深合并**：只覆盖远端确实带了的键，本地多出来的键（比如 `_说明` 注释）
原样保留。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable, Dict, Optional, Tuple

# 段名 -> 允许的键（None 表示该段全放行）
MANAGED_SECTIONS: Dict[str, Any] = {
    "tasks": None,
    "recruit": None,
    "shijing": None,
    "shuishou": None,
    "texing": None,
    "yanwu": None,
    "gongpin": None,
    "safety": None,
    # 日志/截图保留策略：服务端可统一调配。只放行「保留与清理」相关键，
    # `save_screens` 也放行（省磁盘的运维手段，改错只影响能否倒查，不影响跑任务）。
    "logging": ("keep_days", "shots_keep_days", "shots_max_mb",
                "log_keep_days", "cleanup_diag", "cleanup_enabled", "save_screens"),
}

LOCAL_ONLY_SECTIONS = ("device", "emulator", "cloud", "account")


def filter_payload(payload: Any) -> Dict[str, Any]:
    """只保留白名单段。服务端已经过滤过一次，这里是第二道。"""
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for section, allowed in MANAGED_SECTIONS.items():
        val = payload.get(section)
        if not isinstance(val, dict):
            continue
        if allowed is None:
            out[section] = dict(val)
        else:
            out[section] = {k: v for k, v in val.items() if k in allowed}
    return out


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """把 patch 深合并进 base（就地把 base 改掉并返回）。"""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def diff_summary(before: Dict[str, Any], after: Dict[str, Any],
                 path: str = "") -> list:
    """列出到底哪些键被远端改掉了，便于日志里写清楚。"""
    out = []
    for k, v in after.items():
        p = "%s.%s" % (path, k) if path else k
        old = before.get(k, "<<不存在>>")
        if isinstance(v, dict):
            if not isinstance(old, dict):
                old = {}
            out.extend(diff_summary(old, v, p))
        elif isinstance(v, list):
            if list(old or []) != list(v):
                out.append("%s: %s → %s" % (p, old, v))
        elif old != v:
            out.append("%s: %s → %s" % (p, old, v))
    return out


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def apply_remote(cfg: Dict[str, Any], client, state_path: str,
                 logger: Callable[[str], None] = print,
                 force: bool = False) -> Dict[str, Any]:
    """拉取并合并。返回 {ok, version, changed, note, applied:[...]}。

    拉不到（网络/鉴权失败）时**保持本地配置不变**，只记一条日志 ——
    绝不能因为后端连不上就导致任务跑不了。
    """
    res = {"ok": False, "version": 0, "changed": False, "note": "",
           "applied": [], "error": ""}
    state = load_state(state_path)
    res["version"] = int(state.get("version") or 0)

    try:
        ok, data = client.fetch_config(version=res["version"])
    except Exception as e:
        res["error"] = repr(e)
        logger("  ! 拉远端配置异常：%r（继续用本地配置）" % (e,))
        return res

    if not ok:
        res["error"] = str(data)
        logger("  ! 拉远端配置失败：%s（继续用本地配置）" % data)
        return res
    if not isinstance(data, dict):
        res["error"] = "返回格式不对"
        logger("  ! 远端配置格式不对，忽略（继续用本地配置）")
        return res

    ver = int(data.get("version") or 0)
    payload = filter_payload(data.get("payload"))
    res["ok"] = True
    res["version"] = ver
    res["note"] = str(data.get("note") or "")

    # 版本号一样就不重复覆盖（除非 force）
    if ver == int(state.get("version") or -1) and not force:
        logger("  · 远端配置未变化（v%d），用本地配置" % ver)
        state["checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
        save_state(state_path, state)
        return res
    if ver <= 0 and not force:
        logger("  · 服务端还没配过（v0），用本地配置")
        save_state(state_path, {**state, "version": ver,
                                "checked_at": dt.datetime.now().isoformat(timespec="seconds")})
        return res

    snapshot = {k: json.loads(json.dumps(cfg.get(k))) if isinstance(cfg.get(k), dict) else cfg.get(k)
                for k in MANAGED_SECTIONS}
    deep_merge(cfg, payload)
    applied = []
    for sec in payload:
        applied.extend(diff_summary(snapshot.get(sec) or {}, payload.get(sec) or {}, sec))
    res["changed"] = bool(applied)
    res["applied"] = applied

    if applied:
        logger("  · 已应用远端配置 v%d，改动 %d 处：" % (ver, len(applied)))
        for line in applied[:12]:
            logger("      %s" % line)
        if len(applied) > 12:
            logger("      …（还有 %d 处）" % (len(applied) - 12))
    else:
        logger("  · 远端配置 v%d 与本地一致，无需改动" % ver)

    save_state(state_path, {
        "version": ver,
        "note": res["note"],
        "applied": applied,
        "applied_at": dt.datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    })
    return res


def pick_job(client, logger: Callable[[str], None] = print,
             serial: str = "") -> Optional[Dict[str, Any]]:
    """领一条待执行请求。没有就返回 None；出错也不抛。

    `serial` = 本进程已经定下来的目标设备（来自 --serial / STZB_DEVICE_SERIAL）。
    ★ 为什么要按它过滤：任务可以**指定在哪台设备上跑**（后端加的）。
      如果本进程已经锁定用手机跑，却领了一条「指定模拟器」的任务，
      就会在手机上执行模拟器的任务 —— **在错误的设备上点游戏**，
      后果可能是花掉另一台设备上那个号的资源。所以：
        · 任务没指定设备 → 谁都能领（通用任务，按本机配置选设备）
        · 任务指定的设备 == 本进程的目标 → 能领
        · 任务指定了别的设备 → **跳过**，留给挂着那台设备的客户端
      这样即使后端漏了定向（老库里已有、或人工改库），也不会跑错设备。
    """
    try:
        ok, data = client.list_jobs()
    except Exception as e:
        logger("  ! 拉待执行任务异常：%r" % (e,))
        return None
    if not ok or not isinstance(data, dict):
        logger("  ! 拉待执行任务失败：%s" % (data,))
        return None
    jobs = data.get("jobs") or []
    if not jobs:
        return None
    mine = (serial or "").strip()
    job = None
    for j in jobs:
        want = str(j.get("serial") or "").strip()
        if not want or not mine or want == mine:
            job = j
            break
        logger("  · 跳过任务 #%s：它指定在 %s 上跑，本机锁的是 %s"
               % (j.get("id"), want, mine))
    if job is None:
        return None
    if not client.take_job(int(job["id"])):
        logger("  ! 任务 #%s 领取失败（可能已被领走）" % job.get("id"))
        return None
    logger("  · 领到待执行任务 #%s（档位=%s 范围=%s%s%s）"
           % (job.get("id"), job.get("slot"),
              job.get("only") or "按档位全部",
              "，预演" if job.get("dry_run") else "",
              ("，设备=%s" % job["serial"]) if job.get("serial") else ""))
    if job.get("note"):
        logger("      备注：%s" % job["note"])
    return job


def build_payload_from_report(report) -> Dict[str, Any]:
    """把本地报告对象转成上传用的 JSON（不含截图二进制）。"""
    d = report.to_dict()
    return d
