# -*- coding: utf-8 -*-
"""探查「备战区」：点开它，把里面的角色列表 dump 出来。

背景（2026-09-20）：账号 15912614508 的 3 个角色不在区服列表里，
而在「选择服务器 → 已有角色 → 备战区」这个入口里面。

用法：python tools/probe_beizhan.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb import account as A                              # noqa: E402
from stzb.config import load                                # noqa: E402
from stzb.core import (DEFAULT_ADB, GAME_PKG, Device,       # noqa: E402
                       match_score, ocr_image, ocr_image_scaled)
from stzb.ui import Ui                                      # noqa: E402

SHOT = os.path.join(ROOT, "logs", "diag", "probe_beizhan")


def dump(items, title):
    print("=" * 76)
    print(title)
    for it in sorted(items, key=lambda x: (x.center[1], x.center[0])):
        print("   %-30s center=(%4d,%4d) box=%s"
              % (it.text[:28], it.center[0], it.center[1], it.box))
    print("   —— 共 %d 行" % len(items))


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

    # ---- 1. 回到游戏登录页（有「开始游戏 / 点击换区」那一屏）----
    ok, why = A.ensure_game_login_page(ui)
    print("回游戏登录页：%s（%s）" % ("成功" if ok else "失败", why))
    if not ok:
        return 1

    # ---- 2. 打开选择服务器 ----
    if not A._open_server_panel(ui):
        print("!! 打不开选择服务器面板")
        return 1
    items, _ = ui.ocr("bz_srv")
    dump(items, "① 选择服务器面板")

    # ---- 3. 找并点击「备战区」 ----
    hit = None
    for it in items:
        if match_score(it.text, "备战区") >= 0.62:
            hit = it
            break
    if hit is None:
        # 放大再找一次（面板底图是插画，小字常漏读）
        big = ocr_image_scaled(os.path.join(SHOT, os.path.basename(ui.shots[-1])),
                               factor=2)
        for it in big:
            if match_score(it.text, "备战区") >= 0.62:
                hit = it
                break
    if hit is None:
        print("!! 没找到「备战区」入口")
        A._press_confirm(ui)
        return 1
    print("\n→ 点「备战区」%s" % (hit.center,))
    ui.tap(*hit.center, delay=3.0)

    # ---- 4. dump 结果面板（多帧 + 放大）----
    items, path = ui.ocr("bz_after")
    dump(items, "② 点开备战区之后")
    big = ocr_image_scaled(path, factor=2)
    dump(big, "②b 同上，放大 2 倍（漏读兜底）")
    print("\n截图：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
