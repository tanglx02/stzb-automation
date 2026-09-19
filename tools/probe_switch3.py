# -*- coding: utf-8 -*-
"""侦察 3（终版）：把「账号 / 角色切换」的所有入口与界面一次摸清。

上一版的教训（都写进注释，别再踩）：
  1. **必须一次性跑完** —— 沙箱会回收模拟器进程树，跨命令调用留不住模拟器。
  2. **判断界面一律用 Ui.has/MK 别名+模糊匹配**，不能用朴素子串。
     OCR 把「开始游戏」读成「廾始游戏」，子串匹配 → 登录页空转 60 轮什么都没探到。
  3. **登录页绝不能按 Android 返回键**（keyevent 4）——会弹出
     「请问，导致中途退出的原因是？」问卷，下面就有「提交并退出」。
     问卷一出现会盖住整屏，后面所有探测都看到同一画面。
     退出面板只用面板自己的 ✕ / 确定 / 继续游戏。

用法：python tools/probe_switch3.py
输出：recon/switch3.json + logs/shots/ps3_*.png
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

SHOT_DIR = os.path.join(ROOT, "logs", "shots")
OUT = os.path.join(ROOT, "recon", "switch3.json")

FOCUS = ("账号", "帐号", "角色", "换区", "切换", "服务器", "区服", "登录", "退出",
         "用户", "设置", "网易", "大区", "确定", "取消", "继续", "注销", "重新")

# 实测坐标（1920x1080）
P_SERVER_PANEL_TABS = {"已有角色": (235, 249), "经典服": (503, 248), "青春服": (783, 249)}
P_SERVER_SUB = {"最近登录": (246, 325), "经典服角色": (247, 406)}
P_SERVER_ROLE1 = (651, 416)
P_SERVER_ROLE2 = (665, 539)
P_SERVER_OK = (960, 895)
P_SURVEY_CONTINUE = (427, 837)      # 「继续游戏」在左！右侧才是「提交并退出」
P_LOGIN_SWITCH_AREA = (1100, 759)   # 「点击换区」区域
P_HOME_AVATAR = (32, 36)            # 主城左上角头像

RECORDS: list = []


def read(ui: Ui, tag: str, scale_fallback: bool = True):
    items, path = ui.ocr(tag)
    if not items and scale_fallback:
        items = ocr_image_scaled(path, 2, tmp_path=path + ".x2.png")
        if items:
            print("    （帧内 0 行 → 放大 2 倍读到 %d 行）" % len(items))
    return items, path


def dump(items, title: str):
    print("─" * 74)
    print(title)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        flag = "   <<<" if any(f in it.text for f in FOCUS) else ""
        print("   %-40s c=(%4d,%4d) box=(%4d,%4d)-(%4d,%4d)%s"
              % (it.text[:38], cx, cy, x1, y1, x2, y2, flag))
    print("   —— %d 行" % len(items))
    print()


def keep(name: str, items, path: str = "", point=None):
    RECORDS.append({"name": name, "point": list(point) if point else None,
                    "shot": path,
                    "lines": [{"t": i.text, "c": list(i.center),
                               "box": list(i.box)} for i in items]})


def on_login(ui: Ui, items) -> bool:
    return ui.has(items, "开始游戏") and not ui.is_home(items)


def survey_shown(ui: Ui, items) -> bool:
    return ui.has(items, "中途退出")


def dismiss(ui: Ui, items, tag: str, rounds: int = 5):
    """把临时面板收掉，回到登录页。只用 ✕/确定/继续游戏，绝不按返回键。"""
    for r in range(rounds):
        if on_login(ui, items):
            return items
        if survey_shown(ui, items):
            h = ui.hit(items, "继续游戏")
            print("    · 退出问卷 → 点「继续游戏」")
            ui.tap(*(h.center if h else P_SURVEY_CONTINUE))
        else:
            h = ui.hit(items, "确定")
            if h is not None and 700 <= h.center[0] <= 1250:
                print("    · 面板 → 点「确定」@%s" % (h.center,))
                ui.tap(*h.center)
            else:
                print("    · 面板 → 点右上 ✕ @%s" % (SRV_CLOSE,))
                ui.tap(*SRV_CLOSE)
        time.sleep(2.6)
        items, _ = read(ui, "%s_dis%d" % (tag, r))
    return items


SRV_CLOSE = (1760, 156)


def main() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG

    # ------------------------------------------------------------ 起环境
    emu = MuMu(manager=cfg.get("emulator.manager"),
               vmindex=cfg.get("emulator.vmindex", 0), adb=adb,
               serial_candidates=cfg.get("device.serial_candidates"), logger=print)
    if emu.is_running():
        emu.wait_ready(timeout=300)
    elif not emu.start(package=pkg, wait=True):
        print("!! 模拟器启动失败")
        return 1

    dev = Device(adb=adb, serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
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

    # ------------------------------------------------------------ 1. 到登录页
    print()
    print("### 1. 走到登录页")
    items, path = read(ui, "ps3_boot_0")
    for rnd in range(1, 81):
        if on_login(ui, items):
            print("· 第 %d 轮到达登录页（截图 %s）" % (rnd, path))
            break
        if ui.is_home(items):
            print("· 第 %d 轮已在主城（截图 %s）→ 先不进去，回头探" % (rnd, path))
            break
        if not items:
            print("  · 第 %d 轮读不到文字 → 疑似「点击以开始游戏」封面页，点中央" % rnd)
            ui.tap(960, 540)
            time.sleep(5)
        else:
            time.sleep(2.5)
        items, path = read(ui, "ps3_boot_%d" % rnd)
    dump(items, "当前界面（登录页 or 主城）")
    keep("entry", items, path)

    # ------------------------------------------------------------ 2. 登录页各图标
    if on_login(ui, items):
        print()
        print("### 2. 登录页两侧图标（逐一点，点完收回来）")
        cands = [("L1", 60, 62), ("L2", 60, 128), ("L3", 60, 194),
                 ("L4", 60, 261), ("L5", 60, 327),
                 ("R1", 1906, 79), ("R2", 1906, 132), ("R3", 1906, 191)]
        for name, x, y in cands:
            print()
            print("▶ [%s] 点 (%d,%d)" % (name, x, y))
            ui.tap(x, y)
            time.sleep(3.0)
            it2, p2 = read(ui, "ps3_%s" % name)
            if not it2:
                print("   · 无反应（读不到任何文字）→ 纯装饰或无功能")
                continue
            dump(it2, "点 [%s] 之后（%s）" % (name, p2))
            keep(name, it2, p2, (x, y))
            items = dismiss(ui, it2, "ps3_%s" % name)
            if not on_login(ui, items):
                print("   ! 没能回到登录页，重新走一遍")
                items, _ = read(ui, "ps3_%s_re" % name)

    # ------------------------------------------------------------ 3. 选择服务器面板
    print()
    print("### 3. 「选择服务器」面板完整结构")
    items, _ = read(ui, "ps3_pre_srv")
    hit = ui.hit(items, "点击换区", "换区")
    xy = ui._kw_point(hit, ("换区",)) if hit else P_LOGIN_SWITCH_AREA
    print("▶ 点「点击换区」@%s" % (xy,))
    ui.tap(*xy)
    time.sleep(4.0)
    srv, spath = read(ui, "ps3_srv")
    dump(srv, "选择服务器（%s）" % spath)
    keep("server_panel", srv, spath, xy)

    for label, pt in [("经典服角色页签", P_SERVER_SUB["经典服角色"]),
                      ("青春服页签", P_SERVER_PANEL_TABS["青春服"]),
                      ("已有角色页签", P_SERVER_PANEL_TABS["已有角色"])]:
        print("▶ 点「%s」@%s" % (label, pt))
        ui.tap(*pt)
        time.sleep(2.5)
        itn, pn = read(ui, "ps3_srv_%s" % label[:4])
        dump(itn, "%s（%s）" % (label, pn))
        keep("server_%s" % label[:4], itn, pn, pt)

    # 选中第二个条目（备战区/另一个角色）看看高亮变化
    print("▶ 点第 2 个条目 @%s" % (P_SERVER_ROLE2,))
    ui.tap(*P_SERVER_ROLE2)
    time.sleep(2.5)
    itn, pn = read(ui, "ps3_srv_role2")
    dump(itn, "点第 2 个条目后（%s）" % pn)
    keep("server_role2", itn, pn, P_SERVER_ROLE2)

    print("▶ 点「确定」@%s（看是否直接进游戏）" % (P_SERVER_OK,))
    ui.tap(*P_SERVER_OK)
    time.sleep(6)
    itn, pn = read(ui, "ps3_srv_ok")
    dump(itn, "点「确定」后（%s）" % pn)
    keep("server_ok", itn, pn, P_SERVER_OK)
    items = itn

    # ------------------------------------------------------------ 4. 进主城
    print()
    print("### 4. 进主城")
    for rnd in range(1, 31):
        if ui.is_home(items):
            print("· 已在主城")
            break
        if on_login(ui, items):
            h = ui.hit(items, "开始游戏")
            print("▶ 登录页 → 点「开始游戏」")
            ui.tap(*(h.center if h else (960, 876)))
            time.sleep(14)
        else:
            time.sleep(3)
        items, path = read(ui, "ps3_home_%d" % rnd)
    if not ui.is_home(items):
        print("!! 没能进主城，放弃后续探测")
        items, path = read(ui, "ps3_fail")
        dump(items, "最后停在（%s）" % path)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(RECORDS, f, ensure_ascii=False, indent=2)
        return 1
    dump(items, "主城")
    keep("home", items, path)

    # ------------------------------------------------------------ 5. 主城上的账号入口
    print()
    print("### 5. 主城左上角（头像 / 角色名）——找「切换账号」「注销」入口")
    for name, (x, y) in [("home_avatar", P_HOME_AVATAR), ("home_rolename", (376, 20))]:
        print()
        print("▶ [%s] 点 (%d,%d)" % (name, x, y))
        ui.tap(x, y)
        time.sleep(4.0)
        it2, p2 = read(ui, "ps3_%s" % name)
        dump(it2, "点 [%s] 之后（%s）" % (name, p2))
        keep(name, it2, p2, (x, y))
        # 收回：主城的弹窗一般点「确定」或右上 ✕
        for r in range(4):
            if ui.is_home(it2):
                break
            h = ui.hit(it2, "确定", "关闭")
            if h is not None and 600 <= h.center[0] <= 1350:
                ui.tap(*h.center)
            else:
                ui.tap(*SRV_CLOSE)
            time.sleep(2.5)
            it2, p2 = read(ui, "ps3_%s_clr%d" % (name, r))
        print("   · 收回状态：is_home=%s" % ui.is_home(it2))
        items = it2

    # ------------------------------------------------------------ 6. 主城功能栏找「设置/账号」
    print()
    print("### 6. 主城右侧/底部功能入口扫描")
    for name, (x, y) in [("right_magnifier", 1788, 79), ("right_map", 1900, 79),
                         ("bottom_zhanshu", 60, 1000)]:
        print()
        print("▶ [%s] 点 (%d,%d)" % (name, x, y))
        ui.tap(x, y)
        time.sleep(3.5)
        it2, p2 = read(ui, "ps3_%s" % name)
        dump(it2, "点 [%s] 之后（%s）" % (name, p2))
        keep(name, it2, p2, (x, y))
        for r in range(4):
            if ui.is_home(it2):
                break
            h = ui.hit(it2, "确定", "关闭")
            if h is not None and 600 <= h.center[0] <= 1350:
                ui.tap(*h.center)
            else:
                ui.tap(*SRV_CLOSE)
            time.sleep(2.5)
            it2, p2 = read(ui, "ps3_%s_clr%d" % (name, r))

    # ------------------------------------------------------------ 保存
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(RECORDS, f, ensure_ascii=False, indent=2)
    print()
    print("✓ 侦察完成，%d 个界面 → %s" % (len(RECORDS), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())