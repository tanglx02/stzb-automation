# -*- coding: utf-8 -*-
"""侦察小工具：截屏 + OCR 打印当前界面文字（可顺带点一个按钮）。

用法:
    python tools/look.py 标签名                  # 只看
    python tools/look.py 标签名 --tap 取消       # 看并点击含该关键词的项
    python tools/look.py 标签名 --tap 确定 --wait 3
"""
import argparse
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stzb.core import Device, find_text, texts  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="look")
    ap.add_argument("--tap", default=None, help="点击包含该关键词的文字")
    ap.add_argument("--wait", type=float, default=0.0, help="点击后等待秒数")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    dev = Device()
    if args.serial:
        dev.serial = args.serial
    if not dev.connect():
        print("!! ADB 连接失败")
        return 2

    archive = os.path.join(ROOT, "recon", "shots")
    os.makedirs(archive, exist_ok=True)

    items, path = dev.ocr(args.tag)
    shutil.copyfile(path, os.path.join(archive, "%s.png" % args.tag))
    print("截图: %s" % os.path.join(archive, "%s.png" % args.tag))
    print("---- 识别到 %d 行 ----" % len(items))
    for it in items:
        print("  [%4d,%4d]-[%4d,%4d] center=(%4d,%4d)  %s"
              % (it.box[0], it.box[1], it.box[2], it.box[3],
                 it.center[0], it.center[1], it.text))

    if args.tap:
        hit = find_text(items, args.tap)
        if hit is None:
            print("\n!! 没找到含「%s」的文字项" % args.tap)
            return 1
        print("\n>> 点击 %s @%s" % (hit.text, hit.center))
        dev.tap_item(hit)
        if args.wait:
            time.sleep(args.wait)
            items2, path2 = dev.ocr(args.tag + "_after")
            print("---- 点击后 (%s) ----" % path2)
            for it in items2:
                print("  center=(%4d,%4d)  %s" % (it.center[0], it.center[1], it.text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
