# -*- coding: utf-8 -*-
"""侦察 2：账号 / 角色切换的真实入口（修正版）。

**上一版的教训**：在登录页按 Android BACK（keyevent 4）会触发
「请问，导致中途退出的原因是？」问卷，里面有「提交并退出」这个危险按钮。
问卷一旦出现会盖住整屏，后续所有探测都看到同一画面（白探 8 次）。
→ 本版铁律：**登录页上绝不用返回键**；一律用面板自己的 ✕ / 确定 退出。

需要一次性跑完（沙箱会回收模拟器进程树）。

用法：python tools/probe_switch2.py
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                                    # noqa: E402
from stzb.core import DEFAULT_ADB, GAME_PKG, Device, ocr_image_scaled  # noqa: E402
from stzb.emulator import MuMu                                  # noqa: E402
from stzb.ui import BTN_START_GAME, Ui                           # noqa: E402

SHOT_DIR = os.path.join(ROOT, "logs", "shots")
OUT = os.path.join(ROOT, "recon", "switch_probe2.json")

FOCUS = ("账号", "帐号", "角色", "换区", "切换", "服务器", "区服", "登录", "退出",
         "用户", "设置", "网易", "大区", "确定", "取消", "继续")

# 登录页关键元素（实测）
LOGIN_TABS = {"已有角色": (235, 249), "经典服": (503, 248), "青春服": (783, 249)}
LOGIN_LEFT = {"最近登录": (246, 325), "经典服角色": (247, 406)}
ROLE_ENTRIES = [(651, 416), (665, 539)]
BTN_OK = (960, 895)
SURVEY_CONTINUE = (427, 837)   # 「继续游戏」（问卷里唯一安全的键），实测兜底
SRV_CLOSE = (1760, 156)        # 「选择服务器」右上 ✕（按显示比例折算，待实测校验）


def dump(items, title):
    print("=" * 74)
    print(title)
    print("=" * 74)
    for it in items:
        x1, y1, x2, y2 = it.box
        cx, cy = it.center
        flag = "  <<<" if any(f in it.text for f in FOCUS) else ""
        print("  %-38s c=(%4d,%4d) box=(%4d,%4d)-(%4d,%4d)%s"
              % (it.text[:36], cx, cy, x1, y1, x2, y2, flag))
    print("  —— %d 行" % len(items))
    print()


def read(ui, tag: str):
    """读一帧；0 行就放大 2 倍再读（淡色/小字兜底）。"""
    items, path = ui.ocr(tag)
    if not items:
        items = ocr_image_scaled(path, 2, tmp_path=path + ".x2.png")
        print("  （帧内 0 行 → 放大 2 倍读到 %d 行）" % len(items))
    return items, path


ui = None                     # main() 里赋值；下面两个辅助函数要用


def ui_has(items, *kws) -> bool:
    """判断界面是否含某关键词。

    **必须走 Ui.has**（别名表 + 模糊匹配），不能用朴素子串匹配：
    OCR 常把「开始游戏」读成「廾始游戏」，子串匹配会漏 ——
    上一版探测脚本就因此在登录页空转了 60 轮，什么都没探到。
    """
    return ui.has(items, *kws) if ui is not None else False


def is_survey(items) -> bool:
    """是不是那个危险的「导致中途退出的原因是？」问卷。"""
    return ui_has(items, "中途退出")


def clear_to(base_items, ui_, dev, tag: str, max_rounds: int = 6):
    """把屏幕清干净，回到「靠 base_items 判定」的那个界面。

    只用 ✕ / 确定 / 继续游戏，**绝不按返回键**。
    """
    for r in range(max_rounds):
        items, path = read(ui_, "%s_clr%d" % (tag, r))
        if not items:
            return items, path
        # 退出问卷 → 点「继续游戏」（唯一安全键）；找不到就用兜底坐标
        if is_survey(items):
            hit = ui_.hit(items, *KW_继续)
            xy = hit.center if hit else SURVEY_CONTINUE
            print("  · 出现退出问卷 → 点「继续游戏」@%s" % (xy,))
            ui_.tap(*xy)
            time.sleep(3)
            continue
        # 选择服务器 / 其它面板 → 点右上 ✕
        if ui_.has(items, *KW_选服):
            print("  · 面板还开着 → 点右上 ✕ @%s" % (SRV_CLOSE,))
            ui_.tap(*SRV_CLOSE)
            time.sleep(2.5)
            continue
        return items, path
    return read(ui_, "%s_clr_end" % tag)


def go_login(ui, dev, base_items, tag="gl"):
    """确保停在登录页。"""
    items = base_items
    for r in range(20):
        if ui_has(items, "开始游戏") and not ui.is_home(items):
            return items, ""
        if ui.is_home(items):
            print("  · 已在主城，需要回调登录页")
            return items, "home"
        items, path = read(ui, "%s_%02d" % (tag, r))
        if ui_has(items, "开始游戏") and not ui.is_home(items):
            return items, path
        if not items:
            ui.tap(960, 540)          # 封面页
            time.sleep(5)
            continue
        time.sleep(2.5)
    return items, ""


def main() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    global ui
    cfg = load()
    adb = cfg.get("device.adb") or DEFAULT_ADB
    pkg = cfg.get("device.package") or GAME_PKG
    records: list = []

    emu = MuMu(manager=cfg.get("emulator.manager"),
               vmindex=cfg.get("emulator.vmindex", 0), adb=adb,
               serial_candidates=cfg.get("device.serial_candidates"), logger=print)
    if emu.is_running():
        emu.wait_ready(timeout=300)
    else:
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
                break

    ui = Ui(dev, cfg, logger=print, dry_run=False)

    # ---------------------------------------------------------- 到登录页
    items, _ = read(ui, "p2_boot_0")
    blank = 0
    for rnd in range(1, 61):
        if ui_has(items, "开始游戏") and not ui.is_home(items):
            break
        if not items:
            blank += 1
            if blank >= 3:
                blank = 0
                ui.tap(960, 540)
                time.sleep(5)
                items, _ = read(ui, "p2_boot_%d" % rnd)
                continue
        else:
            blank = 0
        time.sleep(2.5)
        items, _ = read(ui, "p2_boot_%d" % rnd)
    else:
        print("!! 没能停在登录页")
        return 1

    dump(items, "登录页（基准）")
    records.append({"name": "login", "lines": [{"t": i.text, "c": list(i.center)}
                                               for i in items]})

    # ---------------------------------------------------------- 探「点击换区」并完整走一遍
    print()
    print("### A. 「选择服务器」面板的完整结构")
    hit = ui.hit(items, "点击换区", "换区")
    xy = ui._kw_point(hit, ("点击换区", "换区")) if hit else (1100, 759)
    print("▶ 点「点击换区」@%s" % (xy,))
    ui.tap(*xy)
    time.sleep(3.5)
    srv, spath = read(ui, "p2_srv")
    dump(srv, "「选择服务器」面板（截图 %s）" % spath)
    records.append({"name": "server_panel", "lines": [{"t": i.text, "c": list(i.center),
                                                       "box": list(i.box)} for i in srv]})

    # A1. 点「经典服角色」子页签
    print("▶ 点左侧「经典服角色」子页签")
    ui.tap(*LOGIN_LEFT["经典服角色"])
    time.sleep(2.5)
    srv2, spath2 = read(ui, "p2_srv_tab2")
    dump(srv2, "「经典服角色」页签（截图 %s）" % spath2)
    records.append({"name": "server_tab_classic", "lines":
                    [{"t": i.text, "c": list(i.center), "box": list(i.box)} for i in srv2]})

    # A2. 点「青春服」大页签
    print("▶ 点顶部「青春服」页签")
    ui.tap(*LOGIN_TABS["青春服"])
    time.sleep(2.5)
    srv3, spath3 = read(ui, "p2_srv_qc")
    dump(srv3, "「青春服」页签（截图 %s）" % spath3)
    records.append({"name": "server_tab_qingchun", "lines":
                    [{"t": i.text, "c": list(i.center), "box": list(i.box)} for i in srv3]})

    # A3. 回到「已有角色」并选中现有角色 → 确定（看是否进入该角色）
    print("▶ 回「已有角色」→ 选第一个角色 → 点「确定」")
    ui.tap(*LOGIN_TABS["已有角色"])
    time.sleep(2.0)
    items_now, _ = read(ui, "p2_srv_back")
    hit = ui.hit(items_now, "X6014", "龙兴之")
    if hit:
        print("  · 找到角色条目「%s」@%s" % (hit.text, hit.center))
        ui.tap(*hit.center)
        time.sleep(2.5)
        after, apath = read(ui, "p2_srv_picked")
        dump(after, "选中角色后（截图 %s）" % apath)
    else:
        print("  · 没读到角色条目，用兜底 %s" % (ROLE_ENTRIES[0],))
        ui.tap(*ROLE_ENTRIES[0])
        time.sleep(2.5)
    ui.tap(*BTN_OK)
    time.sleep(5)
    after_ok, apath2 = read(ui, "p2_srv_ok")
    dump(after_ok, "点「确定」之后（截图 %s）" % apath2)
    records.append({"name": "after_confirm", "lines":
                    [{"t": i.text, "c": list(i.center), "box": list(i.box)} for i in after_ok]})

    # ---------------------------------------------------------- 探登录页左右图标
    print()
    print("### B. 登录页两侧图标（逐一点，点完用 ✕/确定 收回，绝不按返回键）")
    li, _ = go_login(ui, dev, after_ok, tag="p2_relogin")
    if not ui_has(li, "开始游戏"):
        print("  ! 没能回到登录页，跳过图标探测")
    else:
        cands = [("left1", 60, 62), ("left2", 60, 128), ("left3", 60, 194),
                 ("left4", 60, 261), ("left5", 60, 327),
                 ("right1", 1906, 79), ("right2", 1906, 132), ("right3", 1906, 191)]
        for name, x, y in cands:
            print()
            print("▶ [%s] 点 (%d,%d)" % (name, x, y))
            ui.tap(x, y)
            time.sleep(3.0)
            it2, p2 = read(ui, "p2_%s" % name)
            if not it2:
                print("  · 点了没反应（读不到任何文字）→ 判定为无功能/纯装饰")
                continue
            dump(it2, "点 [%s] 之后（截图 %s）" % (name, p2))
            records.append({"name": name, "point": [x, y],
                            "lines": [{"t": i.text, "c": list(i.center),
                                       "box": list(i.box)} for i in it2]})
            # 收回：✕ / 确定 / 继续游戏
            for r in range(4):
                if ui_has(it2, "开始游戏"):
                    break
                if is_survey(it2):
                    h = ui.hit(it2, "继续游戏", "继续")
                    ui.tap(*(h.center if h else SURVEY_CONTINUE))
                else:
                    h = ui.hit(it2, "确定", "关闭")
                    if h:
                        ui.tap(*h.center)
                    else:
                        ui.tap(*SRV_CLOSE)
                time.sleep(2.5)
                it2, p2 = read(ui, "p2_%s_clr%d" % (name, r))
            if ui_has(it2, "开始游戏"):
                print("  · 已回到登录页")
            else:
                print("  ! 收不回来，进入下一步")

    # ---------------------------------------------------------- 保存
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print()
    print("结果已存 %s（%d 个界面）" % (OUT, len(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())