# -*- coding: utf-8 -*-
"""侦察：游戏里的「账号 / 角色」相关入口在哪。

只「看」不点（只点关闭/返回这类无害按钮），把当前界面 OCR 全打出来，
帮助确定「切换账号 / 切换角色」的界面结构。

用法：
    python tools/probe_account.py            # 打印当前界面全部文字
    python tools/probe_account.py --shot xx  # 额外存一张图
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                    # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device, ocr_image, read_png  # noqa: E402
from stzb.ui import Ui                          # noqa: E402

SHOT_DIR = os.path.join(ROOT, "logs", "shots")


def dump(items, title: str):
    print("=" * 68)
    print(title)
    print("=" * 68)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        print("  %-34s box=(%4d,%4d)-(%4d,%4d) center=(%4d,%4d)"
              % (it.text[:32], x1, y1, x2, y2, cx, cy))
    print("  共 %d 行" % len(items))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default="", help="额外保存一张截图，前缀")
    ap.add_argument("--no-boot", action="store_true", help="不尝试回主城")
    args = ap.parse_args()

    os.makedirs(SHOT_DIR, exist_ok=True)
    cfg = load()
    dev = Device(adb=cfg.get("device.adb") or DEFAULT_ADB,
                 serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
    if not dev.connect(retries=6, wait=2.0):
        print("!! ADB 连不上")
        return 1
    print("· ADB=%s" % dev.serial)

    pkg = cfg.get("device.package") or GAME_PKG
    print("· 游戏在跑：%s" % dev.game_running(pkg))
    print("· 前台窗口：%s" % (dev.foreground() or "读不到"))

    ui = Ui(dev, cfg, logger=print, dry_run=True)   # dry_run：绝不真点

    items, path = ui.ocr("probe_cur")
    dump(items, "当前界面")
    print("· 截图：%s" % path)

    if not args.no_boot:
        print()
        print("· 看看 is_home / is_neizheng 怎么判的：")
        print("    is_home      = %s" % ui.is_home(items))
        print("    is_neizheng  = %s" % ui.is_neizheng(items))

    if args.shot:
        p = dev.shot_path("%s" % args.shot)
        dev.screenshot(p)
        print("· 另存：%s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())