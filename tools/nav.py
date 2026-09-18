# -*- coding: utf-8 -*-
"""用新的 Ui 导航层做调试：home / state / look / tap / sub / act / recruit。

用法:
    python tools/nav.py state
    python tools/nav.py home
    python tools/nav.py neizheng
    python tools/nav.py sub 市井
    python tools/nav.py look 任意标签
    python tools/nav.py tap 960 540
    python tools/nav.py act
    python tools/nav.py recruit
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.config import load                       # noqa: E402
from stzb.core import Device, dump_items           # noqa: E402
from stzb.ui import Ui                             # noqa: E402


def main(argv):
    cfg = load()
    adb = cfg.get("device.adb")
    candidates = cfg.get("device.serial_candidates")
    dev = Device(adb=adb, serial_candidates=candidates,
                 shot_dir=os.path.join(ROOT, "logs", "shots"))
    if not dev.connect():
        print("!! ADB 连接失败")
        return 2
    print("ADB 已连接: %s" % dev.serial)
    ui = Ui(dev, cfg, logger=print)

    if not argv:
        argv = ["state"]
    cmd = argv[0]
    if cmd == "state":
        items, p = ui.ocr("state")
        print("状态 = %s   截图=%s" % (ui.state("state2"), p))
        print(dump_items(items))
    elif cmd == "home":
        print("回主城:", ui.to_home())
    elif cmd == "neizheng":
        print("打开内政:", ui.open_neizheng())
    elif cmd == "sub":
        print("打开子面板:", ui.open_sub(argv[1]))
    elif cmd == "act":
        print("打开活动:", ui.open_activity())
    elif cmd == "recruit":
        print("打开招募:", ui.open_recruit())
    elif cmd == "look":
        tag = argv[1] if len(argv) > 1 else "look"
        items, p = ui.ocr(tag)
        print("<<%s>> %d 行  -> %s" % (tag, len(items), p))
        print(dump_items(items))
    elif cmd == "tap":
        ui.tap(int(argv[1]), int(argv[2]), delay=1.0)
        import time
        time.sleep(2)
        ui.state("after_tap")
    else:
        print("未知命令:", cmd)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
