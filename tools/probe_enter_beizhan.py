# -*- coding: utf-8 -*-
"""按「确定」进入备战区，观察落地界面（找 3 个角色在哪）。

前提：当前「选择服务器」面板已打开、且「备战区」已被选中。
用法：python tools/probe_enter_beizhan.py
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

SHOT = os.path.join(ROOT, "logs", "diag", "probe_beizhan")


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

    os.makedirs(SHOT, exist_ok=True)
    ui = Ui(Device(adb, serial, pkg, shot_dir=SHOT), cfg,
            logger=print, dry_run=False)

    items, _ = ui.ocr("eb_before")
    on_panel = A._has(items, "选择服务器", "确定")
    print("当前在选择服务器面板：%s" % on_panel)
    if not on_panel:
        print("!! 面板不在了，请先手动/脚本把面板打开并选中备战区")
        return 1

    print("\n→ 按「确定」进入备战区 …")
    A._press_confirm(ui)
    time.sleep(3.0)

    # 确定只是把「备战区」选中，还要点「开始游戏」才真的进去
    from stzb.ui import BTN_START_GAME
    items, _ = ui.ocr("eb_login")
    if A._has(items, *A.KW_START_GAME):
        print("登录页已切到备战区 → 点「开始游戏」")
        ui.tap(*BTN_START_GAME, delay=0.0)

    # 之后可能：加载 → 进入游戏 → 或弹出角色选择。持续观察 2 分钟。
    for i in range(1, 25):
        time.sleep(5)
        items, path = ui.ocr("eb_%02d" % i)
        name = A.current_role(ui, items)
        role_hits = [x.text for x in items
                     if any(k in x.text for k in ("角色", "选择", "进入", "创建",
                                                  "备战", "赛季", "继续"))]
        print("[%2d] 行=%-3d 角色名=%r  有关键词=%s"
              % (i, len(items), name, role_hits[:6]))
        if i in (2, 6, 12, 24):
            print("     截图：%s" % path)
        if name:
            print("     ✓ 读到角色名 %r（截图 %s）" % (name, path))
            break
    print("\n最后一次截图：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
