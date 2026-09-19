# -*- coding: utf-8 -*-
"""一次性侦察：冷启动 → 登录页 → 主城，把沿途每个界面的 OCR 全打出来。

**为什么要「一次性」**：本机沙箱里，模拟器是由发起命令的进程树拉起来的，
一旦该命令结束，整棵进程树（含 MuMu 虚拟机）会被回收 ——
表现为「刚报就绪，下一个命令再查 is_process_started=False」。
所以侦察必须在**同一次进程运行内**完成：启动 → 等待 → 截图 → OCR → 打印 → 顺便探一下入口。

用法：
    python tools/probe_login.py            # 走到主城并打印全部文字
    python tools/probe_login.py --tour     # 额外探一下「设置/账号/角色」入口
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                                       # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device                # noqa: E402
from stzb.emulator import MuMu                                     # noqa: E402
from stzb.ui import (AD_CLOSE, BTN_START_GAME, DOWNLOAD_CLOSE,     # noqa: E402
                     SUB_CLOSE, Ui)

SHOT_DIR = os.path.join(ROOT, "logs", "shots")
# 登录页 / 主城上可能出现的「账号、角色」相关字样（用来发现入口）
ACCT_KW = ("设置", "账号", "帐号", "角色", "用户中心", "切换", "系统")


def dump(items, title: str, highlight=()):
    print("=" * 72)
    print(title)
    print("=" * 72)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        flag = ""
        for h in highlight:
            if h in it.text:
                flag = "   <<<< 关注"
                break
        print("  %-36s center=(%4d,%4d)  box=(%4d,%4d)-(%4d,%4d)%s"
              % (it.text[:34], cx, cy, x1, y1, x2, y2, flag))
    print("  —— 共 %d 行" % len(items))
    print()


def press_back(dev, adb: str) -> None:
    """按 Android 返回键（比找 ✕ 更通用）。"""
    try:
        subprocess.run([adb, "-s", dev.serial, "shell", "input", "keyevent", "4"],
                       capture_output=True, timeout=15)
    except Exception as e:
        print("  （返回键失败：%r）" % (e,))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tour", action="store_true", help="顺便探一下账号/角色入口")
    ap.add_argument("--no-start", action="store_true", help="不启动模拟器（假定已在跑）")
    args = ap.parse_args()

    os.makedirs(SHOT_DIR, exist_ok=True)
    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG
    log = print

    # ---------------------------------------------------------- 1. 模拟器
    emu = MuMu(manager=cfg.get("emulator.manager"),
               vmindex=cfg.get("emulator.vmindex", 0), adb=adb,
               serial_candidates=cfg.get("device.serial_candidates"),
               logger=log)
    if not args.no_start:
        if emu.is_running():
            print("· 模拟器已在运行，等它就绪")
            if not emu.wait_ready(timeout=300):
                print("!! 等不到就绪")
                return 1
        else:
            print("· 启动模拟器 + 游戏…")
            if not emu.start(package=pkg, wait=True):
                print("!! 模拟器启动失败")
                return 1

    # ---------------------------------------------------------- 2. ADB
    dev = Device(adb=adb, serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
    if not dev.connect(retries=8, wait=2.0):
        print("!! ADB 连不上")
        return 1
    print("· ADB=%s  前台=%s" % (dev.serial, dev.foreground() or "读不到"))

    # ---------------------------------------------------------- 3. 游戏
    if not dev.game_running(pkg):
        print("· 游戏没在跑，拉起 %s" % pkg)
        dev.launch(pkg)
        for i in range(60):
            time.sleep(2)
            if dev.game_running(pkg):
                print("· 游戏进程起来了（%d 秒）" % ((i + 1) * 2))
                break
        else:
            print("!! 游戏进程起不来")
            return 1
    else:
        print("· 游戏本来就在跑")

    ui = Ui(dev, cfg, logger=log, dry_run=False)

    # ---------------------------------------------------------- 4. 走到主城
    clicked_login = 0
    home_items = None
    blank_streak = 0          # 连续「OCR 读不出东西」的帧数
    for rnd in range(1, 61):
        items, path = ui.ocr("probe_%02d" % rnd)
        is_login = ui.has(items, "开始游戏") and not ui.is_home(items)
        is_home = ui.is_home(items)

        if rnd <= 8 or is_login or is_home or len(items) <= 2:
            title = "第 %d 帧（%s）" % (rnd, "主城" if is_home else
                                       ("登录页" if is_login else "过渡中"))
            dump(items, title, highlight=ACCT_KW)

        if is_home:
            home_items = items
            print("✓ 已进入主城（第 %d 帧，截图 %s）" % (rnd, path))
            break

        # ★ 「点击以开始游戏」封面页：整屏是一张淡金色的山水插画，
        #   那行字 OCR 完全读不出来（实测 0 行）。它没有别的出路 —— 点屏幕中央即可。
        if len(items) <= 2:
            blank_streak += 1
            if blank_streak >= 3:
                blank_streak = 0
                print("· 连续多帧读不出文字（疑似「点击以开始游戏」封面页）"
                      "→ 点屏幕中央 (960,540)")
                ui.tap(960, 540)
                time.sleep(6)
                continue
        else:
            blank_streak = 0

        if is_login:
            clicked_login += 1
            if clicked_login <= 2:
                print("· 登录页 → 点「开始游戏」（第 %d 次）" % clicked_login)
            ui.tap(*BTN_START_GAME)
            time.sleep(12)
            continue

        # 资源下载 / 广告弹窗
        if ui.has(items, "资源下载") and ui.has(items, "总包体", "功能说明"):
            print("· 资源下载弹窗 → 点右上 ✕")
            ui.tap(*DOWNLOAD_CLOSE)
            time.sleep(2)
            continue
        if ui.has(items, "来网易游戏中心"):
            print("· 广告弹窗 → 点右上 ✕")
            ui.tap(*AD_CLOSE)
            time.sleep(2)
            continue
        # 关掉居中的信息弹窗
        for kw in ("确定", "确认", "知道了"):
            hit = ui.hit(items, kw)
            if hit is None:
                continue
            x, y = hit.center
            if 450 <= x <= 1450 and 380 <= y <= 990:
                print("· 信息弹窗 → 点「%s」" % hit.text)
                ui.tap(x, y)
                time.sleep(2)
                break
        else:
            time.sleep(3)
    else:
        print("!! 60 帧还没进主城")
        return 1

    # ---------------------------------------------------------- 5. 探账号/角色入口
    if args.tour and home_items is not None:
        print()
        print("#" * 72)
        print("开始探「账号 / 角色」入口")
        print("#" * 72)
        for kw in ACCT_KW:
            hit = ui.hit(home_items, kw)
            if hit is None:
                continue
            print()
            print("· 主城上发现「%s」@%s → 点它看会开什么" % (hit.text, hit.center))
            ui.tap(*hit.center)
            time.sleep(4)
            sub, spath = ui.ocr("tour_%s" % kw)
            dump(sub, "点「%s」之后（截图 %s）" % (hit.text, spath), highlight=ACCT_KW)
            press_back(dev, adb)
            time.sleep(3)
            back_items, _ = ui.ocr("tour_back_%s" % kw)
            if ui.is_home(back_items):
                print("  · 已回到主城，继续找下一个入口")
            else:
                print("  · 没回到主城，再按一次返回")
                press_back(dev, adb)
                time.sleep(3)

    print()
    print("侦察结束。截图都在 %s（前缀 probe_ / tour_）" % SHOT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())