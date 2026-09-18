# -*- coding: utf-8 -*-
"""用 Windows 原生 OCR (Windows.Media.Ocr) 识别截图文字，输出文字 + 中心坐标。

用法: python ocr_probe.py <图片路径> [语言标签]
"""
import asyncio
import os
import sys

from winrt.windows.media.ocr import OcrEngine
from winrt.windows.globalization import Language
from winrt.windows.storage import FileAccessMode
from winrt.windows.storage.streams import FileRandomAccessStream
from winrt.windows.graphics.imaging import BitmapDecoder


async def make_bitmap(path):
    """把本地图片文件读成 SoftwareBitmap。"""
    stream = await FileRandomAccessStream.open_async(os.path.abspath(path), FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    return await decoder.get_software_bitmap_async()


def make_engine(lang_tag="zh-Hans-CN"):
    eng = None
    try:
        eng = OcrEngine.try_create_from_language(Language(lang_tag))
    except Exception as e:
        print("指定语言创建失败:", e)
    if eng is None:
        eng = OcrEngine.try_create_from_user_profile_languages()
    return eng


async def ocr(path, lang_tag="zh-Hans-CN", verbose=True):
    if verbose:
        print("可用识别语言:")
        for lang in OcrEngine.available_recognizer_languages:
            print("   -", lang.language_tag, "|", lang.display_name)
        print()

    bmp = await make_bitmap(path)
    print("图片尺寸: %d x %d" % (bmp.pixel_width, bmp.pixel_height))

    eng = make_engine(lang_tag)
    if eng is None:
        print("!! 无法创建 OCR 引擎")
        return []
    print("引擎语言:", eng.recognizer_language.language_tag)
    print()

    result = await eng.recognize_async(bmp)
    items = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        text = "".join(w.text for w in words)
        x = min(w.bounding_rect.x for w in words)
        y = min(w.bounding_rect.y for w in words)
        x2 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y2 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        cx, cy = int((x + x2) / 2), int((y + y2) / 2)
        items.append({
            "text": text,
            "box": (int(x), int(y), int(x2), int(y2)),
            "center": (cx, cy),
        })
        if verbose:
            print("  [%4d,%4d]-[%4d,%4d] center=(%4d,%4d)  %s" % (x, y, x2, y2, cx, cy, text))
    return items


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else r"E:\Project\率土自动化\recon\game_02.png"
    lt = sys.argv[2] if len(sys.argv) > 2 else "zh-Hans-CN"
    asyncio.run(ocr(p, lt))
