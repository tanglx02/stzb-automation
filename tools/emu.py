# -*- coding: utf-8 -*-
"""模拟器开关小工具 —— 手动把 MuMu / 游戏退出或拉起来。

    python tools/emu.py status      # 看状态（模拟器 / ADB / 游戏）
    python tools/emu.py stop        # 关掉模拟器里的游戏，再关掉模拟器
    python tools/emu.py kill        # 连 MuMu 主程序一起彻底杀掉
    python tools/emu.py start       # 拉起模拟器 + 游戏
    python tools/emu.py restart     # 彻底关掉再冷启动（等于 stop/kill + start）
    python tools/emu.py game-stop   # 只退游戏，不动模拟器

不加参数默认 status。所有参数都能在 config.json 的 emulator / device 段里改。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                                    # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device             # noqa: E402
from stzb.emulator import MuMu                                  # noqa: E402


def _p(msg):
    print(msg, flush=True)


def build(cfg, logger=_p):
    e = cfg.get("emulator", {}) or {}
    adb = cfg.get("device.adb") or DEFAULT_ADB
    cands = cfg.get("device.serial_candidates")
    emu = MuMu(manager=e.get("manager"), vmindex=e.get("vmindex", 0),
               adb=adb, serial_candidates=cands, logger=logger,
               startup_timeout=e.get("startup_timeout", 300))
    dev = Device(adb=adb, serial_candidates=cands,
                 shot_dir=os.path.join(ROOT, "logs", "shots"))
    return emu, dev


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    cfg = load()
    pkg = cfg.get("device.package") or GAME_PKG
    emu, dev = build(cfg)

    if action == "status":
        _p(emu.status_line())
        _p("ADB 在线设备：%s" % (emu.adb_online() or "无"))
        if dev.connect(retries=1):
            _p("前台窗口：%s" % (dev.foreground() or "读不到"))
            _p("游戏在跑：%s" % dev.game_running(pkg))
        return 0

    if action == "stop":
        if dev.connect(retries=1):
            if dev.game_running(pkg):
                _p("· 先退出游戏 %s" % pkg)
                dev.force_stop(pkg)
            else:
                _p("· 游戏没在跑")
        emu.stop()
        return 0

    if action == "kill":
        if dev.connect(retries=1) and dev.game_running(pkg):
            _p("· 先退出游戏 %s" % pkg)
            dev.force_stop(pkg)
        emu.kill_all()
        return 0

    if action == "game-stop":
        if not dev.connect(retries=1):
            _p("!! ADB 连不上，模拟器可能没开")
            return 1
        dev.force_stop(pkg)
        _p("✓ 已退出游戏（模拟器保持运行）")
        return 0

    if action == "start":
        ok = emu.start(package=pkg, wait=True)
        _p("模拟器就绪：%s" % ok)
        return 0 if ok else 1

    if action == "restart":
        emu.stop()
        emu.kill_all()
        ok = emu.start(package=pkg, wait=True)
        _p("模拟器就绪：%s" % ok)
        return 0 if ok else 1

    _p("用法：python tools/emu.py [status|stop|kill|start|restart|game-stop]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
