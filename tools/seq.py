# -*- coding: utf-8 -*-
"""按序列驱动游戏并留证：tap / back / look / sleep / swipe。

用法:
    python tools/seq.py "tap 1765 155" "look a" "back" "look b"
"""
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from stzb.core import Device  # noqa: E402

ARCH = os.path.join(ROOT, "recon", "shots")


def main(steps):
    dev = Device()
    if not dev.connect():
        print("!! ADB 连接失败")
        return 2
    os.makedirs(ARCH, exist_ok=True)
    for st in steps:
        parts = st.split()
        op = parts[0]
        if op == "tap":
            x, y = int(parts[1]), int(parts[2])
            print(">> tap (%d,%d)" % (x, y))
            dev.tap(x, y)
        elif op == "tapkw":
            kw = parts[1]
            items, _ = dev.ocr("kw_" + kw)
            hit = None
            for it in items:
                if kw in it.text.replace(" ", ""):
                    hit = it
                    break
            if hit is None:
                print("!! 未找到 %s" % kw)
                continue
            print(">> tapkw %s @%s" % (kw, hit.center))
            dev.tap_item(hit)
        elif op == "back":
            print(">> back")
            dev.back(1.2)
        elif op == "sleep":
            print(">> sleep %s" % parts[1])
            time.sleep(float(parts[1]))
        elif op == "swipe":
            x1, y1, x2, y2 = map(int, parts[1:5])
            print(">> swipe")
            dev.swipe(x1, y1, x2, y2, 500)
        elif op == "look":
            tag = parts[1]
            items, p = dev.ocr(tag)
            dst = os.path.join(ARCH, tag + ".png")
            shutil.copyfile(p, dst)
            print("   <<%s>> %d 行  -> %s" % (tag, len(items), dst))
            for it in items:
                print("      center=(%4d,%4d)  %s" % (it.center[0], it.center[1], it.text))
        else:
            print("?? 未知操作:", st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
