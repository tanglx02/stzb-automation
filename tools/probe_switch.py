# -*- coding: utf-8 -*-
"""一次性侦察：找出「切换账号 / 切换角色 / 换区」的真实入口与界面结构。

**必须一次性跑完**：本机沙箱里模拟器挂在发起命令的进程树上，命令一结束整棵树被回收，
所以「启动 → 探测 → 打印」必须在同一个进程里完成。

用法：
    python tools/probe_switch.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                                    # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device             # noqa: E402
from stzb.emulator import MuMu                                  # noqa: E402
from stzb.ui import (AD_CLOSE, BTN_START_GAME, DOWNLOAD_CLOSE,  # noqa: E402
                     Ui)

SHOT_DIR = os.path.join(ROOT, "logs", "shots")
OUT = os.path.join(ROOT, "recon", "switch_probe.json")

FOCUS = ("账号", "帐号", "角色", "换区", "切换", "服务器", "区服", "登录",
         "退出", "用户", "设置", "网易", "大区", "推荐", "最近")


def dump(items, title: str, indent: str = "  "):
    print("=" * 74)
    print(title)
    print("=" * 74)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        flag = "  <<<" if any(f in it.text for f in FOCUS) else ""
        print("%s%-38s c=(%4d,%4d) box=(%4d,%4d)-(%4d,%4d)%s"
              % (indent, it.text[:36], cx, cy, x1, y1, x2, y2, flag))
    print("%s—— %d 行" % (indent, len(items)))
    print()


def back(dev, adb: str, n: int = 1):
    for _ in range(n):
        try:
            subprocess.run([adb, "-s", dev.serial, "shell", "input", "keyevent", "4"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(1.2)


def probe(ui, dev, adb, name: str, x: int, y: int, settle: float = 3.5,
          records: list = None):
    """点一个坐标，看弹出什么，然后退回来。"""
    print()
    print("▶ 探测[%s]：点击 (%d,%d)" % (name, x, y))
    ui.tap(x, y)
    time.sleep(settle)
    items, path = ui.ocr("sw_%s" % name)
    if not items:
        # 读不出就放大整图再读（OCR 最小尺寸/淡色字的兜底）
        from stzb.core import ocr_image_scaled
        items = ocr_image_scaled(path, 2, tmp_path=path + ".x2.png")
        print("  （帧内 0 行，放大 2 倍后读到 %d 行）" % len(items))
    dump(items, "点击「%s」之后（截图 %s）" % (name, path))
    if records is not None:
        records.append({"name": name, "point": [x, y], "shot": path,
                        "lines": [{"t": it.text, "c": list(it.center),
                                   "box": list(it.box)} for it in items]})

    # 退出来：先按返回键，再找 ✕
    for attempt in range(3):
        back(dev, adb)
        cur, _ = ui.ocr("sw_%s_back%d" % (name, attempt))
        if ui.has(cur, "开始游戏") and not ui.is_home(cur):
            print("  · 已回到登录页")
            return items, True
        if ui.is_home(cur):
            print("  · 已回到主城")
            return items, True
    print("  ! 退不回去，记录当前状态")
    return items, False


def main() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG
    log = print
    records: list = []

    emu = MuMu(manager=cfg.get("emulator.manager"),
               vmindex=cfg.get("emulator.vmindex", 0), adb=adb,
               serial_candidates=cfg.get("device.serial_candidates"), logger=log)
    if emu.is_running():
        print("· 模拟器在跑，等就绪")
        emu.wait_ready(timeout=300)
    else:
        print("· 启动模拟器 + 游戏…")
        if not emu.start(package=pkg, wait=True):
            print("!! 启动失败")
            return 1

    dev = Device(adb=adb, serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
    if not dev.connect(retries=8, wait=2.0):
        print("!! ADB 连不上")
        return 1
    print("· ADB=%s" % dev.serial)

    if not dev.game_running(pkg):
        dev.launch(pkg)
        for i in range(60):
            time.sleep(2)
            if dev.game_running(pkg):
                print("· 游戏起来（%d 秒）" % ((i + 1) * 2))
                break

    ui = Ui(dev, cfg, logger=log, dry_run=False)

    # ---------------------------------------------------------- 1. 走到登录页（停在登录页）
    print()
    print("### 阶段 1：走到登录页（不点开始游戏）")
    at_login = False
    blank = 0
    for rnd in range(1, 61):
        items, path = ui.ocr("sw_boot_%02d" % rnd)
        if ui.has(items, "开始游戏") and not ui.is_home(items):
            dump(items, "登录页（第 %d 帧）" % rnd)
            records.append({"name": "login_page", "shot": path,
                            "lines": [{"t": it.text, "c": list(it.center),
                                       "box": list(it.box)} for it in items]})
            at_login = True
            break
        if len(items) <= 2:
            blank += 1
            if blank >= 3:
                blank = 0
                print("· 疑似封面页 → 点中央")
                ui.tap(960, 540)
                time.sleep(5)
                continue
        else:
            blank = 0
        time.sleep(2.5)
    if not at_login:
        print("!! 没能停在登录页")
        return 1

    # ---------------------------------------------------------- 2. 探登录页上的入口
    print()
    print("### 阶段 2：探登录页各入口")
    # 2.1 「点击换区」
    hit = ui.hit(items, "点击换区", "换区")
    if hit:
        bx, by = ui._kw_point(hit, ("点击换区", "换区"))
        probe(ui, dev, adb, "换区", bx, by, records=records)
    else:
        print("· 没读到「点击换区」，用区域中心兜底 (1100,759)")
        probe(ui, dev, adb, "换区_fb", 1100, 759, records=records)

    # 2.2 左侧竖排图标（网易账号相关入口）
    for i, (x, y) in enumerate([(60, 62), (60, 128), (60, 194),
                                (60, 261), (60, 327)], 1):
        probe(ui, dev, adb, "left%d" % i, x, y, settle=3.0, records=records)

    # 2.3 右上角图标
    for i, (x, y) in enumerate([(1906, 79), (1906, 132), (1906, 191)], 1):
        probe(ui, dev, adb, "right%d" % i, x, y, settle=3.0, records=records)

    # ---------------------------------------------------------- 3. 进主城
    print()
    print("### 阶段 3：点开始游戏进主城")
    cur, _ = ui.ocr("sw_prehome")
    if not ui.is_home(cur):
        hit = ui.hit(cur, "开始游戏")
        ui.tap(*(hit.center if hit else BTN_START_GAME))
        for rnd in range(1, 41):
            time.sleep(3)
            cur, path = ui.ocr("sw_home_%02d" % rnd)
            if ui.is_home(cur):
                dump(cur, "主城（第 %d 帧）" % rnd)
                records.append({"name": "home", "shot": path,
                                "lines": [{"t": it.text, "c": list(it.center),
                                           "box": list(it.box)} for it in cur]})
                break
        else:
            print("!! 没进主城")
            return 1
    else:
        cur, path = ui.ocr("sw_home_now")
        dump(cur, "本来就在主城")

    # ---------------------------------------------------------- 4. 探主城上的账号/角色入口
    print()
    print("### 阶段 4：探主城左上角（角色名 / 头像）")
    for name, (x, y) in [("主城头像", (32, 36)), ("主城角色名", (376, 20))]:
        probe(ui, dev, adb, name, x, y, settle=4.0, records=records)

    # ---------------------------------------------------------- 保存
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print()
    print("侦察结果已存：%s" % OUT)
    print("共记录 %d 个界面" % len(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())