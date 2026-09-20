# -*- coding: utf-8 -*-
"""把「选择服务器」面板的**所有页签组合**扫一遍，找出这个账号下的全部条目。

顶部页签：已有角色(235,249) / 经典服(503,248) / 青春服(783,249)
左侧子页签：最近登录(246,325) / 经典服角色(247,406)

用法：python tools/probe_srv_tabs.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb import account as A                              # noqa: E402
from stzb.config import load                                # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device         # noqa: E402
from stzb.ui import Ui                                      # noqa: E402

TOP_TABS = [("已有角色", (235, 249)), ("经典服", (503, 248)), ("青春服", (783, 249))]
SUB_TABS = [("最近登录", (246, 325)), ("经典服角色", (247, 406))]


def main() -> int:
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
        print("!! 连不上 ADB")
        return 1

    shot_dir = os.path.join(ROOT, "logs", "diag", "probe_srv")
    os.makedirs(shot_dir, exist_ok=True)
    ui = Ui(Device(adb, serial, pkg, shot_dir=shot_dir), cfg,
            logger=lambda m: None, dry_run=False)

    # 回到游戏登录页
    items, _ = ui.ocr("st_0")
    if not (A._has(items, *A.KW_START_GAME) or A._has(items, *A.KW_AREA_ENTRY)):
        if A.find_masked(items):
            print("在网易登录页 → 点登录回游戏登录页")
            hit = A.find_login_button(items)
            ui.tap(*(hit.center if hit else A.NN_LOGIN_BTN))
            time.sleep(3.0)
            items, _ = ui.ocr("st_1")

    if not A._open_server_panel(ui):
        print("!! 打不开选择服务器面板")
        return 1

    for tname, (tx, ty) in TOP_TABS:
        ui.tap(tx, ty, delay=1.2)
        for sname, (sx, sy) in SUB_TABS:
            ui.tap(sx, sy, delay=1.2)
            time.sleep(0.6)
            items, _ = ui.ocr("st_%s_%s" % (tname, sname))
            # 只打印「列表区」（中下部的条目），过滤掉页签/底部按钮
            entries = [it for it in items
                       if 320 < it.center[1] < 700 and 380 < it.center[0] < 950]
            print("=" * 76)
            print("## 顶部「%s」→ 子页签「%s」   列表区 %d 条" % (tname, sname, len(entries)))
            for it in sorted(entries, key=lambda x: x.center[1]):
                print("   %-30s center=%s" % (it.text[:28], it.center))
            leftovers = [it.text for it in items
                         if not (320 < it.center[1] < 700 and 380 < it.center[0] < 950)]
            print("   —— 其余 %d 行：%s" % (len(leftovers), " | ".join(leftovers)[:240]))

    A._press_confirm(ui)
    time.sleep(1.0)
    print("\n已按「确定」退出面板。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
