# -*- coding: utf-8 -*-
"""侦察小工具：只「看」招募面板，绝不点抽卡按钮。

用途：验证半价判据（红丝带 / 半价字样 / 价格）在**当天的真实界面**上是否成立，
而不用真的花掉那 100 虎符（虎符不够时是 100 玉符）。

用法:
    python tools/probe_recruit.py            # 打开招募面板看一眼就退回主城
    python tools/probe_recruit.py --keep     # 看完停在招募面板不退回
    python tools/probe_recruit.py --free     # 顺便报告免费绿标签状态
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from run_daily import LOCK, RunLock                                   # noqa: E402
from stzb.config import load                                          # noqa: E402
from stzb.core import Device, read_png                                # noqa: E402
from stzb.tasks import (_has_discount_ribbon, _has_free_badge,        # noqa: E402
                        _price_under_btn, _read_recruit_price, _select_pack)
from stzb.ui import Ui                                                # noqa: E402

SHOT_DIR = os.path.join(ROOT, "logs", "shots")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="看完停在招募面板")
    ap.add_argument("--free", action="store_true", help="顺便报告免费绿标签")
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
    if not ui.open_recruit():
        log("!! 打不开招募面板")
        return 5

    # 这里只是「选中卡包、把抽屉打开」，不动任何抽卡按钮
    if not _select_pack(ui, log):
        log("!! 没能选中卡包抽屉")
        return 6

    items, path = ui.ocr("probe_rc")
    img = read_png(path)
    btn = None
    for it in ui.all_of(items, "招募1次"):
        btn = it
        break
    if btn is None:
        log("!! 没读到「招募1次」按钮；OCR %d 行，截图 %s" % (len(items), path))
        return 7

    log("")
    log("截图：%s（OCR %d 行）" % (path, len(items)))
    log("「招募1次」按钮：%s box=%s" % (btn.center, btn.box))
    log("  ① 红色「打折」丝带 : %s" % _has_discount_ribbon(img, btn))
    log("  ② 卡包列「半价」字样: %s"
        % [it.text for it in items if "半价" in it.text])
    log("  ③ 帧内直读到的价格  : %s" % _price_under_btn(items, btn))
    log("  ③ 三重手段最终价格  : %s"
        % _read_recruit_price(ui, items, btn, path, "probe_rc"))
    if args.free:
        log("  ④ 绿色「免费」标签  : %s" % _has_free_badge(img, btn))
    log("")
    log("（本工具不会点击任何抽卡按钮）")

    if not args.keep:
        ui.to_home()
    return 0


if __name__ == "__main__":
    sys.exit(main())
