# -*- coding: utf-8 -*-
"""侦察 4：把「切换账号」和「选择服务器（切角色）」这两条路走到底。

已确认的入口（实测坐标 1920x1080）：
  · 登录页左侧第 1 个图标 (60,62) → 打开「用户中心」
  · 用户中心里「切换账号」@(1566,288)（红色边框按钮，右上角）
  · 用户中心 ✕ @(1671,156)  ← 注意不是 1760，实测偏了 90px
  · 登录页「点击换区」@(1100,759) → 打开「选择服务器」（已有角色/经典服/青春服）

用法：python tools/probe_switch4.py
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                                        # noqa: E402
from stzb.core import (DEFAULT_ADB, GAME_PKG, Device,               # noqa: E402
                       ocr_image_scaled)
from stzb.emulator import MuMu                                      # noqa: E402
from stzb.ui import Ui                                              # noqa: E402

SHOTS = os.path.join(ROOT, "logs", "shots")
OUT = os.path.join(ROOT, "recon", "switch4.json")
REC: list = []

P_UC_ICON = (60, 62)          # 登录页左侧第 1 图标 → 用户中心
P_UC_SWITCH = (1566, 288)     # 用户中心「切换账号」
P_UC_CLOSE = (1671, 156)      # 用户中心 ✕
P_UC_ACCT_MGR = (780, 594)    # 用户中心「账号管理」
P_UC_LOGIN_MGR = (1140, 810)  # 用户中心「登录管理」
P_SWITCH_AREA = (1100, 759)   # 登录页「点击换区」
P_SRV_TAB_EXIST = (235, 249)
P_SRV_SUB_CLASSIC = (247, 406)
P_SRV_ROLE1 = (651, 416)
P_SRV_ROLE2 = (665, 539)
P_SRV_OK = (960, 895)
P_SURVEY_CONT = (427, 837)
P_MID = (960, 540)


def read(ui, tag, fallback=True):
    items, path = ui.ocr(tag)
    if not items and fallback:
        items = ocr_image_scaled(path, 2, tmp_path=path + ".x2.png")
    return items, path


def dump(items, title):
    print("─" * 74)
    print(title)
    for it in items:
        x1, y1, x2, y2 = it.box
        print("   %-42s c=(%4d,%4d) box=(%4d,%4d)-(%4d,%4d)"
              % (it.text[:40], it.center[0], it.center[1], x1, y1, x2, y2))
    print("   —— %d 行" % len(items))
    print()


def keep(name, items, path="", point=None):
    REC.append({"name": name, "point": list(point) if point else None, "shot": path,
                "lines": [{"t": i.text, "c": list(i.center), "box": list(i.box)}
                          for i in items]})


def at_login(ui, items):
    return ui.has(items, "开始游戏") and not ui.is_home(items)


def step(ui, label, xy, settle=3.5, tag=None):
    """点一下 + 读取 + 打印 + 记录。"""
    tag = tag or label
    print("▶ %s：点 %s" % (label, xy))
    ui.tap(*xy)
    time.sleep(settle)
    items, path = read(ui, "ps4_%s" % tag)
    dump(items, "%s 之后（%s）" % (label, path))
    keep(label, items, path, xy)
    return items, path


def close_panel(ui, rounds=4, tag="cls"):
    """用 ✕ / 继续游戏 收回登录页。**绝不按返回键**。"""
    items = None
    for r in range(rounds):
        items, _ = read(ui, "ps4_%s%d" % (tag, r))
        if at_login(ui, items) or ui.is_home(items):
            return items
        if ui.has(items, "中途退出"):
            h = ui.hit(items, "继续游戏")
            print("   · 退出问卷 → 点「继续游戏」")
            ui.tap(*(h.center if h else P_SURVEY_CONT))
        else:
            print("   · 收回：点 ✕ @%s" % (P_UC_CLOSE,))
            ui.tap(*P_UC_CLOSE)
        time.sleep(2.6)
    return items


def boot_to_login(ui):
    items, path = read(ui, "ps4_boot_0")
    for rnd in range(1, 61):
        if at_login(ui, items) or ui.is_home(items):
            print("· 第 %d 轮到位（is_home=%s）" % (rnd, ui.is_home(items)))
            return items
        if not items:
            print("  · 封面页 → 点中央")
            ui.tap(*P_MID)
            time.sleep(5)
        else:
            time.sleep(2.5)
        items, path = read(ui, "ps4_boot_%d" % rnd)
    return items


def main():
    os.makedirs(SHOTS, exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG

    emu = MuMu(manager=cfg.get("emulator.manager"),
               vmindex=cfg.get("emulator.vmindex", 0), adb=adb,
               serial_candidates=cfg.get("device.serial_candidates"), logger=print)
    if emu.is_running():
        emu.wait_ready(timeout=300)
    elif not emu.start(package=pkg, wait=True):
        print("!! 启动失败")
        return 1

    dev = Device(adb=adb, serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOTS)
    if not dev.connect(retries=8, wait=2.0):
        print("!! ADB 连不上")
        return 1
    print("· ADB=%s" % dev.serial)
    if not dev.game_running(pkg):
        dev.launch(pkg)
        for _ in range(60):
            time.sleep(2)
            if dev.game_running(pkg):
                break

    ui = Ui(dev, cfg, logger=print, dry_run=False)
    items = boot_to_login(ui)
    dump(items, "起点界面")
    keep("start", items)
    if ui.is_home(items):
        print("!! 起始就在主城，本脚本期望登录页。请先手动退出到登录页再跑。")
        return 1
    if not at_login(ui, items):
        print("!! 不在登录页，中止")
        return 1

    # ================================================== A. 切换账号
    print()
    print("=" * 74)
    print("A. 切换账号流程")
    print("=" * 74)
    uc, _ = step(ui, "打开用户中心", P_UC_ICON, 3.5, tag="uc")
    if not ui.has(uc, "切换账号"):
        print("!! 用户中心里没看到「切换账号」，中止 A")
    else:
        sw, sp = step(ui, "点切换账号", P_UC_SWITCH, 4.0, tag="sw")
        # 这一步之后可能出现：账号列表 / 网易登录页 / 确认弹窗
        for r in range(3):
            if at_login(ui, sw) or not sw:
                break
            print("   · 继续观察第 %d 次" % (r + 1))
            sw, sp = read(ui, "ps4_sw_more%d" % r)
            dump(sw, "切换账号后-续（%s）" % sp)
            keep("switch_more%d" % r, sw, sp)
            time.sleep(2.5)
        # 尝试收回
        print("   · 尝试收回（切换账号流程可能已经登出到网易登录页）")
        sw2 = close_panel(ui, rounds=3, tag="sw_cls")
        dump(sw2, "收回后（%）".replace("%", "") or "收回后")
        keep("switch_after_close", sw2)
        items = sw2

    # ================================================== B. 选择服务器（切角色）
    print()
    print("=" * 74)
    print("B. 选择服务器（切角色）流程")
    print("=" * 74)
    if not at_login(ui, items):
        items = close_panel(ui, rounds=3, tag="pre_b")
        if not at_login(ui, items):
            print("   ! 不在登录页，先想办法回登录页")
            items, _ = read(ui, "ps4_pre_b2")
    srv, spath = step(ui, "点点击换区", P_SWITCH_AREA, 4.0, tag="srv")
    if ui.has(srv, "选择服务器") or ui.has(srv, "已有角色"):
        print("   ✓ 确认是「选择服务器」面板")
        # B1: 点「已有角色」页签
        it, p = step(ui, "点已有角色页签", P_SRV_TAB_EXIST, 2.5, tag="srv_tab")
        # B2: 点「经典服角色」子页签
        it, p = step(ui, "点经典服角色子页签", P_SRV_SUB_CLASSIC, 2.5, tag="srv_sub")
        # B3: 选第一个角色条目
        it, p = step(ui, "点第一个角色条目", P_SRV_ROLE1, 2.5, tag="srv_r1")
        # B4: 点确定
        it, p = step(ui, "点确定", P_SRV_OK, 6.0, tag="srv_ok")
        print("   点确定之后 is_home=%s at_login=%s" % (ui.is_home(it), at_login(ui, it)))
    else:
        print("   ! 没打开「选择服务器」，当前是别的东西")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(REC, f, ensure_ascii=False, indent=2)
    print()
    print("✓ 完成，%d 条记录 → %s" % (len(REC), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())