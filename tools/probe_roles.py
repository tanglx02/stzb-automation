# -*- coding: utf-8 -*-
"""探查：把「选择服务器」面板里的角色全 dump 出来（含每个页签）。

用途有两个：
  1. 摸清这个面板的真实文字布局，据此写角色识别；
  2. 游戏改版后重新采一遍。

安全约束（沿用 stzb/account.py）：
  · 绝不点「其他账号登录 / 提交并退出 / 退出游戏」；
  · 全程只用界面按钮，不按 Android 返回键；
  · 结束时按「确定」退出面板，不改变当前选中的角色。

用法：
    python tools/probe_roles.py              # 登录页 → 选择服务器 → 逐页签 dump
    python tools/probe_roles.py --no-uc      # 已经在登录页了，跳过用户中心那一步
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb import account as A                              # noqa: E402
from stzb.config import load                                # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device         # noqa: E402
from stzb.ui import Ui                                      # noqa: E402

TABS = ("已有角色", "最近登录", "经典服", "青春服")


def dump(items, title: str):
    print("=" * 74)
    print(title)
    print("=" * 74)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        print("  %-34s center=(%4d,%4d)  box=(%4d,%4d)-(%4d,%4d)"
              % (it.text[:32], cx, cy, x1, y1, x2, y2))
    print("  —— 共 %d 行" % len(items))
    print()
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-uc", action="store_true", help="已经在登录页，跳过用户中心")
    args = ap.parse_args()

    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG
    serial = None
    for s in (cfg.get("device.serial_candidates") or []):
        d = Device(adb, s, pkg)
        try:
            if d.is_online():
                serial = s
                break
        except Exception:
            continue
    if serial is None:
        print("!! 连不上任何 ADB 设备")
        return 1
    print("ADB 设备：%s" % serial)

    # 截图单独放一个子目录：不混进 logs/shots（那边会被定期清理），
    # 也不占用系统临时目录（排查时要能顺着找回来）。
    shot_dir = os.path.join(ROOT, "logs", "diag", "probe_roles")
    os.makedirs(shot_dir, exist_ok=True)
    ui = Ui(Device(adb, serial, pkg, shot_dir=shot_dir), cfg,
            logger=print, dry_run=False)

    # ---- 1. 回到游戏登录页 ----
    if not args.no_uc:
        items, _ = ui.ocr("pr_home")
        dump(items, "① 当前画面（进游戏状态）")
        print("当前脱敏账号：%s" % (A.find_masked(items) or "读不到"))
        print("正在打开 用户中心 → 切换账号 …")
        if not A._open_user_center(ui):
            print("!! 打不开用户中心")
            return 1
        if not A._tap_switch_account(ui):
            print("!! 没进到网易登录页")
            return 1

    items, _ = ui.ocr("pr_login")
    dump(items, "② 网易登录页")
    print("脱敏账号 = %s" % (A.find_masked(items) or "读不到"))
    # 「常用」列表里都有谁（只打印，不乱点）
    masks = sorted({m for it in items for m in [A.mask_of(it.text)] if m})
    print("「常用」列表里读到的脱敏账号：%s" % (masks or "（没读到）"))
    print()

    # ---- 1.5 ★ 网易登录页 → 游戏自己的登录页 ----
    # 「点击换区」不在网易统一登录页上，它在**率土之滨自己的登录页**上。
    # 两页之间差一步「登录」：点完才会回到游戏登录页（这里有坑，实测踩过）。
    if not (A._has(items, *A.KW_START_GAME) or A._has(items, *A.KW_AREA_ENTRY)):
        print("当前在网易统一登录页 → 点「登录」回到游戏登录页 …")
        hit = A.find_login_button(items)
        if hit is not None:
            ui.tap(*hit.center)
        else:
            ui.tap(*A.NN_LOGIN_BTN)
        time.sleep(3.0)
        items, _ = ui.ocr("pr_game_login")
        dump(items, "②b 游戏自己的登录页（应有「开始游戏」「点击换区」）")
        if not (A._has(items, *A.KW_START_GAME) or A._has(items, *A.KW_AREA_ENTRY)):
            print("!! 仍然看不到「开始游戏」或「点击换区」，无法继续")
            return 1

    # ---- 2. 打开「选择服务器」面板 ----
    if not A._open_server_panel(ui):
        print("!! 打不开「选择服务器」面板")
        return 1
    items, _ = ui.ocr("pr_srv_open")
    dump(items, "③ 「选择服务器」面板（默认页签）")

    # ---- 3. 逐页签 dump ----
    for t in TABS:
        A._pick_tab(ui, t)
        time.sleep(0.6)
        items, _ = ui.ocr("pr_tab_%s" % t)
        dump(items, "④ 页签「%s」" % t)

    # ---- 4. 退出面板（按确定，不改动选中项）----
    A._press_confirm(ui)
    time.sleep(1.0)
    items, _ = ui.ocr("pr_after")
    dump(items, "⑤ 按「确定」之后（应回到登录页）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
