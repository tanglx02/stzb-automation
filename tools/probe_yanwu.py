# -*- coding: utf-8 -*-
"""侦察小工具：只「看」演武面板，绝不点任何领取/扫荡按钮。

用途：把「演武」面板的真实文字、坐标、截图全部打印出来，
用来确定锚点词和「扫荡奖励」按钮的位置，而不误领任何东西。

用法:
    python tools/probe_yanwu.py            # 进内政 → 点演武 → 打印 → 回主城
    python tools/probe_yanwu.py --keep     # 看完停在演武面板
    python tools/probe_yanwu.py --tries 3  # 多试几个候选点（默认 3）
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from run_daily import LOCK, RunLock                                   # noqa: E402
from stzb.config import load                                          # noqa: E402
from stzb.core import Device                                          # noqa: E402
from stzb.ui import NZ_ENTRY_FALLBACK, KW, Ui                          # noqa: E402

SHOT_DIR = os.path.join(ROOT, "logs", "shots")


def dump(ui, tag, title):
    items, path = ui.ocr_multi(tag, n=2)
    print("")
    print("=" * 78)
    print("%s  截图=%s  OCR=%d 行" % (title, path, len(items)))
    print("=" * 78)
    for it in sorted(items, key=lambda i: (i.center[1] // 30, i.center[0])):
        print("  %-5s %-4s  %s" % (str(it.center[0]), str(it.center[1]), it.text))
    return items, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="看完停在演武面板")
    ap.add_argument("--tries", type=int, default=3, help="演武入口最多试几个候选点")
    args = ap.parse_args()

    os.makedirs(SHOT_DIR, exist_ok=True)
    log = lambda m: print(m, flush=True)          # noqa: E731
    cfg = load()

    lock = RunLock(LOCK, log)
    if not lock.acquire():
        return 2

    dev = Device(adb=cfg.get("device.adb"),
                 serial_candidates=cfg.get("device.serial_candidates"),
                 shot_dir=SHOT_DIR)
    if not dev.connect():
        log("!! ADB 连接失败：模拟器没启动？")
        return 3

    ui = Ui(dev, cfg, logger=log, dry_run=False)
    if not ui.boot():
        log("!! 没能进入主城")
        return 4

    if not ui.open_neizheng():
        log("!! 打不开内政")
        return 5

    items, _ = dump(ui, "probe_yw_nz", "内政主界面（找「演武」入口）")

    hit = ui.hit(items, *KW["演武"])
    cands = []
    if hit is not None:
        cands.append(tuple(hit.center))
        cands.append((hit.center[0], hit.center[1] - 40))
        cands.append((hit.center[0], hit.center[1] + 40))
    cands.extend(tuple(p) for p in NZ_ENTRY_FALLBACK["演武"])
    # 去重（按 12px 网格）
    seen, pts = set(), []
    for c in cands:
        k = (int(c[0] // 12), int(c[1] // 12))
        if k not in seen:
            seen.add(k)
            pts.append(c)

    for i, p in enumerate(pts[:max(1, args.tries)], 1):
        log("  → 试第 %d 个候选点 %s" % (i, p))
        ui.tap(*p)
        time.sleep(3.0)
        items, _ = ui.ocr("probe_yw_try%d" % i)
        if not ui._is_neizheng_screen(items) and not ui.is_home(items):
            log("    ✓ 好像进去了（既不在主城、也不在内政界面）")
            break
        log("    · 仍在内政界面，换下一个")

    dump(ui, "probe_yw_panel", "演武面板（看锚点词与「扫荡」相关按钮）")
    log("")
    log("（本工具不点击任何领取/扫荡按钮）")

    if not args.keep:
        ui.to_home()
    return 0


if __name__ == "__main__":
    sys.exit(main())
