# -*- coding: utf-8 -*-
"""率土之滨每日任务自动化 —— 核心能力层。

提供：
  - Device      : ADB 设备封装（多端口自动探测 / 截图 / 点击 / 滑动 / 启停应用）
  - 原生 OCR    : Windows.Media.Ocr，识别中文并把结果转成带坐标的 TextItem
  - 模糊匹配    : 游戏用美术字体，OCR 会把「特性」读成「犄性」，所以关键词匹配
                  走「精确 → 子串 → 相似度」三级
  - 模板匹配    : OpenCV 多尺度匹配，作为 OCR 失效时的第二手段
  - wait_until  : 轮询等待

坐标体系：游戏强制横屏，截图与 input 坐标均为 1920x1080（见 recon 记录）。
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 重依赖（OpenCV / numpy / WinRT）**容错导入**。
#
# 这样拆开是有意的：本模块上半部分是**纯文本决策逻辑**（OCR 结果归一化、
# 关键词模糊匹配），下半部分才真正碰图像和系统 OCR。把重依赖做成容错的，
# 纯逻辑就能在没有装 opencv / winrt 的机器上单独跑测试和复用，
# 而不是一 import 就炸。
#
# 真要用到图像/OCR 能力时，下面这些名字一定存在（否则那台机器本来也跑不了
# 抓屏/识别），所以调用点不需要额外判空。
# ---------------------------------------------------------------------------

np = None                                       # type: ignore
cv2 = None                                      # type: ignore
Language = BitmapDecoder = OcrEngine = None     # type: ignore
FileAccessMode = FileRandomAccessStream = None  # type: ignore

try:
    import numpy as np                                        # noqa: F811
    import cv2                                                # noqa: F811
except Exception:                               # pragma: no cover
    pass

try:
    from winrt.windows.globalization import Language                          # noqa: F811
    from winrt.windows.graphics.imaging import BitmapDecoder                   # noqa: F811
    from winrt.windows.media.ocr import OcrEngine                              # noqa: F811
    from winrt.windows.storage import FileAccessMode                           # noqa: F811
    from winrt.windows.storage.streams import FileRandomAccessStream           # noqa: F811
except Exception:                               # pragma: no cover
    pass

# --------------------------------------------------------------------------- 常量

DEFAULT_ADB = r"C:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEFAULT_SERIAL = "127.0.0.1:7555"
GAME_PKG = "com.netease.stzb.netease"
SCREEN_W, SCREEN_H = 1920, 1080

Point = Tuple[int, int]


# --------------------------------------------------------------------------- OCR

def _need_vision(what: str) -> None:
    """图像/OCR 能力缺依赖时给出可操作的报错，而不是莫名的 AttributeError。"""
    if cv2 is None or np is None:
        raise RuntimeError(
            "%s 需要 OpenCV/numpy，但当前 Python 环境里没装。\n"
            "  装依赖：pip install -r requirements-client.txt" % what)
    if OcrEngine is None:
        raise RuntimeError(
            "%s 需要 WinRT 的 Windows.Media.Ocr，但当前环境里没装。\n"
            "  装依赖：pip install -r requirements-client.txt" % what)


@dataclass
class TextItem:
    text: str
    box: Tuple[int, int, int, int]      # x1, y1, x2, y2
    center: Point
    score: float = 1.0                  # find_text 命中时写入，便于排序

    @property
    def w(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def h(self) -> int:
        return self.box[3] - self.box[1]

    def __repr__(self) -> str:
        return "<%s @%s>" % (self.text, self.center)


_ocr_engine: Optional[OcrEngine] = None


def _get_ocr_engine(lang_tag: str = "zh-Hans-CN") -> Optional[OcrEngine]:
    global _ocr_engine
    if OcrEngine is None:
        _need_vision("Windows OCR")
    if _ocr_engine is None:
        try:
            _ocr_engine = OcrEngine.try_create_from_language(Language(lang_tag))
        except Exception:
            _ocr_engine = None
        if _ocr_engine is None:
            _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
    return _ocr_engine


async def _ocr_async(path: str) -> List[TextItem]:
    eng = _get_ocr_engine()
    if eng is None:
        raise RuntimeError("无法创建 Windows OCR 引擎，请确认已安装中文识别语言包")
    stream = await FileRandomAccessStream.open_async(os.path.abspath(path), FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    result = await eng.recognize_async(bmp)
    items: List[TextItem] = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        text = "".join(w.text for w in words)
        x1 = min(w.bounding_rect.x for w in words)
        y1 = min(w.bounding_rect.y for w in words)
        x2 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y2 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        items.append(TextItem(text,
                              (int(x1), int(y1), int(x2), int(y2)),
                              (int((x1 + x2) / 2), int((y1 + y2) / 2))))
    return items


def ocr_image(path: str) -> List[TextItem]:
    """对图片文件做 OCR（内部新建事件循环）。失败返回空列表，不抛异常。"""
    try:
        return asyncio.run(_ocr_async(path))
    except Exception:
        return []


# --------------------------------------------------------------------------- 文本匹配

_DROP = set(" \t\r\n·．.,，。:：;；!！?？()（）[]【】{}<>《》\"'`|/\\-_+=~^*&#@$%")


def norm(s: str) -> str:
    """归一化：去掉 OCR 常见的标点/空格噪声，便于模糊比较。"""
    return "".join(ch for ch in s if ch not in _DROP)


# 美术字体的 OCR 误认字表：左 = OCR 读出来的错字，右 = 正确的字。
# 只收录实测截图核对过的，以及由它们推出的确定性偏旁拆解，不要凭感觉加。
_OCR_SWAP = {
    "犄": "特", "持": "特",              # 「特性」被读成「犄性 / 持性」
    "橈": "税", "稅": "税",              # 「税收」被读成「橈收」
    "巿": "市",                          # 「市井」的「市」和「巿」形近
    "鼠": "即", "刂": "即",              # 「立即获取」被读成「立鼠获取」
    "丬": "征", "氵": "征", "正": "征",  # 「征收」的偏旁被单独识别出来
    "攵": "收", "収": "收", "攴": "收",  # 同上
}
# 插在字中间的噪声字（OCR 把一个字拆成两半时多出来的）。只在**待识别文本**里删掉。
_OCR_DROP = set("叫鞋")

_MATCH_TRANS = str.maketrans(_OCR_SWAP)


def fix_ocr(s: str, drop: bool = False) -> str:
    """修正美术字体误认。

    注意 drop 参数：删噪声字只能对「OCR 读出来的文本」做，绝不能对「关键词」做。
    否则关键词会退化：锚点「特鞋」若被删成单字「特」，就成了「特性」的子串，
    内政主界面上的入口「犄性」会被误判成「已经打开特性面板了」——实测踩过。
    """
    s = s.translate(_MATCH_TRANS)
    if drop:
        s = "".join(ch for ch in s if ch not in _OCR_DROP)
    return s


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_score(text: str, keyword: str) -> float:
    """给一条 OCR 文本和一个关键词打分：1.0 最佳，0 表示不匹配。

    评分策略（高到低）：
      1.00  修正误认字后完全相等（犄性→特性、橈叫攵→税收）
      0.90+ 关键词是文本的子串
      0.62- 形近模糊匹配，且只用于「长度接近」的两个字符串

    关键约束：长度差超过 30% 时一律不算模糊匹配。否则主城右下角的「招募」
    按钮会被算成「招募1次」（相似度 0.667），把主城误判成招募卡包详情页，
    导致 to_home() 永远退不回去。
    """
    t = fix_ocr(norm(text), drop=True)
    k = fix_ocr(norm(keyword))
    if not t or not k:
        return 0.0
    if t == k:
        return 1.0
    if k in t:
        return 0.90 + 0.09 * (len(k) / max(len(t), 1))
    short, long_ = (t, k) if len(t) <= len(k) else (k, t)
    if len(short) < 2 or len(short) / max(len(long_), 1) < 0.70:
        return 0.0
    r = similar(t, k)
    # 顺序无关的字符重合度：OCR 有时会把两个字的位置调换
    common = len(set(t) & set(k)) / max(len(set(k)), 1)
    r = max(r, common)
    if r >= 0.70:
        return min(0.88, 0.62 + (r - 0.70))
    return 0.0


def find_text(items: Sequence[TextItem], *keywords: str,
              exact: bool = False, min_ratio: float = 0.62) -> Optional[TextItem]:
    """在 OCR 结果里找最匹配任一关键词的项。低于 min_ratio 的一律不算命中。"""
    best: Optional[TextItem] = None
    best_score = 0.0
    for it in items:
        for kw in keywords:
            s = match_score(it.text, kw)
            if exact and s < 1.0:
                continue
            if s < min_ratio:
                continue
            if s > best_score:
                best, best_score = it, s
    if best is not None:
        best.score = best_score
    return best


def find_all_text(items: Sequence[TextItem], *keywords: str,
                  min_ratio: float = 0.62) -> List[TextItem]:
    out = []
    for it in items:
        if any(match_score(it.text, kw) >= min_ratio for kw in keywords):
            out.append(it)
    return out


def texts(items: Sequence[TextItem]) -> List[str]:
    return [it.text for it in items]


def find_login_button(items: Sequence[TextItem]) -> Optional[TextItem]:
    """精确找网易登录页那个真正的「登录」按钮。

    为什么不能直接 find_text(items, "登录")：界面上「自动登录 / 上次登录 /
    其他账号登录」都含「登录」二字，模糊匹配会挑错——点「其他账号登录」
    会走去密码登录流程（脚本没有密码，必然卡死）。

    这是 ui.py 与 account.py 共用的实现，放在 core 里避免两处逻辑漂移。
    找不到返回 None，调用方用固定坐标兜底。
    """
    for it in items:
        if norm(it.text) == "登录":
            return it
    for it in items:
        t = norm(it.text)
        if "登录" not in t:
            continue
        if any(bad in t for bad in ("自动登录", "上次登录", "其他账号", "其他帐号")):
            continue
        if match_score(it.text, "登录") >= 0.8 and it.center[1] > 500:
            return it
    return None


def dump_items(items: Sequence[TextItem], prefix: str = "      | ") -> str:
    return "\n".join("%s(%4d,%4d) %s" % (prefix, it.center[0], it.center[1], it.text)
                     for it in items)


# 脱敏账号形如 159****4508 / a***@qq.com
_MASK_RE = re.compile(r"(\d{3}\*{2,4}\d{3,4})|([A-Za-z0-9._-]{1,3}\*{2,6}@?[A-Za-z0-9._-]*)")


def mask_of(text: str) -> Optional[str]:
    """从一段 OCR 文字里抠出脱敏账号（159****4508）。"""
    if not text:
        return None
    m = _MASK_RE.search(text)
    return m.group(0) if m else None


def find_masked(items: Sequence[TextItem]) -> Optional[str]:
    """在当前屏幕的文字里找脱敏账号——它是「这是网易登录页」的铁证之一。"""
    for it in items:
        mk = mask_of(it.text)
        if mk:
            return mk
    return None


# --------------------------------------------------------------------------- 设备

class AdbError(RuntimeError):
    pass


class Device:
    """MuMu 模拟器的 ADB 封装。"""

    def __init__(self, adb: str = DEFAULT_ADB, serial: str = DEFAULT_SERIAL,
                 serial_candidates: Optional[Sequence[str]] = None,
                 timeout: int = 20, shot_dir: Optional[str] = None):
        self.adb = adb
        self.serial = serial
        self.serial_candidates = list(serial_candidates or [serial])
        if serial not in self.serial_candidates:
            self.serial_candidates.insert(0, serial)
        self.timeout = timeout
        self.shot_dir = shot_dir or os.path.join(tempfile.gettempdir(), "stzb_shots")
        os.makedirs(self.shot_dir, exist_ok=True)
        self._shot_seq = 0

    # ---- 底层 ----

    def raw(self, *args: str, check: bool = True, timeout: Optional[int] = None) -> str:
        cmd = [self.adb, "-s", self.serial] + list(args)
        p = subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                           timeout=timeout or self.timeout)
        if check and p.returncode != 0:
            raise AdbError("adb %s 失败: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
        return p.stdout

    def raw_bytes(self, *args: str, timeout: Optional[int] = None) -> bytes:
        cmd = [self.adb, "-s", self.serial] + list(args)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
        if p.returncode != 0:
            raise AdbError("adb %s 失败" % " ".join(args))
        return p.stdout

    def adb_global(self, *args: str, timeout: Optional[int] = None) -> str:
        p = subprocess.run([self.adb] + list(args), capture_output=True, text=True,
                           errors="ignore", timeout=timeout or self.timeout)
        return (p.stdout or "") + (p.stderr or "")

    # ---- 连接 ----

    def _online_serials(self) -> List[str]:
        try:
            out = self.adb_global("devices")
        except Exception:
            return []
        found = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of"):
                continue
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 2 and parts[1] == "device":
                found.append(parts[0])
        return found

    def connect(self, retries: int = 3, wait: float = 1.5) -> bool:
        """确保设备在线。MuMu 的 adb 端口可能变（7555/16384/5555），逐个试。"""
        for i in range(retries):
            # 1) 先看有没有已经连上的候选
            online = self._online_serials()
            for cand in self.serial_candidates:
                if cand in online:
                    self.serial = cand
                    return True
            # 2) 主动 connect 各候选端口
            for cand in self.serial_candidates:
                if cand.startswith("emulator"):
                    continue
                try:
                    self.adb_global("connect", cand, timeout=8)
                except Exception:
                    pass
            online = self._online_serials()
            for cand in self.serial_candidates:
                if cand in online:
                    self.serial = cand
                    return True
            if online:                      # 有设备但不在候选里，也先用着
                self.serial = online[0]
                return True
            time.sleep(wait)
        return False

    def is_online(self) -> bool:
        try:
            return self.serial in self._online_serials()
        except Exception:
            return False

    # ---- 屏幕 ----

    def screenshot(self, save_as: Optional[str] = None) -> np.ndarray:
        """截屏返回 BGR ndarray，同时可选存盘（OCR 需要文件路径）。

        自带三级重试 —— adb 截屏**会偶发失败**（模拟器刚起来、CPU 吃满、连接刚重连
        时最常见），实测报错就是 `adb exec-out screencap -p 失败`。这种一次性抖动
        不该让整个任务挂掉：

          1. 正常走 `exec-out screencap -p`
          2. 失败 → 先 `adb connect` 重连一次再试（连接半死是最常见的原因）
          3. 还失败 → 退到「先存到设备上再 cat 回来」的老办法
             （个别 adb 版本 exec-out 会失败，但 pull/cat 一直好使）
        """
        last = ""
        _need_vision("截图")
        for attempt in (1, 2, 3):
            try:
                if attempt == 1:
                    data = self.raw_bytes("exec-out", "screencap", "-p", timeout=60)
                elif attempt == 2:
                    self.adb_global("connect", self.serial, timeout=8)
                    time.sleep(0.5)
                    data = self.raw_bytes("exec-out", "screencap", "-p", timeout=60)
                else:
                    tmp = "/sdcard/_stzb_shot.png"
                    self.raw("shell", "screencap", "-p", tmp, check=False)
                    data = self.raw_bytes("exec-out", "cat", tmp, timeout=60)
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
                time.sleep(0.8)
                continue

            if not data:
                last = "返回空数据"
                time.sleep(0.8)
                continue
            img = None
            try:
                img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            except Exception as e:
                last = "解码异常 %r" % (e,)
            if img is None:
                last = last or "数据无法解码为图片"
                time.sleep(0.8)
                continue
            if save_as:
                write_png(save_as, img)
            return img

        raise AdbError("截屏连续 3 次失败：%s" % last)

    def shot_path(self, tag: str = "shot") -> str:
        self._shot_seq += 1
        return os.path.join(self.shot_dir, "%s_%03d.png" % (tag, self._shot_seq))

    def snapshot(self, tag: str = "shot") -> Tuple[np.ndarray, str]:
        """截屏 + 存盘，返回 (图像, 文件路径)。"""
        p = self.shot_path(tag)
        img = self.screenshot(save_as=p)
        return img, p

    def ocr(self, tag: str = "ocr") -> Tuple[List[TextItem], str]:
        """截屏 + OCR，返回 (文字项列表, 截图路径)。OCR 失败会自动重试一次。"""
        _, p = self.snapshot(tag)
        items = ocr_image(p)
        if not items:
            time.sleep(0.8)
            _, p = self.snapshot(tag + "_retry")
            items = ocr_image(p)
        return items, p

    # ---- 输入 ----

    def tap(self, x: int, y: int, delay: float = 0.6) -> None:
        self.raw("shell", "input", "tap", str(int(x)), str(int(y)))
        if delay:
            time.sleep(delay)

    def tap_item(self, item: TextItem, delay: float = 0.6) -> None:
        self.tap(item.center[0], item.center[1], delay)

    def tap_text(self, items: Sequence[TextItem], *keywords: str, delay: float = 0.6) -> bool:
        it = find_text(items, *keywords)
        if it is None:
            return False
        self.tap_item(it, delay)
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        self.raw("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(0.6)

    def long_press(self, x: int, y: int, ms: int = 1200) -> None:
        self.raw("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ms))
        time.sleep(0.8)

    def key(self, code: str, delay: float = 0.4) -> None:
        self.raw("shell", "input", "keyevent", code)
        if delay:
            time.sleep(delay)

    def back(self, delay: float = 0.8) -> None:
        """注意：游戏里 BACK 会弹「确定退出率土之滨」，调用方必须能处理该弹窗。"""
        self.key("KEYCODE_BACK", delay)

    # ---- 应用 ----

    def launcher_activity(self, pkg: str = GAME_PKG) -> str:
        """问系统这个包的启动 Activity，形如 pkg/.SomeActivity。查不到返回空串。"""
        out = self.raw("shell", "cmd", "package", "resolve-activity", "--brief", pkg,
                       check=False)
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and pkg in line:
                return line
        return ""

    def launch(self, pkg: str = GAME_PKG) -> None:
        """启动游戏。

        优先用 `am start -n <pkg>/<activity>`：它能明确指定 Activity，
        而且游戏已经在后台时是「拉到前台」而不是重新走一遍开屏。
        monkey 作为兜底（老安卓 / resolve 失败时）。
        """
        act = self.launcher_activity(pkg)
        if act:
            self.raw("shell", "am", "start", "-n", act, check=False)
            return
        self.raw("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1",
                 check=False)

    def force_stop(self, pkg: str = GAME_PKG) -> None:
        self.raw("shell", "am", "force-stop", pkg, check=False)

    def foreground(self) -> str:
        out = self.raw("shell", "dumpsys", "window", check=False)
        for line in out.splitlines():
            if "mCurrentFocus" in line:
                return line.strip()
        return ""

    def foreground_pkg(self) -> str:
        """从 mCurrentFocus 里解析出**当前前台窗口所属的包名**。读不到返回空串。

        实测（2026-09-19，注意这条和「控件树」的结论不同）：
        游戏内这一行**恒定**是
            mCurrentFocus=Window{598f027 u0 com.netease.stzb.netease/com.netease.stzb.Client}
        50/50 帧完全一致，**不随游戏内界面切换而变** —— 所以它**不能**用来
        区分「在主城还是在内政还是税收面板」（那是画面层的事）。
        但它能可靠回答**另一个**问题：**游戏到底在不在前台**。
        这正是本项目的真实薄弱点：模拟器起着、游戏没起来时，OCR 读到的可能是
        桌面/启动器，而脚本会一路盲点右上角直到 300 秒超时（实测空转 7.8 分钟）。

        格式形如：
            mCurrentFocus=Window{8e31bf4 u0 app.lawnchair/app.lawnchair.Launcher}
            mCurrentFocus=null
        所以取 Window{...} 里的 `<pkg>/<activity>`，再切出包名。
        """
        line = self.foreground()
        if not line:
            return ""
        m = re.search(r"Window\{[^}]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", line)
        if m:
            return m.group(1)
        m = re.search(r"\s([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", line)
        return m.group(1) if m else ""

    def foreground_activity(self) -> str:
        """当前前台的 Activity 全名（形如 pkg/.SomeActivity）。读不到返回空串。"""
        line = self.foreground()
        m = re.search(r"Window\{[^}]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", line)
        return "%s/%s" % (m.group(1), m.group(2)) if m else ""

    def game_foreground(self, pkg: str = GAME_PKG) -> bool:
        """游戏是不是**真的在前台**。用于点击前的把关，防止对着桌面/启动器瞎点。

        读不到前台信息时返回 True（宁可放行也不误拦）—— 这条是「不确定就放行」，
        因为它的用途是防止明显的空转，而不是安全阀；安全阀另有 never_tap 管。
        """
        p = self.foreground_pkg()
        if not p:
            return True
        return p == pkg

    def game_running(self, pkg: str = GAME_PKG) -> bool:
        out = self.raw("shell", "pidof", pkg, check=False).strip()
        return bool(out)


# --------------------------------------------------------------------------- 图像工具

def write_png(path: str, img: np.ndarray) -> None:
    """写 PNG。cv2.imwrite 在含中文的路径上会静默失败，必须走 imencode。"""
    _need_vision("写 PNG")
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise AdbError("PNG 编码失败")
    buf.tofile(path)


def read_png(path: str) -> Optional[np.ndarray]:
    if not os.path.exists(path):
        return None
    _need_vision("读 PNG")
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def ocr_image_scaled(path: str, factor: int = 2,
                     tmp_path: Optional[str] = None) -> List[TextItem]:
    """把截图放大若干倍再 OCR，坐标除以倍数还原。

    微软 OCR 在字号偏小时（游戏里的按钮文字高度常只有 20px 上下）容易整条漏读。
    放大 2 倍实测能让这些按钮文字重新被读到——这是「OCR 失败就用别的方法」里的
    第三重手段（前两重是同帧多帧合并、颜色识别）。
    """
    img = read_png(path)
    if img is None:
        return []
    big = cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    tmp = tmp_path or (path + ".x%d.png" % factor)
    try:
        write_png(tmp, big)
    except Exception:
        return []
    items = ocr_image(tmp)
    return [TextItem(it.text,
                     tuple(int(v / factor) for v in it.box),   # type: ignore[arg-type]
                     (int(it.center[0] / factor), int(it.center[1] / factor)))
            for it in items]


def crop(img: np.ndarray, box: Tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = box
    return img[y1:y2, x1:x2]


def ocr_region_scaled(path: str, box: Tuple[int, int, int, int], *,
                      factor: float = 2.5,
                      tmp_path: Optional[str] = None) -> List[TextItem]:
    """只 OCR 截图中的一个区域，并先放大再识别；坐标已还原到原图坐标系。

    为什么需要它（2026-09-19 实测踩到）：
      游戏**冷启动**后停在标题页，那行「点击以开始游戏」是**金底金字**，
      全屏 OCR 直接返回 0 行 —— 脚本认不出这是要点的启动页，干等到超时，
      整轮任务全废。把底部那条带裁出来放大 2.5 倍后，OCR 能读出
      「过`击以开：始：．戏》」（脏但可模糊匹配）。
      所以「整屏读不出」时，要能退一步到「按区域放大再读」。

    与 ocr_image_scaled 的分工：
      · ocr_image_scaled 放**整图**——用于按钮文字偏小（高度 ~20px）被漏读；
      · 本函数放**局部**——用于整图对比度过低时，区域放大能显著提升信噪比。
    注意别裁得太小：Windows 原生 OCR 对小于约 480x270 的图会静默返回 0 行，
    裁完再放大也救不回来（ocr_image_scaled 的注释里也记了同一条）。
    """
    img = read_png(path)
    if img is None:
        return []
    x1, y1, x2, y2 = box
    h, w = img.shape[:2]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(w, int(x2)), min(h, int(y2))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return []
    piece = img[y1:y2, x1:x2]
    big = cv2.resize(piece, None, fx=factor, fy=factor,
                     interpolation=cv2.INTER_CUBIC)
    tmp = tmp_path or (path + ".r%.1f.png" % factor)
    try:
        write_png(tmp, big)
    except Exception:
        return []
    items = ocr_image(tmp)
    out: List[TextItem] = []
    for it in items:
        b = tuple(int(v / factor) for v in it.box)
        # 平移回原图坐标系
        b = (b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1)
        c = (int((b[0] + b[2]) / 2), int((b[1] + b[3]) / 2))
        out.append(TextItem(it.text, b, c))
    return out


class Templates:
    """从 templates/ 目录加载小图，用于在截图里定位固定 UI 元素（多尺度）。"""

    def __init__(self, folder: str, threshold: float = 0.82,
                 scales: Sequence[float] = (1.0, 0.95, 1.05, 0.9, 1.1)):
        self.folder = folder
        self.threshold = threshold
        self.scales = list(scales)
        self._cache: dict = {}

    def load(self, name: str) -> Optional[np.ndarray]:
        if name not in self._cache:
            p = os.path.join(self.folder, name if name.lower().endswith(".png") else name + ".png")
            self._cache[name] = read_png(p)
        return self._cache[name]

    def find(self, screen: np.ndarray, name: str,
             roi: Optional[Tuple[int, int, int, int]] = None,
             threshold: Optional[float] = None
             ) -> Optional[Tuple[int, int, float]]:
        """返回 (中心x, 中心y, 相似度) 或 None。roi = (x, y, w, h)。

        threshold 传了就覆盖实例默认值——底图有噪声的模板（比如游戏标题页的
        艺术字）用自己的实测阈值更稳，不必迁就全局的 0.82。
        """
        _need_vision("模板匹配")
        tpl0 = self.load(name)
        if tpl0 is None:
            return None
        thr = self.threshold if threshold is None else threshold
        sx, sy = 0, 0
        img = screen
        if roi:
            rx, ry, rw, rh = roi
            img = screen[ry:ry + rh, rx:rx + rw]
            sx, sy = rx, ry
        best = None
        for sc in self.scales:
            if sc != 1.0:
                tpl = cv2.resize(tpl0, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            else:
                tpl = tpl0
            if img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
                continue
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            if best is None or maxv > best[2]:
                best = (sx + maxloc[0] + tpl.shape[1] // 2,
                        sy + maxloc[1] + tpl.shape[0] // 2,
                        float(maxv))
        if best is None or best[2] < thr:
            return None
        return best

    def exists(self, screen: np.ndarray, name: str,
               roi: Optional[Tuple[int, int, int, int]] = None) -> bool:
        return self.find(screen, name, roi) is not None


# --------------------------------------------------------------------------- 小工具

def wait_until(predicate: Callable[[], object], timeout: float = 30.0,
               interval: float = 1.0) -> bool:
    """轮询等待 predicate 返回真值。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def merge_items(groups: Sequence[Sequence[TextItem]]) -> List[TextItem]:
    """把多帧 OCR 结果合并去重。

    游戏里有些面板（特性/税收）文字盖在会缓慢移动的插画上，OCR 在某些帧会
    整行糊掉；多截几帧取并集能显著提高识别率。
    """
    out: List[TextItem] = []
    for items in groups:
        for it in items:
            t = norm(it.text)
            if not t:
                continue
            dup = False
            for o in out:
                if norm(o.text) != t and match_score(o.text, it.text) < 0.97:
                    continue
                if abs(o.center[0] - it.center[0]) <= 45 and abs(o.center[1] - it.center[1]) <= 34:
                    dup = True
                    break
            if not dup:
                out.append(it)
    return out


def find_colored_buttons(img: np.ndarray, roi: Tuple[int, int, int, int],
                         lower: Sequence[int], upper: Sequence[int],
                         min_area: int = 400) -> List[Point]:
    """在 roi 里找指定颜色的连通块中心（用于「绿色免费按钮」这类颜色特征兜底）。"""
    _need_vision("颜色识别")
    x, y, w, h = roi
    sub = img[y:y + h, x:x + w]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if a < min_area:
            continue
        bx, by, bw, bh = cv2.boundingRect(c)
        out.append((x + bx + bw // 2, y + by + bh // 2))
    out.sort(key=lambda p: (p[1], p[0]))
    return out
