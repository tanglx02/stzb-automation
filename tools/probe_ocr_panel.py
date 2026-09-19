# -*- coding: utf-8 -*-
"""面板 OCR 诊断：判断「这个面板到底是没打开，还是 OCR 整屏漏读」。

**为什么要有它（2026-09-20 实测事故）：**
税收任务报「进不去「税收」」，排查半天以为是点击坐标不对，最后发现
**面板每次都开成功了、是 OCR 把整屏读成 1 行导致判定不认** ——
于是脚本自己把已经开好的面板退掉重来，循环耗尽后报「进不去」。
同一张图放大 2 倍能读到 12 行。

这个脚本就是当时那套排查过程的固化：喂它一张（或一批）截图，
它把「原图 OCR / 放大 OCR」两路结果和面板判定并排打出来。

用法：
    # 单张图
    python tools/probe_ocr_panel.py logs/shots/xxx.png

    # 按通配符扫一批（看同一面板不同帧的波动）
    python tools/probe_ocr_panel.py "logs/shots/sub_pre_税收_*.png"

    # 加上某个面板的判定列（税收/市井/特性/演武）
    python tools/probe_ocr_panel.py "logs/shots/*税收*.png" --panel 税收

判读要点：
    · 原图行数很低（<4）而放大后行数明显变多 → **OCR 漏读**，
      判定路径必须带放大兜底（见 stzb/ui.py 的 ocr_multi(scaled_fallback=)）。
    · 两路都读不出 → 才是真的没打开 / 画面异常。
    · 同一批图里行数忽高忽低 → 典型的「时好时坏」，属于漏读，不是逻辑错。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stzb.core import ocr_image, ocr_image_scaled          # noqa: E402
from stzb.ui import Ui                                      # noqa: E402


class _HeadlessUi(Ui):
    """不连设备的 Ui：只借用它的纯判定函数（_in_panel / is_home 等）。"""

    def __init__(self) -> None:
        pass

    def log(self, msg: str) -> None:                        # noqa: D102
        pass


def _panel_verdict(ui: Ui, panel: str, items) -> str:
    if panel not in ("税收", "市井", "特性", "演武"):
        return "-"
    try:
        return "是" if ui._in_panel(panel, items) else "否"
    except Exception as e:                                   # pragma: no cover
        return "判定异常(%r)" % (e,)


def main() -> int:
    ap = argparse.ArgumentParser(description="面板 OCR 诊断：漏读 or 真没打开")
    ap.add_argument("paths", nargs="+", help="截图路径，支持通配符")
    ap.add_argument("--panel", default="", help="附带判定这个面板开没开")
    ap.add_argument("--factor", type=int, default=2, help="放大倍数（默认 2）")
    ap.add_argument("--texts", action="store_true", help="打印 OCR 原文")
    args = ap.parse_args()

    files: list[str] = []
    for p in args.paths:
        hit = glob.glob(p)
        files.extend(sorted(hit) if hit else [p])
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        print("没有匹配到任何文件。")
        return 1

    ui = _HeadlessUi()
    low = 0
    for p in files:
        plain = ocr_image(p)
        big = ocr_image_scaled(p, factor=args.factor)
        # 关键判据：原图读得很少、放大后明显变多 = 漏读
        leaked = len(plain) < 4 and len(big) > len(plain)
        if leaked:
            low += 1
        print("=" * 72)
        print("%s" % os.path.basename(p))
        print("  原图 行数=%-3d   放大%dx 行数=%-3d   %s"
              % (len(plain), args.factor, len(big),
                 "★ 疑似漏读（放大救回来了）" if leaked else ""))
        if args.panel:
            print("  _in_panel(%s)：原图=%s  放大=%s"
                  % (args.panel, _panel_verdict(ui, args.panel, plain),
                     _panel_verdict(ui, args.panel, big)))
        if args.texts:
            print("  原图: %s" % " | ".join(it.text for it in plain))
            print("  放大: %s" % " | ".join(it.text for it in big))

    print("=" * 72)
    print("共 %d 张；疑似 OCR 漏读 %d 张。" % (len(files), low))
    if low:
        print("→ 漏读帧要让判定路径带放大兜底（stzb/ui.py: ocr_multi(..., "
              "scaled_fallback=True)）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
